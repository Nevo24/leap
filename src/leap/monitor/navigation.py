"""
IDE navigation for Leap monitor.

Handles navigating to terminal tabs in various IDEs.
"""

import glob
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import AppKit
from ApplicationServices import (
    AXIsProcessTrusted, AXIsProcessTrustedWithOptions,
    AXUIElementCopyAttributeValue, AXUIElementCreateApplication,
    AXUIElementPerformAction, kAXErrorSuccess,
)
from CoreFoundation import kCFBooleanTrue
from Quartz import (
    CGEventCreateKeyboardEvent, CGEventPost, CGEventSetFlags,
    kCGEventFlagMaskCommand, kCGEventFlagMaskControl,
    kCGEventFlagMaskShift, kCGHIDEventTap,
)

from leap.utils.constants import JETBRAINS_IDES

logger = logging.getLogger(__name__)

# JetBrains IDE names used across navigation functions
_JETBRAINS_IDE_NAMES: list[str] = [
    'PyCharm', 'IntelliJ', 'GoLand', 'WebStorm', 'PhpStorm',
    'RubyMine', 'CLion', 'DataGrip', 'JetBrains', 'Android Studio',
]

# Maps IDE display names to their CLI command names
_IDE_CMD_MAP: dict[str, str] = {
    'PyCharm': 'pycharm',
    'IntelliJ IDEA': 'idea',
    'GoLand': 'goland',
    'WebStorm': 'webstorm',
    'PhpStorm': 'phpstorm',
    'Android Studio': 'studio',
}

# Glob patterns for JetBrains .app bundles
_JETBRAINS_APP_PATTERNS: list[str] = [
    'IntelliJ*.app', 'PyCharm*.app', 'WebStorm*.app',
    'PhpStorm*.app', 'GoLand*.app', 'RubyMine*.app',
    'CLion*.app', 'DataGrip*.app', 'Rider*.app', 'Fleet*.app',
    'Android Studio*.app',
]

# Directories to search for JetBrains .app bundles
_JETBRAINS_APP_DIRS: list[str] = [
    '/Applications',
    os.path.expanduser('~/Applications'),
]


def find_jetbrains_app(name: Optional[str]) -> Optional[str]:
    """Return the absolute path of the JetBrains ``.app`` bundle matching *name*.

    Used by GUI Resume to derive ``ide_app_path`` for
    ``_open_jetbrains_terminal`` — without it, the JetBrains CLI runs
    via the Toolbox-generated shim (``open -na`` internally), which is
    flaky for cold-start (forces a new instance whose subsequent
    activation can silently no-op).  Move-to-IDE has this for free
    because the user picks the ``.app`` via QFileDialog.  Resume only
    has the IDE *name* from the recorded session, so we have to find
    the matching bundle ourselves.

    Searches the same locations as ``_jetbrains_env``: ``/Applications``,
    ``~/Applications`` (one subdir deep for the ``JetBrains Toolbox``
    folder layout), and the Toolbox-managed install root recursively.
    Returns the first match — when multiple GoLand/PyCharm/etc. installs
    coexist (CE + Pro, multiple Toolbox versions), Resume has no
    affinity to which one the user wants; first-found is acceptable
    and matches LaunchServices' own resolution.
    """
    if not name:
        return None
    # ``IntelliJ IDEA`` is the canonical detect_ide() return for any
    # IntelliJ edition — the bundle is ``IntelliJ IDEA.app``,
    # ``IntelliJ IDEA CE.app``, or similar.  Match by prefix.
    bundle_glob = f'{name}*.app'
    for app_dir in _JETBRAINS_APP_DIRS:
        for app in glob.glob(f'{app_dir}/{bundle_glob}'):
            return app
        # ``~/Applications/JetBrains Toolbox/<App>.app`` — one subdir level.
        for app in glob.glob(f'{app_dir}/*/{bundle_glob}'):
            return app
    toolbox_apps_root = os.path.expanduser(
        '~/Library/Application Support/JetBrains/Toolbox/apps'
    )
    if os.path.isdir(toolbox_apps_root):
        for app in glob.glob(
            f'{toolbox_apps_root}/**/{bundle_glob}', recursive=True,
        ):
            return app
    return None


def is_ide_app(name: Optional[str]) -> bool:
    """True if *name* is an IDE we can open a project in (vs a plain terminal).

    Used by GUI Resume to decide whether to thread the recorded ``cwd``
    through as ``project_path``: IDEs (VS Code, Cursor, JetBrains family)
    take a project path and open the project; plain terminals (iTerm2,
    Terminal.app, WezTerm, Warp, cmux, Kitty, Ghostty, …) ignore project_path
    and just open at the shell's default cwd.

    Match by substring against the JetBrains list so 'IntelliJ IDEA',
    'PyCharm Professional', etc. all resolve as JetBrains.
    """
    if not name:
        return False
    if name in ('VS Code', 'Cursor'):
        return True
    return any(jb in name for jb in _JETBRAINS_IDE_NAMES)


def is_jetbrains_app(app_path: Optional[str]) -> bool:
    """True if the picked ``.app`` bundle is a JetBrains-family IDE.

    Matches the bundle basename against ``_JETBRAINS_IDE_NAMES`` so
    ``PyCharm.app``, ``IntelliJ IDEA.app``, ``RubyMine.app``,
    ``CLion.app``, ``Android Studio.app`` etc. all resolve as JetBrains -
    *including* the ones :func:`detect_supported_ide_for_move` returns
    ``None`` for (RubyMine/CLion/DataGrip), since the ``.idea/.name``
    tab-alias mechanism works for every JetBrains IDE regardless of
    whether Leap can drive its integrated terminal.
    """
    if not app_path:
        return False
    bundle = os.path.basename(app_path.rstrip('/'))
    return any(jb in bundle for jb in _JETBRAINS_IDE_NAMES)


def detect_supported_ide_for_move(app_path: str) -> Optional[str]:
    """Classify a user-picked ``.app`` for the Move-to-IDE flow and
    return the value to pass as ``preferred_ide`` to
    :func:`open_terminal_with_command`.

    Returns:
        * One of ``_IDE_CMD_MAP``'s canonical JetBrains keys (e.g.
          ``'PyCharm'``, ``'IntelliJ IDEA'``, ``'GoLand'``,
          ``'WebStorm'``, ``'PhpStorm'``, ``'Android Studio'``) when
          the picked bundle resolves to a JetBrains IDE we can
          actually drive — note we only emit canonical keys whose
          ``ide_cmd`` we know, so ``_open_jetbrains_terminal``'s
          exact-lookup against ``_IDE_CMD_MAP`` succeeds.
        * ``'VS Code'`` for any ``Visual Studio Code(*).app``
          (stable or Insiders).
        * ``'Cursor'`` for ``Cursor(*).app`` — a VS Code fork driven
          by the same integrated-terminal + Leap-extension plumbing
          (``open_terminal_with_command`` routes 'Cursor' through
          ``_open_vscode_terminal`` with the ``cursor`` CLI).
        * ``None`` for everything else (Sublime, Xcode, Arduino,
          RubyMine/CLion/DataGrip/Rider/Fleet — not in
          ``_IDE_CMD_MAP`` so we'd fall back to Terminal.app
          silently, which would surprise the user).

    The caller uses ``None`` as the signal to fall back to the
    legacy "just open the .app" behaviour with no popup.
    """
    if not app_path:
        return None
    bundle = os.path.basename(app_path.rstrip('/'))
    # JetBrains: only return a canonical key the downstream helper
    # actually knows how to drive.  Order by length descending so
    # ``IntelliJ IDEA`` matches before any (hypothetical) shorter
    # ``IntelliJ`` token.
    for canonical in sorted(_IDE_CMD_MAP.keys(), key=len, reverse=True):
        if canonical in bundle:
            return canonical
    # VS Code stable bundle = ``Visual Studio Code.app``; Insiders =
    # ``Visual Studio Code - Insiders.app``.
    if bundle.startswith('Visual Studio Code'):
        return 'VS Code'
    # Cursor (bundle ``Cursor.app``) is a VS Code fork: same integrated
    # terminal, same Leap extension, and open_terminal_with_command
    # already routes 'Cursor' through _open_vscode_terminal (cursor CLI),
    # so the move flow drives it identically to VS Code.
    if bundle.startswith('Cursor'):
        return 'Cursor'
    return None


def _jetbrains_env() -> dict[str, str]:
    """Build an env dict with JetBrains CLI tools on PATH.

    Covers four common install layouts:

    * Standalone installs at ``/Applications/<App>.app`` or
      ``~/Applications/<App>.app`` (downloaded ``.dmg``).
    * Toolbox installs at ``~/Applications/JetBrains Toolbox/<App>.app``
      (one subdirectory level under ``~/Applications`` — the Toolbox
      default since the 2.x rewrite when "Generate shell scripts" is
      off but the user has ticked the "Update apps" sync into
      ~/Applications).
    * Toolbox's actual install root,
      ``~/Library/Application Support/JetBrains/Toolbox/apps/**/<App>.app``
      — the canonical Toolbox 2.x location.  Searched recursively
      because the path includes a version directory
      (``apps/GoLand/ch-0/<version>/GoLand.app``).
    * ``~/Library/Application Support/JetBrains/Toolbox/scripts``
      shell-script directory, populated when the user enables
      "Generate shell scripts" in Toolbox settings.

    Without the Toolbox-aware lookups, a Toolbox-only user (no
    ``.app`` in ``/Applications``) ends up with no JetBrains CLI on
    PATH — every ``goland``/``pycharm``/etc. subprocess raises
    ``FileNotFoundError`` and the move-to-IDE flow silently falls
    back to Terminal.app.
    """
    env = os.environ.copy()
    jetbrains_paths: list[str] = []

    # Standalone installs + one subdirectory level (for
    # ~/Applications/JetBrains Toolbox/<App>.app)
    for app_dir in _JETBRAINS_APP_DIRS:
        for pattern in _JETBRAINS_APP_PATTERNS:
            for app in glob.glob(f'{app_dir}/{pattern}'):
                jetbrains_paths.append(f'{app}/Contents/MacOS')
            for app in glob.glob(f'{app_dir}/*/{pattern}'):
                jetbrains_paths.append(f'{app}/Contents/MacOS')

    # Toolbox-managed install root.  Tree is shallow (4-5 levels) so
    # the recursive glob is cheap.
    toolbox_apps_root = os.path.expanduser(
        '~/Library/Application Support/JetBrains/Toolbox/apps'
    )
    if os.path.isdir(toolbox_apps_root):
        for pattern in _JETBRAINS_APP_PATTERNS:
            for app in glob.glob(
                f'{toolbox_apps_root}/**/{pattern}', recursive=True,
            ):
                jetbrains_paths.append(f'{app}/Contents/MacOS')

    # Toolbox shell-script directory (optional; only present if the
    # user enabled the option in Toolbox settings).
    toolbox_scripts = os.path.expanduser(
        '~/Library/Application Support/JetBrains/Toolbox/scripts'
    )
    if os.path.isdir(toolbox_scripts):
        jetbrains_paths.append(toolbox_scripts)

    if jetbrains_paths:
        env['PATH'] = ':'.join(jetbrains_paths) + ':' + env.get('PATH', '')
    return env


