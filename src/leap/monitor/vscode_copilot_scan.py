"""Read-only scanner for VS Code Copilot Chat sessions.

The GUI sibling of :mod:`leap.monitor.cursor_gui_scan`: where that module
surfaces Cursor's open Agent/Composer *tabs*, this one surfaces GitHub
Copilot Chat *sessions* of the workspaces open in a live VS Code.  VS Code
has no "open chat tabs" notion to mirror (the chat panel shows one session;
the Agent Sessions view lists them all), so the visible set is "recently
active or generating" instead - see :data:`RECENT_WINDOW_MS`.

VS Code persists chat state per workspace under
``~/Library/Application Support/Code/User/workspaceStorage/<hash>/``:

* ``workspace.json`` → ``{"folder": "file:///.../<project>"}`` (same
  format as Cursor - both are VS Code at heart).
* ``state.vscdb`` → ``ItemTable['chat.ChatSessionStore.index']`` - a JSON
  index of every chat session: ``title``, ``lastMessageDate``, ``isEmpty``,
  ``timing`` and ``lastResponseState``.  Verified live (a real Copilot
  generation watched on disk): the index is rewritten within ~1s of a
  request starting and again when it ends, so it is fresh enough to drive
  a status column.
* ``chatSessions/<sessionId>.jsonl`` → the session content as an op-log
  (``kind`` 0 = full initial state; later lines patch paths in ``k``).
  Read (signature-cached) for the last user prompt and the Context column's
  usage: each finished request records ``result.metadata.promptTokens``
  (that request's input-side size - a live-context proxy) and
  ``inputState.selectedModel.metadata.maxInputTokens`` is the selected
  model's input limit (the window).

``lastResponseState`` holds the last request's response state.  Mapping
(verified empirically + against VS Code's own index→status function, which
maps ``{1,2}→Completed, 3→Failed, 0→InProgress, 4→NeedsInput``):

* ``2`` - a response is being generated right now    → running
* ``4`` - the agent is waiting for user input        → needs input
* ``3`` - the last request failed                    → failed (idle kind)
* ``1`` (or anything else) - completed / no request  → idle

Which workspaces are *open* is detected exactly like the Cursor scan: from
the ``state.vscdb`` file handles the running VS Code holds open (``lsof``),
frontmost-independent and TTL-cached.

The on-disk schema is undocumented and version-fragile, so every read is
defensive: any unexpected shape skips that row rather than raising.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from leap.monitor.cursor_gui_scan import (
    CURSOR_GUI_ROW_TYPE,
    CURSOR_GUI_TAG_PREFIX,
    _branch_for,
    _db_signature,
    _detect_open_workspace_hashes,
    _pids_for,
    _query_ro,
    _resolve_open_workspace,
    _uri_to_path,
)

logger = logging.getLogger(__name__)

__all__ = [
    'VSCODE_GUI_ROW_TYPE',
    'VSCODE_GUI_TAG_PREFIX',
    'GUI_ROW_TYPES',
    'GUI_TAG_PREFIXES',
    'scan_open_vscode_copilot_sessions',
]

# Row discriminator + tag namespace.  The tag is synthetic (a chat session
# UUID) so it can never collide with a real Leap tag.
VSCODE_GUI_ROW_TYPE: str = 'vscode_copilot_gui'
VSCODE_GUI_TAG_PREFIX: str = 'vscode-gui:'

# Both editor-GUI row families, for the code paths that treat Cursor and
# VS Code Copilot rows the same (reconcile, drag-reorder, prune, render
# dispatch).  ``str.startswith`` accepts the tuple directly.
GUI_ROW_TYPES: tuple[str, ...] = (CURSOR_GUI_ROW_TYPE, VSCODE_GUI_ROW_TYPE)
GUI_TAG_PREFIXES: tuple[str, ...] = (CURSOR_GUI_TAG_PREFIX,
                                     VSCODE_GUI_TAG_PREFIX)

# On-disk locations.  Module-level so tests can repoint them at fixtures.
VSCODE_USER_DIR: Path = (
    Path.home() / "Library" / "Application Support" / "Code" / "User"
)
VSCODE_WORKSPACE_STORAGE: Path = VSCODE_USER_DIR / "workspaceStorage"

# A session with no generation in flight is shown only while its last
# activity is this recent.  VS Code keeps every chat ever in the store, so
# without a cutoff the table would fill with stale history; 48h keeps the
# "what am I working on" set without the museum.  Tracked sessions bypass
# this via ``keep_ids`` (their PR stays monitored like a closed Cursor tab).
RECENT_WINDOW_MS: int = 48 * 3600 * 1000

# ``lastResponseState`` values (see module docstring for the derivation).
_STATE_COMPLETE: int = 1
_STATE_GENERATING: int = 2
_STATE_FAILED: int = 3
_STATE_NEEDS_INPUT: int = 4

# Per-workspace session-index cache, keyed by the db path and invalidated
# by db signature (mtime of db + -wal), like the Cursor scan's caches.
_INDEX_CACHE: dict[str, dict[str, Any]] = {}
# Per-workspace archived-session-id cache (same db-signature invalidation).
_ARCHIVED_CACHE: dict[str, dict[str, Any]] = {}
# Per-session details cache (last user prompt + context usage), keyed by
# the session jsonl signature.
_DETAILS_CACHE: dict[str, tuple[Any, tuple[str, Optional[dict]]]] = {}
# Length cap for the Last Msg cell text.
_LASTMSG_MAX_LEN: int = 200
# Open-workspace detection cache (pgrep+lsof).  VS Code keeps its OWN cache
# dict (the shared helper takes it as a param) so its TTL window is
# independent of Cursor's.
_OPENWS_CACHE: dict[str, Any] = {'mono': 0.0, 'hashes': set()}


# ---- Open-workspace detection (shared impl in cursor_gui_scan) ---------


def _vscode_pids() -> list[str]:
    """PIDs of running VS Code processes (main app + helpers).

    Matched on the app-bundle path so Cursor (also Electron, also matching
    a bare 'Code') and "Visual Studio Code - Insiders.app" are excluded.
    """
    return _pids_for('Visual Studio Code.app')


def _open_workspace_hashes() -> set[str]:
    """Currently-open VS Code workspace hashes (shared impl, VS Code's
    own pids + cache; see cursor_gui_scan._detect_open_workspace_hashes)."""
    return _detect_open_workspace_hashes(_vscode_pids, _OPENWS_CACHE)


def _workspace_for_hash(ws_hash: str) -> Optional[tuple[str, Path]]:
    """``(folder, state_db)`` for an open VS Code workspace hash, or None."""
    return _resolve_open_workspace(ws_hash, VSCODE_WORKSPACE_STORAGE)


# ---- Session index ------------------------------------------------------


def _session_index(ws_db: Path) -> dict[str, dict]:
    """Return ``{sessionId: entry}`` from a workspace's chat-session index
    (cached by db signature)."""
    sig = _db_signature(ws_db)
    cached = _INDEX_CACHE.get(str(ws_db))
    if cached is not None and cached.get('sig') == sig:
        return cached['entries']
    entries: dict[str, dict] = {}
    rows = _query_ro(
        ws_db,
        "SELECT value FROM ItemTable WHERE key='chat.ChatSessionStore.index'",
    )
    if rows:
        try:
            data = json.loads(rows[0][0])
            raw = data.get('entries')
            if isinstance(raw, dict):
                entries = {sid: e for sid, e in raw.items()
                           if isinstance(sid, str) and sid
                           and isinstance(e, dict)}
        except (TypeError, ValueError, IndexError, AttributeError):
            entries = {}
    _INDEX_CACHE[str(ws_db)] = {'sig': sig, 'entries': entries}
    return entries


def _derive_status(entry: dict) -> tuple[str, str]:
    """Map a session-index entry to ``(status_kind, status_text)``.

    Driven by ``lastResponseState`` (see module docstring).  "Needs input"
    uses the ``unread`` kind so it renders in the attention color, same as
    a Cursor row's Unread.  A failed last request is informational, not
    urgent, so it keeps the idle color with explicit text.
    """
    state = entry.get('lastResponseState')
    if state == _STATE_GENERATING:
        return 'running', '●  Running'
    if state == _STATE_NEEDS_INPUT:
        return 'unread', '\U0001f514  Needs input'
    if state == _STATE_FAILED:
        return 'idle', '✗  Failed'
    return 'idle', '○  Idle'


def _entry_last_ms(entry: dict) -> int:
    """The session's last-message epoch-ms, coerced to int (0 if absent)."""
    last_ms = entry.get('lastMessageDate')
    return int(last_ms) if isinstance(last_ms, (int, float)) else 0


