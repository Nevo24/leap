"""
Terminal utilities for ClaudeQ.

Handles terminal title setting, escape sequences, and terminal-related operations.
"""

import sys
from typing import Optional

from claudeq.utils.constants import TERM_TITLE_PREFIX, TERM_TITLE_SUFFIX


def set_terminal_title(title: str) -> None:
    """
    Set the terminal tab/window title.

    Args:
        title: The title to set for the terminal.
    """
    sys.stdout.write(f"{TERM_TITLE_PREFIX}{title}{TERM_TITLE_SUFFIX}")
    sys.stdout.flush()


def print_colored(message: str, color: str = "reset") -> None:
    """
    Print a colored message to stdout.

    Args:
        message: The message to print.
        color: Color name ('yellow', 'green', 'red', 'reset').
    """
    from claudeq.utils.constants import COLORS
    color_code = COLORS.get(color, COLORS["reset"])
    reset_code = COLORS["reset"]
    print(f"{color_code}{message}{reset_code}")


def get_terminal_size() -> tuple[int, int]:
    """
    Get the current terminal size.

    Returns:
        Tuple of (columns, rows).
    """
    import shutil
    cols, rows = shutil.get_terminal_size(fallback=(80, 24))
    return cols, rows


def print_banner(session_type: str, tag: str) -> None:
    """
    Print the ClaudeQ ASCII banner.

    Args:
        session_type: Either 'server' or 'client'.
        tag: The session tag name.
    """
    banner = """
   _____ _                 _       ___
  / ____| |               | |     / _ \\
 | |    | | __ _ _   _  __| | ___| | | |
 | |    | |/ _` | | | |/ _` |/ _ \\ | | |
 | |____| | (_| | |_| | (_| |  __/ |_| |
  \\_____|_|\\__,_|\\__,_|\\__,_|\\___|\\___\\
"""
    print(banner)
    print("=" * 70)
    print(f"  PTY {session_type.upper()} - Session: {tag}")
    print("=" * 70)
