"""
Terminal utilities for ClaudeQ.

Handles terminal title setting, escape sequences, and terminal-related operations.
"""

import sys

from claudeq.utils.constants import TERM_TITLE_PREFIX, TERM_TITLE_SUFFIX


def set_terminal_title(title: str) -> None:
    """
    Set the terminal tab/window title.

    Args:
        title: The title to set for the terminal.
    """
    sys.stdout.write(f"{TERM_TITLE_PREFIX}{title}{TERM_TITLE_SUFFIX}")
    sys.stdout.flush()


def print_banner(session_type: str, tag: str) -> None:
    """
    Print the ClaudeQ ASCII banner.

    Args:
        session_type: Either 'server' or 'client'.
        tag: The session tag name.
    """
    banner = r"""
   _____ _                 _       ___
  / ____| |               | |     / _ \
 | |    | | __ _ _   _  __| | ___| | | |
 | |    | |/ _` | | | |/ _` |/ _ \ | | |
 | |____| | (_| | |_| | (_| |  __/ |_| |
  \_____|_|\__,_|\__,_|\__,_|\___|\___|
"""
    print(banner)
    print("=" * 80)
    print(f"  PTY {session_type.upper()} - Session: {tag}")
    print("=" * 80)
