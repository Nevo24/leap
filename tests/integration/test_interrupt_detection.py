"""'Interrupted' detection through real PTY output.

The basic contract: user presses Escape while the CLI is running, the
CLI eventually prints 'Interrupted…', the tracker moves
running → interrupted.  Includes regressions for large TUI redraw
chunks and ANSI-wrapped matches.
"""

from tests.conftest import PTYFixture


class TestInterruptDetection:
    def test_interrupted_in_running_state(self, pty: PTYFixture) -> None:
        """'Interrupted' in PTY output while running → interrupted."""
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        pty.send_input(b'\x1b')

        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.tracker.current_state == 'interrupted'

    def test_interrupted_lost_in_large_tui_redraw(
        self, pty: PTYFixture,
    ) -> None:
        """BUG REPRO: Claude TUI redraws the full screen after Escape.
        One big on_output chunk contains 'Interrupted' near the start,
        followed by prompt + status bar (hundreds of bytes).  The
        historical 512-byte buffer cap trimmed 'Interrupted' off the
        front — pyte renders the whole buffer, so this must still
        match."""
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        pty.send_input(b'\x1b')

        pty.feed_output(b'A' * 500)
        assert pty.tracker.current_state == 'running'

        chunk = b'\x1b[2J\x1b[H'
        chunk += b'Interrupted \xc2\xb7 What should Claude do instead?\r\n'
        chunk += b'\x1b[32m>\x1b[0m \r\n'
        chunk += b'\x1b[24;1H\x1b[K'
        chunk += b'Nevo.Mashiach [\xe2\x96\x88\xe2\x96\x88\xe2\x96\x88'
        chunk += b'           ] 10% | \x1b[36mOpus 4.6\x1b[0m | '
        chunk += b'\x1b[33mdefault\x1b[0m | \x1b[32m~$0.02\x1b[0m | '
        chunk += b'+ 0 \xe2\x80\x94 0 | v2.1.41 | 1 MCP server failed\r\n'
        chunk += b'\x1b[25;1H\x1b[K'
        chunk += b'\x1b[31m\xe2\x96\xba\xe2\x96\xba bypass permissions on'
        chunk += b' (shift+tab to cycle)\x1b[0m'
        chunk += b'\x1b[1;1H' * 100

        pty.feed_output(chunk)
        assert pty.tracker.current_state == 'interrupted'

    def test_interrupted_with_surrounding_ansi(
        self, pty: PTYFixture,
    ) -> None:
        """'Interrupted' detected even with ANSI codes around it,
        provided _interrupt_pending is set."""
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        pty.tracker.on_input(b'\x1b')
        assert pty.tracker._interrupt_pending

        pty.send_line(r'printf "\033[31mInterrupted\033[0m\n"')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.tracker.current_state == 'interrupted'
