"""Unit tests for the Cursor editor Agent-tab scanner.

Pure-logic + fixture-SQLite tests (no GUI, no live Cursor).  They pin
the status mapping, URI parsing, row shape, the read-only SQLite read,
defensive handling of malformed blobs, and the window-title -> folder
join performed by ``scan_open_cursor_agents``.
"""

import json
import sqlite3

import pytest

from leap.monitor import cursor_gui_scan as cgs


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset the module's signature caches between tests."""
    cgs._GLOBAL_CACHE['sig'] = None
    cgs._GLOBAL_CACHE['data'] = {}
    cgs._WS_CACHE.clear()
    cgs._BRANCH_CACHE.clear()
    cgs._LASTMSG_CACHE.clear()
    cgs._OPENWS_CACHE['mono'] = 0.0
    cgs._OPENWS_CACHE['hashes'] = set()
    yield
    cgs._GLOBAL_CACHE['sig'] = None
    cgs._GLOBAL_CACHE['data'] = {}
    cgs._WS_CACHE.clear()
    cgs._BRANCH_CACHE.clear()
    cgs._LASTMSG_CACHE.clear()
    cgs._OPENWS_CACHE['mono'] = 0.0
    cgs._OPENWS_CACHE['hashes'] = set()


# ---- fixture db builders ----------------------------------------------


def _make_global_db(path, composers: dict) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            'CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)')
        for cid, rec in composers.items():
            value = rec if isinstance(rec, str) else json.dumps(rec)
            conn.execute('INSERT INTO cursorDiskKV VALUES (?, ?)',
                         (f'composerData:{cid}', value))
        conn.commit()
    finally:
        conn.close()


def _make_ws_db(path, selected_ids: list) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            'CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)')
        conn.execute(
            'INSERT INTO ItemTable VALUES (?, ?)',
            ('composer.composerData',
             json.dumps({'selectedComposerIds': selected_ids})))
        conn.commit()
    finally:
        conn.close()


# ---- _derive_status ----------------------------------------------------


def test_status_running_when_generating_bubbles():
    rec = {'generatingBubbleIds': ['b1'], 'hasUnreadMessages': True,
           'status': 'completed'}
    kind, text = cgs._derive_status(rec)
    assert kind == 'running'
    assert 'Running' in text


def test_status_running_when_status_generating():
    # status=='generating' even with no in-flight bubble id yet.
    kind, _ = cgs._derive_status({'generatingBubbleIds': [],
                                  'status': 'generating'})
    assert kind == 'running'


def test_status_unread_when_marked_unread():
    # hasUnreadMessages is Cursor's manual "Mark as unread" flag -> Unread
    # (not "Replied"), and it wins over a finished ('aborted') status.
    rec = {'generatingBubbleIds': [], 'hasUnreadMessages': True,
           'status': 'aborted'}
    kind, text = cgs._derive_status(rec)
    assert kind == 'unread'
    assert 'Unread' in text


def test_status_running_wins_over_unread():
    rec = {'generatingBubbleIds': ['b1'], 'hasUnreadMessages': True}
    assert cgs._derive_status(rec)[0] == 'running'


def test_status_aborted_reads_as_idle_not_aborted():
    # Cursor sets status='aborted' when a generation ENDS (including
    # normal completion), so it must NOT show a scary "Aborted" - it's
    # just a finished/idle chat.
    kind, text = cgs._derive_status({'status': 'aborted'})
    assert kind == 'idle'
    assert 'Idle' in text
    # 'completed' likewise maps to idle.
    assert cgs._derive_status({'status': 'completed'})[0] == 'idle'


def test_status_idle_default():
    kind, _ = cgs._derive_status({'status': 'none'})
    assert kind == 'idle'
    assert cgs._derive_status({})[0] == 'idle'


# ---- _uri_to_path ------------------------------------------------------


def test_uri_to_path_file_scheme():
    assert cgs._uri_to_path('file:///Users/x/ai-workflows_1') == \
        '/Users/x/ai-workflows_1'


def test_uri_to_path_percent_decoding():
    assert cgs._uri_to_path('file:///Users/x/My%20Repo') == '/Users/x/My Repo'


def test_uri_to_path_rejects_non_path():
    assert cgs._uri_to_path('vscode-remote://foo') == ''
    assert cgs._uri_to_path('') == ''
    assert cgs._uri_to_path(None) == ''  # type: ignore[arg-type]