def _is_dismissed(sid: str, entry: dict, hidden: dict) -> bool:
    """True if the user removed this row from Leap and the chat hasn't had
    newer activity since.

    ``hidden`` maps sessionId -> dismiss-time (epoch ms).  Keyed by the
    immutable session UUID, never the title - so a *future* chat with the
    same name gets a new UUID and is unaffected.  A new user message
    (``lastMessageDate`` past the dismiss time) auto-returns the row: the
    chat is active again, so Leap shows it again.
    """
    dismissed_at = hidden.get(sid)
    return dismissed_at is not None and _entry_last_ms(entry) <= dismissed_at


def _is_visible(sid: str, entry: dict, now_ms: int,
                keep_ids: set[str]) -> bool:
    """Decide whether a session earns a monitor row.

    Two removals are applied by the caller *before* this: sessions the
    user archived in VS Code (:func:`_archived_session_ids`) and rows the
    user removed from Leap (:func:`_is_dismissed`).  The rest:

    * never: empty sessions (VS Code pre-creates blank "New Chat" entries
      on every panel open) or external sessions (cloud / other providers -
      the Agent Sessions view lists e.g. Claude Code there, which would
      double-count Leap's own rows);
    * always: a generating session, or one the monitor PR-tracks
      (``keep_ids``) - tracking outliving the recency window mirrors a
      tracked-but-closed Cursor tab;
    * otherwise: only while active within :data:`RECENT_WINDOW_MS`.
    """
    if entry.get('isEmpty') is True or entry.get('isExternal') is True:
        return False
    if sid in keep_ids:
        return True
    if entry.get('lastResponseState') == _STATE_GENERATING:
        return True
    return (now_ms - _entry_last_ms(entry)) <= RECENT_WINDOW_MS


