#!/usr/bin/env python3
"""Interactive CLI provider manager for Leap.

Provides a menu to:
  1. Reorder CLI providers (changes selection menu order)
  2. Edit default flags per CLI (stored in .storage/cli_flags.json)
  3. Show/hide CLIs from the selection menu
  4. Rename CLIs (custom display names shown everywhere)
"""

import os
import re
import select
import sys
import termios
import tty
from pathlib import Path

# Ensure src/ is on the path so leap package can be imported
_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC_DIR = _SCRIPT_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from leap.cli_providers.registry import (
    _BUILTIN_PROVIDERS,
    _load_cli_order,
    get_display_name,
    get_provider,
    list_providers,
    load_cli_aliases,
    load_cli_env,
    load_cli_flags,
    load_cli_hidden,
    load_custom_clis,
    reload_custom_clis,
    save_cli_aliases,
    save_cli_env,
    save_cli_flags,
    save_cli_hidden,
    save_cli_order,
    save_custom_clis,
)

# --- Colors ---
ORANGE = "\033[38;5;208m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"


# --- Terminal helpers ---

def get_key() -> str:
    """Read a single keypress, handling arrow key escape sequences.

    Uses ``os.read`` + ``select`` so a bare Esc can be distinguished
    from the start of an arrow-key CSI/SS3 sequence, and so Python's
    text-mode stdin buffer can't swallow the follow-up bytes.

    Supports q and / (Hebrew keyboard maps q key to /) for quit.
    """
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        b = os.read(fd, 1)
        if not b:
            return "quit"  # EOF
        ch = b.decode("utf-8", errors="replace")
        if ch == "\x1b":
            # CSI bytes arrive back-to-back after the ESC; bare Esc
            # leaves stdin idle.  Poll briefly for the follow-up.
            if not select.select([fd], [], [], 0.1)[0]:
                return "quit"
            rest = os.read(fd, 16).decode("utf-8", errors="replace")
            if rest.startswith("[A") or rest.startswith("OA"):
                return "up"
            if rest.startswith("[B") or rest.startswith("OB"):
                return "down"
            return ""  # unhandled sequence, already fully drained
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":  # Ctrl+C
            return "quit"
        if ch == "\x04":  # Ctrl+D
            return "quit"
        # q and / (Hebrew keyboard maps q key to /)
        if ch in ("q", "/"):
            return "quit"
        if ch in ("\x7f", "\x08"):  # Backspace
            return "quit"
        if ch == " ":
            return "space"
        if ch in ("e", "ק"):  # e and Hebrew e-key
            return "edit_env"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ""


def clear_lines(n: int) -> None:
    """Move cursor up n lines and clear each."""
    for _ in range(n):
        sys.stderr.write("\033[A\033[K")
    sys.stderr.flush()


def read_line_raw(prompt: str, initial: str = "") -> str:
    """Read a line of input with raw terminal mode, supporting editing.

    Returns the entered string, or raises KeyboardInterrupt on Ctrl+C/Ctrl+D.
    """
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    buf = list(initial)
    sys.stderr.write(f"\033[K{prompt}{''.join(buf)}")
    sys.stderr.flush()
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                sys.stderr.write("\n")
                sys.stderr.flush()
                return "".join(buf)
            elif ch == "\x03" or ch == "\x04":  # Ctrl+C / Ctrl+D
                sys.stderr.write("\n")
                sys.stderr.flush()
                raise KeyboardInterrupt
            elif ch == "\x7f" or ch == "\x08":  # Backspace
                if buf:
                    buf.pop()
                    sys.stderr.write("\b \b")
                    sys.stderr.flush()
            elif ch == "\x15":  # Ctrl+U — clear line
                count = len(buf)
                buf.clear()
                sys.stderr.write("\b" * count + " " * count + "\b" * count)
                sys.stderr.flush()
            elif ch == "\x17":  # Ctrl+W — delete word
                deleted = 0
                while buf and buf[-1] == " ":
                    buf.pop()
                    deleted += 1
                while buf and buf[-1] != " ":
                    buf.pop()
                    deleted += 1
                sys.stderr.write("\b" * deleted + " " * deleted + "\b" * deleted)
                sys.stderr.flush()
            elif ch == "\x1b":  # Escape sequences — ignore
                sys.stdin.read(1)
                sys.stdin.read(1)
                continue
            elif ch >= " ":  # Printable
                buf.append(ch)
                sys.stderr.write(ch)
                sys.stderr.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# --- Top-level menu ---