def test_uri_to_path_strips_authority():
    # ``file://<authority>/path`` must yield the path, not a bogus
    # ``authority/path`` relative string (which would slip past the
    # empty-basename guard and make us run git in a nonexistent dir).
    assert cgs._uri_to_path('file://localhost/Users/x/proj') == \
        '/Users/x/proj'


def test_uri_to_path_authority_with_percent_decoding():
    assert cgs._uri_to_path('file://localhost/Users/x/My%20Repo') == \
        '/Users/x/My Repo'


def test_uri_to_path_bare_absolute_path_tolerated():
    assert cgs._uri_to_path('/Users/x/proj') == '/Users/x/proj'


# ---- _build_row --------------------------------------------------------


def test_build_row_shape_and_label():
    rec = {'name': 'Fix login bug', 'status': 'none', 'createdAt': 123}
    row = cgs._build_row('/Users/x/proj', 'proj', 'abc-123', rec)
    assert row['tag'] == 'cursor-gui:abc-123'
    assert row['row_type'] == cgs.CURSOR_GUI_ROW_TYPE
    assert row['display_label'] == 'Fix login bug'  # name only, no project prefix
    assert row['project'] == 'proj'
    assert row['project_path'] == '/Users/x/proj'
    assert row['cursor_window_folder'] == '/Users/x/proj'
    assert row['ide'] == 'Cursor'
    assert row['composer_id'] == 'abc-123'
    assert row['server_pid'] is None
    assert row['status_kind'] == 'idle'


def test_build_row_name_fallback():
    for bad in (None, '', '   '):
        row = cgs._build_row('/p/proj', 'proj', 'id1', {'name': bad})
        assert row['composer_name'] == 'New Agent'
        assert row['display_label'] == 'New Agent'


# ---- _query_ro (read-only SQLite) -------------------------------------


def test_query_ro_reads_fixture_db(tmp_path):
    db = tmp_path / 'state.vscdb'
    _make_ws_db(db, ['x', 'y'])
    rows = cgs._query_ro(
        db, "SELECT value FROM ItemTable WHERE key='composer.composerData'")
    assert len(rows) == 1
    data = json.loads(rows[0][0])
    assert data['selectedComposerIds'] == ['x', 'y']


def test_query_ro_missing_file_returns_empty(tmp_path):
    assert cgs._query_ro(tmp_path / 'nope.vscdb', 'SELECT 1') == []


# ---- _global_composers: malformed blobs skipped -----------------------


def test_global_composers_skips_malformed(tmp_path, monkeypatch):
    gdb = tmp_path / 'global.vscdb'
    _make_global_db(gdb, {
        'good': {'name': 'A', 'status': 'none'},
        'bad': '{not valid json',
        'empty-state': {'name': 'ignored'},
    })
    monkeypatch.setattr(cgs, 'GLOBAL_DB', gdb)
    data = cgs._global_composers()
    assert 'good' in data
    assert 'bad' not in data           # malformed JSON dropped
    assert 'empty-state' not in data   # sentinel key dropped
    assert data['good']['name'] == 'A'


# ---- open-workspace detection (lsof hashes) ---------------------------


def test_open_workspace_hashes_parses_lsof(monkeypatch):
    import subprocess as _sp
    lsof_out = (
        "Cursor  1301 user  txt  REG  1,2  100  /Applications/Cursor.app/x\n"
        "Cursor  1301 user   30u REG  1,2  4096 "
        "/Users/u/Library/Application Support/Cursor/User/workspaceStorage/"
        "0129906297d0bd1f8fc39ec6e5c9cd71/state.vscdb\n"
        "Cursor 70583 user   31u REG  1,2  4096 "
        "/Users/u/Library/Application Support/Cursor/User/workspaceStorage/"
        "6c770be8459f162656229f8cb8268025/state.vscdb-wal\n"
    )
    monkeypatch.setattr(cgs, '_cursor_pids', lambda: ['1301', '70583'])
    monkeypatch.setattr(
        _sp, 'run',
        lambda *a, **k: type('R', (), {'stdout': lsof_out, 'returncode': 1})())
    hashes = cgs._open_workspace_hashes()
    assert hashes == {'0129906297d0bd1f8fc39ec6e5c9cd71',
                      '6c770be8459f162656229f8cb8268025'}


def test_open_workspace_hashes_empty_when_no_cursor(monkeypatch):
    monkeypatch.setattr(cgs, '_cursor_pids', lambda: [])
    assert cgs._open_workspace_hashes() == set()


