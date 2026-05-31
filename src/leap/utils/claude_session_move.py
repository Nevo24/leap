"""Safely relocate a Claude Code session's on-disk state across cwds.

Claude Code stores each session under
``~/.claude/projects/<cwd-slug>/<uuid>.jsonl`` plus an *optional*
sidecar directory at ``~/.claude/projects/<cwd-slug>/<uuid>/`` that
holds sub-agent transcripts and overflow tool outputs.  Resuming a
session requires Claude to be invoked with ``cwd == src_cwd`` so it
can find the transcript by slug.

This module relocates the transcript (and sidecar, if present) so the
same session can be resumed under a different cwd::

  src = ~/.claude/projects/<slug(src_cwd)>/<uuid>.jsonl  [+ <uuid>/]
  dst = ~/.claude/projects/<slug(dst_cwd)>/<uuid>.jsonl  [+ <uuid>/]

The atomic-move primitives (signal blocking, copy/verify/rename) live
in :mod:`leap.utils.relocation`; this module is the Claude-specific
orchestrator.  ``RelocationError`` is re-exported here for callers
that historically imported it from this module.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Optional

from leap.utils.relocation import (
    RelocationError,
    best_effort_remove,
    commit_file,
    commit_tree,
    is_safe_session_id,
    make_tmp_path,
    must_remove_tree,
    signals_blocked,
    snapshot_tree,
    stage_copy_file,
    stage_copy_tree,
    stat_snapshot,
)


__all__ = ['CLAUDE_PROJECTS_ROOT', 'RelocationError', 'relocate_claude_session', 'slugify']


CLAUDE_PROJECTS_ROOT: Path = Path.home() / ".claude" / "projects"

# Claude's per-cwd directory slug: every char *not* in ``[a-zA-Z0-9-]``
# is replaced with ``-``, with no collapsing of consecutive dashes.
# Crucially, underscores ARE replaced (``_`` is *not* preserved) — an
# earlier version of this regex kept ``_`` and so computed the wrong
# slug for any cwd containing one, silently breaking every transcript
# lookup (transcript_says_running / _interrupted) and the resume/move
# pre-flight for those paths.  Empirically derived by matching computed
# slugs against the real ``~/.claude/projects/`` directory names across
# 40 recorded sessions:
#   ``mwb-manifests_3``  → ``mwb-manifests-3``   (underscore → dash)
#   ``ai-workflows_1``   → ``ai-workflows-1``
#   ``Nevo.Mashiach``    → ``Nevo-Mashiach``     (dot → dash)
#   ``/Users/me/.claude``→ ``-Users-me--claude`` (slash + dot → TWO
#                                                  dashes, NOT collapsed)
# The pre-flight check in :func:`relocate_claude_session` validates that
# our computed slug for ``src_cwd`` matches the actual on-disk directory
# name — if Claude changes its encoding in a future version, the move
# aborts before touching any files.
_SLUG_REPLACE_RE: re.Pattern[str] = re.compile(r'[^a-zA-Z0-9-]')


def slugify(path: str) -> str:
    """Return Claude Code's per-cwd directory slug for ``path``."""
    return _SLUG_REPLACE_RE.sub('-', path)


