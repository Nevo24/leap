"""
Terminal utilities for Leap.

Handles terminal title setting, escape sequences, and terminal-related operations.
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from leap.utils.constants import TERM_TITLE_PREFIX, TERM_TITLE_SUFFIX

# VS Code extension watches this file for rename requests
_VSCODE_REQUEST_FILE = Path.home() / '.leap-terminal-request'

# Maps JetBrains IDE display names to their CLI command names
_JETBRAINS_CLI_MAP: dict[str, str] = {
    'PyCharm': 'pycharm',
    'IntelliJ IDEA': 'idea',
    'GoLand': 'goland',
    'WebStorm': 'webstorm',
    'PhpStorm': 'phpstorm',
    'Android Studio': 'studio',
    'RubyMine': 'rubymine',
    'CLion': 'clion',
    'DataGrip': 'datagrip',
    'JetBrains IDE': 'idea',  # fallback
}

# Glob patterns for JetBrains .app bundles
_JETBRAINS_APP_PATTERNS: list[str] = [
    'IntelliJ*.app', 'PyCharm*.app', 'WebStorm*.app',
    'PhpStorm*.app', 'GoLand*.app', 'RubyMine*.app',
    'CLion*.app', 'DataGrip*.app', 'Rider*.app', 'Fleet*.app',
    'Android Studio*.app',
]


def _jetbrains_env() -> dict[str, str]:
    """Build an env dict with JetBrains CLI tools on PATH."""
    env = os.environ.copy()
    jetbrains_paths: list[str] = []

    for app_dir in ['/Applications', os.path.expanduser('~/Applications')]:
        for pattern in _JETBRAINS_APP_PATTERNS:
            for app in glob.glob(f'{app_dir}/{pattern}'):
                jetbrains_paths.append(f'{app}/Contents/MacOS')

    toolbox_scripts = os.path.expanduser(
        '~/Library/Application Support/JetBrains/Toolbox/scripts'
    )
    if os.path.isdir(toolbox_scripts):
        jetbrains_paths.append(toolbox_scripts)

    if jetbrains_paths:
        env['PATH'] = ':'.join(jetbrains_paths) + ':' + env.get('PATH', '')
    return env


def _escape_groovy(s: str) -> str:
    """Escape a string for embedding in a Groovy string literal."""
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')


def _jetbrains_rename_tab(title: str) -> None:
    """Rename the currently selected JetBrains terminal tab via ideScript.

    Runs in the background to avoid blocking startup.
    """
    # Detect IDE from environment
    terminal_emulator = os.environ.get('TERMINAL_EMULATOR', '')
    if 'JetBrains' not in terminal_emulator and 'jetbrains' not in terminal_emulator.lower():
        return

    # Import here to avoid circular imports at module level
    from leap.utils.ide_detection import detect_ide
    ide = detect_ide()

    cli_name = _JETBRAINS_CLI_MAP.get(ide)
    if not cli_name:
        return

    env = _jetbrains_env()
    cli_path = shutil.which(cli_name, path=env.get('PATH'))
    if not cli_path:
        return

    escaped_title = _escape_groovy(title)

    # Try to scope to the project matching the current working directory
    cwd = os.getcwd()
    escaped_cwd = _escape_groovy(cwd)

    groovy_script = f'''import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.openapi.project.ProjectManager

IDE.application.invokeLater {{
    var allProjects = ProjectManager.getInstance().getOpenProjects()
    var targetProject = null

    // Try to find the project matching the current working directory
    for (var i = 0; i < allProjects.length; i++) {{
        var project = allProjects[i]
        var basePath = project.getBasePath()
        if (basePath != null && "{escaped_cwd}".startsWith(basePath)) {{
            targetProject = project
            break
        }}
    }}
    // Fallback to first project
    if (targetProject == null && allProjects.length > 0) {{
        targetProject = allProjects[0]
    }}

    if (targetProject != null) {{
        var toolWindowManager = ToolWindowManager.getInstance(targetProject)
        var terminalWindow = toolWindowManager.getToolWindow("Terminal")
        if (terminalWindow != null) {{
            try {{
                var contentManager = terminalWindow.getContentManager()
                var content = contentManager.getSelectedContent()
                if (content != null) {{
                    content.setDisplayName("{escaped_title}")
                }}
            }} catch (Exception e) {{
            }}
        }}
    }}
}}
'''

    try:
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.groovy', delete=False
        ) as tmp:
            tmp.write(groovy_script)
            tmp_path = tmp.name

        try:
            subprocess.run(
                [cli_path, 'ideScript', tmp_path],
                capture_output=True,
                timeout=5,
                env=env,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except (OSError, subprocess.TimeoutExpired):
        pass


def set_terminal_title(title: str, *, vscode_rename: bool = True) -> None:
    """
    Set the terminal tab/window title.

    Uses OSC escape sequence (works in native terminals and VS Code when
    the proposed onDidWriteTerminalData API is available). Optionally also
    writes a rename request file for the VS Code extension's file watcher.
    For JetBrains IDEs, also renames the tab via ideScript (handles
    manually-named tabs that ignore OSC sequences).

    Args:
        title: The title to set for the terminal.
        vscode_rename: If True (default), also write the VS Code rename
            request file. Set to False for periodic refresh calls to avoid
            unnecessary file watcher churn.
    """
    # OSC title sequence (native terminals + VS Code data listener)
    sys.stdout.write(f"{TERM_TITLE_PREFIX}{title}{TERM_TITLE_SUFFIX}")
    sys.stdout.flush()

    if vscode_rename:
        # VS Code file watcher fallback — the extension watches
        # ~/.leap-terminal-request and renames the active terminal.
        # This must happen FROM the Python process (not from the shell
        # before exec) to avoid a race where VS Code overrides the tab
        # name with the new process name ("Python") after exec.
        try:
            _VSCODE_REQUEST_FILE.write_text(f"rename:{title}")
        except OSError:
            pass

        # JetBrains: rename via ideScript (handles manually-named tabs)
        threading.Thread(
            target=_jetbrains_rename_tab,
            args=(title,),
            daemon=True,
        ).start()


def print_banner(session_type: str, tag: str, cli_name: str = '') -> None:
    """
    Print the Leap ASCII banner.

    Args:
        session_type: Either 'server' or 'client'.
        tag: The session tag name.
        cli_name: CLI display name (e.g. 'Claude Code', 'OpenAI Codex', 'Cursor Agent', 'Gemini CLI').
    """
    subtitle = f" - {cli_name}" if cli_name else ""
    banner = rf"""
  _
 | |    ___  __ _ _ __
 | |   / _ \/ _` | '_ \
 | |__|  __/ (_| | |_) |
 |_____\___|\__,_| .__/
                  |_|    {subtitle}
"""
    print(banner)
    print("=" * 80)
    print(f"  PTY {session_type.upper()} - Session: {tag}")
    print("=" * 80)
