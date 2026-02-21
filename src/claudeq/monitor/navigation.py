"""
IDE navigation for ClaudeQ monitor.

Handles navigating to terminal tabs in various IDEs.
"""

import glob
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# JetBrains IDE names used across navigation functions
_JETBRAINS_IDE_NAMES: list[str] = [
    'PyCharm', 'IntelliJ', 'GoLand', 'WebStorm', 'PhpStorm',
    'RubyMine', 'CLion', 'DataGrip', 'JetBrains',
]

# Maps IDE display names to their CLI command names
_IDE_CMD_MAP: dict[str, str] = {
    'PyCharm': 'pycharm',
    'IntelliJ IDEA': 'idea',
    'GoLand': 'goland',
    'WebStorm': 'webstorm',
    'PhpStorm': 'phpstorm',
}

# Glob patterns for JetBrains .app bundles in /Applications
_JETBRAINS_APP_PATTERNS: list[str] = [
    'IntelliJ*.app', 'PyCharm*.app', 'WebStorm*.app',
    'PhpStorm*.app', 'GoLand*.app', 'RubyMine*.app',
    'CLion*.app', 'DataGrip*.app', 'Rider*.app', 'Fleet*.app',
]


def _jetbrains_env() -> dict[str, str]:
    """Build an env dict with JetBrains CLI tools on PATH."""
    env = os.environ.copy()
    jetbrains_paths: list[str] = []
    for pattern in _JETBRAINS_APP_PATTERNS:
        for app in glob.glob(f'/Applications/{pattern}'):
            jetbrains_paths.append(f'{app}/Contents/MacOS')
    if jetbrains_paths:
        env['PATH'] = ':'.join(jetbrains_paths) + ':' + env.get('PATH', '')
    return env


def _vscode_env_and_path() -> tuple[dict[str, str], Optional[str]]:
    """Build an env dict with VS Code CLI on PATH and return the code binary path."""
    env = os.environ.copy()
    extra_paths = ['/usr/local/bin', '/opt/homebrew/bin']
    current_path = env.get('PATH', '')
    for p in extra_paths:
        if p not in current_path and os.path.exists(p):
            env['PATH'] = f"{p}:{current_path}"
            current_path = env['PATH']
    code_path = shutil.which('code', path=env.get('PATH'))
    return env, code_path


def _escape_groovy(s: str) -> str:
    """Escape a string for safe interpolation in a Groovy double-quoted string."""
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')


def _escape_applescript(s: str) -> str:
    """Escape a string for safe interpolation in an AppleScript double-quoted string."""
    return s.replace('\\', '\\\\').replace('"', '\\"')


def open_terminal_with_command(
    command: str,
    preferred_ide: Optional[str] = None,
    project_path: Optional[str] = None,
) -> bool:
    """
    Open a new terminal tab and run a command in it.

    Opens exclusively in the preferred IDE/terminal when known.
    Falls back to Terminal.app then iTerm2 only when preferred_ide is unknown.

    Args:
        command: Command to execute in the new terminal.
        preferred_ide: IDE or terminal app to open in (from session metadata).
        project_path: Project path for IDE navigation.

    Returns:
        True if a new terminal was opened successfully.
    """
    if preferred_ide:
        # Try the specific IDE first. If it fails, fall through to generic
        # fallback so that a terminal always opens somewhere.
        if any(ide in preferred_ide for ide in _JETBRAINS_IDE_NAMES):
            if _open_jetbrains_terminal(preferred_ide, project_path, command):
                return True
        elif 'VS Code' in preferred_ide:
            if _open_vscode_terminal(project_path, command):
                return True
        elif preferred_ide == 'iTerm2':
            if _open_iterm2_terminal(command):
                return True
        elif preferred_ide == 'Terminal.app':
            if _open_terminal_app_terminal(command):
                return True
        elif preferred_ide == 'Warp':
            if _open_warp_terminal(command):
                return True

    # Preferred IDE failed or unknown — try Terminal.app then iTerm2
    if _open_terminal_app_terminal(command):
        return True
    return _open_iterm2_terminal(command)


