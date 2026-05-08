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

Pre-pick mode (``--cli=<X> --tag=<Y> --session=<Z>``): the GUI calls
this to hand off a session it already picked.  The interactive picker
is skipped; everything after the pick (cwd choice, tag-busy check,
server hand-off) still runs in the terminal where the user can
interact with prompts.

Runs from any directory — the storage location is resolved from the
Leap project root recorded at install time, not from `cwd`.
"""

import argparse
import json
import os
import re
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
    from leap.cli_providers.registry import get_display_name, get_provider
    from leap.utils.claude_session_move import RelocationError
    from leap.utils.resume_store import (
        TagRow, SessionRecord, load_tag_rows, load_raw_tag_rows,
        relocate_records,
    )
except ImportError:
    def get_display_name(name: str) -> str:  # type: ignore[no-redef]
        return name
    get_provider = None  # type: ignore
    RelocationError = Exception  # type: ignore
    TagRow = None  # type: ignore
    SessionRecord = None  # type: ignore
    load_tag_rows = None  # type: ignore
    load_raw_tag_rows = None  # type: ignore
    relocate_records = None  # type: ignore

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
GREEN = "\033[32m"
RESET = "\033[0m"


def _load_tag_entries() -> list:
    """Return picker rows via the shared :mod:`leap.utils.resume_store`.

    The store hands us fully-filtered :class:`TagRow` values (stale
    transcripts already dropped, newest-first).  We keep this thin
    wrapper instead of calling ``load_tag_rows`` directly so the
    import-failure fallback (when leap isn't on ``sys.path`` for
    whatever reason) degrades to an empty list instead of an import
    error.
    """
    if load_tag_rows is None:
        return []
    return load_tag_rows(STORAGE_DIR)


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


def _live_tag_cli_map() -> dict:
    """For every live Leap socket, read ``<tag>.meta`` and return
    ``{tag: cli_provider}`` — the authoritative source of which CLI the
    running server is actually using *right now*.

    A single Leap tag can have recorded sessions across multiple CLIs
    over its lifetime (``cli_sessions/claude/9.json`` from yesterday's
    Claude run plus ``cli_sessions/gemini/9.json`` from today's Gemini
    run).  Only the CLI the *live* server is running should count as an
    owner of the tag's current session.
    """
    live: dict[str, str] = {}
    if not SOCKET_DIR.is_dir():
        return live
    for sock in SOCKET_DIR.glob('*.sock'):
        tag = sock.stem
        if not _server_alive(tag):
            continue
        try:
            data = json.loads((SOCKET_DIR / f'{tag}.meta').read_text())
        except (OSError, json.JSONDecodeError):
            continue
        cli = data.get('cli_provider') if isinstance(data, dict) else None
        if cli:
            live[tag] = cli
    return live


def _live_session_owners(rows: list) -> dict:
    """Return ``{session_id: [(cli, tag), ...]}`` for sessions currently
    running in a Leap server.

    Must be called with **raw** (un-deduped) rows — see
    :func:`leap.utils.resume_store.load_raw_tag_rows`.  If two live
    tags have recorded the same CLI session (a fork race that's
    physically impossible for today's CLIs but would become possible
    if a future CLI supported multi-seat resume), display-layer dedup
    would otherwise hide the older tag and we'd fail to warn the user
    about one of the live owners.

    A row counts as owner only when the live Leap server for its tag is
    actually running row.cli (per the tag's ``.meta`` file).  This
    prevents stale records from a past CLI run under the same tag from
    being misattributed as owners — e.g. if tag ``9`` previously held
    a Claude Code session but today's live server under that tag is
    running Gemini CLI, the old Claude record must not claim ownership.
    """
    live_clis = _live_tag_cli_map()
    owners: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        if live_clis.get(row.tag) != row.cli:
            continue
        newest = row.sessions[0]
        owners.setdefault(newest.session_id, []).append((row.cli, row.tag))
    return owners


_TAG_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


def _prompt_new_tag(old_tag: str) -> Optional[str]:
    """Ask for a new Leap tag to resume the picked session under.

    Called when the tag attached to the picked session still has a
    running Leap server that isn't using this specific session (case 2
    in the resume decision).  Loops on validation errors so a typo
    doesn't dump the user back to the shell — only an empty entry /
    Ctrl+C returns ``None`` (i.e. cancel).
    """
    sys.stderr.write(
        f"  {YELLOW}A different CLI session is currently running under "
        f"Leap tag {BOLD}'{old_tag}'{RESET}{YELLOW}.{RESET}\n"
        f"  {DIM}Enter a new Leap tag to resume your selected CLI session "
        f"(blank/Ctrl+C to cancel):{RESET}\n"
    )
    while True:
        sys.stderr.write(f"  {BOLD}new tag:{RESET} ")
        sys.stderr.flush()
        try:
            line = input('').strip()
        except (KeyboardInterrupt, EOFError):
            sys.stderr.write('\n')
            return None
        if not line:
            return None
        if not _TAG_RE.match(line):
            sys.stderr.write(
                f"  {RED}Invalid — letters, numbers, '-' and '_' only, "
                f"starting with a letter or digit.  Try again.{RESET}\n"
            )
            continue
        if line == old_tag:
            sys.stderr.write(
                f"  {RED}That's the same tag the running server is "
                f"occupying — pick a different one.{RESET}\n"
            )
            continue
        if _server_alive(line):
            sys.stderr.write(
                f"  {RED}Tag '{line}' also has a running Leap server.  "
                f"Try another.{RESET}\n"
            )
            continue
        return line


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
        if ch in ('\x7f', '\x08'):  # DEL / Backspace
            return 'backspace'
        if ch.isprintable():
            return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ''


def _truncate(plain: str, term_cols: int) -> str:
    if len(plain) > term_cols - 1:
        return plain[:term_cols - 2] + "…"
    return plain


def _viewport(idx: int, total: int, term_rows: int) -> tuple[int, int]:
    """Return (start, end) slice of rows that keeps *idx* visible."""
    # Budget: header(1) + search(1) + top-ind(1) + rows + bottom-ind(1) + footer(1) = rows + 5
    visible = max(1, term_rows - 5)
    if total <= visible:
        return 0, total
    start = max(0, min(idx - visible // 2, total - visible))
    return start, start + visible


def _row_freshness(r) -> float:
    """Sort key: most-recent session's ``last_seen`` (0 for empty rows)."""
    return r.sessions[0].last_seen if r.sessions else 0.0