# Standard macOS locations + bundle names for the VS Code-family editors.
_VSCODE_APP_DIRS = ('/Applications', os.path.expanduser('~/Applications'))
_VSCODE_BUNDLE_NAMES = {
    'VS Code': ('Visual Studio Code', 'Visual Studio Code - Insiders'),
    'Cursor': ('Cursor',),
}


def _vscode_cli_in_bundle(ide: str) -> Optional[str]:
    """Path to the ``code`` CLI bundled inside the target editor's own
    ``.app``, or None if the bundle isn't in a standard location.

    Both VS Code and Cursor ship the CLI at
    ``Contents/Resources/app/bin/code``.  Pinning to the bundle avoids a
    PATH ``code`` that belongs to a *different* VS Code-family editor:
    Cursor installs a ``code`` shim that shadows VS Code's on PATH, so a
    plain ``which('code')`` for a VS Code session would drive Cursor
    instead (e.g. closing a VS Code session would pop Cursor open)."""
    for base in _VSCODE_APP_DIRS:
        for name in _VSCODE_BUNDLE_NAMES.get(ide, ()):
            cli = os.path.join(base, f'{name}.app',
                               'Contents', 'Resources', 'app', 'bin', 'code')
            if os.path.isfile(cli):
                return cli
    return None


def _cli_is_other_editor(cli_path: str, ide: str) -> bool:
    """True if *cli_path* resolves into the OTHER VS Code-family editor's
    bundle - so we never drive Cursor for a VS Code session, or VS Code
    for a Cursor session (e.g. Cursor's ``code`` shim symlinked onto PATH)."""
    real = os.path.realpath(cli_path)
    in_cursor = 'Cursor.app/' in real
    in_vscode = 'Visual Studio Code' in real
    return in_vscode if ide == 'Cursor' else in_cursor


def _vscode_env_and_path(
    ide: str = 'VS Code',
) -> tuple[dict[str, str], Optional[str]]:
    """Build an env dict with VS Code/Cursor CLI on PATH and return the binary path."""
    env = os.environ.copy()
    extra_paths = ['/usr/local/bin', '/opt/homebrew/bin']
    current_path = env.get('PATH', '')
    for p in extra_paths:
        if p not in current_path and os.path.exists(p):
            env['PATH'] = f"{p}:{current_path}"
            current_path = env['PATH']
    # Prefer the CLI bundled inside the target editor's own .app; only fall
    # back to a PATH lookup, and then reject a candidate that resolves into
    # the OTHER editor's bundle (Cursor's `code` shim shadows VS Code's on
    # PATH - running it would open the wrong editor).
    code_path = _vscode_cli_in_bundle(ide)
    if not code_path:
        cli_name = 'cursor' if ide == 'Cursor' else 'code'
        cand = shutil.which(cli_name, path=env.get('PATH'))
        if cand and not _cli_is_other_editor(cand, ide):
            code_path = cand
    return env, code_path


def _vscode_applescript_name(ide: str = 'VS Code') -> str:
    """Return the AppleScript application name for VS Code or Cursor."""
    return 'Cursor' if ide == 'Cursor' else 'Visual Studio Code'


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
    fallback_terminal: Optional[str] = None,
    outcome: Optional[dict] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    project_already_open: bool = False,
    ide_app_path: Optional[str] = None,
) -> bool:
    """
    Open a new terminal tab and run a command in it.

    Opens exclusively in the preferred IDE/terminal when known.
    On failure, tries ``fallback_terminal`` next (if given), then
    Terminal.app, then iTerm2 as a last resort.

    Args:
        command: Command to execute in the new terminal.
        preferred_ide: IDE or terminal app to open in (from session metadata).
        project_path: Project path for IDE navigation.
        fallback_terminal: User's configured default terminal
            (``'iTerm2'`` / ``'Terminal.app'`` / ``'Warp'`` / ``'WezTerm'`` /
            ``'cmux'``) to try if the primary path fails.  Spares an iTerm2 user
            from being dropped into Terminal.app when an IDE move
            fails.
        outcome: Optional mutable dict.  When provided, the function
            sets ``outcome['used']`` to the canonical name of whichever
            helper actually opened the terminal — lets the caller
            distinguish "preferred IDE worked" from "fell back to
            iTerm2" for accurate post-completion status messages.
        should_cancel: Optional callable.  Currently consulted only by
            the JetBrains cold-start poll loop, which checks it each
            iteration; if it returns True, the poll bails to the
            fallback chain.  Used to plumb in row-removal signals so
            a user clicking the X mid-move doesn't leave a 10-min
            worker spinning against a dead IDE.
        ide_app_path: Path to the specific ``.app`` bundle the user
            picked (Move-to-IDE flow only).  When set:
              * JetBrains CLI subprocesses are run from that bundle's
                ``Contents/MacOS/`` directly rather than via PATH.
              * VS Code / Cursor's CLI is pinned to that bundle's
                ``Contents/Resources/app/bin/<code|cursor>``.
            In both cases, this prevents subprocess calls from
            routing to a *different* installation than the one
            ``open -a`` activated when the user has multiple
            versions installed (CE + Pro, Stable + Insiders, etc.).

    Returns:
        True if a new terminal was opened successfully.
    """
    # Guard: if project_path doesn't exist on disk, treat as None to avoid
    # IDE crashes (e.g. JetBrains "Could not determine current working directory").
    if project_path and not Path(project_path).is_dir():
        logger.warning("project_path does not exist, ignoring: %s", project_path)
        project_path = None

    def _record(name: str) -> bool:
        """Helper: stamp ``outcome['used']`` and return True."""
        if outcome is not None:
            outcome['used'] = name
        return True

    if preferred_ide:
        # Try the specific IDE first. If it fails, fall through to generic
        # fallback so that a terminal always opens somewhere.
        if any(ide in preferred_ide for ide in _JETBRAINS_IDE_NAMES):
            if _open_jetbrains_terminal(
                preferred_ide, project_path, command,
                should_cancel=should_cancel,
                project_already_open=project_already_open,
                ide_app_path=ide_app_path,
            ):
                return _record(preferred_ide)
        elif preferred_ide in ('VS Code', 'Cursor'):
            if _open_vscode_terminal(
                project_path, command, ide=preferred_ide,
                should_cancel=should_cancel,
                ide_app_path=ide_app_path,
            ):
                return _record(preferred_ide)
        elif preferred_ide == 'iTerm2':
            if _open_iterm2_terminal(command):
                return _record('iTerm2')
            logger.debug("iTerm2 open failed, falling back")
        elif preferred_ide == 'Terminal.app':
            if _open_terminal_app_terminal(command):
                return _record('Terminal.app')
            logger.debug("Terminal.app open failed, falling back")
        elif preferred_ide == 'Warp':
            if _open_warp_terminal(command):
                return _record('Warp')
            logger.debug("Warp open failed, falling back")
        elif preferred_ide == 'WezTerm':
            if _open_wezterm_terminal(command):
                return _record('WezTerm')
            logger.debug("WezTerm open failed, falling back")
        elif preferred_ide == 'cmux':
            if _open_cmux_terminal(command):
                return _record('cmux')
            logger.debug("cmux open failed, falling back")

    # Preferred path failed or unknown.  Try the caller's configured
    # default terminal first (so an iTerm2 user isn't surprised with
    # Terminal.app when their IDE move falls back), then last-resort
    # through Terminal.app / iTerm2.  Warp/WezTerm aren't in the
    # last-resort chain: opening a *new* terminal in an app the user
    # doesn't use is more disruptive than dropping into the default.
    if fallback_terminal and fallback_terminal != preferred_ide:
        if fallback_terminal == 'iTerm2':
            if _open_iterm2_terminal(command):
                return _record('iTerm2')
        elif fallback_terminal == 'Terminal.app':
            if _open_terminal_app_terminal(command):
                return _record('Terminal.app')
        elif fallback_terminal == 'Warp':
            if _open_warp_terminal(command):
                return _record('Warp')
        elif fallback_terminal == 'WezTerm':
            if _open_wezterm_terminal(command):
                return _record('WezTerm')
        elif fallback_terminal == 'cmux':
            if _open_cmux_terminal(command):
                return _record('cmux')

    if _open_terminal_app_terminal(command):
        return _record('Terminal.app')
    if _open_iterm2_terminal(command):
        return _record('iTerm2')
    return False