def close_terminal_with_title(
    title_pattern: str,
    preferred_ide: Optional[str] = None,
    project_path: Optional[str] = None,
    terminal_title: Optional[str] = None
) -> bool:
    """
    Close terminal window/tab with matching title.

    Args:
        title_pattern: Pattern to match in terminal title.
        preferred_ide: Preferred IDE to try first.
        project_path: Project path for IDE navigation.
        terminal_title: Exact terminal title to match.

    Returns:
        True if terminal was found and closed.
    """
    if preferred_ide and any(ide in preferred_ide for ide in _JETBRAINS_IDE_NAMES):
        if _close_jetbrains(preferred_ide, project_path, terminal_title):
            return True

    if preferred_ide and 'VS Code' in preferred_ide:
        if _close_vscode(project_path, terminal_title or title_pattern):
            return True

    if _close_terminal_app(title_pattern):
        return True

    if _close_iterm2(title_pattern):
        return True

    if _close_warp(title_pattern):
        return True

    return False


def find_terminal_with_title(
    title_pattern: str,
    preferred_ide: Optional[str] = None,
    project_path: Optional[str] = None,
    terminal_title: Optional[str] = None
) -> bool:
    """
    Find and focus terminal window/tab with matching title.

    Args:
        title_pattern: Pattern to match in terminal title.
        preferred_ide: Preferred IDE to try first.
        project_path: Project path for IDE navigation.
        terminal_title: Exact terminal title to match.

    Returns:
        True if terminal was found and focused.
    """
    # Try JetBrains IDEs first
    if preferred_ide and any(ide in preferred_ide for ide in _JETBRAINS_IDE_NAMES):
        if _navigate_jetbrains(preferred_ide, project_path, terminal_title):
            return True

    # Try VS Code
    if preferred_ide and 'VS Code' in preferred_ide:
        if _navigate_vscode(project_path, terminal_title or title_pattern):
            return True

    # Try Terminal.app
    if _navigate_terminal_app(title_pattern):
        return True

    # Try iTerm2
    if _navigate_iterm2(title_pattern):
        return True

    # Try Warp
    if _navigate_warp(title_pattern):
        return True

    return False


