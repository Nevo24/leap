"""Tests for CLIStateTracker event-driven state machine."""

import json
from pathlib import Path
from typing import List

import pytest

from leap.cli_providers.claude import ClaudeProvider
from leap.cli_providers.codex import CodexProvider
from leap.cli_providers.states import CLIState, WAITING_STATES
from leap.server.state_tracker import CLIStateTracker as ClaudeStateTracker
from leap.utils.constants import SAFETY_SILENCE_TIMEOUT, SAFETY_WAITING_TIMEOUT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tracker(
    tmp_path: Path,
    t: List[float],
    auto_send_mode: str = 'pause',
    provider: object = None,
) -> ClaudeStateTracker:
    """Create a tracker with fake clock and a signal file in *tmp_path*."""
    signal_file = tmp_path / "test.signal"
    kwargs = dict(
        signal_file=signal_file,
        auto_send_mode=auto_send_mode,
        clock=lambda: t[0],
    )
    if provider is not None:
        kwargs['provider'] = provider
    return ClaudeStateTracker(**kwargs)


def write_signal(tracker: ClaudeStateTracker, state: str) -> None:
    """Write a JSON signal file that the tracker will read."""
    tracker._signal_file.write_text(json.dumps({"state": state}))


def feed_screen_text(tracker: ClaudeStateTracker, text: str) -> None:
    """Feed text into the tracker's pyte screen (simulates PTY output).

    Uses ANSI escape sequences to position and write text so it appears
    on the virtual screen for pattern matching.
    """
    # Move to top-left and clear screen, then write text
    esc = f'\x1b[H\x1b[2J{text}'
    tracker.on_output(esc.encode('utf-8'))


def feed_with_hidden_cursor(tracker: ClaudeStateTracker, text: str) -> None:
    """Feed text with cursor hidden (simulates TUI rendering)."""
    esc = f'\x1b[?25l\x1b[H\x1b[2J{text}'
    tracker.on_output(esc.encode('utf-8'))


def feed_with_visible_cursor(tracker: ClaudeStateTracker, text: str) -> None:
    """Feed text with cursor visible (simulates idle prompt)."""
    esc = f'\x1b[?25h\x1b[H\x1b[2J{text}'
    tracker.on_output(esc.encode('utf-8'))


# ---------------------------------------------------------------------------
# Basic
# ---------------------------------------------------------------------------

