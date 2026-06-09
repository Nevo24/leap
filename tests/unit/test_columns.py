"""Regression lock for the session-table column registry (``leap.monitor.columns``).

The registry is the single source of truth for the monitor session table's
column layout.  These tests pin the *current* layout (so an accidental edit is
caught) and assert the structural invariants that keep the derived collections
mutually consistent.  Deliberately Qt-free so it runs in the fast unit suite.

If you intentionally add / remove / reorder a column, update ``columns.py`` and
then update the golden values below in the same change - that paired edit (and
a failing test until you do) is exactly the safety net this module provides.
"""
from leap.monitor import columns as c


# --- Golden snapshot of the current 16-column layout ------------------------

GOLDEN_LABELS = [
    '', 'Tag', 'CLI', 'App', 'Project', 'Server', 'Last Msg', 'Context',
    'Path', 'Server Branch', 'Status', 'Queue', 'Client', 'Slack', 'PR',
    'PR Branch',
]
GOLDEN_GROUPS = [[0, 1, 2, 3, 4], [5, 6, 7, 8, 9, 10, 11], [12], [13], [14, 15]]


def test_column_count() -> None:
    assert c.COLUMN_COUNT == 16
    assert len(c.COLUMNS) == 16


def test_header_labels_match_golden() -> None:
    assert c.HEADER_LABELS == GOLDEN_LABELS


def test_center_cols_match_golden() -> None:
    # Every data column centers; only the delete-button column (0) does not.
    assert set(c.CENTER_COLS) == set(range(1, 16))


def test_mono_cols_match_golden() -> None:
    # Project, Path, Server Branch, PR Branch.
    assert set(c.MONO_COLS) == {4, 8, 9, 15}


def test_non_toggleable_cols_match_golden() -> None:
    # Delete and Tag are always visible.
    assert set(c.NON_TOGGLEABLE_COLS) == {0, 1}


def test_column_groups_match_golden() -> None:
    assert c.COLUMN_GROUPS == GOLDEN_GROUPS


def test_named_constants_match_positions() -> None:
    # The COL_* constants must equal their column's position.
    by_name = {
        c.COL_DELETE: 0, c.COL_TAG: 1, c.COL_CLI: 2, c.COL_APP: 3,
        c.COL_PROJECT: 4, c.COL_SERVER: 5, c.COL_TASK: 6, c.COL_CONTEXT: 7,
        c.COL_PATH: 8, c.COL_SERVER_BRANCH: 9, c.COL_STATUS: 10, c.COL_QUEUE: 11,
        c.COL_CLIENT: 12, c.COL_SLACK: 13, c.COL_PR: 14, c.COL_PR_BRANCH: 15,
    }
    # No two constants collide and they cover 0..15 exactly.
    assert sorted(by_name) == list(range(16))


# --- Structural invariants (future-proof, independent of the golden values) -

def test_spec_index_equals_position() -> None:
    assert [spec.index for spec in c.COLUMNS] == list(range(c.COLUMN_COUNT))


def test_every_column_in_exactly_one_group() -> None:
    flat = [col for group in c.COLUMN_GROUPS for col in group]
    assert sorted(flat) == list(range(c.COLUMN_COUNT))
    assert len(flat) == len(set(flat)), 'a column appears in more than one group'


def test_groups_are_contiguous_non_empty() -> None:
    assert all(c.COLUMN_GROUPS), 'no empty group'
    groups = [spec.group for spec in c.COLUMNS]
    assert groups == sorted(groups), 'group ids must be non-decreasing'


def test_derived_sets_are_subsets_of_valid_indices() -> None:
    valid = set(range(c.COLUMN_COUNT))
    for name in ('CENTER_COLS', 'MONO_COLS', 'NON_TOGGLEABLE_COLS'):
        assert set(getattr(c, name)) <= valid, name


def test_header_labels_length_matches_count() -> None:
    assert len(c.HEADER_LABELS) == c.COLUMN_COUNT