MAIN_MENU_ITEMS = [
    ("overview", "CLI overview & flags"),
    ("reorder", "Reorder CLIs"),
    ("visibility", "Show/hide CLIs"),
    ("rename", "Rename CLIs"),
    ("create", "Create custom CLIs"),
    ("delete", "Delete custom CLIs"),
]


def render_main_menu(cursor: int) -> int:
    """Render the top-level menu. Returns line count."""
    lines = 0
    sys.stderr.write(f"\r\033[K  {BOLD}Manage CLIs:{RESET}\n")
    lines += 1
    sys.stderr.write(f"\033[K  {DIM}↑/↓ navigate · Enter to select · Esc/q to quit{RESET}\n")
    lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    for i, (_, label) in enumerate(MAIN_MENU_ITEMS):
        if i == cursor:
            sys.stderr.write(f"\033[K    {ORANGE}❯ {label}{RESET}\n")
        else:
            sys.stderr.write(f"\033[K    {DIM}  {label}{RESET}\n")
        lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    sys.stderr.flush()
    return lines


def main_menu() -> str:
    """Show top-level menu, return selected action key or empty string."""
    cursor = 0
    prev_lines = render_main_menu(cursor)

    while True:
        key = get_key()
        if key == "up":
            cursor = (cursor - 1) % len(MAIN_MENU_ITEMS)
        elif key == "down":
            cursor = (cursor + 1) % len(MAIN_MENU_ITEMS)
        elif key == "enter":
            clear_lines(prev_lines)
            return MAIN_MENU_ITEMS[cursor][0]
        elif key == "quit":
            clear_lines(prev_lines)
            return ""
        else:
            continue
        clear_lines(prev_lines)
        prev_lines = render_main_menu(cursor)


# --- Reorder submenu ---

def render_reorder(providers: list[tuple[str, str]], cursor: int, grabbed: bool) -> int:
    """Render the reorder menu. Returns line count."""
    lines = 0
    sys.stderr.write(f"\r\033[K  {BOLD}Reorder CLIs:{RESET}\n")
    lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    for i, (name, label) in enumerate(providers):
        num = f"{i + 1}."
        if i == cursor:
            if grabbed:
                sys.stderr.write(f"\033[K    {GREEN}⇅ {num} {label}{RESET}\n")
            else:
                sys.stderr.write(f"\033[K    {ORANGE}❯ {num} {label}{RESET}\n")
        else:
            sys.stderr.write(f"\033[K    {DIM}  {num} {label}{RESET}\n")
        lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    if grabbed:
        sys.stderr.write(f"\033[K  {GREEN}↕ Moving · ↑/↓ to move · Enter to drop · Esc/q to go back (auto-saves){RESET}\n")
    else:
        sys.stderr.write(f"\033[K  {DIM}↑/↓ navigate · Enter to grab · Esc/q to go back (auto-saves){RESET}\n")
    lines += 1
    sys.stderr.flush()
    return lines


def reorder_menu() -> None:
    """Interactive reorder submenu."""
    all_providers = list_providers()
    if not all_providers:
        sys.stderr.write("\n  No CLI providers found.\n")
        return

    providers = [(name, get_display_name(name)) for name in all_providers]
    cursor = 0
    grabbed = False
    prev_lines = render_reorder(providers, cursor, grabbed)

    while True:
        key = get_key()
        if key == "up":
            if grabbed and cursor > 0:
                providers[cursor], providers[cursor - 1] = providers[cursor - 1], providers[cursor]
                cursor -= 1
            elif not grabbed:
                cursor = (cursor - 1) % len(providers)
        elif key == "down":
            if grabbed and cursor < len(providers) - 1:
                providers[cursor], providers[cursor + 1] = providers[cursor + 1], providers[cursor]
                cursor += 1
            elif not grabbed:
                cursor = (cursor + 1) % len(providers)
        elif key in ("enter", "space"):
            grabbed = not grabbed
        elif key == "quit":
            clear_lines(prev_lines)
            order = [name for name, _ in providers]
            save_cli_order(order)
            sys.stderr.write(f"  {GREEN}✓ CLI order saved{RESET}\n\n")
            sys.stderr.flush()
            return
        else:
            continue
        clear_lines(prev_lines)
        prev_lines = render_reorder(providers, cursor, grabbed)


