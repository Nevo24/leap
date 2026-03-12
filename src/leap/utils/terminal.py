"""
Terminal utilities for Leap.

Handles terminal title setting, escape sequences, and terminal-related operations.
"""

import sys

from leap.utils.constants import TERM_TITLE_PREFIX, TERM_TITLE_SUFFIX


def set_terminal_title(title: str) -> None:
    """
    Set the terminal tab/window title.

    Args:
        title: The title to set for the terminal.
    """
    sys.stdout.write(f"{TERM_TITLE_PREFIX}{title}{TERM_TITLE_SUFFIX}")
    sys.stdout.flush()


def print_banner(session_type: str, tag: str, cli_name: str = '') -> None:
    """
    Print the Leap ASCII banner.

    Args:
        session_type: Either 'server' or 'client'.
        tag: The session tag name.
        cli_name: CLI display name (e.g. 'Claude Code', 'OpenAI Codex').
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