def _filter_rows(rows: list, query: str) -> list:
    """Tag/CLI matches first, cwd-only matches after; each bucket
    sorted newest-first.

    A row that matches the query in *any* of (tag, cli) outranks a
    row that matches only in cwd, so the user can type a tag
    fragment without having a working-directory hit drown out the
    obvious answer.  Inside each bucket we sort explicitly by the
    tag's freshest session's ``last_seen`` so the "newest first"
    contract holds regardless of the caller's input order.
    """
    if not query:
        return sorted(rows, key=_row_freshness, reverse=True)
    q = query.lower()
    tag_hits: list = []
    cwd_hits: list = []
    for r in rows:
        if q in r.tag.lower() or q in r.cli.lower():
            tag_hits.append(r)
        elif q in _shorten_cwd(r.sessions[0].cwd).lower():
            cwd_hits.append(r)
    tag_hits.sort(key=_row_freshness, reverse=True)
    cwd_hits.sort(key=_row_freshness, reverse=True)
    return tag_hits + cwd_hits


def _filter_sessions(sessions: list, query: str) -> list:
    """Session-id (short) matches first, cwd-only matches after; each
    bucket sorted newest-first by ``last_seen``."""
    key = lambda s: s.last_seen
    if not query:
        return sorted(sessions, key=key, reverse=True)
    q = query.lower()
    id_hits: list = []
    cwd_hits: list = []
    for s in sessions:
        if q in s.session_id[:8].lower():
            id_hits.append(s)
        elif q in _shorten_cwd(s.cwd).lower():
            cwd_hits.append(s)
    id_hits.sort(key=key, reverse=True)
    cwd_hits.sort(key=key, reverse=True)
    return id_hits + cwd_hits


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


