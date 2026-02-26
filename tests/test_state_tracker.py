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
        assert tracker.current_state == 'interrupted'

    def test_interrupted_split_across_chunks(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
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

    def test_interrupted_protected_from_idle_signal_within_5s(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
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
        t[0] = 1.0
        tracker.on_output(b'Interrupted')
        assert tracker.current_state == 'interrupted'
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
            b'Accessing workspace:\r\n/Users/test\r\n'
            b'\xe2\x9d\xaf 1. Yes, I trust this folder\r\n'
            b'  2. No, exit\r\n'
        )
        assert tracker.current_state == 'needs_permission'

    def test_trust_dialog_cursor_positioned(self, tmp_path: Path) -> None:
        """Real TUI output: cursor positioning CSI sequences replace spaces.
        After ANSI stripping, words merge (e.g. 'Itrustthisfolder')."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        t[0] = 1.0
        # Realistic Ink rendering: each word positioned via CSI
        tracker.on_output(
            b'\x1b[10;1H\xe2\x9d\xaf\x1b[10;3H1.\x1b[10;6HYes,'
            b'\x1b[10;11HI\x1b[10;13Htrust\x1b[10;19Hthis'
            b'\x1b[10;24Hfolder\r\n'
            b'\x1b[11;3H2.\x1b[11;6HNo,\x1b[11;10Hexit\r\n'
        )
        assert tracker.current_state == 'needs_permission'

    def test_trust_dialog_split_across_chunks(self, tmp_path: Path) -> None:
        """Trust dialog text split across PTY read chunks."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Chunk 1: beginning (1000 bytes of TUI rendering)
        t[0] = 1.0
        tracker.on_output(
            b'\x1b[2J\x1b[H' + b'\xe2\x94\x80' * 100
            + b'\x1b[5;1HAccessingworkspace:\r\n'
            + b'\x1b[7;1HQuicksafetycheck\r\n'
            + b'\x1b[10;1H\xe2\x9d\xaf\x1b[10;3H1.\x1b[10;6HYes,'
            + b'\x1b[10;11HI\x1b[10;13Htrus'  # split mid-word
        )
        assert tracker.current_state == 'idle'
        # Chunk 2: rest of dialog
        t[0] = 1.1
        tracker.on_output(
            b't\x1b[10;18Hthis\x1b[10;23Hfolder\r\n'
            b'\x1b[11;3H2.\x1b[11;6HNo,\x1b[11;10Hexit\r\n'
        )
        assert tracker.current_state == 'needs_permission'

    def test_trust_dialog_clears_buffer_and_accumulator(self, tmp_path: Path) -> None:
        """After trust dialog detection, output buffer and accumulator reset."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        t[0] = 1.0
        tracker.on_output(b'I trust this folder')
        assert tracker.current_state == 'needs_permission'
        assert len(tracker._output_buf) == 0
        assert tracker._idle_output_acc == 0

    def test_trust_dialog_sets_waiting_since(self, tmp_path: Path) -> None:
        """Trust dialog sets _waiting_since for timeout tracking."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        t[0] = 5.0
        tracker.on_output(b'I trust this folder')
        assert tracker._waiting_since == 5.0

    def test_trust_dialog_resume_goes_to_idle(self, tmp_path: Path) -> None:
        """After trust dialog → needs_permission, answering and seeing
        startup output should go to idle (not running), because Claude
        hasn't processed any request yet."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Trust dialog detected
        t[0] = 1.0
        tracker.on_output(b'I trust this folder')
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


# ---------------------------------------------------------------------------
# Signal from idle (idle → needs_permission/has_question via signal file)
# ---------------------------------------------------------------------------

class TestSignalFromIdle:
    """Tests for the fix: signal file must be read even when current state
    is idle, so that Notification hooks writing needs_permission/has_question
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

    def test_signal_from_idle_to_has_question(self, tmp_path: Path) -> None:
        """idle + signal=has_question → has_question."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        assert tracker.get_state(pty_alive=True) == 'idle'
        t[0] = 1.0
        write_signal(tracker, 'has_question')
        assert tracker.get_state(pty_alive=True) == 'has_question'

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
        write_signal(tracker, 'has_question')
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
    """Detect permission/question dialogs from PTY output patterns."""

    def test_dialog_detected_from_pty_output(self, tmp_path: Path) -> None:
        """'Enter to select' + 'Esc to cancel' in running → needs_permission."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Simulate Ink TUI dialog output (plain text, no ANSI)
        tracker.on_output(
            b'Allow Claude to use Bash?\n'
            b'1. Allow once\n'
            b'2. Allow always\n'
            b'3. Deny\n'
            b'Enter to select \xc2\xb7 Esc to cancel\n'
        )
        assert tracker.current_state == 'needs_permission'

    def test_dialog_detected_with_ansi_sequences(self, tmp_path: Path) -> None:
        """Dialog detection works with cursor-positioned Ink TUI output."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Simulate Ink TUI with ANSI cursor positioning between words
        tracker.on_output(
            b'\x1b[3;1H Allow \x1b[3;8H once\n'
            b'\x1b[5;1H Enter \x1b[5;8H to \x1b[5;12H select\n'
            b'\x1b[6;1H Esc \x1b[6;6H to \x1b[6;10H cancel\n'
        )
        assert tracker.current_state == 'needs_permission'

    def test_dialog_seeds_prompt_buf(self, tmp_path: Path) -> None:
        """Dialog detection copies output_buf to _last_prompt_buf."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(
            b'Question here\n'
            b'1. Option A\n'
            b'Enter to select \xc2\xb7 Esc to cancel\n'
        )
        assert tracker.current_state == 'needs_permission'
        assert tracker._last_prompt_buf != b''
        assert len(tracker._output_buf) == 0  # cleared after detection

    def test_no_false_positive_with_only_enter_to_select(
        self, tmp_path: Path,
    ) -> None:
        """Require BOTH patterns to avoid false positives."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'Enter to select something from the list')
        assert tracker.current_state == 'running'

    def test_dialog_split_across_chunks(self, tmp_path: Path) -> None:
        """Dialog detected when patterns arrive in separate chunks."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'1. Option A\nEnter to select\n')
        assert tracker.current_state == 'running'  # only one pattern
        t[0] = 1.1
        tracker.on_output(b'Esc to cancel\n')
        assert tracker.current_state == 'needs_permission'

    def test_interrupted_takes_priority_over_dialog(
        self, tmp_path: Path,
    ) -> None:
        """If 'Interrupted' is in the same chunk, interrupted wins."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(
            b'Interrupted\n'
            b'Enter to select \xc2\xb7 Esc to cancel\n'
        )
        assert tracker.current_state == 'interrupted'


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
            t[0] = 1.0
            tracker.on_output(b'some text Interrupted more text')
            assert tracker.current_state == 'interrupted'
            assert not tracker.is_ready(pty_alive=True)

    def test_interrupted_protected_from_has_question_signal(self, tmp_path: Path) -> None:
        """Notification hook fires has_question for the interrupt dialog —
        interrupted state should be protected for 5s."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'some text Interrupted more text')
        assert tracker.current_state == 'interrupted'
        # Notification hook writes has_question within 5s
        t[0] = 2.0
        write_signal(tracker, 'has_question')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_interrupted_protected_from_idle_signal(self, tmp_path: Path) -> None:
        """Stop hook writes idle on Escape — interrupted should be
        protected for 5s."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'some text Interrupted more text')
        assert tracker.current_state == 'interrupted'
        # Stop hook writes idle within 5s
        t[0] = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_interrupted_yields_to_has_question_after_5s(self, tmp_path: Path) -> None:
        """After 5s grace, a has_question signal should be honored."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'some text Interrupted more text')
        assert tracker.current_state == 'interrupted'
        # After 5s grace, has_question signal is honored
        t[0] = 7.0
        write_signal(tracker, 'has_question')
        assert tracker.get_state(pty_alive=True) == 'has_question'

    def test_has_question_corrected_to_interrupted_by_pty_output(
        self, tmp_path: Path,
    ) -> None:
        """Race: Stop hook → idle, Notification hook → has_question, then PTY
        output with 'Interrupted' arrives.  The on_output handler should
        correct has_question back to interrupted."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Stop hook races ahead, writes idle
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Notification hook writes has_question (interrupt dialog)
        t[0] = 1.1
        write_signal(tracker, 'has_question')
        assert tracker.get_state(pty_alive=True) == 'has_question'
        # Simulate Escape keypress just before
        tracker._last_input_time = 0.9
        # PTY output with "Interrupted" arrives
        t[0] = 1.2
        tracker.on_output(b'Interrupted \xc2\xb7 What should Claude do instead?')
        assert tracker.current_state == 'interrupted'