def close_terminal_with_title(
    title_pattern: str,
    preferred_ide: Optional[str] = None,
    project_path: Optional[str] = None,
    terminal_title: Optional[str] = None
) -> bool:
    """
    Close terminal window/tab with matching title.

    Tries the preferred IDE/terminal first, then falls back to others.

    Args:
        title_pattern: Pattern to match in terminal title.
        preferred_ide: IDE or terminal app to try first (from session metadata).
        project_path: Project path for IDE navigation.
        terminal_title: Exact terminal title to match.

    Returns:
        True if terminal was found and closed.
    """
    if preferred_ide:
        if any(ide in preferred_ide for ide in _JETBRAINS_IDE_NAMES):
            if _close_jetbrains(preferred_ide, project_path, terminal_title):
                return True
        elif preferred_ide in ('VS Code', 'Cursor'):
            if _close_vscode(project_path, terminal_title or title_pattern,
                             ide=preferred_ide):
                return True
        elif preferred_ide == 'Warp':
            if _close_warp(title_pattern):
                return True
        elif preferred_ide == 'WezTerm':
            if _close_wezterm(title_pattern):
                return True
        elif preferred_ide == 'cmux':
            if _close_cmux(title_pattern):
                return True
        elif preferred_ide == 'iTerm2':
            if _close_iterm2(title_pattern):
                return True
        elif preferred_ide == 'Terminal.app':
            if _close_terminal_app(title_pattern):
                return True

    # Preferred IDE failed or unknown — fall back through standalone terminals
    # (Warp/WezTerm last to avoid activating them unexpectedly)
    if _close_terminal_app(title_pattern):
        return True
    if _close_iterm2(title_pattern):
        return True
    if _close_warp(title_pattern):
        return True
    if _close_wezterm(title_pattern):
        return True
    if _close_cmux(title_pattern):
        return True

    return False


def find_terminal_with_title(
    title_pattern: str,
    preferred_ide: Optional[str] = None,
    project_path: Optional[str] = None,
    terminal_title: Optional[str] = None,
    server_pid: Optional[int] = None
) -> bool:
    """
    Find and focus terminal window/tab with matching title.

    Tries the preferred IDE/terminal first, then falls back to others.

    Args:
        title_pattern: Pattern to match in terminal title.
        preferred_ide: IDE or terminal app to try first (from session metadata).
        project_path: Project path for IDE navigation.
        terminal_title: Exact terminal title to match.
        server_pid: PID of the session's Leap server process, used by the
            JetBrains path as a rename-proof fallback identity for the tab
            when its title no longer matches (see ``_navigate_jetbrains``).

    Returns:
        True if terminal was found and focused.
    """
    if preferred_ide:
        if any(ide in preferred_ide for ide in _JETBRAINS_IDE_NAMES):
            if _navigate_jetbrains(preferred_ide, project_path, terminal_title,
                                   server_pid):
                return True
        elif preferred_ide in ('VS Code', 'Cursor'):
            if _navigate_vscode(project_path, terminal_title or title_pattern,
                                ide=preferred_ide):
                return True
        elif preferred_ide == 'Warp':
            if _navigate_warp(title_pattern):
                return True
        elif preferred_ide == 'WezTerm':
            if _navigate_wezterm(title_pattern):
                return True
        elif preferred_ide == 'cmux':
            if _navigate_cmux(title_pattern):
                return True
        elif preferred_ide == 'iTerm2':
            if _navigate_iterm2(title_pattern):
                return True
        elif preferred_ide == 'Terminal.app':
            if _navigate_terminal_app(title_pattern):
                return True
        elif preferred_ide == 'Arduino IDE':
            if _navigate_arduino(title_pattern):
                return True

    # Preferred IDE failed or unknown — fall back through standalone terminals
    # (Warp/WezTerm last to avoid activating them unexpectedly)
    if _navigate_terminal_app(title_pattern):
        return True
    if _navigate_iterm2(title_pattern):
        return True
    if _navigate_warp(title_pattern):
        return True
    if _navigate_wezterm(title_pattern):
        return True
    # probe=False: this is the generic fallback for a navigation that
    # already failed for some other terminal — only do the cheap, focus-
    # preserving workspace-name match, never the surface-probe (which
    # would focus-cycle every cmux surface for a non-cmux session).
    if _navigate_cmux(title_pattern, probe=False):
        return True

    return False


def _parent_pid(pid: int) -> Optional[int]:
    """Return the parent PID of *pid*, or None if it can't be determined.

    Used to derive a JetBrains terminal tab's shell PID from a Leap
    session's server PID: PyCharm spawns one login shell per terminal
    tab, and the Leap server runs as that shell's child — so the shell
    PID (the tab's rename-proof identity) is the server's parent.
    """
    try:
        out = subprocess.run(
            ['ps', '-o', 'ppid=', '-p', str(pid)],
            capture_output=True, text=True, timeout=3,
        )
        ppid = out.stdout.strip()
        return int(ppid) if ppid else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def _navigate_jetbrains(
    ide: str,
    project_path: Optional[str],
    terminal_title: Optional[str],
    server_pid: Optional[int] = None
) -> bool:
    """Navigate to a terminal tab in a JetBrains IDE.

    Polls ``ideScript`` until the Groovy template (see
    ``resources/activate_terminal.groovy``) writes one of three
    sentinels to a temp file:

    * ``QUEUED`` - the tab was located (or no specific tab was
      requested); its selection is queued on the EDT. -> returns True.
    * ``NOTAB``  - the project is open but the requested tab couldn't
      be found (e.g. the user renamed it, and matching it by the
      session's shell PID didn't succeed either). Retrying can't
      help, so we stop and return False rather than reporting a
      false success the way this used to.
    * ``WAITING`` - the project isn't loaded yet; keep polling.

    *server_pid* is the session's Leap server PID. The Groovy uses its
    parent (the login shell PyCharm spawned for the tab) as a
    rename-proof identity: when the tab title no longer matches, it
    selects the tab whose shell process is that PID. Pass None to
    disable the fallback (title matching only).

    Budget is **60 s**, deliberately tighter than
    ``_open_jetbrains_terminal``'s 10 min: navigate is user-triggered
    (they clicked a button and expect a window to come forward),
    not background work, so a long wait would feel like the click
    did nothing.  Early-bail conditions: IDE seen running and then
    disappeared, or IDE never appeared within the 30 s appearance
    grace.
    """
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

    # Result-file the instrumented Groovy writes — see
    # ``_open_jetbrains_terminal`` for why we use a file instead of
    # the subprocess exit code.  ``tmp_script_path`` may not get
    # assigned (if template read / NamedTemporaryFile raises before
    # we reach the inner block) so it stays None and the cleanup
    # loop skips it.
    result_file = tempfile.NamedTemporaryFile(
        mode='w', suffix='.txt', prefix='leap-navresult-', delete=False,
    )
    result_file.close()
    result_path = result_file.name
    tmp_script_path: Optional[str] = None

    try:
        # Read template and substitute values
        with open(groovy_script, 'r') as f:
            template_content = f.read()

        custom_script = template_content
        if project_path:
            custom_script = custom_script.replace(
                'var projectPath = System.getenv("LEAP_PROJECT_PATH")',
                f'var projectPath = "{_escape_groovy(project_path)}"'
            )
        if terminal_title:
            custom_script = custom_script.replace(
                'var terminalTabName = System.getenv("LEAP_TERMINAL_TITLE")',
                f'var terminalTabName = "{_escape_groovy(terminal_title)}"'
            )
        # Rename-proof fallback identity. A user who renames a terminal tab
        # in the IDE strips its "lps <tag>" title, so title matching can no
        # longer find it. The shell PID backing the tab (the server's parent)
        # can't be edited, so the Groovy falls back to matching on it. Empty
        # when unknown — the Groovy then simply skips the PID fallback.
        shell_pid = _parent_pid(server_pid) if server_pid else None
        custom_script = custom_script.replace(
            'var shellPidStr = System.getenv("LEAP_SHELL_PID")',
            f'var shellPidStr = "{shell_pid if shell_pid else ""}"'
        )
        # Always wire up the result path — Python relies on this.
        custom_script = custom_script.replace(
            'var leapResultPath = System.getenv("LEAP_RESULT_PATH")',
            f'var leapResultPath = "{_escape_groovy(result_path)}"'
        )

        with tempfile.NamedTemporaryFile(mode='w', suffix='.groovy', delete=False) as tmp:
            tmp.write(custom_script)
            tmp_script_path = tmp.name

        env = _jetbrains_env()

        # Open/focus the project once; the poll below waits for the
        # IDE to actually load it.
        if project_path:
            try:
                subprocess.run(
                    [ide_cmd, project_path],
                    capture_output=True, env=env, timeout=5,
                )
            except subprocess.TimeoutExpired:
                pass

        deadline = time.monotonic() + 60.0
        appearance_deadline = time.monotonic() + 30.0
        ide_ever_seen = False
        while True:
            running = _is_jetbrains_running(ide)
            if running:
                ide_ever_seen = True
            elif ide_ever_seen:
                return False  # IDE was up and is now gone
            elif time.monotonic() > appearance_deadline:
                return False  # never appeared — bad bundle?

            # Clear stale result from previous iteration.
            try:
                os.unlink(result_path)
            except OSError:
                pass

            try:
                subprocess.run(
                    [ide_cmd, 'ideScript', tmp_script_path],
                    capture_output=True, env=env, timeout=15,
                )
            except subprocess.TimeoutExpired:
                # Same reasoning as the open-helper: an in-flight
                # IPC could still be processed by the IDE — better
                # to bail than risk a second activation request
                # stomping on the first.
                return False

            try:
                with open(result_path, 'r') as f:
                    content = f.read().strip()
                if content == 'QUEUED':
                    return True
                if content == 'NOTAB':
                    # Project is open but the requested tab couldn't be
                    # located (e.g. renamed, and the PID fallback didn't
                    # match either). Retrying won't change that — report
                    # the miss truthfully instead of looping to the deadline.
                    return False
            except (OSError, IOError):
                pass

            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)
    except (subprocess.SubprocessError, OSError):
        pass
    finally:
        # Clean up both temp files on every exit path — even when
        # we bailed out before ``tmp_script_path`` was assigned.
        for p in (tmp_script_path, result_path):
            if not p:
                continue
            try:
                os.unlink(p)
            except OSError:
                pass

    return False


