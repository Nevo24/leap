"""Tests for row-colour / alias cleanup so a reused tag never inherits a
removed session's colour.

Colour and alias are persisted keyed by tag (``row_colors`` / ``aliases``
in ``monitor_prefs.json``).  They are meant to live exactly as long as the
row, cleaned by ``_cleanup_row_state`` on every removal path.  Two gaps
this module pins:

1. ``_close_server``'s ``_on_closed`` (the Close-server ``×`` button on a
   non-PR row) used to remove the pin but skip ``_cleanup_row_state`` - the
   fifth removal path the cleanup-unification missed.  The orphaned colour
   then bled onto the next ``leap <same-tag>``.  Move-to-IDE and Delete-row
   pass ``_from_delete=True`` (``will_remove`` stays False) so they must NOT
   strip the colour here - Move-to-IDE keeps it, Delete-row cleans up via
   ``_remove_pinned_session``.

2. ``_prune_orphan_row_prefs`` self-heals colours/aliases stranded on disk
   by a removal the monitor wasn't running to observe (app closed when the
   session ended).  Run at startup after the first merge: a tag with no
   pin (and not a Cursor GUI row / in-flight tag) is a ghost.
"""

from __future__ import annotations

from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

from leap.monitor._mixins import session_mixin
from leap.monitor._mixins.session_mixin import SessionMixin
from leap.monitor.cursor_gui_scan import CURSOR_GUI_TAG_PREFIX


class _FakeMonitor(SessionMixin):
    """Minimal stand-in for MonitorWindow so the row-cleanup helpers run
    without instantiating Qt.  Inherits the REAL ``_cleanup_row_state``,
    ``_remove_from_row_order`` and ``_prune_orphan_row_prefs`` - only the
    Qt / disk / status edges are stubbed."""

    def __init__(self) -> None:
        self._pinned_sessions: dict[str, dict[str, Any]] = {}
        self._deleted_tags: set[str] = set()
        self._tracked_tags: set[str] = set()
        self._checking_tags: set[str] = set()
        self._starting_tags: set[str] = set()
        self._moving_tags: set[str] = set()
        self._moving_old_pid: dict[str, Any] = {}
        self._row_colors: dict[str, str] = {}
        self._aliases: dict[str, str] = {}
        self._state_changed_at: dict[str, Any] = {}
        self._dismissed_new_status: set[str] = set()
        self._pr_changed_at: dict[str, Any] = {}
        self._dismissed_pr_new_status: set[str] = set()
        self._prefs: dict[str, Any] = {'row_order': []}
        self.sessions: list[dict[str, Any]] = []
        self.table = MagicMock()
        self.save_count = 0

    def _save_prefs(self) -> None:
        self.save_count += 1

    # Edges hit by ``_close_server`` we don't care about in these tests.
    def _set_busy(self, *_a: Any, **_k: Any) -> None:
        pass

    def _show_status(self, *_a: Any, **_k: Any) -> None:
        pass

    def _close_client(self, *_a: Any, **_k: Any) -> None:
        pass


# --------------------------------------------------------------------------
#  _prune_orphan_row_prefs  (startup self-heal)
# --------------------------------------------------------------------------


class TestPruneOrphanRowPrefs:
    def test_removes_orphan_colour(self) -> None:
        m = _FakeMonitor()
        m._pinned_sessions = {'live': {'tag': 'live'}}
        m._row_colors = {'live': '#111111', 'ghost': '#222222'}
        m._prune_orphan_row_prefs()
        assert m._row_colors == {'live': '#111111'}
        assert m.save_count == 1
        m.table.setProperty.assert_called_once_with('_row_colors', m._row_colors)

    def test_removes_orphan_alias(self) -> None:
        m = _FakeMonitor()
        m._pinned_sessions = {'live': {'tag': 'live'}}
        m._aliases = {'live': 'Live', 'ghost': 'Ghost'}
        m._prune_orphan_row_prefs()
        assert m._aliases == {'live': 'Live'}

    def test_keeps_dead_but_pinned_row_colour(self) -> None:
        # A dead row that is still pinned (e.g. PR-tracked) has a pin entry,
        # so its colour must survive.
        m = _FakeMonitor()
        m._pinned_sessions = {'tracked': {'tag': 'tracked', 'pr_tracked': True}}
        m._row_colors = {'tracked': '#abcdef'}
        m._prune_orphan_row_prefs()
        assert m._row_colors == {'tracked': '#abcdef'}
        assert m.save_count == 0  # nothing changed

    def test_keeps_cursor_gui_colour(self) -> None:
        # Cursor editor Agent-tab rows own their tags via a distinct prefix
        # and aren't in _pinned_sessions; they must not be pruned.
        m = _FakeMonitor()
        cursor_tag = CURSOR_GUI_TAG_PREFIX + 'abc-123'
        m._row_colors = {cursor_tag: '#0f0f0f'}
        m._prune_orphan_row_prefs()
        assert m._row_colors == {cursor_tag: '#0f0f0f'}

    @pytest.mark.parametrize(
        'guard',
        ['_tracked_tags', '_checking_tags', '_starting_tags', '_moving_tags'],
    )
    def test_keeps_in_flight_guarded_colour(self, guard: str) -> None:
        m = _FakeMonitor()
        getattr(m, guard).add('busy')
        m._row_colors = {'busy': '#123123'}
        m._prune_orphan_row_prefs()
        assert m._row_colors == {'busy': '#123123'}

    def test_noop_when_no_orphans(self) -> None:
        m = _FakeMonitor()
        m._pinned_sessions = {'a': {'tag': 'a'}, 'b': {'tag': 'b'}}
        m._row_colors = {'a': '#1'}
        m._aliases = {'b': 'Bee'}
        m._prune_orphan_row_prefs()
        assert m.save_count == 0
        m.table.setProperty.assert_not_called()

    def test_prunes_multiple_ghosts_one_save(self) -> None:
        m = _FakeMonitor()
        m._pinned_sessions = {'live': {'tag': 'live'}}
        m._row_colors = {'live': '#1', 'g1': '#2', 'g2': '#3'}
        m._aliases = {'g3': 'x'}
        m._prune_orphan_row_prefs()
        assert m._row_colors == {'live': '#1'}
        assert m._aliases == {}
        assert m.save_count == 1  # single batched save


