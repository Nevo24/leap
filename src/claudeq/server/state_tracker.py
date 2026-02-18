"""
Claude state tracking for ClaudeQ server.

Encapsulates the state machine that detects Claude's current state
(idle, running, needs_permission, has_question) using hook-based
signal files with a PTY silence fallback.
"""

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from claudeq.utils.constants import OUTPUT_SILENCE_TIMEOUT


class ClaudeStateTracker:
    """Tracks Claude CLI state via hook-written signal files.

    The Claude Code hooks write a JSON signal file on state transitions
    (Stop, ToolInput, SubAgentInput).  This class reads that file to
    determine the current state, with a silence-timeout fallback for
    cases where hooks don't fire (e.g. user interrupts with Ctrl+C).

    Thread safety: ``_state`` and ``_waiting_since`` are protected by
    ``_lock``.  ``_last_output_time`` is lock-free (single writer from
    the output filter; stale reads are acceptable for the silence
    timeout heuristic).
    """

    _VALID_SIGNAL_STATES = frozenset({'idle', 'needs_permission', 'has_question'})
    # Matches ANSI escape sequences (CSI, OSC, charset, keypad, cursor
    # save/restore) and carriage returns — used to filter TUI refresh
    # noise from real printable output.
    _ANSI_RE: re.Pattern[bytes] = re.compile(
        rb'\x1b\[[0-9;?]*[A-Za-z]'
        rb'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'
        rb'|\x1b[()][0-9A-Za-z]'
        rb'|\x1b[=>]'
        rb'|\r'
    )

    def __init__(
        self,
        signal_file: Path,
        auto_send_mode: str = 'pause',
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._signal_file = signal_file
        self._auto_send_mode = auto_send_mode
        self._clock = clock or time.time

        self._state: str = 'idle'
        self._lock = threading.Lock()
        self._waiting_since: Optional[float] = None
        self._last_output_time: float = 0.0
        # Track user input to distinguish typing echo from Claude output.
        self._last_input_time: float = self._clock()
        # True after the first real user keystroke (prevents the startup
        # banner from falsely triggering idle → running).
        self._seen_user_input: bool = False
        # Timestamp of the last transition to idle (signal file or silence
        # timeout).  Output accumulation only triggers idle → running if
        # user input occurred *after* this time, preventing false re-triggers
        # from post-idle prompt/TUI rendering.
        self._idle_since: float = 0.0
        # Accumulated printable output bytes while idle (reset on input
        # and state transitions).  Used to detect idle → running.
        self._idle_output_acc: int = 0
        # Buffer recent PTY output so "Interrupted" can be detected even
        # when split across chunk boundaries by the TUI renderer.
        self._output_buf: bytearray = bytearray()

    # -- Public API ----------------------------------------------------------

    def get_state(self, pty_alive: bool) -> str:
        """Poll the signal file and return Claude's current state.

        Args:
            pty_alive: Whether the PTY child process is still running.

        Returns:
            One of: 'idle', 'running', 'needs_permission', 'has_question'.
        """
        if not pty_alive:
            with self._lock:
                self._state = 'idle'
                self._waiting_since = None
            return 'idle'

        with self._lock:
            current = self._state

        if current != 'idle':
            # Check signal file for hook-written state transitions
            try:
                if self._signal_file.exists():
                    raw = self._signal_file.read_text().strip()
                    data = json.loads(raw)
                    new_state = data.get('state', '')
                    # Stop hook fires on Escape too, writing "idle",
                    # but Claude is actually prompting "What should
                    # Claude do instead?" — keep has_question.
                    if (
                        new_state == 'idle'
                        and current == 'has_question'
                        and self._waiting_since is not None
                        and (self._clock() - self._waiting_since) < 5.0
                    ):
                        pass
                    elif new_state in self._VALID_SIGNAL_STATES and new_state != current:
                        with self._lock:
                            self._state = new_state
                            if new_state in ('needs_permission', 'has_question'):
                                self._waiting_since = self._clock()
                            else:
                                self._waiting_since = None
                        self._idle_output_acc = 0
                        self._output_buf.clear()
                        if new_state == 'idle':
                            self._idle_since = self._clock()
                        return new_state
            except (json.JSONDecodeError, OSError):
                pass

            # Fallback: PTY silence timeout (handles interruptions,
            # missing hooks, or any case where the hook doesn't fire)
            if current == 'running' and self._last_output_time > 0:
                silence = self._clock() - self._last_output_time
                if silence > OUTPUT_SILENCE_TIMEOUT:
                    with self._lock:
                        self._state = 'idle'
                        self._waiting_since = None
                    self._idle_output_acc = 0
                    self._idle_since = self._clock()
                    return 'idle'

        return current

    def is_ready(self, pty_alive: bool) -> bool:
        """Check if the auto-sender should send the next message.

        In 'pause' mode: only send when Claude is idle.
        In 'always' mode: send whenever Claude is not running.

        Args:
            pty_alive: Whether the PTY child process is still running.

        Returns:
            True if the auto-sender should send the next queued message.
        """
        state = self.get_state(pty_alive)
        if self._auto_send_mode == 'always':
            return state != 'running'
        # 'pause' mode (default)
        return state == 'idle'

    def on_input(self, data: bytes) -> None:
        """Called when the user types in the server terminal.

        Tracks input timing so output-based idle → running detection
        can distinguish user keystroke echo from Claude's processing
        output.  Ignores multi-byte escape sequences (terminal focus
        events, cursor position reports) that are not real user input.

        Args:
            data: Raw input bytes from the keyboard.
        """
        # Multi-byte sequences starting with ESC are terminal auto-responses
        # (focus in/out \x1b[I/O, cursor reports \x1b[row;colR, etc.),
        # not user keystrokes.  Single ESC byte is the actual Escape key.
        if len(data) > 1 and data[0] == 0x1b:
            return
        self._seen_user_input = True
        self._last_input_time = self._clock()
        self._idle_output_acc = 0

    def on_send(self) -> None:
        """Called when a message is sent to Claude.

        Sets state to 'running' and deletes the stale signal file.
        """
        self._seen_user_input = True
        with self._lock:
            self._state = 'running'
            self._waiting_since = None
        self._output_buf.clear()
        self._idle_output_acc = 0

        try:
            self._signal_file.unlink(missing_ok=True)
        except OSError:
            pass

    def on_output(self, data: bytes) -> None:
        """Called when PTY output is received.

        Updates the last-output timestamp, detects state transitions:
        - idle → running: sustained printable output without recent input
        - running → has_question: "Interrupted" text detected
        - needs_permission/has_question → running: printable output resume

        Args:
            data: Raw output bytes from the PTY.
        """
        now = self._clock()
        prev_output_time = self._last_output_time
        self._last_output_time = now

        # Detect Claude starting to process when user types directly in
        # the server terminal (on_send not called).  Accumulate printable
        # output bytes and transition to 'running' once a threshold is
        # reached.  Reset the accumulator on long output gaps (prevents
        # slow TUI noise from building up) and on user input (via
        # on_input).
        if self._state == 'idle':
            # Detect Escape interruption — the Stop hook may race ahead
            # and write "idle" before PTY output with "Interrupted" arrives.
            # Buffer output within 3s of user input to catch this.
            # Checked before _seen_user_input guard because the signal-file
            # idle transition may have reset _seen_user_input indirectly
            # (via _idle_since), but the Escape race still needs to work.
            if now - self._last_input_time < 3.0:
                self._output_buf.extend(data)
                if len(self._output_buf) > 512:
                    self._output_buf = self._output_buf[-512:]
                if b'Interrupted' in self._output_buf:
                    self._output_buf.clear()
                    self._idle_output_acc = 0
                    with self._lock:
                        self._state = 'has_question'
                        self._waiting_since = self._clock()
                    return
            if not self._seen_user_input:
                return
            # Only accumulate output if user typed AFTER the last idle
            # transition.  Prevents post-idle prompt/TUI rendering from
            # falsely re-triggering running.
            if self._last_input_time < self._idle_since:
                return
            stripped = self._ANSI_RE.sub(b'', data).strip()
            if stripped:
                if now - prev_output_time > 2.0:
                    self._idle_output_acc = 0
                if now - self._last_input_time > 0.5:
                    self._idle_output_acc += len(stripped)
                    if self._idle_output_acc > 200:
                        self._idle_output_acc = 0
                        self._output_buf.clear()
                        with self._lock:
                            self._state = 'running'
                            self._waiting_since = None
                        try:
                            self._signal_file.unlink(missing_ok=True)
                        except OSError:
                            pass
            return

        # Detect Claude interruption — Stop hook doesn't fire on Ctrl+C/Escape,
        # so detect it from PTY output and treat as waiting for user input.
        # Buffer recent output so the pattern is found even when the TUI
        # renderer splits "Interrupted" across chunk boundaries.
        if self._state == 'running':
            self._output_buf.extend(data)
            if len(self._output_buf) > 512:
                self._output_buf = self._output_buf[-512:]
            if b'Interrupted' in self._output_buf:
                self._output_buf.clear()
                with self._lock:
                    self._state = 'has_question'
                    self._waiting_since = self._clock()

        # Detect resume after permission/question — new PTY output means
        # the user answered and Claude is processing again.  Delete the
        # stale signal file so get_state() won't read the old value.
        # Uses elif so interruption detection above isn't immediately undone.
        # Grace period: ignore the first 2s of output after entering the
        # waiting state (prompt text / escape sequences may still render).
        # Require user input AFTER entering the waiting state — prevents
        # TUI status bar rendering from falsely triggering resume.
        elif self._state in ('needs_permission', 'has_question'):
            self._output_buf.clear()
            ws = self._waiting_since
            if (
                ws is not None
                and (self._clock() - ws) > 2.0
                and self._last_input_time > ws
            ):
                # Only treat as resume if the output has printable text
                # beyond ANSI escape sequences — filters TUI cursor blinks
                # and screen refreshes that arrive while Claude is idle.
                stripped = self._ANSI_RE.sub(b'', data).strip()
                if stripped:
                    with self._lock:
                        self._state = 'running'
                        self._waiting_since = None
                    try:
                        self._signal_file.unlink(missing_ok=True)
                    except OSError:
                        pass

    @property
    def current_state(self) -> str:
        """Read the cached state without polling the signal file."""
        return self._state

    @property
    def auto_send_mode(self) -> str:
        """Current auto-send mode ('pause' or 'always')."""
        return self._auto_send_mode

    @auto_send_mode.setter
    def auto_send_mode(self, mode: str) -> None:
        self._auto_send_mode = mode

    def cleanup(self) -> None:
        """Delete the signal file."""
        try:
            self._signal_file.unlink(missing_ok=True)
        except OSError:
            pass