def _navigate_jetbrains(
    ide: str,
    project_path: Optional[str],
    terminal_title: Optional[str]
) -> bool:
    """Navigate to terminal in JetBrains IDE."""
    script_dir = Path(__file__).parent
    groovy_script = script_dir / "resources" / "activate_terminal.groovy"

    # Check for groovy script in Contents/Resources if running from .app bundle
    if not groovy_script.exists():
        for parent in Path(__file__).parents:
            if parent.name == 'Resources' and parent.parent.name == 'Contents':
                groovy_script = parent / "activate_terminal.groovy"
                break

    if not groovy_script.exists():
        return False

    ide_cmd = _IDE_CMD_MAP.get(ide)
    if not ide_cmd:
        return False

    try:
        # Read template and substitute values
        with open(groovy_script, 'r') as f:
            template_content = f.read()

        custom_script = template_content
        if project_path:
            custom_script = custom_script.replace(
                'var projectPath = System.getenv("CLAUDEQ_PROJECT_PATH")',
                f'var projectPath = "{_escape_groovy(project_path)}"'
            )
        if terminal_title:
            custom_script = custom_script.replace(
                'var terminalTabName = System.getenv("CLAUDEQ_TERMINAL_TITLE")',
                f'var terminalTabName = "{_escape_groovy(terminal_title)}"'
            )

        with tempfile.NamedTemporaryFile(mode='w', suffix='.groovy', delete=False) as tmp:
            tmp.write(custom_script)
            tmp_script_path = tmp.name

        try:
            env = _jetbrains_env()

            # First, open/focus the project if we have a project path
            if project_path:
                subprocess.run(
                    [ide_cmd, project_path],
                    capture_output=True,
                    env=env,
                    timeout=5
                )
                time.sleep(0.3)

            # Then run the groovy script to activate terminal
            result = subprocess.run(
                [ide_cmd, 'ideScript', tmp_script_path],
                capture_output=True,
                timeout=5,
                env=env
            )
            if result.returncode == 0:
                return True
        finally:
            try:
                os.unlink(tmp_script_path)
            except OSError:
                pass
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _navigate_vscode(project_path: Optional[str], terminal_name: str) -> bool:
    """Navigate to VS Code window and select terminal tab by name."""
    try:
        env, code_path = _vscode_env_and_path()
        if not code_path:
            return False

        # Open project (focuses the window)
        if project_path:
            subprocess.run(
                [code_path, project_path],
                capture_output=True,
                timeout=5,
                env=env
            )
            time.sleep(0.3)

        # Use file-based trigger for ClaudeQ extension
        # Extension watches ~/.claudeq-terminal-request and selects the terminal
        request_file = os.path.expanduser('~/.claudeq-terminal-request')
        try:
            with open(request_file, 'w') as f:
                f.write(terminal_name)
            # Give the extension a moment to process
            time.sleep(0.1)
        except OSError:
            pass

        return True

    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _navigate_terminal_app(title_pattern: str) -> bool:
    """Navigate to terminal in Terminal.app."""
    safe_pattern = _escape_applescript(title_pattern)
    script = f'''
    tell application "Terminal"
        repeat with w in windows
            repeat with t from 1 to count of tabs of w
                set tabName to custom title of tab t of w
                if tabName contains "{safe_pattern}" then
                    set frontmost of w to true
                    set selected of tab t of w to true
                    activate
                    return true
                end if
            end repeat
        end repeat
    end tell
    return false
    '''

    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0 and 'true' in result.stdout
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _navigate_iterm2(title_pattern: str) -> bool:
    """Navigate to terminal in iTerm2."""
    safe_pattern = _escape_applescript(title_pattern)
    script = f'''
    tell application "iTerm"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if name of s contains "{safe_pattern}" then
                        select w
                        select t
                        select s
                        activate
                        return true
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return false
    '''

    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0 and 'true' in result.stdout
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _close_jetbrains(
    ide: str,
    project_path: Optional[str],
    terminal_title: Optional[str]
) -> bool:
    """Close a terminal tab in JetBrains IDE."""
    if not terminal_title:
        return False

    ide_cmd = _IDE_CMD_MAP.get(ide)
    if not ide_cmd:
        return False

    project_match = ""
    if project_path:
        project_match = f'''
    for (var i = 0; i < allProjects.length; i++) {{
        var project = allProjects[i]
        if (project.getBasePath() != null && project.getBasePath().equals("{_escape_groovy(project_path)}")) {{
            targetProject = project
            break
        }}
    }}'''

    groovy_script = f'''import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.openapi.project.ProjectManager

IDE.application.invokeLater {{
    var targetProject = null
    var allProjects = ProjectManager.getInstance().getOpenProjects()
    {project_match}
    if (targetProject == null && allProjects.length > 0) {{
        targetProject = allProjects[0]
    }}
    if (targetProject != null) {{
        var toolWindowManager = ToolWindowManager.getInstance(targetProject)
        var terminalWindow = toolWindowManager.getToolWindow("Terminal")
        if (terminalWindow != null) {{
            try {{
                var contentManager = terminalWindow.getContentManager()
                var content = contentManager.findContent("{_escape_groovy(terminal_title)}")
                if (content != null) {{
                    contentManager.removeContent(content, true)
                }}
            }} catch (Exception e) {{
            }}
        }}
    }}
}}
'''

    try:
        env = _jetbrains_env()

        with tempfile.NamedTemporaryFile(mode='w', suffix='.groovy', delete=False) as tmp:
            tmp.write(groovy_script)
            tmp_script_path = tmp.name

        try:
            result = subprocess.run(
                [ide_cmd, 'ideScript', tmp_script_path],
                capture_output=True,
                timeout=5,
                env=env
            )
            return result.returncode == 0
        finally:
            try:
                os.unlink(tmp_script_path)
            except OSError:
                pass
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _close_vscode(project_path: Optional[str], terminal_name: str) -> bool:
    """Close a terminal tab in VS Code by writing a close request file."""
    try:
        env, code_path = _vscode_env_and_path()
        if not code_path:
            return False

        if project_path:
            subprocess.run(
                [code_path, project_path],
                capture_output=True,
                timeout=5,
                env=env
            )
            time.sleep(0.3)

        request_file = os.path.expanduser('~/.claudeq-terminal-request')
        with open(request_file, 'w') as f:
            f.write(f'close:{terminal_name}')
        time.sleep(0.1)
        return True
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _close_terminal_app(title_pattern: str) -> bool:
    """Close a terminal tab in Terminal.app."""
    safe_pattern = _escape_applescript(title_pattern)
    script = f'''
    tell application "Terminal"
        repeat with w in windows
            set tabCount to count of tabs of w
            repeat with t from 1 to tabCount
                set tabName to custom title of tab t of w
                if tabName contains "{safe_pattern}" then
                    if tabCount is 1 then
                        close w
                    else
                        set frontmost of w to true
                        set selected of tab t of w to true
                        activate
                        tell application "System Events"
                            tell process "Terminal"
                                keystroke "w" using command down
                            end tell
                        end tell
                    end if
                    return true
                end if
            end repeat
        end repeat
    end tell
    return false
    '''

    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0 and 'true' in result.stdout
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _close_iterm2(title_pattern: str) -> bool:
    """Close all iTerm2 sessions whose name contains the pattern."""
    safe_pattern = _escape_applescript(title_pattern)
    script = f'''
    tell application "iTerm"
        set found to false
        -- Collect matching session IDs first, then close (avoids
        -- mutating the list while iterating).
        set toClose to {{}}
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if name of s contains "{safe_pattern}" then
                        set end of toClose to s
                    end if
                end repeat
            end repeat
        end repeat
        repeat with s in toClose
            close s
            set found to true
        end repeat
        return found
    end tell
    '''

    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0 and 'true' in result.stdout
    except (subprocess.SubprocessError, OSError):
        pass

    return False


