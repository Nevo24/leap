"""Read-only scanner for Cursor (the editor) Agent/Composer tabs.

Cursor's AI Agent tabs live entirely inside its Electron app — there is
no PTY, socket, or public API for them, and Cursor exposes *nothing*
clickable to macOS Accessibility (its window collapses to empty
``AXGroup``s; ``AXManualAccessibility`` is unsupported).  So Leap can't
drive or focus an individual Agent tab.

What it *can* do is read each tab's state straight off disk.  Cursor
persists composer state in two SQLite stores:

* ``~/Library/Application Support/Cursor/User/workspaceStorage/<hash>/``
  - ``workspace.json`` → ``{"folder": "file:///.../<project>"}`` (maps a
    window to its project folder).
  - ``state.vscdb`` → ``ItemTable['composer.composerData']`` JSON whose
    ``selectedComposerIds`` are the Agent tabs **open in that window**.
* global ``~/Library/Application Support/Cursor/User/globalStorage/state.vscdb``
  → ``cursorDiskKV['composerData:<id>']`` JSON per tab, carrying
  ``name`` (the tab title; ``None`` for a fresh "New Agent"),
  ``status`` (``none`` / ``aborted`` / …), ``generatingBubbleIds`` and
  ``hasUnreadMessages``.

:func:`scan_open_cursor_agents` joins these to produce one synthetic row
dict per *currently open* Agent tab.  Which workspaces are open is
determined from the ``state.vscdb`` file handles Cursor holds open
(``lsof``) - a frontmost-independent signal, unlike System Events window
enumeration (which only works while Cursor is the active app).  The rows
are purely for display + a window-level "jump"; they are never real Leap
sessions and carry ``row_type == 'cursor_agent_gui'`` so the monitor
keeps them out of every server-centric code path.

The on-disk schema is undocumented and version-fragile, so every read is
defensive: any unexpected shape skips that row rather than raising.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from leap.utils.ide_detection import get_git_branch

logger = logging.getLogger(__name__)

__all__ = [
    'CURSOR_GUI_ROW_TYPE',
    'CURSOR_GUI_TAG_PREFIX',
    'scan_open_cursor_agents',
]

# Row discriminator + tag namespace.  The tag is synthetic (a composer
# UUID) so it can never collide with a real Leap tag.
CURSOR_GUI_ROW_TYPE: str = 'cursor_agent_gui'
CURSOR_GUI_TAG_PREFIX: str = 'cursor-gui:'

# On-disk locations.  Module-level so tests can repoint them at fixtures.
CURSOR_USER_DIR: Path = (
    Path.home() / "Library" / "Application Support" / "Cursor" / "User"
)
WORKSPACE_STORAGE: Path = CURSOR_USER_DIR / "workspaceStorage"
GLOBAL_DB: Path = CURSOR_USER_DIR / "globalStorage" / "state.vscdb"

# Signature-keyed caches so we only re-parse SQLite when Cursor actually
# wrote (avoids per-refresh work on the ~1 MB global db while idle).
_GLOBAL_CACHE: dict[str, Any] = {'sig': None, 'data': {}}
_WS_CACHE: dict[str, dict[str, Any]] = {}
# Per-folder git-branch cache (TTL'd - the scan runs every ~1s, but a
# folder's branch changes rarely, so avoid a git subprocess per tick).
_BRANCH_CACHE: dict[str, tuple[float, str]] = {}
_BRANCH_TTL: float = 15.0
# Per-composer last-message cache, keyed by the global db signature so it
# only re-reads the message bubble when Cursor actually wrote.
_LASTMSG_CACHE: dict[str, tuple[Any, str]] = {}
# Cap the last-message preview so a huge prompt can't bloat the cell/tooltip.
_LASTMSG_MAX: int = 200
# Which workspaces are OPEN is detected from the ``state.vscdb`` file
# handles Cursor holds open (via lsof).  This is frontmost-independent -
# unlike System Events window enumeration, which returns 0 windows when
# Cursor isn't the active app (so a window-title approach would make the
# rows vanish whenever you look away from Cursor).  Cached briefly so we
# don't run pgrep+lsof on every ~1s scan tick.
_OPENWS_CACHE: dict[str, Any] = {'mono': 0.0, 'hashes': set()}
_OPENWS_TTL: float = 5.0
_OPENWS_RE = re.compile(r'workspaceStorage/([0-9a-f]{32})/state\.vscdb')


# ---- SQLite read helpers (read-only, WAL-safe) -----------------------


def _db_signature(db_path: Path) -> Optional[tuple[int, int]]:
    """Return ``(db_mtime_ns, wal_mtime_ns)`` or ``None`` if missing.

    The ``-wal`` sidecar changes on every write while the main db file
    can sit unchanged for a while, so both feed the cache key.
    """
    try:
        main = db_path.stat().st_mtime_ns
    except OSError:
        return None
    wal_ns = 0
    wal = Path(str(db_path) + '-wal')
    try:
        if wal.is_file():
            wal_ns = wal.stat().st_mtime_ns
    except OSError:
        pass
    return (main, wal_ns)


def _query_ro(db_path: Path, sql: str,
              params: tuple = ()) -> list[tuple]:
    """Run a read-only query against a (possibly live, WAL) Cursor db.

    Primary path opens a read-only WAL connection — SQLite hands back a
    consistent snapshot without blocking Cursor's writer and without us
    copying anything.  If that fails (older SQLite, lock edge cases) we
    fall back to copying the db + ``-wal`` + ``-shm`` to a temp dir and
    reading the private copy.  Any failure yields ``[]``.
    """
    if not db_path.is_file():
        return []
    uri = 'file:' + urllib.request.pathname2url(str(db_path)) + '?mode=ro'
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            conn.execute('PRAGMA query_only=1')
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.debug("read-only query failed, trying copy fallback",
                     exc_info=True)
    return _query_via_copy(db_path, sql, params)


def _query_via_copy(db_path: Path, sql: str,
                    params: tuple = ()) -> list[tuple]:
    """Copy the db (+ WAL/SHM) to a temp dir and query the copy."""
    tmpdir = tempfile.mkdtemp(prefix='leap-cursor-')
    try:
        for suffix in ('', '-wal', '-shm'):
            src = Path(str(db_path) + suffix)
            if src.is_file():
                shutil.copy2(src, Path(tmpdir) / ('db.vscdb' + suffix))
        dst = Path(tmpdir) / 'db.vscdb'
        if not dst.is_file():
            return []
        conn = sqlite3.connect(str(dst), timeout=2.0)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        logger.debug("copy-fallback query failed", exc_info=True)
        return []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---- Parsing -----------------------------------------------------------


def _uri_to_path(folder_uri: str) -> str:
    """Convert a ``file://`` workspace URI to a filesystem path.

    Returns '' for anything that isn't a usable absolute path.
    """
    if not isinstance(folder_uri, str) or not folder_uri:
        return ''
    if folder_uri.startswith('file://'):
        # Use urlparse so a ``file://<authority>/path`` form (e.g.
        # ``file://localhost/Users/x``) yields ``/Users/x`` rather than the
        # bogus ``localhost/Users/x`` a naive ``[len('file://'):]`` strip
        # produces (which would slip past the empty-basename guard and make
        # us run git in a path that doesn't exist).  Normal Cursor URIs are
        # ``file:///...`` (empty authority) and are unaffected.
        parsed = urllib.parse.urlparse(folder_uri)
        path = urllib.parse.unquote(parsed.path)
        return path if path.startswith('/') else ''
    return folder_uri if folder_uri.startswith('/') else ''


def _global_composers() -> dict[str, dict]:
    """Return ``{composerId: record}`` from the global db (cached).

    Reads every ``composerData:<id>`` row in one pass; malformed JSON
    rows are skipped.  Re-reads only when the db signature changes.
    """
    sig = _db_signature(GLOBAL_DB)
    if sig is None:
        return {}
    if _GLOBAL_CACHE['sig'] == sig:
        return _GLOBAL_CACHE['data']
    data: dict[str, dict] = {}
    rows = _query_ro(
        GLOBAL_DB,
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'",
    )
    for key, value in rows:
        if not isinstance(key, str) or ':' not in key:
            continue
        cid = key.split(':', 1)[1]
        if not cid or cid == 'empty-state':
            continue
        try:
            rec = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(rec, dict):
            data[cid] = rec
    _GLOBAL_CACHE['sig'] = sig
    _GLOBAL_CACHE['data'] = data
    return data


def _open_composer_ids(ws_db: Path) -> list[str]:
    """Return the composer ids open in a workspace window (cached).

    Reads ``ItemTable['composer.composerData'].selectedComposerIds``.
    """
    sig = _db_signature(ws_db)
    cached = _WS_CACHE.get(str(ws_db))
    if cached is not None and cached.get('sig') == sig:
        return cached['ids']
    ids: list[str] = []
    rows = _query_ro(
        ws_db,
        "SELECT value FROM ItemTable WHERE key='composer.composerData'",
    )
    if rows:
        try:
            data = json.loads(rows[0][0])
            selected = data.get('selectedComposerIds') or []
            ids = [s for s in selected if isinstance(s, str) and s]
        except (TypeError, ValueError, IndexError, AttributeError):
            ids = []
    _WS_CACHE[str(ws_db)] = {'sig': sig, 'ids': ids}
    return ids


# ---- Open-workspace detection (shared with the VS Code Copilot scanner) --
#
# Cursor and VS Code are both VS Code at heart: identical
# ``workspaceStorage/<hash>/state.vscdb`` layout, so the lsof-based
# open-workspace probe and the workspace.json→folder resolution are one
# implementation parameterized by (pgrep pattern, storage dir, cache).
# vscode_copilot_scan imports these; each editor passes its own cache dict
# so their TTL windows stay independent.


def _pids_for(pgrep_pattern: str) -> list[str]:
    """PIDs whose command line matches *pgrep_pattern* (main app + helpers)."""
    try:
        result = subprocess.run(
            ['pgrep', '-f', pgrep_pattern],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    return [p for p in result.stdout.split() if p.isdigit()]


def _detect_open_workspace_hashes(pids_fn: Callable[[], list[str]],
                                  cache: dict,
                                  ttl: float = _OPENWS_TTL) -> set[str]:
    """Hashes of workspaceStorage dirs whose ``state.vscdb`` the editor
    holds open - i.e. its currently-open workspaces.

    *pids_fn* supplies the editor's process ids (a callable, not a list,
    so a warm cache skips the pgrep entirely and so tests can monkeypatch
    the per-editor ``_*_pids`` seam).  Uses ``lsof`` - pid-scoped, so it
    only sees that editor's files - which works regardless of whether the
    editor is frontmost (System Events only enumerates windows for the
    active app).  Parses stdout regardless of lsof's exit code (lsof
    returns non-zero on benign per-fd warnings while still emitting valid
    output).  Result TTL-cached in the caller-owned *cache* (``mono`` /
    ``hashes`` keys) so the two editors never share a cache.
    """
    now = time.monotonic()
    if (now - cache['mono']) < ttl:
        return cache['hashes']
    hashes: set[str] = set()
    pids = pids_fn()
    if pids:
        try:
            result = subprocess.run(
                ['lsof', '-p', ','.join(pids)],
                capture_output=True, text=True, timeout=8,
            )
            hashes = set(_OPENWS_RE.findall(result.stdout))
        except (subprocess.SubprocessError, OSError):
            logger.debug("lsof open-workspace detection failed", exc_info=True)
    cache['mono'] = now
    cache['hashes'] = hashes
    return hashes


def _resolve_open_workspace(ws_hash: str,
                            workspace_storage: Path
                            ) -> Optional[tuple[str, Path]]:
    """Return ``(folder_path, state_db_path)`` for one workspaceStorage
    hash under *workspace_storage*, or ``None`` if it isn't a usable
    workspace.

    Reading only the hashes that are actually open (rather than every
    workspaceStorage dir) keeps the per-scan I/O to the handful of open
    workspaces.
    """
    d = workspace_storage / ws_hash
    wj = d / 'workspace.json'
    db = d / 'state.vscdb'
    if not (wj.is_file() and db.is_file()):
        return None
    try:
        folder_uri = json.loads(wj.read_text()).get('folder')
    except (OSError, ValueError, AttributeError):
        return None
    folder = _uri_to_path(folder_uri)
    if not folder:
        return None
    return (folder, db)


def _cursor_pids() -> list[str]:
    """PIDs of running Cursor processes (main app + helpers)."""
    return _pids_for('Cursor.app')


def _open_workspace_hashes() -> set[str]:
    """Currently-open Cursor workspace hashes (see
    :func:`_detect_open_workspace_hashes`)."""
    return _detect_open_workspace_hashes(_cursor_pids, _OPENWS_CACHE)


def _workspace_for_hash(ws_hash: str) -> Optional[tuple[str, Path]]:
    """``(folder, state_db)`` for an open Cursor workspace hash, or None."""
    return _resolve_open_workspace(ws_hash, WORKSPACE_STORAGE)


def _branch_for(folder: str) -> str:
    """Return the project folder's current git branch (TTL-cached, '' on miss)."""
    now = time.monotonic()
    cached = _BRANCH_CACHE.get(folder)
    if cached is not None and (now - cached[0]) < _BRANCH_TTL:
        return cached[1]
    try:
        branch = get_git_branch(folder) or ''
    except Exception:
        branch = ''
    _BRANCH_CACHE[folder] = (now, branch)
    return branch


