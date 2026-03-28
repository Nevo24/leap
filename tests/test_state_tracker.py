"""Tests for CLIStateTracker event-driven state machine."""

import json
from pathlib import Path
from typing import List

import pytest

from leap.server.state_tracker import CLIStateTracker as ClaudeStateTracker
from leap.cli_providers.codex import CodexProvider
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
    def test_enter_in_idle_does_not_trigger_running(self, tmp_path: Path) -> None:
        """Enter at idle prompt does NOT trigger running.

        The CLI may handle it as a slash command (/clear, /help) that
        doesn't trigger the Stop hook.  Only on_send() triggers running.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\r')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_on_send_still_triggers_running(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        assert tracker.get_state(pty_alive=True) == 'running'


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
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_signal_file_needs_input(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
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
        from leap.cli_providers.codex import CodexProvider
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
        from leap.cli_providers.codex import CodexProvider
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
        """Running with cursor visible + output silence > 0.5s → idle.

        Handles /clear sent from queue (on_send → running, but Stop
        hook doesn't fire).  The cursor becoming visible + output
        settling signals the CLI returned to idle.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running (e.g., /clear from queue)
        t[0] = 1.0
        # TUI redraws with cursor visible at the end
        feed_with_visible_cursor(tracker, 'Cleared screen')
        # Wait > 0.5s (one poll cycle)
        t[0] = 2.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_cursor_visible_during_streaming_stays_running(
        self, tmp_path: Path,
    ) -> None:
        """During streaming, output arrives constantly.  Even if cursor
        is visible between frames, silence < 0.5s keeps state running."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(tracker, 'Streaming token 1')
        # Only 0.1s since last output — not enough silence
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
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)
        t[0] = 1.0
        tracker.on_output(b'prompt text')
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

        Enter no longer triggers running.  Ctrl+C sets interrupt_pending.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'hello\x03\r')
        assert tracker._interrupt_pending is True
        assert tracker.current_state == 'idle'

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
        t = [0.0]
        tracker = make_tracker(tmp_path, t, auto_send_mode='always')
        assert tracker.is_ready_for_state('idle') is True
        assert tracker.is_ready_for_state('running') is False
        assert tracker.is_ready_for_state('needs_permission') is True
        assert tracker.is_ready_for_state('needs_input') is True
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

class TestCLIStateEnum:
    def test_cli_state_string_comparison(self) -> None:
        from leap.cli_providers.states import CLIState
        assert CLIState.IDLE == 'idle'
        assert CLIState.RUNNING == 'running'

    def test_waiting_states_membership(self) -> None:
        from leap.cli_providers.states import CLIState, WAITING_STATES
        assert CLIState.NEEDS_PERMISSION in WAITING_STATES
        assert CLIState.NEEDS_INPUT in WAITING_STATES
        assert CLIState.INTERRUPTED in WAITING_STATES
        assert CLIState.IDLE not in WAITING_STATES

    def test_backward_compat_signal_alias(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker._signal_file.write_text(
            json.dumps({"state": "has_question"}),
        )
        assert tracker.get_state(pty_alive=True) == 'needs_input'


# ---------------------------------------------------------------------------
# Codex-specific
# ---------------------------------------------------------------------------

class TestCodexSpecific:
    def test_codex_enter_does_not_trigger_running(self, tmp_path: Path) -> None:
        """Codex Enter in idle stays idle — same as all providers."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_input(b'\r')
        assert tracker.get_state(pty_alive=True) == 'idle'

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
    def test_clear_stays_idle(self, tmp_path: Path) -> None:
        """The original bug: /clear typed in idle caused persistent running.

        Fix: Enter in idle does NOT trigger running.  /clear is a CLI
        slash command — the Stop hook may never fire for it.  The state
        stays idle throughout.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input

        # User types /clear + Enter
        for ch in b'/clear':
            tracker.on_input(bytes([ch]))
        tracker.on_input(b'\r')
        # State stays idle — Enter doesn't trigger running
        assert tracker.current_state == 'idle'

        # TUI redraws with cursor visible — still idle
        t[0] = 0.5
        feed_with_visible_cursor(tracker, 'Cleared screen')
        assert tracker.current_state == 'idle'

        # Even at poll time — cursor visible → no auto-resume
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_clear_vs_real_message(self, tmp_path: Path) -> None:
        """A real message typed at server terminal triggers running
        via auto-resume (cursor hidden during processing), while
        /clear does not (cursor visible after brief redraw)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input

        # Simulate: user typed message, CLI starts processing
        # (cursor hidden = streaming output)
        tracker.on_send()  # explicit client send
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle
        # _user_input_since_idle is False after transition

        # CLI auto-starts (background) → cursor hidden
        feed_with_hidden_cursor(tracker, 'Processing your request...')
        # At next poll: cursor hidden → running
        assert tracker.get_state(pty_alive=True) == 'running'


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


# ---------------------------------------------------------------------------
# Pasted text with Enter (bundled bytes)
# ---------------------------------------------------------------------------

class TestPastedEnter:
    def test_pasted_enter_does_not_trigger_running(self, tmp_path: Path) -> None:
        """Enter in idle (even pasted) does NOT trigger running.

        The CLI handles the input — hooks signal when processing starts/ends.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_input(b'hello\r')
        assert tracker.current_state == 'idle'


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