_WARP_BUNDLE_ID = 'dev.warp.Warp-Stable'


def _write_warp_diagnostic(msg: str) -> None:
    """Write a diagnostic line to .storage/warp_nav_diag.txt for debugging."""
    try:
        from claudeq.utils.constants import STORAGE_DIR
        diag_file = STORAGE_DIR / 'warp_nav_diag.txt'
        with open(diag_file, 'a') as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except (ImportError, OSError):
        pass


def _get_app_pid(bundle_id: str) -> Optional[int]:
    """Get PID for a running app by bundle identifier.

    Uses NSWorkspace iteration instead of
    runningApplicationsWithBundleIdentifier_ because the latter can
    return empty results when called from a background thread in a
    py2app bundle.
    """
    try:
        import AppKit
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        for app in workspace.runningApplications():
            if app.bundleIdentifier() == bundle_id:
                return app.processIdentifier()
    except ImportError:
        _write_warp_diagnostic("AppKit ImportError in _get_app_pid")
    except Exception as exc:
        _write_warp_diagnostic("_get_app_pid error: %s" % exc)
    return None


def _check_accessibility_trusted() -> bool:
    """Check if this process has Accessibility permission.

    If not trusted, triggers the macOS system prompt to request permission.
    This handles the case where the .app was rebuilt (changing its ad-hoc
    code signature) and the old Accessibility entry is now stale.
    """
    try:
        from ApplicationServices import AXIsProcessTrusted
        from CoreFoundation import kCFBooleanTrue
    except ImportError:
        _write_warp_diagnostic("FAIL: cannot import ApplicationServices or CoreFoundation")
        return False

    trusted = AXIsProcessTrusted()
    _write_warp_diagnostic("AXIsProcessTrusted() = %s" % trusted)
    if trusted:
        return True

    # Not trusted — trigger the system prompt so the user can re-authorize.
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions
        options = {"AXTrustedCheckOptionPrompt": kCFBooleanTrue}
        AXIsProcessTrustedWithOptions(options)
    except (ImportError, Exception):
        pass

    logger.warning("Accessibility permission not granted for this process. "
                    "Re-add ClaudeQ Monitor in System Settings > Privacy & "
                    "Security > Accessibility after rebuilding the app.")
    return False


def _send_keystroke(
    keycode: int, cmd: bool = False, shift: bool = False, ctrl: bool = False,
) -> bool:
    """Send a keystroke to the frontmost application using CGEvent."""
    try:
        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventSetFlags,
            CGEventPost,
            kCGHIDEventTap,
            kCGEventFlagMaskCommand,
            kCGEventFlagMaskShift,
            kCGEventFlagMaskControl,
        )
    except ImportError:
        return False

    flags = 0
    if cmd:
        flags |= kCGEventFlagMaskCommand
    if shift:
        flags |= kCGEventFlagMaskShift
    if ctrl:
        flags |= kCGEventFlagMaskControl

    key_down = CGEventCreateKeyboardEvent(None, keycode, True)
    key_up = CGEventCreateKeyboardEvent(None, keycode, False)
    CGEventSetFlags(key_down, flags)
    CGEventSetFlags(key_up, flags)
    CGEventPost(kCGHIDEventTap, key_down)
    CGEventPost(kCGHIDEventTap, key_up)
    return True