def _extract_last_message(composer_id: str, rec: dict) -> str:
    """Best-effort 'last message' for a composer: the most recent USER
    prompt (matching Leap's 'Last Msg = what you last sent' semantics),
    falling back to the most recent message with text.

    Message text lives in separate ``cursorDiskKV['bubbleId:<cid>:<bid>']``
    rows; ``composerData.fullConversationHeadersOnly`` is the ordered
    header list (``type`` 1 = user).  Returns '' when nothing readable.
    """
    headers = rec.get('fullConversationHeadersOnly')
    if not isinstance(headers, list) or not headers:
        return ''
    chosen: Optional[dict] = None
    for h in reversed(headers):
        if not isinstance(h, dict):
            continue
        grouping = h.get('grouping')
        has_text = grouping.get('hasText', True) \
            if isinstance(grouping, dict) else True
        if not has_text or not h.get('bubbleId'):
            continue
        if h.get('type') == 1:  # user message - preferred, stop here
            chosen = h
            break
        if chosen is None:  # remember newest with-text as fallback
            chosen = h
    if chosen is None:
        return ''
    rows = _query_ro(
        GLOBAL_DB,
        "SELECT value FROM cursorDiskKV WHERE key=?",
        (f"bubbleId:{composer_id}:{chosen['bubbleId']}",),
    )
    if not rows:
        return ''
    try:
        bubble = json.loads(rows[0][0])
    except (TypeError, ValueError):
        return ''
    text = bubble.get('text') if isinstance(bubble, dict) else None
    if not isinstance(text, str) or not text.strip():
        return ''
    return ' '.join(text.split())[:_LASTMSG_MAX]


