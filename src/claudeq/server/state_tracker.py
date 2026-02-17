"""
Claude state tracking for ClaudeQ server.

Encapsulates the state machine that detects Claude's current state
(idle, running, needs_permission, has_question) using hook-based
signal files with a PTY silence fallback.
"""

import json
import threading
import time
from pathlib import Path
from typing import Optional

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

    def __init__(self, signal_file: Path, auto_send_mode: str = 'pause') -> None:
        self._signal_file = signal_file
        self._auto_send_mode = auto_send_mode

        self._state: str = 'idle'
        self._lock = threading.Lock()
        self._waiting_since: Optional[float] = None
        self._last_output_time: float = 0.0

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
                    if new_state in self._VALID_SIGNAL_STATES and new_state != current:
                        with self._lock:
                            self._state = new_state
                            if new_state in ('needs_permission', 'has_question'):
                                self._waiting_since = time.time()
                            else:
                                self._waiting_since = None
                        return new_state
            except (json.JSONDecodeError, OSError):
                pass

            # Fallback: PTY silence timeout (handles interruptions,
            # missing hooks, or any case where the hook doesn't fire)
            if current == 'running' and self._last_output_time > 0:
                silence = time.time() - self._last_output_time
                if silence > OUTPUT_SILENCE_TIMEOUT:
                    with self._lock:
                        self._state = 'idle'
                        self._waiting_since = None
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

    def on_send(self) -> None:
        """Called when a message is sent to Claude.

        Sets state to 'running' and deletes the stale signal file.
        """
        with self._lock:
            self._state = 'running'
            self._waiting_since = None

        try:
            self._signal_file.unlink(missing_ok=True)
        except OSError:
            pass

    def on_output(self, data: bytes) -> None:
        """Called when PTY output is received.

        Updates the last-output timestamp, detects interruption from
        PTY output (Ctrl+C/Escape), and detects resume after a
        permission/question prompt (with a 2s grace period).

        Args:
            data: Raw output bytes from the PTY.
        """
        self._last_output_time = time.time()

        # Detect Claude interruption — Stop hook doesn't fire on Ctrl+C/Escape,
        # so detect it from PTY output and treat as waiting for user input.
        if self._state == 'running' and b'Interrupted' in data:
            with self._lock:
                self._state = 'has_question'
                self._waiting_since = time.time()

        # Detect resume after permission/question — new PTY output means
        # the user answered and Claude is processing again.  Delete the
        # stale signal file so get_state() won't read the old value.
        # Uses elif so interruption detection above isn't immediately undone.
        # Grace period: ignore the first 2s of output after entering the
        # waiting state (prompt text / escape sequences may still render).
        elif self._state in ('needs_permission', 'has_question'):
            ws = self._waiting_since
            if ws is not None and (time.time() - ws) > 2.0:
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