def _send_cmd_w() -> bool:
    """Send Cmd+W keystroke to close the active tab."""
    return _send_keystroke(13, cmd=True)  # keycode 13 = 'w'


def _navigate_warp(title_pattern: str) -> bool:
    """Navigate to a Warp tab whose title contains the pattern.

    Warp doesn't expose individual tabs in its accessibility tree, so
    the window title only reflects the currently active tab.  Strategy:
    1. Check all windows for a direct title match (active tab matches).
    2. If not found, raise each window and cycle through its tabs with
       Cmd+Shift+] until the title matches or we loop back to the start.
    """
    _write_warp_diagnostic("_navigate_warp called, pattern='%s'" % title_pattern)
    pid = _get_app_pid(_WARP_BUNDLE_ID)
    if pid is None:
        _write_warp_diagnostic("Warp not running (bundle_id=%s)" % _WARP_BUNDLE_ID)
        return False

    try:
        import AppKit
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
            AXUIElementPerformAction,
            kAXErrorSuccess,
        )
    except ImportError:
        _write_warp_diagnostic("FAIL: ImportError for ApplicationServices")
        return False

    if not _check_accessibility_trusted():
        return False

    app_ref = AXUIElementCreateApplication(pid)
    err, windows = AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
    if err != kAXErrorSuccess or not windows:
        _write_warp_diagnostic("AXWindows query: error=%d, pid=%d" % (err, pid))
        return False

    ns_app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)

    def _get_title(window_ref: object) -> str:
        e, t = AXUIElementCopyAttributeValue(window_ref, "AXTitle", None)
        return str(t) if e == kAXErrorSuccess and t else ''

    def _raise_and_activate(window_ref: object) -> None:
        AXUIElementPerformAction(window_ref, "AXRaise")
        if ns_app:
            ns_app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)

    # Phase 1: quick scan — check if any window's active tab already matches
    for window in windows:
        if title_pattern in _get_title(window):
            _raise_and_activate(window)
            _write_warp_diagnostic("MATCH (phase 1): pattern='%s'" % title_pattern)
            return True

    # Phase 2: raise each window and cycle through its tabs
    # Cmd+Shift+] = next tab in Warp  (keycode 30 = ']')
    _write_warp_diagnostic("Phase 2: cycling tabs in %d window(s)" % len(windows))
    for window in windows:
        _raise_and_activate(window)
        time.sleep(0.15)

        initial_title = _get_title(window)
        if not initial_title:
            continue

        for _ in range(20):  # safety cap
            _send_keystroke(30, cmd=True, shift=True)  # Cmd+Shift+]
            time.sleep(0.15)
            current_title = _get_title(window)
            if title_pattern in current_title:
                _write_warp_diagnostic(
                    "MATCH (phase 2): pattern='%s' in title='%s'"
                    % (title_pattern, current_title))
                return True
            if current_title == initial_title:
                break  # cycled back to start — tab not in this window

    _write_warp_diagnostic("NO MATCH after cycling all windows")
    # Phase 2 activated Warp to cycle tabs — switch back to the monitor
    try:
        import AppKit
        AppKit.NSRunningApplication.currentApplication().activateWithOptions_(
            AppKit.NSApplicationActivateIgnoringOtherApps)
    except (ImportError, Exception):
        pass
    return False


def _close_warp(title_pattern: str) -> bool:
    """Close a Warp tab whose title contains the pattern.

    Navigates to the matching tab (cycling if needed), then sends Cmd+W.
    """
    if _navigate_warp(title_pattern):
        time.sleep(0.2)
        return _send_cmd_w()
    return False


def _activate_warp() -> bool:
    """Bring Warp to front without Accessibility permission.

    Cannot target a specific window — just activates the application.
    Used as a fallback when Accessibility permission is not granted.
    """
    script = '''
    tell application "Warp" to activate
    return true
    '''
    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _open_warp_terminal(command: str) -> bool:
    """Open a new Warp tab and run a command.

    If Warp is already running and Accessibility is granted, opens a new
    tab in the frontmost Warp window using Cmd+T and pastes the command.
    Otherwise falls back to Launch Configurations (creates a new window).
    """
    pid = _get_app_pid(_WARP_BUNDLE_ID)
    if pid is not None and _check_accessibility_trusted():
        if _open_warp_tab_with_keystroke(pid, command):
            return True

    # Warp not running or keystroke approach failed — use Launch Configuration
    return _open_warp_via_launch_config(command)


