"""Single source of truth for the monitor session-table columns.

Historically the session table's column layout was encoded as magic numbers in
several places that had to be hand-synchronized: the ``COL_*`` indices and
``_HEADER_LABELS`` / ``_NON_TOGGLEABLE_COLS`` in ``app.py``, ``_CENTER_COLS`` /
``_MONO_COLS`` in ``_mixins/table_builder_mixin.py``, and ``COLUMN_GROUPS`` in
``ui/table_helpers.py``.  Adding, removing, or reordering a column meant editing
all of them in lockstep, with no error if one drifted - the failure modes were
silent (off-by-one group separators, wrong alignment or font on a column).

Everything is now derived from the single ordered :data:`COLUMNS` table below.
To add / remove / reorder a column, edit ONLY that table; ``app.py``,
``table_builder_mixin.py`` and ``table_helpers.py`` import the derived values
from here.  ``MonitorWindow`` re-exports the ``COL_*`` indices as class
attributes so the long-standing ``self.COL_*`` access keeps working unchanged.

Indices are plain ``int`` (not ``IntEnum``) so they behave byte-for-byte like
the historical inline definitions everywhere they are used - table indexing,
``frozenset`` membership, label-keyed persistence, f-string formatting.

This module is a leaf: it imports nothing from ``leap.monitor`` (or Qt), so any
monitor module can import it without risking a circular import.
"""
from __future__ import annotations

from typing import NamedTuple


class ColumnSpec(NamedTuple):
    """One session-table column.

    ``index`` must equal the column's position in :data:`COLUMNS` (asserted at
    import time).  ``group`` is the vertical-separator group; groups must be
    contiguous, non-decreasing runs (also asserted).
    """

    index: int
    label: str       # header text; also the key stored in saved ``hidden_columns``
    mono: bool       # render cell text in the monospace font
    toggleable: bool # may be hidden via the column-visibility context menu
    group: int       # vertical-separator group index (Info / Server / Client / Slack / PR)


# Column index constants - the canonical names referenced throughout the
# monitor.  Kept as explicit module-level ints (rather than derived from
# COLUMNS) so they stay greppable; COLUMNS below reuses them and the
# index-equals-position assert keeps the two in agreement.
COL_DELETE = 0
COL_TAG = 1
COL_CLI = 2
COL_APP = 3
COL_PROJECT = 4
COL_SERVER = 5
COL_TASK = 6
COL_CONTEXT = 7
COL_PATH = 8
COL_SERVER_BRANCH = 9
COL_STATUS = 10
COL_QUEUE = 11
COL_CLIENT = 12
COL_SLACK = 13
COL_PR = 14
COL_PR_BRANCH = 15


# Authoritative, ordered column table.  Group ids: 0 Info | 1 Server |
# 2 Client | 3 Slack | 4 PR.  ``mono`` reproduces the legacy ``_MONO_COLS``
# (Project, Path, Server Branch, PR Branch); ``toggleable`` reproduces the
# legacy ``_NON_TOGGLEABLE_COLS`` (Delete and Tag are always visible).
COLUMNS: list[ColumnSpec] = [
    ColumnSpec(COL_DELETE,        '',              mono=False, toggleable=False, group=0),
    ColumnSpec(COL_TAG,           'Tag',           mono=False, toggleable=False, group=0),
    ColumnSpec(COL_CLI,           'CLI',           mono=False, toggleable=True,  group=0),
    ColumnSpec(COL_APP,           'App',           mono=False, toggleable=True,  group=0),
    ColumnSpec(COL_PROJECT,       'Project',       mono=True,  toggleable=True,  group=0),
    ColumnSpec(COL_SERVER,        'Server',        mono=False, toggleable=True,  group=1),
    ColumnSpec(COL_TASK,          'Last Msg',      mono=False, toggleable=True,  group=1),
    ColumnSpec(COL_CONTEXT,       'Context',       mono=False, toggleable=True,  group=1),
    ColumnSpec(COL_PATH,          'Path',          mono=True,  toggleable=True,  group=1),
    ColumnSpec(COL_SERVER_BRANCH, 'Server Branch', mono=True,  toggleable=True,  group=1),
    ColumnSpec(COL_STATUS,        'Status',        mono=False, toggleable=True,  group=1),
    ColumnSpec(COL_QUEUE,         'Queue',         mono=False, toggleable=True,  group=1),
    ColumnSpec(COL_CLIENT,        'Client',        mono=False, toggleable=True,  group=2),
    ColumnSpec(COL_SLACK,         'Slack',         mono=False, toggleable=True,  group=3),
    ColumnSpec(COL_PR,            'PR',            mono=False, toggleable=True,  group=4),
    ColumnSpec(COL_PR_BRANCH,     'PR Branch',     mono=True,  toggleable=True,  group=4),
]

COLUMN_COUNT: int = len(COLUMNS)

# --- Derived collections (single source = COLUMNS above) --------------------

# Header text for every column, in order.  Passed to
# ``QTableWidget.setHorizontalHeaderLabels`` and used to map a saved
# ``hidden_columns`` label back to its index.
HEADER_LABELS: list[str] = [c.label for c in COLUMNS]

# Columns whose plain-text cells render in monospace (technical/code data).
MONO_COLS: frozenset[int] = frozenset(c.index for c in COLUMNS if c.mono)

# Data columns whose plain-text cells center-align: every column except the
# delete-button column (column 0), matching the legacy "all data columns" set.
CENTER_COLS: frozenset[int] = frozenset(
    c.index for c in COLUMNS if c.index != COL_DELETE
)

# Columns that can never be hidden via the column-visibility menu.
NON_TOGGLEABLE_COLS: frozenset[int] = frozenset(
    c.index for c in COLUMNS if not c.toggleable
)

# Column-index groups for the table's vertical separators, in display order.
_GROUP_COUNT: int = max(c.group for c in COLUMNS) + 1
COLUMN_GROUPS: list[list[int]] = [
    [c.index for c in COLUMNS if c.group == gi] for gi in range(_GROUP_COUNT)
]


# --- Import-time invariants -------------------------------------------------
# Always true for the data above; they convert a future mis-edit (a renumbered
# index, a skipped or non-contiguous group) into an immediate, obvious
# ImportError instead of a silent visual bug at runtime.
assert [c.index for c in COLUMNS] == list(range(COLUMN_COUNT)), \
    'COLUMNS[i].index must equal its position i'
assert all(
    COLUMNS[i].group <= COLUMNS[i + 1].group for i in range(COLUMN_COUNT - 1)
), 'column groups must be contiguous (non-decreasing) for separators to work'
assert all(COLUMN_GROUPS), 'every column group must contain at least one column'
