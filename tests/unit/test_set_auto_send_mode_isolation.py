"""Regression test for the monitor-side cross-session leak.

``TableBuilderMixin._set_auto_send_mode`` used to call
``save_pinned_sessions(self._pinned_sessions)`` — which writes the WHOLE
in-memory ``_pinned_sessions`` dict to disk.  If another session's
server had just written its own ``auto_send_mode`` between the monitor's
last ``_merge_sessions`` refresh and the right-click toggle, the
monitor's full-state save would silently overwrite that other session's
pin with the monitor's stale in-memory value — exactly the cross-session
leak the original fix closed, re-opening through the monitor side.

The fix is to use ``update_pinned_session_field`` (a targeted
read-modify-write of a single field on a single tag).  This test pins
the integration: ``_set_auto_send_mode`` calls the targeted helper, not
the full-state save.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from leap.cli_providers.states import AutoSendMode
from leap.monitor._mixins import table_builder_mixin as tbm
from leap.monitor._mixins.table_builder_mixin import TableBuilderMixin


class _FakeMonitor(TableBuilderMixin):
    """Minimal stand-in for MonitorWindow.

    ``_set_auto_send_mode`` reads ``self.sessions`` + ``self._pinned_sessions``
    + ``self._cell_cache`` and calls ``self._show_status``.  All other
    attributes touched here are stubbed.
    """

    def __init__(
        self,
        sessions: list[dict[str, Any]] | None = None,
        pinned: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.sessions: list[dict[str, Any]] = sessions or []
        self._pinned_sessions: dict[str, dict[str, Any]] = pinned or {}
        self._cell_cache: dict[Any, Any] = {}

    def _show_status(self, _msg: str) -> None:
        pass


@pytest.fixture
def mock_socket_and_writers(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, MagicMock]:
    """Replace the network + disk helpers ``_set_auto_send_mode`` reaches
    for, so the test exercises only the in-process logic."""
    fake_send = MagicMock(return_value={'status': 'ok'})
    fake_update = MagicMock()
    fake_save_all = MagicMock()
    monkeypatch.setattr(tbm, 'send_socket_request', fake_send)
    monkeypatch.setattr(tbm, 'update_pinned_session_field', fake_update)
    # ``save_pinned_sessions`` was removed from this module's namespace
    # when the fix landed — set it via the module dict so the regression
    # guard below can detect a re-introduction.
    monkeypatch.setitem(tbm.__dict__, 'save_pinned_sessions', fake_save_all)
    return {
        'send': fake_send,
        'update': fake_update,
        'save_all': fake_save_all,
    }


class TestPerSessionToggleIsolation:
    def test_calls_targeted_update_not_full_state_save(
        self, mock_socket_and_writers: dict[str, MagicMock],
    ) -> None:
        """The core regression guard: a per-session toggle must use the
        per-tag write, not a write of the whole ``_pinned_sessions``
        map (which would clobber OTHER tags' recent server-side writes)."""
        m = _FakeMonitor(
            sessions=[{'tag': 'A', 'auto_send_mode': 'pause'}],
            pinned={
                'A': {'auto_send_mode': 'pause', 'project_path': '/a'},
                'B': {'auto_send_mode': 'always', 'project_path': '/b'},
            },
        )
        m._set_auto_send_mode('A', AutoSendMode.ALWAYS)

        # Targeted write — only A's auto_send_mode field.
        mock_socket_and_writers['update'].assert_called_once_with(
            'A', 'auto_send_mode', AutoSendMode.ALWAYS,
        )
        # And critically, the full-state save MUST NOT be called.
        mock_socket_and_writers['save_all'].assert_not_called()

    def test_inmemory_pin_for_toggled_tag_updated(
        self, mock_socket_and_writers: dict[str, MagicMock],
    ) -> None:
        m = _FakeMonitor(
            sessions=[{'tag': 'A', 'auto_send_mode': 'pause'}],
            pinned={'A': {'auto_send_mode': 'pause'}},
        )
        m._set_auto_send_mode('A', AutoSendMode.ALWAYS)
        assert m._pinned_sessions['A']['auto_send_mode'] == AutoSendMode.ALWAYS
        assert m.sessions[0]['auto_send_mode'] == AutoSendMode.ALWAYS

    def test_inmemory_pin_for_other_tags_untouched(
        self, mock_socket_and_writers: dict[str, MagicMock],
    ) -> None:
        """Even in memory, toggling A must not reach into B's pin entry."""
        m = _FakeMonitor(
            sessions=[
                {'tag': 'A', 'auto_send_mode': 'pause'},
                {'tag': 'B', 'auto_send_mode': 'always'},
            ],
            pinned={
                'A': {'auto_send_mode': 'pause'},
                'B': {'auto_send_mode': 'always', 'project_path': '/b'},
            },
        )
        m._set_auto_send_mode('A', AutoSendMode.ALWAYS)
        assert m._pinned_sessions['B'] == {
            'auto_send_mode': 'always', 'project_path': '/b',
        }
        # And only A was passed to the targeted update.
        call_args = mock_socket_and_writers['update'].call_args
        assert call_args.args[0] == 'A'

    def test_dead_row_still_persists_via_targeted_write(
        self, mock_socket_and_writers: dict[str, MagicMock],
    ) -> None:
        """Dead row (no server) — socket request fails.  The targeted
        ``update_pinned_session_field`` still fires so the choice
        survives until the user re-launches the session."""
        mock_socket_and_writers['send'].return_value = None  # socket fail
        m = _FakeMonitor(
            sessions=[],  # no live session
            pinned={'A': {'auto_send_mode': 'pause'}},
        )
        m._set_auto_send_mode('A', AutoSendMode.ALWAYS)
        mock_socket_and_writers['update'].assert_called_once_with(
            'A', 'auto_send_mode', AutoSendMode.ALWAYS,
        )
        mock_socket_and_writers['save_all'].assert_not_called()


# --------------------------------------------------------------------------
# Structural guard — save_pinned_sessions must stay out of the
# ``_set_auto_send_mode`` source so a future regression via a stray
# ``from ... import save_pinned_sessions`` is loud at module-import time.
# --------------------------------------------------------------------------


class TestStructuralGuard:
    def test_save_pinned_sessions_not_imported_by_table_builder_mixin(
        self,
    ) -> None:
        """Original cross-session leak shape: a per-session toggle
        called ``save_pinned_sessions(self._pinned_sessions)`` which
        wrote the whole map and overwrote concurrent server-side
        writes for other tags.  If this symbol re-appears in the
        module namespace, someone has likely re-introduced the full-
        state save.  The behavioural tests above are the real
        regression guard; this is the loud canary at import time."""
        # The fixture above injects ``save_pinned_sessions`` into the
        # module dict to test for it; that injection is per-test and
        # reverted by monkeypatch.  At baseline, the symbol must not
        # be in the module namespace.
        assert 'save_pinned_sessions' not in tbm.__dict__, (
            'leap.monitor._mixins.table_builder_mixin.save_pinned_sessions '
            'must stay absent — per-session toggles must use the targeted '
            'update_pinned_session_field helper, not the full-state save. '
            'See the auto_send_mode cross-session leak fix.'
        )