# --- Overview submenu ---

def _get_cli_info(name: str) -> dict:
    """Get info dict for a CLI: display_name, engine, flags, env."""
    display = get_display_name(name)
    flags = load_cli_flags().get(name, "")
    # Check if custom
    custom_entries = {e['id']: e for e in load_custom_clis()}
    entry = custom_entries.get(name)
    if entry:
        base_name = entry.get('base', '')
        engine = get_display_name(base_name)
        # Custom CLIs have built-in env from their definition
        env = dict(entry.get('env', {}))
    else:
        engine = ""  # built-in, engine is itself
        env = {}
    # Merge user-configured env vars (overrides custom CLI's built-in env)
    env.update(load_cli_env().get(name, {}))
    return {
        'display': display,
        'engine': engine,
        'flags': flags,
        'env': env,
        'is_custom': bool(entry),
    }


def render_overview(
    items: list[tuple[str, dict]], cursor: int
) -> int:
    """Render the overview menu. Returns line count."""
    lines = 0
    sys.stderr.write(f"\r\033[K  {BOLD}CLI overview:{RESET}\n")
    lines += 1
    sys.stderr.write(f"\033[K  {DIM}↑/↓ navigate · Enter to edit flags · e to edit env · Esc/q to go back{RESET}\n")
    lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    for i, (name, info) in enumerate(items):
        is_sel = i == cursor
        style = ORANGE if is_sel else DIM
        pointer = "❯" if is_sel else " "

        # Line 1: name + engine
        if info['engine']:
            label = f"{info['display']}  {DIM}({info['engine']}){RESET}"
        else:
            label = info['display']
        sys.stderr.write(f"\033[K    {style}{pointer}{RESET} {style}{label}{RESET}\n")
        lines += 1

        # Line 2: flags
        flag_str = info['flags'] if info['flags'] else f"{DIM}(no flags){RESET}"
        sys.stderr.write(f"\033[K        flags: {flag_str}\n")
        lines += 1

        # Line 3: env vars (if any configured)
        if info['env']:
            env_parts = [f"{k}={v}" for k, v in info['env'].items()]
            sys.stderr.write(f"\033[K        {DIM}env: {', '.join(env_parts)}{RESET}\n")
            lines += 1

        # Blank separator between items
        if i < len(items) - 1:
            sys.stderr.write("\033[K\n")
            lines += 1

    sys.stderr.write("\033[K\n")
    lines += 1
    sys.stderr.flush()
    return lines