# --------------------------------------------------------------------------
#  _close_server  (the leak fix + the Move-to-IDE / PR boundaries)
# --------------------------------------------------------------------------


class _SyncSignal:
    """Stand-in for a Qt signal that fires its slots synchronously."""

    def __init__(self) -> None:
        self._slots: list[Callable[[], None]] = []

    def connect(self, slot: Callable[[], None]) -> None:
        self._slots.append(slot)

    def emit(self) -> None:
        for slot in list(self._slots):
            slot()


class _SyncWorker:
    """Replacement for ``BackgroundCallWorker`` that runs ``func`` and then
    its ``finished`` slots inline on ``start()`` - so ``_on_closed`` (the
    block under test) executes deterministically without a real QThread."""

    def __init__(self, func: Callable[[], Any], _parent: Any = None) -> None:
        self._func = func
        self.finished = _SyncSignal()

    def start(self) -> None:
        self._func()
        self.finished.emit()

    def deleteLater(self) -> None:
        pass


@pytest.fixture
def close_server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every Qt / IO edge ``_close_server`` touches so the real method
    runs to completion headlessly.  ``QMessageBox.question`` answers Yes so
    the will-remove confirm goes through; ``QMessageBox.Yes`` itself stays
    real so the ``reply != QMessageBox.Yes`` comparison is genuine."""
    monkeypatch.setattr(session_mixin, 'BackgroundCallWorker', _SyncWorker)
    monkeypatch.setattr(
        session_mixin.QMessageBox, 'question',
        staticmethod(lambda *_a, **_k: session_mixin.QMessageBox.Yes),
    )
    monkeypatch.setattr(session_mixin, 'load_session_metadata', lambda _tag: {})
    monkeypatch.setattr(
        session_mixin, 'send_socket_request',
        lambda *_a, **_k: {'status': 'ok'},
    )
    monkeypatch.setattr(
        session_mixin, 'close_terminal_with_title', lambda *_a, **_k: None,
    )
    removed: list[str] = []
    monkeypatch.setattr(
        session_mixin, 'remove_pinned_session_tag', lambda t: removed.append(t),
    )


def _colored_session(m: _FakeMonitor, tag: str = 'mr-row') -> None:
    """Seed a coloured, aliased, pinned, in-order row for ``tag``."""
    m._pinned_sessions[tag] = {'tag': tag, 'project_path': '/x'}
    m._row_colors[tag] = '#a0522d'
    m._aliases[tag] = 'My MR'
    m._prefs['row_order'] = [tag]


class TestCloseServerClearsColour:
    def test_close_server_button_clears_colour_and_alias(
        self, close_server_env: None,
    ) -> None:
        """Close-server ``×`` on a non-PR row → will_remove → row gone AND
        its colour/alias/row_order entry cleaned (the leak that bled colour
        onto the next ``leap <same-tag>``)."""
        m = _FakeMonitor()
        _colored_session(m, 'mr-row')
        m._close_server('mr-row', server_pid=4321)  # _from_delete defaults False
        assert 'mr-row' not in m._row_colors
        assert 'mr-row' not in m._aliases
        assert 'mr-row' not in m._prefs['row_order']
        assert 'mr-row' not in m._pinned_sessions

    def test_move_to_ide_keeps_colour(self, close_server_env: None) -> None:
        """Move-to-IDE closes the old server with ``_from_delete=True`` so
        will_remove stays False - the colour/alias/pin must survive the
        relocation."""
        m = _FakeMonitor()
        _colored_session(m, 'mr-row')
        m._close_server('mr-row', server_pid=4321, _from_delete=True)
        assert m._row_colors['mr-row'] == '#a0522d'
        assert m._aliases['mr-row'] == 'My MR'
        assert 'mr-row' in m._pinned_sessions

    def test_close_server_on_pr_tracked_row_keeps_colour(
        self, close_server_env: None,
    ) -> None:
        """A PR-tracked row survives losing its server (no will_remove), so
        its colour stays."""
        m = _FakeMonitor()
        _colored_session(m, 'mr-row')
        m._tracked_tags.add('mr-row')
        m._close_server('mr-row', server_pid=4321)
        assert m._row_colors['mr-row'] == '#a0522d'
        assert 'mr-row' in m._pinned_sessions
