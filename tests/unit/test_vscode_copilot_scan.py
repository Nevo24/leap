"""Unit tests for the VS Code Copilot Chat session scanner.

Pure-logic + fixture-SQLite tests (no GUI, no live VS Code).  They pin
the ``lastResponseState`` status mapping, the visibility rules (empty /
external / recency / hidden / keep-ids), the last-user-prompt op-log
parsing, the row shape, and defensive handling of malformed state.
"""

import base64
import json
import sqlite3
import time
from pathlib import Path

import pytest

from leap.monitor import vscode_copilot_scan as vcs


NOW_MS = int(time.time() * 1000)


@pytest.fixture(autouse=True)
def _clear_caches():
    vcs._INDEX_CACHE.clear()
    vcs._ARCHIVED_CACHE.clear()
    vcs._DETAILS_CACHE.clear()
    vcs._OPENWS_CACHE['mono'] = 0.0
    vcs._OPENWS_CACHE['hashes'] = set()
    yield
    vcs._INDEX_CACHE.clear()
    vcs._ARCHIVED_CACHE.clear()
    vcs._DETAILS_CACHE.clear()
    vcs._OPENWS_CACHE['mono'] = 0.0
    vcs._OPENWS_CACHE['hashes'] = set()


def _entry(state: int = 1, last_ms: int = NOW_MS, empty: bool = False,
           external: bool = False, title: str = 'My chat') -> dict:
    return {
        'sessionId': 'x', 'title': title, 'lastMessageDate': last_ms,
        'isEmpty': empty, 'isExternal': external,
        'lastResponseState': state,
        'timing': {'created': last_ms},
    }


def _session_resource(sid: str) -> str:
    """The vscode-chat-session://local/<base64url> resource for a sid,
    matching how VS Code keys agentSessions.state.cache."""
    b = base64.urlsafe_b64encode(sid.encode('utf-8')).decode().rstrip('=')
    return f'vscode-chat-session://local/{b}'


def _make_workspace(root: Path, ws_hash: str, folder: str,
                    entries: dict, sessions_jsonl: dict = None,
                    archived_ids: list = None) -> None:
    """Build a fixture workspaceStorage/<hash>/ dir: workspace.json +
    state.vscdb with the chat-session index + (optional) the
    agentSessions.state.cache archived flags + chatSessions/*.jsonl."""
    d = root / ws_hash
    d.mkdir(parents=True)
    (d / 'workspace.json').write_text(
        json.dumps({'folder': f'file://{folder}'}))
    conn = sqlite3.connect(str(d / 'state.vscdb'))
    try:
        conn.execute('CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)')
        conn.execute(
            'INSERT INTO ItemTable VALUES (?, ?)',
            ('chat.ChatSessionStore.index',
             json.dumps({'version': 1, 'entries': entries})))
        if archived_ids is not None:
            cache = [{'resource': _session_resource(sid), 'archived': True,
                      'pinned': False, 'read': 1}
                     for sid in archived_ids]
            conn.execute(
                'INSERT INTO ItemTable VALUES (?, ?)',
                ('agentSessions.state.cache', json.dumps(cache)))
        conn.commit()
    finally:
        conn.close()
    chat_dir = d / 'chatSessions'
    chat_dir.mkdir()
    for sid, lines in (sessions_jsonl or {}).items():
        (chat_dir / f'{sid}.jsonl').write_text(
            '\n'.join(json.dumps(ln) if not isinstance(ln, str) else ln
                      for ln in lines))


@pytest.fixture
def storage(tmp_path, monkeypatch):
    root = tmp_path / 'workspaceStorage'
    root.mkdir()
    monkeypatch.setattr(vcs, 'VSCODE_WORKSPACE_STORAGE', root)
    # No git calls in unit tests.
    monkeypatch.setattr(vcs, '_branch_for', lambda folder: 'main')
    return root


def _open_hashes(monkeypatch, hashes):
    monkeypatch.setattr(vcs, '_open_workspace_hashes', lambda: set(hashes))