def overview_menu() -> None:
    """Interactive overview + flags editor."""
    all_providers = list_providers()
    if not all_providers:
        sys.stderr.write("\n  No CLI providers found.\n")
        return

    all_flags = load_cli_flags()

    def build_list() -> list[tuple[str, dict]]:
        return [(name, _get_cli_info(name)) for name in all_providers]

    items = build_list()
    cursor = 0
    prev_lines = render_overview(items, cursor)

    while True:
        key = get_key()
        if key == "up":
            cursor = (cursor - 1) % len(items)
        elif key == "down":
            cursor = (cursor + 1) % len(items)
        elif key == "enter":
            clear_lines(prev_lines)
            name, info = items[cursor]
            current = info['flags']
            sys.stderr.write(f"  {BOLD}{info['display']}{RESET} flags:\n")
            sys.stderr.write(f"  {DIM}(include -- prefix, e.g. --dangerously-skip-permissions){RESET}\n")
            try:
                new_flags = read_line_raw("  flags: ", current)
            except KeyboardInterrupt:
                sys.stderr.write(f"  {DIM}Cancelled{RESET}\n\n")
                items = build_list()
                prev_lines = render_overview(items, cursor)
                continue

            if new_flags != current:
                if new_flags:
                    all_flags[name] = new_flags
                else:
                    all_flags.pop(name, None)
                save_cli_flags(all_flags)
                sys.stderr.write(f"  {GREEN}✓ Saved{RESET}\n\n")
            else:
                sys.stderr.write(f"  {DIM}No change{RESET}\n\n")

            items = build_list()
            prev_lines = render_overview(items, cursor)
            continue
        elif key == "edit_env":
            clear_lines(prev_lines)
            name, info = items[cursor]
            all_env = load_cli_env()
            current_env = all_env.get(name, {})
            # Show merged env (custom CLI built-in + user-configured)
            merged_env = info['env']
            sys.stderr.write(f"  {BOLD}{info['display']}{RESET} environment variables:\n")
            sys.stderr.write(f"  {DIM}(KEY=VALUE per line, empty line to finish, Ctrl+C to cancel){RESET}\n")
            if merged_env:
                sys.stderr.write(f"  {DIM}Current: {', '.join(f'{k}={v}' for k, v in merged_env.items())}{RESET}\n")
            new_env: dict[str, str] = {}
            cancelled = False
            while True:
                try:
                    line = read_line_raw("  env: ", "")
                except KeyboardInterrupt:
                    sys.stderr.write(f"  {DIM}Cancelled{RESET}\n\n")
                    cancelled = True
                    break
                line = line.strip()
                if not line:
                    break
                if '=' in line:
                    k, v = line.split('=', 1)
                    new_env[k.strip()] = v.strip()
                else:
                    sys.stderr.write(f"  {YELLOW}Expected KEY=VALUE format{RESET}\n")
            if not cancelled:
                if new_env != current_env:
                    if new_env:
                        all_env[name] = new_env
                    else:
                        all_env.pop(name, None)
                    save_cli_env(all_env)
                    sys.stderr.write(f"  {GREEN}✓ Saved{RESET}\n\n")
                else:
                    sys.stderr.write(f"  {DIM}No change{RESET}\n\n")
            items = build_list()
            prev_lines = render_overview(items, cursor)
            continue
        elif key == "quit":
            clear_lines(prev_lines)
            return
        else:
            continue
        clear_lines(prev_lines)
        prev_lines = render_overview(items, cursor)


# --- Visibility submenu ---

def render_visibility_menu(
    providers: list[tuple[str, str, bool]], cursor: int
) -> int:
    """Render the show/hide menu. Returns line count.

    providers: list of (name, display_name, is_visible).
    """
    lines = 0
    sys.stderr.write(f"\r\033[K  {BOLD}Show/hide CLIs:{RESET}\n")
    lines += 1
    sys.stderr.write(f"\033[K  {DIM}↑/↓ navigate · Enter to toggle · Esc/q to go back{RESET}\n")
    lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    for i, (name, label, visible) in enumerate(providers):
        check = f"{GREEN}✓{RESET}" if visible else f"{DIM}✗{RESET}"
        if i == cursor:
            style = ORANGE
            sys.stderr.write(f"\033[K    {style}❯{RESET} {check}  {style}{label}{RESET}\n")
        else:
            sys.stderr.write(f"\033[K      {check}  {DIM}{label}{RESET}\n")
        lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    sys.stderr.flush()
    return lines