class TestBasic:
    def test_initial_state_is_idle(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_pty_dead_returns_idle(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        assert tracker.get_state(pty_alive=True) == 'running'
        assert tracker.get_state(pty_alive=False) == 'idle'

    def test_auto_send_mode_property(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        assert tracker.auto_send_mode == 'pause'
        tracker.auto_send_mode = 'always'
        assert tracker.auto_send_mode == 'always'


# ---------------------------------------------------------------------------
# on_send → running
# ---------------------------------------------------------------------------

class TestOnSend:
    def test_on_send_sets_running(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_on_send_deletes_signal_file(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        write_signal(tracker, 'idle')
        assert tracker._signal_file.exists()
        tracker.on_send()
        assert not tracker._signal_file.exists()

    def test_on_send_clears_interrupt_pending(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x1b')  # Escape → interrupt pending
        assert tracker._interrupt_pending is True
        tracker.on_send()
        assert tracker._interrupt_pending is False


# ---------------------------------------------------------------------------
# on_input Enter → running (all providers)
# ---------------------------------------------------------------------------

class TestEnterInIdle:
    def test_enter_in_idle_triggers_running(self, tmp_path: Path) -> None:
        """Enter at idle prompt triggers running (server-terminal typing)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'

    def test_on_send_still_triggers_running(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        assert tracker.get_state(pty_alive=True) == 'running'


# ---------------------------------------------------------------------------
# Enter in waiting states → running (Fix 2)
# ---------------------------------------------------------------------------

class TestEnterInWaitingStates:
    """Enter in NEEDS_PERMISSION/NEEDS_INPUT immediately transitions to RUNNING.

    Before Fix 2, the monitor showed "Permission" for the entire duration
    of the subsequent task (until the Stop hook fired).
    """

    def test_needs_permission_enter_triggers_running_immediately(
        self, tmp_path: Path,
    ) -> None:
        """Enter at a permission dialog flips state to running without
        waiting for the Stop hook."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission

        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'

    def test_needs_input_enter_triggers_running_immediately(
        self, tmp_path: Path,
    ) -> None:
        """Enter at an input elicitation dialog flips state to running."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'What should I name this?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_input')
        tracker.get_state(pty_alive=True)  # → needs_input

        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'


# ---------------------------------------------------------------------------
# Signal file transitions
# ---------------------------------------------------------------------------

class TestSignalFile:
    def test_signal_file_idle(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_signal_file_needs_permission(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # Dialog patterns must be on screen for the guard to accept
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_signal_file_needs_input(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'What should I name this?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'needs_input'

    def test_signal_file_invalid_json_ignored(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker._signal_file.write_text("not valid json {{{")
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_signal_file_unknown_state_ignored(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        write_signal(tracker, 'bogus')
        assert tracker.get_state(pty_alive=True) == 'running'


# ---------------------------------------------------------------------------
# Interrupt detection via _interrupt_pending flag
# ---------------------------------------------------------------------------

class TestInterruptPendingFlag:
    def test_escape_sets_interrupt_pending(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x1b')
        assert tracker._interrupt_pending is True

    def test_ctrl_c_sets_interrupt_pending(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x03')
        assert tracker._interrupt_pending is True

    def test_regular_input_does_not_set_interrupt_pending(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        assert tracker._interrupt_pending is False

    def test_idle_signal_with_interrupt_pending_and_pattern_goes_interrupted(
        self, tmp_path: Path,
    ) -> None:
        """Interrupt pending + pattern on screen → interrupted."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.on_input(b'\x1b')  # interrupt pending
        # CLI shows "Interrupted" on screen
        feed_screen_text(tracker, 'Interrupted')
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_idle_signal_with_interrupt_pending_but_no_pattern_goes_idle(
        self, tmp_path: Path,
    ) -> None:
        """Interrupt pending but NO pattern on screen → idle.

        The user pressed Escape but the CLI ignored it and finished
        normally.  No 'Interrupted' appeared.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.on_input(b'\x1b')  # interrupt pending
        # CLI finishes normally — no "Interrupted" on screen
        feed_screen_text(tracker, 'Done processing')
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_idle_signal_without_interrupt_pending_goes_idle(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_interrupt_pending_cleared_on_transition(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')
        assert tracker._interrupt_pending is True
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # triggers transition
        assert tracker._interrupt_pending is False

    def test_csi_u_ctrl_c_sets_interrupt_pending(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running (so CSI input is processed)
        # Kitty CSI u for Ctrl+C: \x1b[3u
        tracker.on_input(b'\x1b[3u')
        assert tracker._interrupt_pending is True

    def test_csi_u_escape_sets_interrupt_pending(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # Kitty CSI u for Escape: \x1b[27u
        tracker.on_input(b'\x1b[27u')
        assert tracker._interrupt_pending is True


# ---------------------------------------------------------------------------
# _user_responded flag (waiting state protection)
# ---------------------------------------------------------------------------

class TestUserRespondedFlag:
    def test_input_in_waiting_state_sets_user_responded(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'What should I do?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_input')
        tracker.get_state(pty_alive=True)  # → needs_input
        tracker.on_input(b'x')
        assert tracker._user_responded is True

    def test_idle_signal_blocked_without_user_responded(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'What should I do?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_input')
        tracker.get_state(pty_alive=True)  # → needs_input
        # Signal idle without user responding
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'needs_input'

    def test_idle_signal_accepted_with_user_responded(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        write_signal(tracker, 'needs_input')
        tracker.get_state(pty_alive=True)  # → needs_input
        tracker.on_input(b'x')  # user responded
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_interrupted_protected_from_idle_without_user_responded(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')  # pattern on screen
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → interrupted
        # Try to signal idle without responding
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_interrupted_yields_to_idle_with_user_responded(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → interrupted
        tracker.on_input(b'1')  # user responded
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_interrupted_protected_from_needs_input_without_user_responded(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')
        feed_screen_text(tracker, 'Interrupted')
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → interrupted
        # Notification hook writes needs_input (race)
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_user_responded_cursor_hidden_goes_running(
        self, tmp_path: Path,
    ) -> None:
        """When user answers a permission prompt in the terminal and the
        CLI starts processing (cursor hidden), state should transition
        from needs_permission → running without waiting for Stop hook."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # Feed dialog patterns so Late Notification Guard accepts signal
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission
        tracker.on_input(b'1')  # user answered
        # CLI starts processing — cursor hidden in output
        feed_with_hidden_cursor(tracker, 'Processing...')
        # Poll detects cursor hidden + user_responded → running
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_user_responded_cursor_visible_stays_waiting(
        self, tmp_path: Path,
    ) -> None:
        """If the user responded but cursor is still visible (dialog
        still showing), state should remain needs_permission."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission
        tracker.on_input(b'x')  # user typed something
        # Dialog still showing — cursor visible output
        feed_screen_text(tracker, 'Select an option')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_no_user_responded_cursor_hidden_stays_waiting(
        self, tmp_path: Path,
    ) -> None:
        """Cursor hidden without _user_responded should NOT trigger
        the transition — could be a TUI redraw during the dialog."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission
        # Cursor hidden output but user hasn't responded
        feed_with_hidden_cursor(tracker, 'Rendering...')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_interrupted_responded_cursor_hidden_goes_running(
        self, tmp_path: Path,
    ) -> None:
        """INTERRUPTED → running when user types and cursor hides.
        Must also set _suppress_stale_interrupt."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')
        # on_output detected interrupt_pending + pattern → interrupted
        # (state tracker wrote "interrupted" to signal file internally)
        assert tracker.current_state == 'interrupted'
        # Delete signal file — real scenario: the self-written
        # "interrupted" signal is ignored by get_state (line 768)
        tracker._signal_file.unlink(missing_ok=True)
        tracker.on_input(b'1')  # user types new input
        feed_with_hidden_cursor(tracker, 'Working...')
        assert tracker.get_state(pty_alive=True) == 'running'
        assert tracker._suppress_stale_interrupt is True


# ---------------------------------------------------------------------------
# Interrupt pattern on pyte screen
# ---------------------------------------------------------------------------

class TestInterruptPatternOnScreen:
    def test_interrupted_in_running_with_flag(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'interrupted'

    def test_interrupted_in_running_without_flag_stays_running(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # No escape/ctrl+c pressed
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'running'

    def test_interrupted_in_idle_with_flag(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_input(b'\x1b')  # interrupt pending
        # Stop hook already raced ahead to idle, now PTY shows pattern
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'interrupted'

    def test_needs_input_corrected_to_interrupted(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        write_signal(tracker, 'needs_input')
        tracker.get_state(pty_alive=True)  # → needs_input
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'interrupted'

    def test_interrupted_pattern_split_across_lines(self, tmp_path: Path) -> None:
        """Pattern that wraps across pyte screen lines is still detected.

        On a narrow terminal, 'Interrupted' could end up split at a line
        boundary.  compact_full (spaces+newlines removed) handles this.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')  # interrupt pending
        # Resize to narrow screen so text wraps
        tracker.on_resize(24, 15)
        # Position cursor near end of line, write text that wraps
        # Col 10 + "Interrupted" (11 chars) → wraps at col 15
        tracker.on_output(b'\x1b[1;10HInterrupted')
        assert tracker.current_state == 'interrupted'


class TestConfirmedPatternCrossLine:
    """Confirmed interrupt pattern uses compact_lines (newlines preserved)
    to prevent false positives from cross-line text concatenation."""

    def test_cross_line_text_no_false_positive(self, tmp_path: Path) -> None:
        """'Conversation' on line 1 + 'interrupted' on line 2 should
        NOT form 'Conversationinterrupted' for confirmed pattern."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()
        # Two lines that would concatenate to "Conversationinterrupted"
        # if newlines were removed
        tracker.on_output(
            b'\x1b[1;1HConversation\x1b[2;1Hinterrupted the flow'
        )
        # Should stay running — cross-line match blocked
        assert tracker.current_state == 'running'

    def test_same_line_confirmed_pattern_detected(self, tmp_path: Path) -> None:
        """'Conversation interrupted' on SAME line should be detected."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_input(b'x')
        tracker.on_send()
        # Same line — after space removal: "Conversationinterrupted"
        tracker.on_output(
            b'\x1b[1;1HConversation interrupted - tell the model'
        )
        assert tracker.current_state == 'interrupted'


# ---------------------------------------------------------------------------
# Auto-resume via cursor visibility
# ---------------------------------------------------------------------------

class TestAutoResume:
    def test_cursor_hidden_triggers_running_at_poll(self, tmp_path: Path) -> None:
        """Auto-resume is detected at poll time (get_state), not on_output.

        This avoids false triggers from mid-render cursor-hidden state
        in brief TUI redraws.  By poll time (0.5s), brief redraws have
        completed and cursor is visible again.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        # Enter idle via signal (simulate: was running, now idle)
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle
        # _user_input_since_idle was cleared on transition
        # CLI auto-starts: cursor hidden output
        t[0] = 5.0
        feed_with_hidden_cursor(tracker, 'Processing...')
        # on_output doesn't trigger transition — check at poll time
        assert tracker.current_state == 'idle'
        # Poll detects cursor hidden → running
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_cursor_hidden_blocked_if_user_typed(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle
        # User types in idle (sets _user_input_since_idle)
        tracker.on_input(b'y')
        # Cursor hidden output should NOT trigger running (user typed)
        feed_with_hidden_cursor(tracker, 'Echo of typing')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_cursor_visible_does_not_trigger_running(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)
        feed_with_visible_cursor(tracker, 'Status bar update')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_auto_resume_needs_seen_user_input(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # No user input ever seen (startup)
        feed_with_hidden_cursor(tracker, 'Startup output')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_brief_redraw_does_not_false_trigger(self, tmp_path: Path) -> None:
        """A brief TUI redraw (cursor hide → content → cursor show)
        within one output chunk does not trigger auto-resume."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle
        # Brief redraw: hide cursor, render, show cursor — all in one chunk
        tracker.on_output(
            b'\x1b[?25l\x1b[H\x1b[2JStatus update\x1b[?25h'
        )
        # Cursor is visible after the complete render → no auto-resume
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# Safety fallback timeouts
# ---------------------------------------------------------------------------

class TestSafetyTimeouts:
    def test_silence_timeout_triggers_idle(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Cursor hidden (simulates processing that hung without output)
        tracker.on_output(b'\x1b[?25lsome output')
        t[0] = 1.0 + SAFETY_SILENCE_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_silence_timeout_not_before_deadline(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Hide cursor (simulates active processing with cursor hidden)
        tracker.on_output(b'\x1b[?25lsome output')
        t[0] = 1.0 + SAFETY_SILENCE_TIMEOUT - 1.0
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_cursor_visible_plus_silence_triggers_idle(
        self, tmp_path: Path,
    ) -> None:
        """Running with cursor visible + output silence > 5s → idle.

        Handles /clear sent from queue (on_send → running, but Stop
        hook doesn't fire).  Threshold bumped from 2s to 5s to avoid
        false-idle flicker during long streaming responses.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running (e.g., /clear from queue)
        t[0] = 1.0
        # TUI redraws with cursor visible at the end
        feed_with_visible_cursor(tracker, 'Cleared screen')
        # Wait > 5s of silence
        t[0] = 7.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_cursor_visible_during_streaming_stays_running(
        self, tmp_path: Path,
    ) -> None:
        """During streaming, output arrives constantly.  Even if cursor
        is visible between frames, silence < 2s keeps state running."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(tracker, 'Streaming token 1')
        # Only 0.5s since last output — not enough silence (need >2s)
        t[0] = 1.1
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_waiting_timeout_triggers_idle(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission
        t[0] = 1.0
        tracker.on_output(b'prompt text')
        # Remove signal file so timeout can fire
        tracker._signal_file.unlink(missing_ok=True)
        t[0] = 1.0 + SAFETY_WAITING_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_waiting_timeout_respects_signal_confirmation(
        self, tmp_path: Path,
    ) -> None:
        """When the signal still says needs_permission AND the dialog
        patterns are still visible on screen, the 60s safety timeout
        must keep us waiting (not force idle).  Refreshing the dialog
        text with every redraw is what a real Ink TUI does."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)
        t[0] = 1.0
        # Redraw the dialog (still live) so the waiting→idle
        # cursor+silence fallback doesn't see indicator-gone.
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        # Signal still confirms needs_permission
        t[0] = 1.0 + SAFETY_WAITING_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'


# ---------------------------------------------------------------------------
# Trust dialog detection via pyte screen
# ---------------------------------------------------------------------------

class TestTrustDialog:
    def test_trust_dialog_detected_on_screen(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Feed trust dialog text (startup, no user input)
        feed_screen_text(tracker, 'Do you trust the contents of this directory?')
        assert tracker.current_state == 'needs_permission'

    def test_trust_dialog_resume_goes_to_idle(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        feed_screen_text(tracker, 'Do you trust the contents of this directory?')
        assert tracker.current_state == 'needs_permission'
        # User selects option → on_send → running
        tracker.on_send()
        assert tracker.current_state == 'running'
        # Trust dialog phase: output → idle
        t[0] = 1.0
        feed_screen_text(tracker, 'Welcome to Claude Code')
        assert tracker.current_state == 'idle'

    def test_normal_output_does_not_trigger_trust_dialog(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # user input seen → skip startup detection
        feed_screen_text(tracker, 'Do you trust the contents of this directory?')
        assert tracker.current_state == 'idle'


# ---------------------------------------------------------------------------
# Stale interrupt suppression
# ---------------------------------------------------------------------------

class TestStaleInterruptSuppression:
    def test_resume_from_interrupted_suppresses_confirmed_pattern(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')
        feed_screen_text(tracker, 'Interrupted')
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → interrupted
        # Send from interrupted → running (sets suppression)
        tracker.on_send()
        assert tracker._suppress_stale_interrupt is True

    def test_suppression_cleared_on_normal_send(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Non-interrupted send
        tracker.on_send()
        assert tracker._suppress_stale_interrupt is False

    def test_suppression_auto_cleared_when_pattern_scrolls_off(
        self, tmp_path: Path,
    ) -> None:
        """_suppress_stale_interrupt is cleared when the interrupted
        pattern no longer appears on the pyte screen."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()
        tracker.on_input(b'\x1b')
        feed_screen_text(tracker, 'Interrupted')
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → interrupted
        tracker.on_send()  # → running, suppression=True
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle, suppression still True
        assert tracker._suppress_stale_interrupt is True

        # Output without "Interrupted" → suppression cleared
        feed_screen_text(tracker, 'Normal idle prompt')
        assert tracker._suppress_stale_interrupt is False

        # Now a real interrupt should be detected
        tracker.on_input(b'\x1b')
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'interrupted'


# ---------------------------------------------------------------------------
# on_input filtering
# ---------------------------------------------------------------------------

class TestOnInputFiltering:
    def test_csi_sequences_filtered(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x1b[I')  # focus in
        assert tracker._interrupt_pending is False
        assert tracker._seen_user_input is False

    def test_single_escape_accepted(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x1b')
        assert tracker._interrupt_pending is True

    def test_regular_keys_accepted(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'a')
        assert tracker._seen_user_input is True
        assert tracker._interrupt_pending is False

    def test_ctrl_c_in_multi_byte_data(self, tmp_path: Path) -> None:
        """Ctrl+C bundled with text in one on_input call."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'hello\x03')
        assert tracker._interrupt_pending is True
        assert tracker._seen_user_input is True

    def test_embedded_csi_u_escape(self, tmp_path: Path) -> None:
        """CSI u Escape sequence embedded in multi-byte data (not at pos 0)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Typed "hi" then pressed Escape via kitty protocol
        tracker.on_input(b'hi\x1b[27u')
        assert tracker._interrupt_pending is True

    def test_embedded_csi_focus_not_interrupt(self, tmp_path: Path) -> None:
        """Focus event CSI embedded in data should NOT set interrupt."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Text + focus in event (not an interrupt)
        tracker.on_input(b'hi\x1b[I')
        assert tracker._interrupt_pending is False
        # But _seen_user_input should be True (text was typed)
        assert tracker._seen_user_input is True

    def test_focus_event_plus_ctrl_c(self, tmp_path: Path) -> None:
        """Focus event followed by Ctrl+C — both must be handled."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x1b[I\x03')
        assert tracker._interrupt_pending is True
        assert tracker._seen_user_input is True

    def test_pure_focus_event_filtered(self, tmp_path: Path) -> None:
        """Pure focus event (no real user input) is filtered entirely."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x1b[I')
        assert tracker._interrupt_pending is False
        assert tracker._seen_user_input is False

    def test_mixed_text_interrupt_enter(self, tmp_path: Path) -> None:
        """Text + Ctrl+C + Enter all in one chunk.

        Enter triggers running and clears interrupt_pending.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'hello\x03\r')
        assert tracker._interrupt_pending is False
        assert tracker.current_state == 'running'

    def test_text_with_interrupt_sets_user_input_since_idle(
        self, tmp_path: Path,
    ) -> None:
        """Printable text alongside interrupt should still set
        _user_input_since_idle (the text counts)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle
        # Text + Ctrl+C — has printable content
        tracker.on_input(b'hello\x03')
        assert tracker._user_input_since_idle is True

    def test_pure_interrupt_does_not_set_user_input_since_idle(
        self, tmp_path: Path,
    ) -> None:
        """Pure Ctrl+C without text should NOT set _user_input_since_idle."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle
        tracker.on_input(b'\x03')
        assert tracker._user_input_since_idle is False

    def test_null_bytes_ignored(self, tmp_path: Path) -> None:
        """Null bytes (terminal noise) should not set any flags."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x00\x00\x00')
        assert tracker._seen_user_input is False
        assert tracker._interrupt_pending is False
        assert tracker._user_input_since_idle is False


# ---------------------------------------------------------------------------
# is_ready
# ---------------------------------------------------------------------------

class TestIsReady:
    def test_is_ready_pause_mode(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t, auto_send_mode='pause')
        assert tracker.is_ready_for_state('idle') is True
        assert tracker.is_ready_for_state('running') is False
        assert tracker.is_ready_for_state('needs_permission') is False
        assert tracker.is_ready_for_state('needs_input') is False
        assert tracker.is_ready_for_state('interrupted') is False

    def test_is_ready_always_mode(self, tmp_path: Path) -> None:
        """In both modes, only IDLE triggers queue message sending.

        Permission auto-approve is handled by the server loop, not
        by is_ready_for_state.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t, auto_send_mode='always')
        assert tracker.is_ready_for_state('idle') is True
        assert tracker.is_ready_for_state('running') is False
        assert tracker.is_ready_for_state('needs_permission') is False
        assert tracker.is_ready_for_state('needs_input') is False
        assert tracker.is_ready_for_state('interrupted') is False


# ---------------------------------------------------------------------------
# on_resize
# ---------------------------------------------------------------------------

class TestResize:
    def test_resize_updates_screen(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_resize(30, 100)
        assert tracker._screen.lines == 30
        assert tracker._screen.columns == 100

    def test_resize_during_idle_stays_idle(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_resize(30, 100)
        # Output after resize (TUI redraw) should not trigger running
        feed_with_visible_cursor(tracker, 'Redrawn content')
        assert tracker.current_state == 'idle'


# ---------------------------------------------------------------------------
# Prompt output via pyte screen snapshot
# ---------------------------------------------------------------------------

class TestPromptOutput:
    def test_prompt_output_from_snapshot(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # Dialog patterns must be on screen for the guard to accept
        feed_screen_text(
            tracker,
            'Allow tool use?  Enter to select  Esc to cancel',
        )
        # Enter needs_permission via signal
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)
        # Feed prompt text while in needs_permission
        feed_screen_text(tracker, 'Allow tool use?\n1. Yes\n2. No')
        result = tracker.get_prompt_output()
        assert 'Allow tool use?' in result

    def test_empty_prompt_output(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        assert tracker.get_prompt_output() == ''


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_cleanup_deletes_signal_file(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        write_signal(tracker, 'idle')
        tracker.cleanup()
        assert not tracker._signal_file.exists()

    def test_cleanup_no_error_if_missing(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.cleanup()  # no error


# ---------------------------------------------------------------------------
# PTY dead resets flags
# ---------------------------------------------------------------------------

class TestPtyDead:
    def test_pty_dead_clears_flags(self, tmp_path: Path) -> None:
        """When PTY dies, all flags should be reset so a restart
        starts fresh (e.g. trust dialog detection works again)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Set up various flags
        tracker.on_input(b'x')  # _seen_user_input
        tracker.on_send()       # → running
        tracker.on_input(b'\x1b')  # _interrupt_pending
        # PTY dies
        assert tracker.get_state(pty_alive=False) == 'idle'
        # All flags reset
        assert tracker._interrupt_pending is False
        assert tracker._seen_user_input is False
        assert tracker._user_responded is False
        assert tracker._trust_dialog_phase is False
        assert tracker._suppress_stale_interrupt is False

    def test_pty_dead_repeated_calls_no_redundant_reset(
        self, tmp_path: Path,
    ) -> None:
        """Repeated pty_alive=False calls after already idle should
        not keep resetting the screen."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.get_state(pty_alive=False)  # first call: resets
        # Second call: already idle, should be fast no-op
        tracker.get_state(pty_alive=False)
        assert tracker.current_state == 'idle'


# ---------------------------------------------------------------------------
# Escape correction from NEEDS_PERMISSION
# ---------------------------------------------------------------------------

class TestEscapeCorrectionFromPermission:
    def test_escape_in_needs_permission_goes_interrupted(
        self, tmp_path: Path,
    ) -> None:
        """Escape at a permission prompt should detect interrupted
        pattern and transition to interrupted (not just NEEDS_INPUT)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'interrupted'


# ---------------------------------------------------------------------------
# CLIState enum
# ---------------------------------------------------------------------------

class TestClaudeDialogIndicator:
    """Claude's has_dialog_indicator() must detect both standard footer
    patterns and numbered menu prompts."""

    def test_standard_dialog_detected(self) -> None:
        p = ClaudeProvider()
        # Both patterns present
        assert p.has_dialog_indicator('AllowReadEntertoselectEsctocancel')
        # Only one (edit-confirmation style)
        assert p.has_dialog_indicator('MakethiseditEsctocancel')
        assert p.has_dialog_indicator('Entertoselectoption')

    def test_numbered_menu_detected(self) -> None:
        p = ClaudeProvider()
        # ❯ cursor (U+276F)
        assert p.has_dialog_indicator('\u276f1.Yes2.No(esc)')
        # › cursor (U+203A)
        assert p.has_dialog_indicator('\u203a1.Yes2.No(esc)')

    def test_no_dialog_not_detected(self) -> None:
        p = ClaudeProvider()
        assert not p.has_dialog_indicator('Taskcompletehereareresults')
        assert not p.has_dialog_indicator('Processingfiles...')

    def test_strict_rejects_partial_pattern_in_response_text(self) -> None:
        """is_dialog_certain must NOT match response text that happens
        to mention 'Esc to cancel' — only all() of standard patterns
        or a numbered menu cursor qualifies."""
        p = ClaudeProvider()
        # Response text explaining keyboard shortcuts
        response = 'pressEsctocanceltheoperation'
        assert p.has_dialog_indicator(response)  # lenient: matches
        assert not p.is_dialog_certain(response)  # strict: rejects

    def test_strict_detects_numbered_menu(self) -> None:
        p = ClaudeProvider()
        assert p.is_dialog_certain('\u276f1.Yes2.No(esc)')
        assert p.is_dialog_certain('\u203a1.Yes2.No(esc)')

    def test_strict_detects_full_standard_dialog(self) -> None:
        p = ClaudeProvider()
        assert p.is_dialog_certain('AllowReadEntertoselectEsctocancel')
        # Partial (only Esc to cancel) — strict rejects
        assert not p.is_dialog_certain('MakethiseditEsctocancel')


class TestCLIStateEnum:
    def test_cli_state_string_comparison(self) -> None:
        assert CLIState.IDLE == 'idle'
        assert CLIState.RUNNING == 'running'

    def test_waiting_states_membership(self) -> None:
        assert CLIState.NEEDS_PERMISSION in WAITING_STATES
        assert CLIState.NEEDS_INPUT in WAITING_STATES
        assert CLIState.INTERRUPTED in WAITING_STATES
        assert CLIState.IDLE not in WAITING_STATES

    def test_backward_compat_signal_alias(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'What should I do?  Enter to select  Esc to cancel',
        )
        tracker._signal_file.write_text(
            json.dumps({"state": "has_question"}),
        )
        assert tracker.get_state(pty_alive=True) == 'needs_input'


# ---------------------------------------------------------------------------
# Codex-specific
# ---------------------------------------------------------------------------

class TestCodexSpecific:
    def test_codex_enter_triggers_running(self, tmp_path: Path) -> None:
        """Codex Enter in idle triggers running — same as all providers."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'

    def test_codex_silence_timeout(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'output')
        codex_timeout = CodexProvider().silence_timeout
        t[0] = 1.0 + codex_timeout + 1.0
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# /clear scenario (the original bug)
# ---------------------------------------------------------------------------

class TestSlashClear:
    def test_clear_resolves_via_cursor_silence(self, tmp_path: Path) -> None:
        """The original bug: /clear caused persistent running (60s).

        Fix: Enter triggers running, but cursor visible + 2s silence
        resolves to idle quickly.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input

        # User types /clear + Enter → running
        for ch in b'/clear':
            tracker.on_input(bytes([ch]))
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'

        # TUI redraws with cursor visible
        t[0] = 0.5
        feed_with_visible_cursor(tracker, 'Cleared screen')

        # After 5s silence + cursor visible → idle
        t[0] = 6.5
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_clear_vs_real_message(self, tmp_path: Path) -> None:
        """Real messages resolve via Stop hook.
        /clear resolves via cursor+silence check (~5s)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input

        # Real message: Enter → running → Stop hook → idle
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# Stale screen content after state transitions
# ---------------------------------------------------------------------------

class TestStaleScreenContent:
    def test_stale_interrupted_on_screen_no_false_trigger(
        self, tmp_path: Path,
    ) -> None:
        """After resolving an interrupt, stale 'Interrupted' text on
        the pyte screen must not cause false interrupted state when
        user presses Escape later.

        This was a critical bug: pyte screen retained historical content
        across transitions, unlike the old _output_buf which was cleared.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input

        # Phase 1: Real interrupt cycle
        tracker.on_send()  # → running
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'interrupted'

        # Phase 2: Resolve interrupt — send new message
        tracker.on_send()  # → running (clears screen)
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle (clears screen)

        # Phase 3: Accidental Escape in idle
        tracker.on_input(b'\x1b')  # interrupt pending
        assert tracker._interrupt_pending is True

        # New output arrives — screen should NOT contain stale "Interrupted"
        t[0] = 5.0
        feed_screen_text(tracker, 'Normal idle output')
        # Should stay idle — "Interrupted" is not on the fresh screen
        assert tracker.current_state == 'idle'

    def test_screen_reset_on_running_to_idle(self, tmp_path: Path) -> None:
        """Screen is cleared when transitioning running→idle via hook."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(tracker, 'Some running output')
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle

        # Screen should be cleared
        with tracker._screen_lock:
            screen_text = tracker._get_screen_text()
        assert 'Some running output' not in screen_text

    def test_needs_permission_no_false_idle_after_screen_reset(
        self, tmp_path: Path,
    ) -> None:
        """Entering NEEDS_PERMISSION resets the pyte screen.  The Ink TUI
        does not re-render the dialog after the reset (it is already
        displayed from its own perspective).  The indicator-gone fallback
        must NOT fire on the very next poll — an empty fresh screen is not
        evidence that the dialog was dismissed.

        Regression test for the bug observed in the state log:
            19:29:51.559 running→needs_permission (dialog on screen)
            19:29:52.065 NEEDS_PERMISSION→idle (indicator gone + cursor)
            19:29:52.575 signal=needs_permission ignored (stale)
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()       # → running

        # Dialog appears after running for a while; last output was at t=0.
        # Simulate 5.1 seconds of silence with the dialog on screen.
        # Cursor must be visible for the cursor+silence path to fire.
        t[0] = 0.1
        feed_with_visible_cursor(tracker, '❯ 1. Yes\n2. No\nEsc to cancel')
        t[0] = 5.2  # 5.1s of silence since last output

        # get_state detects dialog via cursor+silence → NEEDS_PERMISSION.
        # Screen is reset immediately on entering the state.
        state = tracker.get_state(pty_alive=True)
        assert state == CLIState.NEEDS_PERMISSION

        # 0.5s later (next poll cycle): NO new output has arrived.
        # _last_output_time (0.1) < _waiting_since (5.2) so the
        # indicator-gone check must be suppressed.
        t[0] = 5.7
        state = tracker.get_state(pty_alive=True)
        assert state == CLIState.NEEDS_PERMISSION, (
            'indicator-gone check fired too early on empty post-reset screen'
        )

    def test_needs_permission_self_dismiss_detected_after_new_output(
        self, tmp_path: Path,
    ) -> None:
        """When the CLI genuinely self-dismisses the dialog it sends new
        PTY output to update the screen.  After that output settles, the
        indicator-gone check is allowed to fire and return IDLE.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()

        # Dialog appears with visible cursor (required for cursor+silence path).
        t[0] = 0.1
        feed_with_visible_cursor(tracker, '❯ 1. Yes\n2. No\nEsc to cancel')
        t[0] = 5.2

        # Enter NEEDS_PERMISSION via cursor+silence.
        state = tracker.get_state(pty_alive=True)
        assert state == CLIState.NEEDS_PERMISSION
        waiting_since = tracker._waiting_since
        assert waiting_since is not None

        # CLI self-dismisses: new output arrives AFTER _waiting_since,
        # replacing the dialog with a plain idle prompt (no dialog indicator).
        t[0] = 5.5
        feed_screen_text(tracker, 'Claude is ready')   # no dialog patterns

        # After 5s of silence since the new output, indicator-gone fires.
        t[0] = 10.6  # 5.1s after the post-state output at t=5.5
        state = tracker.get_state(pty_alive=True)
        assert state == CLIState.IDLE, (
            'indicator-gone should fire after new output shows dialog is gone'
        )


# ---------------------------------------------------------------------------
# Pasted text with Enter (bundled bytes)
# ---------------------------------------------------------------------------

class TestPastedEnter:
    def test_pasted_enter_triggers_running(self, tmp_path: Path) -> None:
        """Enter in idle (even pasted) triggers running."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_input(b'hello\r')
        assert tracker.current_state == 'running'


# ---------------------------------------------------------------------------
# Escape doesn't block auto-resume
# ---------------------------------------------------------------------------

class TestEscapeDoesNotBlockAutoResume:
    def test_escape_does_not_set_user_input_since_idle(
        self, tmp_path: Path,
    ) -> None:
        """Escape/Ctrl+C should not set _user_input_since_idle,
        so auto-resume cursor detection is not blocked."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle, clears _user_input_since_idle
        assert tracker._user_input_since_idle is False

        # Escape should NOT set _user_input_since_idle
        tracker.on_input(b'\x1b')
        assert tracker._user_input_since_idle is False
        assert tracker._interrupt_pending is True

        # Auto-resume should still work (detected at poll time)
        t[0] = 5.0
        feed_with_hidden_cursor(tracker, 'Auto processing')
        # Need to clear signal file first (interrupt pending would
        # redirect idle signal to interrupted)
        tracker._signal_file.unlink(missing_ok=True)
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_ctrl_c_does_not_block_auto_resume(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle

        tracker.on_input(b'\x03')  # Ctrl+C
        assert tracker._user_input_since_idle is False

    def test_regular_input_does_block_auto_resume(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle

        tracker.on_input(b'a')  # regular input
        assert tracker._user_input_since_idle is True

        # Auto-resume blocked (even at poll time)
        t[0] = 5.0
        feed_with_hidden_cursor(tracker, 'Some output')
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# Late Notification hook guard
# ---------------------------------------------------------------------------

class TestLateNotificationGuard:
    """The Notification hook can arrive seconds after a permission dialog
    appears — by then the cursor+silence heuristic has already moved
    running→idle and the dialog may have been auto-accepted (bypass
    permissions) or Claude may have finished.

    The guard verifies dialog patterns are visible on the pyte screen
    (or in the saved running snapshot) before accepting an idle→prompt
    signal transition.
    """

    def test_stale_notification_rejected_no_dialog_on_screen(
        self, tmp_path: Path,
    ) -> None:
        """Late Notification with no dialog on screen is rejected."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Claude finishes — cursor+silence fires running→idle
        feed_screen_text(tracker, 'Task complete. Here are the results.')
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

        # Late Notification arrives — no dialog patterns on screen
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Signal file should be deleted to avoid repeated checks
        assert not tracker._signal_file.exists()

    def test_response_mentioning_esc_to_cancel_not_false_positive(
        self, tmp_path: Path,
    ) -> None:
        """Response text explaining keyboard shortcuts (e.g. 'press Esc
        to cancel') must NOT false-trigger dialog detection at the
        running→idle cursor+silence check."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()

        # Response text with "Esc to cancel" in last rows
        feed_with_visible_cursor(
            tracker,
            'Here are the keyboard shortcuts:\n'
            '- Press Esc to cancel the current operation\n'
            '- Press Enter to confirm',
        )
        t[0] = 8.0
        tracker._last_output_time = 2.0
        # cursor visible + 5s silence → should go to IDLE, not
        # needs_permission (would be a false positive)
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_legitimate_notification_accepted_dialog_in_snapshot(
        self, tmp_path: Path,
    ) -> None:
        """Notification accepted when dialog patterns are in the saved
        running snapshot (screen was reset but snapshot has the dialog)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Permission dialog appears — cursor+silence fires running→idle
        # The snapshot saved on running→idle contains the dialog
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Snapshot should contain the dialog text
        assert tracker._last_running_snapshot

        # Late Notification arrives — dialog in snapshot
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_legitimate_notification_accepted_dialog_on_live_screen(
        self, tmp_path: Path,
    ) -> None:
        """Notification accepted when dialog patterns are on the live
        pyte screen (new output arrived after screen reset)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # cursor+silence fires with non-dialog content
        feed_screen_text(tracker, 'Processing...')
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

        # New output renders the dialog on the live screen
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )

        # Late Notification arrives — dialog on live screen
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_legitimate_notification_accepted_partial_dialog_patterns(
        self, tmp_path: Path,
    ) -> None:
        """Notification accepted when only SOME dialog patterns are
        present (e.g., 'Esc to cancel' without 'Enter to select').
        Claude Code's edit-confirmation dialogs show numbered options
        with 'Esc to cancel' but no 'Enter to select'."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Permission dialog with only "Esc to cancel"
        feed_screen_text(
            tracker,
            'Do you want to make this edit?\n'
            '1. Yes\n'
            '2. Yes, allow all\n'
            '3. No\n'
            'Esc to cancel',
        )
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        assert tracker._last_running_snapshot

        # Late Notification arrives — partial dialog patterns in snapshot
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_numbered_menu_permission_accepted_from_running(
        self, tmp_path: Path,
    ) -> None:
        """Notification hook accepted when a numbered menu permission prompt
        (e.g., 'Network request outside of sandbox') is visible.
        These prompts use ❯ cursor indicator + numbered options instead
        of the standard 'Enter to select / Esc to cancel' footer."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Numbered menu permission prompt appears while still RUNNING
        feed_screen_text(
            tracker,
            'Network request outside of sandbox\n'
            '    Host: mcp-proxy.anthropic.com\n'
            'Do you want to allow this connection?\n'
            '\u276f 1. Yes\n'
            '  2. Yes, and don\'t ask again\n'
            '  3. No, and tell Claude what to do differently (esc)',
        )

        # Hook signal arrives while still running (cursor may be hidden)
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_numbered_menu_permission_accepted_from_snapshot(
        self, tmp_path: Path,
    ) -> None:
        """Numbered menu dialog in saved running snapshot is accepted
        after cursor+silence moves to idle."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Numbered menu permission prompt
        feed_screen_text(
            tracker,
            'Do you want to allow this connection?\n'
            '\u276f 1. Yes\n'
            '  2. Yes, and don\'t ask again\n'
            '  3. No (esc)',
        )
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        assert tracker._last_running_snapshot

        # Late Notification arrives — numbered menu in snapshot
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_numbered_menu_detected_at_running_to_idle(
        self, tmp_path: Path,
    ) -> None:
        """Running→idle cursor+silence check detects numbered menu
        prompts and goes directly to needs_permission."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Cursor visible + numbered menu prompt on last 5 rows
        feed_with_visible_cursor(
            tracker,
            'Do you want to allow?\n'
            '\u203a 1. Yes\n'
            '  2. No (esc)',
        )
        t[0] = 8.0
        tracker._last_output_time = 2.0
        # No signal yet — proactive detection via cursor+silence (>5s)
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_stale_snapshot_cleared_on_waiting_to_idle(
        self, tmp_path: Path,
    ) -> None:
        """After answering a dialog (waiting→idle), the running snapshot
        must be cleared so a late Notification hook doesn't false-match
        the old dialog content in the snapshot."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()

        # Numbered menu detected via hook → needs_permission
        feed_screen_text(
            tracker,
            '\u276f 1. Yes\n  2. No (esc)',
        )
        write_signal(tracker, 'needs_permission')
        t[0] = 1.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

        # User answers → user_responded
        tracker.on_input(b'1\r')

        # Stop hook → idle (snapshot must not retain stale dialog content)
        write_signal(tracker, 'idle')
        t[0] = 5.0
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Snapshot may be empty list or list of blank rows — either way
        # no dialog indicator should be present in it.
        snapshot_text = ''.join(tracker._last_running_snapshot).replace(' ', '')
        assert not tracker._provider.has_dialog_indicator(snapshot_text)

        # Late stale Notification — must be rejected
        write_signal(tracker, 'needs_permission')
        t[0] = 8.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_stale_needs_input_rejected_no_dialog_on_screen(
        self, tmp_path: Path,
    ) -> None:
        """Late Notification for elicitation_dialog (needs_input) with
        no dialog on screen is rejected — same guard as needs_permission."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Claude finishes — cursor+silence fires running→idle
        feed_screen_text(tracker, 'Task complete.')
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

        # Late elicitation Notification arrives — no dialog on screen
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'idle'
        assert not tracker._signal_file.exists()

    def test_legitimate_needs_input_accepted_with_dialog(
        self, tmp_path: Path,
    ) -> None:
        """Elicitation dialog with patterns on screen is accepted."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        feed_screen_text(
            tracker,
            'What should I name this?\n'
            '1. Type something\n'
            'Enter to select  Esc to cancel',
        )
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        assert tracker._last_running_snapshot

        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'needs_input'

    def test_stale_notification_rejected_after_enter_from_permission(
        self, tmp_path: Path,
    ) -> None:
        """After Enter answers a permission dialog (→ RUNNING via Fix 2),
        a stale Notification hook signal is rejected by the Late Notification
        Guard.

        The Enter transition clears _last_running_snapshot and resets the
        pyte screen, so the guard finds no dialog patterns and blocks the
        stale needs_permission signal.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()  # → running

        # Permission dialog appears via hook signal
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

        # User presses Enter → immediately RUNNING, screen+snapshot cleared
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'
        assert tracker._last_running_snapshot == []

        # Stale Notification hook fires for the dialog that was already answered
        write_signal(tracker, 'needs_permission')

        # Guard rejects: live screen is empty (reset) and snapshot is empty
        assert tracker.get_state(pty_alive=True) == 'running'
        assert not tracker._signal_file.exists()


# ---------------------------------------------------------------------------
# Mid-session proactive dialog detection (AskUserQuestion / "Proceed?")
# ---------------------------------------------------------------------------

class TestProactiveIdleDialogDetection:
    """Some Claude tools (notably AskUserQuestion / "Proceed?") do NOT fire
    PreToolUse — only Stop, so the state tracker transitions running→idle
    while the dialog is still visible.  Without proactive detection,
    auto-approve never fires.  We use the strict ``is_dialog_certain``
    check (all dialog_patterns must appear) so single-pattern prose
    doesn't false-trigger.
    """

    def test_idle_to_needs_permission_when_full_dialog_on_screen(
        self, tmp_path: Path,
    ) -> None:
        # User has typed at least once (the gating condition that the
        # original startup-only proactive check excluded us from).
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)
        assert tracker._state == 'idle'
        assert tracker._seen_user_input is True

        # AskUserQuestion-shaped dialog: contains BOTH "Enter to select"
        # AND "Esc to cancel" — the strict patterns Claude uses.
        feed_screen_text(
            tracker,
            'Do you want to proceed?\n'
            '> 1. Yes\n'
            '  2. No\n'
            'Enter to select · Esc to cancel',
        )
        assert tracker._state == 'needs_permission'

    def test_idle_stays_when_only_one_pattern_present(
        self, tmp_path: Path,
    ) -> None:
        # Conversational text mentioning ONE shortcut (not a real dialog)
        # must NOT trigger the proactive transition.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)

        feed_screen_text(
            tracker,
            'The shortcut is Esc to cancel — useful for aborting.',
        )
        assert tracker._state == 'idle'

    def test_proactive_transition_survives_idle_heartbeat(
        self, tmp_path: Path,
    ) -> None:
        # Regression: an earlier version called _reset_screen() right
        # after the proactive transition, wiping the dialog from the
        # live pyte buffer.  Then a Claude TUI heartbeat (cursor blink
        # / partial repaint) would update _last_output_time without
        # re-rendering the full dialog, so the waiting→idle self-
        # dismissal check at get_state would see "no dialog patterns"
        # on the empty screen and revert to idle — even though the
        # dialog is still on the user's actual terminal.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)

        # Full AskUserQuestion-style dialog renders.
        feed_screen_text(
            tracker,
            '□ Cats vs dogs\n'
            'Are cats better than dogs?\n'
            '> 1. Yes\n'
            '  2. No\n'
            'Enter to select · Esc to cancel',
        )
        assert tracker._state == 'needs_permission'

        # Idle TUI heartbeat: just toggle cursor visibility.  This is
        # output (advances _last_output_time) but does NOT redraw the
        # dialog cells.  Without the fix, the dialog would be lost
        # because _reset_screen() had wiped it.
        tracker.on_output(b'\x1b[?25h')

        # 5+ seconds later, get_state runs the self-dismissal check.
        # Live screen still has the dialog → has_dialog_indicator
        # returns True → state must remain needs_permission.
        t[0] = 10.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_idle_stays_when_patterns_quoted_above_tail(
        self, tmp_path: Path,
    ) -> None:
        # Both dialog patterns appear above the footer region, but the
        # last 5 non-blank rows are plain prose (no patterns).  The
        # tail-only check must reject this — patterns must appear in
        # the dialog-footer region (last 5 lines) to count.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)

        feed_screen_text(
            tracker,
            'When Claude shows a dialog, you see\n'
            '"Enter to select · Esc to cancel" in the footer.\n'
            'It is the standard pattern across all Ink TUIs.\n'
            'Line four of the response.\n'
            'Line five of the response.\n'
            'Line six of the response.\n'
            'Line seven, well past the tail window.\n'
            'Line eight is also clean prose.\n'
            'End of explanation here.\n',
        )
        assert tracker._state == 'idle'


# ---------------------------------------------------------------------------
# Claude conversation-compaction detection
# ---------------------------------------------------------------------------

class TestClaudeCompactingIndicator:
    """Claude Code runs /compact and auto-compact without firing any
    hook for the compaction itself.  Between-turns auto-compact starts
    right after a Stop hook wrote 'idle' — without running-indicator
    detection the session would read as idle for the full duration."""

    def test_idle_transitions_to_running_when_compacting_appears(
        self, tmp_path: Path,
    ) -> None:
        """Auto-compact fires right after Stop → indicator moves idle→running."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

        t[0] = 1.0
        feed_screen_text(tracker, '* Compacting conversation...')
        assert tracker.current_state == 'running'

    def test_idle_to_running_needs_seen_user_input(
        self, tmp_path: Path,
    ) -> None:
        """Before any user input, indicator-based transition is suppressed
        (matches the general gating for post-startup checks)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        feed_screen_text(tracker, '* Compacting conversation...')
        assert tracker.current_state == 'idle'

    def test_running_idle_signal_ignored_while_compacting(
        self, tmp_path: Path,
    ) -> None:
        """Stop hook writing idle during an on-screen compaction must not
        flip the state to idle."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(tracker, '* Compacting conversation... (12s)')
        assert tracker.current_state == 'running'

        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'running'
        # The stale signal should be cleared so it doesn't keep re-firing.
        assert not tracker._signal_file.exists()

    def test_cursor_silence_fallback_skipped_while_compacting(
        self, tmp_path: Path,
    ) -> None:
        """The running→idle cursor+silence fallback must not fire while
        the compaction indicator is on screen."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # Cursor visible + indicator on screen.
        feed_with_visible_cursor(
            tracker, '* Compacting conversation... (3s)',
        )
        # Advance past the 5s silence window without any new output.
        t[0] = 10.0
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_silence_safety_timeout_skipped_while_compacting(
        self, tmp_path: Path,
    ) -> None:
        """Safety silence timeout must not force-idle while compacting."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(tracker, '* Compacting conversation...')
        t[0] = SAFETY_SILENCE_TIMEOUT + 10.0
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_compaction_end_allows_idle_transition(
        self, tmp_path: Path,
    ) -> None:
        """Once the indicator is gone (compaction finished), a subsequent
        idle signal is honoured normally."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(tracker, '* Compacting conversation...')
        assert tracker.current_state == 'running'

        # Compaction finishes — indicator replaced by the normal prompt.
        feed_screen_text(tracker, '> ')
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_other_providers_unaffected(
        self, tmp_path: Path,
    ) -> None:
        """Providers without running indicators keep the default behaviour."""
        t = [0.0]
        tracker = make_tracker(
            tmp_path, t, provider=CodexProvider(),
        )
        assert tracker._provider.running_indicator_patterns == []
        tracker.on_input(b'x')
        feed_screen_text(tracker, 'Compacting conversation...')
        # No idle→running transition — the pattern is provider-specific.
        assert tracker.current_state == 'idle'