def _open_warp_tab_with_keystroke(pid: int, command: str) -> bool:
    """Open a new tab in the frontmost Warp window and run a command.

    Uses Cmd+T to create the tab, waits for the shell to initialize by
    polling the window title for a change, dismisses Warp's "New terminal
    session" overlay, then pastes the command.  Includes a retry loop to
    handle timing variations in overlay appearance.
    Requires Accessibility permission.
    """
    try:
        import AppKit
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
            kAXErrorSuccess,
        )
    except ImportError:
        return False

    ns_app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if not ns_app:
        return False
    ns_app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    time.sleep(0.3)

    # Get the frontmost window and its current title (e.g. "cq-server tag")
    app_ref = AXUIElementCreateApplication(pid)
    err, windows = AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
    if err != kAXErrorSuccess or not windows:
        return False

    def _title() -> str:
        e, t = AXUIElementCopyAttributeValue(windows[0], "AXTitle", None)
        return str(t) if e == kAXErrorSuccess and t else ''

    old_title = _title()

    # Cmd+T — new tab in the frontmost window
    if not _send_keystroke(17, cmd=True):  # keycode 17 = 't'
        return False

    # Wait for the new tab's shell to initialize.  The window title will
    # change from the server tab's title (e.g. "cq-server tag") to the
    # new tab's default title (e.g. the cwd) once the shell is ready.
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if _title() != old_title:
            break

    # Copy command to clipboard (done once, reused across retries)
    try:
        proc = subprocess.run(
            ['pbcopy'], input=command.encode('utf-8'), timeout=2,
        )
        if proc.returncode != 0:
            return False
    except (subprocess.SubprocessError, OSError):
        return False

    # Warp shows a "New terminal session" overlay on new tabs that
    # captures Enter.  The overlay can appear at varying times after
    # the shell is ready.  Strategy: try Escape → paste → Enter, then
    # check the title to verify the command ran.  If it didn't, retry.
    for attempt in range(4):
        # Wait progressively longer for the overlay to appear
        time.sleep(0.3 if attempt == 0 else 0.8)
        _send_keystroke(53)            # Escape (dismiss overlay)
        time.sleep(0.2)
        _send_keystroke(32, ctrl=True) # Ctrl+U (clear input line)
        time.sleep(0.1)
        _send_keystroke(9, cmd=True)   # Cmd+V (paste into clean input)
        time.sleep(0.15)
        _send_keystroke(36)            # Return (execute)
        time.sleep(0.5)

        # Check if the command executed — cq sets the title to "cq-*"
        current = _title()
        if 'cq-' in current:
            return True

    return True  # Exhausted retries — command may still execute


