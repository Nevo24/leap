"""Tests for ClaudeStateTracker state machine logic."""

import json
from pathlib import Path
from typing import List

import pytest

from leap.server.state_tracker import ClaudeStateTracker
from leap.cli_providers.codex import CodexProvider
from leap.utils.constants import OUTPUT_SILENCE_TIMEOUT, WAITING_STATE_TIMEOUT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tracker(
    tmp_path: Path,
    t: List[float],
    auto_send_mode: str = 'pause',
) -> ClaudeStateTracker:
    """Create a tracker with fake clock and a signal file in *tmp_path*."""
    signal_file = tmp_path / "test.signal"
    return ClaudeStateTracker(
        signal_file=signal_file,
        auto_send_mode=auto_send_mode,
        clock=lambda: t[0],
    )


def write_signal(tracker: ClaudeStateTracker, state: str) -> None:
    """Write a JSON signal file that the tracker will read."""
    tracker._signal_file.write_text(json.dumps({"state": state}))


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


# ---------------------------------------------------------------------------
# Signal file transitions (running → idle/needs_permission/needs_input)
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
# Silence timeout (running → idle)
# ---------------------------------------------------------------------------

class TestSilenceTimeout:
    def test_silence_timeout_triggers_idle(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # Produce some output so _last_output_time is set
        t[0] = 1.0
        tracker.on_output(b'some output')
        # Advance past the silence timeout
        t[0] = 1.0 + OUTPUT_SILENCE_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_silence_timeout_not_triggered_before_deadline(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'some output')
        # Just under the timeout
        t[0] = 1.0 + OUTPUT_SILENCE_TIMEOUT - 0.1
        assert tracker.get_state(pty_alive=True) == 'running'


# ---------------------------------------------------------------------------
# Idle → running (output accumulation)
# ---------------------------------------------------------------------------

class TestOutputAccumulation:
    def _setup_idle_with_input(self, tmp_path: Path, t: List[float]) -> ClaudeStateTracker:
        """Return a tracker in idle state that has seen user input."""
        tracker = make_tracker(tmp_path, t)
        # Mark user input at t=0
        tracker.on_input(b'x')
        return tracker

    def test_output_accumulation_triggers_running(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = self._setup_idle_with_input(tmp_path, t)
        # Advance past input cooldown (0.5s)
        t[0] = 1.0
        # Send enough printable output to cross the 200-byte threshold
        tracker.on_output(b'A' * 201)
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_output_accumulation_needs_user_input_first(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # No on_input() call — _seen_user_input is False
        t[0] = 1.0
        tracker.on_output(b'A' * 300)
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_output_accumulation_resets_on_input(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = self._setup_idle_with_input(tmp_path, t)
        t[0] = 1.0
        # Accumulate some but not enough
        tracker.on_output(b'A' * 150)
        assert tracker.get_state(pty_alive=True) == 'idle'
        # User types again — resets accumulator
        t[0] = 2.0
        tracker.on_input(b'y')
        t[0] = 3.0
        # Another 150 bytes, but accumulator was reset so still under 200
        tracker.on_output(b'A' * 150)
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_output_accumulation_ignores_ansi(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = self._setup_idle_with_input(tmp_path, t)
        t[0] = 1.0
        # Send only ANSI escape sequences — no printable content
        tracker.on_output(b'\x1b[2J\x1b[H' * 100)
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_output_accumulation_resets_on_gap(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = self._setup_idle_with_input(tmp_path, t)
        t[0] = 1.0
        tracker.on_output(b'A' * 150)
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Gap of >2s resets accumulator
        t[0] = 4.0
        tracker.on_output(b'A' * 150)
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_output_within_input_cooldown_ignored(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = self._setup_idle_with_input(tmp_path, t)
        # Output within 0.5s of input (cooldown period)
        t[0] = 0.3
        tracker.on_output(b'A' * 300)
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_signal_idle_blocks_false_running_retrigger(self, tmp_path: Path) -> None:
        """After signal file transitions to idle, prompt rendering
        should not falsely re-trigger running (input predates idle)."""
        t = [0.0]
        tracker = self._setup_idle_with_input(tmp_path, t)
        # Output accumulation → running
        t[0] = 1.0
        tracker.on_output(b'A' * 201)
        assert tracker.get_state(pty_alive=True) == 'running'
        # Signal file says idle (Claude finished)
        t[0] = 5.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Prompt rendering arrives — should NOT re-trigger running
        # because user input (t=0) predates idle transition (t=5)
        t[0] = 6.0
        tracker.on_output(b'B' * 300)
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_silence_timeout_blocks_false_running_retrigger(self, tmp_path: Path) -> None:
        """After silence timeout transitions to idle, prompt rendering
        should not falsely re-trigger running."""
        t = [0.0]
        tracker = self._setup_idle_with_input(tmp_path, t)
        t[0] = 1.0
        tracker.on_output(b'A' * 201)
        assert tracker.get_state(pty_alive=True) == 'running'
        # Silence timeout → idle
        t[0] = 1.0 + OUTPUT_SILENCE_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Prompt rendering — should NOT re-trigger running
        t[0] = 1.0 + OUTPUT_SILENCE_TIMEOUT + 2.0
        tracker.on_output(b'B' * 300)
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_new_input_after_idle_allows_accumulation(self, tmp_path: Path) -> None:
        """User typing AFTER an idle transition should allow output
        accumulation to detect running again."""
        t = [0.0]
        tracker = self._setup_idle_with_input(tmp_path, t)
        t[0] = 1.0
        tracker.on_output(b'A' * 201)
        assert tracker.get_state(pty_alive=True) == 'running'
        # Signal idle
        t[0] = 5.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        # User types again (AFTER idle transition)
        t[0] = 7.0
        tracker.on_input(b'x')
        # New output → should trigger running
        t[0] = 8.0
        tracker.on_output(b'C' * 201)
        assert tracker.get_state(pty_alive=True) == 'running'


# ---------------------------------------------------------------------------
# Running → needs_input (Interrupted detection)
# ---------------------------------------------------------------------------

class TestInterruptedDetection:
    def test_interrupted_detected_in_running(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x1b')  # User presses Escape
        t[0] = 1.0
        tracker.on_output(b'some text Interrupted more text')
        assert tracker.current_state == 'interrupted'

    def test_interrupted_split_across_chunks(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x1b')  # User presses Escape
        t[0] = 1.0
        tracker.on_output(b'some text Inter')
        assert tracker.current_state == 'running'
        t[0] = 1.1
        tracker.on_output(b'rupted more text')
        assert tracker.current_state == 'interrupted'

    def test_output_buffer_capped_at_8192(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Fill buffer with >8192 bytes of non-matching data
        tracker.on_output(b'X' * 10000)
        assert len(tracker._output_buf) <= 8192

    def test_interrupted_in_large_chunk_after_buffer_trim(
        self, tmp_path: Path,
    ) -> None:
        """BUG FIX: 'Interrupted' near the start of a large TUI redraw
        chunk (>512 bytes) was lost after buffer trim.  The fix checks
        the raw chunk before trimming."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x1b')  # User presses Escape
        t[0] = 1.0
        # Fill buffer with prior Claude response output
        tracker.on_output(b'A' * 400)
        assert tracker.current_state == 'running'
        # Large TUI redraw: "Interrupted" near start, >512 bytes after
        chunk = b'\x1b[2J\x1b[H'
        chunk += b'Interrupted \xc2\xb7 What should Claude do instead?\r\n'
        chunk += b'B' * 600  # status bar / prompt rendering
        t[0] = 1.1
        tracker.on_output(chunk)
        assert tracker.current_state == 'interrupted'


# ---------------------------------------------------------------------------
# False positive: "Interrupted" in Claude's response text (no Escape pressed)
# ---------------------------------------------------------------------------

class TestInterruptedWordInOutputNotFalsePositive:
    """The word 'Interrupted' in Claude's normal response text must NOT
    trigger interrupted state when the user didn't press Escape."""

    def test_interrupted_word_after_send_no_escape(self, tmp_path: Path) -> None:
        """on_send() followed by output containing 'Interrupted' — no Escape
        was pressed, so state should remain running."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'some text Interrupted more text')
        assert tracker.current_state == 'running'

    def test_interrupted_word_after_normal_typing(self, tmp_path: Path) -> None:
        """User types a normal message, Claude responds with 'Interrupted'
        in its output — should stay running."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'hello')
        t[0] = 1.0
        tracker.on_output(b'A' * 201)
        assert tracker.get_state(pty_alive=True) == 'running'
        t[0] = 2.0
        tracker.on_output(b'Request interrupted by user')
        assert tracker.current_state == 'running'

    def test_interrupted_word_with_escape_triggers(self, tmp_path: Path) -> None:
        """Same scenario but with Escape — should trigger interrupted."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x1b')
        t[0] = 1.0
        tracker.on_output(b'some text Interrupted more text')
        assert tracker.current_state == 'interrupted'

    def test_interrupted_with_ctrl_c_triggers(self, tmp_path: Path) -> None:
        """Ctrl+C (0x03) should also trigger interrupted detection."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x03')
        t[0] = 1.0
        tracker.on_output(
            b'Interrupted \xc2\xb7 What should Claude do instead?',
        )
        assert tracker.current_state == 'interrupted'

    def test_ctrl_c_without_interrupted_output_stays_running(
        self, tmp_path: Path,
    ) -> None:
        """Ctrl+C alone (without 'Interrupted' in output) stays running."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x03')
        t[0] = 1.0
        tracker.on_output(b'normal output continues')
        assert tracker.current_state == 'running'


# ---------------------------------------------------------------------------
# False positive: "Interrupted" inside ANSI escape sequences
# ---------------------------------------------------------------------------

class TestInterruptedFalsePositive:
    """Ensure 'Interrupted' inside ANSI escape sequences does not trigger
    a false interrupted state."""

    def test_interrupted_in_hyperlink_osc_ignored(self, tmp_path: Path) -> None:
        """Hyperlink OSC containing 'Interrupted' in URL must not trigger."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # OSC 8 hyperlink: \x1b]8;;URL\x07 text \x1b]8;;\x07
        chunk = (
            b'\x1b]8;;https://example.com/Interrupted\x07'
            b'click here'
            b'\x1b]8;;\x07'
            b' normal output continues'
        )
        tracker.on_output(chunk)
        assert tracker.current_state == 'running'

    def test_interrupted_in_osc_buffer_ignored(self, tmp_path: Path) -> None:
        """'Interrupted' split across buffer via OSC must not trigger."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'\x1b]8;;https://example.com/Inter')
        t[0] = 1.1
        tracker.on_output(b'rupted\x07link text\x1b]8;;\x07')
        assert tracker.current_state == 'running'

    def test_real_interrupted_still_detected(self, tmp_path: Path) -> None:
        """Visible 'Interrupted' text (not in ANSI) must still trigger."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x1b')  # User presses Escape
        t[0] = 1.0
        chunk = b'\x1b[2J\x1b[HInterrupted \xc2\xb7 What next?\r\n'
        tracker.on_output(chunk)
        assert tracker.current_state == 'interrupted'

    def test_escape_race_interrupted_in_osc_ignored(
        self, tmp_path: Path,
    ) -> None:
        """Escape race: 'Interrupted' in ANSI must not trigger in idle."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Get to idle with recent input (Escape race window)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'idle')
        tracker.get_state(True)
        t[0] = 1.5
        tracker.on_input(b'\x1b')  # Escape key
        t[0] = 2.0
        # Output with 'Interrupted' only inside OSC
        tracker.on_output(
            b'\x1b]8;;https://example.com/Interrupted\x07ok\x1b]8;;\x07',
        )
        assert tracker.current_state == 'idle'


# ---------------------------------------------------------------------------
# Escape race (idle state Interrupted detection)
# ---------------------------------------------------------------------------

class TestEscapeRace:
    def test_escape_race_interrupted_in_idle(self, tmp_path: Path) -> None:
        """Stop hook writes idle, then PTY outputs 'Interrupted' → interrupted."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Simulate: server was running, hook wrote idle, user pressed Escape
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        # User presses Escape (single byte)
        t[0] = 1.5
        tracker.on_input(b'\x1b')
        # PTY outputs "Interrupted" within 3s of input
        t[0] = 2.0
        tracker.on_output(b'Interrupted')
        assert tracker.current_state == 'interrupted'

    def test_escape_race_after_signal_idle_transition(self, tmp_path: Path) -> None:
        """Signal idle is blocked when user pressed Escape during running,
        keeping state as running until PTY outputs 'Interrupted'."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # User types directly → output accumulation → running
        tracker.on_input(b'x')
        t[0] = 1.0
        tracker.on_output(b'A' * 201)
        assert tracker.get_state(pty_alive=True) == 'running'
        # User presses Escape
        t[0] = 5.0
        tracker.on_input(b'\x1b')
        # Stop hook fires → signal file says idle → blocked (recent input)
        t[0] = 5.1
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'running'
        # PTY outputs "Interrupted" shortly after → detected from running
        t[0] = 5.2
        tracker.on_output(b'Interrupted')
        assert tracker.current_state == 'interrupted'

    def test_escape_race_only_within_10s_of_input(self, tmp_path: Path) -> None:
        """'Interrupted' in idle state ignored if >10s after input."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)
        # User pressed Escape
        t[0] = 1.5
        tracker.on_input(b'\x1b')
        # PTY outputs "Interrupted" after >10s
        t[0] = 12.0
        tracker.on_output(b'Interrupted')
        # Should stay idle — too late for the race window
        assert tracker.current_state == 'idle'

    def test_escape_race_ignores_normal_typing_redraw(self, tmp_path: Path) -> None:
        """Normal typing that triggers TUI redraw with 'Interrupted' in AI text.

        When user types a normal character while idle, the Ink TUI redraws
        the visible screen.  If the AI's previous response discussed interrupts,
        the redraw contains 'Interrupted' — but the user didn't press Escape,
        so the escape-race detector must NOT trigger.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)
        # User types a normal character (NOT Escape)
        t[0] = 1.5
        tracker.on_input(b'y')
        # PTY redraws screen — output contains 'Interrupted' from AI text
        t[0] = 1.52
        tracker.on_output(b'Interrupted')
        # Should stay idle — no Escape was pressed
        assert tracker.current_state == 'idle'

    def test_escape_race_still_works_after_escape(self, tmp_path: Path) -> None:
        """Escape race still triggers correctly when user presses Escape."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)
        # User presses Escape
        t[0] = 1.5
        tracker.on_input(b'\x1b')
        # PTY outputs "Interrupted" (even as part of a large redraw)
        t[0] = 1.52
        large_redraw = b'A' * 800 + b'Interrupted' + b'B' * 400
        tracker.on_output(large_redraw)
        # Should detect interrupt — Escape WAS pressed
        assert tracker.current_state == 'interrupted'


# ---------------------------------------------------------------------------
# Stop hook race (needs_input protected from idle signal)
# ---------------------------------------------------------------------------

class TestStopHookRace:
    def test_needs_input_protected_from_idle_signal_within_5s(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'needs_input'
        # Immediately write idle signal — within 5s grace
        t[0] = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'needs_input'

    def test_needs_input_yields_to_idle_signal_after_5s(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'needs_input'
        # After 5s grace, idle signal is honored
        t[0] = 7.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_interrupted_protected_from_idle_signal_within_5s(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x1b')  # User presses Escape
        t[0] = 1.0
        tracker.on_output(b'Interrupted')
        assert tracker.current_state == 'interrupted'
        # Immediately write idle signal — within 5s grace
        t[0] = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_interrupted_yields_to_idle_signal_after_5s(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x1b')  # User presses Escape
        t[0] = 1.0
        tracker.on_output(b'Interrupted')
        assert tracker.current_state == 'interrupted'
        # After 5s grace, idle signal is honored
        t[0] = 7.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# Resume detection (needs_input/needs_permission → running)
# ---------------------------------------------------------------------------

class TestResumeDetection:
    def test_resume_after_permission_on_printable_output(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # User types 'y' to approve (AFTER entering waiting state)
        t[0] = 3.0
        tracker.on_input(b'y')
        # After 2s grace, printable output → running
        t[0] = 4.0
        tracker.on_output(b'Processing...')
        assert tracker.current_state == 'running'

    def test_resume_ignored_during_grace_period(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # User types, but output within 2s grace period → stays
        t[0] = 1.5
        tracker.on_input(b'y')
        t[0] = 2.0
        tracker.on_output(b'Prompt text rendering...')
        assert tracker.current_state == 'needs_permission'

    def test_resume_ignored_for_ansi_only_output(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'needs_input'
        # User types, but only ANSI output → stays needs_input
        t[0] = 3.0
        tracker.on_input(b'y')
        t[0] = 4.0
        tracker.on_output(b'\x1b[2J\x1b[H\r')
        assert tracker.current_state == 'needs_input'

    def test_resume_blocked_without_user_input(self, tmp_path: Path) -> None:
        """TUI status bar rendering after needs_input should NOT
        trigger resume (no user input since entering waiting state)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'needs_input'
        # After grace, printable output but NO user input → stays
        t[0] = 4.0
        tracker.on_output(b'Nevo.Mashiach 10% Opus 4.6 default')
        assert tracker.current_state == 'needs_input'


# ---------------------------------------------------------------------------
# on_input filtering
# ---------------------------------------------------------------------------

class TestOnInputFiltering:
    def test_on_input_ignores_escape_sequences(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Multi-byte ESC sequences (terminal focus events) are ignored
        tracker.on_input(b'\x1b[I')  # focus in
        assert not tracker._seen_user_input

    def test_on_input_accepts_single_escape(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Single ESC byte = Escape key press
        tracker.on_input(b'\x1b')
        assert tracker._seen_user_input

    def test_on_input_accepts_regular_keys(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'a')
        assert tracker._seen_user_input


# ---------------------------------------------------------------------------
# is_ready
# ---------------------------------------------------------------------------

class TestIsReady:
    def test_is_ready_pause_mode(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t, auto_send_mode='pause')
        # Idle → ready
        assert tracker.is_ready(pty_alive=True)
        # Running → not ready
        tracker.on_send()
        assert not tracker.is_ready(pty_alive=True)
        # needs_permission → not ready in pause mode
        t[0] = 1.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        assert not tracker.is_ready(pty_alive=True)

    def test_is_ready_always_mode(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t, auto_send_mode='always')
        # Idle → ready
        assert tracker.is_ready(pty_alive=True)
        # Running → not ready
        tracker.on_send()
        assert not tracker.is_ready(pty_alive=True)
        # needs_permission → ready in always mode
        t[0] = 1.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        assert tracker.is_ready(pty_alive=True)
        # needs_input → ready in always mode
        t[0] = 7.0  # past 5s grace
        write_signal(tracker, 'needs_input')
        # Read state to transition
        tracker.get_state(pty_alive=True)
        assert tracker.is_ready(pty_alive=True)


# ---------------------------------------------------------------------------
# Startup trust dialog detection
# ---------------------------------------------------------------------------

class TestTrustDialog:
    def test_trust_dialog_plain_text(self, tmp_path: Path) -> None:
        """Workspace trust dialog with literal spaces → needs_permission."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        assert tracker.get_state(pty_alive=True) == 'idle'
        t[0] = 1.0
        tracker.on_output(
            b'Is this a project you created or one you trust?\r\n'
            b'\xe2\x9d\xaf 1. Yes, I trust this folder\r\n'
            b'  2. No, exit\r\n'
        )
        assert tracker.current_state == 'needs_permission'

    def test_trust_dialog_old_text(self, tmp_path: Path) -> None:
        """Old trust dialog wording still detected."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        t[0] = 1.0
        tracker.on_output(
            b'Do you trust the contents of this directory?\r\n'
            b'\xe2\x9d\xaf 1. Yes, continue\r\n'
            b'  2. No, quit\r\n'
        )
        assert tracker.current_state == 'needs_permission'

    def test_trust_dialog_cursor_positioned(self, tmp_path: Path) -> None:
        """Real TUI output: cursor positioning CSI sequences replace spaces.
        After ANSI stripping, words merge (e.g. 'Yes,Itrustthisfolder')."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        t[0] = 1.0
        # Realistic Ink rendering: each word positioned via CSI
        tracker.on_output(
            b'\x1b[10;1HIs\x1b[10;4Hthis\x1b[10;9Ha\x1b[10;11Hproject'
            b'\x1b[10;19Hyou\x1b[10;23Htrust?\r\n'
            b'\x1b[11;3H1.\x1b[11;6HYes,\x1b[11;11HI'
            b'\x1b[11;13Htrust\x1b[11;19Hthis\x1b[11;24Hfolder\r\n'
            b'\x1b[12;3H2.\x1b[12;6HNo,\x1b[12;10Hexit\r\n'
        )
        assert tracker.current_state == 'needs_permission'

    def test_trust_dialog_split_across_chunks(self, tmp_path: Path) -> None:
        """Trust dialog text split across PTY read chunks."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Chunk 1: beginning (TUI rendering)
        t[0] = 1.0
        tracker.on_output(
            b'\x1b[2J\x1b[H' + b'\xe2\x94\x80' * 100
            + b'\x1b[5;1HAccessingworkspace:\r\n'
            + b'\x1b[7;1HQuicksafetycheck\r\n'
            + b'\x1b[10;1HIs\x1b[10;4Hthis\x1b[10;9Ha'
            + b'\x1b[10;11Hproject\x1b[10;19Hyou\x1b[10;23Htru'  # split mid-word
        )
        assert tracker.current_state == 'idle'
        # Chunk 2: rest of dialog
        t[0] = 1.1
        tracker.on_output(
            b'st?\r\n'
            b'\x1b[11;3H1.\x1b[11;6HYes,\x1b[11;11HI'
            b'\x1b[11;13Htrust\x1b[11;19Hthis\x1b[11;24Hfolder\r\n'
        )
        assert tracker.current_state == 'needs_permission'

    def test_trust_dialog_clears_buffer_and_accumulator(self, tmp_path: Path) -> None:
        """After trust dialog detection, output buffer and accumulator reset."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        t[0] = 1.0
        tracker.on_output(
            b'Is this a project you trust?\r\n'
            b'\xe2\x9d\xaf 1. Yes, I trust this folder\r\n'
        )
        assert tracker.current_state == 'needs_permission'
        assert len(tracker._output_buf) == 0
        assert tracker._idle_output_acc == 0

    def test_trust_dialog_sets_waiting_since(self, tmp_path: Path) -> None:
        """Trust dialog sets _waiting_since for timeout tracking."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        t[0] = 5.0
        tracker.on_output(
            b'Is this a project you trust?\r\n'
            b'\xe2\x9d\xaf 1. Yes, I trust this folder\r\n'
        )
        assert tracker._waiting_since == 5.0

    def test_trust_dialog_resume_goes_to_idle(self, tmp_path: Path) -> None:
        """After trust dialog → needs_permission, answering and seeing
        startup output should go to idle (not running), because Claude
        hasn't processed any request yet."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Trust dialog detected
        t[0] = 1.0
        tracker.on_output(
            b'Is this a project you trust?\r\n'
            b'\xe2\x9d\xaf 1. Yes, I trust this folder\r\n'
        )
        assert tracker.current_state == 'needs_permission'
        assert tracker._trust_dialog_phase is True
        # User answers (presses Enter)
        t[0] = 3.0
        tracker.on_input(b'\r')
        # After 2s grace, Claude startup output arrives
        t[0] = 4.0
        tracker.on_output(b'Claude Code v2.1.41 Opus 4.6')
        assert tracker.current_state == 'idle'
        assert tracker._trust_dialog_phase is False

    def test_trust_dialog_resume_does_not_affect_normal_permission(
        self, tmp_path: Path,
    ) -> None:
        """Normal permission prompts (not trust dialog) still resume to
        running as before."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        assert tracker._trust_dialog_phase is False
        # User approves
        t[0] = 3.0
        tracker.on_input(b'y')
        # After grace, output → running (normal behavior)
        t[0] = 4.0
        tracker.on_output(b'Processing...')
        assert tracker.current_state == 'running'

    def test_normal_output_does_not_trigger_trust_dialog(self, tmp_path: Path) -> None:
        """Output without the trust dialog pattern stays idle."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        t[0] = 1.0
        tracker.on_output(b'Hello world, processing your request...')
        assert tracker.current_state == 'idle'

    def test_standard_dialog_at_startup(self, tmp_path: Path) -> None:
        """Standard dialog patterns (Enter to select, Esc to cancel) at
        startup should also be detected as needs_permission."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        assert not tracker._seen_user_input
        t[0] = 1.0
        tracker.on_output(
            b'Some permission prompt\r\n'
            b'Enter to select  Esc to cancel\r\n'
        )
        assert tracker.current_state == 'needs_permission'
        # Standard dialog should NOT set trust_dialog_phase
        assert tracker._trust_dialog_phase is False


# ---------------------------------------------------------------------------
# Signal from idle (idle → needs_permission/needs_input via signal file)
# ---------------------------------------------------------------------------

class TestSignalFromIdle:
    """Tests for the fix: signal file must be read even when current state
    is idle, so that Notification hooks writing needs_permission/needs_input
    trigger a proper state transition."""

    def test_signal_from_idle_to_needs_permission(self, tmp_path: Path) -> None:
        """idle + signal=needs_permission → needs_permission."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Hook writes needs_permission while idle
        t[0] = 1.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_signal_from_idle_to_needs_input(self, tmp_path: Path) -> None:
        """idle + signal=needs_input → needs_input."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        assert tracker.get_state(pty_alive=True) == 'idle'
        t[0] = 1.0
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'needs_input'

    def test_signal_from_idle_to_idle_is_noop(self, tmp_path: Path) -> None:
        """idle + signal=idle → stays idle (no transition)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        assert tracker.get_state(pty_alive=True) == 'idle'
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        # _waiting_since should remain None (no transition occurred)
        assert tracker._waiting_since is None

    def test_signal_from_idle_sets_waiting_since(self, tmp_path: Path) -> None:
        """Transition from idle sets _waiting_since for timeout tracking."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        assert tracker._waiting_since is None
        t[0] = 3.0
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)
        assert tracker._waiting_since == 3.0

    def test_signal_from_idle_clears_output_acc(self, tmp_path: Path) -> None:
        """Transition from idle resets output accumulator and buffer."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Accumulate some output
        tracker._idle_output_acc = 150
        tracker._output_buf.extend(b'some data')
        t[0] = 1.0
        write_signal(tracker, 'needs_input')
        tracker.get_state(pty_alive=True)
        assert tracker._idle_output_acc == 0
        assert len(tracker._output_buf) == 0

    def test_stale_signal_file_deleted_on_init(self, tmp_path: Path) -> None:
        """A stale signal file from a SIGKILL'd server must not cause a
        false transition when the new tracker starts."""
        t = [0.0]
        signal_file = tmp_path / "test.signal"
        # Simulate stale signal left by a killed server
        signal_file.write_text(json.dumps({"state": "needs_permission"}))
        assert signal_file.exists()
        # New tracker deletes it on init
        tracker = ClaudeStateTracker(
            signal_file=signal_file, clock=lambda: t[0],
        )
        assert not signal_file.exists()
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# PTY dialog detection (running state)
# ---------------------------------------------------------------------------

class TestPTYDialogDetection:
    """Permission dialogs during running state are detected via Notification
    hooks (signal file), NOT PTY output patterns.  PTY dialog detection was
    removed to eliminate false positives from conversation text containing
    dialog-like patterns (e.g. Claude discussing permission prompts).

    These tests verify that:
    - Dialog-like text in conversation does NOT trigger needs_permission
    - Interrupt detection still works correctly alongside dialog text
    - Signal-file-based permission detection works during running state
    """

    def test_dialog_text_in_conversation_stays_running(
        self, tmp_path: Path,
    ) -> None:
        """Dialog patterns in conversation text should NOT trigger
        needs_permission — only Notification hooks (signal file) do."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(
            b'Allow Claude to use Bash?\n'
            b'1. Allow once\n'
            b'2. Allow always\n'
            b'3. Deny\n'
            b'Enter to select \xc2\xb7 Esc to cancel\n'
        )
        assert tracker.current_state == 'running'

    def test_permission_detected_via_signal_file(
        self, tmp_path: Path,
    ) -> None:
        """Real permission prompts are detected via the Notification hook
        writing needs_permission to the signal file."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Notification hook fires → signal file written
        signal_file = tmp_path / 'test.signal'
        signal_file.write_text('{"state": "needs_permission"}')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_interrupted_takes_priority_over_dialog(
        self, tmp_path: Path,
    ) -> None:
        """If 'Interrupted' is in the same chunk, interrupted wins."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x1b')  # User presses Escape
        t[0] = 1.0
        tracker.on_output(
            b'Interrupted\n'
            b'Enter to select \xc2\xb7 Esc to cancel\n'
        )
        assert tracker.current_state == 'interrupted'

    def test_interrupt_detected_without_escape(
        self, tmp_path: Path,
    ) -> None:
        """Interrupt detected via confirmed_interrupt_pattern
        (Interrupted·) even without user Escape."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # No Escape pressed — Ctrl+C bypassed on_input or CLI
        # self-interrupted.  TUI renders the interrupt prompt.
        tracker.on_output(
            b'Interrupted \xc2\xb7 What should Claude do instead?\n'
            b'1. Try again\n'
            b'2. Stop\n'
            b'Enter to select \xc2\xb7 Esc to cancel\n'
        )
        assert tracker.current_state == 'interrupted'

    def test_interrupt_with_preceding_output(
        self, tmp_path: Path,
    ) -> None:
        """Interrupt detected when prompt follows earlier tool output."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Tool output before the interrupt
        tracker.on_output(b'Processing files...\nDone with step 1\n')
        t[0] = 2.0
        # CLI interrupts, TUI re-renders full screen
        tracker.on_output(
            b'Interrupted \xc2\xb7 What should Claude do instead?\n'
            b'1. Try again\n'
            b'Enter to select \xc2\xb7 Esc to cancel\n'
        )
        assert tracker.current_state == 'interrupted'

    def test_no_false_interrupted_from_conversation_text(
        self, tmp_path: Path,
    ) -> None:
        """'Interrupted' in conversation text (no middle dot) should
        NOT false-positive as interrupted."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Claude discusses interrupts — no middle dot after the word
        tracker.on_output(
            b'The process was Interrupted due to a timeout.\n'
        )
        assert tracker.current_state == 'running'

    def test_no_false_interrupted_from_conversation_plus_dialog(
        self, tmp_path: Path,
    ) -> None:
        """'Interrupted' in conversation text followed by dialog-like
        patterns should stay running — not trigger interrupted or
        needs_permission."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Claude discusses interrupts in its conversational output
        tracker.on_output(
            b'The process was Interrupted due to a timeout.\n'
        )
        assert tracker.current_state == 'running'
        t[0] = 2.0
        # Later, dialog-like text in conversation (not a real dialog)
        tracker.on_output(
            b'Allow Bash?\n'
            b'1. Allow once\n'
            b'Enter to select \xc2\xb7 Esc to cancel\n'
        )
        # Stays running — only the Notification hook can trigger
        # needs_permission during running state
        assert tracker.current_state == 'running'

    def test_interrupt_prompt_split_across_chunks(
        self, tmp_path: Path,
    ) -> None:
        """Interrupt detected when 'Interrupted·' arrives in a
        separate chunk from dialog patterns."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # First chunk: just tool output
        tracker.on_output(b'Running command...\n')
        assert tracker.current_state == 'running'
        t[0] = 1.1
        # Second chunk: the interrupt prompt with middle dot
        tracker.on_output(
            b'Interrupted \xc2\xb7 What should Claude do instead?\n'
        )
        assert tracker.current_state == 'interrupted'


# ---------------------------------------------------------------------------
# Idle-state interrupt detection (confirmed pattern fallback)
# ---------------------------------------------------------------------------

class TestIdleInterruptDetection:
    """Test confirmed_interrupt_pattern detection while in idle state.

    Covers the case where the state transitioned to idle (via Stop hook
    or silence timeout) before the interrupt PTY output arrived.
    """

    def test_claude_interrupt_detected_in_idle(self, tmp_path: Path) -> None:
        """Claude interrupt prompt detected while idle."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Simulate: running → idle via signal file, then interrupt output
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        t[0] = 2.0
        # Interrupt PTY output arrives while idle
        tracker.on_output(
            b'Interrupted \xc2\xb7 What should Claude do instead?\n'
            b'1. Try again\n'
        )
        assert tracker.current_state == 'interrupted'

    def test_codex_interrupt_detected_in_idle(self, tmp_path: Path) -> None:
        """Codex interrupt prompt detected while idle (critical:
        output_triggers_running=False means no further processing
        without this check)."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Simulate: running → idle via signal file
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        t[0] = 2.0
        # Interrupt PTY output arrives while idle
        tracker.on_output(
            b'Conversation interrupted - tell the model what to do.'
        )
        assert tracker.current_state == 'interrupted'

    def test_no_false_interrupted_in_idle_from_conversation(
        self, tmp_path: Path,
    ) -> None:
        """Generic 'Interrupted' in conversation text while idle
        should NOT trigger false interrupted (no middle dot)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)
        t[0] = 2.0
        tracker.on_output(
            b'The process was Interrupted due to a timeout.\n'
        )
        assert tracker.current_state == 'idle'

    def test_idle_interrupt_requires_seen_user_input(
        self, tmp_path: Path,
    ) -> None:
        """Confirmed pattern check should not fire during startup
        (before any user input)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # No on_send() or on_input() — _seen_user_input is False
        t[0] = 1.0
        tracker.on_output(
            b'Interrupted \xc2\xb7 What should Claude do instead?\n'
        )
        # Should stay idle (startup phase, not a real interrupt)
        assert tracker.current_state == 'idle'


class TestCleanup:
    def test_cleanup_deletes_signal_file(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        write_signal(tracker, 'idle')
        assert tracker._signal_file.exists()
        tracker.cleanup()
        assert not tracker._signal_file.exists()

    def test_cleanup_no_error_if_missing(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        assert not tracker._signal_file.exists()
        tracker.cleanup()  # Should not raise


# ---------------------------------------------------------------------------
# Interrupted state specific behavior
# ---------------------------------------------------------------------------

class TestInterruptedState:
    def test_interrupted_blocks_auto_send(self, tmp_path: Path) -> None:
        """Interrupted state should block auto-send regardless of mode."""
        t = [0.0]
        for mode in ('pause', 'always'):
            tracker = make_tracker(tmp_path, t, auto_send_mode=mode)
            tracker.on_send()
            t[0] = 0.5
            tracker.on_input(b'\x1b')  # User presses Escape
            t[0] = 1.0
            tracker.on_output(b'some text Interrupted more text')
            assert tracker.current_state == 'interrupted'
            assert not tracker.is_ready(pty_alive=True)

    def test_interrupted_protected_from_needs_input_signal(self, tmp_path: Path) -> None:
        """Notification hook fires needs_input for the interrupt dialog —
        interrupted state should be protected for 5s."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x1b')  # User presses Escape
        t[0] = 1.0
        tracker.on_output(b'some text Interrupted more text')
        assert tracker.current_state == 'interrupted'
        # Notification hook writes needs_input within 5s
        t[0] = 2.0
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_interrupted_protected_from_idle_signal(self, tmp_path: Path) -> None:
        """Stop hook writes idle on Escape — interrupted should be
        protected for 5s."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x1b')  # User presses Escape
        t[0] = 1.0
        tracker.on_output(b'some text Interrupted more text')
        assert tracker.current_state == 'interrupted'
        # Stop hook writes idle within 5s
        t[0] = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_interrupted_yields_to_needs_input_after_5s(self, tmp_path: Path) -> None:
        """After 5s grace, a needs_input signal should be honored."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 0.5
        tracker.on_input(b'\x1b')  # User presses Escape
        t[0] = 1.0
        tracker.on_output(b'some text Interrupted more text')
        assert tracker.current_state == 'interrupted'
        # After 5s grace, needs_input signal is honored
        t[0] = 7.0
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'needs_input'

    def test_needs_input_corrected_to_interrupted_by_pty_output(
        self, tmp_path: Path,
    ) -> None:
        """Race: Stop hook → idle, Notification hook → needs_input, then PTY
        output with 'Interrupted' arrives.  The on_output handler should
        correct needs_input back to interrupted."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Stop hook races ahead, writes idle
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Notification hook writes needs_input (interrupt dialog)
        t[0] = 1.1
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'needs_input'
        # Simulate Escape keypress just before
        tracker._last_input_time = 0.9
        tracker._last_escape_time = 0.9
        # PTY output with "Interrupted" arrives
        t[0] = 1.2
        tracker.on_output(b'Interrupted \xc2\xb7 What should Claude do instead?')
        assert tracker.current_state == 'interrupted'

    def test_interrupted_user_answered_resumes_running(
        self, tmp_path: Path,
    ) -> None:
        """After 5s protection expires with stale idle signal, if the user
        typed after the interrupt, timing is preserved so on_output()
        can detect the resume when printable output arrives."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # User presses Escape
        tracker.on_input(b'\x1b')
        # PTY outputs "Interrupted"
        t[0] = 1.2
        tracker.on_output(b'Interrupted')
        assert tracker.current_state == 'interrupted'
        # Stop hook writes idle
        write_signal(tracker, 'idle')
        # Within 5s protection, idle is blocked
        t[0] = 3.0
        assert tracker.get_state(pty_alive=True) == 'interrupted'
        # User types an answer at T=3.5
        t[0] = 3.5
        tracker.on_input(b'do something else')
        # Claude produces output (ANSI-only during protection window)
        t[0] = 4.0
        tracker.on_output(b'\x1b[2K\x1b[1G')  # ANSI only, no printable text
        # Protection expires at T=6.2 (1.2 + 5.0) — stale idle accepted
        # but timing preserved (user_answered)
        t[0] = 7.0
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Now Claude produces printable output — idle→running fires
        # because _idle_since was preserved (not reset to T=7.0)
        t[0] = 7.5
        tracker.on_output(b'Here is my response...')
        t[0] = 8.0
        tracker.on_output(b'x' * 201)  # exceed 200-byte threshold
        assert tracker.current_state == 'running'

    def test_interrupted_user_answered_without_preserve_would_be_stuck(
        self, tmp_path: Path,
    ) -> None:
        """Verify the fix: without timing preservation, _idle_since would
        be set to now (T=7.0), making _last_input_time (T=3.5) < _idle_since,
        which would block idle→running accumulation permanently."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_input(b'\x1b')
        t[0] = 1.2
        tracker.on_output(b'Interrupted')
        assert tracker.current_state == 'interrupted'
        write_signal(tracker, 'idle')
        # User types
        t[0] = 3.5
        tracker.on_input(b'do something else')
        # Protection expires, timing preserved
        t[0] = 7.0
        tracker.get_state(pty_alive=True)
        # Key assertion: _idle_since should be the old _waiting_since (1.2),
        # NOT 7.0.  This ensures _last_input_time (3.5) > _idle_since (1.2).
        assert tracker._idle_since < tracker._last_input_time

    def test_interrupted_no_input_accepts_stale_idle(
        self, tmp_path: Path,
    ) -> None:
        """If user didn't type after interrupt, stale idle is accepted
        after the 5s window (existing behavior preserved)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_input(b'\x1b')
        t[0] = 1.2
        tracker.on_output(b'Interrupted')
        assert tracker.current_state == 'interrupted'
        write_signal(tracker, 'idle')
        # No user input after interrupt — stale idle accepted after 5s
        t[0] = 7.0
        assert tracker.get_state(pty_alive=True) == 'idle'
        # _idle_since is set to now (normal behavior)
        assert tracker._idle_since == 7.0

    def test_interrupted_to_needs_input_preserves_timing(
        self, tmp_path: Path,
    ) -> None:
        """When interrupted→needs_input after 5s with user input,
        _waiting_since is preserved so on_output() resume works."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_input(b'\x1b')
        t[0] = 1.2
        tracker.on_output(b'Interrupted')
        assert tracker.current_state == 'interrupted'
        # User types at T=3
        t[0] = 3.0
        tracker.on_input(b'answer')
        # Notification hook writes needs_input (stale, from interrupt dialog)
        write_signal(tracker, 'needs_input')
        # Protection expires
        t[0] = 7.0
        assert tracker.get_state(pty_alive=True) == 'needs_input'
        # _waiting_since should be preserved (not reset to T=7.0)
        assert tracker._waiting_since < tracker._last_input_time
        # Now printable output triggers resume
        t[0] = 7.5
        tracker.on_output(b'Processing your request...')
        assert tracker.current_state == 'running'

    def test_needs_input_user_answered_resumes_running(
        self, tmp_path: Path,
    ) -> None:
        """on_output() resume works when user types after needs_input
        and printable output follows (existing behavior, no get_state involved)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Signal needs_input directly
        write_signal(tracker, 'needs_input')
        t[0] = 0.5
        assert tracker.get_state(pty_alive=True) == 'needs_input'
        # User answers at T=3
        t[0] = 3.0
        tracker.on_input(b'yes')
        # Output follows (Claude starts processing) — after 2s grace
        t[0] = 3.5
        tracker.on_output(b'\x1b[2Kprocessing...')
        assert tracker.current_state == 'running'

    def test_interrupted_fresh_needs_permission_honored(
        self, tmp_path: Path,
    ) -> None:
        """A fresh needs_permission signal should be honored with fresh
        timing — user_answered should NOT apply for non-stale signals."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_input(b'\x1b')
        t[0] = 1.2
        tracker.on_output(b'Interrupted')
        assert tracker.current_state == 'interrupted'
        # User types at T=3
        t[0] = 3.0
        tracker.on_input(b'run bash command')
        # Output follows (ANSI only)
        t[0] = 4.0
        tracker.on_output(b'\x1b[2K\x1b[1G')
        # Claude hits permission prompt — fresh signal
        t[0] = 7.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # _waiting_since should be reset to now (fresh timing),
        # NOT preserved from the old interrupt
        assert tracker._waiting_since == 7.0


# ---------------------------------------------------------------------------
# CLIState enum and backward compatibility
# ---------------------------------------------------------------------------

class TestCLIStateEnum:
    """Tests for CLIState enum, state sets, and backward-compat alias."""

    def test_cli_state_string_comparison(self) -> None:
        """CLIState members compare equal to their string values."""
        from leap.cli_providers.states import CLIState
        assert CLIState.IDLE == 'idle'
        assert CLIState.RUNNING == 'running'
        assert CLIState.NEEDS_PERMISSION == 'needs_permission'
        assert CLIState.NEEDS_INPUT == 'needs_input'
        assert CLIState.INTERRUPTED == 'interrupted'

    def test_waiting_states_membership(self) -> None:
        from leap.cli_providers.states import CLIState, WAITING_STATES
        assert CLIState.NEEDS_PERMISSION in WAITING_STATES
        assert CLIState.NEEDS_INPUT in WAITING_STATES
        assert CLIState.INTERRUPTED in WAITING_STATES
        assert CLIState.IDLE not in WAITING_STATES
        assert CLIState.RUNNING not in WAITING_STATES

    def test_signal_states_membership(self) -> None:
        from leap.cli_providers.states import CLIState, SIGNAL_STATES
        assert CLIState.IDLE in SIGNAL_STATES
        assert CLIState.NEEDS_PERMISSION in SIGNAL_STATES
        assert CLIState.NEEDS_INPUT in SIGNAL_STATES
        assert CLIState.RUNNING not in SIGNAL_STATES
        assert CLIState.INTERRUPTED not in SIGNAL_STATES

    def test_prompt_states_membership(self) -> None:
        from leap.cli_providers.states import CLIState, PROMPT_STATES
        assert CLIState.NEEDS_PERMISSION in PROMPT_STATES
        assert CLIState.NEEDS_INPUT in PROMPT_STATES
        assert CLIState.IDLE not in PROMPT_STATES
        assert CLIState.RUNNING not in PROMPT_STATES
        assert CLIState.INTERRUPTED not in PROMPT_STATES

    def test_string_membership_in_state_sets(self) -> None:
        """Plain strings work with state sets (str, Enum)."""
        from leap.cli_providers.states import WAITING_STATES, PROMPT_STATES
        assert 'needs_input' in WAITING_STATES
        assert 'needs_permission' in PROMPT_STATES

    def test_backward_compat_signal_alias(self, tmp_path: Path) -> None:
        """Old hooks writing 'has_question' should be read as 'needs_input'."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Write old-style signal
        tracker._signal_file.write_text(json.dumps({"state": "has_question"}))
        assert tracker.get_state(pty_alive=True) == 'needs_input'

    def test_import_from_cli_providers(self) -> None:
        """CLIState can be imported from the cli_providers package."""
        from leap.cli_providers import CLIState
        assert CLIState.IDLE == 'idle'


# ---------------------------------------------------------------------------
# Codex-specific helpers
# ---------------------------------------------------------------------------

def make_codex_tracker(
    tmp_path: Path,
    t: List[float],
    auto_send_mode: str = 'pause',
) -> ClaudeStateTracker:
    """Create a tracker with Codex provider, fake clock, signal file."""
    signal_file = tmp_path / "test.signal"
    return ClaudeStateTracker(
        signal_file=signal_file,
        auto_send_mode=auto_send_mode,
        clock=lambda: t[0],
        provider=CodexProvider(),
    )


# ---------------------------------------------------------------------------
# Codex: enter_triggers_running
# ---------------------------------------------------------------------------

class TestCodexEnterTriggersRunning:
    """Test Enter-key based idle→running detection for Ratatui CLIs."""

    def test_enter_in_idle_transitions_to_running(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        assert tracker.get_state(pty_alive=True) == 'idle'
        tracker.on_input(b'\r')
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_enter_not_in_idle_is_noop(self, tmp_path: Path) -> None:
        """Enter while running should not reset the running state."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        assert tracker.get_state(pty_alive=True) == 'running'
        t[0] = 1.0
        tracker.on_input(b'\r')
        # Still running (not reset)
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_enter_deletes_signal_file(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        # Write a stale signal
        write_signal(tracker, 'idle')
        assert tracker._signal_file.exists()
        tracker.on_input(b'\r')
        assert not tracker._signal_file.exists()

    def test_enter_clears_output_buf(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_output(b'some TUI output')
        tracker.on_input(b'\r')
        assert tracker._output_buf == bytearray()


# ---------------------------------------------------------------------------
# Codex: silence timeout
# ---------------------------------------------------------------------------

class TestCodexSilenceTimeout:
    """Test Codex-specific shorter silence timeout (8s vs 15s)."""

    def test_codex_uses_shorter_silence_timeout(self, tmp_path: Path) -> None:
        """Codex falls back to idle after 8s of silence, not 15s."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'thinking...')
        # After 9 seconds of silence — Codex timeout (8s) should fire
        t[0] = 10.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_codex_not_idle_before_timeout(self, tmp_path: Path) -> None:
        """Codex stays running if output arrived within 8s."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'thinking...')
        # Only 5 seconds — still within 8s timeout
        t[0] = 6.0
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_claude_uses_longer_timeout(self, tmp_path: Path) -> None:
        """Claude (default) still uses 15s timeout, not 8s."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'processing...')
        # 10 seconds — within Claude's 15s timeout
        t[0] = 11.0
        assert tracker.get_state(pty_alive=True) == 'running'
        # 16 seconds — exceeds timeout
        t[0] = 17.0
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# Codex: interrupted detection
# ---------------------------------------------------------------------------

class TestCodexInterrupted:
    """Test interrupted state detection with Codex's lowercase pattern."""

    def test_interrupted_detected_from_pty_output(self, tmp_path: Path) -> None:
        """Codex outputs 'interrupted' (lowercase) → interrupted state."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Simulate user pressing Escape
        tracker.on_input(b'\x1b')
        tracker._last_input_time = t[0]
        t[0] = 1.5
        # PTY outputs the interrupted message
        tracker.on_output(
            b'Conversation interrupted - tell the model what to do differently.'
        )
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_interrupted_detected_with_ansi(self, tmp_path: Path) -> None:
        """Interrupted pattern detected even with ANSI sequences mixed in."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_input(b'\x1b')
        tracker._last_input_time = t[0]
        t[0] = 1.5
        tracker.on_output(
            b'\x1b[31mConversation interrupted\x1b[0m - ...'
        )
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_signal_idle_blocked_during_escape_race(self, tmp_path: Path) -> None:
        """Stop hook writing 'idle' is blocked while awaiting interrupt pattern."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # User presses Escape (single byte)
        tracker.on_input(b'\x1b')
        t[0] = 1.5
        # Stop hook fires, writes idle
        write_signal(tracker, 'idle')
        # Within ESCAPE_RACE_WINDOW — idle blocked
        assert tracker.get_state(pty_alive=True) == 'running'
        # Now PTY outputs interrupted pattern
        t[0] = 2.0
        tracker.on_output(b'Conversation interrupted')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_interrupt_detected_without_escape(self, tmp_path: Path) -> None:
        """Codex interrupt detected via confirmed_interrupt_pattern
        even when Ctrl+C bypassed on_input()."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # No Escape/Ctrl+C seen by on_input — bypassed to child process
        tracker.on_output(
            b'Conversation interrupted - tell the model what to do differently.'
        )
        assert tracker.current_state == 'interrupted'

    def test_no_false_interrupted_from_conversation_text(
        self, tmp_path: Path,
    ) -> None:
        """Generic 'interrupted' in conversation text should NOT
        trigger false interrupted state."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Codex outputs text discussing interrupts — but not
        # "Conversation interrupted" as a phrase
        tracker.on_output(
            b'The task was interrupted by a network error.'
        )
        assert tracker.current_state == 'running'

    def test_interrupt_with_ansi_without_escape(self, tmp_path: Path) -> None:
        """Codex interrupt with ANSI detected without Escape keypress."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(
            b'\x1b[31m\xe2\x96\xa0 Conversation interrupted\x1b[0m'
            b' - tell the model what to do differently.'
        )
        assert tracker.current_state == 'interrupted'


# ---------------------------------------------------------------------------
# Codex: CSI escape sequence filtering
# ---------------------------------------------------------------------------

class TestCodexCSIFiltering:
    """Test that CSI terminal events don't hold ESCAPE_RACE_WINDOW open."""

    def test_focus_events_dont_block_idle_signal(self, tmp_path: Path) -> None:
        """Focus in/out events (\x1b[I, \x1b[O) should not refresh
        _last_input_time and block running→idle signal transition."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Simulate real Escape key
        tracker.on_input(b'\x1b')
        t[0] = 3.0  # Past ESCAPE_RACE_WINDOW (2s)
        # Focus event arrives (terminal switching)
        tracker.on_input(b'\x1b[I')  # focus in
        t[0] = 3.5
        # Stop hook writes idle
        write_signal(tracker, 'idle')
        # Should accept idle because focus event didn't refresh
        # _last_input_time for ESCAPE_RACE_WINDOW purposes
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_real_escape_still_blocks_idle(self, tmp_path: Path) -> None:
        """A real Escape key (single byte) should still block idle signal."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_input(b'\x1b')  # Real Escape
        t[0] = 1.5
        write_signal(tracker, 'idle')
        # Within 2s of Escape — idle blocked
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_bundled_escape_plus_csi_updates_time(self, tmp_path: Path) -> None:
        """Escape key + CSI bundled (\x1b\x1b[I) should update time."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Bundled: Escape key + focus event in same read
        tracker.on_input(b'\x1b\x1b[I')
        t[0] = 1.5
        write_signal(tracker, 'idle')
        # Should block — the bundled Escape updated _last_input_time
        assert tracker.get_state(pty_alive=True) == 'running'


# ---------------------------------------------------------------------------
# Codex: output_triggers_running disabled
# ---------------------------------------------------------------------------

class TestCodexOutputDoesNotTriggerRunning:
    """Verify output accumulation doesn't trigger idle→running for Codex."""

    def test_output_does_not_trigger_running(self, tmp_path: Path) -> None:
        """Large output while idle should NOT trigger running for Codex."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        # Simulate user input to mark seen_user_input
        tracker.on_input(b'x')
        t[0] = 1.0
        # Go to idle via signal
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # Process signal
        # Large output (TUI redraw) should not trigger running
        t[0] = 2.0
        tracker.on_output(b'x' * 500)
        t[0] = 3.0
        tracker.on_output(b'y' * 500)
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# Waiting state timeout (applies to all providers)
# ---------------------------------------------------------------------------

class TestWaitingStateTimeout:
    """Test fallback timeout for stuck waiting states."""

    def test_interrupted_times_out_to_idle(self, tmp_path: Path) -> None:
        """Interrupted state falls back to idle after WAITING_STATE_TIMEOUT."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_input(b'\x1b')
        tracker._last_input_time = t[0]
        t[0] = 1.5
        tracker.on_output(b'some text Interrupted more text')
        assert tracker.get_state(pty_alive=True) == 'interrupted'
        # Output timestamp is set to 1.5
        # After WAITING_STATE_TIMEOUT (30s) with no output → idle
        t[0] = 1.5 + WAITING_STATE_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_interrupted_stays_if_output_arrives(self, tmp_path: Path) -> None:
        """Interrupted state should NOT time out if output keeps arriving."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_input(b'\x1b')
        tracker._last_input_time = t[0]
        t[0] = 1.5
        tracker.on_output(b'some text Interrupted more text')
        assert tracker.get_state(pty_alive=True) == 'interrupted'
        # Output arrives within the timeout window
        t[0] = 20.0
        tracker.on_output(b'\x1b[Hsome TUI redraw')
        # Not timed out yet (last output was at t=20)
        t[0] = 25.0
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_needs_permission_times_out(self, tmp_path: Path) -> None:
        """needs_permission also falls back to idle after timeout."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # Some output at transition
        tracker.on_output(b'prompt text')
        # Wait beyond timeout
        t[0] = 1.0 + WAITING_STATE_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_codex_interrupted_times_out(self, tmp_path: Path) -> None:
        """Codex interrupted state also times out correctly."""
        t = [0.0]
        tracker = make_codex_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_input(b'\x1b')
        tracker._last_input_time = t[0]
        t[0] = 1.5
        tracker.on_output(b'Conversation interrupted')
        assert tracker.get_state(pty_alive=True) == 'interrupted'
        t[0] = 1.5 + WAITING_STATE_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'idle'