# ---- Status mapping -----------------------------------------------------


def test_status_generating():
    kind, text = vcs._derive_status({'lastResponseState': 2})
    assert kind == 'running'
    assert 'Running' in text


def test_status_needs_input():
    kind, text = vcs._derive_status({'lastResponseState': 4})
    assert kind == 'unread'
    assert 'Needs input' in text


def test_status_failed_reads_as_idle_kind():
    kind, text = vcs._derive_status({'lastResponseState': 3})
    assert kind == 'idle'
    assert 'Failed' in text


def test_status_complete_and_unknown_default_to_idle():
    assert vcs._derive_status({'lastResponseState': 1})[0] == 'idle'
    assert vcs._derive_status({'lastResponseState': 0})[0] == 'idle'
    assert vcs._derive_status({})[0] == 'idle'


# ---- Visibility rules ---------------------------------------------------


def test_visibility_skips_empty_and_external():
    assert not vcs._is_visible('s', _entry(empty=True), NOW_MS, set())
    assert not vcs._is_visible('s', _entry(external=True), NOW_MS, set())


def test_visibility_recency_window():
    old = NOW_MS - vcs.RECENT_WINDOW_MS - 1000
    assert vcs._is_visible('s', _entry(last_ms=NOW_MS), NOW_MS, set())
    assert not vcs._is_visible('s', _entry(last_ms=old), NOW_MS, set())


def test_visibility_generating_beats_recency():
    old = NOW_MS - vcs.RECENT_WINDOW_MS - 1000
    assert vcs._is_visible('s', _entry(state=2, last_ms=old), NOW_MS, set())


def test_visibility_keep_ids_bypass_recency():
    old = NOW_MS - vcs.RECENT_WINDOW_MS - 1000
    assert vcs._is_visible('s', _entry(last_ms=old), NOW_MS, {'s'})


# ---- Remove-row dismissal (UUID-keyed, auto-returns on new activity) -----


def test_dismissed_when_no_newer_activity():
    # Removed at NOW; last message predates that -> stays removed.
    hidden = {'s': NOW_MS}
    assert vcs._is_dismissed('s', _entry(last_ms=NOW_MS - 5000), hidden)


def test_dismissed_auto_returns_on_new_message():
    # A new user message after the dismiss time -> row returns.
    hidden = {'s': NOW_MS - 1000}
    assert not vcs._is_dismissed('s', _entry(last_ms=NOW_MS), hidden)


def test_dismissed_only_matches_the_exact_uuid():
    # A different session id (e.g. a future same-named chat) is unaffected.
    hidden = {'old-uuid': NOW_MS}
    assert not vcs._is_dismissed('new-uuid', _entry(last_ms=NOW_MS), hidden)


def test_scan_drops_dismissed_session(storage, monkeypatch):
    h = 'd' * 32
    _make_workspace(storage, h, '/repos/app',
                    {'keep': _entry(), 'gone': _entry()})
    _open_hashes(monkeypatch, {h})
    rows = vcs.scan_open_vscode_copilot_sessions(hidden={'gone': NOW_MS + 1000})
    assert [r['chat_id'] for r in rows] == ['keep']


def test_scan_dismissed_beats_tracking(storage, monkeypatch):
    """Removing a TRACKED chat drops it from the scan (so the reconcile can
    synthesize its 'Removed' row) - dismissal wins over the keep_ids bypass."""
    h = 'e' * 32
    _make_workspace(storage, h, '/repos/app', {'t1': _entry()})
    _open_hashes(monkeypatch, {h})
    rows = vcs.scan_open_vscode_copilot_sessions(
        hidden={'t1': NOW_MS + 1000}, keep_ids={'t1'})
    assert rows == []


# ---- Archived-session detection (VS Code's own archive) -----------------


def test_session_id_from_resource_roundtrips():
    sid = 'a7e5d323-3cdf-47d2-9c02-cb3b59fa3078'
    assert vcs._session_id_from_resource(_session_resource(sid)) == sid


