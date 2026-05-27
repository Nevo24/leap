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
from unittest.mock import patch

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
        self._prefs: dict[str, Any] = {'row_order': []}

    def _save_prefs(self) -> None:
        """No-op stub — production saves prefs to disk, tests don't care."""
        pass

    def _cleanup_row_state(self, tag: str) -> None:
        """No-op stub — production clears UI state, tests don't care."""
        pass


@pytest.fixture
def no_disk_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop ``save_pinned_sessions`` from touching the real .storage dir."""
    monkeypatch.setattr(
        'leap.monitor._mixins.session_mixin.save_pinned_sessions',
        lambda _sessions: None,
    )


def _make_active(
    tag: str = 'mytag',
    auto_send_mode: str = AutoSendMode.PAUSE,
    cli_provider: str = 'claude',
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