def _navigate_vscode(
    project_path: Optional[str],
    terminal_name: str,
    ide: str = 'VS Code',
) -> bool:
    """Navigate to VS Code/Cursor window and select terminal tab by name.

    Uses AppleScript to focus the correct window (matching the project
    folder name) instead of the CLI, which would open a new window or
    replace the current workspace.
    """
    try:
        app_name = _vscode_applescript_name(ide)
        # Focus the window whose title contains the project folder name
        if project_path:
            folder_name = _escape_applescript(os.path.basename(project_path))
            script = f'''
            tell application "{app_name}"
                activate
                set found to false
                repeat with w in windows
                    if name of w contains "{folder_name}" then
                        set index of w to 1
                        set found to true
                        exit repeat
                    end if
                end repeat
                if not found then
                    -- No matching window; just activate the app
                    activate
                end if
            end tell
            '''
        else:
            script = f'''
            tell application "{app_name}"
                activate
            end tell
            '''
        subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, timeout=5,
        )
        time.sleep(0.3)

        # Use file-based trigger for Leap extension
        # Extension watches ~/.leap-terminal-request and selects the terminal.
        # Atomic write so the extension never reads a half-written request.
        _write_terminal_request(terminal_name)
        # Give the extension a moment to process
        time.sleep(0.1)

        return True

    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _write_terminal_request(content: str) -> bool:
    """Atomically write *content* to ``~/.leap-terminal-request``.

    The Leap editor extension watches this file (fs.watch + a 500 ms poll)
    and reads it with a plain ``readFileSync``.  A non-atomic
    truncate-then-write can be read mid-flight: a partial payload misses
    the known ``focusComposer:`` / ``closeComposer:`` prefix, falls through
    to the extension's catch-all "select terminal by name", and is then
    ``unlink``-ed - silently dropping the request.  Writing to a temp file
    in the same directory and ``os.replace``-ing it onto the target makes
    the swap atomic, so a reader always sees either the previous file or
    the complete new one.  ``os.replace`` also overwrites any stale,
    never-consumed request in one step.  Returns False on failure.
    """
    request_file = os.path.expanduser('~/.leap-terminal-request')
    target_dir = os.path.dirname(request_file)
    try:
        fd, tmp = tempfile.mkstemp(dir=target_dir, prefix='.leap-req-')
    except OSError:
        return False
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
        os.replace(tmp, request_file)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def focus_cursor_window(folder: str,
                        composer_id: Optional[str] = None) -> bool:
    """Bring the Cursor window for *folder* to the front, and optionally
    focus a specific Agent tab inside it.

    Cursor exposes nothing clickable to Accessibility (its Agent tabs are
    invisible to AX), so the *window* is raised via the System Events
    bridge: match the native window whose title contains the project
    folder's basename, ``AXRaise`` it, and make Cursor frontmost.

    For tab-level focus we hand off to the Leap Cursor extension: after
    raising the window we drop a ``focusComposer:<composer_id>`` request
    in ``~/.leap-terminal-request``.  The extension running in the now
    foreground window calls Cursor's id-based composer-focus command.
    This is best-effort - if the extension isn't installed or the Cursor
    build gates that command, the window-level raise still happened.

    Returns ``True`` when Cursor was brought forward, ``False`` when it
    isn't running or the raise script failed.
    """
    raised = _raise_editor_window('Cursor', 'Cursor', folder)

    # Hand off tab-level focus to the Leap Cursor extension.  Write the
    # request AFTER raising the window so the (now foreground) window
    # that owns the tab is the one whose extension acts on it - other
    # windows skip it via the extension's focus gate.  AXRaise returns
    # before the WindowServer finishes activating the window, so settle
    # briefly first: otherwise, with multiple Cursor windows open, a
    # still-foreground non-owning window's extension could consume the
    # request (its focus gate passes) and silently drop it (it doesn't own
    # the composer, so no tab switches, but it unlinks the file).
    if raised:
        time.sleep(0.3)
        if composer_id:
            _write_terminal_request(f'focusComposer:{composer_id}')

    return raised


def focus_vscode_chat_session(folder: str,
                              session_id: Optional[str] = None) -> bool:
    """Bring the VS Code window for *folder* to the front, and optionally
    open a specific Copilot Chat session inside it.

    VS Code sibling of :func:`focus_cursor_window` - same System Events
    window raise (match the native window whose title contains the
    project folder's basename), then session-level focus is handed off to
    the Leap extension via a ``focusChatSession:<session_id>`` request:
    the extension opens the session's ``vscode-chat-session://local/...``
    resource, for which VS Code registers a chat editor.  Best-effort -
    if the extension isn't installed the window-level raise still
    happened.

    Returns ``True`` when VS Code was brought forward, ``False`` when it
    isn't running or the raise script failed.
    """
    raised = _raise_editor_window('VS Code', 'Code', folder)
    # Same settle-then-request dance as the Cursor path (see above): only
    # the focused window's extension consumes the shared request file.
    if raised:
        time.sleep(0.3)
        if session_id:
            _write_terminal_request(f'focusChatSession:{session_id}')
    return raised


def rename_vscode_chat_session(folder: str, session_id: str) -> bool:
    """Open VS Code's own rename input for a Copilot Chat session.

    Raises the owning VS Code window, then asks the Leap extension to run
    ``agentSession.rename`` for this session (via a
    ``renameChatSession:<id>`` request).  VS Code shows its native input
    box pre-filled with the current title; the new title persists into
    the chat-session store, so the monitor row label follows on the next
    scan.  Best-effort; returns whether the window raise succeeded.
    """
    if not session_id:
        return False
    raised = _raise_editor_window('VS Code', 'Code', folder)
    if raised:
        time.sleep(0.3)
        _write_terminal_request(f'renameChatSession:{session_id}')
    return raised


def _raise_editor_window(ide_label: str, process_name: str,
                         folder: str) -> bool:
    """Raise the *ide_label* (Cursor / VS Code) window for *folder*.

    Neither editor exposes clickable AX elements, so the *window* is
    raised via the System Events bridge: match the native window whose
    title contains the project folder's basename, ``AXRaise`` it, and
    make the app frontmost.  Returns ``True`` when the app was brought
    forward, ``False`` when it isn't running or the script failed.
    """
    if not _is_vscode_running(ide_label):
        return False
    base = os.path.basename(folder.rstrip('/')) if folder else ''
    safe = _escape_applescript(base)
    proc = _escape_applescript(process_name)
    script = (
        'with timeout of 8 seconds\n'
        'tell application "System Events"\n'
        f'  if not (exists process "{proc}") then return "noproc"\n'
        f'  tell process "{proc}"\n'
        '    set frontmost to true\n'
        '    repeat with w in windows\n'
        f'      if name of w contains "{safe}" then\n'
        '        try\n'
        '          perform action "AXRaise" of w\n'
        '        end try\n'
        '        exit repeat\n'
        '      end if\n'
        '    end repeat\n'
        '  end tell\n'
        'end tell\n'
        'return "ok"\n'
        'end timeout'
    )
    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=12,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.returncode == 0 and 'noproc' not in (result.stdout or '')


def close_cursor_composer(folder: str, composer_id: str) -> bool:
    """Close a Cursor Agent/Composer tab by id (the chat stays in history).

    Raises the owning Cursor window first (so the now-foreground window -
    the only one whose extension acts, via its focus gate, and the only
    one that has this composer in ``selectedComposerIds``) then drops a
    ``closeComposer:<id>`` request for the Leap Cursor extension, which
    runs ``composer.closeComposerTab``.  Best-effort; returns whether the
    window raise succeeded.
    """
    if not composer_id:
        return False
    # focus_cursor_window already settles after the raise (the now-foreground
    # window that owns this composer in its selectedComposerIds is the one
    # whose extension consumes the close request - not a different still-
    # foreground window), so no extra sleep is needed here.
    raised = focus_cursor_window(folder)  # raise the window (no tab focus)
    if raised:
        _write_terminal_request(f'closeComposer:{composer_id}')
    return raised


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


