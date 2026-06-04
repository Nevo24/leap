import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.openapi.project.ProjectManager
import com.intellij.notification.Notification
import com.intellij.notification.NotificationType
import com.intellij.notification.Notifications
import java.io.FileWriter

// Python's poll loop reads ``leapResultPath`` to decide whether to
// retry or report success.  Sentinels:
//   QUEUED  - project found AND the target tab was located (its selection
//             is queued on the EDT). Also written when no specific tab was
//             requested (caller just wants the Terminal panel surfaced).
//   NOTAB   - project found but the requested tab could NOT be located
//             (e.g. the user renamed it in the IDE so it no longer carries
//             the "lps <tag>" title, and the PID fallback didn't match
//             either). Python treats this as a definitive failure and stops
//             retrying — previously this silently reported QUEUED, so the
//             jump button "succeeded" while landing the user nowhere.
//   WAITING - project_path was set but isn't in getOpenProjects() yet.
//
// The tab search runs SYNCHRONOUSLY here (not inside invokeLater) because
// Python must know whether the tab was found BEFORE we write the sentinel,
// and ContentManager / TerminalToolWindowManager reads are safe to call
// directly from the ideScript thread. Only the UI mutation (show +
// setSelectedContent + activate) is deferred to the EDT via invokeLater.
//
// FileWriter calls are inlined (not wrapped in a closure) — Groovy closures
// defined at the script's top-level binding scope failed to compile under
// JetBrains' ``ideScript`` runner, with no error surfaced to subprocess
// stderr. The inline writes are fine.
var leapResultPath = System.getenv("LEAP_RESULT_PATH")

// Synchronous project lookup (NOT inside invokeLater) so a missing
// project can short-circuit with WAITING before any EDT work is
// queued — Python will then retry.
var allProjects = ProjectManager.getInstance().getOpenProjects()
var targetProject = null
var projectPath = System.getenv("LEAP_PROJECT_PATH")

if (projectPath != null && !projectPath.isEmpty()) {
    for (var i = 0; i < allProjects.length; i++) {
        var project = allProjects[i]
        var basePath = project.getBasePath()
        if (basePath != null && basePath.equals(projectPath)) {
            targetProject = project
            break
        }
    }
    // Note: deliberately no allProjects[0] fallback when
    // projectPath is set — would land in the wrong project window
    // during cold-start session-restore races.
} else if (allProjects.length > 0) {
    targetProject = allProjects[0]
}

if (targetProject == null) {
    if (leapResultPath != null && !leapResultPath.isEmpty()) {
        try {
            var waitingFw = new FileWriter(leapResultPath)
            waitingFw.write("WAITING")
            waitingFw.close()
        } catch (Exception ignored) { }
    }
    return
}

// ``finalProject`` (not a re-declared ``targetProject``) below —
// shadowing a script-body ``var`` from inside an ``invokeLater``
// closure compiles silently but the script never runs.  Took a
// bisect to find; renaming the inner reference makes Groovy happy.
var finalProject = targetProject

var terminalTabName = System.getenv("LEAP_TERMINAL_TITLE")
// PyCharm spawns a login shell per terminal tab; ``LEAP_SHELL_PID`` is the
// PID of the shell backing the session's tab (the server process's parent,
// computed by the monitor). It's the rename-proof identity for the PID
// fallback below — the title can be edited by the user, the PID can't.
var shellPidStr = System.getenv("LEAP_SHELL_PID")
var titleProvided = (terminalTabName != null && !terminalTabName.isEmpty())

// Acquire the Terminal tool window. Wrapped in try so an EDT/threading
// assertion (an Error on internal/EAP builds) can't escape and leave the
// script with no sentinel written — which would waste Python's whole 60 s
// poll budget. Stays null on any failure.
var terminalWindow = null
try {
    terminalWindow = ToolWindowManager.getInstance(finalProject).getToolWindow("Terminal")
} catch (Throwable twEx) {
    // Leave terminalWindow null; handled as WAITING just below.
}

// The project is open but its Terminal tool window isn't available yet — a
// brief gap while it registers during project post-open, or a transient
// failure acquiring it. This is NOT a definitive miss, so tell Python to
// retry (WAITING) rather than declaring the tab gone (NOTAB): otherwise a
// cold-start jump would fail spuriously before the tool window appears.
if (terminalWindow == null) {
    if (leapResultPath != null && !leapResultPath.isEmpty()) {
        try {
            var waitFw = new FileWriter(leapResultPath)
            waitFw.write("WAITING")
            waitFw.close()
        } catch (Exception ignored) { }
    }
    return
}

// Result of the synchronous search: the Content to select (or null), plus a
// flag distinguishing "ambiguous title" from "simply not found" for the
// notification text.
var matched = null
var ambiguous = false

