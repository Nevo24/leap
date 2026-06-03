"""Pure-logic tests for the monitor's live search/filter.

``TableBuilderMixin._apply_search_filter`` is a self-contained list
transform: it reads only ``self._search_query`` and ``self._aliases`` and
returns the filtered/bucketed session list.  We exercise it on a tiny stub
``self`` so no QApplication / widget is constructed.
"""

from __future__ import annotations

from leap.monitor._mixins.table_builder_mixin import TableBuilderMixin


class _Stub(TableBuilderMixin):
    # Subclasses the mixin so the helper it now delegates to
    # (``_row_match_rank``) resolves.  No ``_row_sort_mode`` is set, so
    # ``_apply_search_filter`` takes the default 'manual' path (relevance
    # bucketing) - which is exactly what these tests assert.
    def __init__(self, query: str, aliases: dict | None = None) -> None:
        self._search_query = query
        self._aliases = aliases or {}


def _filter(query: str, sessions: list[dict],
            aliases: dict | None = None) -> list[dict]:
    return TableBuilderMixin._apply_search_filter(_Stub(query, aliases), sessions)


def _cursor_row(composer_id: str, label: str, project: str = 'app',
                path: str = '/repos/app') -> dict:
    return {
        'tag': f'cursor-gui:{composer_id}',
        'row_type': 'cursor_agent_gui',
        'display_label': label,
        'project': project,
        'project_path': path,
        'ide': 'Cursor',
        'cli_provider': 'cursor-gui',
    }


def _leap_row(tag: str, project: str = 'svc', path: str = '/repos/svc',
              cli: str = 'claude') -> dict:
    return {
        'tag': tag,
        'project': project,
        'project_path': path,
        'ide': 'iTerm2',
        'cli_provider': cli,
    }


def test_empty_query_returns_all_unchanged() -> None:
    rows = [_leap_row('a'), _cursor_row('c1', 'Hello')]
    assert _filter('', rows) == rows


def test_cursor_row_matched_by_visible_chat_name() -> None:
    # The Tag column shows display_label ("Refactor auth"), not the raw
    # cursor-gui:<id> tag, so filtering by the visible name must hit.
    rows = [_cursor_row('c1', 'Refactor auth flow')]
    assert _filter('refactor', rows) == rows
    assert _filter('AUTH', rows) == rows  # case-insensitive
    assert _filter('nomatch', rows) == []


def test_cursor_row_still_matched_by_raw_tag_and_project() -> None:
    rows = [_cursor_row('deadbeef', 'Some chat', project='myproj')]
    assert _filter('deadbeef', rows) == rows   # raw composer id
    assert _filter('myproj', rows) == rows      # project basename


def test_cursor_row_matched_by_alias() -> None:
    row = _cursor_row('c1', 'Some chat')
    rows = [row]
    aliases = {'cursor-gui:c1': 'My Pinned Chat'}
    assert _filter('pinned', rows, aliases) == rows


def test_normal_row_without_display_label_unaffected() -> None:
    # Normal rows carry no display_label; widening the Tag match must not
    # change their behaviour (matched by tag / project / path as before).
    rows = [_leap_row('mytag', project='backend', path='/repos/backend')]
    assert _filter('mytag', rows) == rows
    assert _filter('backend', rows) == rows
    assert _filter('zzz', rows) == []


def test_chat_name_does_not_leak_into_other_rows() -> None:
    # A query that matches one cursor row's chat name must not pull in an
    # unrelated leap row that has no such field.
    c = _cursor_row('c1', 'Unique Banana Name', project='app', path='/r/app')
    other = _leap_row('svc', project='svc', path='/r/svc')
    out = _filter('banana', [c, other])
    assert out == [c]