def _render_tags(rows: list, idx: int, first: bool, last_n: int = 0,
                 query: str = "") -> int:
    """Render the top-level tag picker inside a scrolling viewport.

    Each row is a ``(tag, cli)`` pair prefixed with a ``[cli]`` badge so
    users can tell at a glance which CLI owns each recorded session.
    Tags with more than one recorded session show ``N sessions`` in the
    meta column instead of the UUID — the UUID becomes meaningful only
    in the sub-picker where each session is listed individually.

    Returns the number of body lines rendered (excluding header/footer)
    so the caller can move the cursor up by exactly that amount + 2 on
    the next redraw.
    """
    term = shutil.get_terminal_size(fallback=(80, 24))
    term_cols, term_rows = term.columns, term.lines
    start, end = _viewport(idx, len(rows), term_rows)
    if not first:
        sys.stderr.write(f"\033[{last_n + 2}A")
    sys.stderr.write("\033[J")
    sys.stderr.write(f"  {BOLD}Select a Leap session to resume:{RESET}\n")
    n = 1  # search line is always rendered
    if query:
        sys.stderr.write(f"  {CYAN}/ {query}{DIM}_{RESET}\n")
    else:
        sys.stderr.write(f"  {DIM}/ type to filter…{RESET}\n")
    if not rows:
        sys.stderr.write(f"  {DIM}No matches.{RESET}\n")
        n += 1
    else:
        if start > 0:
            sys.stderr.write(f"  {DIM}↑ {start} more{RESET}\n")
            n += 1
        for i in range(start, end):
            row = rows[i]
            marker = "❯" if i == idx else " "
            label = _cli_label(row.cli)
            newest = row.sessions[0]
            age = _format_age(newest.last_seen)
            cwd_display = _shorten_cwd(newest.cwd)
            nsess = len(row.sessions)
            if nsess > 1:
                meta = f"{nsess} sessions · {age} · {cwd_display}"
                first_meta_token = f"{nsess} sessions · "
            else:
                meta = f"{age} · {newest.session_id[:8]} · {cwd_display}"
                first_meta_token = f"{age} · "
            plain = _truncate(f"  {marker} {label} {row.tag}  {meta}", term_cols)
            split = plain.find(first_meta_token)
            if split < 0:
                split = len(plain)
            _write_row(plain, is_selected=(i == idx), split_at=split)
            n += 1
        below = len(rows) - end
        if below > 0:
            sys.stderr.write(f"  {DIM}↓ {below} more{RESET}\n")
            n += 1
    footer = _truncate("  ↑/↓ navigate · Enter to resume · Esc/q to cancel", term_cols)
    sys.stderr.write(f"{DIM}{footer}{RESET}\n")
    sys.stderr.flush()
    return n


def _render_sessions(tag: str, cli: str, sessions: list, idx: int, first: bool,
                     last_n: int = 0, query: str = "") -> int:
    """Render the per-tag session sub-picker inside a scrolling viewport.

    Returns the number of body lines rendered (excluding header/footer).
    """
    term = shutil.get_terminal_size(fallback=(80, 24))
    term_cols, term_rows = term.columns, term.lines
    start, end = _viewport(idx, len(sessions), term_rows)
    if not first:
        sys.stderr.write(f"\033[{last_n + 2}A")
    sys.stderr.write("\033[J")
    header = _truncate(f"  Sessions for {_cli_label(cli)} '{tag}':", term_cols)
    sys.stderr.write(f"{BOLD}{header}{RESET}\n")
    n = 1  # search line is always rendered
    if query:
        sys.stderr.write(f"  {CYAN}/ {query}{DIM}_{RESET}\n")
    else:
        sys.stderr.write(f"  {DIM}/ type to filter…{RESET}\n")
    if not sessions:
        sys.stderr.write(f"  {DIM}No matches.{RESET}\n")
        n += 1
    else:
        if start > 0:
            sys.stderr.write(f"  {DIM}↑ {start} more{RESET}\n")
            n += 1
        for i in range(start, end):
            s = sessions[i]
            marker = "❯" if i == idx else " "
            short_id = s.session_id[:8]
            age = _format_age(s.last_seen)
            size = _format_size(s.size)
            cwd_display = _shorten_cwd(s.cwd)
            plain = _truncate(f"  {marker} {short_id}  {age} · {size} · {cwd_display}", term_cols)
            split = plain.find(f"{age} · ")
            if split < 0:
                split = len(plain)
            _write_row(plain, is_selected=(i == idx), split_at=split)
            n += 1
        below = len(sessions) - end
        if below > 0:
            sys.stderr.write(f"  {DIM}↓ {below} more{RESET}\n")
            n += 1
    footer = _truncate("  ↑/↓ navigate · Enter to resume · Esc to go back · q to cancel", term_cols)
    sys.stderr.write(f"{DIM}{footer}{RESET}\n")
    sys.stderr.flush()
    return n