def _last_message(composer_id: str, rec: dict) -> str:
    """Cached wrapper for :func:`_extract_last_message` (keyed by the
    global db signature, so it re-reads only when Cursor wrote)."""
    sig = _db_signature(GLOBAL_DB)
    cached = _LASTMSG_CACHE.get(composer_id)
    if cached is not None and cached[0] == sig:
        return cached[1]
    text = _extract_last_message(composer_id, rec)
    _LASTMSG_CACHE[composer_id] = (sig, text)
    return text


def _derive_status(rec: dict) -> tuple[str, str]:
    """Map a composer record to ``(status_kind, status_text)``.

    Best-effort, derived from the persisted composer fields (Cursor's
    richer ``activityState`` is computed in-memory and not on disk):
      * actively generating          → running
      * manually marked unread       → unread
      * otherwise                    → idle

    Two deliberate choices, both backed by Cursor's own code:

    * **No "Aborted" state.**  Cursor writes ``status='aborted'`` whenever
      a generation *ends* (the stop path does
      ``updateComposer(h, {chatGenerationUUID: undefined, status:'aborted'})``),
      not only on an explicit user stop - so a normally-finished chat
      also reads ``'aborted'``.  That would make every done chat look
      interrupted; "Idle" is accurate.
    * **"Unread", not "Replied".**  ``hasUnreadMessages`` is set true ONLY
      by Cursor's manual "Mark as unread" action and cleared when the
      chat is viewed - it is NOT an automatic "the agent just replied"
      signal, so it's labelled "Unread".
    """
    gen = rec.get('generatingBubbleIds')
    # Both signals matter: generatingBubbleIds is the live in-flight set;
    # status=='generating' is the composer-level flag (either may lead).
    if (isinstance(gen, list) and gen) or rec.get('status') == 'generating':
        return 'running', '●  Running'
    if rec.get('hasUnreadMessages') is True:
        return 'unread', '\U0001f514  Unread'
    return 'idle', '○  Idle'