def visibility_menu() -> None:
    """Interactive show/hide submenu."""
    all_providers = list_providers()
    if not all_providers:
        sys.stderr.write("\n  No CLI providers found.\n")
        return

    hidden = set(load_cli_hidden())

    def build_list() -> list[tuple[str, str, bool]]:
        return [
            (name, get_display_name(name), name not in hidden)
            for name in all_providers
        ]

    providers = build_list()
    cursor = 0
    prev_lines = render_visibility_menu(providers, cursor)

    while True:
        key = get_key()
        if key == "up":
            cursor = (cursor - 1) % len(providers)
        elif key == "down":
            cursor = (cursor + 1) % len(providers)
        elif key in ("enter", "space"):
            name = providers[cursor][0]
            if name in hidden:
                hidden.discard(name)
            else:
                # Don't allow hiding all CLIs
                visible_count = sum(1 for _, _, v in providers if v)
                if visible_count <= 1:
                    # Re-render with warning — skip toggle
                    clear_lines(prev_lines)
                    prev_lines = render_visibility_menu(providers, cursor)
                    sys.stderr.write(f"\033[K  {YELLOW}⚠ At least one CLI must remain visible{RESET}\n")
                    prev_lines += 1
                    sys.stderr.flush()
                    continue
                hidden.add(name)
            save_cli_hidden(sorted(hidden))
            providers = build_list()
        elif key == "quit":
            clear_lines(prev_lines)
            return
        else:
            continue
        clear_lines(prev_lines)
        prev_lines = render_visibility_menu(providers, cursor)


# --- Rename submenu ---

def render_rename_menu(
    providers: list[tuple[str, str, str]], cursor: int
) -> int:
    """Render the rename menu. Returns line count.

    providers: list of (name, current_display, original_display).
    """
    lines = 0
    sys.stderr.write(f"\r\033[K  {BOLD}Rename CLIs:{RESET}\n")
    lines += 1
    sys.stderr.write(f"\033[K  {DIM}↑/↓ navigate · Enter to rename · Esc/q to go back{RESET}\n")
    lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    for i, (name, current, original) in enumerate(providers):
        if current != original:
            label = f"{current}  {DIM}({original}){RESET}"
        else:
            label = current
        if i == cursor:
            sys.stderr.write(f"\033[K    {ORANGE}❯ {label}{RESET}\n")
        else:
            sys.stderr.write(f"\033[K    {DIM}  {label}{RESET}\n")
        lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    sys.stderr.flush()
    return lines


def rename_menu() -> None:
    """Interactive rename submenu."""
    all_providers = list_providers()
    if not all_providers:
        sys.stderr.write("\n  No CLI providers found.\n")
        return

    aliases = load_cli_aliases()

    def build_list() -> list[tuple[str, str, str]]:
        return [
            (name, get_display_name(name), get_provider(name).display_name)
            for name in all_providers
        ]

    providers = build_list()
    cursor = 0
    prev_lines = render_rename_menu(providers, cursor)

    while True:
        key = get_key()
        if key == "up":
            cursor = (cursor - 1) % len(providers)
        elif key == "down":
            cursor = (cursor + 1) % len(providers)
        elif key == "enter":
            clear_lines(prev_lines)
            name, current, original = providers[cursor]
            sys.stderr.write(f"  {BOLD}{original}{RESET} — new name:\n")
            sys.stderr.write(f"  {DIM}(Ctrl+U to clear for default, Enter to confirm, Ctrl+C to cancel){RESET}\n")
            try:
                new_name = read_line_raw("  name: ", current)
            except KeyboardInterrupt:
                sys.stderr.write(f"  {DIM}Cancelled{RESET}\n\n")
                providers = build_list()
                prev_lines = render_rename_menu(providers, cursor)
                continue

            new_name = new_name.strip()
            if new_name and new_name != original:
                aliases[name] = new_name
            else:
                # Empty or same as original — remove alias
                aliases.pop(name, None)
            save_cli_aliases(aliases)

            if new_name != current:
                sys.stderr.write(f"  {GREEN}✓ Saved{RESET}\n\n")
            else:
                sys.stderr.write(f"  {DIM}No change{RESET}\n\n")

            providers = build_list()
            prev_lines = render_rename_menu(providers, cursor)
            continue
        elif key == "quit":
            clear_lines(prev_lines)
            return
        else:
            continue
        clear_lines(prev_lines)
        prev_lines = render_rename_menu(providers, cursor)


# --- Create custom CLI ---

