"""Pure-logic tests for the monitor table's row sort modes.

``TableBuilderMixin._sort_for_display`` and its key helpers are
self-contained list transforms: they read only ``self._prefs`` /
``self._row_sort_mode`` / the fire-timestamp dicts / ``self._aliases``
and reorder the combined (sessions + Cursor) row list.  We exercise them
on a tiny stub subclass so no QApplication / widget is constructed.
"""

from __future__ import annotations

from typing import Optional

from leap.cli_providers.registry import get_display_name
from leap.monitor._mixins.table_builder_mixin import TableBuilderMixin
from leap.monitor.cursor_gui_scan import CURSOR_GUI_ROW_TYPE


class _Stub(TableBuilderMixin):
    """Carries just the attributes ``_sort_for_display`` reads.

    Subclasses the mixin so the helper methods (``_recent_activity_ts``,
    ``_category_sort_key``, ``_tag_sort_key``) resolve, but never calls
    ``super().__init__`` - no Qt object is created.
    """

    def __init__(self, mode: str = 'manual', row_order: tuple = (), *,
                 state: Optional[dict] = None, pr: Optional[dict] = None,
                 aliases: Optional[dict] = None, search: str = '',
                 pinned: Optional[dict] = None) -> None:
        self._row_sort_mode = mode
        self._prefs = {'row_order': list(row_order)}
        self._state_changed_at = dict(state or {})
        self._pr_changed_at = dict(pr or {})
        self._aliases = dict(aliases or {})
        self._search_query = search
        self._pinned_sessions = dict(pinned or {})
        self.saved = False

    def _save_prefs(self) -> None:  # no-op: keep the test off disk
        self.saved = True


def _sort(stub: _Stub, combined: list[dict]) -> list[str]:
    return [s['tag'] for s in stub._sort_for_display(combined)]


def _row(tag: str, project: str = 'proj', *,
         ide: str = 'iTerm2', cli: str = 'claude') -> dict:
    return {'tag': tag, 'project': project, 'ide': ide, 'cli_provider': cli}


def _cursor(cid: str, label: str, project: str = 'proj') -> dict:
    return {
        'tag': f'cursor-gui:{cid}',
        'row_type': CURSOR_GUI_ROW_TYPE,
        'display_label': label,
        'project': project,
    }


# ── manual ─────────────────────────────────────────────────────────────

def test_manual_respects_row_order() -> None:
    stub = _Stub('manual', ['b', 'a', 'c'])
    rows = [_row('a'), _row('b'), _row('c')]
    assert _sort(stub, rows) == ['b', 'a', 'c']


def test_manual_appends_unknown_tags_and_persists() -> None:
    stub = _Stub('manual', ['a'])
    rows = [_row('a'), _row('b'), _row('c')]
    # b, c not in row_order -> appended at the end in encounter order
    assert _sort(stub, rows) == ['a', 'b', 'c']
    assert stub._prefs['row_order'] == ['a', 'b', 'c']
    assert stub.saved is True


def test_manual_unknown_tag_appended_even_in_auto_mode() -> None:
    # row_order must stay complete in every mode so switching back to
    # Manual finds a slot for every live tag.
    stub = _Stub('tag', ['a'])
    _sort(stub, [_row('a'), _row('b')])
    assert stub._prefs['row_order'] == ['a', 'b']
    assert stub.saved is True


# ── project ────────────────────────────────────────────────────────────

def test_project_groups_alphabetical_then_manual() -> None:
    stub = _Stub('project', ['z1', 'a1', 'z2', 'a2'])
    rows = [
        _row('z1', project='Zebra'),
        _row('a1', project='alpha'),
        _row('z2', project='Zebra'),
        _row('a2', project='alpha'),
    ]
    # alpha before Zebra (case-insensitive); manual order within a project
    assert _sort(stub, rows) == ['a1', 'a2', 'z1', 'z2']


def test_project_blank_sinks_to_bottom() -> None:
    stub = _Stub('project', ['x', 'y', 'z'])
    rows = [
        _row('x', project=''),
        _row('y', project='beta'),
        _row('z', project='N/A'),
    ]
    # real project first; blank + N/A last, in manual order between them
    assert _sort(stub, rows) == ['y', 'x', 'z']


# ── tag ─────────────────────────────────────────────────────────────────