def relocate_claude_session(
    session_id: str,
    src_cwd: str,
    dst_cwd: str,
    *,
    on_committed: Optional[Callable[[str], None]] = None,
) -> str:
    """Move a Claude session's on-disk state from ``src_cwd`` to ``dst_cwd``.

    Args:
        session_id: The Claude session UUID (basename of the JSONL,
            without the ``.jsonl`` suffix).
        src_cwd: The directory the session was originally recorded in.
        dst_cwd: The directory we want Claude to find the session in
            on its next ``--resume=<id>`` launch.
        on_committed: Optional callback invoked *after* the destination
            is fully verified in-place but *before* the source is
            deleted.  Receives the new transcript path.  Called inside
            the signal-blocked critical section so any bookkeeping the
            caller does there is also atomic w.r.t. user interruption.
            If the callback raises, the source is *not* deleted (state
            stays recoverable) and the exception is re-raised wrapped
            in ``RelocationError``.

    Returns:
        Absolute path to the new transcript file on disk.

    Raises:
        RelocationError: on any failure.  The source is always intact;
            any half-written destination temp files are cleaned up.
    """
    if not session_id:
        raise RelocationError("missing session id")
    if not is_safe_session_id(session_id):
        # Defense-in-depth: a crafted session id with `/` would let us
        # escape ``CLAUDE_PROJECTS_ROOT/<slug>/`` once we join.  Real
        # Claude UUIDs always match this pattern.
        raise RelocationError(
            f"refusing to relocate: session id has unexpected format: "
            f"{session_id!r}"
        )
    if not src_cwd:
        raise RelocationError("missing source cwd")
    if not dst_cwd:
        raise RelocationError("missing destination cwd")

    src_slug = slugify(src_cwd)
    dst_slug = slugify(dst_cwd)

    src_proj = CLAUDE_PROJECTS_ROOT / src_slug
    dst_proj = CLAUDE_PROJECTS_ROOT / dst_slug

    src_jsonl = src_proj / f"{session_id}.jsonl"
    dst_jsonl = dst_proj / f"{session_id}.jsonl"
    src_dir = src_proj / session_id
    dst_dir = dst_proj / session_id

    # ---- Pre-flight (no mutations) ------------------------------------

    # Slug-encoding sanity check: Claude must already have this dir on
    # disk because we know a transcript lives there.  If our computed
    # slug for src_cwd doesn't match Claude's actual directory, our
    # encoding has drifted and we must abort *before* writing anything.
    if not src_proj.is_dir():
        raise RelocationError(
            f"source project directory not found: {src_proj}\n"
            f"  Computed slug for {src_cwd!r} doesn't match any directory "
            f"under {CLAUDE_PROJECTS_ROOT}.  Claude's path encoding may "
            f"have changed; refusing to relocate."
        )
    if not src_jsonl.is_file():
        raise RelocationError(
            f"source transcript not found: {src_jsonl}"
        )

    # Slug collision: different cwds can produce the same slug
    # (e.g. ``/a/b`` and ``/a-b`` both become ``-a-b``).  In that case
    # the move is a no-op — the file is already at the right path —
    # and we report success so the caller skips the chdir.
    if src_slug == dst_slug:
        return str(src_jsonl)

    # Case-insensitive filesystem defense (macOS APFS default): two
    # slugs that differ only in case (or by some other character that
    # the filesystem folds away) can resolve to the same on-disk inode.
    # If we proceeded, ``os.replace(jsonl_tmp, dst_jsonl)`` would
    # silently overwrite the source's bytes and the Phase 3 ``unlink``
    # would then delete the *only* copy on disk — actual data loss.
    # ``samefile`` catches this before we touch anything.
    try:
        if dst_proj.exists() and src_proj.samefile(dst_proj):
            return str(src_jsonl)
    except OSError:
        # Permission or stat failure here is rare; fall through and let
        # the explicit checks below surface a cleaner error.
        pass

    # Collision check on the destination — refuse to overwrite.
    if dst_jsonl.exists():
        raise RelocationError(
            f"destination transcript already exists: {dst_jsonl}\n"
            f"  Refusing to overwrite.  Move or delete it manually if you "
            f"want to proceed."
        )
    has_sidecar = src_dir.is_dir()
    if has_sidecar and dst_dir.exists():
        raise RelocationError(
            f"destination sidecar directory already exists: {dst_dir}\n"
            f"  Refusing to overwrite.  Move or delete it manually if you "
            f"want to proceed."
        )

    # Make sure the destination project dir is in place — also catches
    # permission errors before we start the expensive copy.
    try:
        dst_proj.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RelocationError(
            f"could not create destination project directory {dst_proj}: {e}"
        )

    # Per-process tmp paths so two concurrent ``leap --resume`` runs on
    # the same session never share a tmp file.
    jsonl_tmp = make_tmp_path(dst_jsonl)
    dir_tmp = make_tmp_path(dst_dir)
    # If a previous, crashed run with the *same* PID (PIDs do recycle)
    # left these around, clear them now so ``copytree`` doesn't trip
    # on an existing directory.
    best_effort_remove(jsonl_tmp)
    best_effort_remove(dir_tmp)

    # ---- Critical section --------------------------------------------
    # Snapshot of src_jsonl's stat captured at the moment we know its
    # bytes match our copy — used right before unlink to detect a
    # rogue writer (a Claude running directly, outside leap, with the
    # same session id) that may have appended after our verify.  We
    # would otherwise unlink src and lose those new bytes.
    src_stat_after_verify: Optional[tuple[int, int]] = None
    src_dir_snapshot: Optional[dict[Path, tuple[int, int]]] = None

    with signals_blocked():
        # Phase 1: copy + fsync + verify (source still untouched).
        stage_copy_file(src_jsonl, jsonl_tmp)
        # Capture src stat right after verify — this is the known
        # last moment at which src's bytes equal our committed
        # snapshot.  Any later mtime/size change means a rogue
        # writer modified src and we must NOT unlink.
        src_stat_after_verify = stat_snapshot(src_jsonl)

        if has_sidecar:
            stage_copy_tree(src_dir, dir_tmp)
            # Capture the sidecar tree snapshot right after verify.
            src_dir_snapshot = snapshot_tree(src_dir)

        # Phase 2: atomic commit.
        commit_file(jsonl_tmp, dst_jsonl)
        if has_sidecar:
            try:
                commit_tree(dir_tmp, dst_dir)
            except RelocationError:
                # Roll back the JSONL placement so the caller sees a
                # clean "nothing happened" state.  Source is still the
                # only valid copy.
                best_effort_remove(dst_jsonl)
                raise

        # Caller bookkeeping happens here — *before* we delete the
        # source — so a crash mid-callback leaves a recoverable
        # duplicate state rather than orphaning the new files.
        if on_committed is not None:
            try:
                on_committed(str(dst_jsonl))
            except Exception as e:
                raise RelocationError(
                    f"destination committed but bookkeeping callback "
                    f"failed: {e}\n  Source files left intact at {src_jsonl}"
                    + (f" and {src_dir}" if has_sidecar else "") + "."
                ) from e

        # Phase 3: delete source.  Re-stat first to detect any rogue
        # writer that touched src after our verify.
        try:
            cur_src_stat = stat_snapshot(src_jsonl)
        except OSError as e:
            raise RelocationError(
                f"source transcript disappeared during the move: {src_jsonl}: {e}\n"
                f"  Destination at {dst_jsonl} is intact."
            ) from e
        if src_stat_after_verify is None or cur_src_stat != src_stat_after_verify:
            raise RelocationError(
                f"source transcript was modified after our copy was committed; "
                f"refusing to delete it to avoid losing those changes.\n"
                f"  Destination at {dst_jsonl} contains the verified snapshot.\n"
                f"  Source at {src_jsonl} still has whatever was appended.\n"
                f"  If a Claude session is currently running in {src_cwd}, "
                f"exit it before relocating."
            )
        try:
            src_jsonl.unlink()
        except OSError as e:
            raise RelocationError(
                f"destination committed but source transcript could not be "
                f"deleted: {src_jsonl}: {e}\n"
                f"  Both copies now exist; delete the source manually."
            ) from e

        if has_sidecar:
            # Symmetric to the src_jsonl stat re-check above: detect a
            # rogue writer that touched any sub-agent file (or added a
            # new one) after we copied the sidecar.
            cur_dir_snapshot = snapshot_tree(src_dir)
            if cur_dir_snapshot != src_dir_snapshot:
                added = sorted(set(cur_dir_snapshot) - set(src_dir_snapshot or {}))
                modified = sorted(
                    rel for rel, st in cur_dir_snapshot.items()
                    if (src_dir_snapshot or {}).get(rel) not in (None, st)
                    and rel not in added
                )
                raise RelocationError(
                    f"source sidecar dir was modified after our copy was "
                    f"committed; refusing to delete it to avoid losing "
                    f"those changes.\n"
                    f"  Destination at {dst_dir} contains the verified snapshot.\n"
                    f"  Source at {src_dir} has new/changed files: "
                    f"added={added[:5]} modified={modified[:5]}.\n"
                    f"  If a Claude session is currently running in {src_cwd}, "
                    f"exit it before relocating."
                )
            must_remove_tree(src_dir, dst_for_message=dst_dir)

        return str(dst_jsonl)
