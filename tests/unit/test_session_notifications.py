"""Tests for DockBadge.update_sessions' transition gate.

The MIN_BUSY_SECONDS gate is meant to suppress *completed* (Running->Idle)
notifications from trivially short runs.  It must NOT suppress attention-needed
transitions (needs permission / input, interrupted): a session that runs
briefly and then blocks on the user has to notify regardless of duration.

Pure-logic: DockBadge is built via __new__ with _render_total stubbed, so no
QApplication / dock icon is touched.  Elapsed time is simulated by rewinding
the recorded _busy_since timestamp rather than sleeping.
"""

from __future__ import annotations

from typing import Any

from leap.cli_providers.states import CLIState
from leap.monitor.ui.dock_badge import DockBadge, NotificationType


def _badge() -> Any:
    b = DockBadge.__new__(DockBadge)
    b._seen_session_states = {}
    b._busy_since = {}
    b._session_changed = 0
    b._session_notified = set()
    b._render_total = lambda: None
    return b


def _poll(b: Any, tag: str, state: Any) -> list:
    return b.update_sessions([{'tag': tag, 'cli_state': state}],
                             window_active=False)


def _types(events: list) -> set:
    return {e.type for e in events}


class TestAttentionStatesFireOnFastTransition:
    def test_needs_permission_fires_even_when_run_was_short(self) -> None:
        b = _badge()
        _poll(b, 'x', CLIState.RUNNING)        # records busy_since (now)
        evs = _poll(b, 'x', CLIState.NEEDS_PERMISSION)  # ~0s later
        assert NotificationType.SESSION_NEEDS_PERMISSION in _types(evs)

    def test_needs_input_fires_even_when_run_was_short(self) -> None:
        b = _badge()
        _poll(b, 'x', CLIState.RUNNING)
        evs = _poll(b, 'x', CLIState.NEEDS_INPUT)
        assert NotificationType.SESSION_NEEDS_INPUT in _types(evs)

    def test_interrupted_fires_even_when_run_was_short(self) -> None:
        b = _badge()
        _poll(b, 'x', CLIState.RUNNING)
        evs = _poll(b, 'x', CLIState.INTERRUPTED)
        assert NotificationType.SESSION_INTERRUPTED in _types(evs)


class TestCompletedStillGated:
    def test_completed_suppressed_for_short_run(self) -> None:
        b = _badge()
        _poll(b, 'x', CLIState.RUNNING)
        evs = _poll(b, 'x', CLIState.IDLE)  # < MIN_BUSY_SECONDS elapsed
        assert NotificationType.SESSION_COMPLETED not in _types(evs)

    def test_completed_fires_for_long_run(self) -> None:
        b = _badge()
        _poll(b, 'x', CLIState.RUNNING)
        # Simulate the run having started >MIN_BUSY_SECONDS ago.
        b._busy_since['x'] -= (b.MIN_BUSY_SECONDS + 1.0)
        evs = _poll(b, 'x', CLIState.IDLE)
        assert NotificationType.SESSION_COMPLETED in _types(evs)


class TestNoStartupFire:
    def test_first_sight_non_running_does_not_fire(self) -> None:
        b = _badge()
        # Never observed RUNNING first -> no busy_since -> no notification.
        assert _poll(b, 'x', CLIState.NEEDS_PERMISSION) == []

    def test_repeated_needs_permission_coalesces_badge(self) -> None:
        # Flicker NEEDS_PERMISSION -> RUNNING -> NEEDS_PERMISSION: the event
        # fires each time, but the badge counts it once (dedup), so removing
        # the gate can't spam the badge.
        b = _badge()
        _poll(b, 'x', CLIState.RUNNING)
        _poll(b, 'x', CLIState.NEEDS_PERMISSION)
        changed_after_first = b._session_changed
        _poll(b, 'x', CLIState.RUNNING)
        _poll(b, 'x', CLIState.NEEDS_PERMISSION)
        assert b._session_changed == changed_after_first