def test_session_id_from_resource_ignores_other_schemes():
    # External providers (claude-code:) and junk must not decode.
    assert vcs._session_id_from_resource('claude-code:/abc') is None
    assert vcs._session_id_from_resource('not a uri') is None
    assert vcs._session_id_from_resource(None) is None


def test_archived_session_ids_parses_state_cache(storage):
    h = 'a' * 32
    _make_workspace(storage, h, '/repos/app',
                    {'s1': _entry(), 's2': _entry()},
                    archived_ids=['s2'])
    ws_db = storage / h / 'state.vscdb'
    assert vcs._archived_session_ids(ws_db) == {'s2'}


# ---- Last-user-prompt + context extraction -------------------------------


def _msg_op(texts):
    return {'kind': 2, 'k': ['requests'],
            'v': [{'message': {'text': t}} for t in texts]}


def _last_msg(f):
    return vcs._extract_session_details(f)[0]


def test_last_message_from_init_record(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text(json.dumps(
        {'kind': 0, 'v': {'requests': [{'message': {'text': 'hello world'}}]}}
    ))
    assert _last_msg(f) == 'hello world'


def test_last_message_last_op_wins(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text('\n'.join(json.dumps(op) for op in [
        {'kind': 0, 'v': {'requests': []}},
        _msg_op(['first']),
        _msg_op(['first', 'second']),
    ]))
    assert _last_msg(f) == 'second'


def test_last_message_per_index_op(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text('\n'.join(json.dumps(op) for op in [
        {'kind': 0, 'v': {'requests': [{'message': {'text': 'a'}}]}},
        {'kind': 2, 'k': ['requests', 1], 'v': {'message': {'text': 'b'}}},
    ]))
    assert _last_msg(f) == 'b'


def test_last_message_survives_malformed_lines(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text('\n'.join([
        'not json at all',
        json.dumps({'kind': 2, 'k': ['requests'], 'v': 'wrong shape'}),
        json.dumps(_msg_op(['ok'])),
        '{truncated',
    ]))
    assert _last_msg(f) == 'ok'


def test_last_message_whitespace_collapsed_and_capped(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text(json.dumps(_msg_op(['  multi\n\nline\tprompt  ' + 'x' * 500])))
    out = _last_msg(f)
    assert out.startswith('multi line prompt')
    assert len(out) <= vcs._LASTMSG_MAX_LEN


def test_session_details_missing_file_is_empty(tmp_path):
    assert vcs._session_details(tmp_path / 'nope.jsonl') == ('', None)


def test_session_details_cache_invalidates_on_write(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text(json.dumps(_msg_op(['one'])))
    assert vcs._session_details(f)[0] == 'one'
    f.write_text(json.dumps(_msg_op(['one', 'two longer text here'])))
    assert vcs._session_details(f)[0] == 'two longer text here'


def _model_state(max_input=127790, name='Auto'):
    return {'selectedModel':
            {'metadata': {'name': name, 'id': name.lower(),
                          'maxInputTokens': max_input,
                          'maxOutputTokens': 64000}}}


def _finished_request(prompt_tokens, text='do it'):
    return {'message': {'text': text},
            'result': {'metadata': {'promptTokens': prompt_tokens,
                                    'outputTokens': 36}}}


def test_context_from_init_record(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text(json.dumps(
        {'kind': 0, 'v': {'requests': [_finished_request(15540)],
                          'inputState': _model_state()}}))
    _, ctx = vcs._extract_session_details(f)
    assert ctx == {'used_tokens': 15540, 'window': 127790, 'model': 'Auto'}


def test_context_uses_newest_finished_request(tmp_path):
    # The in-flight last request (no result yet) is skipped; the newest
    # finished one provides the live-context proxy.
    f = tmp_path / 's.jsonl'
    f.write_text(json.dumps(
        {'kind': 0, 'v': {'requests': [_finished_request(10_000),
                                       _finished_request(22_000),
                                       {'message': {'text': 'pending'}}],
                          'inputState': _model_state()}}))
    _, ctx = vcs._extract_session_details(f)
    assert ctx is not None and ctx['used_tokens'] == 22_000


def test_context_none_without_finished_request(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text(json.dumps(
        {'kind': 0, 'v': {'requests': [{'message': {'text': 'pending'}}],
                          'inputState': _model_state()}}))
    assert vcs._extract_session_details(f)[1] is None


def test_context_none_without_model_limit(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text(json.dumps(
        {'kind': 0, 'v': {'requests': [_finished_request(15540)]}}))
    assert vcs._extract_session_details(f)[1] is None


def test_context_model_switch_op_updates_window(tmp_path):
    # A mid-session model change arrives as an inputState patch op; the new
    # model's input limit becomes the window.
    f = tmp_path / 's.jsonl'
    f.write_text('\n'.join(json.dumps(op) for op in [
        {'kind': 0, 'v': {'requests': [_finished_request(15540)],
                          'inputState': _model_state(max_input=127790)}},
        {'kind': 1, 'k': ['inputState'],
         'v': _model_state(max_input=200000, name='GPT-5')},
    ]))
    _, ctx = vcs._extract_session_details(f)
    assert ctx == {'used_tokens': 15540, 'window': 200000, 'model': 'GPT-5'}


def test_context_selected_model_path_op(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text('\n'.join(json.dumps(op) for op in [
        {'kind': 0, 'v': {'requests': [_finished_request(500)]}},
        {'kind': 1, 'k': ['inputState', 'selectedModel'],
         'v': _model_state(max_input=64000)['selectedModel']},
    ]))
    _, ctx = vcs._extract_session_details(f)
    assert ctx is not None and ctx['window'] == 64000


# ---- Full scan ----------------------------------------------------------


def test_scan_row_shape(storage, monkeypatch):
    h = 'a' * 32
    _make_workspace(
        storage, h, '/repos/app',
        {'sid-1': _entry(state=2, title='Fix the bug')},
        {'sid-1': [{'kind': 0,
                    'v': {'requests': [{'message': {'text': 'fix it'}}]}}]},
    )
    _open_hashes(monkeypatch, {h})
    rows = vcs.scan_open_vscode_copilot_sessions()
    assert len(rows) == 1
    row = rows[0]
    assert row['tag'] == 'vscode-gui:sid-1'
    assert row['row_type'] == vcs.VSCODE_GUI_ROW_TYPE
    assert row['display_label'] == 'Fix the bug'
    assert row['project'] == 'app'
    assert row['project_path'] == '/repos/app'
    assert row['window_folder'] == '/repos/app'
    assert row['chat_id'] == 'sid-1'
    assert row['branch'] == 'main'
    assert row['last_msg'] == 'fix it'
    assert row['ide'] == 'VS Code'
    assert row['status_kind'] == 'running'


def test_scan_filters_empty_stale_and_archived(storage, monkeypatch):
    h = 'b' * 32
    old = NOW_MS - vcs.RECENT_WINDOW_MS - 1000
    _make_workspace(storage, h, '/repos/app', {
        'live': _entry(),
        'empty': _entry(empty=True),
        'stale': _entry(last_ms=old),
        'archived': _entry(),
        'tracked-stale': _entry(last_ms=old),
    }, archived_ids=['archived'])
    _open_hashes(monkeypatch, {h})
    rows = vcs.scan_open_vscode_copilot_sessions(keep_ids={'tracked-stale'})
    assert sorted(r['chat_id'] for r in rows) == ['live', 'tracked-stale']


def test_scan_archived_beats_tracking(storage, monkeypatch):
    """Archiving a TRACKED chat must drop it from the scan (so the
    reconcile can synthesize its 'archived' row) - archive wins over the
    keep_ids tracking bypass."""
    h = 'c' * 32
    _make_workspace(storage, h, '/repos/app',
                    {'t1': _entry()}, archived_ids=['t1'])
    _open_hashes(monkeypatch, {h})
    rows = vcs.scan_open_vscode_copilot_sessions(keep_ids={'t1'})
    assert rows == []


def test_scan_no_open_workspaces_returns_empty_and_clears_caches(
        storage, monkeypatch):
    vcs._INDEX_CACHE['leftover'] = {'sig': None, 'entries': {}}
    vcs._ARCHIVED_CACHE['leftover'] = {'sig': None, 'ids': set()}
    vcs._DETAILS_CACHE['leftover'] = (None, ('x', None))
    _open_hashes(monkeypatch, set())
    assert vcs.scan_open_vscode_copilot_sessions() == []
    assert not vcs._INDEX_CACHE
    assert not vcs._ARCHIVED_CACHE
    assert not vcs._DETAILS_CACHE


def test_scan_survives_malformed_index(storage, monkeypatch):
    h = 'c' * 32
    d = storage / h
    d.mkdir(parents=True)
    (d / 'workspace.json').write_text(json.dumps({'folder': 'file:///r/app'}))
    conn = sqlite3.connect(str(d / 'state.vscdb'))
    try:
        conn.execute('CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)')
        conn.execute('INSERT INTO ItemTable VALUES (?, ?)',
                     ('chat.ChatSessionStore.index', '{not json'))
        conn.commit()
    finally:
        conn.close()
    _open_hashes(monkeypatch, {h})
    assert vcs.scan_open_vscode_copilot_sessions() == []


def test_scan_skips_workspace_without_metadata(storage, monkeypatch):
    (storage / ('d' * 32)).mkdir(parents=True)  # no workspace.json/state.vscdb
    _open_hashes(monkeypatch, {'d' * 32})
    assert vcs.scan_open_vscode_copilot_sessions() == []


def test_scan_default_title_for_blank(storage, monkeypatch):
    h = 'e' * 32
    _make_workspace(storage, h, '/repos/app', {'s1': _entry(title='  ')})
    _open_hashes(monkeypatch, {h})
    rows = vcs.scan_open_vscode_copilot_sessions()
    assert rows[0]['display_label'] == 'New Chat'


def test_tag_prefixes_and_row_types_cover_both_editors():
    assert 'cursor-gui:x'.startswith(vcs.GUI_TAG_PREFIXES)
    assert 'vscode-gui:x'.startswith(vcs.GUI_TAG_PREFIXES)
    assert not 'leap-tag'.startswith(vcs.GUI_TAG_PREFIXES)
    assert vcs.VSCODE_GUI_ROW_TYPE in vcs.GUI_ROW_TYPES
    assert vcs.CURSOR_GUI_ROW_TYPE in vcs.GUI_ROW_TYPES


# ---- Shared open-workspace detection helper (deduped with Cursor scan) ----


def test_detect_open_workspace_hashes_uses_passed_cache_and_pids_seam():
    """The shared helper computes via the caller's pids_fn and stamps the
    caller-owned cache, skipping pids_fn entirely on a warm cache - so the
    Cursor and VS Code scanners keep independent TTL windows."""
    import leap.monitor.cursor_gui_scan as cgs
    cache = {'mono': 0.0, 'hashes': set()}
    calls = []

    def fake_pids():
        calls.append(1)
        return []  # empty -> no lsof, result is empty set

    out = cgs._detect_open_workspace_hashes(fake_pids, cache, ttl=999)
    assert out == set()
    assert calls == [1]            # cold cache -> pids_fn consulted
    assert cache['mono'] > 0.0     # cache stamped

    out2 = cgs._detect_open_workspace_hashes(fake_pids, cache, ttl=999)
    assert out2 == set()
    assert calls == [1]            # warm cache -> pids_fn NOT consulted again


def test_vscode_and_cursor_caches_are_distinct_objects():
    """Regression guard for the dedup: the two scanners must not share one
    cache dict (which would let one editor's scan mask the other's)."""
    import leap.monitor.cursor_gui_scan as cgs
    assert vcs._OPENWS_CACHE is not cgs._OPENWS_CACHE
