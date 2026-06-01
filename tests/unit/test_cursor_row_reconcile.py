"""Tests for ``TableBuilderMixin._reconcile_cursor_gui_rows`` — the logic
that lets a PR-tracked Cursor tab survive its tab being closed.

The two close buttons on a Cursor row differ only because of this:
- The Open-cell X closes the tab but KEEPS tracking → on the next scan the
  tab is gone, and this method synthesizes a "tab closed" row so the PR
  stays monitored (like a dead-but-tracked regular row).
- The leftmost X also stops tracking → no synthesis → the row drops.

Called as an unbound method on a tiny stub ``self`` so no Qt is built.
"""

from __future__ import annotations

from typing import Any

from leap.monitor._mixins.table_builder_mixin import TableBuilderMixin

PREFIX = 'cursor-gui:'


class _Stub:
    def __init__(self) -> None:
        self._cursor_gui_rows: list[dict] = []
        self._cursor_row_cache: dict[str, dict] = {}
        self._tracked_tags: set[str] = set()
        self._pr_statuses: dict[str, Any] = {}
        self._pr_widgets: dict[str, Any] = {}
        self._pr_approval_widgets: dict[str, Any] = {}
        self._pr_changed_at: dict[str, Any] = {}
        self._dismissed_pr_new_status: set[str] = set()
        self._cell_cache: dict[Any, Any] = {}

    # No-op stand-ins so _untrack_cursor_pr can run without Qt.
    def _sync_scm_poll_timer(self) -> None:
        pass

    def _update_table(self) -> None:
        pass


def _row(cid: str, **extra: Any) -> dict:
    d = {
        'tag': PREFIX + cid,
        'row_type': 'cursor_agent_gui',
        'composer_id': cid,
        'project': 'app',
        'project_path': '/repos/app',
        'branch': 'main',
        'display_label': f'chat {cid}',
        'status_kind': 'idle',
        'status_text': '○  Idle',
    }
    d.update(extra)
    return d


def _reconcile(stub: _Stub, rows: list[dict]) -> None:
    TableBuilderMixin._reconcile_cursor_gui_rows(stub, rows)


def test_live_rows_passthrough_no_synthesis() -> None:
    s = _Stub()
    rows = [_row('a'), _row('b')]
    _reconcile(s, rows)
    assert [r['tag'] for r in s._cursor_gui_rows] == [PREFIX + 'a', PREFIX + 'b']
    assert all(not r.get('_tab_closed') for r in s._cursor_gui_rows)
    # Cache populated for live rows.
    assert set(s._cursor_row_cache) == {PREFIX + 'a', PREFIX + 'b'}


def test_tracked_tab_close_is_synthesized() -> None:
    s = _Stub()
    # Tab 'a' seen live and tracked.
    _reconcile(s, [_row('a')])
    s._tracked_tags.add(PREFIX + 'a')
    # Next scan: tab 'a' is gone (closed via Open-cell X, tracking kept).
    _reconcile(s, [])
    assert len(s._cursor_gui_rows) == 1
    synth = s._cursor_gui_rows[0]
    assert synth['tag'] == PREFIX + 'a'
    assert synth['_tab_closed'] is True
    assert synth['status_text'] == '○  Tab closed'
    # Still tracked, still cached (so it keeps being shown + polled).
    assert PREFIX + 'a' in s._tracked_tags
    assert PREFIX + 'a' in s._cursor_row_cache
    # The synthesized row preserves the data needed to poll the PR / jump.
    assert synth['project_path'] == '/repos/app'
    assert synth['branch'] == 'main'


def test_manual_tab_close_keeps_only_the_tracked_row() -> None:
    # A tab closed MANUALLY in Cursor simply disappears from the scan -
    # exactly the same input reconcile sees for a Server-X close.  So a
    # PR-tracked tab must survive (synthesized "tab closed" row), while an
    # untracked one is dropped.  The synthesis is scan-driven and doesn't
    # know or care how the tab was closed.
    s = _Stub()
    _reconcile(s, [_row('keep'), _row('drop')])   # both live + cached
    s._tracked_tags.add(PREFIX + 'keep')          # track only one
    # Both tabs closed by hand in Cursor → next scan returns neither.
    _reconcile(s, [])
    tags = {r['tag'] for r in s._cursor_gui_rows}
    assert PREFIX + 'keep' in tags                 # tracked → stays
    synth = next(r for r in s._cursor_gui_rows if r['tag'] == PREFIX + 'keep')
    assert synth['_tab_closed'] is True
    assert PREFIX + 'drop' not in tags             # untracked → dropped
    assert PREFIX + 'keep' in s._tracked_tags       # tracking preserved
    assert PREFIX + 'keep' in s._cursor_row_cache   # still pollable


