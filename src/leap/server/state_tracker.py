"""
CLI state tracking for Leap server.

Event-driven state machine that detects the CLI's current state
(idle, running, needs_permission, needs_input, interrupted) using
hook-based signal files, explicit input events, boolean flags, and
pyte terminal emulation.

No timing-based cooldowns or debounce windows — state transitions
are triggered by discrete events (hooks, user input, cursor visibility).
Safety fallback timeouts (60s) exist only for crash recovery.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import pyte

from leap.cli_providers.base import CLIProvider
from leap.cli_providers.registry import get_provider
from leap.cli_providers.states import AutoSendMode, CLIState, PROMPT_STATES, WAITING_STATES
from leap.utils.constants import (
    SAFETY_SILENCE_TIMEOUT,
    SAFETY_WAITING_TIMEOUT,
    STATE_LOG_DIR,
    STORAGE_DIR,
)

_log = logging.getLogger('leap.state')


def _setup_debug_log(signal_file: Path) -> None:
    """Set up per-session debug log.

    Real sessions: .storage/state_logs/<tag>.log
    Tests (tmp_path): <tmp_dir>/<tag>.state.log (avoids .storage pollution)
    """
    # Remove any stale handlers (e.g. from a previous session in the
    # same process — shouldn't happen, but be safe).
    for h in _log.handlers[:]:
        _log.removeHandler(h)
        h.close()
    tag = signal_file.stem
    try:
        is_real = signal_file.parent.resolve().is_relative_to(
            STORAGE_DIR.resolve())
    except (OSError, ValueError):
        is_real = False
    if is_real:
        STATE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = STATE_LOG_DIR / f'{tag}.log'
    else:
        log_path = signal_file.parent / f'{tag}.state.log'
    handler = logging.FileHandler(str(log_path), mode='w')
    handler.setFormatter(logging.Formatter(
        '%(asctime)s.%(msecs)03d %(message)s', datefmt='%H:%M:%S',
    ))
    _log.addHandler(handler)
    _log.setLevel(logging.DEBUG)


class CLIStateTracker:
    """Tracks CLI state via hook-written signal files and explicit events.

    State transitions are driven by:
    - Hook signal files (Stop, Notification) — primary mechanism
    - Explicit user events (on_send, on_input Enter) — idle→running
    - Boolean flags (_interrupt_pending, _user_responded) — replace timing
    - pyte cursor visibility — auto-resume detection
    - pyte rendered screen — pattern matching (interrupts, dialogs)

    Thread safety: ``_state`` and ``_waiting_since`` are protected by
    ``_lock``.  ``_screen`` and ``_prompt_snapshot`` are protected by
    ``_screen_lock``.  Boolean flags are lock-free (GIL-atomic).
    """

    # Screen dimensions for the virtual terminal.
    _SCREEN_COLS = 200
    _SCREEN_ROWS = 50

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
        self._screen_lock = threading.Lock()
        self._waiting_since: Optional[float] = None
        self._last_output_time: float = 0.0
        self._running_since: float = 0.0

        # -- Boolean flags (replace all timing windows) --
        # True after user pressed Escape/Ctrl+C — next "idle" signal
        # is reinterpreted as INTERRUPTED.
        self._interrupt_pending: bool = False
        # True after user typed while in a WAITING state — gates
        # waiting→idle and waiting→running transitions.
        self._user_responded: bool = False
        # True after the first real user keystroke (prevents startup
        # banner from falsely triggering idle→running).
        self._seen_user_input: bool = False
        # True after user typed since entering idle (gates auto-resume
        # cursor detection — don't auto-resume if user just typed).
        self._user_input_since_idle: bool = False

        # -- Trust dialog phase --
        self._trust_dialog_phase: bool = False

        # -- Stale interrupt suppression --
        self._suppress_stale_interrupt: bool = False

        # -- pyte virtual terminal --
        self._screen: pyte.Screen = pyte.Screen(
            self._SCREEN_COLS, self._SCREEN_ROWS,
        )
        self._stream: pyte.Stream = pyte.Stream(self._screen)
        # Snapshot of screen lines when entering a prompt state.
        self._prompt_snapshot: list[str] = []
        # Fallback: screen content saved when leaving RUNNING state.
        # Used when a hook signal arrives after the cursor+silence
        # heuristic has already transitioned to idle and cleared the
        # pyte screen — the Notification hook can fire seconds later,
        # by which time the screen is empty and the Ink TUI produces
        # no new output (it's waiting for user input).
        self._last_running_snapshot: list[str] = []

        # Delete stale signal file from previous server.
        try:
            self._signal_file.unlink(missing_ok=True)
        except OSError:
            pass

        _setup_debug_log(signal_file)
        _log.debug(
            'INIT state=idle signal_file=%s provider=%s',
            signal_file, self._provider.name,
        )

    def _write_interrupted_signal(self) -> None:
        """Write 'interrupted' state to the signal file."""
        try:
            self._signal_file.write_text(
                json.dumps({'state': CLIState.INTERRUPTED}),
            )
        except OSError:
            pass

    # -- Screen helpers -------------------------------------------------------

    def _capture_prompt_snapshot(self) -> list[str]:
        """Capture a prompt snapshot from the current screen, with fallback.

        After a running→idle transition the pyte screen is reset.  By
        the time the Notification hook fires (seconds later), the live
        screen may be empty or may contain only partial TUI redraws
        without the full dialog.  Fall back to ``_last_running_snapshot``
        (saved at running→idle time) whenever it has more non-blank
        lines than the live screen.

        Must be called with _screen_lock held.
        """
        snapshot = self._get_display_lines()
        if self._last_running_snapshot:
            live_filled = sum(1 for ln in snapshot if ln.strip())
            saved_filled = sum(1 for ln in self._last_running_snapshot if ln.strip())
            if saved_filled > live_filled:
                _log.debug(
                    'prompt snapshot sparse (%d lines vs %d saved), '
                    'using last running snapshot',
                    live_filled, saved_filled,
                )
                snapshot = self._last_running_snapshot
        self._last_running_snapshot = []
        return snapshot

    def _reset_screen(self) -> None:
        """Reset the pyte screen and stream to clear stale content.

        Called on state transitions so pattern matching only sees output
        produced AFTER the transition — not historical scrollback.
        Also recreates the Stream to clear any corrupted parser state
        (e.g., stuck mid-escape-sequence after a feed failure).
        pyte.Screen.reset() preserves current dimensions.
        Must be called with _screen_lock held.
        """
        self._screen.reset()
        self._stream = pyte.Stream(self._screen)

    def _get_display_lines(self) -> list[str]:
        """Return the screen content as a list of strings (one per row).

        Reads the screen buffer directly instead of using pyte's
        ``screen.display`` property, which crashes on empty chars in the
        buffer (``wcwidth(char[0])`` with ``char=''``).  Empty cells
        (wide-char placeholders or corrupted entries) are replaced with
        spaces — safe for pattern matching and snapshots.
        Must be called with _screen_lock held.
        """
        lines: list[str] = []
        for y in range(self._screen.lines):
            row = self._screen.buffer[y]
            chars: list[str] = []
            for x in range(self._screen.columns):
                data = row[x].data
                chars.append(data if data else ' ')
            lines.append(''.join(chars).rstrip())
        return lines

    def _get_screen_text(self) -> str:
        """Return the rendered screen content as a single string.

        Lines are joined with newlines for readability. Pattern matching
        should use the compact form (spaces removed) via
        ``screen_text.replace(' ', '').replace('\\n', '')`` to handle
        patterns that wrap across screen lines.
        Must be called with _screen_lock held.
        """
        return '\n'.join(self._get_display_lines())

    # -- Public API -----------------------------------------------------------

    @property
    def provider(self) -> CLIProvider:
        """The CLI provider used for pattern matching."""
        return self._provider

    def on_input(self, data: bytes) -> None:
        """Called when the user types in the server terminal.

        Handles:
        - Escape/Ctrl+C → sets _interrupt_pending
        - Enter in IDLE → transitions to RUNNING
        - Any input in WAITING_STATES → sets _user_responded
        - CSI u protocol (kitty keyboard) for Ctrl+C/Escape

        The input filter may bundle multiple keypresses into one call
        (paste, fast typing).  This method scans the entire data for
        interrupt signals, Enter, and printable content.
        """
        is_interrupt = False
        has_enter = False
        has_real_input = False  # printable chars, Enter, or Ctrl+C
        has_non_interrupt_input = False  # printable or Enter (not just Esc/Ctrl+C)

        # Scan the data byte-by-byte for interrupt signals and content.
        i = 0
        while i < len(data):
            b = data[i]

            if b == 0x03:  # Ctrl+C
                is_interrupt = True
                has_real_input = True
                i += 1

            elif b == 0x0d:  # Enter (CR)
                has_enter = True
                has_real_input = True
                has_non_interrupt_input = True
                i += 1

            elif b == 0x1b:  # Escape byte
                if i + 1 >= len(data):
                    # Standalone Escape at end of data
                    is_interrupt = True
                    has_real_input = True
                    i += 1
                elif data[i + 1] == 0x5b:
                    # CSI sequence: \x1b[ params final
                    end = i + 2
                    while end < len(data) and 0x20 <= data[end] <= 0x3f:
                        end += 1  # skip parameter bytes
                    if end < len(data):
                        end += 1  # include final byte
                    seq = data[i:end]
                    if self._is_csi_u_interrupt(seq):
                        is_interrupt = True
                        has_real_input = True
                    # Non-interrupt CSI (focus, mouse, cursor report)
                    # is silently skipped — not real user input.
                    i = end
                elif data[i + 1] in (0x5d, 0x50, 0x58, 0x5e, 0x5f):
                    # OSC/DCS/SOS/PM/APC — skip to ST terminator
                    end = i + 2
                    while end < len(data):
                        if data[end] == 0x07:
                            end += 1
                            break
                        if (data[end] == 0x1b and end + 1 < len(data)
                                and data[end + 1] == 0x5c):
                            end += 2
                            break
                        end += 1
                    i = end
                elif data[i + 1] == 0x4f:
                    # SS3 (e.g. \x1bOP for F1) — skip 3 bytes
                    i += 3 if i + 2 < len(data) else len(data)
                elif 0x40 <= data[i + 1] <= 0x5f:
                    # Two-byte escape (e.g. ESC M) — skip
                    i += 2
                else:
                    # Not a recognized sequence introducer — standalone
                    # Escape keypress.
                    is_interrupt = True
                    has_real_input = True
                    i += 1

            elif 0x20 <= b < 0x7f or b >= 0x80:
                # Printable ASCII or high byte (UTF-8 continuation)
                has_real_input = True
                has_non_interrupt_input = True
                i += 1

            elif b == 0x00:
                # Null byte — terminal noise, not real input.
                i += 1

            else:
                # Other control bytes (backspace, tab, etc.)
                has_real_input = True
                has_non_interrupt_input = True
                i += 1

        # Pure terminal events (focus, mouse) with no real user input
        # — skip entirely to avoid false flag updates.
        if not has_real_input:
            _log.debug(
                'ON_INPUT filtered terminal event len=%d data=%r',
                len(data), data[:20],
            )
            return

        if is_interrupt:
            self._interrupt_pending = True
            _log.debug('ON_INPUT _interrupt_pending=True')

        _log.debug(
            'ON_INPUT state=%s data=%r len=%d interrupt=%s enter=%s',
            self._state, data[:20], len(data), is_interrupt, has_enter,
        )

        self._seen_user_input = True
        # Don't set _user_input_since_idle for interrupt-only input —
        # Escape and Ctrl+C alone don't cause TUI redraws, so they
        # shouldn't block auto-resume detection.  But if the user typed
        # printable text alongside the interrupt, that counts.
        if has_non_interrupt_input:
            self._user_input_since_idle = True

        # Any input in WAITING_STATES → user responded.
        # This includes Escape (dismiss prompt) and regular keys (answer).
        if self._state in WAITING_STATES:
            self._user_responded = True
            _log.debug('ON_INPUT _user_responded=True')

        # Enter in idle → RUNNING.  This covers server-terminal typing
        # (the only path — on_send() only fires for client/queue sends).
        # For slash commands (/clear, /help) where the Stop hook doesn't
        # fire, the running→idle cursor+silence check in get_state()
        # resolves it within ~1s.
        if has_enter and self._state == CLIState.IDLE:
            _log.debug('ON_INPUT Enter in idle → running')
            self._running_since = self._clock()
            self._interrupt_pending = False
            self._user_input_since_idle = False
            with self._screen_lock:
                self._reset_screen()
                self._prompt_snapshot = []
                self._last_running_snapshot = []
                with self._lock:
                    self._state = CLIState.RUNNING
                    self._waiting_since = None
            try:
                self._signal_file.unlink(missing_ok=True)
            except OSError:
                pass

    def on_send(self) -> None:
        """Called when a message is sent to the CLI.

        Sets state to RUNNING and clears all flags.
        """
        if self._state == CLIState.INTERRUPTED:
            self._suppress_stale_interrupt = True
        else:
            self._suppress_stale_interrupt = False
        _log.debug('ON_SEND → running')
        self._seen_user_input = True
        self._running_since = self._clock()
        self._interrupt_pending = False
        self._user_responded = False
        self._user_input_since_idle = False
        # Acquire _screen_lock first to maintain consistent lock
        # ordering with on_output (screen_lock → lock).
        with self._screen_lock:
            self._reset_screen()
            self._prompt_snapshot = []
            self._last_running_snapshot = []
            with self._lock:
                self._state = CLIState.RUNNING
                self._waiting_since = None

        try:
            self._signal_file.unlink(missing_ok=True)
        except OSError:
            pass

    def on_resize(self, rows: int, cols: int) -> None:
        """Called when the terminal is resized.

        Updates the pyte screen dimensions. No timing suppression needed —
        TUI redraws are absorbed naturally by the virtual terminal.
        """
        if rows < 1 or cols < 1:
            return
        with self._screen_lock:
            self._screen.resize(rows, cols)
        _log.debug('ON_RESIZE %dx%d', cols, rows)

    def on_output(self, data: bytes) -> None:
        """Called when PTY output is received.

        Feeds data through pyte and dispatches to state-specific handlers.

        Exceptions are caught and logged — an uncaught exception here
        would propagate to pexpect's interact loop and kill the PTY.
        """
        now = self._clock()
        self._last_output_time = now

        # Feed through pyte virtual terminal
        with self._screen_lock:
            text = data.decode('utf-8', errors='replace')
            try:
                self._stream.feed(text)
            except (IndexError, ValueError, AssertionError):
                # Feed failed — screen may be in a corrupted state.
                self._reset_screen()
                _log.debug('ON_OUTPUT pyte feed failed, screen reset')
                return

            try:
                if self._state == CLIState.IDLE:
                    self._handle_idle_output(now)
                elif self._state == CLIState.RUNNING:
                    self._handle_running_output(now)
                elif self._state in WAITING_STATES:
                    self._handle_waiting_output(now)
            except Exception:
                # Never let handler exceptions kill the PTY.
                _log.debug(
                    'ON_OUTPUT handler exception, resetting screen',
                    exc_info=True,
                )
                self._reset_screen()

    # -- on_output sub-handlers -----------------------------------------------

    def _handle_idle_output(self, now: float) -> None:
        """Handle output while idle.

        Checks for: startup dialogs, interrupt patterns, auto-resume.
        Must be called with _screen_lock held.
        """
        screen_text = self._get_screen_text()
        compact = screen_text.replace(' ', '').replace('\n', '')
        compact_lines = screen_text.replace(' ', '')

        # Clear stale interrupt suppression once the pattern is no longer
        # on screen (old text scrolled out of the TUI's visible area).
        if self._suppress_stale_interrupt:
            interrupted_pattern = self._provider.interrupted_pattern
            pattern_str = interrupted_pattern.decode('utf-8', errors='replace')
            if pattern_str not in compact:
                self._suppress_stale_interrupt = False
                _log.debug(
                    'ON_OUTPUT idle: cleared _suppress_stale_interrupt '
                    '(pattern no longer on screen)',
                )

        # -- Startup dialog detection --
        if not self._seen_user_input:
            trust_patterns = self._provider.trust_dialog_patterns
            dialog_patterns = self._provider.dialog_patterns

            _log.debug(
                'ON_OUTPUT idle (startup) compact_tail=%r',
                compact[-120:],
            )

            is_trust = any(
                p.decode('utf-8', errors='replace') in compact
                for p in trust_patterns
            )
            is_dialog = (
                bool(dialog_patterns)
                and all(
                    p.decode('utf-8', errors='replace') in compact
                    for p in dialog_patterns
                )
            )

            if is_trust or is_dialog:
                _log.debug(
                    'ON_OUTPUT idle→needs_permission '
                    '(startup dialog: trust=%s dialog=%s)',
                    is_trust, is_dialog,
                )
                self._prompt_snapshot = self._get_display_lines()
                self._reset_screen()
                if is_trust:
                    self._trust_dialog_phase = True
                self._interrupt_pending = False
                with self._lock:
                    self._state = CLIState.NEEDS_PERMISSION
                    self._waiting_since = self._clock()
                return

        # -- Interrupt detection (Escape race) --
        # Gated by _suppress_stale_interrupt: after resolving an interrupt,
        # the TUI re-renders its visible area including old "Interrupted"
        # text in scrollback.  Without suppression, an accidental Escape
        # would false-trigger interrupted from that stale text.
        if self._interrupt_pending and not self._suppress_stale_interrupt:
            interrupted_pattern = self._provider.interrupted_pattern
            pattern_str = interrupted_pattern.decode('utf-8', errors='replace')
            if pattern_str in compact:
                _log.debug(
                    'ON_OUTPUT idle→interrupted (interrupt_pending + pattern)',
                )
                self._interrupt_pending = False
                self._prompt_snapshot = []
                self._reset_screen()
                with self._lock:
                    self._state = CLIState.INTERRUPTED
                    self._waiting_since = self._clock()
                self._write_interrupted_signal()
                return

        # -- Confirmed interrupt pattern (no flag needed) --
        # Uses compact_lines (newlines preserved) to prevent cross-line
        # false positives.
        confirmed = self._provider.confirmed_interrupt_pattern
        if confirmed and self._seen_user_input:
            if not self._suppress_stale_interrupt:
                confirmed_str = confirmed.decode('utf-8', errors='replace')
                if confirmed_str in compact_lines:
                    _log.debug(
                        'ON_OUTPUT idle→interrupted (confirmed pattern)',
                    )
                    self._interrupt_pending = False
                    self._prompt_snapshot = []
                    self._reset_screen()
                    with self._lock:
                        self._state = CLIState.INTERRUPTED
                        self._waiting_since = self._clock()
                    self._write_interrupted_signal()
                    return

        if not self._seen_user_input:
            return

        # -- Auto-resume via cursor visibility --
        # Don't check cursor here (on_output) — a mid-render chunk may
        # have cursor hidden but the show-cursor sequence arrives in
        # the next chunk.  Instead, set a flag for get_state() to check
        # at poll time (0.5s later).  Brief TUI redraws will have
        # restored cursor visibility by then.
        # (Auto-resume check is in get_state(), not here.)

    def _handle_running_output(self, now: float) -> None:
        """Handle output while running.

        Checks for: interrupt patterns, trust dialog startup.
        Must be called with _screen_lock held.
        """
        screen_text = self._get_screen_text()
        # compact_full: spaces+newlines removed (for wrap-across-lines
        # pattern matching with _interrupt_pending flag)
        compact_full = screen_text.replace(' ', '').replace('\n', '')
        # compact_lines: spaces removed but newlines preserved (for
        # confirmed_interrupt_pattern — prevents false positives from
        # cross-line text concatenation like "Conversation\ninterrupted")
        compact_lines = screen_text.replace(' ', '')
        interrupted_pattern = self._provider.interrupted_pattern
        pattern_str = interrupted_pattern.decode('utf-8', errors='replace')
        has_interrupted = pattern_str in compact_full

        # Clear stale interrupt suppression once pattern scrolls off.
        if self._suppress_stale_interrupt and not has_interrupted:
            self._suppress_stale_interrupt = False

        stripped_preview = screen_text.strip()
        if stripped_preview:
            _log.debug(
                'ON_OUTPUT running has_Interrupted=%s',
                has_interrupted,
            )

        # -- Interrupt with pending flag --
        if has_interrupted and self._interrupt_pending:
            _log.debug('ON_OUTPUT running→interrupted (interrupt_pending)')
            self._interrupt_pending = False
            self._prompt_snapshot = []
            self._reset_screen()
            with self._lock:
                self._state = CLIState.INTERRUPTED
                self._waiting_since = self._clock()
            self._write_interrupted_signal()
            return

        # -- Trust dialog phase: startup output → idle --
        if self._trust_dialog_phase:
            if stripped_preview:
                _log.debug(
                    'ON_OUTPUT running→idle (trust dialog startup)',
                )
                self._trust_dialog_phase = False
                self._seen_user_input = False
                self._interrupt_pending = False
                self._suppress_stale_interrupt = False
                self._prompt_snapshot = []
                self._reset_screen()
                with self._lock:
                    self._state = CLIState.IDLE
                    self._waiting_since = None
                self._user_input_since_idle = False
                return

        # -- Confirmed interrupt pattern (no flag needed) --
        # Uses compact_lines (newlines preserved) to avoid false positives
        # from cross-line text concatenation.  E.g., "Conversation" on
        # line 1 + "interrupted" on line 2 would form
        # "Conversationinterrupted" in compact_full but not compact_lines.
        confirmed = self._provider.confirmed_interrupt_pattern
        if has_interrupted and confirmed:
            if not self._suppress_stale_interrupt:
                confirmed_str = confirmed.decode('utf-8', errors='replace')
                if confirmed_str in compact_lines:
                    _log.debug(
                        'ON_OUTPUT running→interrupted (confirmed pattern)',
                    )
                    self._interrupt_pending = False
                    self._prompt_snapshot = []
                    self._reset_screen()
                    with self._lock:
                        self._state = CLIState.INTERRUPTED
                        self._waiting_since = self._clock()
                    self._write_interrupted_signal()
                    return

    def _handle_waiting_output(self, now: float) -> None:
        """Handle output while in a waiting state.

        Checks for: interrupt correction, trust dialog recovery.
        Must be called with _screen_lock held.
        """
        screen_text = self._get_screen_text()
        compact = screen_text.replace(' ', '').replace('\n', '')
        interrupted_pattern = self._provider.interrupted_pattern
        pattern_str = interrupted_pattern.decode('utf-8', errors='replace')

        # -- Escape correction: waiting → interrupted --
        # Applies to NEEDS_INPUT and NEEDS_PERMISSION — the user may
        # press Escape to cancel/dismiss any prompt.
        if (
            self._state in (CLIState.NEEDS_INPUT, CLIState.NEEDS_PERMISSION)
            and self._interrupt_pending
            and pattern_str in compact
        ):
            _log.debug(
                'ON_OUTPUT %s→interrupted (interrupt_pending)',
                self._state,
            )
            self._interrupt_pending = False
            self._prompt_snapshot = []
            self._reset_screen()
            with self._lock:
                self._state = CLIState.INTERRUPTED
                self._waiting_since = now
            self._write_interrupted_signal()
            return

        # -- Trust dialog recovery --
        if (
            self._trust_dialog_phase
            and self._waiting_since is not None
            and self._seen_user_input
        ):
            stripped = screen_text.strip()
            if stripped:
                _log.debug(
                    'ON_OUTPUT %s→idle (trust dialog startup)',
                    self._state,
                )
                self._trust_dialog_phase = False
                self._seen_user_input = False
                self._interrupt_pending = False
                self._suppress_stale_interrupt = False
                self._reset_screen()
                with self._lock:
                    self._state = CLIState.IDLE
                    self._waiting_since = None
                self._user_input_since_idle = False
                self._prompt_snapshot = []
                return

        # -- Accumulate prompt snapshot --
        # During trust_dialog_phase, the snapshot was already captured
        # at detection time (in _handle_idle_output).  Don't overwrite
        # it — the screen was reset after capture and may contain only
        # fragments from subsequent Ink TUI redraws.
        #
        # Similarly, after a running→needs_permission transition the
        # screen is reset (line 943 in get_state).  The initial snapshot
        # captured the full dialog, but subsequent TUI redraws on the
        # fresh screen may be partial.  Only replace the snapshot when
        # the new content has at least as many non-blank lines.
        if not self._trust_dialog_phase:
            new_lines = self._get_display_lines()
            old_filled = sum(1 for ln in self._prompt_snapshot if ln.strip())
            new_filled = sum(1 for ln in new_lines if ln.strip())
            if new_filled >= old_filled:
                self._prompt_snapshot = new_lines

    # -- State polling --------------------------------------------------------

    def get_state(self, pty_alive: bool) -> str:
        """Poll the signal file and return the CLI's current state."""
        if not pty_alive:
            was_idle = self._state == CLIState.IDLE
            with self._lock:
                self._state = CLIState.IDLE
                self._waiting_since = None
            # Only reset flags/screen on the transition to dead,
            # not on every poll cycle while dead.
            if not was_idle:
                self._interrupt_pending = False
                self._user_responded = False
                self._user_input_since_idle = False
                self._seen_user_input = False
                self._trust_dialog_phase = False
                self._suppress_stale_interrupt = False
                with self._screen_lock:
                    self._reset_screen()
                    self._prompt_snapshot = []
                    self._last_running_snapshot = []
            return CLIState.IDLE

        with self._lock:
            current = self._state

        # -- Read signal file --
        new_state = self._read_signal_state()
        if new_state and new_state != current:
            # Ignore self-written "interrupted" signals.
            if new_state == CLIState.INTERRUPTED:
                _log.debug(
                    'GET_STATE signal=interrupted but current=%s — '
                    'ignoring (deleting)',
                    current,
                )
                try:
                    self._signal_file.unlink(missing_ok=True)
                except OSError:
                    pass

            # -- running → idle (check interrupt flag + screen) --
            elif (
                new_state == CLIState.IDLE
                and current == CLIState.RUNNING
            ):
                # Only convert to INTERRUPTED if the interrupt pattern
                # is actually visible on the pyte screen.  The user may
                # have pressed Escape but the CLI ignored it (busy) and
                # finished normally — in that case, "Interrupted" never
                # appeared and we should accept the idle transition.
                has_pattern = False
                if self._interrupt_pending:
                    interrupted_pattern = self._provider.interrupted_pattern
                    pattern_str = interrupted_pattern.decode(
                        'utf-8', errors='replace',
                    )
                    with self._screen_lock:
                        screen_text = self._get_screen_text()
                        compact = screen_text.replace(
                            ' ', '',
                        ).replace('\n', '')
                    has_pattern = pattern_str in compact

                if self._interrupt_pending and has_pattern:
                    _log.debug(
                        'GET_STATE signal=idle + interrupt_pending '
                        '+ pattern on screen → interrupted',
                    )
                    self._interrupt_pending = False
                    self._user_responded = False
                    with self._lock:
                        self._state = CLIState.INTERRUPTED
                        self._waiting_since = self._clock()
                    self._write_interrupted_signal()
                    self._user_input_since_idle = False
                    with self._screen_lock:
                        self._reset_screen()
                    return CLIState.INTERRUPTED
                else:
                    if self._interrupt_pending:
                        _log.debug(
                            'GET_STATE signal=idle + interrupt_pending '
                            'but NO pattern on screen → idle '
                            '(CLI ignored the Escape)',
                        )
                    else:
                        _log.debug('GET_STATE signal transition running→idle')
                    self._interrupt_pending = False
                    with self._lock:
                        self._state = CLIState.IDLE
                        self._waiting_since = None
                    self._user_input_since_idle = False
                    if self._trust_dialog_phase:
                        self._seen_user_input = False
                    self._trust_dialog_phase = False
                    with self._screen_lock:
                        self._last_running_snapshot = self._get_display_lines()
                        self._reset_screen()
                        self._prompt_snapshot = []
                    return CLIState.IDLE

            # -- waiting → idle (requires _user_responded) --
            elif (
                new_state == CLIState.IDLE
                and current in WAITING_STATES
            ):
                if self._user_responded:
                    _log.debug(
                        'GET_STATE signal transition %s→idle '
                        '(user_responded)',
                        current,
                    )
                    if current == CLIState.INTERRUPTED:
                        self._suppress_stale_interrupt = True
                    self._interrupt_pending = False
                    self._user_responded = False
                    with self._lock:
                        self._state = CLIState.IDLE
                        self._waiting_since = None
                    self._user_input_since_idle = False
                    self._trust_dialog_phase = False
                    with self._screen_lock:
                        self._reset_screen()
                        self._prompt_snapshot = []
                    return CLIState.IDLE
                else:
                    _log.debug(
                        'GET_STATE signal=idle but %s without '
                        'user_responded — ignoring',
                        current,
                    )

            # -- interrupted: protect from needs_input (Notification hook race) --
            elif (
                new_state == CLIState.NEEDS_INPUT
                and current == CLIState.INTERRUPTED
                and not self._user_responded
            ):
                _log.debug(
                    'GET_STATE signal=needs_input but protecting '
                    'interrupted (no user_responded)',
                )

            # -- All other signal transitions --
            else:
                # Late Notification guard: the Notification hook can
                # take ~6s to arrive — by then the cursor+silence
                # heuristic has already moved running→idle and the
                # dialog may have been auto-accepted (bypass) or the
                # CLI finished.  Verify the dialog is actually visible
                # before transitioning.
                # Covers both needs_permission (permission_prompt) and
                # needs_input (elicitation_dialog) — both use the same
                # Ink TUI chrome with dialog_patterns.
                if (
                    current in (CLIState.IDLE, CLIState.RUNNING)
                    and new_state in (
                        CLIState.NEEDS_PERMISSION,
                        CLIState.NEEDS_INPUT,
                    )
                    and self._provider.dialog_patterns
                ):
                    with self._screen_lock:
                        screen_text = self._get_screen_text()
                        compact = screen_text.replace(
                            ' ', '',
                        ).replace('\n', '')
                        # After running→idle the screen was reset.  The
                        # Notification hook can arrive seconds later, by
                        # which time the live screen may be empty or may
                        # contain only partial TUI redraws (without the
                        # full dialog).  Always check the snapshot saved
                        # at running→idle time as a fallback.
                        if self._last_running_snapshot:
                            fallback = '\n'.join(
                                self._last_running_snapshot,
                            )
                            compact += fallback.replace(
                                ' ', '',
                            ).replace('\n', '')
                    has_dialog = any(
                        p.decode('utf-8', errors='replace') in compact
                        for p in self._provider.dialog_patterns
                    )
                    if not has_dialog:
                        _log.debug(
                            'GET_STATE signal=%s from idle but no '
                            'dialog patterns on screen — ignoring '
                            'stale notification',
                            new_state,
                        )
                        # Delete the stale signal so it doesn't block
                        # future transitions on every poll cycle.
                        try:
                            self._signal_file.unlink(missing_ok=True)
                        except OSError:
                            pass
                        return current

                if current == CLIState.INTERRUPTED:
                    self._suppress_stale_interrupt = True
                _log.debug(
                    'GET_STATE signal transition %s→%s',
                    current, new_state,
                )
                self._interrupt_pending = False
                with self._lock:
                    self._state = new_state
                    if new_state in PROMPT_STATES:
                        self._waiting_since = self._clock()
                    else:
                        self._waiting_since = None
                self._user_responded = False
                # Clear trust dialog phase on any signal transition —
                # if a real permission prompt fires after the trust
                # dialog, we must not treat its output as startup.
                if self._trust_dialog_phase:
                    if new_state == CLIState.IDLE:
                        self._seen_user_input = False
                    self._trust_dialog_phase = False
                with self._screen_lock:
                    if new_state in PROMPT_STATES:
                        self._prompt_snapshot = self._capture_prompt_snapshot()
                    else:
                        self._prompt_snapshot = []
                        self._last_running_snapshot = []
                    self._reset_screen()
                if new_state == CLIState.IDLE:
                    self._user_input_since_idle = False
                return new_state

        # -- Transcript-based idle detection (Codex) --
        if current == CLIState.RUNNING and self._provider.transcript_sessions_dir:
            msg = self._provider.read_transcript_completion(
                since=self._running_since,
            )
            if msg is not None:
                _log.debug(
                    'GET_STATE transcript task_complete → idle (msg=%r)',
                    msg[:60] if msg else '',
                )
                try:
                    signal_data = {'state': CLIState.IDLE}
                    if msg:
                        signal_data['last_assistant_message'] = msg
                    self._signal_file.write_text(
                        json.dumps(signal_data),
                    )
                except OSError:
                    pass
                self._interrupt_pending = False
                with self._lock:
                    self._state = CLIState.IDLE
                    self._waiting_since = None
                self._user_input_since_idle = False
                with self._screen_lock:
                    self._last_running_snapshot = self._get_display_lines()
                    self._reset_screen()
                return CLIState.IDLE

        # -- Waiting → running via cursor visibility (poll-based) --
        # When the user answers a permission/input prompt directly in the
        # terminal, on_input() sets _user_responded but no signal fires
        # until the CLI finishes (Stop hook → idle).  The monitor stays
        # stuck at needs_permission/needs_input for the entire run.
        # Detect that the CLI has moved past the dialog by checking
        # cursor visibility at poll time: Ink TUIs hide the cursor while
        # processing.  Poll-based (not on_output) to avoid false triggers
        # from mid-render cursor-hidden chunks.
        # Does NOT delete the signal file — if this is a false trigger
        # (brief cursor hide during TUI redraw), the signal file lets
        # the Late Notification guard self-correct on the next poll.
        if (
            current in WAITING_STATES
            and self._user_responded
            and not self._provider.cursor_hidden_while_idle
        ):
            with self._screen_lock:
                cursor_hidden = self._screen.cursor.hidden
            if cursor_hidden:
                _log.debug(
                    'GET_STATE %s→running (user_responded + cursor '
                    'hidden at poll)',
                    current,
                )
                if current == CLIState.INTERRUPTED:
                    self._suppress_stale_interrupt = True
                self._running_since = self._clock()
                self._interrupt_pending = False
                self._user_responded = False
                self._user_input_since_idle = False
                with self._screen_lock:
                    self._prompt_snapshot = []
                    self._last_running_snapshot = []
                    self._reset_screen()
                with self._lock:
                    self._state = CLIState.RUNNING
                    self._waiting_since = None
                return CLIState.RUNNING

        # -- Auto-resume via cursor visibility (poll-based) --
        # Checked at poll time (every 0.5s) rather than on_output to
        # avoid false triggers from mid-render cursor-hidden state.
        # By poll time, brief TUI redraws have completed and cursor
        # is visible again.  Only true auto-resumes (sustained
        # processing) keep cursor hidden across a poll boundary.
        # Disabled for full-screen TUIs (Ratatui) that keep cursor
        # hidden permanently — they use silence_timeout + transcript
        # detection instead.
        if (
            current == CLIState.IDLE
            and self._seen_user_input
            and not self._provider.cursor_hidden_while_idle
        ):
            with self._screen_lock:
                cursor_hidden = self._screen.cursor.hidden
            should_resume = False
            if not self._user_input_since_idle and cursor_hidden:
                should_resume = True
                with self._screen_lock:
                    self._reset_screen()
                    self._last_running_snapshot = []
            if should_resume:
                _log.debug(
                    'GET_STATE idle→running (cursor hidden at poll, '
                    'auto-resume)',
                )
                self._running_since = self._clock()
                self._interrupt_pending = False
                with self._lock:
                    self._state = CLIState.RUNNING
                    self._waiting_since = None
                try:
                    self._signal_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return CLIState.RUNNING

        # -- Running → idle via cursor visibility + output silence --
        # For Ink TUIs: cursor visible + no output for >2s = CLI
        # returned to idle prompt.  Handles cases where the Stop hook
        # doesn't fire (e.g. /clear, /help).  2s is long enough that
        # brief streaming pauses don't false-trigger, but short enough
        # that /clear resolves quickly.  Disabled for Ratatui TUIs.
        if (
            current == CLIState.RUNNING
            and not self._provider.cursor_hidden_while_idle
            and self._last_output_time > 0
            and (self._clock() - self._last_output_time) > 2.0
        ):
            with self._screen_lock:
                cursor_visible = not self._screen.cursor.hidden
            if cursor_visible:
                # Before transitioning to idle, check if the screen
                # has a permission/input dialog.  This detects prompts
                # seconds earlier than the Notification hook and avoids
                # a false "idle" flash in the monitor.
                # Check only the last 5 non-blank rows (where the
                # dialog footer lives) and require ALL patterns to
                # reduce false positives from response text mentioning
                # these phrases.  Using non-blank rows handles tall
                # terminals where blank rows pad the screen below the
                # dialog.
                if self._provider.dialog_patterns:
                    with self._screen_lock:
                        all_lines = self._get_display_lines()
                    filled = [ln for ln in all_lines if ln.strip()]
                    tail = filled[-5:] if filled else []
                    compact_tail = ''.join(tail).replace(
                        ' ', '',
                    )
                    has_dialog = all(
                        p.decode('utf-8', errors='replace') in compact_tail
                        for p in self._provider.dialog_patterns
                    )
                    if has_dialog:
                        _log.debug(
                            'GET_STATE running→needs_permission '
                            '(dialog on screen + output silent %.1fs)',
                            self._clock() - self._last_output_time,
                        )
                        self._interrupt_pending = False
                        self._user_responded = False
                        if self._trust_dialog_phase:
                            self._trust_dialog_phase = False
                        with self._lock:
                            self._state = CLIState.NEEDS_PERMISSION
                            self._waiting_since = self._clock()
                        with self._screen_lock:
                            # Reuse the lines already captured above
                            # instead of re-reading the screen.
                            self._prompt_snapshot = all_lines
                            self._last_running_snapshot = list(
                                all_lines)
                            self._reset_screen()
                        return CLIState.NEEDS_PERMISSION

                _log.debug(
                    'GET_STATE running→idle (cursor visible + '
                    'output silent %.1fs)',
                    self._clock() - self._last_output_time,
                )
                self._interrupt_pending = False
                with self._lock:
                    self._state = CLIState.IDLE
                    self._waiting_since = None
                self._user_input_since_idle = False
                with self._screen_lock:
                    self._last_running_snapshot = self._get_display_lines()
                    self._reset_screen()
                    self._prompt_snapshot = []
                return CLIState.IDLE

        # -- Safety fallback: silence timeout --
        silence_timeout = (
            self._provider.silence_timeout
            if self._provider.silence_timeout is not None
            else SAFETY_SILENCE_TIMEOUT
        )
        if current == CLIState.RUNNING and self._last_output_time > 0:
            silence = self._clock() - self._last_output_time
            if silence > silence_timeout:
                _log.debug(
                    'GET_STATE safety timeout %.1fs → idle', silence,
                )
                self._interrupt_pending = False
                with self._lock:
                    self._state = CLIState.IDLE
                    self._waiting_since = None
                self._user_input_since_idle = False
                with self._screen_lock:
                    self._last_running_snapshot = self._get_display_lines()
                    self._reset_screen()
                    self._prompt_snapshot = []
                return CLIState.IDLE

        # -- Safety fallback: stuck waiting state --
        if current in WAITING_STATES and self._last_output_time > 0:
            silence = self._clock() - self._last_output_time
            if silence > SAFETY_WAITING_TIMEOUT:
                signal_state = self._read_signal_state()
                if signal_state == current or self._trust_dialog_phase:
                    _log.debug(
                        'GET_STATE waiting timeout %s %.1fs but '
                        '%s — keeping',
                        current, silence,
                        'trust dialog active'
                        if self._trust_dialog_phase
                        else 'signal confirms',
                    )
                else:
                    _log.debug(
                        'GET_STATE waiting timeout %s %.1fs → idle',
                        current, silence,
                    )
                    self._interrupt_pending = False
                    self._user_responded = False
                    with self._lock:
                        self._state = CLIState.IDLE
                        self._waiting_since = None
                    self._user_input_since_idle = False
                    with self._screen_lock:
                        self._reset_screen()
                        self._prompt_snapshot = []
                    return CLIState.IDLE

        return current

    def is_ready(self, pty_alive: bool) -> bool:
        """Check if the auto-sender should send the next message."""
        return self.is_ready_for_state(self.get_state(pty_alive))

    def is_ready_for_state(self, state: str) -> bool:
        """Check readiness for sending the next queued message.

        In both modes, queued messages are only sent when IDLE.
        Permission auto-approve (ALWAYS mode) is handled separately
        by the auto-sender loop in ``LeapServer``.
        """
        return state == CLIState.IDLE

    @staticmethod
    def _is_csi_u_interrupt(data: bytes) -> bool:
        """Check if a CSI sequence encodes Ctrl+C or Escape.

        Kitty CSI u: ``\\x1b[<codepoint>;<modifiers>u``
        Legacy xterm: ``\\x1b[27;<modifier>;<keycode>~``

        Ctrl+C: codepoint 3 (raw) or 99 with Ctrl modifier (bit 4).
        Escape: codepoint 27 (standalone).
        """
        if len(data) < 4:
            return False
        final = data[-1]
        params = data[2:-1]
        parts = params.split(b';')

        # Legacy xterm format: \x1b[27;<mod>;<keycode>~
        if final == 0x7e and len(parts) == 3:  # ends with '~'
            try:
                prefix = int(parts[0].split(b':')[0])
                mod = int(parts[1].split(b':')[0])
                keycode = int(parts[2].split(b':')[0])
            except ValueError:
                return False
            if prefix == 27:
                # Ctrl+'c' (keycode 99) or raw Ctrl+C (keycode 3)
                if keycode in (3, 99) and (mod - 1) & 0x04 != 0:
                    return True
                # Standalone Escape (keycode 27, any modifier)
                if keycode == 27:
                    return True
            return False

        # Kitty CSI u format: \x1b[<codepoint>;<modifiers>u
        if final != 0x75:  # must end with 'u'
            return False
        codepoint_raw = parts[0] if parts else b''
        try:
            codepoint = int(codepoint_raw.split(b':')[0])
        except ValueError:
            return False
        if codepoint in (3, 27):  # Ctrl+C or Escape
            return True
        if codepoint == 99 and len(parts) >= 2:  # 'c' with modifiers
            try:
                modifiers = int(parts[1].split(b':')[0])
            except ValueError:
                return False
            return (modifiers - 1) & 0x04 != 0  # Ctrl bit set
        return False

    def get_prompt_output(self) -> str:
        """Return PTY output from the last permission/input prompt.

        Reads from the pyte screen snapshot taken when entering the
        prompt state.  Falls back to the live screen if the snapshot
        is empty but the state is still a prompt state — this covers
        the trust dialog case where the snapshot can be overwritten
        by subsequent output after a screen reset.
        """
        with self._screen_lock:
            snapshot = self._prompt_snapshot
            if not snapshot and self._state in WAITING_STATES:
                snapshot = self._get_display_lines()
        if not snapshot:
            return ''
        _box_chars = set('─━│┃┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬═║')
        lines = [line.rstrip() for line in snapshot]
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
        """Read the state from the signal file."""
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


# Backwards-compatible alias.
ClaudeStateTracker = CLIStateTracker
