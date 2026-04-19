#!/usr/bin/env python3
"""Interactive picker for `leap --resume`.

Scans every ``.storage/cli_sessions/<cli>/*.json`` for Leap tags that
still have at least one recorded session whose transcript exists on
disk.  Each ``(tag, cli)`` pair is shown as a separate row in the
picker — tags with multiple live sessions open a sub-picker — and the
selection hand-off is CLI-agnostic:

  1. `chdir` into the session's original cwd (CLIs like Claude Code
     store transcripts under a cwd-derived slug, so resume only works
     when cwd matches).
  2. Export ``LEAP_RESUME_SESSION_ID``, ``LEAP_RESUME_CLI`` and
     ``LEAP_CLI`` before execing ``leap-main.sh <tag>``.  The server
     then calls the provider's ``resume_args(session_id)`` and prepends
     the right argv — ``--resume=<uuid>`` for Claude, ``resume <uuid>``
     for Codex, whatever a custom CLI implements.

Runs from any directory — the storage location is resolved from the
Leap project root recorded at install time, not from `cwd`.
"""

import json
import os
import select
import shutil
import socket
import stat
import sys
import termios
import time
import tty
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
STORAGE_DIR = PROJECT_DIR / ".storage"
SESSIONS_ROOT = STORAGE_DIR / "cli_sessions"
SOCKET_DIR = STORAGE_DIR / "sockets"
LEAP_MAIN = SCRIPT_DIR / "leap-main.sh"

# Make the ``leap`` package importable so we can ask providers for their
# display names.  Same pattern as leap-hook-process.py.
_SRC_DIR = SCRIPT_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

try:
    from leap.cli_providers.registry import get_display_name
except ImportError:
    def get_display_name(name: str) -> str:  # type: ignore[no-redef]
        return name

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
GREEN = "\033[32m"
RESET = "\033[0m"


def _load_tag_entries() -> list[dict]:
    """Collect every valid (on-disk) session per ``(tag, cli)`` pair.

    Scans ``.storage/cli_sessions/<cli>/*.json`` across every CLI subdir
    (Claude, Codex, any custom providers).  Returns rows shaped
    ``{tag, cli, sessions, last_seen}`` — each row is a distinct
    ``(tag, cli)`` pair so a tag that happened to run under two CLIs
    shows up twice (once per CLI).  ``sessions`` is newest-first.
    """
    if not SESSIONS_ROOT.is_dir():
        return []
    rows: list[dict] = []
    for cli_dir in SESSIONS_ROOT.iterdir():
        if not cli_dir.is_dir():
            continue
        cli = cli_dir.name
        for path in cli_dir.glob("*.json"):
            tag = path.stem
            try:
                entries = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(entries, list):
                continue
            sessions: list[dict] = []
            for entry in reversed(entries):  # newest-first
                if not isinstance(entry, dict):
                    continue
                transcript_path = entry.get("transcript_path", "")
                session_id = entry.get("session_id", "")
                if not session_id:
                    continue
                if transcript_path:
                    try:
                        size = os.path.getsize(transcript_path)
                    except OSError:
                        continue  # transcript file gone — drop
                else:
                    # Some CLIs may record without a transcript_path
                    # (e.g. a future CLI that only stores ids).  Keep
                    # the entry but mark size unknown.
                    size = 0
                sessions.append({
                    "session_id": session_id,
                    "transcript_path": transcript_path,
                    "cwd": entry.get("cwd", "") or (
                        os.path.dirname(transcript_path) if transcript_path else ""
                    ),
                    "last_seen": float(entry.get("last_seen") or 0),
                    "size": size,
                })
            if not sessions:
                continue
            rows.append({
                "tag": tag,
                "cli": cli,
                "sessions": sessions,
                "last_seen": sessions[0]["last_seen"],
            })
    rows.sort(key=lambda r: r["last_seen"], reverse=True)
    return rows


