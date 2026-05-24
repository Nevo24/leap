"""
Terminal utilities for Leap.

Handles terminal title setting, escape sequences, and terminal-related operations.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

from leap.utils.constants import COLORS, TERM_TITLE_PREFIX, TERM_TITLE_SUFFIX
from leap.utils.ide_detection import detect_ide

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

# Cached JetBrains CLI path (None = not yet resolved, '' = resolved but not found)
_jetbrains_cli_path: Optional[str] = None


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


def _resolve_jetbrains_cli() -> str:
    """Resolve and cache the JetBrains IDE CLI path.

    Returns the CLI path, or '' if not in a JetBrains terminal or CLI
    not found.  Result is cached for the lifetime of the process.
    """
    global _jetbrains_cli_path
    if _jetbrains_cli_path is not None:
        return _jetbrains_cli_path

    terminal_emulator = os.environ.get('TERMINAL_EMULATOR', '')
    if 'JetBrains' not in terminal_emulator and 'jetbrains' not in terminal_emulator.lower():
        _jetbrains_cli_path = ''
        return ''

    ide = detect_ide()

    cli_name = _JETBRAINS_CLI_MAP.get(ide, '')
    if not cli_name:
        _jetbrains_cli_path = ''
        return ''

    env = _jetbrains_env()
    _jetbrains_cli_path = shutil.which(cli_name, path=env.get('PATH')) or ''
    return _jetbrains_cli_path


def _escape_groovy(s: str) -> str:
    """Escape a string for embedding in a Groovy string literal."""
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')


def _jetbrains_rename_tab(title: str) -> None:
    """Rename the currently selected JetBrains terminal tab via ideScript.

    Runs in the background to avoid blocking startup.
    """
    cli_path = _resolve_jetbrains_cli()
    if not cli_path:
        return

    escaped_title = _escape_groovy(title)

    # Scope to the project matching the current working directory
    cwd = os.getcwd()
    escaped_cwd = _escape_groovy(cwd)

    # Match the project with the longest basePath prefix to avoid
    # /foo/project matching when /foo/project-v2 is the real project.
    groovy_script = f'''import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.openapi.project.ProjectManager

IDE.application.invokeLater {{
    var allProjects = ProjectManager.getInstance().getOpenProjects()
    var targetProject = null
    var bestLen = 0

    var cwd = "{escaped_cwd}"
    for (var i = 0; i < allProjects.length; i++) {{
        var project = allProjects[i]
        var basePath = project.getBasePath()
        if (basePath != null
                && (cwd.equals(basePath) || cwd.startsWith(basePath + "/"))
                && basePath.length() > bestLen) {{
            targetProject = project
            bestLen = basePath.length()
        }}
    }}
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
                env=_jetbrains_env(),
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except (OSError, subprocess.TimeoutExpired):
        pass


def _jetbrains_sweep_stale_tabs(
    storage_dir: Path,
    ide_name: str,
    project_path: str,
    exclude_tag: Optional[str] = None,
) -> None:
    """Rename stale ``lps <tag>``/``lpc <tag>`` JetBrains terminal tabs.

    Force-quit / kernel panic / power-loss kills the IDE before our
    cleanup OSC reset can be processed (SIGKILL is uncatchable; the
    IDE saves workspace.xml on its own schedule), so the next IDE
    launch restores tabs at their last live name — ``lps <tag>`` /
    ``lpc <tag>`` — even though the server is long dead.  This
    function runs on every new leap-server start in a JetBrains
    terminal: it walks the current project's Terminal tool window,
    checks each ``lp[sc] <tag>`` tab against the per-tag
    ``<tag>.meta`` file, and renames any whose meta is missing OR
    whose meta says the session belongs to a different IDE / project.

    Scoped to ``project_path``'s project only — a stale ``lps X`` in
    a different GoLand window / different project is left alone.  A
    live session in ``project_path`` is also left alone (because its
    meta matches our ``ide_name`` + ``project_path``).

    ``exclude_tag`` is the tag of the session that's *starting right
    now*: it is force-removed from the live-tags allow-list, so a
    pre-existing ``lps <that-tag>`` tab (force-quit leftover) is
    treated as stale and renamed.  Our own newly-created tab still
    has its default JetBrains name (e.g. "Local") at this point —
    the ``lps <tag>`` OSC fires later in ``_run()`` — so the sweep
    doesn't accidentally touch it.  Callers MUST run the sweep
    synchronously BEFORE the OSC, otherwise the rename can race the
    OSC and rename our own live tab back to bare.

    Best-effort: silent on every failure path (no JetBrains CLI, meta
    files unreadable, ideScript hung, project closed in IDE, etc.).
    """
    cli_path = _resolve_jetbrains_cli()
    if not cli_path:
        return

    # Build the allow-list of tags genuinely live in *this* IDE+project.
    # A tab named ``lps <tag>`` whose <tag> is in this set is the live
    # tab for our own (or a sibling concurrent) session and stays put;
    # anything else is treated as a leftover.
    live_tags: list[str] = []
    sockets_dir = Path(storage_dir) / 'sockets'
    try:
        meta_files = list(sockets_dir.glob('*.meta'))
    except OSError:
        meta_files = []
    for meta in meta_files:
        try:
            with open(meta) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if (data.get('ide') == ide_name
                and data.get('project_path') == project_path):
            tag = meta.stem
            if tag == exclude_tag:
                # Our own about-to-start session: force-remove from
                # the allow-list so a pre-existing ``lps <ourTag>``
                # tab (force-quit leftover) gets renamed to bare,
                # freeing the name for our OSC to claim a moment later.
                continue
            # Verify the recorded PID is actually alive — ``kill -9``
            # leaves the meta file lingering with no live server, and
            # without this check the stale tab would be protected.
            # ``os.kill(pid, 0)`` raises ProcessLookupError when the
            # PID is gone; treat that as "session is dead, meta is
            # garbage, tab is stale".
            pid = data.get('pid')
            if isinstance(pid, int) and pid > 0:
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, OSError):
                    continue
            live_tags.append(tag)

    # Format the allow-list as a Groovy Set literal.  Empty list is
    # fine — Groovy ``[] as Set`` is a valid empty set.
    groovy_set_items = ', '.join(
        f'"{_escape_groovy(t)}"' for t in live_tags
    )
    escaped_project_path = _escape_groovy(project_path)

    groovy_script = f'''import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.openapi.project.ProjectManager

IDE.application.invokeLater {{
    var liveTags = [{groovy_set_items}] as Set
    var allProjects = ProjectManager.getInstance().getOpenProjects()
    var targetProject = null
    for (var i = 0; i < allProjects.length; i++) {{
        var project = allProjects[i]
        if (project.getBasePath() != null
                && project.getBasePath().equals("{escaped_project_path}")) {{
            targetProject = project
            break
        }}
    }}
    if (targetProject == null) return

    var tw = ToolWindowManager.getInstance(targetProject).getToolWindow("Terminal")
    if (tw == null) return

    var cm = tw.getContentManager()
    if (cm == null) return

    var n = cm.getContentCount()
    for (var i = 0; i < n; i++) {{
        var content = cm.getContent(i)
        if (content == null) continue
        var name = content.getDisplayName()
        if (name == null) continue
        var prefix = null
        if (name.startsWith("lps ")) prefix = "lps "
        else if (name.startsWith("lpc ")) prefix = "lpc "
        if (prefix == null) continue
        var tag = name.substring(prefix.length())
        if (!liveTags.contains(tag)) {{
            try {{
                content.setDisplayName(tag)
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
                timeout=10,
                env=_jetbrains_env(),
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except (OSError, subprocess.SubprocessError):
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

        # JetBrains: rename via ideScript (handles manually-named tabs).
        # Quick-exit if not in JetBrains (cached check, no thread spawned).
        if _resolve_jetbrains_cli():
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
    if session_type == 'server':
        print(f"  {COLORS['cyan']}Tip: ^^ queues a message, even mid-run{COLORS['reset']}")