def _session_id_from_resource(resource: Any) -> Optional[str]:
    """Decode a ``vscode-chat-session://local/<base64url(id)>`` resource
    back to its session id, or ``None`` for any other scheme/shape.

    VS Code addresses local sessions this way in ``agentSessions.state.cache``
    (external providers like ``claude-code:`` use a different scheme, which
    we ignore - they aren't Leap rows)."""
    prefix = 'vscode-chat-session://local/'
    if not isinstance(resource, str) or not resource.startswith(prefix):
        return None
    b = resource[len(prefix):]
    try:
        return base64.urlsafe_b64decode(b + '=' * (-len(b) % 4)).decode('utf-8')
    except (ValueError, UnicodeDecodeError):
        return None


def _archived_session_ids(ws_db: Path) -> set[str]:
    """Session ids the user has archived in this workspace.

    Archive is VS Code's persisted, recoverable "close" (the analog of
    Cursor's close-tab-keep-in-history).  The archived flag lives in
    ``agentSessions.state.cache`` (entry shape
    ``{resource, archived, pinned, read}``), NOT in the session index, so
    the scan reads it separately and drops archived sessions - that's how
    the monitor row disappears when the chat is archived.  Cached by db
    signature, like the index."""
    sig = _db_signature(ws_db)
    cached = _ARCHIVED_CACHE.get(str(ws_db))
    if cached is not None and cached.get('sig') == sig:
        return cached['ids']
    ids: set[str] = set()
    rows = _query_ro(
        ws_db,
        "SELECT value FROM ItemTable WHERE key='agentSessions.state.cache'",
    )
    if rows:
        try:
            data = json.loads(rows[0][0])
            if isinstance(data, list):
                for e in data:
                    if isinstance(e, dict) and e.get('archived'):
                        sid = _session_id_from_resource(e.get('resource'))
                        if sid:
                            ids.add(sid)
        except (TypeError, ValueError, IndexError):
            ids = set()
    _ARCHIVED_CACHE[str(ws_db)] = {'sig': sig, 'ids': ids}
    return ids


# ---- Last user prompt + context usage --------------------------------------