def _build_row(folder: str, base: str, composer_id: str,
               rec: dict, branch: str = '', last_msg: str = '') -> dict:
    """Build one synthetic monitor row dict for an open Agent tab."""
    name = rec.get('name')
    if not isinstance(name, str) or not name.strip():
        name = 'New Agent'
    kind, text = _derive_status(rec)
    return {
        'tag': CURSOR_GUI_TAG_PREFIX + composer_id,
        'row_type': CURSOR_GUI_ROW_TYPE,
        # Just the chat name - the project is already its own column,
        # so a "<project>: " prefix is redundant and only steals width
        # from the name (which is the point of the row).
        'display_label': name,
        'project': base,
        'project_path': folder,
        'branch': branch,
        'last_msg': last_msg,
        'ide': 'Cursor',
        'cli_provider': 'cursor-gui',
        # Generic keys shared with VS Code Copilot rows (see
        # vscode_copilot_scan) so the render pipeline reads one shape.
        'window_folder': folder,
        'chat_id': composer_id,
        'chat_name': name,
        'status_kind': kind,
        'status_text': text,
        'created_at': rec.get('createdAt'),
        'server_pid': None,
    }


# ---- Public entry point -----------------------------------------------


def scan_open_cursor_agents() -> list[dict]:
    """Return one row dict per Agent tab open in a live Cursor window.

    Returns ``[]`` when Cursor isn't running / has no open workspaces.
    Never raises - any disk/schema problem degrades to fewer rows.
    """
    try:
        open_hashes = _open_workspace_hashes()
    except Exception:
        logger.debug("open-workspace detection failed", exc_info=True)
        return []
    if not open_hashes:
        # Cursor fully closed (or no open workspaces) - every cached entry
        # is now stale, so drop them all rather than leaking them until
        # Cursor reopens.
        _LASTMSG_CACHE.clear()
        _BRANCH_CACHE.clear()
        _WS_CACHE.clear()
        return []

    composers = _global_composers()

    # Gather open composers per workspace.  A composer can appear in
    # MULTIPLE workspaces' selectedComposerIds: Cursor lists the blank
    # default "New Agent" composer in every window that has no real chat
    # focused, so the same composer id shows up in several windows.  We
    # must dedupe by composer id - otherwise one chat renders as two rows
    # sharing the same ``cursor-gui:<id>`` tag (which also corrupts the
    # per-tag PR widget / alias / color).
    workspaces: list[tuple[str, list[str]]] = []  # (folder, composer_ids)
    open_ws_keys: set[str] = set()   # state.vscdb paths of open workspaces
    open_folders: set[str] = set()   # folders of open workspaces
    for ws_hash in open_hashes:
        ws = _workspace_for_hash(ws_hash)
        if ws is None:
            continue
        folder, ws_db = ws
        if not os.path.basename(folder.rstrip('/')):
            continue
        open_ws_keys.add(str(ws_db))
        open_folders.add(folder)
        workspaces.append((folder, _open_composer_ids(ws_db)))

    # Attribute a shared composer to the workspace with the most open
    # tabs (most likely the project the user is actually working in);
    # folder order breaks ties for determinism.
    workspaces.sort(key=lambda w: (-len(w[1]), w[0]))

    rows: list[dict] = []
    seen: set[str] = set()
    for folder, composer_ids in workspaces:
        base = os.path.basename(folder.rstrip('/'))
        branch = _branch_for(folder)  # cached; once per folder
        for cid in composer_ids:
            if cid in seen:
                continue
            seen.add(cid)
            rec = composers.get(cid)
            if rec is None:
                continue
            last_msg = _last_message(cid, rec)
            rows.append(_build_row(folder, base, cid, rec, branch, last_msg))

    # Prune the signature/TTL caches to what's currently open so none of
    # them grow without bound across a long session: last-message by
    # composer id, workspace ids by state.vscdb path, branch by folder.
    for cid in [c for c in _LASTMSG_CACHE if c not in seen]:
        _LASTMSG_CACHE.pop(cid, None)
    for key in [k for k in _WS_CACHE if k not in open_ws_keys]:
        _WS_CACHE.pop(key, None)
    for fld in [f for f in _BRANCH_CACHE if f not in open_folders]:
        _BRANCH_CACHE.pop(fld, None)
    return rows
