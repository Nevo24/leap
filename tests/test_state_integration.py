"""Integration tests for state tracking with a real PTY process.

These tests spawn a real bash process via pexpect (the same library
the server uses), wire its output to ClaudeStateTracker.on_output(),
and verify state transitions with real I/O and real timing.
"""

import json
import time
from pathlib import Path
from typing import Optional

import pexpect
import pytest

from claudeq.server.state_tracker import ClaudeStateTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class PTYFixture:
    """Wraps a pexpect-spawned bash process + wired state tracker."""

    def __init__(self, signal_file: Path) -> None:
        self.signal_file = signal_file
        self.tracker = ClaudeStateTracker(signal_file=signal_file)
        self.child = pexpect.spawn(
            '/bin/bash', ['--norc', '--noprofile'],
            dimensions=(24, 80),
            encoding=None,  # binary mode
        )
        # Wait for bash prompt to appear and drain it
        time.sleep(0.3)
        self._drain()

    def send_input(self, data: bytes) -> None:
        """Send raw bytes to the PTY (simulates keyboard input)."""
        self.tracker.on_input(data)
        self.child.send(data)

    def send_line(self, text: str) -> None:
        """Send a command to bash (but DON'T call on_input — this
        simulates programmatic send, not user typing)."""
        self.child.sendline(text)

    def drain_to_tracker(self, timeout: float = 0.5) -> bytes:
        """Read all available PTY output and feed it to the tracker.

        This replicates what _output_filter does in the real server:
        every chunk of PTY output goes through on_output().
        """
        collected = b''
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = self.child.read_nonblocking(4096, timeout=0.1)
                if data:
                    self.tracker.on_output(data)
                    collected += data
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                break
        return collected

    def write_signal(self, state: str) -> None:
        """Write a JSON signal file (simulates Claude Code hook)."""
        self.signal_file.write_text(json.dumps({"state": state}))

    def get_state(self) -> str:
        """Poll the tracker (simulates monitor/auto-sender polling)."""
        return self.tracker.get_state(pty_alive=self.child.isalive())

    def wait_for_state(
        self,
        expected: str,
        timeout: float = 5.0,
        poll_interval: float = 0.1,
    ) -> Optional[str]:
        """Poll until the tracker reports *expected*, or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.get_state()
            if state == expected:
                return state
            time.sleep(poll_interval)
        return self.get_state()

    def close(self) -> None:
        """Terminate the bash process."""
        if self.child.isalive():
            self.child.close(force=True)


@pytest.fixture
def pty(tmp_path: Path) -> PTYFixture:
    """Create a PTY fixture with a real bash process."""
    fixture = PTYFixture(signal_file=tmp_path / "test.signal")
    yield fixture
    fixture.close()


def _drain(self: PTYFixture) -> None:
    """Drain initial output (bash prompt)."""
    try:
        self.child.read_nonblocking(4096, timeout=0.3)
    except (pexpect.TIMEOUT, pexpect.EOF):
        pass

PTYFixture._drain = _drain


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPTYSignalFile:
    """Signal file transitions with real file I/O."""

    def test_on_send_then_signal_idle(self, pty: PTYFixture) -> None:
        """on_send → running, then signal file → idle."""
        assert pty.get_state() == 'idle'
        pty.tracker.on_send()
        assert pty.get_state() == 'running'
        pty.write_signal('idle')
        assert pty.get_state() == 'idle'

    def test_signal_needs_permission(self, pty: PTYFixture) -> None:
        pty.tracker.on_send()
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

    def test_signal_has_question(self, pty: PTYFixture) -> None:
        pty.tracker.on_send()
        pty.write_signal('has_question')
        assert pty.get_state() == 'has_question'


class TestPTYOutputAccumulation:
    """Output accumulation with real PTY output (ANSI codes, line
    endings, terminal rendering)."""

    def test_real_pty_output_triggers_running(self, pty: PTYFixture) -> None:
        """Bash output (with real ANSI/prompt noise) triggers running
        after user input + cooldown."""
        # Simulate user typing (sets _seen_user_input + _last_input_time)
        pty.send_input(b'x')
        time.sleep(0.1)
        pty.send_input(b'\n')

        # Wait past the 0.5s input cooldown
        time.sleep(0.6)

        # Generate substantial output through bash
        pty.send_line('printf "%0.sA" $(seq 1 300)')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.get_state() == 'running'

    def test_no_false_running_without_user_input(self, pty: PTYFixture) -> None:
        """PTY output alone (no user input) should not trigger running."""
        # Generate output without ever calling on_input
        pty.send_line('printf "%0.sB" $(seq 1 300)')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.get_state() == 'idle'


class TestPTYInterrupted:
    """'Interrupted' detection through real PTY output."""

    def test_interrupted_in_running_state(self, pty: PTYFixture) -> None:
        """'Interrupted' in PTY output while running → has_question."""
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        # Make bash output "Interrupted"
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.tracker.current_state == 'has_question'

    def test_interrupted_with_surrounding_ansi(self, pty: PTYFixture) -> None:
        """'Interrupted' detected even with ANSI codes around it."""
        pty.tracker.on_send()

        # Output with ANSI codes around "Interrupted" (like Claude TUI does)
        pty.send_line(r'printf "\033[31mInterrupted\033[0m\n"')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.tracker.current_state == 'has_question'


class TestPTYResumeDetection:
    """Resume detection: has_question/needs_permission → running."""

    def test_tui_rendering_after_interrupted_stays_has_question(
        self, pty: PTYFixture,
    ) -> None:
        """After 'Interrupted' → has_question, TUI status bar rendering
        should NOT falsely trigger resume to running."""
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        # "Interrupted" detected → has_question
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'has_question'

        # Wait past 2s grace period
        time.sleep(2.5)

        # TUI-like status bar output (printable text after ANSI stripping)
        pty.send_line(r'printf "\033[24;1H\033[KNevo.Mashiach 10%% Opus\n"')
        pty.drain_to_tracker(timeout=1.0)

        # Should stay has_question — no user input since entering wait
        assert pty.tracker.current_state == 'has_question'

    def test_resume_after_user_types(self, pty: PTYFixture) -> None:
        """After has_question, user typing then output → running."""
        pty.tracker.on_send()
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'has_question'

        # Wait past grace period
        time.sleep(2.5)

        # User types (answers the question)
        pty.tracker.on_input(b'y')

        # Claude produces output
        pty.send_line('printf "%0.sX" $(seq 1 100)')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.tracker.current_state == 'running'


class TestPTYFalseRunningRetrigger:
    """After Claude goes idle via signal, prompt/TUI rendering should
    not falsely re-trigger 'running'."""

    def test_output_after_signal_idle_stays_idle(self, pty: PTYFixture) -> None:
        """Output accumulation → running → signal idle → more output
        should NOT re-trigger running (input predates idle)."""
        # User types → output accumulation → running
        pty.send_input(b'x')
        pty.send_input(b'\n')
        time.sleep(0.6)
        pty.send_line('printf "%0.sA" $(seq 1 300)')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.get_state() == 'running'

        # Signal idle (Claude finished)
        pty.write_signal('idle')
        assert pty.get_state() == 'idle'

        # More output arrives (prompt rendering) — should stay idle
        pty.send_line('printf "%0.sB" $(seq 1 300)')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.get_state() == 'idle'

    def test_new_input_after_idle_allows_running(self, pty: PTYFixture) -> None:
        """After idle, fresh user input should allow running detection."""
        # running → idle cycle
        pty.send_input(b'x')
        pty.send_input(b'\n')
        time.sleep(0.6)
        pty.send_line('printf "%0.sA" $(seq 1 300)')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.get_state() == 'running'

        pty.write_signal('idle')
        assert pty.get_state() == 'idle'

        # User types again (AFTER idle)
        time.sleep(0.1)
        pty.send_input(b'y')
        pty.send_input(b'\n')
        time.sleep(0.6)

        # New output after fresh input → should trigger running
        pty.send_line('printf "%0.sC" $(seq 1 300)')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.get_state() == 'running'


class TestPTYEscapeRace:
    """Escape race: Stop hook writes idle before 'Interrupted' arrives."""

    def test_escape_race_detected(self, pty: PTYFixture) -> None:
        """Signal file says idle, then PTY outputs 'Interrupted' →
        should detect has_question via the Escape race path."""
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        # Stop hook fires first → idle
        pty.write_signal('idle')
        assert pty.get_state() == 'idle'

        # User pressed Escape (single byte)
        pty.tracker.on_input(b'\x1b')

        # PTY outputs "Interrupted" shortly after
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.tracker.current_state == 'has_question'

    def test_escape_after_false_idle(self, pty: PTYFixture) -> None:
        """The user's reported scenario: type → running → idle signal →
        press Escape → should get has_question if Interrupted appears."""
        # User types → running (via accumulation)
        pty.send_input(b'h')
        pty.send_input(b'\n')
        time.sleep(0.6)
        pty.send_line('printf "%0.sA" $(seq 1 300)')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.get_state() == 'running'

        # Signal idle
        pty.write_signal('idle')
        assert pty.get_state() == 'idle'

        # User presses Escape
        pty.tracker.on_input(b'\x1b')

        # PTY outputs "Interrupted"
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.tracker.current_state == 'has_question'
