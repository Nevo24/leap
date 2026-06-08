"""Filesystem persistence helpers for the Notes dialog.

Notes live as ``.txt`` files under ``NOTES_DIR``; per-note metadata
(mode, created_at, ordering) lives in a single ``.notes_meta.json`` at
the root of that directory.

These helpers are deliberately stateless and module-level so tests can
monkey-patch ``NOTES_DIR`` / ``_NOTES_META_FILE`` to redirect FS access
into a tmp dir — the same pattern used by ``notes_undo``'s tests.

This module is the **canonical** home for note metadata IO.  Earlier
revisions of the codebase had byte-identical duplicate copies of these
helpers in ``notes_undo.py``; ``notes_undo.py`` now imports from here.
"""

import json
import time
from datetime import datetime
from pathlib import Path

from leap.utils.atomic_write import atomic_write_json
from leap.utils.constants import NOTES_DIR


_NOTES_META_FILE: Path = NOTES_DIR / '.notes_meta.json'


# ── Note paths / listing ────────────────────────────────────────────

def _note_path(name: str) -> Path:
    """Return the .txt path for a note name."""
    return NOTES_DIR / f'{name}.txt'


def _migrate_old_notes_file() -> None:
    """One-time migration: move .storage/notes.txt → .storage/notes/Notes.txt."""
    old_file = NOTES_DIR.parent / 'notes.txt'
    if old_file.exists() and old_file.is_file():
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        dest = NOTES_DIR / 'Notes.txt'
        if not dest.exists():
            try:
                old_file.rename(dest)
            except OSError:
                pass


def _list_notes() -> list[str]:
    """Return note names (relative paths without .txt) sorted by mtime desc."""
    _migrate_old_notes_file()
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    files = [p for p in NOTES_DIR.rglob('*.txt') if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p.relative_to(NOTES_DIR).with_suffix('')) for p in files]


# ── Mtime / created_at formatting ───────────────────────────────────

def _format_mtime(path: Path) -> str:
    """Return the file's mtime as a human-readable string (minute precision)."""
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M')
    except OSError:
        return ''


def _folder_mtime(folder_path: str) -> str:
    """Return the most recent note mtime under folder_path as a formatted string."""
    try:
        folder = NOTES_DIR / folder_path
        mtimes = [p.stat().st_mtime for p in folder.rglob('*.txt') if p.is_file()]
        if not mtimes:
            return ''
        return datetime.fromtimestamp(int(max(mtimes))).strftime('%Y-%m-%d %H:%M')
    except OSError:
        return ''


def _get_note_created_at(name: str) -> str:
    """Return the note's creation date as a formatted string, or '' if unknown."""
    ts = _load_notes_meta().get(name, {}).get('created_at')
    if ts is None:
        return ''
    try:
        return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M')
    except (OSError, TypeError, ValueError, OverflowError):
        return ''


# ── Note metadata ───────────────────────────────────────────────────

def _load_notes_meta() -> dict:
    try:
        if _NOTES_META_FILE.exists():
            return json.loads(_NOTES_META_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_notes_meta(meta: dict) -> None:
    # ``atomic_write_json`` (tmp + fsync + atomic rename) instead of a
    # bare ``write_text`` — a crash mid-write would otherwise truncate
    # ``.notes_meta.json`` and the load path's ``except
    # json.JSONDecodeError: return {}`` would silently wipe the user's
    # folder structure, pinned state, mode flags, and created_at
    # timestamps with no visible error.
    try:
        atomic_write_json(_NOTES_META_FILE, meta)
    except OSError:
        pass


def _set_note_created_at(name: str) -> None:
    """Stamp the note's created_at timestamp (now) in metadata."""
    meta = _load_notes_meta()
    meta.setdefault(name, {})['created_at'] = int(time.time())
    _save_notes_meta(meta)


def _get_note_mode(name: str) -> str:
    """Return 'text' or 'checklist' for a note."""
    return _load_notes_meta().get(name, {}).get('mode', 'text')


def _set_note_mode(name: str, mode: str) -> None:
    meta = _load_notes_meta()
    meta.setdefault(name, {})['mode'] = mode
    _save_notes_meta(meta)


def _get_note_add_draft(name: str) -> str:
    """Return the unsubmitted "Add item" text saved for a checklist note.

    Stored in on-disk (``![image](hash.png)`` marker) form, same as item
    bodies; '' when the note has no pending draft.
    """
    return _load_notes_meta().get(name, {}).get('add_draft', '') or ''


def _set_note_add_draft(name: str, text: str) -> None:
    """Persist (or clear) a checklist note's unsubmitted "Add item" text.

    Kept out of the ``.txt`` body so it never turns into a real item;
    an empty draft drops the key rather than storing ''.
    """
    meta = _load_notes_meta()
    entry = meta.get(name)
    if text:
        if entry is None:
            entry = {}
            meta[name] = entry
        entry['add_draft'] = text
    else:
        if entry is None or 'add_draft' not in entry:
            return  # nothing stored — avoid a needless rewrite
        entry.pop('add_draft', None)
    _save_notes_meta(meta)


def _remove_note_meta(name: str) -> None:
    meta = _load_notes_meta()
    if meta.pop(name, None) is not None:
        _save_notes_meta(meta)


def _rename_note_meta(old_name: str, new_name: str) -> None:
    meta = _load_notes_meta()
    if old_name in meta:
        meta[new_name] = meta.pop(old_name)
        _save_notes_meta(meta)