def test_tag_alphabetical_by_tag() -> None:
    stub = _Stub('tag', ['c', 'b', 'a'])
    rows = [_row('c'), _row('b'), _row('a')]
    assert _sort(stub, rows) == ['a', 'b', 'c']


def test_tag_uses_alias_over_tag() -> None:
    stub = _Stub('tag', ['t1', 't2'], aliases={'t2': 'aaa'})
    rows = [_row('t1'), _row('t2')]   # 'aaa' (alias) < 't1' (tag)
    assert _sort(stub, rows) == ['t2', 't1']


def test_tag_cursor_uses_display_label() -> None:
    stub = _Stub('tag', ['cursor-gui:c1', 'm'])
    rows = [_cursor('c1', 'Zeta chat'), _row('m')]
    # 'm' (tag) < 'zeta chat' (cursor label) -> m first
    assert _sort(stub, rows) == ['m', 'cursor-gui:c1']


# ── recent ──────────────────────────────────────────────────────────────

def test_recent_orders_by_latest_fire_desc() -> None:
    stub = _Stub(
        'recent', ['a', 'b', 'c'],
        state={'a': ('idle', 100.0), 'b': ('idle', 300.0)},
        pr={'c': (('x',), 200.0)},
    )
    rows = [_row('a'), _row('b'), _row('c')]
    assert _sort(stub, rows) == ['b', 'c', 'a']  # 300 > 200 > 100


def test_recent_uses_max_of_state_and_pr() -> None:
    stub = _Stub(
        'recent', ['a', 'b'],
        state={'a': ('idle', 50.0), 'b': ('idle', 250.0)},
        pr={'a': (('x',), 400.0)},   # a's PR fire beats b's state change
    )
    rows = [_row('a'), _row('b')]
    assert _sort(stub, rows) == ['a', 'b']  # max(a)=400 > 250


def test_recent_ties_fall_back_to_manual_order() -> None:
    # Nothing observed yet (all timestamps 0) -> manual order is the order
    stub = _Stub('recent', ['b', 'a'])
    rows = [_row('a'), _row('b')]
    assert _sort(stub, rows) == ['b', 'a']


def test_unknown_mode_falls_back_to_manual() -> None:
    stub = _Stub('bogus', ['b', 'a'])
    rows = [_row('a'), _row('b')]
    assert _sort(stub, rows) == ['b', 'a']


# ── group dividers ───────────────────────────────────────────────────────

def test_boundaries_empty_in_non_grouped_modes() -> None:
    rows = [_row('a', project='X'), _row('b', project='Y')]
    for mode in ('manual', 'recent', 'tag'):
        stub = _Stub(mode)
        assert stub._group_boundaries(rows) == set()


def test_boundaries_mark_each_project_change() -> None:
    # Rows already in project order; a divider starts every new project,
    # never row 0, and never between same-project rows.
    stub = _Stub('project')
    rows = [
        _row('a', project='alpha'),
        _row('b', project='alpha'),
        _row('c', project='beta'),
        _row('d', project='gamma'),
        _row('e', project='gamma'),
    ]
    assert stub._group_boundaries(rows) == {2, 3}


def test_boundaries_treat_blank_and_na_as_one_group() -> None:
    # Blank and 'N/A' share the sort's (1, '') key, so no divider splits
    # them - they form a single trailing group.
    stub = _Stub('project')
    rows = [
        _row('a', project='real'),
        _row('b', project=''),
        _row('c', project='N/A'),
    ]
    assert stub._group_boundaries(rows) == {1}


def test_boundaries_case_insensitive_same_group() -> None:
    # 'MyProj' and 'myproj' casefold to one sort group -> no divider.
    stub = _Stub('project')
    rows = [_row('a', project='MyProj'), _row('b', project='myproj')]
    assert stub._group_boundaries(rows) == set()


# ── app ──────────────────────────────────────────────────────────────────

def test_app_groups_alphabetical_then_manual() -> None:
    stub = _Stub('app', ['w1', 'i1', 'w2', 'i2'])
    rows = [
        _row('w1', ide='WezTerm'),
        _row('i1', ide='iTerm2'),
        _row('w2', ide='WezTerm'),
        _row('i2', ide='iTerm2'),
    ]
    # 'iterm2' < 'wezterm' (case-insensitive); manual order within an app
    assert _sort(stub, rows) == ['i1', 'i2', 'w1', 'w2']