def _pick_tag(all_rows: list) -> tuple:
    query = ""
    filtered = all_rows
    idx = 0
    n = _render_tags(filtered, idx, first=True, query=query)
    while True:
        key = _get_key()
        if key == 'up':
            if filtered:
                idx = (idx - 1) % len(filtered)
            n = _render_tags(filtered, idx, first=False, last_n=n, query=query)
        elif key == 'down':
            if filtered:
                idx = (idx + 1) % len(filtered)
            n = _render_tags(filtered, idx, first=False, last_n=n, query=query)
        elif key == 'enter':
            if filtered:
                return filtered[idx], n
        elif key in ('quit', 'escape'):
            return None, n
        elif key == 'backspace':
            query = query[:-1]
            filtered = _filter_rows(all_rows, query)
            idx = 0
            n = _render_tags(filtered, idx, first=False, last_n=n, query=query)
        elif len(key) == 1 and key.isprintable():
            query += key
            filtered = _filter_rows(all_rows, query)
            idx = 0
            n = _render_tags(filtered, idx, first=False, last_n=n, query=query)


# Sentinel for "user cancelled the whole picker from the sub-view"
_ABORT = object()


def _pick_session(tag: str, cli: str, all_sessions: list) -> tuple:
    """Return ``(SessionRecord, n)``, ``(None, n)`` to go back, or ``(_ABORT, n)``."""
    query = ""
    filtered = all_sessions
    idx = 0
    n = _render_sessions(tag, cli, filtered, idx, first=True, query=query)
    while True:
        key = _get_key()
        if key == 'up':
            if filtered:
                idx = (idx - 1) % len(filtered)
            n = _render_sessions(tag, cli, filtered, idx, first=False, last_n=n, query=query)
        elif key == 'down':
            if filtered:
                idx = (idx + 1) % len(filtered)
            n = _render_sessions(tag, cli, filtered, idx, first=False, last_n=n, query=query)
        elif key == 'enter':
            if filtered:
                return filtered[idx], n
        elif key == 'escape':
            return None, n  # back to tag picker
        elif key == 'quit':
            return _ABORT, n
        elif key == 'backspace':
            query = query[:-1]
            filtered = _filter_sessions(all_sessions, query)
            idx = 0
            n = _render_sessions(tag, cli, filtered, idx, first=False, last_n=n, query=query)
        elif len(key) == 1 and key.isprintable():
            query += key
            filtered = _filter_sessions(all_sessions, query)
            idx = 0
            n = _render_sessions(tag, cli, filtered, idx, first=False, last_n=n, query=query)


_CWD_CHOICE_OPTIONS: tuple[tuple[str, str], ...] = (
    # Action-first labels — make it explicit what each option does to
    # the user's cwd:
    #   "original" → leap-resume.py chdirs into the recorded path,
    #     leaving the session's on-disk state where it is.
    #   "current"  → leap-resume.py calls provider.relocate_session()
    #     so the session's on-disk state ends up under the current
    #     cwd (file moves for cwd-bound CLIs; logical no-op move for
    #     Codex which only updates Leap's recorded cwd).
    ('original', 'CD into the original directory'),
    ('current',  'Stay in the current directory'),
)


def _render_cwd_choice(
    recorded_cwd: str, current_cwd: str, idx: int,
    first: bool, last_n: int = 0,
) -> int:
    """Draw the cwd-choice picker and return the body-line count.

    Mirrors the redraw protocol used by :func:`_render_tags` so the
    next call can erase exactly the right region by moving the cursor
    up ``last_n + 2`` lines (header + footer = 2 framing lines).
    """
    if not first:
        sys.stderr.write(f"\033[{last_n + 2}A")
    sys.stderr.write("\033[J")
    sys.stderr.write(f"  {BOLD}Where do you want to resume?{RESET}\n")
    paths = (_shorten_cwd(recorded_cwd), _shorten_cwd(current_cwd))
    n = 0
    for i, (_value, label) in enumerate(_CWD_CHOICE_OPTIONS):
        marker = "❯" if i == idx else " "
        path = paths[i]
        if i == idx:
            sys.stderr.write(
                f"  {CYAN}{marker}{RESET} "
                f"{BOLD}{label}:{RESET}  {DIM}{path}{RESET}\n"
            )
        else:
            sys.stderr.write(
                f"  {marker} {DIM}{label}:{RESET}  {DIM}{path}{RESET}\n"
            )
        n += 1
    footer = "  ↑/↓ navigate · Enter to confirm · Esc/q to cancel"
    sys.stderr.write(f"{DIM}{footer}{RESET}\n")
    sys.stderr.flush()
    return n