# ---- last-message extraction ------------------------------------------


def _make_kv_db(path, items: dict) -> None:
    """Build a cursorDiskKV db from a {key: value} map (values json-encoded
    unless already strings)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            'CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)')
        for k, v in items.items():
            conn.execute('INSERT INTO cursorDiskKV VALUES (?, ?)',
                         (k, v if isinstance(v, str) else json.dumps(v)))
        conn.commit()
    finally:
        conn.close()


def test_last_message_prefers_most_recent_user(tmp_path, monkeypatch):
    cid = 'comp1'
    rec = {'fullConversationHeadersOnly': [
        {'bubbleId': 'b1', 'type': 1, 'grouping': {'hasText': True}},
        {'bubbleId': 'b2', 'type': 1, 'grouping': {'hasText': True}},
        {'bubbleId': 'b3', 'type': 2, 'grouping': {'hasText': True}},  # AI, newest
    ]}
    db = tmp_path / 'g.vscdb'
    _make_kv_db(db, {
        f'composerData:{cid}': rec,
        f'bubbleId:{cid}:b1': {'text': 'first prompt'},
        f'bubbleId:{cid}:b2': {'text': 'are u there?'},   # most recent user
        f'bubbleId:{cid}:b3': {'text': 'yes I am here'},  # AI response
    })
    monkeypatch.setattr(cgs, 'GLOBAL_DB', db)
    # newest USER message wins, not the newer AI bubble
    assert cgs._extract_last_message(cid, rec) == 'are u there?'


def test_last_message_falls_back_to_newest_with_text(tmp_path, monkeypatch):
    cid = 'comp2'
    rec = {'fullConversationHeadersOnly': [
        {'bubbleId': 'b1', 'type': 2, 'grouping': {'hasText': True}},
        {'bubbleId': 'b2', 'type': 2, 'grouping': {'hasText': True}},  # newest, AI
    ]}
    db = tmp_path / 'g.vscdb'
    _make_kv_db(db, {
        f'bubbleId:{cid}:b1': {'text': 'older'},
        f'bubbleId:{cid}:b2': {'text': 'newest ai line'},
    })
    monkeypatch.setattr(cgs, 'GLOBAL_DB', db)
    assert cgs._extract_last_message(cid, rec) == 'newest ai line'


def test_last_message_empty_when_no_headers():
    assert cgs._extract_last_message('x', {}) == ''
    assert cgs._extract_last_message('x', {'fullConversationHeadersOnly': []}) == ''


def test_last_message_collapses_whitespace_and_truncates(tmp_path, monkeypatch):
    cid = 'comp3'
    rec = {'fullConversationHeadersOnly': [
        {'bubbleId': 'b1', 'type': 1, 'grouping': {'hasText': True}}]}
    db = tmp_path / 'g.vscdb'
    _make_kv_db(db, {f'bubbleId:{cid}:b1': {'text': 'line one\n\n  line  two   ' + 'x' * 500}})
    monkeypatch.setattr(cgs, 'GLOBAL_DB', db)
    out = cgs._extract_last_message(cid, rec)
    assert '\n' not in out and '  ' not in out
    assert len(out) <= cgs._LASTMSG_MAX


# ---- scan_open_cursor_agents: end-to-end with fixtures ----------------


def _setup_workspace(tmp_path, monkeypatch, *, folder_name, selected_ids,
                     composers, open_hashes):
    # Workspace storage dir name is the "hash"; the lsof-based detector
    # returns the set of open hashes.
    ws_storage = tmp_path / 'workspaceStorage'
    hash_dir = ws_storage / 'hash1'
    hash_dir.mkdir(parents=True)
    folder = tmp_path / folder_name
    folder.mkdir()
    (hash_dir / 'workspace.json').write_text(
        json.dumps({'folder': f'file://{folder}'}))
    _make_ws_db(hash_dir / 'state.vscdb', selected_ids)

    gdir = tmp_path / 'globalStorage'
    gdir.mkdir()
    gdb = gdir / 'state.vscdb'
    _make_global_db(gdb, composers)

    monkeypatch.setattr(cgs, 'WORKSPACE_STORAGE', ws_storage)
    monkeypatch.setattr(cgs, 'GLOBAL_DB', gdb)
    monkeypatch.setattr(cgs, '_open_workspace_hashes', lambda: set(open_hashes))
    return str(folder)


def test_scan_builds_rows_for_open_workspace(tmp_path, monkeypatch):
    folder = _setup_workspace(
        tmp_path, monkeypatch,
        folder_name='ai-workflows_1',
        selected_ids=['c1', 'c2'],
        composers={
            'c1': {'name': 'Tab name change', 'status': 'aborted'},
            'c2': {'name': None, 'generatingBubbleIds': ['z']},
            'c3': {'name': 'closed tab', 'status': 'none'},  # not selected
        },
        open_hashes={'hash1'},
    )
    rows = cgs.scan_open_cursor_agents()
    assert len(rows) == 2  # only the two selected/open tabs
    by_id = {r['composer_id']: r for r in rows}
    assert by_id['c1']['status_kind'] == 'idle'  # 'aborted' = done -> idle
    assert by_id['c1']['display_label'] == 'Tab name change'
    assert by_id['c1']['project'] == 'ai-workflows_1'
    assert by_id['c2']['status_kind'] == 'running'
    assert by_id['c2']['display_label'] == 'New Agent'
    assert all(r['project_path'] == folder for r in rows)


def test_scan_returns_empty_when_no_workspace_open(tmp_path, monkeypatch):
    _setup_workspace(
        tmp_path, monkeypatch,
        folder_name='proj', selected_ids=['c1'],
        composers={'c1': {'name': 'X'}},
        open_hashes=set(),  # Cursor not running / no open workspace
    )
    assert cgs.scan_open_cursor_agents() == []


def test_scan_skips_workspace_not_held_open(tmp_path, monkeypatch):
    _setup_workspace(
        tmp_path, monkeypatch,
        folder_name='proj-a', selected_ids=['c1'],
        composers={'c1': {'name': 'X'}},
        open_hashes={'some-other-hash'},  # our 'hash1' isn't open
    )
    assert cgs.scan_open_cursor_agents() == []


def test_scan_skips_composer_missing_from_global(tmp_path, monkeypatch):
    _setup_workspace(
        tmp_path, monkeypatch,
        folder_name='proj', selected_ids=['c1', 'ghost'],
        composers={'c1': {'name': 'X'}},  # 'ghost' not present
        open_hashes={'hash1'},
    )
    rows = cgs.scan_open_cursor_agents()
    assert [r['composer_id'] for r in rows] == ['c1']


def test_scan_dedupes_composer_shared_across_workspaces(tmp_path, monkeypatch):
    # Cursor lists the blank default composer ('shared') in BOTH windows'
    # selectedComposerIds.  It must render as ONE row, attributed to the
    # busier workspace (the one with more open tabs).
    ws_storage = tmp_path / 'workspaceStorage'
    # home: only the shared blank composer
    (ws_storage / 'home').mkdir(parents=True)
    home_folder = tmp_path / 'home_dir'
    home_folder.mkdir()
    (ws_storage / 'home' / 'workspace.json').write_text(
        json.dumps({'folder': f'file://{home_folder}'}))
    _make_ws_db(ws_storage / 'home' / 'state.vscdb', ['shared'])
    # project: the shared composer PLUS a real one
    (ws_storage / 'proj').mkdir(parents=True)
    proj_folder = tmp_path / 'ai-workflows_1'
    proj_folder.mkdir()
    (ws_storage / 'proj' / 'workspace.json').write_text(
        json.dumps({'folder': f'file://{proj_folder}'}))
    _make_ws_db(ws_storage / 'proj' / 'state.vscdb', ['shared', 'real'])

    gdir = tmp_path / 'globalStorage'
    gdir.mkdir()
    gdb = gdir / 'state.vscdb'
    _make_global_db(gdb, {
        'shared': {'name': None},               # blank New Agent
        'real': {'name': 'Availability check'},
    })
    monkeypatch.setattr(cgs, 'WORKSPACE_STORAGE', ws_storage)
    monkeypatch.setattr(cgs, 'GLOBAL_DB', gdb)
    monkeypatch.setattr(cgs, '_open_workspace_hashes', lambda: {'home', 'proj'})

    rows = cgs.scan_open_cursor_agents()
    # 'shared' appears once (not twice), under the busier workspace.
    ids = [r['composer_id'] for r in rows]
    assert ids.count('shared') == 1
    assert sorted(ids) == ['real', 'shared']
    shared_row = next(r for r in rows if r['composer_id'] == 'shared')
    assert shared_row['project'] == 'ai-workflows_1'  # busier workspace wins