def test_app_blank_sinks_to_bottom() -> None:
    stub = _Stub('app', ['x', 'y'])
    rows = [_row('x', ide=''), _row('y', ide='Ghostty')]
    assert _sort(stub, rows) == ['y', 'x']


def test_app_boundaries_mark_each_change() -> None:
    stub = _Stub('app')
    rows = [
        _row('a', ide='iTerm2'),
        _row('b', ide='iTerm2'),   # same app -> no divider
        _row('c', ide='WezTerm'),  # new app -> divider
    ]
    assert stub._group_boundaries(rows) == {2}


# ── cli ──────────────────────────────────────────────────────────────────

def test_cli_value_matches_visible_column() -> None:
    # The grouping value must equal what the CLI column shows.
    stub = _Stub('cli', pinned={'d': {'cli_provider': 'codex'}})
    assert stub._category_value(_row('a', cli='claude'), 'cli') == 'Claude Code'
    assert stub._category_value(_cursor('c1', 'x'), 'cli') == 'Cursor Editor'
    # Dead row (no cli_provider) falls back to the pinned provider.
    assert stub._category_value({'tag': 'd'}, 'cli') == 'OpenAI Codex'
    # Truly unknown -> blank (bottom group).
    assert stub._category_value({'tag': 'z'}, 'cli') == ''


def test_cli_groups_by_display_name_then_manual() -> None:
    stub = _Stub('cli', ['a', 'b', 'c'])
    rows = [
        _row('a', cli='claude'),   # Claude Code
        _row('b', cli='gemini'),   # Gemini CLI
        _row('c', cli='claude'),   # Claude Code
    ]
    # 'claude code' < 'gemini cli' -> claude group (a, c) first, manual order
    assert _sort(stub, rows) == ['a', 'c', 'b']


def test_cli_boundaries_mark_each_change() -> None:
    stub = _Stub('cli')
    rows = [
        _row('a', cli='claude'),
        _row('c', cli='claude'),   # same CLI -> no divider
        _row('b', cli='gemini'),   # new CLI -> divider
    ]
    assert stub._group_boundaries(rows) == {2}


# ── filter x sort interaction ───────────────────────────────────────────

def _filtered(stub: _Stub, rows: list[dict]) -> list[str]:
    return [s['tag'] for s in stub._apply_search_filter(rows)]


def test_filter_preserves_sort_order_in_auto_modes() -> None:
    # In a non-manual mode the filter must NOT re-bucket by match field;
    # it keeps the incoming (sorted) order and only drops non-matches.
    rows = [
        _row('a', project='svc-1'),       # project match
        _row('svc-x', project='other'),   # tag match
        _row('b', project='other'),       # no match -> dropped
        _row('svc-y', project='svc-2'),   # tag match
    ]
    for mode in ('recent', 'project', 'tag'):
        stub = _Stub(mode, search='svc')
        # Order preserved (a, svc-x, svc-y); 'b' dropped.  Bucketing would
        # have hoisted the tag matches (svc-x, svc-y) above project-match a.
        assert _filtered(stub, rows) == ['a', 'svc-x', 'svc-y'], mode


def test_filter_buckets_in_manual_mode() -> None:
    # Manual mode keeps the Resume-style relevance bucketing: a tag match
    # is hoisted above an earlier project match.
    stub = _Stub('manual', search='svc')
    rows = [
        _row('a', project='svc-1'),       # project match (rank 1)
        _row('svc-x', project='other'),   # tag match (rank 0)
    ]
    assert _filtered(stub, rows) == ['svc-x', 'a']


def test_filter_project_dividers_stay_clean_under_filter() -> None:
    # The regression: in Project mode + filter the dividers must still mark
    # contiguous project groups (because the filter no longer reorders).
    stub = _Stub('project', search='x')
    rows = [
        _row('a', project='alpha'),   # 'x' not in tag/project... dropped
        _row('bx', project='beta'),   # tag match
        _row('cx', project='beta'),   # tag match (same project -> no split)
        _row('dx', project='gamma'),  # tag match (new project)
    ]
    visible = stub._apply_search_filter(rows)
    assert [s['tag'] for s in visible] == ['bx', 'cx', 'dx']
    # beta (rows 0,1) then gamma (row 2): one divider, at the gamma row.
    assert stub._group_boundaries(visible) == {2}
