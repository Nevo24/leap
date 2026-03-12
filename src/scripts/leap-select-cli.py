#!/usr/bin/env python3
"""Interactive CLI provider selector for Leap.

Presents an arrow-key menu to choose between Claude Code and Codex,
then prints the selected provider name to stdout for the shell wrapper.
"""

import sys
import tty
import termios


CHOICES = [
    ("claude", "Claude Code"),
    ("codex", "OpenAI Codex"),
]

ORANGE = "\033[38;5;208m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def get_key() -> str:
    """Read a single keypress, handling arrow key escape sequences."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                if ch3 == "A":
                    return "up"
                if ch3 == "B":
                    return "down"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":  # Ctrl+C
            return "quit"
        if ch == "\x04":  # Ctrl+D
            return "quit"
        if ch == "q":
            return "quit"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ""


def render(selected: int) -> None:
    """Render the menu to stderr."""
    sys.stderr.write(f"\r\033[K  {BOLD}Select CLI provider:{RESET}\n")
    for i, (_, label) in enumerate(CHOICES):
        if i == selected:
            sys.stderr.write(f"\033[K    {ORANGE}❯ {label}{RESET}\n")
        else:
            sys.stderr.write(f"\033[K    {DIM}  {label}{RESET}\n")
    sys.stderr.flush()


def clear_menu() -> None:
    """Move cursor up and clear the menu lines."""
    # 1 header + len(CHOICES) option lines
    for _ in range(1 + len(CHOICES)):
        sys.stderr.write("\033[A\033[K")
    sys.stderr.flush()


def main() -> None:
    selected = 0
    render(selected)

    while True:
        key = get_key()
        if key == "up":
            selected = (selected - 1) % len(CHOICES)
        elif key == "down":
            selected = (selected + 1) % len(CHOICES)
        elif key == "enter":
            clear_menu()
            # Print selected provider to stdout for the shell to capture
            print(CHOICES[selected][0])
            return
        elif key == "quit":
            clear_menu()
            sys.exit(130)
        else:
            continue

        # Re-render: move up, then redraw
        for _ in range(1 + len(CHOICES)):
            sys.stderr.write("\033[A")
        render(selected)


if __name__ == "__main__":
    main()
