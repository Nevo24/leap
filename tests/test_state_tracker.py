"""Tests for ClaudeStateTracker state machine logic."""

import json
from pathlib import Path
from typing import List

import pytest

from claudeq.server.state_tracker import ClaudeStateTracker
from claudeq.utils.constants import OUTPUT_SILENCE_TIMEOUT


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
# Signal file transitions (running → idle/needs_permission/has_question)
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

    def test_signal_file_has_question(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        write_signal(tracker, 'has_question')
        assert tracker.get_state(pty_alive=True) == 'has_question'

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
# Running → has_question (Interrupted detection)
# ---------------------------------------------------------------------------

class TestInterruptedDetection:
    def test_interrupted_detected_in_running(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'some text Interrupted more text')
        assert tracker.current_state == 'has_question'

    def test_interrupted_split_across_chunks(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'some text Inter')
        assert tracker.current_state == 'running'
        t[0] = 1.1
        tracker.on_output(b'rupted more text')
        assert tracker.current_state == 'has_question'

    def test_output_buffer_capped_at_512(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Fill buffer with >512 bytes of non-matching data
        tracker.on_output(b'X' * 600)
        assert len(tracker._output_buf) <= 512


# ---------------------------------------------------------------------------
# Escape race (idle state Interrupted detection)
# ---------------------------------------------------------------------------

class TestEscapeRace:
    def test_escape_race_interrupted_in_idle(self, tmp_path: Path) -> None:
        """Stop hook writes idle, then PTY outputs 'Interrupted' → has_question."""
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
        assert tracker.current_state == 'has_question'

    def test_escape_race_after_signal_idle_transition(self, tmp_path: Path) -> None:
        """Escape race detection must work even when the running→idle
        signal transition happened just before 'Interrupted' arrives."""
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
        # Stop hook fires → signal file says idle → get_state transitions
        t[0] = 5.1
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        # PTY outputs "Interrupted" shortly after
        t[0] = 5.2
        tracker.on_output(b'Interrupted')
        assert tracker.current_state == 'has_question'

    def test_escape_race_only_within_3s_of_input(self, tmp_path: Path) -> None:
        """'Interrupted' in idle state ignored if >3s after input."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)
        # User pressed Escape
        t[0] = 1.5
        tracker.on_input(b'\x1b')
        # PTY outputs "Interrupted" after >3s
        t[0] = 5.0
        tracker.on_output(b'Interrupted')
        # Should stay idle — too late for the race window
        assert tracker.current_state == 'idle'


# ---------------------------------------------------------------------------
# Stop hook race (has_question protected from idle signal)
# ---------------------------------------------------------------------------

class TestStopHookRace:
    def test_has_question_protected_from_idle_signal_within_5s(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'has_question')
        assert tracker.get_state(pty_alive=True) == 'has_question'
        # Immediately write idle signal — within 5s grace
        t[0] = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'has_question'

    def test_has_question_yields_to_idle_signal_after_5s(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'has_question')
        assert tracker.get_state(pty_alive=True) == 'has_question'
        # After 5s grace, idle signal is honored
        t[0] = 7.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# Resume detection (has_question/needs_permission → running)
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
        write_signal(tracker, 'has_question')
        assert tracker.get_state(pty_alive=True) == 'has_question'
        # User types, but only ANSI output → stays has_question
        t[0] = 3.0
        tracker.on_input(b'y')
        t[0] = 4.0
        tracker.on_output(b'\x1b[2J\x1b[H\r')
        assert tracker.current_state == 'has_question'

    def test_resume_blocked_without_user_input(self, tmp_path: Path) -> None:
        """TUI status bar rendering after has_question should NOT
        trigger resume (no user input since entering waiting state)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'has_question')
        assert tracker.get_state(pty_alive=True) == 'has_question'
        # After grace, printable output but NO user input → stays
        t[0] = 4.0
        tracker.on_output(b'Nevo.Mashiach 10% Opus 4.6 default')
        assert tracker.current_state == 'has_question'


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
        # has_question → ready in always mode
        t[0] = 7.0  # past 5s grace
        write_signal(tracker, 'has_question')
        # Read state to transition
        tracker.get_state(pty_alive=True)
        assert tracker.is_ready(pty_alive=True)


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

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
