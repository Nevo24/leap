"""Terminal resize during various states.

``on_resize`` updates the pyte dimensions but must not drop
in-progress flags or force a state transition.  Real terminals fire
SIGWINCH freely (dragging window, iterm split-pane reshuffling) and
the CLI redraws the screen — we need to be robust to that.
"""

from tests.conftest import PTYFixture


_DIALOG = (
    b'Allow tool?\r\n'
    b'1. Yes\r\n2. No\r\n'
    b'Enter to select  Esc to cancel\r\n'
)
_INDICATOR = b'\xe2\x9c\xbb Compacting conversation... (5s)'


class TestResizePreservesState:
    def test_resize_while_idle_stays_idle(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_input(b'x')
        assert pty.get_state() == 'idle'
        pty.resize(40, 120)
        assert pty.get_state() == 'idle'

    def test_resize_while_running_stays_running(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        assert pty.get_state() == 'running'
        pty.resize(50, 160)
        assert pty.get_state() == 'running'

    def test_resize_while_needs_permission_stays(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.resize(30, 100)
        assert pty.get_state() == 'needs_permission'

    def test_resize_while_interrupted_stays(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.feed_output(b'Interrupted')
        assert pty.get_state() == 'interrupted'

        pty.resize(30, 100)
        assert pty.get_state() == 'interrupted'

    def test_resize_while_compacting_stays_running(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_INDICATOR)
        assert pty.get_state() == 'running'

        pty.resize(30, 100)
        assert pty.get_state() == 'running'


class TestResizeScreenBuffer:
    def test_resize_does_not_invalidate_indicator(
        self, pty: PTYFixture,
    ) -> None:
        """After resize, ``_screen_has_running_indicator`` must keep
        working on the next output frame."""
        pty.tracker.on_send()
        pty.feed_output(_INDICATOR)
        assert pty.tracker._screen_has_running_indicator()

        pty.resize(40, 140)
        # CLI redraws at the new dimensions.
        pty.feed_output(b'\x1b[2J\x1b[H' + _INDICATOR)
        assert pty.tracker._screen_has_running_indicator()

    def test_zero_dim_resize_is_noop(
        self, pty: PTYFixture,
    ) -> None:
        """Pexpect occasionally sends (0, 0) when a pane is collapsing —
        must not crash or flip state."""
        pty.tracker.on_send()
        before = pty.tracker.current_state
        pty.tracker.on_resize(0, 0)
        assert pty.tracker.current_state == before


class TestResizeDuringTransitions:
    def test_resize_between_signal_and_poll(
        self, pty: PTYFixture,
    ) -> None:
        """Signal fires, then a resize, then we poll — the signal
        transition still lands."""
        pty.tracker.on_send()
        pty.write_signal('idle')
        pty.resize(50, 160)
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

    def test_resize_doesnt_trigger_false_running(
        self, pty: PTYFixture,
    ) -> None:
        """Resize produces no output, so it must not act like a keystroke
        (no idle→running)."""
        pty.tracker.on_input(b'x')
        pty.resize(40, 120)
        assert pty.get_state() == 'idle'