def _ask_cwd_choice(
    recorded_cwd: str, current_cwd: str,
) -> Optional[str]:
    """Arrow-key picker: pick between resuming in the recorded or current cwd.

    Returns ``'original'`` / ``'current'`` / ``None`` (cancelled via
    Esc/q/Ctrl+C/Ctrl+D).  Caller should only invoke when the two
    cwds actually differ — picking is meaningless otherwise.

    Same UX as the tag/session pickers above: ``❯`` cursor, ↑/↓
    to move, Enter to confirm.  Falls back gracefully when the keypress
    reader sees EOF (returns ``None`` so callers can treat it as a
    Cancel — no caller relies on a specific exit code here).
    """
    idx = 0
    n = _render_cwd_choice(recorded_cwd, current_cwd, idx, first=True)
    while True:
        key = _get_key()
        if key == 'up':
            idx = (idx - 1) % len(_CWD_CHOICE_OPTIONS)
            n = _render_cwd_choice(
                recorded_cwd, current_cwd, idx, first=False, last_n=n,
            )
        elif key == 'down':
            idx = (idx + 1) % len(_CWD_CHOICE_OPTIONS)
            n = _render_cwd_choice(
                recorded_cwd, current_cwd, idx, first=False, last_n=n,
            )
        elif key == 'enter':
            # Erase the picker so the subsequent "Resuming…" / "Cancelled."
            # status line lands on a clean region — same gesture
            # ``main()`` performs after ``_pick_tag`` / ``_pick_session``.
            sys.stderr.write(f"\033[{n + 2}A\033[J")
            sys.stderr.flush()
            return _CWD_CHOICE_OPTIONS[idx][0]
        elif key in ('quit', 'escape'):
            sys.stderr.write(f"\033[{n + 2}A\033[J")
            sys.stderr.flush()
            return None
        # any other key: ignore and stay on the current selection


def _try_relocate(
    *,
    cli: str,
    session_id: str,
    old_transcript_path: str,
    src_cwd: str,
    dst_cwd: str,
) -> Optional[bool]:
    """Move the picked CLI session's on-disk state from ``src_cwd`` to ``dst_cwd``.

    Returns ``True`` when the session was relocated (caller should skip
    the legacy chdir-into-original-cwd step), ``False`` when the
    provider doesn't support cross-cwd relocation (caller falls back
    to chdir), or ``None`` on a hard error (caller exits non-zero).

    For providers whose ``relocate_session`` actually moves files
    (Claude/Gemini/Cursor), the move is signal-blocked so Ctrl+C
    cannot interrupt a half-committed state, and the bookkeeping
    callback runs inside that same critical section.  Codex's
    ``relocate_session`` is a logical no-op (no files move; only the
    recorded cwd in ``.storage/cli_sessions/codex/<tag>.json`` is
    bumped) so it doesn't need the signal block.
    """
    if get_provider is None or relocate_records is None:
        return False  # leap module wasn't importable; fall back to chdir
    try:
        provider = get_provider(cli)
    except ValueError:
        return False

    def _on_committed(new_path: str) -> None:
        # Inside the signal-blocked section: rewrite every
        # cli_sessions/<cli>/*.json entry for this session so the
        # picker (and any other consumer) reflects the new cwd
        # immediately.  Matches by session_id (always present,
        # always unique per tag) rather than by transcript_path so
        # CLIs like Cursor that record an empty transcript_path
        # still get their cwd bumped.
        #
        # ``new_transcript_path=new_path`` updates the stored path
        # for CLIs that actually move files (Claude/Gemini moves to
        # new on-disk path; Codex's no-op move passes through the
        # unchanged path).  Cursor's relocate returns the new chat
        # dir path but its picker records keep transcript_path
        # empty by convention — so we explicitly pass an empty
        # value for cursor-agent to preserve that.
        nt = '' if cli == 'cursor-agent' else new_path
        relocate_records(
            STORAGE_DIR, cli,
            session_id=session_id,
            new_cwd=dst_cwd,
            new_transcript_path=nt,
        )

    try:
        new_path = provider.relocate_session(
            session_id, src_cwd, dst_cwd,
            transcript_path=old_transcript_path,
            on_committed=_on_committed,
        )
    except RelocationError as e:
        sys.stderr.write(
            f"  {RED}Could not relocate session to current directory:{RESET}\n"
            f"  {RED}{e}{RESET}\n"
        )
        return None
    except Exception as e:
        sys.stderr.write(
            f"  {RED}Unexpected error relocating session: {e}{RESET}\n"
        )
        return None

    return new_path is not None