def _navigate_arduino(_title_pattern: str) -> bool:
    """Navigate to Arduino IDE.

    Arduino IDE (Theia-based) has a single terminal, so just
    activate the app window.
    """
    script = '''
    tell application "Arduino IDE"
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
                var tabName = "{_escape_groovy(terminal_title)}"
                var content = contentManager.findContent(tabName)
                if (content == null) {{
                    var contents = contentManager.getContents()
                    var bestLen = 0
                    var matchCount = 0
                    for (var i = 0; i < contents.length; i++) {{
                        var c = contents[i]
                        var name = c.getDisplayName()
                        if (name == null) continue
                        var matched = false
                        var matchLen = name.length()
                        var ellIdx = name.indexOf("\u2026")
                        if (ellIdx >= 0) {{
                            var prefix = name.substring(0, ellIdx)
                            var suffix = name.substring(ellIdx + 1)
                                .replaceFirst("\\\\s+\\\\(\\\\d+\\\\)\\$", "")
                            matched = tabName.startsWith(prefix) && tabName.endsWith(suffix)
                            matchLen = prefix.length() + suffix.length()
                        }} else {{
                            matched = tabName.contains(name)
                        }}
                        if (matched) {{
                            if (matchLen > bestLen) {{
                                content = c
                                bestLen = matchLen
                                matchCount = 1
                            }} else if (matchLen == bestLen) {{
                                matchCount++
                            }}
                        }}
                    }}
                    if (matchCount > 1) {{
                        content = null
                    }}
                }}
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


def _close_vscode(
    project_path: Optional[str],
    terminal_name: str,
    ide: str = 'VS Code',
) -> bool:
    """Close a terminal tab in VS Code/Cursor by writing a close request file."""
    try:
        env, code_path = _vscode_env_and_path(ide)
        if not code_path:
            return False

        if project_path:
            subprocess.run(
                [code_path, '--reuse-window', project_path],
                capture_output=True,
                timeout=5,
                env=env
            )
            time.sleep(0.3)

        _write_terminal_request(f'close:{terminal_name}')
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


_WEZTERM_BUNDLE_ID = 'com.github.wez.wezterm'


def _find_wezterm_cli() -> Optional[str]:
    """Find the wezterm CLI binary.

    Checks PATH first, then known .app bundle locations, then falls back
    to ``mdfind`` (Spotlight) to locate the app anywhere on disk.
    """
    cli = shutil.which('wezterm')
    if cli:
        return cli
    for app_dir in ('/Applications', os.path.expanduser('~/Applications')):
        candidate = os.path.join(app_dir, 'WezTerm.app', 'Contents', 'MacOS', 'wezterm')
        if os.path.isfile(candidate):
            return candidate
    # Spotlight fallback — finds the app even if installed in ~/Downloads etc.
    try:
        result = subprocess.run(
            ['mdfind', f'kMDItemCFBundleIdentifier == "{_WEZTERM_BUNDLE_ID}"'],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            candidate = os.path.join(line.strip(), 'Contents', 'MacOS', 'wezterm')
            if os.path.isfile(candidate):
                return candidate
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _wezterm_list_panes(cli: str) -> list[dict[str, Any]]:
    """Return the list of panes from ``wezterm cli list --format json``."""
    try:
        result = subprocess.run(
            [cli, 'cli', 'list', '--format', 'json'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return []


def _activate_wezterm() -> bool:
    """Bring WezTerm to the foreground."""
    try:
        result = subprocess.run(
            ['open', '-a', 'WezTerm'],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        pass
    return False


def _navigate_wezterm(title_pattern: str) -> bool:
    """Navigate to a WezTerm pane whose title contains *title_pattern*.

    Uses ``wezterm cli list`` to find the pane, then
    ``wezterm cli activate-pane`` to focus it.
    """
    cli = _find_wezterm_cli()
    if not cli:
        return False

    panes = _wezterm_list_panes(cli)
    for pane in panes:
        title = pane.get('title', '')
        if title_pattern in title:
            pane_id = pane.get('pane_id')
            if pane_id is None:
                continue
            try:
                subprocess.run(
                    [cli, 'cli', 'activate-pane', '--pane-id', str(pane_id)],
                    capture_output=True, timeout=5,
                )
            except (subprocess.SubprocessError, OSError):
                return False
            _activate_wezterm()
            return True
    return False


def _close_wezterm(title_pattern: str) -> bool:
    """Close a WezTerm pane whose title contains *title_pattern*."""
    cli = _find_wezterm_cli()
    if not cli:
        return False

    panes = _wezterm_list_panes(cli)
    for pane in panes:
        title = pane.get('title', '')
        if title_pattern in title:
            pane_id = pane.get('pane_id')
            if pane_id is None:
                continue
            try:
                subprocess.run(
                    [cli, 'cli', 'kill-pane', '--pane-id', str(pane_id)],
                    capture_output=True, timeout=5,
                )
                return True
            except (subprocess.SubprocessError, OSError):
                return False
    return False


def _open_wezterm_terminal(command: str) -> bool:
    """Open a new WezTerm tab and run *command*."""
    cli = _find_wezterm_cli()
    if not cli:
        return False

    try:
        result = subprocess.run(
            [cli, 'cli', 'spawn', '--', command],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            _activate_wezterm()
            return True
    except (subprocess.SubprocessError, OSError):
        pass
    return False


_CMUX_BUNDLE_ID = 'com.cmuxterm.app'


def _find_cmux_cli() -> Optional[str]:
    """Find the bundled cmux CLI binary (``new-workspace`` etc.).

    Checks PATH (the user-created ``/usr/local/bin/cmux`` symlink), then
    the binary shipped inside ``cmux.app``, then Spotlight as a last
    resort.
    """
    cli = shutil.which('cmux')
    if cli:
        return cli
    for app_dir in ('/Applications', os.path.expanduser('~/Applications')):
        candidate = os.path.join(
            app_dir, 'cmux.app', 'Contents', 'Resources', 'bin', 'cmux')
        if os.path.isfile(candidate):
            return candidate
    # Spotlight fallback — finds the app even if installed outside /Applications.
    try:
        result = subprocess.run(
            ['mdfind', f'kMDItemCFBundleIdentifier == "{_CMUX_BUNDLE_ID}"'],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            candidate = os.path.join(
                line.strip(), 'Contents', 'Resources', 'bin', 'cmux')
            if os.path.isfile(candidate):
                return candidate
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def _activate_cmux() -> bool:
    """Bring cmux to the foreground."""
    try:
        result = subprocess.run(
            ['open', '-a', 'cmux'],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        pass
    return False


def _navigate_cmux(title_pattern: str, probe: bool = True) -> bool:
    """Navigate to (focus) a cmux terminal whose title contains *title_pattern*.

    Driven via cmux's AppleScript dictionary (``cmux.sdef``:
    ``application > windows > tabs (workspaces) > terminals``) rather
    than its bundled CLI, which defaults to ``socketControlMode:
    cmuxOnly`` and rejects an outside process like the monitor.

    cmux is awkward to navigate because it does *not* expose a
    per-surface title: every ``terminal``'s ``name`` reads as a generic
    "Terminal", and a shell's OSC title (the server sets ``lps <tag>``)
    is surfaced only as the **workspace (tab) name**, and only for the
    workspace's *active* surface.  So we use two passes (both verified
    against a live app):

    1. Match the workspace name directly - hits when the session's
       surface is the active one (or is the workspace's only surface).
    2. If that misses, the target surface is inactive and its title is
       hidden, so probe: focus each surface in a multi-surface workspace
       and re-read the workspace name; on a match leave that surface
       focused (that *is* the jump), otherwise restore the workspace's
       original focus.  If no workspace matches at all (e.g. the session
       was closed out-of-band), the surface that was frontmost before
       probing is restored so the jump doesn't leave cmux parked on a
       random workspace.

    *probe* gates pass 2.  The preferred-IDE path (the recorded terminal
    really is cmux) passes ``True``; the generic fallback chain passes
    ``False`` so a *failed* navigation for some *other* terminal doesn't
    focus-cycle every cmux surface hunting for a title that was never
    there.  Pass 1 is always cheap and leaves focus untouched until a
    match.

    Per-workspace work is wrapped in ``try`` so one odd workspace (e.g. a
    browser-only surface with no focused terminal) can't abort the whole
    search.  The app-level ``terminals`` element is unreliable (reports
    count 0), so both passes walk ``windows > tabs``.  Guarded on cmux
    already running so a "jump to" whose recorded terminal is stale
    doesn't cold-launch cmux.
    """
    if _get_app_pid(_CMUX_BUNDLE_ID) is None:
        return False
    safe_pattern = _escape_applescript(title_pattern)
    probe_block = f'''
        set savedTerm to missing value
        try
            set savedTerm to focused terminal of (selected tab of (front window))
        end try
        repeat with w in windows
            repeat with tb in tabs of w
                try
                    set surfs to terminals of tb
                    if (count of surfs) > 1 then
                        set orig to focused terminal of tb
                        repeat with t in surfs
                            focus t
                            delay 0.2
                            if name of tb contains "{safe_pattern}" then
                                activate
                                return true
                            end if
                        end repeat
                        focus orig
                    end if
                end try
            end repeat
        end repeat
        try
            if savedTerm is not missing value then focus savedTerm
        end try
    ''' if probe else ''
    script = f'''
    tell application "cmux"
        repeat with w in windows
            repeat with tb in tabs of w
                try
                    if name of tb contains "{safe_pattern}" then
                        focus (focused terminal of tb)
                        activate
                        return true
                    end if
                end try
            end repeat
        end repeat
        {probe_block}
    end tell
    return false
    '''
    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=20,
        )
        return result.returncode == 0 and 'true' in result.stdout
    except (subprocess.SubprocessError, OSError):
        pass
    return False


def _close_cmux(title_pattern: str) -> bool:
    """Close a cmux terminal whose title contains *title_pattern*.

    cmux exposes a surface's OSC title (``lps <tag>``) only as the
    workspace (tab) name, and only while that surface is active (see
    ``_navigate_cmux``).  We match the workspace name and close its
    focused terminal; with cmux's default
    ``keepWorkspaceOpenWhenClosingLastSurface = false`` that also closes
    the now-empty workspace.  Unlike navigation we deliberately don't
    probe inactive surfaces here — closing via the X button is rare and
    focusing surfaces just to close one would be more disruptive than
    the occasional miss.

    Guarded on cmux running so the fallback close-chain never launches
    cmux just to hunt for a tab that isn't there.
    """
    if _get_app_pid(_CMUX_BUNDLE_ID) is None:
        return False
    safe_pattern = _escape_applescript(title_pattern)
    script = f'''
    tell application "cmux"
        repeat with w in windows
            repeat with tb in tabs of w
                try
                    if name of tb contains "{safe_pattern}" then
                        close (focused terminal of tb)
                        return true
                    end if
                end try
            end repeat
        end repeat
    end tell
    return false
    '''
    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and 'true' in result.stdout
    except (subprocess.SubprocessError, OSError):
        pass
    return False


def _open_cmux_terminal(command: str) -> bool:
    """Open a new cmux workspace (tab) and run *command*.

    Primary path: the bundled ``cmux`` CLI's ``new-workspace --command``,
    which sends the command text **plus Enter** so it actually runs.
    This only works when the monitor can reach cmux's control socket
    (``socketControlMode`` set to ``allowAll``/``password``, or the
    monitor itself was launched from inside cmux); the default
    ``cmuxOnly`` rejects outside processes, in which case we fall back.

    Fallback path: AppleScript ``new tab`` + ``input text`` with a
    trailing return.  Verified against a live cmux: ``input text`` runs
    the line as a normal command (it is *not* held by bracketed paste),
    so the session starts in the new tab with no manual keypress.
    """
    cli = _find_cmux_cli()
    if cli:
        try:
            result = subprocess.run(
                [cli, 'new-workspace', '--command', command, '--focus', 'true'],
                capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                _activate_cmux()
                return True
        except (subprocess.SubprocessError, OSError):
            pass

    safe_command = _escape_applescript(command)
    script = f'''
    tell application "cmux"
        activate
        set newTab to new tab
        set theTerm to focused terminal of newTab
        input text ("{safe_command}" & return) to theTerm
        focus theTerm
    end tell
    return true
    '''
    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        pass
    return False


_WARP_BUNDLE_ID = 'dev.warp.Warp-Stable'


def _get_app_pid(bundle_id: str) -> Optional[int]:
    """Get PID for a running app by bundle identifier.

    Uses NSWorkspace iteration instead of
    runningApplicationsWithBundleIdentifier_ because the latter can
    return empty results when called from a background thread in a
    py2app bundle.
    """
    try:
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        for app in workspace.runningApplications():
            if app.bundleIdentifier() == bundle_id:
                return app.processIdentifier()
    except Exception as exc:
        logger.debug("_get_app_pid error: %s", exc)
    return None


def _check_accessibility_trusted() -> bool:
    """Check if this process has Accessibility permission.

    If not trusted, triggers the macOS system prompt to request permission.
    This handles the case where the .app was rebuilt (changing its ad-hoc
    code signature) and the old Accessibility entry is now stale.
    """
    trusted = AXIsProcessTrusted()
    if trusted:
        return True

    # Not trusted — trigger the system prompt so the user can re-authorize.
    try:
        options = {"AXTrustedCheckOptionPrompt": kCFBooleanTrue}
        AXIsProcessTrustedWithOptions(options)
    except Exception:
        pass

    logger.warning("Accessibility permission not granted for this process. "
                    "Re-add Leap Monitor in System Settings > Privacy & "
                    "Security > Accessibility after rebuilding the app.")
    return False


def _ensure_app_focused(ns_app: Any) -> bool:
    """Activate an app and wait until it is actually frontmost (up to 2s).

    ``activateWithOptions_`` is asynchronous — this helper polls
    ``isActive`` so that subsequent CGEvent keystrokes hit the right app.
    """
    ns_app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    for _ in range(20):
        if ns_app.isActive():
            return True
        time.sleep(0.1)
    return ns_app.isActive()


def _send_keystroke(
    keycode: int, cmd: bool = False, shift: bool = False, ctrl: bool = False,
) -> bool:
    """Send a keystroke to the frontmost application using CGEvent."""
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
    pid = _get_app_pid(_WARP_BUNDLE_ID)
    if pid is None:
        return False

    if not _check_accessibility_trusted():
        return False

    app_ref = AXUIElementCreateApplication(pid)
    err, windows = AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
    if err != kAXErrorSuccess or not windows:
        return False

    ns_app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)

    def _get_title(window_ref: Any) -> str:
        e, t = AXUIElementCopyAttributeValue(window_ref, "AXTitle", None)
        return str(t) if e == kAXErrorSuccess and t else ''

    def _raise_and_activate(window_ref: Any) -> None:
        AXUIElementPerformAction(window_ref, "AXRaise")
        if ns_app:
            ns_app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)

    # Phase 1: quick scan — check if any window's active tab already matches
    for window in windows:
        if title_pattern in _get_title(window):
            _raise_and_activate(window)
            return True

    # Phase 2: raise each window and cycle through its tabs
    # Cmd+Shift+] = next tab in Warp  (keycode 30 = ']')
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
                return True
            if current_title == initial_title:
                break  # cycled back to start — tab not in this window

    # Phase 2 activated Warp to cycle tabs — switch back to the monitor
    try:
        AppKit.NSRunningApplication.currentApplication().activateWithOptions_(
            AppKit.NSApplicationActivateIgnoringOtherApps)
    except Exception:
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
    If Warp is not running, launches it and types the command into its
    initial session (no extra window).
    Falls back to Launch Configuration if keystroke approach fails.
    """
    was_running = _get_app_pid(_WARP_BUNDLE_ID) is not None

    if not was_running:
        # Launch Warp and wait for it to be ready
        try:
            subprocess.run(
                ['open', '-a', 'Warp'], capture_output=True, timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            return _open_warp_via_launch_config(command)

        # Wait for Warp process to appear (up to 5s)
        pid = None
        for _ in range(25):
            time.sleep(0.2)
            pid = _get_app_pid(_WARP_BUNDLE_ID)
            if pid is not None:
                break
        if pid is not None and _check_accessibility_trusted():
            # Type command into Warp's initial session (no Cmd+T)
            if _type_command_in_warp(pid, command):
                return True
        # Fallback
        return _open_warp_via_launch_config(command)

    # Warp is already running — open a new tab
    pid = _get_app_pid(_WARP_BUNDLE_ID)
    if pid is not None and _check_accessibility_trusted():
        if _open_warp_tab_with_keystroke(pid, command):
            return True

    # Keystroke approach failed — use Launch Configuration
    return _open_warp_via_launch_config(command)


def _type_command_in_warp(pid: int, command: str) -> bool:
    """Type a command into Warp's current session (no new tab).

    Used when Warp was just launched and we want to use its initial
    session.  Waits for Warp's window to appear, dismisses the
    "New terminal session" overlay, then pastes and executes the command.
    """
    ns_app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if not ns_app:
        return False
    _ensure_app_focused(ns_app)

    # Wait for Warp's window to appear (just launched, may take a moment)
    app_ref = AXUIElementCreateApplication(pid)
    windows = None
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        time.sleep(0.3)
        err, windows = AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
        if err == kAXErrorSuccess and windows:
            break
    if not windows:
        return False

    # Wait a bit more for the shell to be ready
    time.sleep(1.0)

    # Copy command to clipboard
    try:
        proc = subprocess.run(
            ['pbcopy'], input=command.encode('utf-8'), timeout=2,
        )
        if proc.returncode != 0:
            return False
    except (subprocess.SubprocessError, OSError):
        return False

    # Re-focus Warp (user may have switched away during the wait)
    if not _ensure_app_focused(ns_app):
        return False

    # Dismiss overlay and paste command (same retry logic as tab approach)
    for attempt in range(4):
        time.sleep(0.3 if attempt == 0 else 0.8)
        _ensure_app_focused(ns_app)    # Re-focus before each retry too
        _send_keystroke(53)            # Escape (dismiss overlay)
        time.sleep(0.2)
        _send_keystroke(32, ctrl=True)  # Ctrl+U (clear input line)
        time.sleep(0.1)
        _send_keystroke(9, cmd=True)   # Cmd+V (paste)
        time.sleep(0.15)
        _send_keystroke(36)            # Return (execute)
        time.sleep(0.5)

        # Check if the command executed
        def _title() -> str:
            e, t = AXUIElementCopyAttributeValue(windows[0], "AXTitle", None)
            return str(t) if e == kAXErrorSuccess and t else ''

        if 'lps ' in _title() or 'lpc ' in _title():
            return True

    return True  # Exhausted retries — command may still execute


def _open_warp_tab_with_keystroke(pid: int, command: str) -> bool:
    """Open a new tab in the frontmost Warp window and run a command.

    Uses Cmd+T to create the tab, waits for the shell to initialize by
    polling the window title for a change, dismisses Warp's "New terminal
    session" overlay, then pastes the command.  Includes a retry loop to
    handle timing variations in overlay appearance.
    Requires Accessibility permission.
    """
    ns_app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if not ns_app:
        return False
    _ensure_app_focused(ns_app)

    # Get the frontmost window and its current title (e.g. "lps tag")
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
    # change from the server tab's title (e.g. "lps tag") to the
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

    # Re-focus Warp (user may have switched away during the wait)
    if not _ensure_app_focused(ns_app):
        return False

    # Warp shows a "New terminal session" overlay on new tabs that
    # captures Enter.  The overlay can appear at varying times after
    # the shell is ready.  Strategy: try Escape → paste → Enter, then
    # check the title to verify the command ran.  If it didn't, retry.
    for attempt in range(4):
        # Wait progressively longer for the overlay to appear
        time.sleep(0.3 if attempt == 0 else 0.8)
        _ensure_app_focused(ns_app)    # Re-focus before each retry too
        _send_keystroke(53)            # Escape (dismiss overlay)
        time.sleep(0.2)
        _send_keystroke(32, ctrl=True) # Ctrl+U (clear input line)
        time.sleep(0.1)
        _send_keystroke(9, cmd=True)   # Cmd+V (paste into clean input)
        time.sleep(0.15)
        _send_keystroke(36)            # Return (execute)
        time.sleep(0.5)

        # Check if the command executed — leap sets the title to "lps/lpc *"
        current = _title()
        if 'lps ' in current or 'lpc ' in current:
            return True

    return True  # Exhausted retries — command may still execute


def _open_warp_via_launch_config(command: str) -> bool:
    """Open a new Warp window via Launch Configuration.

    Creates a temporary YAML launch config in ~/.warp/launch_configurations/
    and opens it via the warp:// URI scheme.  Used when Warp is not running
    or Accessibility is unavailable.
    """
    config_name = f"leap-{uuid.uuid4().hex[:8]}"
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


def _is_vscode_running(ide: str = 'VS Code') -> bool:
    """Return True if VS Code or Cursor is currently running.

    Counterpart to ``_is_jetbrains_running`` for the VS Code / Cursor
    poll loops.  Matched primarily by ``localizedName`` because
    Cursor's bundle id (``com.todesktop.230313mzl4w4u92``) doesn't
    contain ``'cursor'``; VS Code's name-and-bundle both contain
    ``'visual studio code'`` / ``'vscode'`` so we accept either.
    """
    try:
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            name = (app.localizedName() or '').lower()
            bundle = (app.bundleIdentifier() or '').lower()
            if ide == 'Cursor':
                if 'cursor' in name:
                    return True
            elif 'visual studio code' in name or 'vscode' in bundle:
                return True
    except Exception:
        logger.debug("_is_vscode_running error", exc_info=True)
        return True
    return False


def _is_jetbrains_running(ide: str) -> bool:
    """Return True if a JetBrains app matching *ide* is currently running.

    Used by ``_open_jetbrains_terminal``'s poll loop as a liveness check:
    if the IDE was once running and is no longer (e.g. the user closed
    it, or it crashed during cold-start), keep polling against nothing
    is pointless — bail to the fallback chain.

    Matches via lower-cased substring against ``localizedName`` and
    ``bundleIdentifier``; ``'IntelliJ IDEA'`` is normalised to
    ``'intellij'`` so it matches ``com.jetbrains.intellij`` for any
    edition.  On any NSWorkspace error returns True so a transient
    Cocoa failure doesn't spuriously bail the move.
    """
    needle = ide.lower().replace(' idea', '').strip()
    try:
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            name = (app.localizedName() or '').lower()
            bundle = (app.bundleIdentifier() or '').lower()
            if needle and (needle in name or needle in bundle):
                return True
    except Exception:
        logger.debug("_is_jetbrains_running error", exc_info=True)
        return True
    return False


def is_jetbrains_project_open(
    app_path: Optional[str],
    project_path: Optional[str],
) -> bool:
    """Best-effort: True iff the JetBrains *app_path* currently has
    *project_path* open.

    Used to decide whether to skip the ``.idea/.name`` alias prompt.
    JetBrains reads ``.idea/.name`` only when it *loads* a project, so
    relabelling one that's already open is futile - ``open -a`` merely
    focuses the live window (no reload) and the IDE overwrites our write
    on its next workspace save.

    Works for every JetBrains IDE: resolves the launcher from the picked
    ``.app`` bundle via ``JETBRAINS_IDES`` (the full ``cmd -> display name``
    map), so RubyMine/CLion/DataGrip are covered too - not just the subset in
    ``_IDE_CMD_MAP`` wired for "move session".

    Conservative by design: returns False whenever it can't *positively*
    confirm the project is open - not a recognised JetBrains bundle, IDE not
    running, or any ideScript error/timeout. A False just means "go ahead and
    prompt", which is the pre-probe behaviour, so a probe failure never makes
    things worse.

    Runs synchronously on the caller's thread; gated on
    ``_is_jetbrains_running`` first so we never cold-start an IDE just to
    probe it, and capped at a short timeout so a busy/indexing IDE can't
    stall the caller (it falls through to "prompt" instead).
    """
    if not app_path or not project_path:
        return False
    bundle = os.path.basename(app_path.rstrip('/'))
    ide_cmd: Optional[str] = None
    ide_name: Optional[str] = None
    for cmd, display in JETBRAINS_IDES.items():
        if display in bundle:
            ide_cmd, ide_name = cmd, display
            break
    if not ide_cmd or not ide_name:
        return False  # Unrecognised JetBrains bundle - default to prompting.
    if not _is_jetbrains_running(ide_name):
        return False  # Not running -> the project can't be open.

    # Pin to the user-picked bundle's binary so ideScript reaches the
    # instance we'll actually open (mirrors _open_jetbrains_terminal).
    candidate = os.path.join(app_path, 'Contents', 'MacOS', ide_cmd)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        ide_cmd = candidate

    result_path: Optional[str] = None
    tmp_script_path: Optional[str] = None
    try:
        result_file = tempfile.NamedTemporaryFile(
            mode='w', suffix='.txt', prefix='leap-ideprobe-', delete=False,
        )
        result_file.close()
        result_path = result_file.name

        groovy_script = f'''import com.intellij.openapi.project.ProjectManager