def test_untracked_tab_close_drops_the_row() -> None:
    s = _Stub()
    _reconcile(s, [_row('a')])  # seen live, NOT tracked
    _reconcile(s, [])           # tab closed
    assert s._cursor_gui_rows == []          # row dropped
    assert s._cursor_row_cache == {}          # cache pruned
    assert PREFIX + 'a' not in s._tracked_tags


def test_untrack_then_close_drops_synthesized_row() -> None:
    # After the leftmost X: _untrack_cursor_pr removes the tag from
    # _tracked_tags AND pops the cache, so the next reconcile must NOT
    # re-synthesize it.
    s = _Stub()
    _reconcile(s, [_row('a')])
    s._tracked_tags.add(PREFIX + 'a')
    _reconcile(s, [])                      # synthesized "tab closed"
    assert len(s._cursor_gui_rows) == 1
    # leftmost X effect: stop tracking + drop cache
    s._tracked_tags.discard(PREFIX + 'a')
    s._cursor_row_cache.pop(PREFIX + 'a', None)
    _reconcile(s, [])
    assert s._cursor_gui_rows == []


def test_reopen_after_close_returns_to_live_row() -> None:
    s = _Stub()
    _reconcile(s, [_row('a')])
    s._tracked_tags.add(PREFIX + 'a')
    _reconcile(s, [])                      # tab closed → synthesized
    assert s._cursor_gui_rows[0]['_tab_closed'] is True
    # Tab reopened (e.g. via the Open button) → live again next scan.
    _reconcile(s, [_row('a')])
    assert len(s._cursor_gui_rows) == 1
    assert not s._cursor_gui_rows[0].get('_tab_closed')


def test_stale_untracked_pr_state_is_pruned() -> None:
    # Leftover PR state for a cursor tag that's neither live nor tracked
    # gets cleaned up; a tracked one is kept.
    s = _Stub()
    s._pr_statuses[PREFIX + 'gone'] = object()
    s._pr_widgets[PREFIX + 'gone'] = object()
    s._cell_cache[(PREFIX + 'gone', 'pr')] = object()
    _reconcile(s, [])
    assert PREFIX + 'gone' not in s._pr_statuses
    assert PREFIX + 'gone' not in s._pr_widgets
    assert (PREFIX + 'gone', 'pr') not in s._cell_cache


def test_non_cursor_state_untouched() -> None:
    # A real leap tag's PR state must never be pruned by cursor reconcile.
    s = _Stub()
    s._pr_statuses['realtag'] = object()
    s._tracked_tags.add('realtag')
    _reconcile(s, [])
    assert 'realtag' in s._pr_statuses
    assert 'realtag' in s._tracked_tags


def _untrack(stub: _Stub, tag: str) -> None:
    TableBuilderMixin._untrack_cursor_pr(stub, tag)


def test_untrack_drops_synthesized_row_immediately() -> None:
    # Untracking a synthesized "tab closed" row must remove it from the
    # overlay right away, not leave it on screen (untracked, with a
    # "Track PR" button) until the next scan reconciles it away.
    s = _Stub()
    live = _row('live')
    closed = _row('closed', _tab_closed=True, status_text='○  Tab closed')
    s._cursor_gui_rows = [live, closed]
    s._tracked_tags.update({PREFIX + 'live', PREFIX + 'closed'})
    _untrack(s, PREFIX + 'closed')
    tags = [r['tag'] for r in s._cursor_gui_rows]
    assert PREFIX + 'closed' not in tags    # synthesized row gone now
    assert PREFIX + 'live' in tags          # live row untouched
    assert PREFIX + 'closed' not in s._tracked_tags


def test_untrack_keeps_live_row_in_overlay() -> None:
    # Untracking a LIVE row (its tab is still open) must keep it in the
    # overlay so it can render its "Track PR" button.
    s = _Stub()
    live = _row('live')  # no _tab_closed flag
    s._cursor_gui_rows = [live]
    s._tracked_tags.add(PREFIX + 'live')
    _untrack(s, PREFIX + 'live')
    assert [r['tag'] for r in s._cursor_gui_rows] == [PREFIX + 'live']
    assert PREFIX + 'live' not in s._tracked_tags