def _parse_args() -> argparse.Namespace:
    """Parse pre-pick args (used by the GUI) or fall through to interactive.

    All three of ``--cli``/``--tag``/``--session`` must be supplied
    together; passing only some is rejected so a typo can't accidentally
    fall through to the picker (where the user might re-pick something
    different and be confused).
    """
    p = argparse.ArgumentParser(
        prog='leap --resume',
        description='Pick a recorded CLI session and resume it.',
    )
    p.add_argument('--cli', dest='cli', default='',
                   help="Pre-picked CLI provider name (skip the picker).")
    p.add_argument('--tag', dest='tag', default='',
                   help="Pre-picked Leap tag (skip the picker).")
    p.add_argument('--session', dest='session', default='',
                   help="Pre-picked CLI session id (skip the picker).")
    args = p.parse_args()
    if any((args.cli, args.tag, args.session)):
        if not all((args.cli, args.tag, args.session)):
            p.error("--cli, --tag and --session must be provided together")
    return args


def _lookup_pre_picked(
    rows: list, cli: str, tag: str, session_id: str,
) -> Optional[tuple]:
    """Find ``(TagRow, SessionRecord)`` for an already-picked session.

    Returns ``None`` when the records have moved on since the GUI
    showed them — e.g. the user picked a session that was relocated
    or pruned between dialog open and terminal launch.  The caller
    surfaces a clear error rather than bouncing them into the
    interactive picker (which would surprise the user with a fresh
    selection step they didn't ask for).
    """
    for r in rows:
        if r.cli != cli or r.tag != tag:
            continue
        for s in r.sessions:
            if s.session_id == session_id:
                return r, s
    return None