import java.io.FileWriter

var allProjects = ProjectManager.getInstance().getOpenProjects()
var found = false
for (var i = 0; i < allProjects.length; i++) {{
    var bp = allProjects[i].getBasePath()
    if (bp != null && bp.equals("{_escape_groovy(project_path)}")) {{
        found = true
        break
    }}
}}
var fw = new FileWriter("{_escape_groovy(result_path)}")
fw.write(found ? "OPEN" : "CLOSED")
fw.close()
'''
        env = _jetbrains_env()
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.groovy', delete=False,
        ) as tmp:
            tmp.write(groovy_script)
            tmp_script_path = tmp.name
        try:
            subprocess.run(
                [ide_cmd, 'ideScript', tmp_script_path],
                capture_output=True, timeout=8, env=env,
            )
        except subprocess.TimeoutExpired:
            return False
        try:
            with open(result_path) as f:
                return f.read().strip() == 'OPEN'
        except OSError:
            return False
    except Exception:
        logger.debug("is_jetbrains_project_open error", exc_info=True)
        return False
    finally:
        for p in (result_path, tmp_script_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _open_jetbrains_terminal(
    ide: str,
    project_path: Optional[str],
    command: str,
    should_cancel: Optional[Callable[[], bool]] = None,
    project_already_open: bool = False,
    ide_app_path: Optional[str] = None,
) -> bool:
    """Open a new terminal tab in JetBrains IDE and run a command.

    Polls the IDE's scripting engine for up to 10 minutes.  Cold-start
    of PyCharm/IDEA can take 30 s+ on slower machines or first launch
    (indexes, plugins); on a fresh install with a large project we've
    seen multi-minute waits.  A single short attempt would force a
    Terminal.app fallback every time the IDE wasn't already running —
    we'd rather make the user wait than surprise them.

    The poll bails early if:
      * ``should_cancel`` is provided and returns True (e.g. the user
        removed the row via the X button), or
      * the IDE process was seen running and then disappeared (the
        user closed the IDE, or it crashed mid-startup), or
      * the IDE never appeared at all within the appearance grace
        period (90 s).

    If ``project_already_open`` is True the function skips the
    ``[ide_cmd, project_path]`` open call — the caller is responsible
    for having already invoked ``open -a`` on the bundle (e.g. the
    Move-to-IDE flow does this for reliability: the JetBrains
    Toolbox-generated CLI shims use ``open -na`` which forces a
    second instance and the activation is unreliable; ``open -a`` on
    the exact ``.app`` path the user picked bypasses that and is
    guaranteed to focus the right window).

    If ``ide_app_path`` is given, the per-call CLI binary is resolved
    *inside that .app's* ``Contents/MacOS/`` directory — not via
    ``_jetbrains_env()``'s PATH walk.  Important when the user has
    multiple PyCharm installs (e.g. CE + Pro, or several Toolbox
    versions): otherwise ``open -a`` opens the picked instance, but
    the ``[ide_cmd, ...]`` subprocess invocations end up talking to
    whichever installation comes first on PATH — typically a
    different instance, leading to ``ideScript`` landing in the
    wrong IDE and the poll ``WAITING`` until it times out.
    """
    ide_cmd = _IDE_CMD_MAP.get(ide)
    if not ide_cmd:
        return False

    # Pin the CLI binary to the user-picked .app when we have it,
    # falling back to PATH-resolution otherwise (callers without an
    # .app context).
    if ide_app_path:
        candidate = os.path.join(ide_app_path, 'Contents', 'MacOS', ide_cmd)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            ide_cmd = candidate

    # Result file the Groovy script writes to.  We use this — *not*
    # ideScript's exit code — to detect success, because:
    #   * ``ideScript`` swallows uncaught exceptions and returns 0
    #     anyway, so a top-level throw can't signal failure.
    #   * ``System.exit(N)`` would propagate, but ``ideScript`` runs
    #     inside the *running* IDE (forwarded via IPC), so calling
    #     ``System.exit`` would kill the user's actual IDE.  Hard no.
    # The Groovy writes one of three sentinels synchronously before
    # returning to ``ideScript``:
    #   * ``QUEUED``  — target project found, terminal-creation work
    #                   queued onto EDT.  Treat as success.
    #   * ``WAITING`` — project_path was provided but isn't yet in
    #                   ``ProjectManager.getOpenProjects()`` (cold-
    #                   start hasn't finished loading it).  Retry.
    #   * absent      — script didn't run (IDE not ready / IPC
    #                   failed).  Retry.
    result_file = tempfile.NamedTemporaryFile(
        mode='w', suffix='.txt', prefix='leap-ideresult-', delete=False,
    )
    result_file.close()
    result_path = result_file.name
    tmp_script_path: Optional[str] = None

    project_match = ""
    if project_path:
        # Synchronous loop at top level (NOT inside invokeLater) so the
        # ``WAITING`` branch can short-circuit the script before any
        # EDT work is queued.
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
import java.io.FileWriter

var allProjects = ProjectManager.getInstance().getOpenProjects()
var targetProject = null
{project_match}

// When project_path is given, only accept an exact match - never
// fall back to ``allProjects[0]`` (which is some restored-session
// project, not what the user asked for).  When project_path is
// empty, any open project is acceptable.
if (targetProject == null && allProjects.length > 0 && {('false' if project_path else 'true')}) {{
    targetProject = allProjects[0]
}}

if (targetProject == null) {{
    // Project not loaded yet - Python's poll loop will retry.
    var fw = new FileWriter("{_escape_groovy(result_path)}")
    fw.write("WAITING")
    fw.close()
    return
}}

var p = targetProject
IDE.application.invokeLater {{
    var terminalManager = TerminalToolWindowManager.getInstance(p)
    var widget = terminalManager.createLocalShellWidget(p.getBasePath(), "leap")
    // ``createLocalShellWidget`` usually shows the Terminal tool window
    // and selects the new tab, but only when the tool window is in a
    // normal docked state.  If the user has it detached / minimised /
    // floating, the new tab can land off-screen unfocused.  Explicit
    // ``show + activate`` guarantees the new tab is the visible one
    // regardless of layout state.
    var tw = ToolWindowManager.getInstance(p).getToolWindow("Terminal")
    if (tw != null) {{
        tw.show(null)
        tw.activate(null, true, true)
    }}
    new Thread({{
        Thread.sleep(500)
        IDE.application.invokeLater {{
            widget.executeCommand("{_escape_groovy(command)}")
        }}
    }} as Runnable).start()
}}

// Synchronous: project found, EDT work queued.  Python treats this
// as success even though the terminal hasn't actually been rendered
// yet - the invokeLater above is what does that, asynchronously.
var fw = new FileWriter("{_escape_groovy(result_path)}")
fw.write("QUEUED")
fw.close()
'''

    try:
        env = _jetbrains_env()

        if project_path and not project_already_open:
            try:
                subprocess.run(
                    [ide_cmd, project_path],
                    capture_output=True,
                    env=env,
                    timeout=5
                )
            except subprocess.TimeoutExpired:
                # CLI hung handing off to the IDE — the IDE may still
                # be coming up.  Don't bail; the poll loop below will
                # wait for ideScript to become responsive.  (Without
                # this catch the outer except would swallow the
                # TimeoutExpired and we'd never enter the poll.)
                pass

        with tempfile.NamedTemporaryFile(mode='w', suffix='.groovy', delete=False) as tmp:
            tmp.write(groovy_script)
            tmp_script_path = tmp.name

        # Poll the IDE's scripting engine until QUEUED appears in
        # the result file or our (deliberately generous) budget is
        # exhausted.  PyCharm cold-start with indexing/plugin load
        # can take several minutes on a slow machine or first run.
        #
        # Per-iteration: clear the result file, run ``ideScript``,
        # read the file.  ``QUEUED`` = success; anything else (or
        # missing) = retry.  We retry on subprocess non-zero too
        # but *not* on TimeoutExpired (could leave a duplicate
        # ``leap`` tab queued in the IDE — see fix #A).
        # Per-call timeout is 30 s.
        #
        # Three early-bail conditions besides the 10-min deadline:
        #   * ``should_cancel`` — caller asks us to stop (X button).
        #   * IDE seen running, then gone — closed/crashed.
        #   * IDE never appeared within ``appearance_deadline``
        #     (90 s) — probably picked a broken bundle.
        deadline = time.monotonic() + 600.0  # 10 minutes
        appearance_deadline = time.monotonic() + 90.0
        ide_ever_seen = False
        while True:
            if should_cancel is not None and should_cancel():
                return False

            running = _is_jetbrains_running(ide)
            if running:
                ide_ever_seen = True
            elif ide_ever_seen:
                return False  # IDE was up, now gone — give up
            elif time.monotonic() > appearance_deadline:
                return False  # never showed up — bad bundle?

            # Clear result file from previous iteration so we
            # don't read a stale value.
            try:
                os.unlink(result_path)
            except OSError:
                pass

            try:
                subprocess.run(
                    [ide_cmd, 'ideScript', tmp_script_path],
                    capture_output=True,
                    timeout=30,
                    env=env
                )
            except subprocess.TimeoutExpired:
                # The original "bail on TimeoutExpired" rule existed to
                # avoid leaving a duplicate ``leap`` tab queued in the
                # IDE — see the Groovy: ``QUEUED`` is written AFTER
                # ``invokeLater`` enqueues terminal creation, so if a
                # timeout fired while the EDT was processing a slow
                # ``invokeLater`` the next retry would queue a SECOND
                # tab.  But the same rule blanket-fails cold-start: a
                # freshly-launched IDE whose IPC isn't ready yet
                # blocks the CLI for >30 s, no Groovy ran, no tab
                # queued — yet the bail dumps the user into the
                # fallback terminal.  Distinguish via ``result_path``:
                # if the Groovy wrote anything there (WAITING or
                # QUEUED) the script DID run, possible terminal was
                # queued, bail to be safe.  If it's empty/missing,
                # nothing ran (no tab risk), retry on the next
                # iteration.  Without this, GUI Resume against a
                # closed IDE silently lands in iTerm2.
                try:
                    acted = (os.path.exists(result_path)
                             and os.path.getsize(result_path) > 0)
                except OSError:
                    acted = False
                if acted:
                    return False
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.5)
                continue

            # Check result file.
            try:
                with open(result_path, 'r') as f:
                    content = f.read().strip()
                if content == 'QUEUED':
                    return True
                # 'WAITING' or anything else — retry
            except (OSError, IOError):
                pass  # File missing — IDE didn't get our request; retry

            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)
    except (subprocess.SubprocessError, OSError):
        pass
    finally:
        # Clean up both temp files on every exit path — even when we
        # bailed out before ``tmp_script_path`` was assigned.
        for path in (tmp_script_path, result_path):
            if not path:
                continue
            try:
                os.unlink(path)
            except OSError:
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
    """Open a new iTerm2 tab and run a command.

    Creates a new window if none exists, otherwise opens a new tab
    in the current window.
    """
    escaped = command.replace('\\', '\\\\').replace('"', '\\"')
    script = f'''
    tell application "iTerm"
        if (count of windows) = 0 then
            create window with default profile
        else
            tell current window
                create tab with default profile
            end tell
        end if
        tell current session of current window
            write text "{escaped}"
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
        if result.returncode != 0:
            logger.debug("iTerm2 AppleScript failed: %s", result.stderr.strip())
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        pass

    return False


def _open_vscode_terminal(
    project_path: Optional[str],
    command: str,
    ide: str = 'VS Code',
    should_cancel: Optional[Callable[[], bool]] = None,
    ide_app_path: Optional[str] = None,
) -> bool:
    """Open a new VS Code/Cursor terminal tab and run a command.

    Talks to the Leap VS Code extension via ``~/.leap-terminal-request``.
    The extension reads the file, creates a terminal with the command,
    and ``unlink``s the file (see ``vscode-extension/extension.js``
    ``processRequestFile``).  We use the unlink as our success signal
    — if the file persists, the extension didn't process it (cold
    start, not installed, wrong window focused, etc.) and we should
    keep waiting or fall back.

    Symmetric with ``_open_jetbrains_terminal``'s poll: same 10-min
    budget, same early-bail conditions (cancellation, IDE never
    appeared in 90 s, IDE was up and disappeared).  Per-iteration
    sleep is 0.5 s.

    ``ide_app_path``: when set (Move-to-IDE flow), pins ``code_path``
    to the picked bundle's CLI at ``Contents/Resources/app/bin/<cli>``
    instead of resolving via PATH.  Avoids the same multi-install
    routing bug ``ide_app_path`` fixes for JetBrains: if the user
    has VS Code Stable + Insiders and PATH resolves to the wrong
    one, ``--reuse-window`` opens the wrong installation.
    """
    try:
        env, code_path = _vscode_env_and_path(ide)
        # Prefer the CLI inside the picked .app — same rationale as
        # the JetBrains ``ide_app_path`` pinning.
        if ide_app_path:
            cli_name = 'cursor' if ide == 'Cursor' else 'code'
            candidate = os.path.join(
                ide_app_path, 'Contents', 'Resources', 'app', 'bin', cli_name,
            )
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                code_path = candidate
        if not code_path:
            return False

        if project_path:
            try:
                subprocess.run(
                    [code_path, '--reuse-window', project_path],
                    capture_output=True,
                    timeout=5,
                    env=env
                )
            except subprocess.TimeoutExpired:
                # CLI hung handing off to VS Code — the IDE may still
                # be coming up.  Don't bail; the poll loop below will
                # wait for the extension to consume our request.
                pass

        request_file = os.path.expanduser('~/.leap-terminal-request')
        # Atomic write (temp + os.replace) so the extension never reads a
        # half-written ``open:`` request (a partial read misses the prefix,
        # hits the catch-all, and gets unlinked — silently dropping it).
        # The replace also overwrites any stale request from a prior attempt
        # in one step, so we don't mistake it for the extension consuming
        # ours when we poll for the file to disappear below.
        _write_terminal_request(f'open:{command}')

        # Poll for the extension to process and unlink the request.
        deadline = time.monotonic() + 600.0  # 10 minutes
        appearance_deadline = time.monotonic() + 90.0
        ide_ever_seen = False
        while True:
            if should_cancel is not None and should_cancel():
                # Try to clean up our orphaned request so a later
                # extension load doesn't surprise the user with a
                # stray terminal.
                try:
                    os.unlink(request_file)
                except OSError:
                    pass
                return False

            running = _is_vscode_running(ide)
            if running:
                ide_ever_seen = True
            elif ide_ever_seen:
                # VS Code/Cursor was up and is now gone — give up.
                try:
                    os.unlink(request_file)
                except OSError:
                    pass
                return False
            elif time.monotonic() > appearance_deadline:
                try:
                    os.unlink(request_file)
                except OSError:
                    pass
                return False

            # Extension consumes the request by unlinking — that's
            # our success signal.
            if not os.path.exists(request_file):
                return True

            if time.monotonic() >= deadline:
                try:
                    os.unlink(request_file)
                except OSError:
                    pass
                return False
            time.sleep(0.5)
    except (subprocess.SubprocessError, OSError):
        pass

    return False
