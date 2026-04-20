"""Output-alone must not trigger running.

In the event-driven state model, running is only reached via on_send()
or Enter-in-idle — not from PTY output.  Background redraws or spurious
bytes must not move state forward.
"""

import time

from tests.conftest import PTYFixture


class TestOutputAccumulation:
    """PTY output alone (ANSI codes, line endings, terminal rendering)."""

    def test_real_pty_output_does_not_trigger_running(
        self, pty: PTYFixture,
    ) -> None:
        """Even with _seen_user_input set, raw output must not flip
        idle → running."""
        pty.send_input(b'x')
        time.sleep(0.1)

        pty.send_line('printf "%0.sA" $(seq 1 300)')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.get_state() == 'idle'

    def test_no_false_running_without_user_input(
        self, pty: PTYFixture,
    ) -> None:
        """Without any prior keystroke, output definitely must not
        trigger running."""
        pty.send_line('printf "%0.sB" $(seq 1 300)')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.get_state() == 'idle'
