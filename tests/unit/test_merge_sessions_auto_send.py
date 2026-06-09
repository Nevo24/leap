"""Tests for ``SessionMixin._merge_sessions`` preserving ``auto_send_mode``
in the pin entry.

The race this guards against: the server snapshots ``auto_send_mode``
to ``pinned_sessions.json`` in its ``__init__`` so the Claude
``PermissionRequest`` hook (which reads pin from disk) sees a stable
per-session value across the lifetime of the session.  But the monitor's
``_merge_sessions`` writes the same file using its in-memory
``_pinned_sessions`` cache — which was loaded from disk ONCE at monitor
startup, so for a brand-new session the cache lacks ``auto_send_mode``.
Without the fix, the monitor's first auto-pin write for that tag would
build ``pin_data`` without ``auto_send_mode`` and clobber the snapshot,
re-exposing the global-fallback leak the original fix tried to close.

The fix pulls ``auto_send_mode`` from the live session's status response
(``s.get('auto_send_mode')``) into ``pin_data`` explicitly.  These tests
pin that behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from leap.cli_providers.states import AutoSendMode
from leap.monitor._mixins.session_mixin import SessionMixin


# --------------------------------------------------------------------------
# Fixture: a bare object with just the attrs ``_merge_sessions`` touches
# --------------------------------------------------------------------------


class _FakeMonitor(SessionMixin):
    """Minimal stand-in for MonitorWindow so we can call _merge_sessions
    without instantiating Qt.  Only the attributes the function reads
    + writes are populated."""

    def __init__(self, pinned: dict[str, Any] | None = None) -> None:
        self._pinned_sessions: dict[str, dict[str, Any]] = pinned or {}
        self._deleted_tags: set[str] = set()
        self._tracked_tags: set[str] = set()
        self._checking_tags: set[str] = set()
        self._starting_tags: set[str] = set()
        self._moving_tags: set[str] = set()
        self._moving_old_pid: dict[str, Any] = {}
        self._prefs: dict[str, Any] = {'row_order': []}

    def _save_prefs(self) -> None:
        """No-op stub — production saves prefs to disk, tests don't care."""
        pass

    def _cleanup_row_state(self, tag: str) -> None:
        """No-op stub — production clears UI state, tests don't care."""
        pass


