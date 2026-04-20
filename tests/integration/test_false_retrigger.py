"""After a running → idle transition, subsequent prompt / TUI rendering
must not falsely re-trigger 'running'.  Only on_send() or Enter-in-idle
can move idle → running.
"""

from tests.conftest import PTYFixture


class TestFalseRetrigger:
    def test_output_after_signal_idle_stays_idle(
        self, pty: PTYFixture,
    ) -> None:
        """on_send → running → signal idle → more output should NOT
        re-trigger running (no new on_send/Enter)."""
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        pty.send_line('printf "%0.sB" $(seq 1 300)')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.get_state() == 'idle'

    def test_new_send_after_idle_allows_running(
        self, pty: PTYFixture,
    ) -> None:
        """After idle, a fresh on_send() transitions back to running."""
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        pty.tracker.on_send()
        assert pty.get_state() == 'running'
