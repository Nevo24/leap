"""Shared read/write layer for ``leap --resume`` session records.

Every resumable CLI session recorded by the hook processor lands in
``<storage>/cli_sessions/<cli>/<tag>.json``.  This module is the
single source of truth for that file's schema and lifecycle so the
writer (``leap-hook-process.py``) and the reader (``leap-resume.py``)
can't drift apart.

On disk, each file holds a JSON list of entries shaped::

    {
        "session_id":      str,    # CLI-specific stable id (uuid, chat id, …)
        "transcript_path": str,    # may be '' for CLIs that don't write one
        "cwd":             str,    # the CLI's cwd at record time
        "last_seen":       float,  # Unix timestamp of the most recent hook fire
    }

Writers call :func:`record_session` to upsert an entry (dedup by
``session_id``, cap to :data:`MAX_ENTRIES_PER_TAG`, atomic rename);
readers call :func:`load_tag_rows` to get a pre-filtered list of
``TagRow`` values, newest-first, with stale (disk-deleted transcript)
entries already dropped.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


# Cap per (cli, tag) file; oldest-first trimming keeps this bounded.
MAX_ENTRIES_PER_TAG: int = 20

# Matches the ``<tag>`` and ``<cli>`` identifiers we're willing to
# persist on disk — plain alphanumerics plus ``-``/``_``.  Guards
# against path-traversal in the ``.storage/cli_sessions/<cli>/<tag>.json``
# layout when the PPID-walk fallback recovers a crafted PID mapping or
# an attacker otherwise controls the env vars the hook processor reads.
_SAFE_ID: re.Pattern[str] = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


def _is_safe_id(value: str) -> bool:
    return bool(_SAFE_ID.match(value))


@dataclass(frozen=True)
class SessionRecord:
    """One recorded resume target — a single past session for (cli, tag)."""
    session_id: str
    transcript_path: str
    cwd: str
    last_seen: float
    size: int = 0  # transcript bytes on disk (0 when no transcript_path)


@dataclass
class TagRow:
    """All still-valid sessions for one ``(tag, cli)`` pair, newest-first."""
    tag: str
    cli: str
    sessions: list[SessionRecord] = field(default_factory=list)
    last_seen: float = 0.0


def _sessions_root(storage_dir: Path) -> Path:
    return storage_dir / "cli_sessions"


def _tag_file(storage_dir: Path, cli: str, tag: str) -> Path:
    return _sessions_root(storage_dir) / cli / f"{tag}.json"


def _load_raw_entries(tag_file: Path) -> list[dict]:
    """Return the on-disk list of entry dicts, or ``[]`` on any error.

    Silently drops non-dict entries so the rest of the file survives a
    single corrupt record.
    """
    if not tag_file.is_file():
        return []
    try:
        parsed = json.loads(tag_file.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(parsed, list):
        return []
    return [e for e in parsed if isinstance(e, dict)]


def record_session(
    storage_dir: Path,
    cli: str,
    tag: str,
    *,
    session_id: str,
    transcript_path: str = "",
    cwd: str = "",
) -> None:
    """Upsert an entry into ``<storage>/cli_sessions/<cli>/<tag>.json``.

    Dedupes by ``session_id`` (a repeated hook for the same session
    just bumps ``last_seen``), trims to :data:`MAX_ENTRIES_PER_TAG`, and
    writes atomically via tmp-file + ``os.replace``.  Silent on all
    failures — this is best-effort bookkeeping, never the critical path.
    """
    if not (cli and tag and session_id):
        return
    # Defense in depth: tag/cli land in a filesystem path; reject anything
    # that could escape ``cli_sessions/<cli>/`` even if the caller's
    # upstream validation was bypassed.
    if not (_is_safe_id(cli) and _is_safe_id(tag)):
        return
    tag_file = _tag_file(storage_dir, cli, tag)
    # Normalize transcript_path to absolute using the hook's cwd as the
    # reference frame — if the reader (the picker) runs from a different
    # cwd, a relative path would otherwise resolve incorrectly for the
    # ``os.path.getsize`` / stale-filter checks in ``_resumable_sessions``.
    # Today's built-in CLIs all pass absolute paths; this hardens against
    # a future custom CLI that emits a relative one.
    if transcript_path and not os.path.isabs(transcript_path):
        transcript_path = os.path.abspath(transcript_path)
    try:
        tag_file.parent.mkdir(parents=True, exist_ok=True)
        entries = _load_raw_entries(tag_file)
        entries = [e for e in entries if e.get("session_id") != session_id]
        entries.append({
            "session_id": session_id,
            "transcript_path": transcript_path or "",
            "cwd": cwd or "",
            "last_seen": time.time(),
        })
        entries = entries[-MAX_ENTRIES_PER_TAG:]
        tmp = tag_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries, indent=2))
        os.replace(tmp, tag_file)
    except (OSError, ValueError):
        pass


def _resumable_sessions(raw: list[dict]) -> list[SessionRecord]:
    """Project raw entries → newest-first SessionRecords, dropping stale ones.

    A session is "stale" when its recorded ``transcript_path`` no longer
    exists on disk — the CLI can't resume from a file that's gone.
    Entries without a transcript_path (a future CLI that records only
    ids) are kept with ``size=0``.
    """
    out: list[SessionRecord] = []
    for entry in reversed(raw):  # file is oldest-first; we want newest-first
        sid = entry.get("session_id", "")
        if not sid:
            continue
        tp = entry.get("transcript_path", "") or ""
        size = 0
        if tp:
            try:
                size = os.path.getsize(tp)
            except OSError:
                continue  # transcript gone — drop
        # last_seen *should* always be a float we wrote via time.time(),
        # but a hand-edited or corrupted record shouldn't crash the whole
        # picker — coerce defensively and fall back to 0.
        try:
            last_seen = float(entry.get("last_seen") or 0)
        except (TypeError, ValueError):
            last_seen = 0.0
        out.append(SessionRecord(
            session_id=sid,
            transcript_path=tp,
            cwd=entry.get("cwd", "") or "",
            last_seen=last_seen,
            size=size,
        ))
    return out


def prune_stale(storage_dir: Path) -> int:
    """Delete ``cli_sessions/<cli>/<tag>.json`` files whose every entry
    points at a transcript that's been removed from disk.

    The picker already filters stale entries at read time, but orphaned
    files linger forever otherwise — once every entry's transcript is
    gone, the file is dead weight.  Files with *any* live entries are
    left alone; their oldest-first 20-entry cap lets them self-heal as
    fresh sessions push stale ones out.

    Returns the number of files deleted (for logging / testing).

    Note: on macOS APFS under heavy concurrent invocation, ``os.unlink``
    can report success from multiple threads racing on the same file
    (kernel-level quirk, not a bug here).  The final on-disk state is
    always correct — all stale files end up gone — but the returned
    count may be over-reported when several ``prune_stale`` calls race.
    The caller (``cleanup_dead_sockets``) discards the return value, so
    this is purely a cosmetic concern for tests.
    """
    root = _sessions_root(storage_dir)
    try:
        if not root.is_dir():
            return 0
        cli_dirs = list(root.iterdir())
    except OSError:
        # Permission denied, I/O error, etc. on the root itself — best-effort
        # means giving up rather than crashing the caller.
        return 0
    removed = 0
    for cli_dir in cli_dirs:
        try:
            if not cli_dir.is_dir():
                continue
            tag_files = list(cli_dir.glob('*.json'))
        except OSError:
            continue
        for tag_file in tag_files:
            # mtime guard against the TOCTOU race where the hook atomically
            # writes a fresh entry AFTER we decide to delete but BEFORE we
            # actually ``unlink``.  Without the re-check we'd wipe the new
            # entry.  Narrows the window to the ~µs between the second
            # ``stat`` and the ``unlink``.
            try:
                mtime_before = tag_file.stat().st_mtime
            except OSError:
                continue
            entries = _load_raw_entries(tag_file)
            # A file is "dead" only when EVERY entry's transcript is gone.
            # Entries without a transcript_path (future CLIs storing only
            # ids) are treated as live — we can't verify, so we don't
            # delete.  Empty / unparsable files also go (no useful state).
            should_delete = not entries
            if entries and not should_delete:
                pass  # unreachable, kept for clarity
            if entries:
                any_live = False
                for entry in entries:
                    tp = entry.get('transcript_path', '') or ''
                    if not tp:
                        any_live = True
                        break
                    try:
                        os.stat(tp)
                        any_live = True
                        break
                    except FileNotFoundError:
                        continue  # transcript definitely gone
                    except OSError:
                        # Some other stat error (permission, I/O, network
                        # filesystem hiccup).  Treat as live to avoid a
                        # false-positive delete we can't undo.
                        any_live = True
                        break
                should_delete = not any_live
            if should_delete:
                try:
                    # Re-stat: if the file was modified while we were
                    # deciding, a concurrent writer beat us to it — skip.
                    if tag_file.stat().st_mtime != mtime_before:
                        continue
                    tag_file.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def _dedup_sessions_across_tags(rows: list[TagRow]) -> list[TagRow]:
    """For each ``(cli, session_id)`` keep the session only on the tag
    with the newest ``last_seen``.

    Forked resume (``leap --resume`` → tag busy → user picks new tag ``9b``)
    lands the same CLI session UUID in both the old tag's file and the new
    one's.  Both are "correct" — the UUID genuinely lived under both Leap
    tags — but the picker would otherwise show the user two rows pointing
    at the same conversation.  We keep only the freshest touch; older
    rows drop the duplicated session.  A row that loses *every* session
    this way is dropped entirely.
    """
    best: dict[tuple[str, str], tuple[float, str]] = {}
    for row in rows:
        for s in row.sessions:
            key = (row.cli, s.session_id)
            prev = best.get(key)
            if prev is None or s.last_seen > prev[0]:
                best[key] = (s.last_seen, row.tag)
    deduped: list[TagRow] = []
    for row in rows:
        kept = [
            s for s in row.sessions
            if best.get((row.cli, s.session_id), (0.0, row.tag))[1] == row.tag
        ]
        if not kept:
            continue
        deduped.append(TagRow(
            tag=row.tag,
            cli=row.cli,
            sessions=kept,
            last_seen=kept[0].last_seen,
        ))
    return deduped


def load_raw_tag_rows(storage_dir: Path) -> list[TagRow]:
    """Return one :class:`TagRow` per ``(cli, tag)`` pair with live sessions,
    WITHOUT dedup across tags.

    Scans every ``cli_sessions/<cli>/*.json`` so custom CLIs appear
    alongside the built-in providers.  Rows are sorted newest-first by
    the freshest session's ``last_seen``.

    Ownership checks that need to know which session each live tag is
    *actually* running (``_live_session_owners``) must read raw rows —
    :func:`load_tag_rows`'s cross-tag dedup is a display concern only,
    and dropping the older tag's copy of a session would hide a
    legitimately-live second owner in the pathological (though
    physically rare) case where two Leap tags are resuming the same
    CLI session concurrently.
    """
    root = _sessions_root(storage_dir)
    if not root.is_dir():
        return []
    rows: list[TagRow] = []
    for cli_dir in root.iterdir():
        if not cli_dir.is_dir():
            continue
        cli = cli_dir.name
        # Matches write-side: skip anything that doesn't look like a
        # legitimate provider name so stray directories (e.g. created
        # by manual tampering or a prior path-traversal attempt) don't
        # surface in the picker.
        if not _is_safe_id(cli):
            continue
        for path in cli_dir.glob("*.json"):
            tag = path.stem
            if not _is_safe_id(tag):
                continue
            sessions = _resumable_sessions(_load_raw_entries(path))
            if not sessions:
                continue
            rows.append(TagRow(
                tag=tag,
                cli=cli,
                sessions=sessions,
                last_seen=sessions[0].last_seen,
            ))
    rows.sort(key=lambda r: r.last_seen, reverse=True)
    return rows


def load_tag_rows(storage_dir: Path) -> list[TagRow]:
    """Return dedup'd rows for the picker display.

    Same as :func:`load_raw_tag_rows` plus ``(cli, session_id)``
    cross-tag dedup: a forked-tag resume (``leap --resume`` under a
    new Leap tag while the original is busy) leaves the same UUID in
    both files, and the picker should show the conversation only once
    (on its newest tag).
    """
    rows = _dedup_sessions_across_tags(load_raw_tag_rows(storage_dir))
    rows.sort(key=lambda r: r.last_seen, reverse=True)
    return rows
