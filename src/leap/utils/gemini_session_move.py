"""Safely relocate a Gemini CLI session's on-disk state across cwds.

Gemini CLI stores sessions per-project under::

  ~/.gemini/tmp/<slug>/chats/session-<ts>-<short>.jsonl

with a registry at ``~/.gemini/projects.json`` mapping ``cwd → slug``.
The slug is derived from ``basename(cwd)`` (lowercased, non-alphanumeric
replaced by ``-``, consecutive dashes collapsed, leading/trailing dashes
stripped) plus a disambiguation suffix (``-1``, ``-2``, …) if the
default slug is already taken by another project.

To resume a session originally recorded in cwd A from cwd B, Gemini
running in B would compute or look up B's slug, look in
``tmp/<slug_B>/chats/``, and find nothing — Gemini doesn't search
across slugs.  This module physically moves the session's JSONL from
A's slug to B's slug so the resume actually works.

Atomic-move primitives (signal blocking, copy/verify/rename) live in
:mod:`leap.utils.relocation`; this module is the Gemini-specific
orchestrator (slug resolution, file lookup by ``sessionId``,
projects.json registry update).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from leap.utils.atomic_write import atomic_write_json
from leap.utils.relocation import (
    RelocationError,
    best_effort_remove,
    commit_file,
    is_safe_session_id,
    make_tmp_path,
    signals_blocked,
    stage_copy_file,
)


__all__ = ['relocate_gemini_session', 'resume_cwd_for_session', 'slugify']


GEMINI_HOME: Path = Path.home() / ".gemini"
GEMINI_PROJECTS_REGISTRY: Path = GEMINI_HOME / "projects.json"
GEMINI_TMP_ROOT: Path = GEMINI_HOME / "tmp"
GEMINI_HISTORY_ROOT: Path = GEMINI_HOME / "history"

# Slug character class — matches Gemini's ``slugify`` exactly:
# ``replace(/[^a-z0-9]/g, '-')``.  Note: input is already
# lower-cased, so we don't allow uppercase here.
_SLUG_REPLACE_RE: re.Pattern[str] = re.compile(r'[^a-z0-9]')
# Used to collapse consecutive dashes.
_SLUG_COLLAPSE_RE: re.Pattern[str] = re.compile(r'-+')


def slugify(text: str) -> str:
    """Mirror Gemini's slug algorithm.

    Lowercase → replace ``[^a-z0-9]`` with ``-`` → collapse
    consecutive ``-`` → strip leading/trailing ``-`` → fall back to
    ``'project'`` if the result is empty.
    """
    s = _SLUG_REPLACE_RE.sub('-', text.lower())
    s = _SLUG_COLLAPSE_RE.sub('-', s)
    s = s.strip('-')
    return s or 'project'


def relocate_gemini_session(
    session_id: str,
    src_cwd: str,
    dst_cwd: str,
    *,
    on_committed: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Move a Gemini session's state from ``src_cwd``'s slug to ``dst_cwd``'s.

    Returns the new transcript path on success, ``None`` when:

    * ``session_id`` fails the safety regex,
    * ``~/.gemini/projects.json`` doesn't exist or lacks ``src_cwd``,
    * the session file isn't found at the expected slug.

    The caller treats ``None`` as "couldn't relocate, fall through to
    chdir-to-recorded-cwd" — same contract as Claude/Codex.

    Raises :class:`RelocationError` on disk-side failures (copy,
    verify, rename, registry write).  Source intact in those cases.
    """
    if not is_safe_session_id(session_id):
        return None
    if not src_cwd or not dst_cwd:
        return None
    if not GEMINI_HOME.is_dir():
        return None

    # ---- Locate src side ----------------------------------------------
    registry = _load_projects_registry()
    src_slug = registry.get(src_cwd)
    if not src_slug:
        # Nothing recorded for this cwd — either the session was
        # recorded under a legacy hash dir that hasn't been migrated
        # yet, or it never existed.  Either way, we don't have enough
        # info to find it.
        return None
    src_chats_dir = GEMINI_TMP_ROOT / src_slug / "chats"
    src_file = _find_session_file(src_chats_dir, session_id)
    if src_file is None:
        return None

    # ---- Resolve dst slug ---------------------------------------------
    # If the destination cwd is already registered, reuse its slug —
    # other sessions may already live there.  Otherwise claim a fresh
    # one (basename slugified + ``-N`` disambiguation).
    dst_slug = registry.get(dst_cwd)
    if not dst_slug:
        dst_slug = _claim_new_slug(dst_cwd, registry)

    # Same-slug edge case: dst already registered to the same slug as
    # src (e.g. user toggled cwd back-and-forth).  Nothing to move.
    if src_slug == dst_slug:
        # Make sure the registry has the dst_cwd mapping (it should
        # already; cosmetic bookkeeping otherwise) and return the path.
        if registry.get(dst_cwd) != dst_slug:
            registry[dst_cwd] = dst_slug
            _save_projects_registry(registry)
        if on_committed is not None:
            on_committed(str(src_file))
        return str(src_file)

    dst_chats_dir = GEMINI_TMP_ROOT / dst_slug / "chats"
    dst_file = dst_chats_dir / src_file.name

    # Refuse to overwrite an existing destination file.
    if dst_file.exists():
        raise RelocationError(
            f"destination session file already exists: {dst_file}\n"
            f"  Refusing to overwrite.  Move or delete it manually if "
            f"you want to proceed."
        )

    # Make sure the destination chats dir exists.  Gemini creates
    # ``tmp/<slug>/`` lazily on first session, so we replicate that
    # behaviour for the brand-new-slug case.
    try:
        dst_chats_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RelocationError(
            f"could not create destination dir {dst_chats_dir}: {e}"
        ) from e

    tmp = make_tmp_path(dst_file)
    best_effort_remove(tmp)

    with signals_blocked():
        # Phase 1: copy + fsync + verify (source still untouched).
        stage_copy_file(src_file, tmp)

        # Phase 2: atomic commit of the file.
        commit_file(tmp, dst_file)

        # Phase 3: register dst_cwd in projects.json (we don't remove
        # src_cwd's mapping — other sessions may still live there).
        # Atomic via tmp + os.replace inside the helper.
        try:
            registry[dst_cwd] = dst_slug
            _save_projects_registry(registry)
        except OSError as e:
            # Registry write failed but the file is at the new slug.
            # Roll back the file to keep state consistent: gemini in
            # dst_cwd would otherwise claim a *different* fresh slug
            # next time it starts and the session would be invisible.
            best_effort_remove(dst_file)
            raise RelocationError(
                f"failed to update projects.json: {e}\n"
                f"  Rolled back the destination commit; source intact."
            ) from e

        # Caller bookkeeping — runs after the file + registry are
        # durable but before src is unlinked, so a crash mid-callback
        # leaves a recoverable duplicate state.
        if on_committed is not None:
            try:
                on_committed(str(dst_file))
            except Exception as e:
                raise RelocationError(
                    f"destination committed but bookkeeping callback "
                    f"failed: {e}\n  Source file left intact at "
                    f"{src_file}.  Both copies now exist on disk."
                ) from e

        # Phase 4: delete source.
        try:
            src_file.unlink()
        except OSError as e:
            raise RelocationError(
                f"destination committed but source could not be "
                f"deleted: {src_file}: {e}\n"
                f"  Both copies now exist; delete the source manually."
            ) from e

        return str(dst_file)


