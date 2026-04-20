"""Rapid / overlapping state transitions.

Real usage produces bursts of events that can arrive inside a single
poll window: Stop fires while the user is mid-keystroke, a second
Stop fires before the first idle is consumed, send is called while
the tracker is still in needs_permission, etc.  The state machine
must stay coherent through these bursts and never wedge.
"""

from tests.conftest import PTYFixture


_DIALOG = (
    b'Allow tool?\r\n'
    b'1. Yes\r\n2. No\r\n'
    b'Enter to select  Esc to cancel\r\n'
)


class TestDuplicateStopHooks:
    def test_double_idle_signal_is_idempotent(
        self, pty: PTYFixture,
    ) -> None:
        """Two Stop hooks firing back-to-back must not destabilise
        state or leave a leftover signal file."""
        pty.tracker.on_send()
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        # Second hook fires with state already idle — no-op.
        pty.write_signal('idle')
        assert pty.get_state() == 'idle'

    def test_idle_then_new_send_returns_running(
        self, pty: PTYFixture,
    ) -> None:
        """Idle → immediate on_send → running with fresh _running_since."""
        pty.tracker.on_send()
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        first = pty.tracker._running_since
        pty.tracker.on_send()
        assert pty.tracker.current_state == 'running'
        assert pty.tracker._running_since >= first


class TestSendWhileWaiting:
    def test_on_send_from_needs_permission_jumps_to_running(
        self, pty: PTYFixture,
    ) -> None:
        """Auto-sender may call on_send() if a queued item is ready
        and mode is ALWAYS — must unconditionally move to running."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.tracker.on_send()
        assert pty.tracker.current_state == 'running'


class TestStopHookWhileTyping:
    def test_stop_fires_while_user_typing_resets_cleanly(
        self, pty: PTYFixture,
    ) -> None:
        """User is typing in the idle prompt (or just pressed Enter
        to submit).  Stop fires mid-keystroke.  State must still
        settle to idle once the signal resolves."""
        pty.tracker.on_input(b'h')
        pty.tracker.on_input(b'i')
        pty.tracker.on_input(b'\r')
        assert pty.tracker.current_state == 'running'

        pty.tracker.on_input(b'e')  # user types into the idle-again buffer
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'


class TestEscapeThenEnter:
    def test_escape_followed_by_enter_in_running(
        self, pty: PTYFixture,
    ) -> None:
        """User hits Escape mid-response, then immediately hits Enter
        to submit a follow-up — must land in running, not in an
        interrupted wedge (since the 'Interrupted' pattern never
        rendered)."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        assert pty.tracker._interrupt_pending is True

        # Claude ignored the Escape (didn't emit 'Interrupted') and
        # the Stop hook fires normally.
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'
        # _interrupt_pending should have been cleared.
        assert pty.tracker._interrupt_pending is False

        # User submits a new message.
        pty.tracker.on_input(b'go\r')
        assert pty.tracker.current_state == 'running'


class TestInterleavedEvents:
    def test_stop_and_new_send_same_poll_window(
        self, pty: PTYFixture,
    ) -> None:
        """Between two polls: Stop fires → idle, then the auto-sender
        calls on_send(). When we poll, we must see running (on_send's
        unconditional write wins)."""
        pty.tracker.on_send()
        pty.write_signal('idle')
        # Before we poll, simulate the auto-sender sending the next
        # queued message.
        pty.tracker.on_send()
        # The poll now sees running, and unlinks the stale signal.
        assert pty.get_state() == 'running'

    def test_signal_then_output_chunk(
        self, pty: PTYFixture,
    ) -> None:
        """Signal idle arrives, then a trailing output chunk from the
        previous turn.  We must already be idle and the chunk mustn't
        re-trigger running."""
        pty.tracker.on_send()
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        # Trailing output (status bar redraw) — must stay idle.
        pty.feed_output(b'\x1b[?25h\x1b[24;1H\x1b[KOpus 4.6')
        assert pty.get_state() == 'idle'


class TestSignalFileRaces:
    """The running-indicator path in ``_handle_idle_output`` unlinks
    the signal file unconditionally when it transitions idle→running.
    That's safe when the file contains a stale ``idle`` (the common
    case), but theoretically racy: a Notification hook could have
    written ``needs_permission`` in the microsecond gap.  The PTY
    cursor+silence → needs_permission fallback is the safety net.
    """

    def test_signal_unlinked_when_indicator_fires(
        self, pty: PTYFixture,
    ) -> None:
        """Document the unlink behaviour — when the compact indicator
        moves us idle→running, the signal file is cleared."""
        from tests.integration.test_compact_scenarios import (
            _CLEAR, _INDICATOR,
        )

        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        # Re-arm signal file with a stale 'idle' (common race setup).
        pty.write_signal('idle')
        pty.feed_output(_CLEAR + _INDICATOR)
        assert pty.tracker.current_state == 'running'
        assert not pty.signal_file.exists()

    def test_lost_needs_permission_recovers_via_pty(
        self, pty: PTYFixture,
    ) -> None:
        """Worst-case race: a needs_permission signal gets unlinked
        by our transition.  Coverage net: the PTY cursor+silence path
        detects the dialog on screen and re-enters needs_permission
        without the hook."""
        from tests.integration.test_compact_scenarios import (
            _CLEAR, _INDICATOR,
        )

        def _advance(seconds: float) -> None:
            base = pty.tracker._clock()
            pty.tracker._clock = lambda: base + seconds

        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        # Race setup: a needs_permission hook fires AT THE SAME MOMENT
        # as the compact indicator appears.  Our on_output runs first
        # and unlinks.  Simulate by writing needs_permission and then
        # feeding the indicator.
        pty.write_signal('needs_permission')
        pty.feed_output(_CLEAR + _INDICATOR)
        assert pty.tracker.current_state == 'running'
        assert not pty.signal_file.exists()  # lost

        # Compaction ends; dialog is still live (CLI is waiting).  The
        # cursor+silence running→needs_permission fallback takes over.
        pty.feed_output(
            b'\x1b[?25h'
            b'Allow tool?\r\n'
            b'1. Yes\r\n2. No\r\n'
            b'Enter to select  Esc to cancel\r\n',
        )
        _advance(6.0)
        # PTY fallback recovers the lost signal.
        assert pty.get_state() == 'needs_permission'

    def test_no_signal_loss_when_already_running(
        self, pty: PTYFixture,
    ) -> None:
        """When state is already running and the indicator appears,
        the ``_handle_idle_output`` path is never entered — no unlink
        happens, so a concurrent hook signal is preserved."""
        from tests.integration.test_compact_scenarios import (
            _CLEAR, _INDICATOR,
        )

        pty.tracker.on_send()  # state=running
        pty.write_signal('needs_permission')
        # Feed compact indicator while running — idle handler not called.
        pty.feed_output(_CLEAR + _INDICATOR)
        # Signal file preserved.
        assert pty.signal_file.exists()


class TestRapidDialogChurn:
    def test_permission_then_immediate_input_then_stop(
        self, pty: PTYFixture,
    ) -> None:
        """Permission prompt, user answers instantly, Stop fires —
        all within the same poll window."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.tracker.on_input(b'1')
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

    def test_two_dialogs_back_to_back(
        self, pty: PTYFixture,
    ) -> None:
        """CLI shows dialog A, user answers, dialog B shows before
        the idle signal has been flushed.  State should end up on B."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.tracker.on_input(b'1')
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_input')
        assert pty.get_state() == 'needs_input'