def _format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)}{unit}"
        n //= 1024
    return f"{int(n)}TB"


def _shorten_cwd(cwd: str) -> str:
    """Replace the user's home prefix with ``~``.

    Guards against the naive ``startswith`` trap — ``home="/Users/me"``
    must not match ``"/Users/mewithrestof/..."``; only ``home`` itself
    or a path that continues with ``/`` counts.
    """
    home = str(Path.home())
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return "~" + cwd[len(home):]
    return cwd


def _format_age(ts: float) -> str:
    if ts <= 0:
        return "unknown"
    delta = max(0, time.time() - ts)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def _server_alive(tag: str) -> bool:
    """Return True iff a Leap server for `tag` is currently accepting connections."""
    sock_path = SOCKET_DIR / f"{tag}.sock"
    try:
        st = sock_path.stat()
    except OSError:
        return False
    if not stat.S_ISSOCK(st.st_mode):
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(str(sock_path))
        s.close()
        return True
    except OSError:
        return False


def _get_key() -> str:
    """Read a single keypress using ``os.read`` on the raw fd.

    We deliberately avoid ``sys.stdin.read`` because Python's text-mode
    stdin buffer can swallow the `[A` follow-up bytes of an arrow-key
    escape sequence right after we consume the ESC byte — ``select`` on
    the fd would then see an empty OS buffer and we'd wrongly treat the
    arrow as a bare Esc.  ``os.read`` bypasses that buffer.

    Also handles SS3-form cursor keys (``ESC O A``/``O B``) for terminals
    in application cursor mode, and returns ``'quit'`` on stdin EOF so
    `_pick` can't get stuck in an infinite empty-read loop.
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
            # Read the whole CSI/SS3 tail in one call so Python buffering
            # can't fragment it across reads.
            rest = os.read(fd, 16).decode('utf-8', errors='replace')
            if rest.startswith('[A') or rest.startswith('OA'):
                return 'up'
            if rest.startswith('[B') or rest.startswith('OB'):
                return 'down'
            return ''  # unhandled sequence, already fully drained
        if ch in ('\r', '\n'):
            return 'enter'
        if ch in ('\x03', '\x04'):  # Ctrl+C / Ctrl+D
            return 'quit'
        if ch == 'q':
            return 'quit'
        if ch.isdigit():
            return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ''


def _truncate(plain: str, term_cols: int) -> str:
    if len(plain) > term_cols - 1:
        return plain[:term_cols - 2] + "…"
    return plain


def _write_row(plain: str, is_selected: bool, split_at: int) -> None:
    """Emit a picker row, colouring the selection marker + head.

    ``split_at`` is the plain-text offset where the dim "meta" tail begins
    (after the first age column).  Head includes the marker, tag/id, any
    suffix; tail is everything from the age onward.
    """
    head, tail = plain[:split_at], plain[split_at:]
    if is_selected:
        sys.stderr.write(f"{CYAN}{head[:4]}{RESET}{BOLD}{head[4:]}{RESET}{DIM}{tail}{RESET}\n")
    else:
        sys.stderr.write(f"{head}{DIM}{tail}{RESET}\n")


def _cli_label(cli: str) -> str:
    """``[cli]`` badge shown at the start of each tag row.

    Uses the provider's ``display_name`` when available (so custom CLIs
    show their registered name too); falls back to the raw registry key
    for unknown/removed providers so we never hide a resumable session.
    """
    label = get_display_name(cli)
    # Short, bracketed form — e.g. ``[Claude Code]`` → just ``[claude]``
    # would drop useful detail, so keep the display name as-is but trim
    # it if it happens to be very long.
    if len(label) > 18:
        label = label[:17] + "…"
    return f"[{label}]"


def _render_tags(rows: list[dict], idx: int, first: bool) -> None:
    """Render the top-level tag picker.

    Each row is a ``(tag, cli)`` pair prefixed with a ``[cli]`` badge so
    users can tell at a glance which CLI owns each recorded session.
    Tags with more than one recorded session show ``N sessions`` in the
    meta column instead of the UUID — the UUID becomes meaningful only
    in the sub-picker where each session is listed individually.
    """
    term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    if not first:
        sys.stderr.write(f"\033[{len(rows) + 2}A")
    sys.stderr.write("\033[J")
    sys.stderr.write(f"  {BOLD}Select a Leap session to resume:{RESET}\n")
    for i, row in enumerate(rows):
        marker = "❯" if i == idx else " "
        tag = row["tag"]
        label = _cli_label(row["cli"])
        newest = row["sessions"][0]
        age = _format_age(newest["last_seen"])
        cwd_display = _shorten_cwd(newest["cwd"])
        n = len(row["sessions"])
        if n > 1:
            meta = f"{n} sessions · {age} · {cwd_display}"
            first_meta_token = f"{n} sessions · "
        else:
            meta = f"{age} · {newest['session_id'][:8]} · {cwd_display}"
            first_meta_token = f"{age} · "
        plain = _truncate(f"  {marker} {label} {tag}  {meta}", term_cols)
        split = plain.find(first_meta_token)
        if split < 0:
            split = len(plain)
        _write_row(plain, is_selected=(i == idx), split_at=split)
    footer = _truncate("  ↑/↓ navigate · Enter to resume · Esc/q to cancel", term_cols)
    sys.stderr.write(f"{DIM}{footer}{RESET}\n")
    sys.stderr.flush()


def _render_sessions(tag: str, cli: str, sessions: list[dict], idx: int, first: bool) -> None:
    """Render the per-tag session sub-picker."""
    term_cols = shutil.get_terminal_size(fallback=(80, 24)).columns
    if not first:
        sys.stderr.write(f"\033[{len(sessions) + 2}A")
    sys.stderr.write("\033[J")
    header = _truncate(f"  Sessions for {_cli_label(cli)} '{tag}':", term_cols)
    sys.stderr.write(f"{BOLD}{header}{RESET}\n")
    for i, s in enumerate(sessions):
        marker = "❯" if i == idx else " "
        short_id = s["session_id"][:8]
        age = _format_age(s["last_seen"])
        size = _format_size(s["size"])
        cwd_display = _shorten_cwd(s["cwd"])
        plain = _truncate(f"  {marker} {short_id}  {age} · {size} · {cwd_display}", term_cols)
        split = plain.find(f"{age} · ")
        if split < 0:
            split = len(plain)
        _write_row(plain, is_selected=(i == idx), split_at=split)
    footer = _truncate("  ↑/↓ navigate · Enter to resume · Esc to go back · q to cancel", term_cols)
    sys.stderr.write(f"{DIM}{footer}{RESET}\n")
    sys.stderr.flush()


def _pick_tag(rows: list[dict]) -> Optional[dict]:
    idx = 0
    _render_tags(rows, idx, first=True)
    while True:
        key = _get_key()
        if key == 'up':
            idx = (idx - 1) % len(rows)
            _render_tags(rows, idx, first=False)
        elif key == 'down':
            idx = (idx + 1) % len(rows)
            _render_tags(rows, idx, first=False)
        elif key == 'enter':
            return rows[idx]
        elif key in ('quit', 'escape'):
            return None


# Sentinel for "user cancelled the whole picker from the sub-view"
_ABORT = object()


def _pick_session(tag: str, cli: str, sessions: list[dict]):
    """Return a chosen session dict, ``None`` to go back, or ``_ABORT`` to exit."""
    idx = 0
    _render_sessions(tag, cli, sessions, idx, first=True)
    while True:
        key = _get_key()
        if key == 'up':
            idx = (idx - 1) % len(sessions)
            _render_sessions(tag, cli, sessions, idx, first=False)
        elif key == 'down':
            idx = (idx + 1) % len(sessions)
            _render_sessions(tag, cli, sessions, idx, first=False)
        elif key == 'enter':
            return sessions[idx]
        elif key == 'escape':
            return None  # back to tag picker
        elif key == 'quit':
            return _ABORT


def main() -> int:
    rows = _load_tag_entries()
    if not rows:
        sys.stderr.write(
            f"  {YELLOW}No resumable Claude sessions found.{RESET}\n"
            f"  {DIM}Run `leap <tag>` with the Claude CLI at least once; "
            f"new sessions are recorded automatically.{RESET}\n"
        )
        return 1

    if not sys.stdin.isatty():
        sys.stderr.write(f"  {RED}leap --resume requires an interactive terminal.{RESET}\n")
        return 1

    # Outer loop so Esc from the session sub-picker can bounce back to
    # the tag picker without restarting `main`.
    chosen_tag: Optional[dict] = None
    chosen_session: Optional[dict] = None
    try:
        while True:
            tag_row = _pick_tag(rows)
            sys.stderr.write(f"\033[{len(rows) + 2}A\033[J")
            if tag_row is None:
                sys.stderr.write(f"  {DIM}Cancelled.{RESET}\n")
                return 130
            sessions = tag_row["sessions"]
            if len(sessions) == 1:
                chosen_tag, chosen_session = tag_row, sessions[0]
                break
            result = _pick_session(tag_row["tag"], tag_row["cli"], sessions)
            sys.stderr.write(f"\033[{len(sessions) + 2}A\033[J")
            if result is _ABORT:
                sys.stderr.write(f"  {DIM}Cancelled.{RESET}\n")
                return 130
            if result is None:
                continue  # Esc in sub-picker → back to tag picker
            chosen_tag, chosen_session = tag_row, result
            break
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        return 130

    tag = chosen_tag["tag"]
    cli = chosen_tag["cli"]
    session_id = chosen_session["session_id"]
    target_cwd = chosen_session["cwd"]

    if _server_alive(tag):
        sys.stderr.write(
            f"  {RED}A Leap server is already running for '{tag}'.{RESET}\n"
            f"  {DIM}Stop it first (exit the server terminal) and re-run "
            f"`leap --resume` to attach a fresh session.{RESET}\n"
        )
        return 1

    if target_cwd and not os.path.isdir(target_cwd):
        sys.stderr.write(
            f"  {RED}Session's original directory no longer exists: {target_cwd}{RESET}\n"
            f"  {DIM}Some CLIs (Claude Code) store transcripts per-cwd, so resume "
            f"cannot locate the session.{RESET}\n"
        )
        return 1

    sys.stderr.write(
        f"  {GREEN}Resuming{RESET} {_cli_label(cli)} {BOLD}{tag}{RESET} "
        f"{DIM}(session {session_id[:8]}"
        f"{' in ' + target_cwd if target_cwd else ''}){RESET}\n"
    )
    sys.stderr.flush()

    # Hand the session id + CLI to the server via env vars.  leap-main.sh
    # is CLI-agnostic — it just propagates LEAP_RESUME_* through.  The
    # server reads them, calls ``provider.resume_args(session_id)``, and
    # prepends the right argv tokens before spawning the CLI.
    env = dict(os.environ)
    env["LEAP_RESUME_SESSION_ID"] = session_id
    env["LEAP_RESUME_CLI"] = cli
    env["LEAP_CLI"] = cli
    if target_cwd:
        try:
            os.chdir(target_cwd)
        except OSError as e:
            sys.stderr.write(
                f"  {RED}Could not enter session's directory {target_cwd}: {e}{RESET}\n"
            )
            return 1
    # Exec leap-main.sh directly via its shebang — avoids a PATH lookup
    # for `bash` and preserves argv[0] = the real script path.
    os.execvpe(str(LEAP_MAIN), [str(LEAP_MAIN), tag], env)


if __name__ == "__main__":
    sys.exit(main() or 0)