# ---- Internals -------------------------------------------------------


def _load_projects_registry() -> dict[str, str]:
    """Read ``~/.gemini/projects.json`` and return ``{cwd: slug}``.

    Gemini uses the JSON shape ``{"projects": {cwd: slug, ...}}``.
    Tolerates a missing file or an empty/malformed registry by
    returning ``{}`` — the caller's "src_slug not found" path then
    cleanly falls through to ``return None``.
    """
    if not GEMINI_PROJECTS_REGISTRY.is_file():
        return {}
    try:
        data = json.loads(GEMINI_PROJECTS_REGISTRY.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    projects = data.get('projects') if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return {}
    out: dict[str, str] = {}
    for cwd, slug in projects.items():
        if isinstance(cwd, str) and isinstance(slug, str):
            out[cwd] = slug
    return out


def resume_cwd_for_session(transcript_path: str, cwd: str) -> str:
    """Return the cwd ``gemini --resume`` needs, healing mid-session ``cd`` drift.

    Gemini stores a session at ``tmp/<slug>/chats/session-*.jsonl`` where
    ``<slug>`` is the registry slug of the cwd the session was *started* in.
    ``record_session`` keeps overwriting the recorded cwd with whatever cwd a
    hook last saw, so a session that ``cd``'d mid-run ends up with a cwd whose
    registry slug no longer maps to its session dir — and then resume can't
    find it from *any* directory (same failure class as Claude's slug drift).

    The session dir's slug is authoritative.  If the given cwd's registry slug
    already matches it, return it unchanged (the common case).  Otherwise
    reverse-lookup the registry for the cwd whose slug matches; accept it only
    when the match is unique (Gemini's ``-N`` dedup keeps slugs 1:1 with cwds),
    else fall back to the given cwd — never invent a path.
    """
    if (not transcript_path
            or '/.gemini/tmp/' not in transcript_path
            or '/chats/' not in transcript_path):
        return cwd
    actual_slug = Path(transcript_path).parent.parent.name
    if not actual_slug:
        return cwd
    registry = _load_projects_registry()
    if cwd and registry.get(cwd) == actual_slug:
        return cwd
    matches = [k for k, slug in registry.items() if slug == actual_slug]
    if len(matches) == 1:
        return matches[0]
    return cwd


def _save_projects_registry(projects: dict[str, str]) -> None:
    """Atomically rewrite ``~/.gemini/projects.json`` with ``projects``.

    Wrapped in the same JSON shape Gemini itself writes.
    ``atomic_write_json`` gives us a unique tmp filename and an explicit
    ``fsync`` before rename — without the fsync a crash between
    ``write`` and ``rename`` could lose the registry update and leave a
    moved JSONL orphaned (file at new slug, registry still pointing at
    the old one).  Without the unique tmp name two concurrent relocates
    sharing this module would clobber each other's tmp.
    """
    payload = {'projects': projects}
    atomic_write_json(GEMINI_PROJECTS_REGISTRY, payload)


def _claim_new_slug(cwd: str, registry: dict[str, str]) -> str:
    """Pick a fresh slug for *cwd*, mirroring Gemini's collision logic.

    Starts from ``slugify(basename(cwd))``; if that's already taken
    (either as a value in ``registry`` or as an existing dir under
    ``tmp/`` / ``history/``), append ``-1``, ``-2``, … until a free
    one is found.  Doesn't update the registry — the caller does that
    once after the file move so the registry never points at an
    empty slug.
    """
    base_name = os.path.basename(cwd) or 'project'
    slug = slugify(base_name)
    taken: set[str] = set(registry.values())
    counter = 0
    while True:
        candidate = slug if counter == 0 else f"{slug}-{counter}"
        counter += 1
        if candidate in taken:
            continue
        if (GEMINI_TMP_ROOT / candidate).exists():
            continue
        if (GEMINI_HISTORY_ROOT / candidate).exists():
            continue
        return candidate


def _find_session_file(chats_dir: Path, session_id: str) -> Optional[Path]:
    """Return the JSONL whose first-line ``sessionId`` matches.

    Gemini's session filename embeds only the first 8 chars of the
    UUID (``session-<ts>-<short>.jsonl``), so a strict-prefix glob
    would risk false matches.  We open each candidate and parse the
    first line, which Gemini reliably writes as a ``session_meta``-
    style record with the full ``sessionId``.
    """
    if not chats_dir.is_dir():
        return None
    short = session_id[:8].lower()
    candidates = sorted(
        chats_dir.glob(f'session-*-{short}.jsonl'),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        if _file_session_id_matches(path, session_id):
            return path
    # Fallback: glob with the short was empty (filename pattern may
    # have shifted in a future Gemini version).  Walk every .jsonl
    # and match by first-line sessionId.
    for path in chats_dir.glob('*.jsonl'):
        if _file_session_id_matches(path, session_id):
            return path
    return None


def _file_session_id_matches(path: Path, session_id: str) -> bool:
    """True iff ``path``'s first JSONL line has ``sessionId == session_id``."""
    try:
        with open(path, 'r') as f:
            first = f.readline()
    except OSError:
        return False
    if not first.strip():
        return False
    try:
        entry = json.loads(first)
    except (json.JSONDecodeError, ValueError):
        return False
    return entry.get('sessionId') == session_id