def _open_warp_via_launch_config(command: str) -> bool:
    """Open a new Warp window via Launch Configuration.

    Creates a temporary YAML launch config in ~/.warp/launch_configurations/
    and opens it via the warp:// URI scheme.  Used when Warp is not running
    or Accessibility is unavailable.
    """
    config_name = f"claudeq-{uuid.uuid4().hex[:8]}"
    config_dir = Path.home() / ".warp" / "launch_configurations"
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    config_path = config_dir / f"{config_name}.yaml"

    # Extract cwd from "cd /path && ..." pattern, otherwise use home
    cwd = str(Path.home())
    if command.startswith('cd '):
        # Parse: cd '/some/path' && rest  or  cd /some/path && rest
        parts = command.split('&&', 1)
        cd_part = parts[0].strip()
        # Remove 'cd ' prefix and strip quotes
        cd_path = cd_part[3:].strip().strip("'\"")
        if cd_path:
            cwd = cd_path

    # Escape for YAML double-quoted strings
    escaped_cmd = command.replace('\\', '\\\\').replace('"', '\\"')
    escaped_cwd = cwd.replace('\\', '\\\\').replace('"', '\\"')

    yaml_content = (
        f'name: "{config_name}"\n'
        f'windows:\n'
        f'  - tabs:\n'
        f'      - layout:\n'
        f'          cwd: "{escaped_cwd}"\n'
        f'          commands:\n'
        f'            - exec: "{escaped_cmd}"\n'
    )

    try:
        config_path.write_text(yaml_content, encoding='utf-8')
        result = subprocess.run(
            ['open', f'warp://launch/{config_name}'],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            _cleanup_warp_config(config_path)
            return False
        # Clean up after Warp has had time to read the config
        _schedule_warp_config_cleanup(config_path)
        return True
    except (subprocess.SubprocessError, OSError):
        _cleanup_warp_config(config_path)

    return False


def _cleanup_warp_config(path: Path) -> None:
    """Remove a temporary Warp launch config file."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _schedule_warp_config_cleanup(path: Path) -> None:
    """Schedule removal of a temporary Warp launch config after a delay."""

    def _cleanup() -> None:
        time.sleep(3)
        _cleanup_warp_config(path)

    t = threading.Thread(target=_cleanup, daemon=True)
    t.start()


def _open_jetbrains_terminal(
    ide: str,
    project_path: Optional[str],
    command: str
) -> bool:
    """Open a new terminal tab in JetBrains IDE and run a command."""
    ide_cmd = _IDE_CMD_MAP.get(ide)
    if not ide_cmd:
        return False

    project_match = ""
    if project_path:
        project_match = f'''
    for (var i = 0; i < allProjects.length; i++) {{
        var project = allProjects[i]
        if (project.getBasePath() != null && project.getBasePath().equals("{_escape_groovy(project_path)}")) {{
            targetProject = project
            break
        }}
    }}'''

    groovy_script = f'''import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.openapi.project.ProjectManager
import org.jetbrains.plugins.terminal.TerminalToolWindowManager

IDE.application.invokeLater {{
    var targetProject = null
    var allProjects = ProjectManager.getInstance().getOpenProjects()
    {project_match}
    if (targetProject == null && allProjects.length > 0) {{
        targetProject = allProjects[0]
    }}
    if (targetProject != null) {{
        var terminalManager = TerminalToolWindowManager.getInstance(targetProject)
        var widget = terminalManager.createLocalShellWidget(targetProject.getBasePath(), "cq")
        // Short delay to let the shell initialize, then run the command
        new Thread({{
            Thread.sleep(500)
            IDE.application.invokeLater {{
                widget.executeCommand("{_escape_groovy(command)}")
            }}
        }} as Runnable).start()
    }}
}}
'''

    try:
        env = _jetbrains_env()

        if project_path:
            subprocess.run(
                [ide_cmd, project_path],
                capture_output=True,
                env=env,
                timeout=5
            )
            time.sleep(0.3)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.groovy', delete=False) as tmp:
            tmp.write(groovy_script)
            tmp_script_path = tmp.name

        try:
            result = subprocess.run(
                [ide_cmd, 'ideScript', tmp_script_path],
                capture_output=True,
                timeout=10,
                env=env
            )
            return result.returncode == 0
        finally:
            try:
                os.unlink(tmp_script_path)
            except OSError:
                pass
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _open_terminal_app_terminal(command: str) -> bool:
    """Open a new Terminal.app tab and run a command."""
    escaped = command.replace('\\', '\\\\').replace('"', '\\"')
    script = f'''
    tell application "Terminal"
        do script "{escaped}"
        activate
    end tell
    return true
    '''

    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _open_iterm2_terminal(command: str) -> bool:
    """Open a new iTerm2 tab and run a command."""
    escaped = command.replace('\\', '\\\\').replace('"', '\\"')
    script = f'''
    tell application "iTerm"
        tell current window
            create tab with default profile
            tell current session
                write text "{escaped}"
            end tell
        end tell
        activate
    end tell
    return true
    '''

    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _open_vscode_terminal(project_path: Optional[str], command: str) -> bool:
    """Open a new VS Code terminal tab and run a command."""
    try:
        env, code_path = _vscode_env_and_path()
        if not code_path:
            return False

        if project_path:
            subprocess.run(
                [code_path, project_path],
                capture_output=True,
                timeout=5,
                env=env
            )
            time.sleep(0.3)

        request_file = os.path.expanduser('~/.claudeq-terminal-request')
        with open(request_file, 'w') as f:
            f.write(f'open:{command}')
        time.sleep(0.1)
        return True
    except (subprocess.SubprocessError, OSError):
        pass

    return False