def _extract_session_details(session_file: Path) -> tuple[str, Optional[dict]]:
    """Best-effort ``(last user prompt, context usage)`` from a session's
    op-log jsonl.

    Requests (= user turns) appear in two forms, both observed live:
    the ``kind:0`` first line carries ``v.requests`` (the compacted full
    state), and later ops re-set the array (``k == ['requests']``) or one
    element (``k == ['requests', <idx>]``).  The newest request list wins;
    its last element's ``message.text`` is the prompt.

    Context usage pairs the newest finished request's
    ``result.metadata.promptTokens`` (that request's input-side size - a
    live-context proxy, input-only like the CLI providers) with the selected
    model's ``inputState.selectedModel.metadata.maxInputTokens`` (the
    window).  ``inputState`` comes from the ``kind:0`` state and is patched
    by ``k == ['inputState']`` / ``['inputState', 'selectedModel']`` ops, so
    a mid-session model switch updates the window.  The usage dict is
    ``{used_tokens, window, model}`` (the row's ``context`` key), or ``None``
    when either side is missing (no finished request yet, or no readable
    model limit) -> blank Context cell.
    """
    requests: list = []
    selected_model: Optional[dict] = None
    try:
        with open(session_file, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                try:
                    op = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(op, dict):
                    continue
                if op.get('kind') == 0:
                    init = op.get('v')
                    reqs = init.get('requests') if isinstance(init, dict) \
                        else None
                    if isinstance(reqs, list):
                        requests = reqs
                    inp = init.get('inputState') if isinstance(init, dict) \
                        else None
                    sm = inp.get('selectedModel') if isinstance(inp, dict) \
                        else None
                    if isinstance(sm, dict):
                        selected_model = sm
                    continue
                k = op.get('k')
                if not isinstance(k, list) or not k:
                    continue
                v = op.get('v')
                if k[0] == 'inputState':
                    if len(k) == 1 and isinstance(v, dict):
                        sm = v.get('selectedModel')
                        if isinstance(sm, dict):
                            selected_model = sm
                    elif (len(k) == 2 and k[1] == 'selectedModel'
                            and isinstance(v, dict)):
                        selected_model = v
                    continue
                if k[0] != 'requests':
                    continue
                if len(k) == 1 and isinstance(v, list):
                    requests = v
                elif (len(k) == 2 and isinstance(k[1], int)
                        and isinstance(v, dict)):
                    idx = k[1]
                    if 0 <= idx < len(requests):
                        requests[idx] = v
                    elif idx == len(requests):
                        requests.append(v)
    except OSError:
        return '', None
    last_msg = ''
    for req in reversed(requests):
        if not isinstance(req, dict):
            continue
        msg = req.get('message')
        text = msg.get('text') if isinstance(msg, dict) else None
        if isinstance(text, str) and text.strip():
            last_msg = ' '.join(text.split())[:_LASTMSG_MAX_LEN]
            break
    return last_msg, _derive_context(requests, selected_model)


def _derive_context(requests: list,
                    selected_model: Optional[dict]) -> Optional[dict]:
    """Pair the newest request's prompt tokens with the model's input limit."""
    metadata = (selected_model.get('metadata')
                if isinstance(selected_model, dict) else None)
    if not isinstance(metadata, dict):
        return None
    window = metadata.get('maxInputTokens')
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        return None
    for req in reversed(requests):
        if not isinstance(req, dict):
            continue
        result = req.get('result')
        res_md = result.get('metadata') if isinstance(result, dict) else None
        used = res_md.get('promptTokens') if isinstance(res_md, dict) else None
        if isinstance(used, int) and not isinstance(used, bool) and used > 0:
            name = metadata.get('name') or metadata.get('id')
            return {'used_tokens': used, 'window': window,
                    'model': name if isinstance(name, str) else ''}
    return None


def _session_details(session_file: Path) -> tuple[str, Optional[dict]]:
    """Cached wrapper for :func:`_extract_session_details` (keyed by the
    session file's mtime+size, so it re-reads only after a write)."""
    try:
        st = session_file.stat()
        sig: Any = (st.st_mtime_ns, st.st_size)
    except OSError:
        return '', None
    key = str(session_file)
    cached = _DETAILS_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    details = _extract_session_details(session_file)
    _DETAILS_CACHE[key] = (sig, details)
    return details


# ---- Row building --------------------------------------------------------


def _build_row(folder: str, base: str, sid: str, entry: dict,
               branch: str = '', last_msg: str = '',
               context: Optional[dict] = None) -> dict:
    """Build one synthetic monitor row dict for a Copilot Chat session.

    Same shape as a Cursor GUI row (shared render/reconcile pipeline);
    ``row_type`` and the generic ``chat_id`` / ``window_folder`` keys are
    what the per-editor code paths dispatch on.
    """
    title = entry.get('title')
    if not isinstance(title, str) or not title.strip():
        title = 'New Chat'
    kind, text = _derive_status(entry)
    timing = entry.get('timing')
    created = timing.get('created') if isinstance(timing, dict) else None
    return {
        'tag': VSCODE_GUI_TAG_PREFIX + sid,
        'row_type': VSCODE_GUI_ROW_TYPE,
        'display_label': title,
        'project': base,
        'project_path': folder,
        'branch': branch,
        'last_msg': last_msg,
        'context': context,
        'ide': 'VS Code',
        'cli_provider': 'vscode-copilot-gui',
        'window_folder': folder,
        'chat_id': sid,
        'chat_name': title,
        'status_kind': kind,
        'status_text': text,
        'created_at': created,
        'server_pid': None,
    }


# ---- Public entry point -----------------------------------------------


def scan_open_vscode_copilot_sessions(
    hidden: Optional[dict] = None,
    keep_ids: Optional[set[str]] = None,
) -> list[dict]:
    """Return one row dict per visible Copilot Chat session of each
    workspace open in a live VS Code.

    Two user removals drop a row, both keyed by the immutable session
    UUID (never the title, so a future same-named chat is unaffected):
    *hidden* maps sessionId -> dismiss-time for rows the user removed from
    Leap (auto-returns on newer activity - see :func:`_is_dismissed`), and
    sessions the user archived in VS Code itself
    (:func:`_archived_session_ids`).  *keep_ids* (PR-tracked session ids)
    bypass the recency filter so a tracked chat keeps its row.

    Returns ``[]`` when VS Code isn't running / has no open workspaces.
    Never raises - any disk/schema problem degrades to fewer rows.
    """
    hidden = hidden or {}
    keep_ids = keep_ids or set()
    try:
        open_hashes = _open_workspace_hashes()
    except Exception:
        logger.debug("VS Code open-workspace detection failed", exc_info=True)
        return []
    if not open_hashes:
        # VS Code fully closed (or no open workspaces) - drop every cache
        # rather than leaking entries until it reopens.
        _INDEX_CACHE.clear()
        _ARCHIVED_CACHE.clear()
        _DETAILS_CACHE.clear()
        return []

    now_ms = int(time.time() * 1000)
    rows: list[dict] = []
    open_ws_keys: set[str] = set()
    seen_files: set[str] = set()
    for ws_hash in sorted(open_hashes):
        ws = _workspace_for_hash(ws_hash)
        if ws is None:
            continue
        folder, ws_db = ws
        base = os.path.basename(folder.rstrip('/'))
        if not base:
            continue
        open_ws_keys.add(str(ws_db))
        entries = _session_index(ws_db)
        archived = _archived_session_ids(ws_db)
        visible = [(sid, e) for sid, e in entries.items()
                   if sid not in archived
                   and not _is_dismissed(sid, e, hidden)
                   and _is_visible(sid, e, now_ms, keep_ids)]
        if not visible:
            continue
        branch = _branch_for(folder)  # cached; once per folder
        for sid, entry in visible:
            session_file = ws_db.parent / 'chatSessions' / f'{sid}.jsonl'
            seen_files.add(str(session_file))
            last_msg, context = _session_details(session_file)
            rows.append(_build_row(folder, base, sid, entry, branch,
                                   last_msg, context))

    # Prune caches to what's currently open/visible so they stay bounded
    # across a long monitor run.
    for key in [k for k in _INDEX_CACHE if k not in open_ws_keys]:
        _INDEX_CACHE.pop(key, None)
    for key in [k for k in _ARCHIVED_CACHE if k not in open_ws_keys]:
        _ARCHIVED_CACHE.pop(key, None)
    for key in [k for k in _DETAILS_CACHE if k not in seen_files]:
        _DETAILS_CACHE.pop(key, None)
    return rows
