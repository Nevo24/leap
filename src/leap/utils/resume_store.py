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
import time
from dataclasses import dataclass, field
from pathlib import Path


# Cap per (cli, tag) file; oldest-first trimming keeps this bounded.
MAX_ENTRIES_PER_TAG: int = 20


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
    tag_file = _tag_file(storage_dir, cli, tag)
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
        out.append(SessionRecord(
            session_id=sid,
            transcript_path=tp,
            cwd=entry.get("cwd", "") or "",
            last_seen=float(entry.get("last_seen") or 0),
            size=size,
        ))
    return out


def load_tag_rows(storage_dir: Path) -> list[TagRow]:
    """Return one :class:`TagRow` per ``(cli, tag)`` pair with live sessions.

    Scans every ``cli_sessions/<cli>/*.json`` so custom CLIs appear
    alongside the built-in providers.  Rows are sorted newest-first by
    the freshest session's ``last_seen``.
    """
    root = _sessions_root(storage_dir)
    if not root.is_dir():
        return []
    rows: list[TagRow] = []
    for cli_dir in root.iterdir():
        if not cli_dir.is_dir():
            continue
        cli = cli_dir.name
        for path in cli_dir.glob("*.json"):
            sessions = _resumable_sessions(_load_raw_entries(path))
            if not sessions:
                continue
            rows.append(TagRow(
                tag=path.stem,
                cli=cli,
                sessions=sessions,
                last_seen=sessions[0].last_seen,
            ))
    rows.sort(key=lambda r: r.last_seen, reverse=True)
    return rows