@pytest.fixture
def no_disk_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the targeted pin writers from touching the real .storage dir."""
    monkeypatch.setattr(
        'leap.monitor._mixins.session_mixin.write_pinned_session_entry',
        lambda _tag, _entry: None,
    )
    monkeypatch.setattr(
        'leap.monitor._mixins.session_mixin.remove_pinned_session_tag',
        lambda _tag: None,
    )


def _make_active(
    tag: str = 'mytag',
    auto_send_mode: str = AutoSendMode.PAUSE,
    cli_provider: str = 'claude',
    server_pid: Any = None,
) -> dict[str, Any]:
    """Mimic the dict shape session_manager.get_active_sessions() emits."""
    return {
        'tag': tag,
        'project_path': '/Users/x/proj',
        'ide': 'JetBrains',
        'branch': 'main',
        'cli_provider': cli_provider,
        'auto_send_mode': auto_send_mode,
        'cli_state': 'idle',
        'queue_size': 0,
        'recently_sent': [],
        'server_pid': server_pid,
    }


# --------------------------------------------------------------------------
# The race-window guard: brand-new session, server has snapshotted, but
# the monitor's in-memory pin doesn't know about this tag yet.
# --------------------------------------------------------------------------


class TestRaceWindow:
    def test_brand_new_session_pin_picks_up_live_mode(
        self, no_disk_write: None,
    ) -> None:
        """Monitor has never seen this tag.  The pin built by
        _merge_sessions must include auto_send_mode from the live
        session (server's snapshotted value), not be missing it."""
        m = _FakeMonitor(pinned={})
        active = [_make_active(tag='mytag', auto_send_mode='always')]
        m._merge_sessions(active)
        assert m._pinned_sessions['mytag']['auto_send_mode'] == 'always'

    def test_brand_new_session_default_pause(
        self, no_disk_write: None,
    ) -> None:
        m = _FakeMonitor(pinned={})
        active = [_make_active(tag='mytag', auto_send_mode='pause')]
        m._merge_sessions(active)
        assert m._pinned_sessions['mytag']['auto_send_mode'] == 'pause'


# --------------------------------------------------------------------------
# Live-server value wins over a stale in-memory pin (e.g., user toggled
# via the client between monitor startup and the next refresh).
# --------------------------------------------------------------------------


class TestLiveWinsOverStale:
    def test_live_always_overrides_stale_pause_in_memory(
        self, no_disk_write: None,
    ) -> None:
        """Existing in-memory pin says pause (loaded at monitor startup),
        but the live server reports always (user toggled via client).
        The fresh value must propagate into the pin so a subsequent
        save_pinned_sessions doesn't roll the file back."""
        m = _FakeMonitor(pinned={
            'mytag': {
                'tag': 'mytag',
                'project_path': '/Users/x/proj',
                'ide': 'JetBrains',
                'branch': 'main',
                'cli_provider': 'claude',
                'auto_send_mode': 'pause',
            },
        })
        active = [_make_active(tag='mytag', auto_send_mode='always')]
        m._merge_sessions(active)
        assert m._pinned_sessions['mytag']['auto_send_mode'] == 'always'

    def test_live_pause_overrides_stale_always_in_memory(
        self, no_disk_write: None,
    ) -> None:
        m = _FakeMonitor(pinned={
            'mytag': {
                'tag': 'mytag',
                'project_path': '/Users/x/proj',
                'ide': 'JetBrains',
                'branch': 'main',
                'cli_provider': 'claude',
                'auto_send_mode': 'always',
            },
        })
        active = [_make_active(tag='mytag', auto_send_mode='pause')]
        m._merge_sessions(active)
        assert m._pinned_sessions['mytag']['auto_send_mode'] == 'pause'


# --------------------------------------------------------------------------
# Defensive: when the live session somehow omits auto_send_mode (e.g.,
# a future test fixture or an old server build), prefer the existing
# pinned value rather than blanking it.  Fall through to PAUSE only as
# a last resort.
# --------------------------------------------------------------------------


class TestFallback:
    def test_falls_back_to_existing_pin_when_live_omits_mode(
        self, no_disk_write: None,
    ) -> None:
        m = _FakeMonitor(pinned={
            'mytag': {
                'tag': 'mytag',
                'project_path': '/Users/x/proj',
                'ide': 'JetBrains',
                'branch': 'main',
                'cli_provider': 'claude',
                'auto_send_mode': 'always',
            },
        })
        active = [_make_active(tag='mytag')]
        del active[0]['auto_send_mode']
        m._merge_sessions(active)
        assert m._pinned_sessions['mytag']['auto_send_mode'] == 'always'

    def test_defaults_to_pause_when_no_signal_anywhere(
        self, no_disk_write: None,
    ) -> None:
        m = _FakeMonitor(pinned={})
        active = [_make_active(tag='mytag')]
        del active[0]['auto_send_mode']
        m._merge_sessions(active)
        assert m._pinned_sessions['mytag']['auto_send_mode'] == AutoSendMode.PAUSE


# --------------------------------------------------------------------------
# Unrelated pin fields must remain untouched (PR fields, IDE, branch …).
# --------------------------------------------------------------------------


class TestMoveGuardClears:
    """``_merge_sessions`` must drop the Move-to-IDE guard once the
    RELAUNCHED server (a different pid than the one we moved away from)
    registers — otherwise the guard stayed armed for the full 12-min
    safety window and a quick close-in-IDE flipped the row to a stuck
    'Starting...' dead row.
    """

    def _pin(self, tag: str = 'mytag') -> dict[str, Any]:
        return {
            'tag': tag,
            'project_path': '/Users/x/proj',
            'ide': 'JetBrains',
            'branch': 'main',
            'cli_provider': 'claude',
            'auto_send_mode': 'pause',
        }

    def test_new_pid_clears_guard(self, no_disk_write: None) -> None:
        m = _FakeMonitor(pinned={'mytag': self._pin()})
        m._moving_tags.add('mytag')
        m._moving_old_pid['mytag'] = 111  # the server we moved away from
        # Relaunched server registers with a different pid.
        m._merge_sessions([_make_active(tag='mytag', server_pid=222)])
        assert 'mytag' not in m._moving_tags
        assert 'mytag' not in m._moving_old_pid

    def test_same_pid_keeps_guard(self, no_disk_write: None) -> None:
        # During the close the OLD server is briefly still alive (same
        # pid).  Clearing then would drop the dead-row bridge, so the
        # guard must survive while the live pid still matches the old one.
        m = _FakeMonitor(pinned={'mytag': self._pin()})
        m._moving_tags.add('mytag')
        m._moving_old_pid['mytag'] = 111
        m._merge_sessions([_make_active(tag='mytag', server_pid=111)])
        assert 'mytag' in m._moving_tags
        assert m._moving_old_pid['mytag'] == 111

    def test_dead_gap_keeps_guard(self, no_disk_write: None) -> None:
        # Old server gone, new one not up yet → no live server for the
        # tag.  The guard (and the dead row it protects) must persist.
        m = _FakeMonitor(pinned={'mytag': self._pin()})
        m._moving_tags.add('mytag')
        m._moving_old_pid['mytag'] = 111
        merged = m._merge_sessions([])  # nothing active
        assert 'mytag' in m._moving_tags  # bridge preserved
        # And the row itself survived as a dead row (not auto-removed).
        assert any(r['tag'] == 'mytag' for r in merged)

    def test_missing_old_pid_clears_on_any_live(
        self, no_disk_write: None,
    ) -> None:
        # No recorded old pid → there was no old server to confuse the
        # new one with, so the first live server clears the guard.
        m = _FakeMonitor(pinned={'mytag': self._pin()})
        m._moving_tags.add('mytag')  # no _moving_old_pid entry
        m._merge_sessions([_make_active(tag='mytag', server_pid=222)])
        assert 'mytag' not in m._moving_tags


class TestPreservesOtherFields:
    def test_pr_fields_preserved_when_writing_auto_send_mode(
        self, no_disk_write: None,
    ) -> None:
        m = _FakeMonitor(pinned={
            'mytag': {
                'tag': 'mytag',
                'remote_project_path': 'group/proj',
                'host_url': 'https://gitlab.example.com',
                'scm_type': 'gitlab',
                'branch': 'feature-x',
                'project_path': '/Users/x/proj',
                'ide': 'JetBrains',
                'cli_provider': 'claude',
            },
        })
        active = [_make_active(tag='mytag', auto_send_mode='always')]
        m._merge_sessions(active)
        pin = m._pinned_sessions['mytag']
        assert pin['auto_send_mode'] == 'always'
        assert pin['remote_project_path'] == 'group/proj'
        assert pin['host_url'] == 'https://gitlab.example.com'
        assert pin['scm_type'] == 'gitlab'
        # PR-pinned branch must come from existing pin, not from `s`.
        assert pin['branch'] == 'feature-x'


# --------------------------------------------------------------------------
# Helper-call regression guard.  The in-memory assertions above pass even
# if the disk write is silently dropped — so if a future refactor
# replaces ``write_pinned_session_entry`` with a no-op (or, worse, with
# the old full-state ``save_pinned_sessions``), the existing tests
# wouldn't catch it.  These tests assert that the per-tag helper is
# actually invoked with the right args.
# --------------------------------------------------------------------------


@pytest.fixture
def record_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, MagicMock]:
    """Replace the targeted helpers with MagicMocks so tests can assert
    on their call args."""
    write_mock = MagicMock()
    remove_mock = MagicMock()
    monkeypatch.setattr(
        'leap.monitor._mixins.session_mixin.write_pinned_session_entry',
        write_mock,
    )
    monkeypatch.setattr(
        'leap.monitor._mixins.session_mixin.remove_pinned_session_tag',
        remove_mock,
    )
    return {'write': write_mock, 'remove': remove_mock}


