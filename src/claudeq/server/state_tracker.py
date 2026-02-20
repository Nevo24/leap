"""
Claude state tracking for ClaudeQ server.

Encapsulates the state machine that detects Claude's current state
(idle, running, needs_permission, has_question) using hook-based
signal files with a PTY silence fallback.
"""

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

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
        # True while the trust dialog is showing.  When the user answers
        # and Claude starts up, resume detection goes to 'idle' instead
        # of 'running' because there's no pending request to process.
        self._trust_dialog_phase: bool = False
        # Snapshot of _output_buf captured just before clearing on
        # needs_permission / has_question transitions.  Used by Slack
        # integration to show the actual prompt text + numbered options.
        self._last_prompt_buf: bytes = b''

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
            One of: 'idle', 'running', 'needs_permission', 'has_question'.
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
            # Claude do instead?" — keep has_question.
            if (
                new_state == 'idle'
                and current == 'has_question'
                and self._waiting_since is not None
                and (self._clock() - self._waiting_since) < 5.0
            ):
                _log.debug(
                    'GET_STATE signal=idle but protecting has_question '
                    '(%.1fs since wait)',
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
                if new_state in ('needs_permission', 'has_question'):
                    self._last_prompt_buf = bytes(self._output_buf)
                else:
                    self._last_prompt_buf = b''
                self._output_buf.clear()
                if new_state == 'idle':
                    self._idle_since = self._clock()
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
        with self._lock:
            self._state = 'running'
            self._waiting_since = None
        self._output_buf.clear()
        self._idle_output_acc = 0
        self._last_prompt_buf = b''

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
                has_interrupted = b'Interrupted' in data
                if has_interrupted or b'Interrupted' in self._output_buf:
                    _log.debug(
                        'ON_OUTPUT idle→has_question (Escape race detected)',
                    )
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
                        _log.debug(
                            'ON_OUTPUT idle→running (accumulated %d bytes)',
                            self._idle_output_acc,
                        )
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
            # Check the raw chunk BEFORE buffer trim — a large TUI redraw
            # chunk can exceed 512 bytes with "Interrupted" near the start,
            # causing it to be trimmed out of the rolling buffer.
            has_interrupted = b'Interrupted' in data
            self._output_buf.extend(data)
            if len(self._output_buf) > 512:
                self._output_buf = self._output_buf[-512:]
            if not has_interrupted:
                has_interrupted = b'Interrupted' in self._output_buf
            # Log every chunk while running to diagnose detection failures
            stripped_preview = self._ANSI_RE.sub(b'', data).strip()
            if stripped_preview:
                _log.debug(
                    'ON_OUTPUT running chunk len=%d buf_len=%d '
                    'has_Interrupted=%s stripped=%r',
                    len(data), len(self._output_buf),
                    has_interrupted, stripped_preview[:80],
                )
            if has_interrupted:
                _log.debug('ON_OUTPUT running→has_question (Interrupted)')
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
        if not self._last_prompt_buf:
            return ''
        return self._render_screen(self._last_prompt_buf)

    @staticmethod
    def _render_screen(raw: bytes, rows: int = 50, cols: int = 200) -> str:
        """Render cursor-positioned PTY output into readable text.

        Ink (React-based TUI) renders text using CSI cursor-positioning
        sequences rather than simple newlines.  Stripping ANSI merges
        words together.  This mini virtual terminal processes CUP, cursor
        movement, erase, and printable text into a 2-D character grid,
        then extracts readable lines.

        Args:
            raw: Raw PTY output bytes (may contain ANSI/CSI sequences).
            rows: Virtual screen height.
            cols: Virtual screen width.

        Returns:
            Cleaned multi-line text with proper spacing.
        """
        text = raw.decode('utf-8', errors='replace')

        # Strip a leading partial CSI sequence left by buffer truncation.
        # E.g. buffer trimmed mid-SGR: ";2;153;153;153m..." — the ESC [
        # prefix was lost, so the params + final byte appear as text.
        # Require at least one ';' to distinguish from regular text.
        _csi_tail = re.match(r'[0-9;?]*[;?][0-9;?]*[A-Za-z]', text)
        if _csi_tail:
            text = text[_csi_tail.end():]

        grid: list[list[str]] = [[' '] * cols for _ in range(rows)]
        cr, cc = 0, 0  # cursor row, col

        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == '\x1b' and i + 1 < n:
                nch = text[i + 1]
                if nch == '[':
                    # CSI sequence: ESC [ params final_byte
                    j = i + 2
                    while j < n and (text[j].isdigit() or text[j] in ';?'):
                        j += 1
                    if j >= n:
                        break
                    params_s = text[i + 2:j]
                    final = text[j]
                    # Private-mode sequences (CSI ? ...) like ?25l
                    # (hide cursor), ?2004h (bracketed paste) — skip.
                    if params_s.startswith('?'):
                        i = j + 1
                        continue
                    parts = (
                        [int(p) if p else 0 for p in params_s.split(';')]
                        if params_s else []
                    )
                    if final in ('H', 'f'):  # CUP
                        cr = max(0, min(
                            (parts[0] if parts else 1) - 1, rows - 1,
                        ))
                        cc = max(0, min(
                            (parts[1] if len(parts) > 1 else 1) - 1,
                            cols - 1,
                        ))
                    elif final == 'A':
                        cr = max(0, cr - (parts[0] if parts else 1))
                    elif final == 'B':
                        cr = min(rows - 1, cr + (parts[0] if parts else 1))
                    elif final == 'C':
                        cc = min(cols - 1, cc + (parts[0] if parts else 1))
                    elif final == 'D':
                        cc = max(0, cc - (parts[0] if parts else 1))
                    elif final == 'G':  # CHA
                        cc = max(0, min(
                            (parts[0] if parts else 1) - 1, cols - 1,
                        ))
                    elif final == 'K':  # EL
                        mode = parts[0] if parts else 0
                        if mode == 0:
                            for c in range(cc, cols):
                                grid[cr][c] = ' '
                        elif mode == 1:
                            for c in range(cc + 1):
                                grid[cr][c] = ' '
                        elif mode == 2:
                            grid[cr] = [' '] * cols
                    elif final == 'J':  # ED
                        mode = parts[0] if parts else 0
                        if mode == 0:
                            for c in range(cc, cols):
                                grid[cr][c] = ' '
                            for r in range(cr + 1, rows):
                                grid[r] = [' '] * cols
                        elif mode == 1:
                            for r in range(cr):
                                grid[r] = [' '] * cols
                            for c in range(cc + 1):
                                grid[cr][c] = ' '
                        elif mode in (2, 3):
                            grid = [[' '] * cols for _ in range(rows)]
                    # m (SGR), l, h, etc. — style/mode sequences, skip
                    i = j + 1
                    continue
                elif nch == ']':
                    # OSC: ESC ] ... BEL or ST
                    j = i + 2
                    while j < n and text[j] != '\x07':
                        if (
                            text[j] == '\x1b'
                            and j + 1 < n
                            and text[j + 1] == '\\'
                        ):
                            j += 1
                            break
                        j += 1
                    i = j + 1
                    continue
                else:
                    i += 2
                    continue
            elif ch == '\n':
                cr = min(rows - 1, cr + 1)
                cc = 0
            elif ch == '\r':
                cc = 0
            elif ch == '\x08':
                cc = max(0, cc - 1)
            elif ord(ch) >= 0x20:
                if cr < rows and cc < cols:
                    grid[cr][cc] = ch
                    cc += 1
                    if cc >= cols:
                        cc = 0
                        cr = min(rows - 1, cr + 1)
            i += 1

        lines = [''.join(r).rstrip() for r in grid]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
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
