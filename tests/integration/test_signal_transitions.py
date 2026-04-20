"""Signal-file transitions with real PTY I/O.

Covers the primary happy path: a hook writes a state to the signal
file and the tracker picks it up on the next poll.  Verifies that the
late-notification guard requires dialog patterns on screen before
accepting needs_permission / needs_input signals.
"""

from tests.conftest import PTYFixture


class TestSignalFile:
    """Signal file transitions with real file I/O."""

    def test_on_send_then_signal_idle(self, pty: PTYFixture) -> None:
        """on_send → running, then signal file → idle."""
        assert pty.get_state() == 'idle'
        pty.tracker.on_send()
        assert pty.get_state() == 'running'
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

    def test_signal_needs_permission(self, pty: PTYFixture) -> None:
        pty.tracker.on_send()
        # Late-notification guard (a821533) requires dialog patterns
        # visible on screen before accepting the signal, to reject
        # late-arriving Notification hooks that fire after the CLI
        # already finished.
        pty.feed_output(
            b'Allow tool?  Enter to select  Esc to cancel\n')
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

    def test_signal_needs_input(self, pty: PTYFixture) -> None:
        pty.tracker.on_send()
        # See test_signal_needs_permission — dialog patterns required.
        pty.feed_output(
            b'Allow tool?  Enter to select  Esc to cancel\n')
        pty.write_signal('needs_input')
        assert pty.get_state() == 'needs_input'