class TestHelpersAreCalled:
    def test_new_session_triggers_write_entry(
        self, record_writes: dict[str, MagicMock],
    ) -> None:
        """When _merge_sessions adds a new pin, it must call the per-tag
        upsert helper (not the legacy full-state save)."""
        m = _FakeMonitor(pinned={})
        active = [_make_active(tag='mytag', auto_send_mode='pause')]
        m._merge_sessions(active)
        assert record_writes['write'].call_count == 1
        call_tag, call_entry = record_writes['write'].call_args.args
        assert call_tag == 'mytag'
        assert call_entry['tag'] == 'mytag'
        assert call_entry['auto_send_mode'] == 'pause'
        assert record_writes['remove'].call_count == 0

    def test_dead_row_triggers_remove_tag(
        self, record_writes: dict[str, MagicMock],
    ) -> None:
        """When _merge_sessions removes a dead row, it must call the
        per-tag removal helper, not write the whole map."""
        m = _FakeMonitor(pinned={
            'deadtag': {
                'tag': 'deadtag',
                'project_path': '/old',
                'auto_send_mode': 'pause',
            },
        })
        # No active sessions → deadtag is unreferenced → gets removed.
        m._merge_sessions([])
        record_writes['remove'].assert_called_once_with('deadtag')
        # And no spurious upsert.
        assert record_writes['write'].call_count == 0

    def test_unchanged_session_no_helpers_called(
        self, record_writes: dict[str, MagicMock],
    ) -> None:
        """No-op refresh — the in-memory pin already matches the live
        socket data, so no disk write should fire."""
        m = _FakeMonitor(pinned={
            'mytag': {
                'tag': 'mytag',
                'project_path': '/Users/x/proj',
                'ide': 'JetBrains',
                'branch': 'main',
                'cli_provider': 'claude',
                'auto_send_mode': 'pause',
                'churn_queue_mode': 'wait',
            },
        })
        active = [_make_active(tag='mytag', auto_send_mode='pause')]
        m._merge_sessions(active)
        assert record_writes['write'].call_count == 0
        assert record_writes['remove'].call_count == 0

    def test_multiple_changed_sessions_each_get_own_write(
        self, record_writes: dict[str, MagicMock],
    ) -> None:
        """Two new sessions → two per-tag upserts (not one batch save).
        The whole point: each tag writes independently so a concurrent
        server-side write for tag B can't be clobbered by the monitor
        writing tag A."""
        m = _FakeMonitor(pinned={})
        active = [
            _make_active(tag='A', auto_send_mode='pause'),
            _make_active(tag='B', auto_send_mode='always'),
        ]
        m._merge_sessions(active)
        assert record_writes['write'].call_count == 2
        called_tags = {
            call.args[0] for call in record_writes['write'].call_args_list
        }
        assert called_tags == {'A', 'B'}
