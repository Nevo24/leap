"""Unit tests for the VS Code Copilot Chat session scanner.

Pure-logic + fixture-SQLite tests (no GUI, no live VS Code).  They pin
the ``lastResponseState`` status mapping, the visibility rules (empty /
external / recency / hidden / keep-ids), the last-user-prompt op-log
parsing, the row shape, and defensive handling of malformed state.
"""

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
    vcs._LASTMSG_CACHE.clear()
    vcs._OPENWS_CACHE['mono'] = 0.0
    vcs._OPENWS_CACHE['hashes'] = set()
    yield
    vcs._INDEX_CACHE.clear()
    vcs._LASTMSG_CACHE.clear()
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


def _make_workspace(root: Path, ws_hash: str, folder: str,
                    entries: dict, sessions_jsonl: dict = None) -> None:
    """Build a fixture workspaceStorage/<hash>/ dir: workspace.json +
    state.vscdb with the chat-session index + chatSessions/*.jsonl."""
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
    assert not vcs._is_visible('s', _entry(empty=True), NOW_MS, {}, set())
    assert not vcs._is_visible('s', _entry(external=True), NOW_MS, {}, set())


def test_visibility_recency_window():
    old = NOW_MS - vcs.RECENT_WINDOW_MS - 1000
    assert vcs._is_visible('s', _entry(last_ms=NOW_MS), NOW_MS, {}, set())
    assert not vcs._is_visible('s', _entry(last_ms=old), NOW_MS, {}, set())


def test_visibility_generating_beats_recency():
    old = NOW_MS - vcs.RECENT_WINDOW_MS - 1000
    assert vcs._is_visible('s', _entry(state=2, last_ms=old),
                           NOW_MS, {}, set())


def test_visibility_keep_ids_bypass_recency():
    old = NOW_MS - vcs.RECENT_WINDOW_MS - 1000
    assert vcs._is_visible('s', _entry(last_ms=old), NOW_MS, {}, {'s'})


def test_visibility_hidden_beats_tracking():
    # Hiding a TRACKED chat must drop it from the scan: that's what lets
    # the reconcile synthesize its "Chat hidden" row (which keeps the PR
    # polled) instead of the live row sticking around as if nothing
    # happened.
    old = NOW_MS - vcs.RECENT_WINDOW_MS - 1000
    assert not vcs._is_visible('s', _entry(last_ms=old), NOW_MS,
                               {'s': NOW_MS}, {'s'})


def test_visibility_hidden_until_new_activity():
    hidden = {'s': NOW_MS - 1000}
    # Last activity BEFORE the dismissal → stays hidden.
    assert not vcs._is_visible('s', _entry(last_ms=NOW_MS - 5000),
                               NOW_MS, hidden, set())
    # New user message AFTER the dismissal → auto-unhides.
    assert vcs._is_visible('s', _entry(last_ms=NOW_MS), NOW_MS,
                           hidden, set())


def test_visibility_hidden_beats_generating():
    # Dismissing a chat mid-generation keeps it hidden until the user
    # sends a NEW prompt (lastMessageDate is the last user message).
    hidden = {'s': NOW_MS}
    assert not vcs._is_visible('s', _entry(state=2, last_ms=NOW_MS - 1000),
                               NOW_MS, hidden, set())


# ---- Last-user-prompt extraction ----------------------------------------


def _msg_op(texts):
    return {'kind': 2, 'k': ['requests'],
            'v': [{'message': {'text': t}} for t in texts]}


def test_last_message_from_init_record(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text(json.dumps(
        {'kind': 0, 'v': {'requests': [{'message': {'text': 'hello world'}}]}}
    ))
    assert vcs._extract_last_message(f) == 'hello world'


def test_last_message_last_op_wins(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text('\n'.join(json.dumps(op) for op in [
        {'kind': 0, 'v': {'requests': []}},
        _msg_op(['first']),
        _msg_op(['first', 'second']),
    ]))
    assert vcs._extract_last_message(f) == 'second'


def test_last_message_per_index_op(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text('\n'.join(json.dumps(op) for op in [
        {'kind': 0, 'v': {'requests': [{'message': {'text': 'a'}}]}},
        {'kind': 2, 'k': ['requests', 1], 'v': {'message': {'text': 'b'}}},
    ]))
    assert vcs._extract_last_message(f) == 'b'


def test_last_message_survives_malformed_lines(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text('\n'.join([
        'not json at all',
        json.dumps({'kind': 2, 'k': ['requests'], 'v': 'wrong shape'}),
        json.dumps(_msg_op(['ok'])),
        '{truncated',
    ]))
    assert vcs._extract_last_message(f) == 'ok'


def test_last_message_whitespace_collapsed_and_capped(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text(json.dumps(_msg_op(['  multi\n\nline\tprompt  ' + 'x' * 500])))
    out = vcs._extract_last_message(f)
    assert out.startswith('multi line prompt')
    assert len(out) <= vcs._LASTMSG_MAX_LEN


def test_last_message_missing_file_is_empty(tmp_path):
    assert vcs._last_message(tmp_path / 'nope.jsonl') == ''


def test_last_message_cache_invalidates_on_write(tmp_path):
    f = tmp_path / 's.jsonl'
    f.write_text(json.dumps(_msg_op(['one'])))
    assert vcs._last_message(f) == 'one'
    f.write_text(json.dumps(_msg_op(['one', 'two longer text here'])))
    assert vcs._last_message(f) == 'two longer text here'


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


def test_scan_filters_empty_stale_and_hidden(storage, monkeypatch):
    h = 'b' * 32
    old = NOW_MS - vcs.RECENT_WINDOW_MS - 1000
    _make_workspace(storage, h, '/repos/app', {
        'live': _entry(),
        'empty': _entry(empty=True),
        'stale': _entry(last_ms=old),
        'dismissed': _entry(),
        'tracked-stale': _entry(last_ms=old),
    })
    _open_hashes(monkeypatch, {h})
    rows = vcs.scan_open_vscode_copilot_sessions(
        hidden={'dismissed': NOW_MS + 1000},
        keep_ids={'tracked-stale'},
    )
    assert sorted(r['chat_id'] for r in rows) == ['live', 'tracked-stale']


def test_scan_no_open_workspaces_returns_empty_and_clears_caches(
        storage, monkeypatch):
    vcs._INDEX_CACHE['leftover'] = {'sig': None, 'entries': {}}
    vcs._LASTMSG_CACHE['leftover'] = (None, 'x')
    _open_hashes(monkeypatch, set())
    assert vcs.scan_open_vscode_copilot_sessions() == []
    assert not vcs._INDEX_CACHE
    assert not vcs._LASTMSG_CACHE


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