if (titleProvided) {
    try {
        var contentManager = terminalWindow.getContentManager()
        // 1) Exact title match.
        matched = contentManager.findContent(terminalTabName)

        // 2) Fallback: JetBrains truncates long Content display names with a
        // Unicode ellipsis (U+2026), e.g. "lps mani…-error-handling".
        // Split on the ellipsis and check prefix/suffix against our title.
        if (matched == null) {
            var contents = contentManager.getContents()
            var bestLen = 0
            var matchCount = 0
            for (var i = 0; i < contents.length; i++) {
                var c = contents[i]
                var name = c.getDisplayName()
                if (name == null) continue
                var isMatch = false
                var matchLen = name.length()
                var ellIdx = name.indexOf(0x2026)
                if (ellIdx >= 0) {
                    // Truncated name - match prefix + suffix.
                    // Strip JetBrains' " (N)" dedup suffix before matching.
                    var prefix = name.substring(0, ellIdx)
                    var suffix = name.substring(ellIdx + 1)
                        .replaceFirst("\\s+\\(\\d+\\)\$", "")
                    isMatch = terminalTabName.startsWith(prefix) && terminalTabName.endsWith(suffix)
                    // Score by effective match length (excluding ellipsis and dedup suffix)
                    matchLen = prefix.length() + suffix.length()
                } else {
                    // Non-truncated - exact contains check
                    isMatch = terminalTabName.contains(name)
                }
                if (isMatch) {
                    if (matchLen > bestLen) {
                        matched = c
                        bestLen = matchLen
                        matchCount = 1
                    } else if (matchLen == bestLen) {
                        matchCount++
                    }
                }
            }
            // Ambiguous: multiple tabs matched with the same score.
            if (matchCount > 1) {
                matched = null
                ambiguous = true
            }
        }

        // 3) PID fallback. The tab may have been renamed in the IDE so it no
        // longer carries the "lps <tag>" title — the title match above can't
        // find it (or matched several tabs ambiguously). Identify it instead
        // by the shell process PyCharm spawned for that tab, which the monitor
        // passes as LEAP_SHELL_PID. The PID is unique, so it also resolves the
        // ambiguous-title case. Referenced fully-qualified (no top-level
        // import) so a missing terminal plugin only disables this fallback
        // rather than breaking the whole script.
        if (matched == null && shellPidStr != null && !shellPidStr.isEmpty()) {
            try {
                var targetPid = Long.parseLong(shellPidStr.trim())
                if (targetPid > 0) {
                    var mgr = org.jetbrains.plugins.terminal.TerminalToolWindowManager.getInstance(finalProject)
                    var contents = contentManager.getContents()
                    for (var i = 0; i < contents.length; i++) {
                        var c = contents[i]
                        // Per-tab try: a non-local tab (SSH/remote/WSL) may not
                        // expose a local process, so getProcessTtyConnector /
                        // getProcess can throw. Skip that tab rather than
                        // aborting the whole search and missing a later tab
                        // that does match.
                        try {
                            var w = mgr.getWidgetByContent(c)
                            if (w == null) continue
                            var tc = w.getProcessTtyConnector()
                            if (tc == null) continue
                            var proc = tc.getProcess()
                            if (proc != null && proc.pid() == targetPid) {
                                matched = c
                                break
                            }
                        } catch (Throwable perTab) {
                            // This tab can't report a PID — move on.
                        }
                    }
                }
            } catch (Throwable pidEx) {
                // PID fallback setup failed (e.g. terminal plugin absent on
                // this IDE); leave ``matched`` as-is and report NOTAB below.
            }
        }
    } catch (Throwable searchEx) {
        // Search failed — treat as not found; the sentinel below reports NOTAB
        // and the EDT block still surfaces the Terminal panel. Catch Throwable
        // (not just Exception) so an IDE threading assertion — an Error, raised
        // on internal/EAP builds if these reads ever require the EDT — degrades
        // to a clean NOTAB instead of an uncaught Error that writes no sentinel
        // (which would hang Python's poll until the 60 s budget runs out).
    }
}

var foundTab = (matched != null)
var contentToSelect = matched

// Defer all UI mutation to the EDT.
IDE.application.invokeLater {
    if (terminalWindow != null) {
        terminalWindow.show(null)
        if (contentToSelect != null) {
            terminalWindow.getContentManager().setSelectedContent(contentToSelect)
        }
        // Bring the tool window forward regardless — even on a miss the user
        // at least lands on the Terminal panel rather than nothing.
        terminalWindow.activate(null)
    }
}

// Tell the user when a specific tab was requested but couldn't be located —
// otherwise the failure is invisible.
if (titleProvided && !foundTab) {
    try {
        var msg = ambiguous
            ? "Multiple terminal tabs match '${terminalTabName}'. Use shorter or more distinct tag names to avoid ambiguity."
            : "Couldn't find terminal tab '${terminalTabName}'. If you renamed it in the IDE, rename it back to that or re-run leap for the session."
        Notifications.Bus.notify(new Notification(
            "Leap", "Leap", msg, NotificationType.WARNING
        ))
    } catch (Exception ne) {
        // Notification API may not be available — ignore.
    }
}

// Synchronous sentinel write — runs before ``ideScript`` returns. The EDT
// work above is queued; the sentinel reflects whether we actually located
// the requested tab (or that no specific tab was requested).
if (leapResultPath != null && !leapResultPath.isEmpty()) {
    try {
        var resultFw = new FileWriter(leapResultPath)
        resultFw.write((foundTab || !titleProvided) ? "QUEUED" : "NOTAB")
        resultFw.close()
    } catch (Exception ignored) { }
}
