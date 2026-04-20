"""Waiting-state wedges: needs_permission / needs_input.

Same shape as the interrupted-wedge suite — document every happy-path
exit plus the scenarios that can leave the session stuck showing a
dialog that's already been dismissed by the CLI.
"""

import time

import pytest

from leap.utils.constants import SAFETY_WAITING_TIMEOUT

from tests.conftest import PTYFixture


# A dialog snippet Claude's Ink TUI would render; satisfies both
# ``has_dialog_indicator`` and ``is_dialog_certain``.
_DIALOG_BYTES = (
    b'Allow the tool to run?\r\n'
    b'1. Yes\r\n'
    b'2. No\r\n'
    b'Enter to select  Esc to cancel\r\n'
)


class TestWaitingExitPaths:
    """All the supported ways out of needs_permission / needs_input."""

    def test_signal_idle_after_user_responded(
        self, pty: PTYFixture,
    ) -> None:
        """Normal: user answers the dialog, Stop hook fires idle,
        state returns to idle."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG_BYTES)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.tracker.on_input(b'1')
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

    def test_signal_idle_without_response_is_gated(
        self, pty: PTYFixture,
    ) -> None:
        """An idle signal with no user_responded is refused — gates
        out spurious Stop hooks that fire while a dialog is live."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG_BYTES)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.write_signal('idle')
        assert pty.get_state() == 'needs_permission'

    def test_cursor_hidden_after_response_resumes_running(
        self, pty: PTYFixture,
    ) -> None:
        """User answers, CLI starts processing (cursor hidden) — we
        transition straight to running without needing a signal."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG_BYTES)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.tracker.on_input(b'1')
        pty.feed_output(b'\x1b[?25l\x1b[2J\x1b[HProcessing...')
        assert pty.get_state() == 'running'

    def test_escape_then_signal_idle_exits(
        self, pty: PTYFixture,
    ) -> None:
        """Escape dismisses the dialog, hook fires idle — exit."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG_BYTES)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.tracker.on_input(b'\x1b')
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'


class TestLateNotificationGuard:
    """The late-notification guard rejects hook signals whose dialog
    patterns aren't visible on the live pyte screen — prevents stale
    Notifications (fired just as Claude finished) from sticking."""

    def test_needs_permission_without_dialog_on_screen_rejected(
        self, pty: PTYFixture,
    ) -> None:
        """Signal arrives with no dialog patterns on screen → ignored."""
        pty.tracker.on_send()
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        # Late Notification arrives — screen is empty.
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'idle'
        assert not pty.signal_file.exists()

    def test_needs_input_without_dialog_on_screen_rejected(
        self, pty: PTYFixture,
    ) -> None:
        """Same guard applies to needs_input (elicitation_dialog)."""
        pty.tracker.on_send()
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        pty.write_signal('needs_input')
        assert pty.get_state() == 'idle'

    def test_needs_permission_with_dialog_in_last_running_snapshot(
        self, pty: PTYFixture,
    ) -> None:
        """Dialog was rendered before Stop→idle cleared the screen;
        the guard consults ``_last_running_snapshot`` and accepts the
        signal.  This lets a Notification hook that arrives *just
        after* Stop still open the dialog."""
        pty.tracker.on_send()
        # Dialog rendered during running.
        pty.feed_output(_DIALOG_BYTES)
        # Stop hook fires → running→idle, snapshot captured.
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'
        assert pty.tracker._last_running_snapshot

        # Late needs_permission arrives — guard consults snapshot.
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'


class TestWaitingWedges:
    """Scenarios that currently leave the session stuck."""

    @pytest.mark.xfail(
        strict=True,
        reason='If the CLI dismisses its own dialog (timeout, '
               'auto-cancel) with no Stop hook, the tracker has no '
               'event-driven way to notice — state stays at '
               'needs_permission until SAFETY_WAITING_TIMEOUT (60s).',
    )
    def test_cli_dismisses_dialog_without_signal(
        self, pty: PTYFixture,
    ) -> None:
        """The CLI aborts the dialog without a hook — ideally the
        tracker would notice the missing dialog patterns and resume."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG_BYTES)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        # Dialog disappears — CLI redraws a plain prompt.
        pty.feed_output(b'\x1b[?25h\x1b[2J\x1b[H> ')
        # Expectation: within a couple polls, we notice and exit.
        assert pty.wait_for_state('idle', timeout=2.0) == 'idle'

    def test_safety_timeout_force_exits_stuck_waiting(
        self, pty: PTYFixture,
    ) -> None:
        """Belt-and-braces: after SAFETY_WAITING_TIMEOUT with no
        confirming signal, we force-exit."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG_BYTES)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.clear_signal()
        original_clock = pty.tracker._clock
        frozen = time.time() + SAFETY_WAITING_TIMEOUT + 5.0
        pty.tracker._clock = lambda: frozen
        try:
            assert pty.get_state() == 'idle'
        finally:
            pty.tracker._clock = original_clock


class TestWaitingTransitions:
    """Crossovers between dialog types and the interrupted state."""

    def test_needs_permission_to_needs_input(
        self, pty: PTYFixture,
    ) -> None:
        """Upgrade from permission prompt to elicitation dialog via a
        second Notification signal."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG_BYTES)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.feed_output(_DIALOG_BYTES)
        pty.write_signal('needs_input')
        assert pty.get_state() == 'needs_input'

    def test_escape_in_needs_permission_with_interrupt_pattern(
        self, pty: PTYFixture,
    ) -> None:
        """User hits Escape to cancel; if the CLI emits the
        'Interrupted' pattern in response, we move to interrupted
        (Escape-in-waiting is a valid cancel)."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG_BYTES)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.tracker.on_input(b'\x1b')
        pty.feed_output(b'Interrupted')
        assert pty.get_state() == 'interrupted'
