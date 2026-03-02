"""
Claude state tracking for ClaudeQ server.

Encapsulates the state machine that detects Claude's current state
(idle, running, needs_permission, has_question, interrupted) using
hook-based signal files with a PTY silence fallback.
"""

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import pyte

from claudeq.utils.constants import OUTPUT_SILENCE_TIMEOUT, STORAGE_DIR

_log = logging.getLogger('cq.state')


def _setup_debug_log() -> None:
    """Write state tracker debug messages to .storage/state_debug.log."""
    if _log.handlers:
        return
    log_path = STORAGE_DIR / 'state_debug.log'
    handler = logging.FileHandler(str(log_path), mode='w')
    handler.setFormatter(logging.Formatter(
        '%(asctime)s.%(msecs)03d %(message)s', datefmt='%H:%M:%S',
    ))
    _log.addHandler(handler)
    _log.setLevel(logging.DEBUG)


class ClaudeStateTracker:
    """Tracks Claude CLI state via hook-written signal files.

    The Claude Code hooks write a JSON signal file on state transitions
    (Stop, ToolInput, SubAgentInput).  This class reads that file to
    determine the current state, with a silence-timeout fallback for
    cases where hooks don't fire (e.g. user interrupts with Ctrl+C).

    Thread safety: ``_state`` and ``_waiting_since`` are protected by
    ``_lock``.  ``_output_buf`` and ``_last_prompt_buf`` are protected
    by ``_buf_lock`` for compound read+clear operations.
    ``_last_output_time`` is lock-free (single writer from the output
    filter; stale reads are acceptable for the silence timeout
    heuristic).
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
        self._buf_lock = threading.Lock()
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
        # True while the trust dialog is showing.  When the user answers
        # and Claude starts up, resume detection goes to 'idle' instead
        # of 'running' because there's no pending request to process.
        self._trust_dialog_phase: bool = False
        # Snapshot of _output_buf captured just before clearing on
        # needs_permission / has_question transitions.  Used by Slack
        # integration to show the actual prompt text + numbered options.
        self._last_prompt_buf: bytes = b''
        # Timestamp when on_send() set state to running.  Used to detect
        # user input *during* the running state (i.e. interrupt attempts).
        self._running_since: float = 0.0

        # Delete any stale signal file from a previous server (e.g. after
        # SIGKILL).  Since get_state() now reads the signal file even while
        # idle, a leftover needs_permission/has_question would cause a false
        # transition on the first poll.
        try:
            self._signal_file.unlink(missing_ok=True)
        except OSError:
            pass

        _setup_debug_log()
        _log.debug('INIT state=idle signal_file=%s', signal_file)

    # -- Public API ----------------------------------------------------------

    def get_state(self, pty_alive: bool) -> str:
        """Poll the signal file and return Claude's current state.

        Args:
            pty_alive: Whether the PTY child process is still running.

        Returns:
            One of: 'idle', 'running', 'needs_permission',
            'has_question', 'interrupted'.
        """
        if not pty_alive:
            with self._lock:
                self._state = 'idle'
                self._waiting_since = None
            return 'idle'

        with self._lock:
            current = self._state

        # Check signal file for hook-written state transitions.
        # Always read regardless of current state — a Notification hook
        # can write needs_permission/has_question while idle.
        new_state = self._read_signal_state()
        if new_state and new_state != current:
            # Stop hook fires on Escape too, writing "idle",
            # but Claude is actually prompting "What should
            # Claude do instead?" — keep has_question/interrupted.
            # Also guard running→idle when user pressed a key *during*
            # the running state (interrupt attempt): the Stop hook fires
            # before the PTY outputs "Interrupted", so delay accepting
            # idle to let on_output() detect interruption first.
            if (
                new_state == 'idle'
                and current == 'running'
                and self._last_input_time > self._running_since
                and (self._clock() - self._last_input_time) < 2.0
            ):
                _log.debug(
                    'GET_STATE signal=idle but protecting running '
                    '(user input %.1fs ago, waiting for PTY)',
                    self._clock() - self._last_input_time,
                )
            elif (
                new_state == 'idle'
                and current in ('has_question', 'interrupted')
                and self._waiting_since is not None
                and (self._clock() - self._waiting_since) < 5.0
            ):
                _log.debug(
                    'GET_STATE signal=idle but protecting %s '
                    '(%.1fs since wait)',
                    current, self._clock() - self._waiting_since,
                )
            # Notification hook fires for the interrupt dialog
            # ("What should Claude do instead?"), writing
            # has_question — protect interrupted from this.
            elif (
                new_state == 'has_question'
                and current == 'interrupted'
                and self._waiting_since is not None
                and (self._clock() - self._waiting_since) < 5.0
            ):
                _log.debug(
                    'GET_STATE signal=has_question but protecting '
                    'interrupted (%.1fs since wait)',
                    self._clock() - self._waiting_since,
                )
            else:
                _log.debug(
                    'GET_STATE signal transition %s→%s',
                    current, new_state,
                )
                with self._lock:
                    self._state = new_state
                    if new_state in ('needs_permission', 'has_question'):
                        self._waiting_since = self._clock()
                    else:
                        self._waiting_since = None
                self._idle_output_acc = 0
                with self._buf_lock:
                    if new_state in ('needs_permission', 'has_question'):
                        self._last_prompt_buf = bytes(self._output_buf)
                    else:
                        self._last_prompt_buf = b''
                    self._output_buf.clear()
                if new_state == 'idle':
                    self._idle_since = self._clock()
                    self._trust_dialog_phase = False
                return new_state

        # Fallback: PTY silence timeout (handles interruptions,
        # missing hooks, or any case where the hook doesn't fire)
        if current == 'running' and self._last_output_time > 0:
            silence = self._clock() - self._last_output_time
            if silence > OUTPUT_SILENCE_TIMEOUT:
                _log.debug(
                    'GET_STATE silence timeout %.1fs → idle', silence,
                )
                with self._lock:
                    self._state = 'idle'
                    self._waiting_since = None
                self._idle_output_acc = 0
                self._idle_since = self._clock()
                return 'idle'

        return current

    def is_ready(self, pty_alive: bool) -> bool:
        """Check if the auto-sender should send the next message.

        Polls ``get_state()`` and evaluates readiness based on the
        current auto-send mode.  For callers that already have a
        fresh state (e.g. the auto-sender loop), use
        ``is_ready_for_state()`` to avoid a redundant signal-file
        read that can race with the first one.

        Args:
            pty_alive: Whether the PTY child process is still running.

        Returns:
            True if the auto-sender should send the next queued message.
        """
        return self.is_ready_for_state(self.get_state(pty_alive))

    def is_ready_for_state(self, state: str) -> bool:
        """Check readiness given an already-computed state.

        In 'pause' mode: only send when Claude is idle.
        In 'always' mode: send whenever Claude is not running.
        Interrupted always blocks regardless of mode.

        Args:
            state: The current Claude state (from a prior
                ``get_state()`` call).

        Returns:
            True if the auto-sender should send the next queued message.
        """
        if state == 'interrupted':
            return False
        if self._auto_send_mode == 'always':
            return state != 'running'
        # 'pause' mode (default): only send when idle
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
            _log.debug('ON_INPUT filtered escape seq len=%d', len(data))
            return
        _log.debug(
            'ON_INPUT state=%s data=%r len=%d',
            self._state, data[:20], len(data),
        )
        self._seen_user_input = True
        self._last_input_time = self._clock()
        self._idle_output_acc = 0

    def on_send(self) -> None:
        """Called when a message is sent to Claude.

        Sets state to 'running' and deletes the stale signal file.
        """
        _log.debug('ON_SEND → running')
        self._seen_user_input = True
        self._running_since = self._clock()
        with self._lock:
            self._state = 'running'
            self._waiting_since = None
        with self._buf_lock:
            self._output_buf.clear()
            self._last_prompt_buf = b''
        self._idle_output_acc = 0

        try:
            self._signal_file.unlink(missing_ok=True)
        except OSError:
            pass

    def on_output(self, data: bytes) -> None:
        """Called when PTY output is received.

        Updates the last-output timestamp, detects state transitions:
        - idle → running: sustained printable output without recent input
        - running → interrupted: "Interrupted" text detected
        - needs_permission/has_question/interrupted → running: printable output resume

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
            # Buffer all idle output for pattern detection (trust dialog
            # at startup, Interrupted during Escape race).
            self._output_buf.extend(data)
            if len(self._output_buf) > 16384:
                self._output_buf = self._output_buf[-16384:]

            # Detect startup prompts (workspace trust dialog) from PTY
            # output.  These appear before Claude Code's hooks are active,
            # so no signal file is written.  Buffer + strip ANSI + remove
            # spaces: the TUI (Ink) uses cursor-positioning CSI sequences
            # for spacing, so stripping ANSI merges words together
            # (e.g. "I trust this folder" → "Itrustthisfolder").
            if not self._seen_user_input:
                compact = self._ANSI_RE.sub(
                    b'', bytes(self._output_buf),
                ).replace(b' ', b'')
                _log.debug(
                    'ON_OUTPUT idle (startup) len=%d buf_len=%d '
                    'compact=%r',
                    len(data), len(self._output_buf),
                    compact[-120:],
                )
                if b'Itrustthisfolder' in compact:
                    _log.debug(
                        'ON_OUTPUT idle→needs_permission (trust dialog)',
                    )
                    self._last_prompt_buf = bytes(self._output_buf)
                    self._output_buf.clear()
                    self._idle_output_acc = 0
                    self._trust_dialog_phase = True
                    with self._lock:
                        self._state = 'needs_permission'
                        self._waiting_since = self._clock()
                    return

            # Detect Escape interruption — the Stop hook may race ahead
            # and write "idle" before PTY output with "Interrupted" arrives.
            # Checked before _seen_user_input guard because the signal-file
            # idle transition may have reset _seen_user_input indirectly
            # (via _idle_since), but the Escape race still needs to work.
            if now - self._last_input_time < 3.0:
                # Strip ANSI before checking — raw PTY bytes may contain
                # "Interrupted" inside escape sequences (e.g. hyperlink OSC).
                stripped_chunk = self._ANSI_RE.sub(b'', data)
                has_interrupted = b'Interrupted' in stripped_chunk
                if has_interrupted or b'Interrupted' in self._ANSI_RE.sub(
                    b'', bytes(self._output_buf),
                ):
                    _log.debug(
                        'ON_OUTPUT idle→interrupted (Escape race detected)',
                    )
                    self._output_buf.clear()
                    self._idle_output_acc = 0
                    with self._lock:
                        self._state = 'interrupted'
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
                        _log.debug(
                            'ON_OUTPUT idle→running (accumulated %d bytes)',
                            self._idle_output_acc,
                        )
                        self._idle_output_acc = 0
                        self._output_buf.clear()
                        self._running_since = self._clock()
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
            # Strip ANSI before checking — raw PTY bytes may contain
            # "Interrupted" inside escape sequences (e.g. hyperlink OSC)
            # which would cause false positives.
            stripped_data = self._ANSI_RE.sub(b'', data)
            has_interrupted = b'Interrupted' in stripped_data
            self._output_buf.extend(data)
            if len(self._output_buf) > 8192:
                self._output_buf = self._output_buf[-8192:]
            if not has_interrupted:
                has_interrupted = b'Interrupted' in self._ANSI_RE.sub(
                    b'', bytes(self._output_buf),
                )
            # Log every chunk while running to diagnose detection failures
            stripped_preview = stripped_data.strip()
            if stripped_preview:
                _log.debug(
                    'ON_OUTPUT running chunk len=%d buf_len=%d '
                    'has_Interrupted=%s stripped=%r',
                    len(data), len(self._output_buf),
                    has_interrupted, stripped_preview[:80],
                )
            if has_interrupted and (now - self._last_input_time) < 3.0:
                _log.debug('ON_OUTPUT running→interrupted (Interrupted)')
                self._output_buf.clear()
                with self._lock:
                    self._state = 'interrupted'
                    self._waiting_since = self._clock()
            else:
                # Trust dialog phase: after user answered via select_option,
                # on_send() sets state to running.  Startup output means
                # Claude is booting — go straight to idle.
                if self._trust_dialog_phase:
                    stripped = self._ANSI_RE.sub(b'', data).strip()
                    if stripped:
                        _log.debug(
                            'ON_OUTPUT running→idle '
                            '(trust dialog startup)',
                        )
                        self._trust_dialog_phase = False
                        self._output_buf.clear()
                        with self._lock:
                            self._state = 'idle'
                            self._waiting_since = None
                        self._idle_since = self._clock()
                        self._idle_output_acc = 0
                        self._last_prompt_buf = b''
                        return

                # Detect permission/question dialogs from PTY output.
                # Claude Code's Notification hook fires ~15s late, but
                # the dialog content is in the PTY output immediately.
                # Check the compact (ANSI-stripped, space-removed) buffer
                # for "Enter to select" + "Esc to cancel" — both present
                # in all Ink option selector dialogs.
                compact = self._ANSI_RE.sub(
                    b'', bytes(self._output_buf),
                ).replace(b' ', b'')
                if b'Entertoselect' in compact and b'Esctocancel' in compact:
                    _log.debug(
                        'ON_OUTPUT running→needs_permission '
                        '(dialog detected from PTY output)',
                    )
                    self._last_prompt_buf = bytes(self._output_buf)
                    self._output_buf.clear()
                    self._idle_output_acc = 0
                    with self._lock:
                        self._state = 'needs_permission'
                        self._waiting_since = self._clock()

        # Detect resume after permission/question — new PTY output means
        # the user answered and Claude is processing again.  Delete the
        # stale signal file so get_state() won't read the old value.
        # Uses elif so interruption detection above isn't immediately undone.
        # Grace period: ignore the first 2s of output after entering the
        # waiting state (prompt text / escape sequences may still render).
        # Require user input AFTER entering the waiting state — prevents
        # TUI status bar rendering from falsely triggering resume.
        elif self._state in ('needs_permission', 'has_question', 'interrupted'):
            # Notification hook for the interrupt dialog may race ahead
            # and set has_question before PTY "Interrupted" output arrives.
            # Override to interrupted if we see it in fresh output shortly
            # after user input (Escape key).
            if (
                self._state == 'has_question'
                and b'Interrupted' in self._ANSI_RE.sub(b'', data)
                and (now - self._last_input_time) < 3.0
            ):
                _log.debug(
                    'ON_OUTPUT has_question→interrupted '
                    '(Interrupted in output, Escape race)',
                )
                with self._lock:
                    self._state = 'interrupted'
                    self._waiting_since = now
            self._output_buf.clear()
            # Continue accumulating prompt output for Slack rendering.
            # The dialog may still be rendering after the state transition.
            self._last_prompt_buf += data
            if len(self._last_prompt_buf) > 16384:
                self._last_prompt_buf = self._last_prompt_buf[-16384:]
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
                    # Trust dialog phase: startup output after user answered
                    # the trust prompt is Claude booting up, not processing
                    # a request.  Go straight to idle.
                    if self._trust_dialog_phase:
                        _log.debug(
                            'ON_OUTPUT %s→idle (trust dialog startup)',
                            self._state,
                        )
                        self._trust_dialog_phase = False
                        with self._lock:
                            self._state = 'idle'
                            self._waiting_since = None
                        self._idle_since = self._clock()
                        self._idle_output_acc = 0
                        self._last_prompt_buf = b''
                        return

                    _log.debug(
                        'ON_OUTPUT %s→running (resume, stripped=%r)',
                        self._state, stripped[:60],
                    )
                    self._running_since = self._clock()
                    with self._lock:
                        self._state = 'running'
                        self._waiting_since = None
                    self._last_prompt_buf = b''
                    try:
                        self._signal_file.unlink(missing_ok=True)
                    except OSError:
                        pass

    def get_prompt_output(self) -> str:
        """Return PTY output from the last permission/question prompt.

        Processes the raw PTY bytes through a minimal virtual terminal
        to properly handle Ink TUI cursor-positioning layout.
        """
        with self._buf_lock:
            buf = self._last_prompt_buf
        if not buf:
            return ''
        return self._render_screen(buf)

    @staticmethod
    def _render_screen(raw: bytes, rows: int = 50, cols: int = 200) -> str:
        """Render cursor-positioned PTY output into readable text.

        Uses the ``pyte`` terminal emulator library to process raw PTY
        bytes (ANSI/CSI escape sequences, cursor positioning, erases,
        etc.) into a virtual screen, then extracts readable lines.

        Args:
            raw: Raw PTY output bytes (may contain ANSI/CSI sequences).
            rows: Virtual screen height.
            cols: Virtual screen width.

        Returns:
            Cleaned multi-line text with proper spacing.
        """
        screen = pyte.Screen(cols, rows)
        stream = pyte.Stream(screen)
        text = raw.decode('utf-8', errors='replace')
        try:
            stream.feed(text)
            display = screen.display
        except (IndexError, ValueError, AssertionError):
            # pyte bugs: wcwidth(char[0]) crashes on empty strings in
            # the screen buffer, stream.feed() can raise on malformed
            # escape sequences, and screen.display has an assert that
            # can fire if character data is corrupted.
            return ''

        # Box-drawing characters used by Ink's TUI borders.
        _box_chars = set('─━│┃┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬═║')
        lines = [line.rstrip() for line in display]
        # Strip leading/trailing blank or purely decorative border lines.
        while lines and (
            not lines[0].strip()
            or all(c in _box_chars | {' '} for c in lines[0])
        ):
            lines.pop(0)
        while lines and (
            not lines[-1].strip()
            or all(c in _box_chars | {' '} for c in lines[-1])
        ):
            lines.pop()
        return '\n'.join(lines)

    def _read_signal_state(self) -> Optional[str]:
        """Read the state from the signal file.

        Returns:
            A valid signal state string, or None if the file is missing,
            unreadable, or contains an unknown state.
        """
        try:
            if not self._signal_file.exists():
                return None
            raw = self._signal_file.read_text().strip()
            data = json.loads(raw)
            state = data.get('state', '')
            if state in self._VALID_SIGNAL_STATES:
                return state
            return None
        except (json.JSONDecodeError, OSError):
            return None

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
