#!/usr/bin/env python3
"""Interactive session name input with history for Leap.

Prompts for a session name with arrow-up/down support to cycle through
previously used tags (most recent first). Prints the selected tag to
stdout for the shell wrapper.

History is stored in .storage/tag_history (one tag per line, newest last).
"""

import os
import re
import select
import sys
import tty
import termios
from pathlib import Path

TAG_PATTERN = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')
MAX_HISTORY = 50

DIM = "\033[2m"
RESET = "\033[0m"

# Resolve storage dir the same way as leap-select.sh
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
STORAGE_DIR = PROJECT_DIR / ".storage"
HISTORY_FILE = STORAGE_DIR / "tag_history"


def load_history() -> list[str]:
    """Load tag history (newest last), deduplicated."""
    if not HISTORY_FILE.exists():
        return []
    try:
        lines = HISTORY_FILE.read_text().strip().splitlines()
        # Deduplicate preserving order (latest occurrence wins)
        seen: set[str] = set()
        result: list[str] = []
        for tag in reversed(lines):
            tag = tag.strip()
            if tag and tag not in seen:
                seen.add(tag)
                result.append(tag)
        # result is newest-first; reverse to get oldest-first (newest last)
        result.reverse()
        return result[-MAX_HISTORY:]
    except OSError:
        return []


def save_history(history: list[str], new_tag: str) -> None:
    """Append tag to history, deduplicating."""
    # Remove existing occurrence so it moves to the end (most recent)
    history = [t for t in history if t != new_tag]
    history.append(new_tag)
    history = history[-MAX_HISTORY:]
    try:
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text('\n'.join(history) + '\n')
    except OSError:
        pass


def get_key() -> str:
    """Read a single keypress, handling special keys.

    Uses ``os.read`` + ``select`` so a bare Esc can be distinguished
    from the start of an arrow-key CSI/SS3 sequence, and so Python's
    text-mode stdin buffer can't swallow the follow-up bytes.
    """
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        b = os.read(fd, 1)
        if not b:
            return 'quit'  # EOF
        ch = b.decode('utf-8', errors='replace')
        if ch == '\x1b':
            # CSI bytes arrive back-to-back after the ESC; bare Esc
            # leaves stdin idle.  Poll briefly for the follow-up.
            if not select.select([fd], [], [], 0.1)[0]:
                return 'escape'
            rest = os.read(fd, 16).decode('utf-8', errors='replace')
            if rest.startswith('[A') or rest.startswith('OA'):
                return 'up'
            if rest.startswith('[B') or rest.startswith('OB'):
                return 'down'
            return ''  # unhandled sequence, already fully drained
        if ch in ('\r', '\n'):
            return 'enter'
        if ch == '\x03':  # Ctrl+C
            return 'quit'
        if ch == '\x04':  # Ctrl+D
            return 'quit'
        if ch in ('\x7f', '\x08'):  # Backspace
            return 'backspace'
        if ch == '\x15':  # Ctrl+U (clear line)
            return 'clear'
        if ch == '\x17':  # Ctrl+W (delete word)
            return 'delete_word'
        # Regular printable character
        if ch.isprintable():
            return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ''


def render_prompt(text: str, hint: str) -> None:
    """Render the input prompt with current text."""
    sys.stderr.write(f"\r\033[K  Session name: {text}")
    if not text and hint:
        # Show hint after cursor position
        sys.stderr.write(f" {DIM}{hint}{RESET}")
        # Move cursor back to after "Session name: "
        sys.stderr.write("\r\033[16C")
    sys.stderr.flush()


def main() -> None:
    history = load_history()
    # History index: -1 means "typing new input", 0 = most recent, etc.
    hist_index = -1
    # Reversed history for navigation (index 0 = most recent)
    hist_reversed = list(reversed(history))
    saved_input = ''  # What the user was typing before navigating history
    text = ''

    hint = '(↑ for recent sessions)' if history else ''
    render_prompt(text, hint)

    while True:
        key = get_key()

        if key == 'up':
            if not hist_reversed:
                continue
            if hist_index == -1:
                saved_input = text
            if hist_index < len(hist_reversed) - 1:
                hist_index += 1
                text = hist_reversed[hist_index]
            render_prompt(text, '')

        elif key == 'down':
            if hist_index > 0:
                hist_index -= 1
                text = hist_reversed[hist_index]
            elif hist_index == 0:
                hist_index = -1
                text = saved_input
            render_prompt(text, hint if not text else '')

        elif key == 'enter':
            sys.stderr.write('\n')
            sys.stderr.flush()
            tag = text.strip()
            if not tag:
                sys.stderr.write('  ❌ Error: Session name is required.\n')
                sys.exit(1)
            if not TAG_PATTERN.match(tag):
                sys.stderr.write(
                    '  ❌ Error: Session name must contain only '
                    'letters, numbers, hyphens, and underscores\n'
                )
                sys.exit(1)
            save_history(history, tag)
            print(tag)
            return

        elif key == 'quit':
            sys.stderr.write('\n')
            sys.stderr.flush()
            sys.exit(130)

        elif key == 'escape':
            sys.stderr.write('\n')
            sys.stderr.flush()
            sys.exit(130)

        elif key == 'backspace':
            if text:
                text = text[:-1]
                if not text and hist_index >= 0:
                    hist_index = -1
            render_prompt(text, hint if not text else '')

        elif key == 'clear':
            text = ''
            hist_index = -1
            render_prompt(text, hint)

        elif key == 'delete_word':
            # Delete last word
            stripped = text.rstrip()
            space_idx = stripped.rfind(' ')
            if space_idx >= 0:
                text = stripped[:space_idx + 1]
            else:
                text = ''
            if not text:
                hist_index = -1
            render_prompt(text, hint if not text else '')

        elif len(key) == 1:
            text += key
            if hist_index >= 0:
                hist_index = -1
            render_prompt(text, '')


if __name__ == '__main__':
    main()
