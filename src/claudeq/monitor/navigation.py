"""
IDE navigation for ClaudeQ monitor.

Handles navigating to terminal tabs in various IDEs.
"""

import glob
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional


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
        jetbrains_ides = ['PyCharm', 'IntelliJ', 'GoLand', 'WebStorm', 'PhpStorm',
                          'RubyMine', 'CLion', 'DataGrip', 'JetBrains']
        if any(ide in preferred_ide for ide in jetbrains_ides):
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
    jetbrains_ides = ['PyCharm', 'IntelliJ', 'GoLand', 'WebStorm', 'PhpStorm',
                      'RubyMine', 'CLion', 'DataGrip', 'JetBrains']
    if preferred_ide and any(ide in preferred_ide for ide in jetbrains_ides):
        if _close_jetbrains(preferred_ide, project_path, terminal_title):
            return True

    if preferred_ide and 'VS Code' in preferred_ide:
        if _close_vscode(project_path, terminal_title or title_pattern):
            return True

    if _close_terminal_app(title_pattern):
        return True

    if _close_iterm2(title_pattern):
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
    jetbrains_ides = ['PyCharm', 'IntelliJ', 'GoLand', 'WebStorm', 'PhpStorm',
                      'RubyMine', 'CLion', 'DataGrip', 'JetBrains']
    if preferred_ide and any(ide in preferred_ide for ide in jetbrains_ides):
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

    ide_cmd_map = {
        'PyCharm': 'pycharm',
        'IntelliJ IDEA': 'idea',
        'GoLand': 'goland',
        'WebStorm': 'webstorm',
        'PhpStorm': 'phpstorm',
    }

    ide_cmd = ide_cmd_map.get(ide)
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
                f'var projectPath = "{project_path}"'
            )
        if terminal_title:
            custom_script = custom_script.replace(
                'var terminalTabName = System.getenv("CLAUDEQ_TERMINAL_TITLE")',
                f'var terminalTabName = "{terminal_title}"'
            )

        with tempfile.NamedTemporaryFile(mode='w', suffix='.groovy', delete=False) as tmp:
            tmp.write(custom_script)
            tmp_script_path = tmp.name

        try:
            # Expand PATH to include JetBrains CLI tools
            env = os.environ.copy()
            jetbrains_paths = []

            for pattern in ['IntelliJ*.app', 'PyCharm*.app', 'WebStorm*.app',
                           'PhpStorm*.app', 'GoLand*.app', 'RubyMine*.app',
                           'CLion*.app', 'DataGrip*.app', 'Rider*.app', 'Fleet*.app']:
                for app in glob.glob(f'/Applications/{pattern}'):
                    jetbrains_paths.append(f'{app}/Contents/MacOS')

            if jetbrains_paths:
                env['PATH'] = ':'.join(jetbrains_paths) + ':' + env.get('PATH', '')

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
        # Expand PATH to include common locations
        env = os.environ.copy()
        extra_paths = ['/usr/local/bin', '/opt/homebrew/bin']
        current_path = env.get('PATH', '')
        for path in extra_paths:
            if path not in current_path and os.path.exists(path):
                env['PATH'] = f"{path}:{current_path}"
                current_path = env['PATH']

        code_path = shutil.which('code', path=env.get('PATH'))
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
    script = f'''
    tell application "Terminal"
        repeat with w in windows
            repeat with t from 1 to count of tabs of w
                set tabName to custom title of tab t of w
                if tabName contains "{title_pattern}" then
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
            text=True
        )
        return result.returncode == 0 and 'true' in result.stdout
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _navigate_iterm2(title_pattern: str) -> bool:
    """Navigate to terminal in iTerm2."""
    script = f'''
    tell application "iTerm"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if name of s contains "{title_pattern}" then
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
            text=True
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

    ide_cmd_map = {
        'PyCharm': 'pycharm',
        'IntelliJ IDEA': 'idea',
        'GoLand': 'goland',
        'WebStorm': 'webstorm',
        'PhpStorm': 'phpstorm',
    }

    ide_cmd = ide_cmd_map.get(ide)
    if not ide_cmd:
        return False

    project_match = ""
    if project_path:
        project_match = f'''
    for (var i = 0; i < allProjects.length; i++) {{
        var project = allProjects[i]
        if (project.getBasePath() != null && project.getBasePath().equals("{project_path}")) {{
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
                var content = contentManager.findContent("{terminal_title}")
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
        env = os.environ.copy()
        jetbrains_paths = []
        for pattern in ['IntelliJ*.app', 'PyCharm*.app', 'WebStorm*.app',
                        'PhpStorm*.app', 'GoLand*.app', 'RubyMine*.app',
                        'CLion*.app', 'DataGrip*.app', 'Rider*.app', 'Fleet*.app']:
            for app in glob.glob(f'/Applications/{pattern}'):
                jetbrains_paths.append(f'{app}/Contents/MacOS')
        if jetbrains_paths:
            env['PATH'] = ':'.join(jetbrains_paths) + ':' + env.get('PATH', '')

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
        env = os.environ.copy()
        extra_paths = ['/usr/local/bin', '/opt/homebrew/bin']
        current_path = env.get('PATH', '')
        for p in extra_paths:
            if p not in current_path and os.path.exists(p):
                env['PATH'] = f"{p}:{current_path}"
                current_path = env['PATH']

        code_path = shutil.which('code', path=env.get('PATH'))
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
    script = f'''
    tell application "Terminal"
        repeat with w in windows
            set tabCount to count of tabs of w
            repeat with t from 1 to tabCount
                set tabName to custom title of tab t of w
                if tabName contains "{title_pattern}" then
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
            text=True
        )
        return result.returncode == 0 and 'true' in result.stdout
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _close_iterm2(title_pattern: str) -> bool:
    """Close a terminal tab/session in iTerm2."""
    script = f'''
    tell application "iTerm"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if name of s contains "{title_pattern}" then
                        close s
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
            text=True
        )
        return result.returncode == 0 and 'true' in result.stdout
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _open_jetbrains_terminal(
    ide: str,
    project_path: Optional[str],
    command: str
) -> bool:
    """Open a new terminal tab in JetBrains IDE and run a command."""
    ide_cmd_map = {
        'PyCharm': 'pycharm',
        'IntelliJ IDEA': 'idea',
        'GoLand': 'goland',
        'WebStorm': 'webstorm',
        'PhpStorm': 'phpstorm',
    }

    ide_cmd = ide_cmd_map.get(ide)
    if not ide_cmd:
        return False

    project_match = ""
    if project_path:
        project_match = f'''
    for (var i = 0; i < allProjects.length; i++) {{
        var project = allProjects[i]
        if (project.getBasePath() != null && project.getBasePath().equals("{project_path}")) {{
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
                widget.executeCommand("{command}")
            }}
        }} as Runnable).start()
    }}
}}
'''

    try:
        env = os.environ.copy()
        jetbrains_paths = []
        for pattern in ['IntelliJ*.app', 'PyCharm*.app', 'WebStorm*.app',
                        'PhpStorm*.app', 'GoLand*.app', 'RubyMine*.app',
                        'CLion*.app', 'DataGrip*.app', 'Rider*.app', 'Fleet*.app']:
            for app in glob.glob(f'/Applications/{pattern}'):
                jetbrains_paths.append(f'{app}/Contents/MacOS')
        if jetbrains_paths:
            env['PATH'] = ':'.join(jetbrains_paths) + ':' + env.get('PATH', '')

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
    script = f'''
    tell application "Terminal"
        do script "{command}"
        activate
    end tell
    return true
    '''

    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _open_iterm2_terminal(command: str) -> bool:
    """Open a new iTerm2 tab and run a command."""
    script = f'''
    tell application "iTerm"
        tell current window
            create tab with default profile
            tell current session
                write text "{command}"
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
            text=True
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _open_vscode_terminal(project_path: Optional[str], command: str) -> bool:
    """Open a new VS Code terminal tab and run a command."""
    try:
        env = os.environ.copy()
        extra_paths = ['/usr/local/bin', '/opt/homebrew/bin']
        current_path = env.get('PATH', '')
        for p in extra_paths:
            if p not in current_path and os.path.exists(p):
                env['PATH'] = f"{p}:{current_path}"
                current_path = env['PATH']

        code_path = shutil.which('code', path=env.get('PATH'))
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