def main() -> int:
    args = _parse_args()
    rows = _load_tag_entries()
    if not rows:
        sys.stderr.write(
            f"  {YELLOW}No resumable CLI sessions found.{RESET}\n"
            f"  {DIM}Run `leap <tag>` with any CLI that implements the Leap "
            f"Resume protocol at least once; new sessions are recorded "
            f"automatically.{RESET}\n"
        )
        return 1

    if not sys.stdin.isatty():
        sys.stderr.write(f"  {RED}leap --resume requires an interactive terminal.{RESET}\n")
        return 1

    chosen_tag = None
    chosen_session = None

    if args.cli and args.tag and args.session:
        # Pre-pick mode (GUI hand-off).  Skip the picker entirely; the
        # GUI already did the selection step.  Surface a clear error
        # if the records moved on since then so we never fall back to
        # interactive picking against the user's expectations.
        found = _lookup_pre_picked(rows, args.cli, args.tag, args.session)
        if found is None:
            sys.stderr.write(
                f"  {RED}The picked session is no longer recorded — it may "
                f"have been pruned, relocated, or its transcript deleted.{RESET}\n"
                f"  {DIM}cli={args.cli} tag={args.tag} session={args.session[:8]}…{RESET}\n"
            )
            return 1
        chosen_tag, chosen_session = found
    else:
        # Interactive picker.  Outer loop so Esc from the session
        # sub-picker bounces back to the tag picker without restarting.
        try:
            while True:
                tag_row, n_tags = _pick_tag(rows)
                sys.stderr.write(f"\033[{n_tags + 2}A\033[J")
                if tag_row is None:
                    sys.stderr.write(f"  {DIM}Cancelled.{RESET}\n")
                    return 130
                sessions = tag_row.sessions
                if len(sessions) == 1:
                    chosen_tag, chosen_session = tag_row, sessions[0]
                    break
                result, n_sessions = _pick_session(tag_row.tag, tag_row.cli, sessions)
                sys.stderr.write(f"\033[{n_sessions + 2}A\033[J")
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

    tag = chosen_tag.tag
    cli = chosen_tag.cli
    session_id = chosen_session.session_id
    target_cwd = chosen_session.cwd

    # Is the *CLI session UUID* already being used by a live Leap server?
    # That's the real conflict — not whether the Leap tag has a running
    # server (the user may have started it fresh without resuming).
    # Use RAW rows here (pre-dedup): display-layer dedup drops the
    # older tag's copy of a session, but an ownership check needs
    # every live tag to have a chance to claim the session.
    raw_rows = load_raw_tag_rows(STORAGE_DIR) if load_raw_tag_rows else rows
    owners = _live_session_owners(raw_rows).get(session_id, [])
    # Filter out the obvious self-case where the picked tag's server *is*
    # running exactly this session — we still want to tell the user where
    # to go.
    if owners:
        tags_str = ", ".join(
            f"{BOLD}{u_tag}{RESET} {DIM}({get_display_name(u_cli)}){RESET}"
            for u_cli, u_tag in owners
        )
        sys.stderr.write(
            f"  {RED}This CLI session is already running under Leap tag "
            f"{tags_str}{RED}.{RESET}\n"
            f"  {DIM}Check your open terminals (or use the Leap Monitor "
            f"app, if installed) to find it.{RESET}\n"
        )
        return 1

    # Session itself is free — but if the picked tag's server is running
    # something else, we need a different tag to spawn the resumed session
    # under.  Ask the user.
    if _server_alive(tag):
        new_tag = _prompt_new_tag(tag)
        if not new_tag:
            sys.stderr.write(f"  {DIM}Cancelled.{RESET}\n")
            return 130
        tag = new_tag

    # Cross-cwd resume: only providers with cwd-bound resume need the
    # picker (Claude, Gemini).  CLIs that key sessions by UUID alone
    # (Codex) or handle cross-cwd resume in their own UI (Cursor's
    # built-in prompt) keep working from the current cwd unmodified —
    # asking would just be a redundant prompt before their own.
    current_cwd = os.getcwd()
    needs_cwd_pick = False
    if get_provider is not None:
        try:
            needs_cwd_pick = get_provider(cli).requires_cwd_bound_resume
        except ValueError:
            needs_cwd_pick = False
    if not needs_cwd_pick:
        # Hand off from wherever the user currently is; the CLI will
        # locate the session by id (or prompt the user itself).
        target_cwd = current_cwd
    elif target_cwd and target_cwd != current_cwd:
        choice = _ask_cwd_choice(target_cwd, current_cwd)
        if choice is None:
            sys.stderr.write(f"  {DIM}Cancelled.{RESET}\n")
            return 130
        if choice == 'current':
            relocated = _try_relocate(
                cli=cli,
                session_id=session_id,
                old_transcript_path=chosen_session.transcript_path,
                src_cwd=target_cwd,
                dst_cwd=current_cwd,
            )
            if relocated is None:
                return 1
            # ``_try_relocate`` returns False when the provider doesn't
            # support cross-cwd relocation.  In that case the resume
            # itself may not find the session, but the user explicitly
            # opted into the current cwd — accept it and let the CLI
            # start fresh if it has to.
            target_cwd = current_cwd

    # Verify the chosen cwd is usable.  When the user picked "current"
    # this is by definition the current dir (always exists); when they
    # picked "original", we check the recorded dir is still on disk.
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
        # Keep ``$PWD`` in sync with the new cwd.  ``os.chdir`` updates
        # the OS cwd but not the env var; bash usually self-corrects on
        # startup, but downstream tools that read ``$PWD`` directly
        # (instead of ``getcwd``) would otherwise see the stale path.
        env["PWD"] = target_cwd
    # Exec leap-main.sh directly via its shebang — avoids a PATH lookup
    # for `bash` and preserves argv[0] = the real script path.
    os.execvpe(str(LEAP_MAIN), [str(LEAP_MAIN), tag], env)


if __name__ == "__main__":
    sys.exit(main() or 0)