def render_base_picker(bases: list[tuple[str, str]], cursor: int) -> int:
    """Render base CLI picker. Returns line count."""
    lines = 0
    sys.stderr.write(f"\r\033[K  {BOLD}Select base CLI:{RESET}\n")
    lines += 1
    sys.stderr.write(f"\033[K  {DIM}↑/↓ navigate · Enter to select · Esc/q to go back{RESET}\n")
    lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    for i, (name, label) in enumerate(bases):
        if i == cursor:
            sys.stderr.write(f"\033[K    {ORANGE}❯ {label}{RESET}\n")
        else:
            sys.stderr.write(f"\033[K    {DIM}  {label}{RESET}\n")
        lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    sys.stderr.flush()
    return lines


def create_custom_cli() -> None:
    """Interactive flow to create a custom CLI."""
    # Step 1: pick base CLI
    bases = [
        (name, get_display_name(name))
        for name in sorted(_BUILTIN_PROVIDERS.keys())
        if get_provider(name).is_installed()
    ]
    if not bases:
        sys.stderr.write(f"\n  {YELLOW}No installed CLIs to base on.{RESET}\n\n")
        return

    cursor = 0
    prev_lines = render_base_picker(bases, cursor)
    base_name = ""

    while True:
        key = get_key()
        if key == "up":
            cursor = (cursor - 1) % len(bases)
        elif key == "down":
            cursor = (cursor + 1) % len(bases)
        elif key == "enter":
            clear_lines(prev_lines)
            base_name = bases[cursor][0]
            break
        elif key == "quit":
            clear_lines(prev_lines)
            return
        else:
            continue
        clear_lines(prev_lines)
        prev_lines = render_base_picker(bases, cursor)

    base_display = get_display_name(base_name)
    sys.stderr.write(f"  Base: {BOLD}{base_display}{RESET}\n\n")

    # Step 2: display name
    sys.stderr.write(f"  {BOLD}Display name{RESET} (shown in menus, monitor, Slack):\n")
    try:
        display_name = read_line_raw("  name: ", "")
    except KeyboardInterrupt:
        sys.stderr.write(f"  {DIM}Cancelled{RESET}\n\n")
        return
    display_name = display_name.strip()
    if not display_name:
        sys.stderr.write(f"  {YELLOW}Name cannot be empty{RESET}\n\n")
        return

    # Step 3: generate ID from display name
    custom_id = re.sub(r'[^a-z0-9]+', '-', display_name.lower()).strip('-')
    if not custom_id:
        custom_id = f"custom-{base_name}"

    # Check for ID conflicts
    existing = load_custom_clis()
    existing_ids = {e['id'] for e in existing}
    all_ids = set(_BUILTIN_PROVIDERS.keys()) | existing_ids
    if custom_id in all_ids:
        # Append a number
        i = 2
        while f"{custom_id}-{i}" in all_ids:
            i += 1
        custom_id = f"{custom_id}-{i}"

    # Step 4: environment variables (key=value, one per line)
    sys.stderr.write(f"  {BOLD}Environment variables{RESET} {DIM}(one per line, KEY=VALUE, empty line to finish):{RESET}\n")
    env_vars: dict[str, str] = {}
    while True:
        try:
            line = read_line_raw(f"  {DIM}env:{RESET} ", "")
        except KeyboardInterrupt:
            sys.stderr.write(f"  {DIM}Cancelled{RESET}\n\n")
            return
        line = line.strip()
        if not line:
            break
        if '=' in line:
            k, v = line.split('=', 1)
            env_vars[k.strip()] = v.strip()
        else:
            sys.stderr.write(f"  {YELLOW}  Expected KEY=VALUE format{RESET}\n")

    # Step 5: default flags
    sys.stderr.write(f"\n  {BOLD}Default flags{RESET} {DIM}(include -- prefix, empty for none):{RESET}\n")
    try:
        flags = read_line_raw("  flags: ", "")
    except KeyboardInterrupt:
        sys.stderr.write(f"  {DIM}Cancelled{RESET}\n\n")
        return

    # Save
    entry = {
        'id': custom_id,
        'base': base_name,
        'display_name': display_name,
        'env': env_vars,
    }
    existing.append(entry)
    save_custom_clis(existing)

    # Save flags if provided
    flags = flags.strip()
    if flags:
        all_flags = load_cli_flags()
        all_flags[custom_id] = flags
        save_cli_flags(all_flags)

    reload_custom_clis()

    sys.stderr.write(f"\n  {GREEN}✓ Created '{display_name}'{RESET}\n\n")


