import com.intellij.openapi.actionSystem.ActionManager
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.openapi.project.ProjectManager
import com.intellij.notification.Notification
import com.intellij.notification.NotificationType
import com.intellij.notification.Notifications
import java.io.FileWriter

// Python's poll loop reads ``leapResultPath`` to decide whether to
// retry or report success.  Sentinels:
//   QUEUED  - project found, tab activation queued on the EDT
//   WAITING - project_path was set but isn't in getOpenProjects() yet
// We write QUEUED synchronously before returning to ``ideScript``
// (which always returns 0 regardless of script outcome — see the
// design note in ``_open_jetbrains_terminal``).
//
// FileWriter calls are inlined (not wrapped in a closure) — Groovy
// closures defined at the script's top-level binding scope failed
// to compile under JetBrains' ``ideScript`` runner, with no error
// surfaced to subprocess stderr.  Two inline writes is fine.
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
var actionManager = ActionManager.getInstance()

IDE.application.invokeLater {
    if (finalProject != null) {
        // Activate the Terminal tool window in the project
        var toolWindowManager = ToolWindowManager.getInstance(finalProject)
        var terminalWindow = toolWindowManager.getToolWindow("Terminal")

        if (terminalWindow != null) {
            // Show the Terminal window
            terminalWindow.show(null)

            // Try to find and activate the specific terminal tab by name
            var terminalTabName = System.getenv("LEAP_TERMINAL_TITLE")
            var foundTab = false

            if (terminalTabName != null && !terminalTabName.isEmpty()) {
                try {
                    var contentManager = terminalWindow.getContentManager()
                    // First try exact match
                    var content = contentManager.findContent(terminalTabName)

                    // Fallback: JetBrains truncates long Content display
                    // names with a Unicode ellipsis (U+2026), e.g.
                    // "lps mani\u2026-error-handling".  Split on the
                    // ellipsis and check prefix/suffix against our title.
                    if (content == null) {
                        var contents = contentManager.getContents()
                        var bestLen = 0
                        var matchCount = 0
                        for (var i = 0; i < contents.length; i++) {
                            var c = contents[i]
                            var name = c.getDisplayName()
                            if (name == null) continue
                            var matched = false
                            var matchLen = name.length()
                            var ellIdx = name.indexOf("\u2026")
                            if (ellIdx >= 0) {
                                // Truncated name - match prefix + suffix.
                                // Strip JetBrains' " (N)" dedup suffix before matching.
                                var prefix = name.substring(0, ellIdx)
                                var suffix = name.substring(ellIdx + 1)
                                    .replaceFirst("\\s+\\(\\d+\\)\$", "")
                                matched = terminalTabName.startsWith(prefix) && terminalTabName.endsWith(suffix)
                                // Score by effective match length (excluding ellipsis and dedup suffix)
                                matchLen = prefix.length() + suffix.length()
                            } else {
                                // Non-truncated - exact contains check
                                matched = terminalTabName.contains(name)
                            }
                            if (matched) {
                                if (matchLen > bestLen) {
                                    content = c
                                    bestLen = matchLen
                                    matchCount = 1
                                } else if (matchLen == bestLen) {
                                    matchCount++
                                }
                            }
                        }
                        // Ambiguous: multiple tabs matched with same score
                        if (matchCount > 1) {
                            content = null
                            try {
                                Notifications.Bus.notify(new Notification(
                                    "Leap", "Leap",
                                    "Multiple terminal tabs match '${terminalTabName}'. " +
                                    "Use shorter or more distinct tag names to avoid ambiguity.",
                                    NotificationType.WARNING
                                ))
                            } catch (Exception ne) {
                                // Notification API may not be available
                            }
                        }
                    }

                    if (content != null) {
                        // Found the tab with matching name - select it
                        contentManager.setSelectedContent(content)
                        foundTab = true
                    }
                } catch (Exception e) {
                    // Continue to fallback if tab search fails
                }
            }

            // If we didn't find a specific tab, just activate the terminal window
            if (!foundTab) {
                terminalWindow.activate(null)
            }
        }
    }
}

// Synchronous — runs before ``ideScript`` returns.  The EDT work
// above is queued; even though it hasn't rendered yet, the project
// was loaded and we've handed the request off.  Python's poll
// treats this as success.
if (leapResultPath != null && !leapResultPath.isEmpty()) {
    try {
        var queuedFw = new FileWriter(leapResultPath)
        queuedFw.write("QUEUED")
        queuedFw.close()
    } catch (Exception ignored) { }
}
