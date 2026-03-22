"""
CLI state tracking for Leap server.

Encapsulates the state machine that detects the CLI's current state
(idle, running, needs_permission, needs_input, interrupted) using
hook-based signal files with a PTY silence fallback.

Supports multiple CLI backends (Claude, Codex, etc.) via the
CLIProvider abstraction.
"""

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import pyte

from leap.cli_providers.base import CLIProvider
from leap.cli_providers.registry import get_provider
from leap.cli_providers.states import AutoSendMode, CLIState, PROMPT_STATES, WAITING_STATES
from leap.utils.constants import (
    AUTO_RESUME_GRACE,
    ESCAPE_CORRECTION_WINDOW,
    IDLE_OUTPUT_THRESHOLD,
    IDLE_SIGNAL_DEBOUNCE,
    INPUT_COOLDOWN,
    INTERRUPT_DETECT_WINDOW,
    OUTPUT_GAP_RESET,
    OUTPUT_SILENCE_TIMEOUT,
    RESUME_GRACE_PERIOD,
    STATE_PROTECTION_WINDOW,
    STORAGE_DIR,
    WAITING_STATE_TIMEOUT,
)

_log = logging.getLogger('leap.state')


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


class CLIStateTracker:
    """Tracks CLI state via hook-written signal files.

    The CLI hooks write a JSON signal file on state transitions
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
        auto_send_mode: AutoSendMode = AutoSendMode.PAUSE,
        clock: Optional[Callable[[], float]] = None,
        provider: Optional[CLIProvider] = None,
    ) -> None:
        self._signal_file = signal_file
        self._auto_send_mode = auto_send_mode
        self._clock = clock or time.time
        self._provider = provider or get_provider()

        self._state: str = CLIState.IDLE
        self._lock = threading.Lock()
        self._buf_lock = threading.Lock()
        self._waiting_since: Optional[float] = None
        self._last_output_time: float = 0.0
        # Track user input to distinguish typing echo from CLI output.
        self._last_input_time: float = self._clock()
        # Track when the user last attempted an interrupt — Escape (0x1b),
        # Ctrl+C (0x03), or multi-byte escape sequences while running.
        # Used by the interrupted-pattern detector in on_output() to avoid
        # false positives from normal typing that triggers TUI redraws
        # containing "Interrupted" in the AI's conversational text.
        self._last_escape_time: float = -INTERRUPT_DETECT_WINDOW
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
        # Buffer recent PTY output so patterns can be detected even
        # when split across chunk boundaries by the TUI renderer.
        self._output_buf: bytearray = bytearray()
        # True while the trust dialog is showing.  When the user answers
        # and the CLI starts up, resume detection goes to 'idle' instead
        # of 'running' because there's no pending request to process.
        self._trust_dialog_phase: bool = False
        # Snapshot of _output_buf captured just before clearing on
        # needs_permission / needs_input transitions.  Used by Slack
        # integration to show the actual prompt text + numbered options.
        self._last_prompt_buf: bytes = b''
        # Timestamp when on_send() set state to running.  Used to detect
        # user input *during* the running state (i.e. interrupt attempts).
        self._running_since: float = 0.0
        # Debounce timer for running→idle signal transitions.  When the
        # Stop hook writes "idle", we delay accepting it so on_output()
        # has time to detect the "Interrupted ·" pattern while still in
        # the RUNNING state.  This single mechanism handles all interrupt
        # types (user Escape, Ctrl+C, self-interrupt, split chunks)
        # without needing per-case timing guards.
        self._idle_debounce_at: float = 0.0
        # Suppress the confirmed_interrupt_pattern check after leaving
        # the interrupted state.  The Ink TUI keeps the old "Interrupted ·"
        # prompt visible in scrollback; every TUI redraw re-renders it
        # into _output_buf, causing an infinite interrupted→running→
        # interrupted cycle.  Cleared when the pattern disappears from
        # the buffer (old text scrolled out of view).
        self._suppress_stale_interrupt: bool = False

        # Delete any stale signal file from a previous server (e.g. after
        # SIGKILL).  Since get_state() now reads the signal file even while
        # idle, a leftover needs_permission/needs_input would cause a false
        # transition on the first poll.
        try:
            self._signal_file.unlink(missing_ok=True)
        except OSError:
            pass

        _setup_debug_log()
        _log.debug(
            'INIT state=idle signal_file=%s provider=%s',
            signal_file, self._provider.name,
        )

    def _write_interrupted_signal(self) -> None:
        """Write 'interrupted' state to the signal file.

        The hook script never writes 'interrupted' (only idle /
        needs_permission / needs_input), so the WAITING_STATE_TIMEOUT
        fallback would find no confirmation and reset to idle.  By
        writing the signal file ourselves, the timeout check
        (signal_state == current) finds the confirmation and keeps the
        interrupted state.
        """
        try:
            self._signal_file.write_text(
                json.dumps({'state': CLIState.INTERRUPTED}),
            )
        except OSError:
            pass

    # -- Public API ----------------------------------------------------------

    @property
    def provider(self) -> CLIProvider:
        """The CLI provider used for pattern matching."""
        return self._provider

    def get_state(self, pty_alive: bool) -> str:
        """Poll the signal file and return the CLI's current state.

        Args:
            pty_alive: Whether the PTY child process is still running.

        Returns:
            One of: 'idle', 'running', 'needs_permission',
            'needs_input', 'interrupted'.
        """
        if not pty_alive:
            with self._lock:
                self._state = CLIState.IDLE
                self._waiting_since = None
            return CLIState.IDLE

        with self._lock:
            current = self._state

        # Check signal file for hook-written state transitions.
        # Always read regardless of current state — a Notification hook
        # can write needs_permission/needs_input while idle.
        new_state = self._read_signal_state()
        if new_state and new_state != current:
            # "interrupted" in the signal file is written by the state
            # tracker itself (not by hooks) purely so the
            # WAITING_STATE_TIMEOUT check can find confirmation.
            # It should never trigger a *new* transition — interrupted
            # is always detected from PTY output patterns.
            if new_state == CLIState.INTERRUPTED:
                _log.debug(
                    'GET_STATE signal=interrupted but current=%s — '
                    'ignoring stale signal (deleting)',
                    current,
                )
                try:
                    self._signal_file.unlink(missing_ok=True)
                except OSError:
                    pass
            # Debounce running→idle: the Stop hook fires before the
            # PTY finishes rendering "Interrupted ·".  Instead of
            # per-case guards for each interrupt type, delay ALL
            # running→idle signal transitions so on_output() has time
            # to detect interrupt patterns while still in RUNNING.
            if (
                new_state == CLIState.IDLE
                and current == CLIState.RUNNING
            ):
                if self._idle_debounce_at == 0:
                    self._idle_debounce_at = self._clock()
                if (self._clock() - self._idle_debounce_at) < IDLE_SIGNAL_DEBOUNCE:
                    _log.debug(
                        'GET_STATE signal=idle, debouncing running→idle '
                        '(%.0fms elapsed)',
                        (self._clock() - self._idle_debounce_at) * 1000,
                    )
                else:
                    # Debounce expired — accept the transition below.
                    self._idle_debounce_at = 0
            if (
                new_state == CLIState.IDLE
                and current == CLIState.RUNNING
                and self._idle_debounce_at != 0
            ):
                pass  # debounce in progress
            elif (
                new_state == CLIState.IDLE
                and current in (CLIState.NEEDS_INPUT, CLIState.INTERRUPTED)
                and self._waiting_since is not None
                and (self._clock() - self._waiting_since) < STATE_PROTECTION_WINDOW
            ):
                _log.debug(
                    'GET_STATE signal=idle but protecting %s '
                    '(%.1fs since wait)',
                    current, self._clock() - self._waiting_since,
                )
            # Notification hook fires for the interrupt dialog,
            # writing needs_input — protect interrupted from this.
            elif (
                new_state == CLIState.NEEDS_INPUT
                and current == CLIState.INTERRUPTED
                and self._waiting_since is not None
                and (self._clock() - self._waiting_since) < STATE_PROTECTION_WINDOW
            ):
                _log.debug(
                    'GET_STATE signal=needs_input but protecting '
                    'interrupted (%.1fs since wait)',
                    self._clock() - self._waiting_since,
                )
            else:
                # Check if user answered the prompt (typed after entering
                # the waiting state).
                user_answered = (
                    current in (CLIState.INTERRUPTED, CLIState.NEEDS_INPUT)
                    and new_state in (CLIState.IDLE, CLIState.NEEDS_INPUT)
                    and self._waiting_since is not None
                    and self._last_input_time > self._waiting_since
                )
                old_waiting_since = self._waiting_since

                if current == CLIState.INTERRUPTED:
                    self._suppress_stale_interrupt = True
                _log.debug(
                    'GET_STATE signal transition %s→%s%s',
                    current, new_state,
                    ' (user_answered, preserving timing)' if user_answered else '',
                )
                with self._lock:
                    self._state = new_state
                    if new_state in PROMPT_STATES:
                        if user_answered:
                            pass  # preserve existing _waiting_since
                        else:
                            self._waiting_since = self._clock()
                    else:
                        self._waiting_since = None
                self._idle_output_acc = 0
                with self._buf_lock:
                    if new_state in PROMPT_STATES:
                        self._last_prompt_buf = bytes(self._output_buf)
                    else:
                        self._last_prompt_buf = b''
                    self._output_buf.clear()
                if new_state == CLIState.IDLE:
                    if user_answered and old_waiting_since is not None:
                        self._idle_since = old_waiting_since
                    else:
                        self._idle_since = self._clock()
                    self._trust_dialog_phase = False
                return new_state

        # Fast path: transcript-based idle detection.
        # For CLIs with accessible transcripts (e.g. Codex), check for
        # task_complete events instead of waiting for the silence timeout.
        # This detects idle in ~0.5s instead of 8s.
        # Note: with concurrent Codex sessions, this may read the wrong
        # session's transcript.  The silence timeout is the safe fallback.
        if current == CLIState.RUNNING and self._provider.transcript_sessions_dir:
            # Pass _running_since so only task_complete entries AFTER we
            # entered RUNNING are accepted.  This prevents false idle from
            # stale completions when the transcript is updated incrementally
            # (user message written before the new task_complete).
            msg = self._provider.read_transcript_completion(
                since=self._running_since,
            )
            if msg is not None:
                _log.debug(
                    'GET_STATE transcript task_complete → idle '
                    '(msg=%r)',
                    msg[:60] if msg else '',
                )
                # Write the response to the signal file so
                # output_capture can read it without the fallback.
                try:
                    signal_data = {'state': CLIState.IDLE}
                    if msg:
                        signal_data['last_assistant_message'] = msg
                    self._signal_file.write_text(
                        json.dumps(signal_data),
                    )
                except OSError:
                    pass
                with self._lock:
                    self._state = CLIState.IDLE
                    self._waiting_since = None
                self._idle_output_acc = 0
                self._idle_since = self._clock()
                return CLIState.IDLE

        # Fallback: PTY silence timeout (handles interruptions,
        # missing hooks, or any case where the hook doesn't fire)
        silence_timeout = (
            self._provider.silence_timeout
            if self._provider.silence_timeout is not None
            else OUTPUT_SILENCE_TIMEOUT
        )
        if current == CLIState.RUNNING and self._last_output_time > 0:
            silence = self._clock() - self._last_output_time
            if silence > silence_timeout:
                _log.debug(
                    'GET_STATE silence timeout %.1fs → idle', silence,
                )
                with self._lock:
                    self._state = CLIState.IDLE
                    self._waiting_since = None
                self._idle_output_acc = 0
                self._idle_since = self._clock()
                with self._buf_lock:
                    self._output_buf.clear()
                return CLIState.IDLE

        # Fallback: waiting states (interrupted, needs_permission,
        # needs_input) can get stuck if the hook doesn't fire again
        # and no output arrives to trigger resume detection.
        # After WAITING_STATE_TIMEOUT with no PTY output, fall back
        # to idle — but only if the signal file doesn't still confirm
        # the current waiting state.  Without this check, the timeout
        # fires every poll cycle after 30s of silence (since
        # _last_output_time is never reset), the signal file
        # immediately reasserts the waiting state on the next poll,
        # and the state rapidly toggles between idle and waiting.
        if current in WAITING_STATES and self._last_output_time > 0:
            silence = self._clock() - self._last_output_time
            if silence > WAITING_STATE_TIMEOUT:
                # Trust the signal file over the silence heuristic:
                # if the hook wrote the current state, the CLI really
                # is waiting — don't override it.
                signal_state = self._read_signal_state()
                if signal_state == current:
                    _log.debug(
                        'GET_STATE waiting timeout %s %.1fs but '
                        'signal file confirms — keeping',
                        current, silence,
                    )
                else:
                    _log.debug(
                        'GET_STATE waiting timeout %s %.1fs → idle',
                        current, silence,
                    )
                    with self._lock:
                        self._state = CLIState.IDLE
                        self._waiting_since = None
                    self._idle_output_acc = 0
                    self._idle_since = self._clock()
                    with self._buf_lock:
                        self._output_buf.clear()
                        self._last_prompt_buf = b''
                    return CLIState.IDLE

        return current

    def is_ready(self, pty_alive: bool) -> bool:
        """Check if the auto-sender should send the next message.

        Args:
            pty_alive: Whether the PTY child process is still running.

        Returns:
            True if the auto-sender should send the next queued message.
        """
        return self.is_ready_for_state(self.get_state(pty_alive))

    def is_ready_for_state(self, state: str) -> bool:
        """Check readiness given an already-computed state.

        In 'pause' mode: only send when CLI is idle.
        In 'always' mode: send whenever CLI is not running.
        Interrupted always blocks regardless of mode.

        Args:
            state: The current CLI state (from a prior
                ``get_state()`` call).

        Returns:
            True if the auto-sender should send the next queued message.
        """
        if state == CLIState.INTERRUPTED:
            return False
        if self._auto_send_mode == AutoSendMode.ALWAYS:
            return state != CLIState.RUNNING
        # 'pause' mode (default): only send when idle
        return state == CLIState.IDLE

    def on_input(self, data: bytes) -> None:
        """Called when the user types in the server terminal.

        Tracks input timing so output-based idle → running detection
        can distinguish user keystroke echo from CLI processing output.
        Ignores multi-byte escape sequences (terminal focus events,
        cursor position reports) that are not real user input.

        However, when the CLI is running, multi-byte escape sequences
        starting with ``\\x1b`` still update ``_last_input_time`` because
        ``os.read()`` may bundle a real Escape keypress with a subsequent
        terminal event (focus report, cursor position) into one chunk.
        Without this, the interrupt-detection time window never opens.

        For Ratatui-based CLIs (enter_triggers_running=True), pressing
        Enter while idle transitions directly to running, since
        output-based detection is unreliable for full-screen TUIs.

        Args:
            data: Raw input bytes from the keyboard.
        """
        if len(data) > 1 and data[0] == 0x1b:
            if self._state == CLIState.RUNNING:
                # Only update _last_input_time for potential Escape
                # keypresses, not terminal-generated CSI sequences
                # (focus in/out, cursor position reports, mouse events).
                # CSI sequences start with \x1b[ and are never real
                # Escape key presses.  A bundled Escape + CSI event
                # starts with \x1b\x1b[ (double escape), which this
                # check correctly lets through.
                if len(data) >= 2 and data[1] == 0x5b:  # \x1b[
                    _log.debug(
                        'ON_INPUT filtered CSI seq len=%d in running '
                        '(not updating _last_input_time)',
                        len(data),
                    )
                else:
                    _log.debug(
                        'ON_INPUT filtered escape seq len=%d '
                        '(updating _last_input_time for interrupt detection)',
                        len(data),
                    )
                    self._last_input_time = self._clock()
                    self._last_escape_time = self._last_input_time
            else:
                _log.debug('ON_INPUT filtered escape seq len=%d', len(data))
            return
        _log.debug(
            'ON_INPUT state=%s data=%r len=%d',
            self._state, data[:20], len(data),
        )
        self._seen_user_input = True
        self._last_input_time = self._clock()
        # Track interrupt attempts for the interrupted-pattern detector.
        # Escape (0x1b) and Ctrl+C (0x03) both interrupt the CLI and
        # produce the "Interrupted" PTY output that on_output() looks for.
        if data in (b'\x1b', b'\x03'):
            self._last_escape_time = self._last_input_time
        self._idle_output_acc = 0

        # For Ratatui-based CLIs: Enter while idle → running.
        # Output-based detection is disabled because full-screen TUI
        # redraws are indistinguishable from real processing output.
        if (
            data == b'\r'
            and self._state == CLIState.IDLE
            and self._provider.enter_triggers_running
        ):
            _log.debug('ON_INPUT Enter in idle → running (Ratatui)')
            self._running_since = self._clock()
            self._idle_debounce_at = 0
            with self._lock:
                self._state = CLIState.RUNNING
                self._waiting_since = None
            with self._buf_lock:
                self._output_buf.clear()
                self._last_prompt_buf = b''
            try:
                self._signal_file.unlink(missing_ok=True)
            except OSError:
                pass

    def on_send(self) -> None:
        """Called when a message is sent to the CLI.

        Sets state to 'running' and deletes the stale signal file.
        """
        if self._state == CLIState.INTERRUPTED:
            self._suppress_stale_interrupt = True
        _log.debug('ON_SEND → running')
        self._seen_user_input = True
        self._running_since = self._clock()
        self._idle_debounce_at = 0
        with self._lock:
            self._state = CLIState.RUNNING
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

        Updates the last-output timestamp and dispatches to a
        state-specific handler for transition detection.

        Args:
            data: Raw output bytes from the PTY.
        """
        now = self._clock()
        prev_output_time = self._last_output_time
        self._last_output_time = now

        if self._state == CLIState.IDLE:
            self._handle_idle_output(data, now, prev_output_time)
        elif self._state == CLIState.RUNNING:
            self._handle_running_output(data, now)
        elif self._state in WAITING_STATES:
            self._handle_waiting_output(data, now)

    # -- on_output sub-handlers -----------------------------------------------

    def _handle_idle_output(
        self, data: bytes, now: float, prev_output_time: float,
    ) -> None:
        """Handle output while idle: startup dialogs, escape race, idle→running."""
        interrupted_pattern = self._provider.interrupted_pattern
        trust_patterns = self._provider.trust_dialog_patterns
        dialog_patterns = self._provider.dialog_patterns

        self._output_buf.extend(data)
        if len(self._output_buf) > 16384:
            self._output_buf = self._output_buf[-16384:]

        # Detect startup prompts from PTY output.
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
            is_trust = any(p in compact for p in trust_patterns)
            is_dialog = (
                bool(dialog_patterns)
                and all(p in compact for p in dialog_patterns)
            )
            if is_trust or is_dialog:
                _log.debug(
                    'ON_OUTPUT idle→needs_permission '
                    '(startup dialog: trust=%s dialog=%s)',
                    is_trust, is_dialog,
                )
                self._last_prompt_buf = bytes(self._output_buf)
                self._output_buf.clear()
                self._idle_output_acc = 0
                if is_trust:
                    self._trust_dialog_phase = True
                with self._lock:
                    self._state = CLIState.NEEDS_PERMISSION
                    self._waiting_since = self._clock()
                return

        # Detect interruption — the Stop hook may race ahead
        # and write "idle" before PTY output with the interrupted
        # pattern arrives.
        # Use _last_escape_time (not _last_input_time) so that normal
        # typing (which triggers TUI redraws of the visible screen)
        # doesn't false-positive on "Interrupted" in the AI's text.
        if now - self._last_escape_time < INTERRUPT_DETECT_WINDOW:
            stripped_chunk = self._ANSI_RE.sub(b'', data)
            has_interrupted = interrupted_pattern in stripped_chunk
            if has_interrupted or interrupted_pattern in self._ANSI_RE.sub(
                b'', bytes(self._output_buf),
            ):
                _log.debug(
                    'ON_OUTPUT idle→interrupted (Escape race detected)',
                )
                self._output_buf.clear()
                self._idle_output_acc = 0
                with self._lock:
                    self._state = CLIState.INTERRUPTED
                    self._waiting_since = self._clock()
                self._write_interrupted_signal()
                return

        # Confirmed interrupt pattern fallback — catches interrupts
        # that reached idle (e.g. Stop hook raced ahead of PTY output,
        # Ctrl+C bypassed on_input, or silence timeout expired).
        # Check the accumulated buffer so split-chunk patterns (where
        # "Interrupted" and "·" arrive in separate TUI render frames)
        # are detected.  The buffer is cleared on every state transition
        # (signal and timeout paths), so it only contains output since
        # entering idle.
        confirmed = self._provider.confirmed_interrupt_pattern
        if self._suppress_stale_interrupt:
            # Clear suppression once "Interrupted" scrolls out of buffer.
            if interrupted_pattern not in self._output_buf:
                self._suppress_stale_interrupt = False
        if confirmed and self._seen_user_input:
            if self._suppress_stale_interrupt:
                pass  # skip confirmed check — stale scrollback
            else:
                compact_buf = self._ANSI_RE.sub(
                    b'', bytes(self._output_buf),
                ).replace(b' ', b'')
                if confirmed in compact_buf:
                    _log.debug(
                        'ON_OUTPUT idle→interrupted '
                        '(confirmed interrupt pattern in buffer)',
                    )
                    self._output_buf.clear()
                    self._idle_output_acc = 0
                    with self._lock:
                        self._state = CLIState.INTERRUPTED
                        self._waiting_since = self._clock()
                    self._write_interrupted_signal()
                    return

        if not self._seen_user_input:
            return
        # For Ratatui-based CLIs, skip output-based idle→running.
        # Full-screen TUI redraws produce hundreds of bytes after ANSI
        # stripping (box-drawing, spinners, status bar) that are
        # indistinguishable from real processing output.
        if not self._provider.output_triggers_running:
            return
        auto_resume = self._last_input_time < self._idle_since
        if auto_resume:
            # No user input since entering idle.  Output might be
            # post-idle TUI rendering (prompt redraw, status bar) or
            # the CLI auto-starting a new turn (e.g. background
            # command results).  Allow idle→running after a grace
            # period so TUI rendering settles before we start
            # accumulating.
            if now - self._idle_since < AUTO_RESUME_GRACE:
                return
        stripped = self._ANSI_RE.sub(b'', data).strip()
        if stripped:
            if now - prev_output_time > OUTPUT_GAP_RESET:
                self._idle_output_acc = 0
            if now - self._last_input_time > INPUT_COOLDOWN:
                self._idle_output_acc += len(stripped)
                if self._idle_output_acc > IDLE_OUTPUT_THRESHOLD:
                    _log.debug(
                        'ON_OUTPUT idle→running (accumulated %d bytes%s)',
                        self._idle_output_acc,
                        ', auto-resume' if auto_resume else '',
                    )
                    self._idle_output_acc = 0
                    self._output_buf.clear()
                    self._running_since = self._clock()
                    self._idle_debounce_at = 0
                    with self._lock:
                        self._state = CLIState.RUNNING
                        self._waiting_since = None
                    try:
                        self._signal_file.unlink(missing_ok=True)
                    except OSError:
                        pass

    def _handle_running_output(self, data: bytes, now: float) -> None:
        """Handle output while running: interruption, trust dialog."""
        interrupted_pattern = self._provider.interrupted_pattern

        stripped_data = self._ANSI_RE.sub(b'', data)
        has_interrupted = interrupted_pattern in stripped_data
        self._output_buf.extend(data)
        if len(self._output_buf) > 8192:
            self._output_buf = self._output_buf[-8192:]
        if not has_interrupted:
            has_interrupted = interrupted_pattern in self._ANSI_RE.sub(
                b'', bytes(self._output_buf),
            )
        stripped_preview = stripped_data.strip()
        if stripped_preview:
            _log.debug(
                'ON_OUTPUT running chunk len=%d buf_len=%d '
                'has_Interrupted=%s stripped=%r',
                len(data), len(self._output_buf),
                has_interrupted, stripped_preview[:80],
            )
        if has_interrupted and (now - self._last_escape_time) < INTERRUPT_DETECT_WINDOW:
            _log.debug('ON_OUTPUT running→interrupted')
            self._output_buf.clear()
            with self._lock:
                self._state = CLIState.INTERRUPTED
                self._waiting_since = self._clock()
            self._write_interrupted_signal()
        else:
            # Trust dialog phase: after user answered via select_option,
            # on_send() sets state to running.  Startup output means
            # the CLI is booting — go straight to idle.
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
                        self._state = CLIState.IDLE
                        self._waiting_since = None
                    self._idle_since = self._clock()
                    self._idle_output_acc = 0
                    self._last_prompt_buf = b''
                    return

            # Interrupt detection without Escape keypress: the Ctrl+C or
            # self-interrupt may have bypassed on_input().  Use the
            # provider's confirmed_interrupt_pattern — a specific pattern
            # that only appears in the real interrupt prompt, not in
            # conversational text.
            confirmed = self._provider.confirmed_interrupt_pattern
            if has_interrupted and confirmed:
                if self._suppress_stale_interrupt:
                    _log.debug(
                        'ON_OUTPUT running: suppressing stale '
                        'confirmed interrupt pattern',
                    )
                else:
                    compact = self._ANSI_RE.sub(
                        b'', bytes(self._output_buf),
                    ).replace(b' ', b'')
                    if confirmed in compact:
                        _log.debug(
                            'ON_OUTPUT running→interrupted '
                            '(confirmed interrupt pattern in buffer)',
                        )
                        self._output_buf.clear()
                        with self._lock:
                            self._state = CLIState.INTERRUPTED
                            self._waiting_since = self._clock()
                        self._write_interrupted_signal()
                        return
            elif not has_interrupted:
                self._suppress_stale_interrupt = False

    def _handle_waiting_output(self, data: bytes, now: float) -> None:
        """Handle output while in a waiting state: correction, prompt accumulation, resume."""
        interrupted_pattern = self._provider.interrupted_pattern

        # Override to interrupted if we see the interrupted pattern
        # in fresh output shortly after user pressed Escape.
        # Use _last_escape_time to avoid false positives from normal
        # typing that triggers TUI redraws with "Interrupted" in AI text.
        if (
            self._state == CLIState.NEEDS_INPUT
            and interrupted_pattern in self._ANSI_RE.sub(b'', data)
            and (now - self._last_escape_time) < ESCAPE_CORRECTION_WINDOW
        ):
            _log.debug(
                'ON_OUTPUT needs_input→interrupted '
                '(pattern in output, Escape race)',
            )
            with self._lock:
                self._state = CLIState.INTERRUPTED
                self._waiting_since = now
            self._output_buf.clear()
            self._last_prompt_buf = b''
            self._write_interrupted_signal()
            return
        self._output_buf.clear()
        # Continue accumulating prompt output for Slack rendering.
        self._last_prompt_buf += data
        if len(self._last_prompt_buf) > 16384:
            self._last_prompt_buf = self._last_prompt_buf[-16384:]
        ws = self._waiting_since
        if (
            ws is not None
            and (self._clock() - ws) > RESUME_GRACE_PERIOD
            and self._last_input_time > ws
        ):
            stripped = self._ANSI_RE.sub(b'', data).strip()
            if stripped:
                if self._trust_dialog_phase:
                    _log.debug(
                        'ON_OUTPUT %s→idle (trust dialog startup)',
                        self._state,
                    )
                    self._trust_dialog_phase = False
                    with self._lock:
                        self._state = CLIState.IDLE
                        self._waiting_since = None
                    self._idle_since = self._clock()
                    self._idle_output_acc = 0
                    self._last_prompt_buf = b''
                    return

                if self._state == CLIState.INTERRUPTED:
                    self._suppress_stale_interrupt = True
                _log.debug(
                    'ON_OUTPUT %s→running (resume, stripped=%r)',
                    self._state, stripped[:60],
                )
                self._running_since = self._clock()
                self._idle_debounce_at = 0
                with self._lock:
                    self._state = CLIState.RUNNING
                    self._waiting_since = None
                self._last_prompt_buf = b''
                try:
                    self._signal_file.unlink(missing_ok=True)
                except OSError:
                    pass

    # -- Prompt output -------------------------------------------------------

    def get_prompt_output(self) -> str:
        """Return PTY output from the last permission/input prompt.

        Processes the raw PTY bytes through a minimal virtual terminal
        to properly handle TUI cursor-positioning layout.
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
            return ''

        # Box-drawing characters used by TUI borders.
        _box_chars = set('─━│┃┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬═║')
        lines = [line.rstrip() for line in display]
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
            return self._provider.parse_signal_file(raw)
        except OSError:
            return None

    @property
    def current_state(self) -> str:
        """Read the cached state without polling the signal file."""
        return self._state

    @property
    def auto_send_mode(self) -> AutoSendMode:
        """Current auto-send mode."""
        return self._auto_send_mode

    @auto_send_mode.setter
    def auto_send_mode(self, mode: AutoSendMode) -> None:
        self._auto_send_mode = mode

    def cleanup(self) -> None:
        """Delete the signal file."""
        try:
            self._signal_file.unlink(missing_ok=True)
        except OSError:
            pass


# Backwards-compatible alias for existing code that imports the old name.
ClaudeStateTracker = CLIStateTracker
