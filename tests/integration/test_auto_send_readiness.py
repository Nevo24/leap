"""The ``is_ready`` / ``is_ready_for_state`` contract.

The auto-sender calls ``is_ready()`` on every poll to decide whether
to flush the next queued message.  Contract: **only IDLE returns
True**, regardless of auto-send mode.  (Permission auto-approve in
ALWAYS mode is handled separately in the server loop, not here.)
"""

from leap.cli_providers.states import AutoSendMode

from tests.conftest import PTYFixture


_DIALOG = (
    b'Allow tool?\r\n1. Yes\r\n2. No\r\n'
    b'Enter to select  Esc to cancel\r\n'
)


class TestReadinessPerState:
    def test_idle_is_ready(self, pty: PTYFixture) -> None:
        assert pty.tracker.is_ready_for_state('idle') is True

    def test_running_is_not_ready(self, pty: PTYFixture) -> None:
        pty.tracker.on_send()
        assert pty.tracker.is_ready_for_state('running') is False

    def test_needs_permission_is_not_ready(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'
        assert pty.tracker.is_ready_for_state('needs_permission') is False

    def test_needs_input_is_not_ready(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_input')
        assert pty.get_state() == 'needs_input'
        assert pty.tracker.is_ready_for_state('needs_input') is False

    def test_interrupted_is_not_ready(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.feed_output(b'Interrupted')
        assert pty.tracker.is_ready_for_state('interrupted') is False


class TestReadinessModeIndependence:
    def test_pause_mode_on_idle(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.auto_send_mode = AutoSendMode.PAUSE
        assert pty.tracker.is_ready_for_state('idle') is True

    def test_always_mode_on_idle(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.auto_send_mode = AutoSendMode.ALWAYS
        assert pty.tracker.is_ready_for_state('idle') is True

    def test_always_mode_does_not_flip_readiness_for_running(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.auto_send_mode = AutoSendMode.ALWAYS
        pty.tracker.on_send()
        assert pty.tracker.is_ready_for_state('running') is False

    def test_always_mode_does_not_flip_readiness_for_permission(
        self, pty: PTYFixture,
    ) -> None:
        """Auto-approve is NOT in is_ready — that's a separate server-
        loop decision; is_ready stays strict."""
        pty.tracker.auto_send_mode = AutoSendMode.ALWAYS
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'
        assert pty.tracker.is_ready_for_state('needs_permission') is False


class TestIsReadyLivePoll:
    def test_is_ready_uses_live_get_state(
        self, pty: PTYFixture,
    ) -> None:
        """is_ready() polls get_state internally — changes in signal
        file propagate without manual get_state calls."""
        assert pty.tracker.is_ready(pty_alive=True) is True

        pty.tracker.on_send()
        assert pty.tracker.is_ready(pty_alive=True) is False

        pty.write_signal('idle')
        # Polling via is_ready picks up the signal.
        assert pty.tracker.is_ready(pty_alive=True) is True

    def test_dead_pty_is_ready(
        self, pty: PTYFixture,
    ) -> None:
        """Dead PTY is normalised to idle → ready.  Harmless because
        the auto-sender gates on ``cli_running`` first."""
        pty.tracker.on_send()
        assert pty.tracker.is_ready(pty_alive=False) is True


class TestModePersistence:
    def test_mode_set_and_read_back(
        self, pty: PTYFixture,
    ) -> None:
        assert pty.tracker.auto_send_mode == AutoSendMode.PAUSE
        pty.tracker.auto_send_mode = AutoSendMode.ALWAYS
        assert pty.tracker.auto_send_mode == AutoSendMode.ALWAYS
        pty.tracker.auto_send_mode = AutoSendMode.PAUSE
        assert pty.tracker.auto_send_mode == AutoSendMode.PAUSE
