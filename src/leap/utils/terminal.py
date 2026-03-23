"""
Terminal utilities for Leap.

Handles terminal title setting, escape sequences, and terminal-related operations.
"""

import sys
from pathlib import Path

from leap.utils.constants import TERM_TITLE_PREFIX, TERM_TITLE_SUFFIX

# VS Code extension watches this file for rename requests
_VSCODE_REQUEST_FILE = Path.home() / '.leap-terminal-request'


def set_terminal_title(title: str, *, vscode_rename: bool = True) -> None:
    """
    Set the terminal tab/window title.

    Uses OSC escape sequence (works in native terminals and VS Code when
    the proposed onDidWriteTerminalData API is available). Optionally also
    writes a rename request file for the VS Code extension's file watcher.

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