# --- Delete custom CLI ---

def render_delete_menu(
    customs: list[tuple[str, str, str]], cursor: int
) -> int:
    """Render delete menu. Returns line count.

    customs: list of (id, display_name, base_display).
    """
    lines = 0
    sys.stderr.write(f"\r\033[K  {BOLD}Delete custom CLI:{RESET}\n")
    lines += 1
    sys.stderr.write(f"\033[K  {DIM}↑/↓ navigate · Enter to delete · Esc/q to go back{RESET}\n")
    lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    if not customs:
        sys.stderr.write(f"\033[K    {DIM}(no custom CLIs){RESET}\n")
        lines += 1
    else:
        for i, (cid, display, base_display) in enumerate(customs):
            if i == cursor:
                sys.stderr.write(
                    f"\033[K    {ORANGE}❯ {display}{RESET}  {DIM}({base_display}){RESET}\n"
                )
            else:
                sys.stderr.write(
                    f"\033[K    {DIM}  {display}  ({base_display}){RESET}\n"
                )
            lines += 1
    sys.stderr.write("\033[K\n")
    lines += 1
    sys.stderr.flush()
    return lines


def delete_custom_cli() -> None:
    """Interactive flow to delete a custom CLI."""
    existing = load_custom_clis()
    if not existing:
        sys.stderr.write(f"\n  {DIM}No custom CLIs to delete.{RESET}\n\n")
        return

    def build_list() -> list[tuple[str, str, str]]:
        return [
            (e['id'], e.get('display_name', e['id']), get_display_name(e.get('base', '?')))
            for e in existing
        ]

    customs = build_list()
    cursor = 0
    prev_lines = render_delete_menu(customs, cursor)

    while True:
        key = get_key()
        if not customs:
            if key in ("enter", "quit"):
                clear_lines(prev_lines)
                return
            continue
        if key == "up":
            cursor = (cursor - 1) % len(customs)
        elif key == "down":
            cursor = (cursor + 1) % len(customs)
        elif key == "enter":
            cid, display, _ = customs[cursor]
            # Remove from custom list
            existing[:] = [e for e in existing if e['id'] != cid]
            save_custom_clis(existing)
            # Clean up flags, aliases, hidden, order, env
            all_flags = load_cli_flags()
            all_flags.pop(cid, None)
            save_cli_flags(all_flags)
            aliases = load_cli_aliases()
            aliases.pop(cid, None)
            save_cli_aliases(aliases)
            hidden = load_cli_hidden()
            if cid in hidden:
                hidden.remove(cid)
                save_cli_hidden(hidden)
            all_env = load_cli_env()
            if cid in all_env:
                del all_env[cid]
                save_cli_env(all_env)
            order = _load_cli_order()
            if cid in order:
                order.remove(cid)
                save_cli_order(order)
            reload_custom_clis()
            clear_lines(prev_lines)
            sys.stderr.write(f"  {GREEN}✓ Deleted '{display}'{RESET}\n\n")
            sys.stderr.flush()
            if not existing:
                return
            customs = build_list()
            cursor = min(cursor, len(customs) - 1)
            prev_lines = render_delete_menu(customs, cursor)
            continue
        elif key == "quit":
            clear_lines(prev_lines)
            return
        else:
            continue
        clear_lines(prev_lines)
        prev_lines = render_delete_menu(customs, cursor)


# --- Entry point ---

def main() -> None:
    while True:
        action = main_menu()
        if not action:
            return
        if action == "overview":
            overview_menu()
        elif action == "reorder":
            reorder_menu()
        elif action == "visibility":
            visibility_menu()
        elif action == "rename":
            rename_menu()
        elif action == "create":
            create_custom_cli()
        elif action == "delete":
            delete_custom_cli()


if __name__ == "__main__":
    main()
