"""The Escape race: Stop hook can fire 'idle' before PTY output of
'Interrupted' reaches the tracker.  The tracker must correct itself
once the pattern appears and `_interrupt_pending` is still set.
"""

from tests.conftest import PTYFixture


class TestEscapeRace:
    def test_interrupt_detected_after_stop_wrote_idle(
        self, pty: PTYFixture,
    ) -> None:
        """Sequence: running → idle signal → user hit Escape → later
        'Interrupted' arrives.  The escape-race path in _handle_idle_output
        reclaims interrupted state when the pattern appears while
        ``_interrupt_pending`` is still set."""
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        pty.tracker.on_input(b'\x1b')

        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.tracker.current_state == 'interrupted'
