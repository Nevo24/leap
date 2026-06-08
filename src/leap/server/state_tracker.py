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
import os
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

# Minimum time a turn must have been RUNNING before the footer-based idle
# detection (idle_indicator_patterns) may conclude idle.  Skips the idle
# footer that can render for a frame at turn start before the working
# indicator appears.  Only affects providers that set
# idle_indicator_patterns (GitHub Copilot).
_IDLE_INDICATOR_GRACE: float = 2.0


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
        cwd: Optional[str] = None,
        tag: str = '',
    ) -> None:
        self._signal_file = signal_file
        self._auto_send_mode = auto_send_mode
        self._clock = clock or time.time
        self._provider = provider or get_provider()
        # Captured once; immune to runtime chdir.  Used by the
        # transcript-aware "still running" check to locate the per-cwd
        # transcript directory for cwd-bound CLIs (Claude/Gemini/Cursor).
        self._cwd: str = cwd if cwd is not None else os.getcwd()
        self._tag: str = tag
        # Storage dir derived from signal_file: <storage>/sockets/<tag>.signal
        self._storage_dir: Path = signal_file.parent.parent

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
        # True iff there's a real in-flight query — set on
        # ``on_send`` (Leap-dispatched message) and on ``on_input``
        # Enter (user typed Enter directly into Claude's input).
        # Reset on every transition to IDLE.  Distinct from
        # ``_seen_user_input`` (which is set for ANY input, including
        # paste echoes) — this flag is the clean "Claude is
        # processing a real submitted query" signal that the
        # dispatcher uses to decide whether the current RUNNING
        # state is real or phantom (paste-echo / cursor blink).
        self._query_in_flight: bool = False
        # Tracks whether ``on_input`` is currently scanning bytes
        # inside a bracketed paste (between ``\x1b[200~`` and
        # ``\x1b[201~``).  Persists across ``on_input`` calls because
        # the input filter chunks at arbitrary boundaries — a paste
        # can start in one ``on_input`` and finish in the next.
        # Used to skip Enter-detection on ``\r`` / ``\n`` bytes that
        # are paste content, not real submits.
        self._in_bracketed_paste: bool = False
        # True from the moment the user answers a mid-turn dialog
        # (Enter from NEEDS_PERMISSION / NEEDS_INPUT — e.g.
        # AskUserQuestion, which is excluded from hook auto-approve so it
        # is always answered by hand) until the turn next reaches IDLE.
        # Answering moves the state WAITING→RUNNING, but Claude then
        # resumes the SAME turn, and its first post-answer output (the
        # model's first token) can lag several seconds — while the
        # dialog-dismissal render emits a tiny burst of output almost
        # immediately.  That dismissal burst defeats the
        # ``max(_last_output_time, _running_since)`` rebase, so the 5 s
        # cursor+silence running→idle fallback then misfires on the
        # first-token gap and lets the auto-sender flush a queued message
        # INTO the still-running turn (confirmed in the wild).  While
        # this flag is set the 5 s fallback is suppressed; the turn is
        # ended only by a real Stop-hook signal or the 60 s safety
        # timeout.  Reset on every transition to IDLE and on ``on_send``.
        self._awaiting_resume_after_prompt: bool = False

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
        lines than the live screen — BUT only when the live screen does
        not already contain dialog content.  If the dialog is on the live
        screen, use it regardless of line counts: the saved snapshot may
        be denser (full Claude TUI with conversation history) but won't
        contain the actual dialog options the user needs to answer.

        Must be called with _screen_lock held.
        """
        snapshot = self._get_display_lines()
        if self._last_running_snapshot:
            live_text = ''.join(snapshot).replace(' ', '').replace('\n', '')
            live_has_dialog = self._provider.has_dialog_indicator(live_text)
            if live_has_dialog:
                _log.debug(
                    'prompt snapshot: live screen has dialog - using live screen',
                )
            else:
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

    def _screen_has_running_indicator(self) -> bool:
        """Return True if the screen shows a provider 'busy' indicator
        (e.g. Claude's "Compacting conversation…").

        The idle-prompt footer WINS over the running indicator: some TUIs
        (GitHub Copilot) leave a stale "<verb>  esc cancel" status line on
        screen after a tool/question finishes, which keeps matching the
        running indicator even though the input prompt is already back.
        When the provider's ``idle_indicator_patterns`` is on the bottom
        rows, the session is idle, not running - so a lingering "esc
        cancel" can't trap it in RUNNING (the "answering a question
        sticks in Running" bug).

        A dialog/question footer ALSO wins, for the same reason: Copilot
        renders a per-step permission prompt ("Do you want to edit…? …
        enter to select · esc to cancel") with a "● Creating files  esc
        cancel" working line co-displayed BELOW the box.  The working
        "esc cancel" must not win there, or the dialog is never detected
        and an Always-mode auto-approve sequence sticks RUNNING after the
        first prompt.  Awaiting-the-user beats busy.

        Must be called with ``_screen_lock`` held.
        """
        patterns = self._provider.running_indicator_patterns
        if not patterns:
            return False
        idle_pats = self._provider.idle_indicator_patterns
        if idle_pats:
            filled = [ln for ln in self._get_display_lines() if ln.strip()]
            tail = ''.join(filled[-5:]).replace(' ', '')
            if any(p.decode('utf-8', errors='replace') in tail
                   for p in idle_pats):
                return False
            # Dialog/question footer in the tail -> awaiting user, not
            # busy (see docstring).  Same patterns the footer-detector
            # promotes on, so detection and this guard stay consistent.
            if self._provider.is_dialog_certain(tail) or any(
                p.decode('utf-8', errors='replace') in tail
                for p in self._provider.input_dialog_patterns
            ):
                return False
        compact = self._get_screen_text().replace(' ', '').replace('\n', '')
        return any(
            p.decode('utf-8', errors='replace') in compact
            for p in patterns
        )

    def _transcript_says_running(self) -> bool:
        """True iff the provider's transcript proves the agent is still
        in a tool loop after ``_running_since``.

        Used as a final gate before flipping RUNNING → IDLE: if the
        screen-based heuristics or hook signal claim idle but the
        transcript shows an unanswered ``tool_use`` from the current
        turn, the flip is blocked.

        Lock-free file I/O; caller must NOT hold ``_screen_lock`` or
        ``_lock`` (avoid pinning state mutation behind disk reads).
        """
        try:
            return self._provider.transcript_says_running(
                since=self._running_since,
                cwd=self._cwd,
                tag=self._tag,
                storage_dir=self._storage_dir,
            )
        except Exception:
            # Defensive: never let transcript parsing kill the tracker.
            _log.debug(
                'transcript_says_running raised; falling back to False',
                exc_info=True,
            )
            return False

    def _transcript_says_interrupted(self) -> bool:
        """True iff the provider's transcript proves the user cancelled
        the agent loop after ``_running_since`` — i.e. a
        ``[Request interrupted by user]`` entry was written above the
        current turn's most recent assistant tool_use.

        Used as a *positive* signal alongside the screen-pattern check
        when flipping RUNNING → INTERRUPTED.  Catches the case where
        the Ink TUI's redraw cleared "Interrupted" out of pyte's
        rendered buffer before the on_output handler observed it —
        without this, the cursor+silence and signal=idle fallbacks
        degrade to IDLE and the auto-sender immediately dispatches
        the queued message into Claude's "What should Claude do
        instead?" prompt as if no interrupt had happened.

        Same lock-free I/O contract as :meth:`_transcript_says_running`.
        """
        try:
            return self._provider.transcript_says_interrupted(
                since=self._running_since,
                cwd=self._cwd,
                tag=self._tag,
                storage_dir=self._storage_dir,
            )
        except Exception:
            _log.debug(
                'transcript_says_interrupted raised; falling back '
                'to False',
                exc_info=True,
            )
            return False

    def _post_answer_grace_holds(self, silence_ref: Optional[float]) -> bool:
        """True iff a mid-turn dialog was just answered (``on_input``
        armed ``_awaiting_resume_after_prompt``) and we're still within
        the resume grace — so the *heuristic* idle fallbacks must keep
        the session RUNNING rather than concluding idle and letting the
        auto-sender flush a queued message into the resuming turn.

        Answering an `AskUserQuestion` / permission prompt moves the
        state WAITING→RUNNING, but Claude resumes the SAME turn and its
        first post-answer output can lag seconds; the cursor+silence
        fallbacks (both the running→idle one and the waiting→idle one a
        stale-footer re-promotion can reach) would otherwise misfire on
        that gap.  The grace is capped at the provider's safety-silence
        timeout (default ``SAFETY_SILENCE_TIMEOUT``), measured from
        *silence_ref*, so a genuinely hung post-answer turn (missing Stop
        hook) still recovers via the normal idle fallbacks once the cap
        elapses.  The authoritative ``signal=idle`` (Stop hook) path does
        NOT consult this — only the unreliable heuristics do, so a real
        turn end still idles promptly.

        ``silence_ref`` is ``Optional`` because the waiting→idle caller
        passes ``self._waiting_since``, which a concurrent ``on_input``
        answer can null out between this block's outer guard and here;
        treat ``None`` as "grace does not hold" rather than risk a
        ``clock - None`` crash.
        """
        if not self._awaiting_resume_after_prompt or silence_ref is None:
            return False
        grace_timeout = (
            self._provider.silence_timeout
            if self._provider.silence_timeout is not None
            else SAFETY_SILENCE_TIMEOUT
        )
        return (self._clock() - silence_ref) <= grace_timeout

    # -- Public API -----------------------------------------------------------

    @property
    def provider(self) -> CLIProvider:
        """The CLI provider used for pattern matching."""
        return self._provider

    def on_input(self, data: bytes) -> None:
        """Called when the user types in the server terminal.

        Handles:
        - Escape/Ctrl+C in non-IDLE states → sets _interrupt_pending
          (IDLE has nothing to interrupt — the CLI just clears its
          input box.  Setting the flag in IDLE would let ambient
          ``Interrupted`` substrings in conversational scrollback
          false-trigger the INTERRUPTED state on the next on_output.)
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
        # Bracketed-paste awareness: ``\r`` / ``\n`` between
        # ``\x1b[200~`` and ``\x1b[201~`` is paste content, not a
        # real Enter submit.  Persists across calls (paste can span
        # multiple ``on_input`` chunks).
        in_paste = self._in_bracketed_paste

        # Scan the data byte-by-byte for interrupt signals and content.
        i = 0
        while i < len(data):
            b = data[i]

            # Inside bracketed paste, all bytes count as "input"
            # but \r / \n / Esc / Ctrl+C are paste data, not real
            # keypresses — don't fire Enter / interrupt detection.
            if in_paste:
                # Watch for the end marker.
                if (b == 0x1b and i + 5 < len(data)
                        and data[i:i + 6] == b'\x1b[201~'):
                    in_paste = False
                    i += 6
                    continue
                has_real_input = True
                has_non_interrupt_input = True
                i += 1
                continue

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
                # Bracketed paste start? — switch to in-paste mode.
                if (i + 5 < len(data)
                        and data[i:i + 6] == b'\x1b[200~'):
                    in_paste = True
                    i += 6
                    continue
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
                elif 0x20 <= data[i + 1] <= 0x7e:
                    # ESC + printable ASCII byte — Meta/Alt key combo
                    # (e.g. Alt+B = \x1bb for word-back, Alt+F = \x1bf
                    # for word-forward, ESC M for line-up, etc.).  These
                    # are *not* bare Escape keypresses — terminals bundle
                    # the modifier and key into the same read, so the
                    # second byte landing in the same chunk is the
                    # disambiguator.  Without this branch, every Alt+B
                    # / Alt+F press while RUNNING used to set
                    # ``_interrupt_pending = True`` and the next time
                    # the rendered screen contained "Interrupted"
                    # anywhere (conversational text, code, commit
                    # messages) the running→interrupted transition
                    # would false-fire.  Counts as real user input
                    # (so the "pure terminal event" filter doesn't
                    # drop it), but not as ``has_non_interrupt_input``
                    # because Meta combos are typically navigation,
                    # not text-typing that should block auto-resume.
                    has_real_input = True
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

        # Persist bracketed-paste state for the next call — pastes
        # can span ``on_input`` chunk boundaries, so the in-paste
        # flag carries over.
        self._in_bracketed_paste = in_paste

        # Pure terminal events (focus, mouse) with no real user input
        # — skip entirely to avoid false flag updates.
        if not has_real_input:
            _log.debug(
                'ON_INPUT filtered terminal event len=%d data=%r',
                len(data), data[:20],
            )
            return

        # Only arm the flag when there's actually something to interrupt.
        # Esc/Ctrl+C in IDLE has no semantic effect on Claude (the prompt
        # box just clears) — but with the flag set, any subsequent on_output
        # whose compact screen contains the substring "Interrupted" (e.g.
        # the literal word in conversation, code, or commit messages) would
        # false-trigger the idle→interrupted transition guarded by
        # `_interrupt_pending and not _suppress_stale_interrupt` in
        # `_handle_idle_output`.  Also neutralises chunk-split arrow-keys
        # (\x1b alone arriving as the tail of a read, before the [A
        # continuation lands) when the user is at the idle prompt
        # navigating history.
        if is_interrupt and self._state != CLIState.IDLE:
            self._interrupt_pending = True
            # A user interrupt cancels the post-answer resume grace: it
            # exists to let Claude resume an *answered* turn, which is the
            # opposite of "stop".  Disarm it here so the subsequent
            # INTERRUPTED handling (and its →idle dismissal) isn't held in
            # RUNNING by the grace.  Self-interrupts that never reach
            # on_input are additionally covered by the PROMPT_STATES guard
            # on the waiting→idle routing.
            self._awaiting_resume_after_prompt = False
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
        # Also covers IDLE: a false running→idle may leave us in IDLE
        # while a permission dialog is still on screen.  If the user
        # answers before the Notification hook signal arrives, we need
        # _user_responded=True to survive the IDLE→NEEDS_PERMISSION
        # transition (which only resets it when coming from non-IDLE).
        if self._state in WAITING_STATES or self._state == CLIState.IDLE:
            self._user_responded = True
            _log.debug('ON_INPUT _user_responded=True')

        # Enter in idle, interrupted, or waiting states → RUNNING.  Covers:
        # * server-terminal typing in idle (slash commands, new prompts)
        # * typing a reply into Claude's "What should Claude do?"
        #   interrupt dialog — without this path the user stays stuck
        #   in interrupted until the 60s safety timeout.
        # * answering a permission/input dialog — the monitor would
        #   otherwise show "Permission" for the entire task duration
        #   (until the Stop hook fires) instead of flipping to Running
        #   as soon as the user submits their answer.
        if has_enter and self._state in (
            CLIState.IDLE, CLIState.INTERRUPTED,
            CLIState.NEEDS_PERMISSION, CLIState.NEEDS_INPUT,
        ):
            _log.debug(
                'ON_INPUT Enter in %s → running', self._state,
            )
            # Answering a permission/question dialog (Enter from a PROMPT
            # state) must NOT reset the pyte screen.  Claude advances to
            # the next view (a later tab of a multi-question
            # AskUserQuestion) via an Ink INCREMENTAL repaint that never
            # re-emits the unchanged footer.  Resetting wipes that footer,
            # and for the ~5 s until Claude's next full re-render the live
            # screen then has no dialog footer - which drives two bugs,
            # both confirmed against a real session log:
            #   * the cursor+silence check reads "no dialog" and flips
            #     RUNNING->idle, falsely marking the still-pending question
            #     as done (and letting the auto-sender fire into it), and
            #   * the ↑/↓ input filter's screen_has_active_dialog() also
            #     reads "no dialog" and steals the arrows for history
            #     recall, so the next question can't be navigated by arrow.
            # Leaving pyte intact lets the footer survive the incremental
            # repaint, so both the promotion (->needs_permission) and the
            # arrow check stay correct.  The running->needs_permission
            # promotion path already skips _reset_screen() for the same
            # reason; mirror it here.  IDLE (a fresh prompt) and INTERRUPTED
            # (an interrupt reply) DO reset - there the prior screen is
            # stale scrollback we want gone, with nothing rendered
            # incrementally on top of it.
            from_prompt = self._state in PROMPT_STATES
            if self._state == CLIState.INTERRUPTED:
                self._suppress_stale_interrupt = True
            self._running_since = self._clock()
            self._interrupt_pending = False
            self._user_responded = False
            self._user_input_since_idle = False
            self._query_in_flight = True
            # Answering a mid-turn dialog: Claude resumes the SAME turn,
            # so suppress the 5 s cursor+silence idle fallback until a
            # real end signal arrives (see the flag's definition).  A
            # fresh Enter from IDLE / a reply from INTERRUPTED is a new
            # turn, not a dialog answer, so it does NOT arm the grace.
            self._awaiting_resume_after_prompt = from_prompt
            with self._screen_lock:
                if not from_prompt:
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
        self._query_in_flight = True
        self._running_since = self._clock()
        self._interrupt_pending = False
        self._user_responded = False
        self._user_input_since_idle = False
        self._awaiting_resume_after_prompt = False
        # Acquire _screen_lock first to maintain consistent lock
        # ordering with on_output (screen_lock → lock).
        with self._screen_lock:
            # Footer-driven providers (Copilot) must KEEP the screen synced
            # with the terminal here: resetting blanks pyte, but the CLI
            # repaints only incrementally - so when one dialog replaces
            # another (e.g. a free-text question right after the user
            # answers a menu question), the blanked screen never refills
            # and the footer-detector goes blind to the follow-up dialog,
            # leaving it stuck reading RUNNING/IDLE.  The post-send grace on
            # the footer-detector's dialog promotion handles the stale
            # just-answered footer instead.  Other providers reset as before.
            if not self._provider.idle_indicator_patterns:
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

            _log.debug(
                'ON_OUTPUT idle (startup) compact_tail=%r',
                compact[-120:],
            )

            is_trust = any(
                p.decode('utf-8', errors='replace') in compact
                for p in trust_patterns
            )
            is_dialog = self._provider.is_dialog_certain(compact)

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

        # -- Running indicator detection --
        # The CLI can be actively processing a long-running operation
        # with no hook to signal it (Claude's "Compacting conversation…"
        # during /compact and auto-compact).  Detect the on-screen
        # label and move idle → running immediately so the monitor
        # reflects reality and queued messages don't auto-send into
        # a compacting CLI.
        if self._screen_has_running_indicator():
            _log.debug(
                'ON_OUTPUT idle→running (running indicator on screen)',
            )
            self._running_since = now
            self._interrupt_pending = False
            self._user_input_since_idle = False
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
            return

        # -- Mid-session proactive dialog detection --
        # Some Claude tools (notably AskUserQuestion / "Proceed?" prompts)
        # do NOT fire PreToolUse hook — only Stop, so the state tracker
        # transitions running→idle while the dialog is still visible and
        # the auto-sender loop never fires `_try_auto_approve`.
        # Match the same shape used by the running→needs_permission
        # proactive check (see ``get_state`` ~line 1300): scan only the
        # last 5 non-blank rows (the dialog footer/menu region) with the
        # strict ``is_dialog_certain``.  Restricting to the tail keeps
        # response text that happens to quote dialog phrases mid-screen
        # from false-triggering — only patterns at the bottom matter.
        # NOTE: deliberately do NOT call _reset_screen() here.  The
        # waiting→idle self-dismissal check at ``get_state`` ~line 1207
        # uses ``has_dialog_indicator`` on the LIVE screen to decide
        # whether the dialog has been answered.  If we wiped the screen,
        # the next idle TUI heartbeat (cursor blink, partial repaint)
        # would update _last_output_time without re-rendering the full
        # dialog — the dismissal check would then see "no patterns" and
        # falsely revert to idle while the dialog is still on screen.
        all_lines = self._get_display_lines()
        filled = [ln for ln in all_lines if ln.strip()]
        tail_compact = ''.join(filled[-5:]).replace(' ', '')
        if self._provider.is_dialog_certain(tail_compact):
            _log.debug(
                'ON_OUTPUT idle→needs_permission '
                '(proactive dialog detection, mid-session)',
            )
            self._prompt_snapshot = all_lines
            self._interrupt_pending = False
            with self._lock:
                self._state = CLIState.NEEDS_PERMISSION
                self._waiting_since = self._clock()
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
            # Footer-driven providers (Copilot) must KEEP the screen here:
            # resetting wipes pyte, and the CLI repaints only incrementally
            # (it thinks the footer is already drawn), so the idle footer
            # never re-renders into pyte and the footer-detector can never
            # see "/ commands" to leave INTERRUPTED -> the session sticks
            # in INTERRUPTED forever.  Other providers reset as before.
            if not self._provider.idle_indicator_patterns:
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
                self._last_running_snapshot = []
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
            # Keep the screen for footer-driven providers (see
            # running→interrupted above) so the idle footer survives for
            # the INTERRUPTED→idle transition.
            if not self._provider.idle_indicator_patterns:
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
                self._last_running_snapshot = []
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
                self._awaiting_resume_after_prompt = False
                with self._screen_lock:
                    self._reset_screen()
                    self._prompt_snapshot = []
                    self._last_running_snapshot = []
            return CLIState.IDLE

        with self._lock:
            current = self._state

        # The post-answer resume grace (armed when the user answers a
        # mid-turn dialog) only applies while we run through that one
        # turn.  Once we're back at IDLE the turn is over — drop it here
        # so it can't leak into a following auto-resumed turn and wrongly
        # suppress that turn's cursor+silence idle fallback.
        if current == CLIState.IDLE:
            self._awaiting_resume_after_prompt = False

        # -- Read signal file --
        new_state = self._read_signal_state()
        if new_state and new_state != current:
            # Ignore self-written "interrupted" signals.
            if new_state == CLIState.INTERRUPTED:
                _log.debug(
                    'GET_STATE signal=interrupted but current=%s - '
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
                # Running indicator guard: a between-turns auto-compact
                # immediately follows the Stop hook that wrote 'idle'.
                # Honouring the signal here would make the session read
                # as idle for the entire compaction — stay running
                # until the indicator disappears.
                with self._screen_lock:
                    running_indicator = self._screen_has_running_indicator()
                if running_indicator:
                    _log.debug(
                        'GET_STATE signal=idle but running indicator '
                        'on screen - keeping running',
                    )
                    try:
                        self._signal_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return current

                # Transcript guard: provider's per-session transcript
                # shows an unanswered tool_use from the current turn.
                # Catches Stop-hook-fires-mid-tool races (the model still
                # has work pending) that the screen guards above don't
                # cover.  See ClaudeProvider.transcript_says_running.
                if self._transcript_says_running():
                    _log.debug(
                        'GET_STATE signal=idle but transcript shows '
                        'unanswered tool_use - keeping running',
                    )
                    try:
                        self._signal_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return current

                # Convert to INTERRUPTED if EITHER:
                #   (a) ``_interrupt_pending`` + interrupt pattern on
                #       pyte's rendered screen, OR
                #   (b) the transcript records a ``[Request interrupted
                #       by user]`` entry above the current turn's most
                #       recent assistant tool_use.
                # Path (a) alone misses the common Ink-TUI redraw case
                # where pyte never observes "Interrupted" in its buffer
                # (verified on a real session: 0 has_Interrupted=True
                # observations across 147k RUNNING ON_OUTPUT polls
                # spanning the entire interrupt window).
                # Path (b) doesn't require ``_interrupt_pending`` —
                # the transcript is independent evidence and the timestamp
                # filter in ``transcript_says_interrupted`` already
                # restricts to the current turn.
                has_pattern = False
                has_transcript_interrupt = False
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
                # Transcript check is independent of the pending flag
                # — covers races where Esc fires while ``pty_alive``
                # is briefly False (which silently clears
                # ``_interrupt_pending`` in the pty-dead path).
                if not has_pattern:
                    has_transcript_interrupt = (
                        self._transcript_says_interrupted()
                    )

                if (self._interrupt_pending and has_pattern) \
                        or has_transcript_interrupt:
                    _log.debug(
                        'GET_STATE signal=idle + %s → interrupted',
                        'interrupt_pending + pattern on screen'
                        if has_pattern
                        else 'transcript interrupt marker',
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

                    # Stop hook fires for some Claude tools (notably
                    # AskUserQuestion / "Proceed?") that leave a dialog
                    # awaiting user input — the agent is "done" from the
                    # hook's perspective but the user still has to answer.
                    # If a dialog footer is in the bottom 5 rows, treat
                    # the signal as a running→needs_permission transition.
                    # Mirrors the cursor+silence proactive check at
                    # ~line 1300, but for the immediate signal path.
                    with self._screen_lock:
                        all_lines = self._get_display_lines()
                    filled = [ln for ln in all_lines if ln.strip()]
                    compact_tail = ''.join(filled[-5:]).replace(' ', '')
                    if self._provider.is_dialog_certain(compact_tail):
                        _log.debug(
                            'GET_STATE signal=idle but dialog on '
                            'screen → needs_permission',
                        )
                        # Defensive reset (matches the cursor+silence
                        # proactive check at ~line 1330): clear any
                        # stale _user_responded so the next waiting→idle
                        # signal isn't accepted before the user has
                        # actually answered THIS dialog.
                        self._user_responded = False
                        if self._trust_dialog_phase:
                            self._seen_user_input = False
                            self._trust_dialog_phase = False
                        with self._lock:
                            self._state = CLIState.NEEDS_PERMISSION
                            self._waiting_since = self._clock()
                        self._user_input_since_idle = False
                        with self._screen_lock:
                            self._prompt_snapshot = all_lines
                            # Do NOT reset the screen here.  See
                            # _handle_idle_output's mid-session proactive
                            # check for the rationale: the dialog must
                            # remain in the live buffer so the
                            # waiting→idle self-dismissal check at
                            # ~line 1207 can correctly tell whether the
                            # user has answered.
                        try:
                            self._signal_file.unlink(missing_ok=True)
                        except OSError:
                            pass
                        return CLIState.NEEDS_PERMISSION

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
                        self._last_running_snapshot = []
                    return CLIState.IDLE
                else:
                    _log.debug(
                        'GET_STATE signal=idle but %s without '
                        'user_responded - ignoring',
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
                # needs_input (elicitation_dialog).  Dialogs may use
                # different UI formats: standard footer ("Enter to
                # select / Esc to cancel") or numbered menus (❯ 1. Yes).
                # Delegate to provider.has_dialog_indicator() which
                # knows all its dialog formats.
                # Skip for providers with empty dialog_patterns (Codex)
                # — they have no PTY-based dialog detection and rely
                # entirely on hook signals.
                if (
                    current == CLIState.IDLE
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
                    has_dialog = self._provider.has_dialog_indicator(
                        compact,
                    )
                    if not has_dialog:
                        _log.debug(
                            'GET_STATE signal=%s from idle but no '
                            'dialog patterns on screen - ignoring '
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

                # RUNNING-state stale-signal guard.  Distinct rationale
                # from the IDLE guard above: the only path that reaches
                # RUNNING with an incoming permission/input signal that
                # is *actually stale* is when the user pressed Enter to
                # answer a prompt — that transition resets both the
                # pyte screen and ``_last_running_snapshot``, so an
                # empty-screen + empty-snapshot pair is the signature
                # of a freshly-answered dialog whose hook is arriving
                # late (see ``test_stale_notification_rejected_after_
                # enter_from_permission``).
                #
                # We deliberately do NOT require dialog patterns on
                # screen here.  In multi-agent runs the parent stays
                # RUNNING for the entire turn (no ``Stop`` hook fires
                # for subagents) and ``_last_running_snapshot`` never
                # gets populated.  When the subagent's permission
                # ``Notification`` hook fires before pyte has processed
                # the dialog footer bytes, the live screen has the
                # subagent's *prior* output but no footer pattern yet —
                # the old pattern-only check rejected those valid
                # signals, leaving auto-approve stuck waiting for the
                # 5 s cursor+silence fallback (or indefinitely, if the
                # TUI kept emitting bytes).  Treat any non-empty screen
                # as evidence that the running state isn't a freshly-
                # reset post-Enter snapshot, and let the signal through.
                if (
                    current == CLIState.RUNNING
                    and new_state in (
                        CLIState.NEEDS_PERMISSION,
                        CLIState.NEEDS_INPUT,
                    )
                    and self._provider.dialog_patterns
                ):
                    # Read both pieces under the same lock so a concurrent
                    # on_input(Enter) can't reset one but not the other
                    # between our reads — without this, a screen-cleared-
                    # but-snapshot-not-yet-cleared interleaving could let
                    # a genuinely stale signal slip past the guard (or
                    # vice versa).  Mirrors the IDLE guard above, which
                    # also reads ``_last_running_snapshot`` inside the
                    # screen lock.
                    with self._screen_lock:
                        screen_text = self._get_screen_text()
                        screen_compact = screen_text.replace(
                            ' ', '',
                        ).replace('\n', '')
                        snapshot_empty = not self._last_running_snapshot
                    if not screen_compact and snapshot_empty:
                        _log.debug(
                            'GET_STATE signal=%s from running with '
                            'empty screen + empty snapshot - ignoring '
                            'stale notification (post-Enter race)',
                            new_state,
                        )
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
                # Preserve _user_responded when coming from IDLE: a false
                # running→idle may have left us in IDLE while a dialog
                # was still on screen.  If the user answered during that
                # IDLE window, on_input() set _user_responded=True; we
                # must not wipe it here or the cursor-hidden / waiting→
                # idle exit paths will never fire.  All other source
                # states (RUNNING, INTERRUPTED) reset it as before.
                if current != CLIState.IDLE:
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

        # -- Footer-driven transitions for non-quiescent CLIs (Copilot) --
        # Some CLIs animate their idle prompt and emit PTY output
        # continuously even when idle (GitHub Copilot repaints its input
        # box in a focused terminal), so every silence-based heuristic
        # below never fires - the session sticks in whatever non-idle
        # state it was in (RUNNING after a turn, INTERRUPTED after a
        # Ctrl+C, NEEDS_PERMISSION after a self-dismissed dialog).  When
        # the provider sets idle_indicator_patterns, drive transitions
        # OUT of a non-idle state purely off the bottom-row footer,
        # independent of output activity:
        #   * running indicator present ("esc cancel")  -> still working
        #   * RUNNING + a certain dialog footer          -> needs_permission
        #   * idle indicator present ("/ commands...")   -> idle (turn
        #     ended / dialog dismissed / interrupt acknowledged)
        # RUNNING waits out a short grace first (the idle footer can flash
        # for one frame at turn start before the working footer renders);
        # the waiting/interrupted states already happened, so they idle as
        # soon as the prompt is back.  Gated on idle_indicator_patterns,
        # so every built-in provider except Copilot is unaffected.
        if (
            self._provider.idle_indicator_patterns
            and current != CLIState.IDLE
        ):
            with self._screen_lock:
                running_indicator = self._screen_has_running_indicator()
                footer_lines = self._get_display_lines()
            if not running_indicator:
                filled = [ln for ln in footer_lines if ln.strip()]
                compact_tail = ''.join(filled[-5:]).replace(' ', '')
                # RUNNING must clear a short grace before the footer is
                # trusted for ANY transition.  Two reasons: the idle/dialog
                # footer can flash for one frame at turn start, and - because
                # footer-driven providers keep the screen across on_send
                # (see on_send) - a just-answered dialog's footer lingers in
                # pyte until the CLI repaints the next state.  Gating both
                # the dialog promotion and the idle drop on the grace skips
                # that stale frame, so answering one dialog can't re-detect
                # itself (a phantom needs_permission would auto-approve).
                # Non-RUNNING states (interrupted) transition immediately.
                past_grace = (
                    current != CLIState.RUNNING
                    or (self._running_since > 0
                        and (self._clock() - self._running_since)
                        > _IDLE_INDICATOR_GRACE)
                )
                # A RUNNING turn paused on a footer menu is awaiting the
                # user.  A question (ask_user, "enter to confirm") is
                # needs_input - so ALWAYS-mode auto-approve leaves it for
                # the user; a tool-permission prompt ("enter to select")
                # is needs_permission (auto-approvable).  Checked
                # input-first so a question is never mis-promoted to
                # needs_permission and auto-answered.
                promote_to: Optional[str] = None
                if current == CLIState.RUNNING and past_grace:
                    # ANY input-dialog pattern (Copilot has one per
                    # question footer shape: "enter to confirm" /
                    # "enter to submit") marks a question -> needs_input.
                    input_pats = self._provider.input_dialog_patterns
                    if input_pats and any(
                        p.decode('utf-8', errors='replace') in compact_tail
                        for p in input_pats
                    ):
                        promote_to = CLIState.NEEDS_INPUT
                    elif self._provider.is_dialog_certain(compact_tail):
                        promote_to = CLIState.NEEDS_PERMISSION
                if promote_to is not None:
                    _log.debug(
                        'GET_STATE running→%s '
                        '(footer dialog; idle-indicator provider)',
                        promote_to,
                    )
                    self._interrupt_pending = False
                    self._user_responded = False
                    with self._lock:
                        self._state = promote_to
                        self._waiting_since = self._clock()
                    self._user_input_since_idle = False
                    with self._screen_lock:
                        self._prompt_snapshot = footer_lines
                        self._last_running_snapshot = list(footer_lines)
                    return promote_to
                idle_on_screen = any(
                    p.decode('utf-8', errors='replace') in compact_tail
                    for p in self._provider.idle_indicator_patterns
                )
                if idle_on_screen and past_grace:
                    _log.debug(
                        'GET_STATE %s→idle '
                        '(idle footer on screen; idle-indicator provider)',
                        current,
                    )
                    # Mirror the other INTERRUPTED→idle exits: suppress the
                    # lingering "cancelled" banner so it can't re-trigger.
                    if current == CLIState.INTERRUPTED:
                        self._suppress_stale_interrupt = True
                    self._interrupt_pending = False
                    self._user_responded = False
                    with self._lock:
                        self._state = CLIState.IDLE
                        self._waiting_since = None
                    self._user_input_since_idle = False
                    with self._screen_lock:
                        self._last_running_snapshot = list(footer_lines)
                        self._reset_screen()
                        self._prompt_snapshot = []
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
            # `dialogs_hide_cursor` providers (GitHub Copilot) keep the
            # cursor HIDDEN while the dialog itself is on screen, so a
            # hidden cursor here does NOT mean "moved past the dialog" —
            # it would falsely flip a still-pending prompt to RUNNING the
            # moment the user types a printable char into it.  Their
            # answer reaches RUNNING via the on_input Enter path (and
            # auto-approve via on_send) instead, so skip this heuristic.
            and not self._provider.dialogs_hide_cursor
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

        # -- Waiting → idle via indicator-gone + cursor visible + silence --
        # Mirror of the running→idle cursor+silence fallback, for the
        # cases where no hook fires to end a waiting state:
        # * INTERRUPTED + double-Escape: user dismissed the interrupt
        #   prompt entirely.  Gated on _user_responded so a mid-TUI
        #   redraw (pattern briefly off screen) doesn't false-fire.
        # * NEEDS_PERMISSION / NEEDS_INPUT + CLI self-dismissed: tool
        #   timed out, dialog auto-cancelled, or a replacement dialog
        #   rendered with different content.  No gate — the 5s
        #   silence already proves the CLI isn't in the middle of a
        #   redraw.
        # Disabled for full-screen TUIs (cursor always hidden) and
        # while trust_dialog_phase is active (startup output would
        # otherwise look like dialog dismissal).
        if (
            current in WAITING_STATES
            and not self._provider.cursor_hidden_while_idle
            and not self._trust_dialog_phase
            and self._last_output_time > 0
            # Require output AFTER entering the waiting state.  When the
            # state is entered via the cursor+silence heuristic (or via a
            # Notification hook signal), _reset_screen() is called and the
            # fresh pyte screen is empty.  The Ink TUI does not re-render
            # the dialog because it is already on screen from its own
            # perspective.  Without this guard, the very next poll sees an
            # empty screen (no dialog indicator) and falsely concludes the
            # dialog was dismissed.  Only trigger dismissal detection once
            # the TUI has emitted at least one byte after we entered this
            # state — that proves the screen represents a real update.
            and self._waiting_since is not None
            and self._last_output_time > self._waiting_since
            and (self._clock() - self._last_output_time) > 5.0
        ):
            with self._screen_lock:
                cursor_visible = not self._screen.cursor.hidden
                screen_text = self._get_screen_text()
                compact = screen_text.replace(' ', '').replace('\n', '')
            if cursor_visible:
                indicator_gone = False
                if current == CLIState.INTERRUPTED:
                    if self._user_responded:
                        pattern_str = self._provider.interrupted_pattern.decode(
                            'utf-8', errors='replace',
                        )
                        indicator_gone = pattern_str not in compact
                else:  # NEEDS_PERMISSION / NEEDS_INPUT
                    indicator_gone = (
                        not self._provider.has_dialog_indicator(compact)
                    )
                if indicator_gone:
                    # Post-answer guard (mirror of the running→idle
                    # grace).  If the user just answered this dialog, the
                    # indicator disappearing means Claude RESUMED the turn
                    # (the answered footer cleared) — NOT that the session
                    # went idle.  This branch is reachable post-answer
                    # because the running→idle block above can re-promote
                    # RUNNING→NEEDS_PERMISSION off the still-on-screen
                    # answered footer (its own grace check sits after that
                    # promotion), landing us here a few seconds later when
                    # the footer finally clears.  Concluding idle here
                    # would flush a queued message into the live turn —
                    # the exact bug.  Route to RUNNING instead, so the
                    # Stop hook (whose running→idle path needs no
                    # _user_responded — which the answer cleared) and the
                    # running→idle grace decide the real end.  Restricted
                    # to PROMPT_STATES: the flag can still be set from an
                    # earlier dialog answer when the state has since become
                    # INTERRUPTED (answer → grace → user hits Esc), and an
                    # interrupt dismissal must idle, not resume — so the
                    # grace must NOT hijack the INTERRUPTED branch above.
                    if (current in PROMPT_STATES
                            and self._post_answer_grace_holds(
                                self._waiting_since)):
                        _log.debug(
                            'GET_STATE %s→running (dialog answered + '
                            'indicator gone - Claude resuming, not idle)',
                            current,
                        )
                        self._running_since = self._clock()
                        self._interrupt_pending = False
                        # Mirror the on_input dialog-answer transition: do
                        # NOT _reset_screen().  The indicator is already
                        # gone from the live screen, so there's no stale
                        # footer to clear (is_dialog_certain, stricter than
                        # the has_dialog_indicator that just returned
                        # False, can't re-promote off it); and resetting
                        # would desync pyte from Ink's incremental repaint,
                        # the very failure mode the answer/promotion paths
                        # avoid.  Just clear the stale dialog snapshots.
                        with self._screen_lock:
                            self._prompt_snapshot = []
                            self._last_running_snapshot = []
                        with self._lock:
                            self._state = CLIState.RUNNING
                            self._waiting_since = None
                        return CLIState.RUNNING
                    _log.debug(
                        'GET_STATE %s→idle '
                        '(indicator gone + cursor visible + silence)',
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
                    with self._screen_lock:
                        self._reset_screen()
                        self._prompt_snapshot = []
                        self._last_running_snapshot = []
                    try:
                        self._signal_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return CLIState.IDLE

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
            # Footer-idle providers (Copilot) drive idle→running off the
            # running indicator appearing on screen (_handle_idle_output),
            # not the cursor — their idle-prompt animation toggles the
            # cursor and would false-resume idle→running here.
            and not self._provider.idle_indicator_patterns
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
        #
        # Silence is measured from ``max(_last_output_time,
        # _running_since)`` so that pre-RUNNING silence does not count.
        # Without this baseline, answering an AskUserQuestion /
        # permission dialog with Enter (which moves WAITING→RUNNING
        # but does not refresh ``_last_output_time``) instantly trips
        # the 5 s threshold off the silence accumulated while the
        # dialog was on screen — the transcript guard can't help
        # because the next assistant entry hasn't been written yet
        # (the latest one is the OLD tool_use call at
        # ``ts <= _running_since``) and the auto-sender would flush a
        # queued message before Claude resumes producing output.
        # Using the max preserves the hung-after-send case: once the
        # baseline is ``_running_since``, a real 5 s / 60 s of
        # post-transition silence still triggers the fallbacks.
        silence_baseline = max(
            self._last_output_time, self._running_since,
        )
        if (
            current == CLIState.RUNNING
            and not self._provider.cursor_hidden_while_idle
            and silence_baseline > 0
            and (self._clock() - silence_baseline) > 5.0
        ):
            with self._screen_lock:
                cursor_visible = not self._screen.cursor.hidden
                running_indicator = self._screen_has_running_indicator()
            # Long-running op in progress (e.g. "Compacting
            # conversation…") — skip the idle fallback.
            if running_indicator:
                return current
            # Proactive permission/input dialog detection.  A *certain*
            # dialog footer in the bottom rows promotes RUNNING →
            # NEEDS_PERMISSION seconds before any Notification hook and
            # avoids a false "idle" flash.  Whether it may fire with the
            # cursor HIDDEN is provider-specific: for most Ink CLIs a
            # hidden cursor means "still processing", so dialog-ish text
            # on screen is transient render rather than a real prompt and
            # the check must stay cursor-gated (see
            # TestCursorSilenceNeedsCursor).  Full-screen TUIs like GitHub
            # Copilot instead HIDE the cursor while their menu dialogs are
            # up, so for them (dialogs_hide_cursor) the cursor isn't a
            # reliable "working" signal and a certain footer is trusted
            # regardless — without this a hookless Copilot session stayed
            # stuck in RUNNING for the whole dialog (confirmed live).
            # Strict is_dialog_certain() on the last 5 non-blank rows
            # keeps response text that merely mentions "Esc to cancel"
            # from false-promoting.
            with self._screen_lock:
                all_lines = self._get_display_lines()
            filled = [ln for ln in all_lines if ln.strip()]
            compact_tail = ''.join(filled[-5:]).replace(' ', '')
            if (cursor_visible or self._provider.dialogs_hide_cursor) \
                    and self._provider.is_dialog_certain(compact_tail):
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
                    # Reuse the lines already captured above instead of
                    # re-reading the screen.
                    self._prompt_snapshot = all_lines
                    self._last_running_snapshot = list(all_lines)
                    # Deliberately do NOT _reset_screen() here.  The
                    # dialog (e.g. AskUserQuestion, which fires no
                    # permission hook) is on the live screen right now,
                    # and the waiting→idle dismissal checks below read the
                    # LIVE screen to decide whether it has been answered.
                    # Resetting wipes pyte, and Ink — believing the dialog
                    # is already drawn — then only partially repaints, so
                    # the footer never returns to the buffer.  The
                    # dismissal checks would see "no dialog" and flip the
                    # session idle while the user still has to answer (the
                    # Permission↔Idle oscillation).
                return CLIState.NEEDS_PERMISSION

            if cursor_visible:
                # Transcript guard before falling to idle: a long
                # silent tool call (Bash, WebFetch with no progress
                # output) leaves the cursor visible and the screen
                # quiet without ending the agent loop.  If the
                # transcript shows an unanswered tool_use, stay
                # running until the tool actually returns.
                if self._transcript_says_running():
                    _log.debug(
                        'GET_STATE cursor+silence would idle but '
                        'transcript shows tool_use - keeping running',
                    )
                    return current

                # User-interrupt guard before falling to idle: when
                # ``transcript_says_running`` is False *because* the
                # user cancelled mid-tool-use, the transcript records
                # ``[Request interrupted by user]`` above the in-turn
                # assistant entry.  Without this branch we'd flip to
                # IDLE and the auto-sender would dispatch the next
                # queued message into Claude's "What should Claude do
                # instead?" prompt — visually indistinguishable from
                # the interrupt being silently ignored.
                if self._transcript_says_interrupted():
                    _log.debug(
                        'GET_STATE cursor+silence + transcript '
                        'interrupt marker → interrupted',
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

                # Just answered a mid-turn dialog and Claude hasn't truly
                # resumed yet: this silence is the model's first-token
                # latency, not end-of-turn.  The dialog-dismissal render
                # already moved _last_output_time past _running_since (so
                # the rebase gate opened), the transcript can't confirm
                # running because its only assistant entry is the dialog's
                # tool_use at ts <= _running_since, and the running
                # indicator only matches "Compacting conversation" — so
                # nothing else here is holding us in RUNNING.  Idling now
                # would flush a queued message into the live turn.  Stay
                # RUNNING and let the Stop hook end the turn at the right
                # moment.  Cap the grace at the safety-silence timeout
                # (NOT an unconditional ``return``) so a genuinely hung
                # post-answer turn with no Stop hook still recovers via the
                # safety fallback below — this block runs before it, so
                # returning early past the cap would starve that net.
                if self._post_answer_grace_holds(silence_baseline):
                    _log.debug(
                        'GET_STATE cursor+silence would idle but a '
                        'dialog was just answered - keeping running '
                        '(awaiting post-answer resume; %.1fs silent)',
                        self._clock() - self._last_output_time,
                    )
                    return current

                # Interactive UI guard.  An is_dialog_certain miss above
                # does NOT mean "idle" — it can also be a slash-command
                # picker (/model, /resume, /mcp, …) or a dialog whose
                # footer isn't the strict "Enter to select / Esc to
                # cancel" form (e.g. Claude's "Esc to close" / "Enter to
                # approve" / multi-select footers).  All of those leave
                # the idle input box GONE from the bottom of the screen.
                # But "idle box absent" alone is too broad — plain response
                # text (a numbered list, a long body ending in "> ") also
                # lacks the box yet must still idle.  A real picker/dialog
                # additionally shows EITHER a ❯/› selection cursor on a focused
                # option (has_selection_cursor) OR a nav/dismiss footer at the
                # bottom (has_interactive_footer — e.g. the /agents tabbed view,
                # which has no cursor); plain response text has neither.  So
                # require: idle box absent AND (selection cursor OR nav footer).
                # Idling a real UI here would (a) _reset_screen(), blanking it
                # so screen_has_active_dialog() then reads "no dialog" and ↑/↓
                # get stolen for history recall (the "arrows stuck in a picker
                # after a few seconds" bug), and (b) let the auto-sender flush
                # a queued message straight into the open UI.  When the user
                # dismisses the UI the idle box returns and the normal idle
                # path fires; a genuinely silent in-flight tool is already
                # held above by transcript_says_running().  No-op for providers
                # without these detectors (base defaults: is_idle_prompt_visible
                # True / has_selection_cursor / has_interactive_footer False) —
                # Claude-only.
                if (not self._provider.is_idle_prompt_visible(filled)
                        and (self._provider.has_selection_cursor(filled)
                             or self._provider.has_interactive_footer(filled))):
                    _log.debug(
                        'GET_STATE cursor+silence would idle but the idle '
                        'prompt is absent and a selection cursor / nav footer '
                        'is on screen (picker/dialog) - keeping running',
                    )
                    return current

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
        # Same ``max(_last_output_time, _running_since)`` baseline as
        # the cursor+silence path above — pre-RUNNING silence (e.g.
        # from a long-deliberation dialog wait) must not count toward
        # the safety timeout, or a 60 s+ dialog would force-idle the
        # session the instant the user answers.  Hung-after-send is
        # still covered: silence ticks from ``_running_since`` onward.
        safety_baseline = max(
            self._last_output_time, self._running_since,
        )
        if current == CLIState.RUNNING and safety_baseline > 0:
            silence = self._clock() - safety_baseline
            if silence > silence_timeout:
                with self._screen_lock:
                    running_indicator = self._screen_has_running_indicator()
                if running_indicator:
                    # Long-running op still on screen — don't force
                    # idle; wait for the indicator to disappear.
                    return current
                # Transcript guard: a tool that runs > silence_timeout
                # without emitting output (e.g. a slow Bash test, a
                # WebFetch with no progress reporting) is the canonical
                # case that fires this safety fallback.  If the
                # transcript shows we're still in a tool_use, the agent
                # is genuinely waiting — don't force idle.
                if self._transcript_says_running():
                    _log.debug(
                        'GET_STATE safety timeout %.1fs but transcript '
                        'shows tool_use - keeping running',
                        silence,
                    )
                    return current
                # User-interrupt guard: same rationale as the
                # cursor+silence path above.  See that branch for the
                # full reasoning.
                if self._transcript_says_interrupted():
                    _log.debug(
                        'GET_STATE safety timeout %.1fs + transcript '
                        'interrupt marker → interrupted',
                        silence,
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
                # Screen guard (PROMPT states only): a hookless dialog
                # like AskUserQuestion writes no needs_permission signal,
                # so without this the safety net force-demotes a dialog
                # that is *still rendered on screen* to idle every
                # SAFETY_WAITING_TIMEOUT seconds — the status then
                # oscillates Permission↔Idle for as long as the dialog
                # sits unanswered.  The promotion paths leave the dialog
                # in pyte's buffer (no reset), so the live screen is an
                # honest signal: footer still there ⇒ prompt still
                # pending ⇒ keep.  ``has_dialog_indicator`` matches the
                # indicator-gone fallback above, so both demotion paths
                # agree on "is a dialog visible".  Scoped to PROMPT_STATES
                # so a stuck INTERRUPTED still recovers via this timeout.
                dialog_on_screen = False
                if current in PROMPT_STATES:
                    with self._screen_lock:
                        compact = self._get_screen_text().replace(
                            ' ', '').replace('\n', '')
                    dialog_on_screen = (
                        self._provider.has_dialog_indicator(compact)
                    )
                if (signal_state == current or self._trust_dialog_phase
                        or dialog_on_screen):
                    _log.debug(
                        'GET_STATE waiting timeout %s %.1fs but %s - '
                        'keeping', current, silence,
                        'trust dialog active'
                        if self._trust_dialog_phase
                        else 'signal confirms'
                        if signal_state == current
                        else 'dialog still on screen',
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
                        self._last_running_snapshot = []
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

    def screen_has_active_dialog(self) -> bool:
        """True iff the live pyte screen shows something interactive at
        the bottom — a permission dialog OR a slash-command picker —
        that should receive ↑/↓ instead of Leap's history recall.

        Used by the server's ↑/↓ input filter to skip history-recall
        interception when something interactive is on screen but the
        state tracker hasn't flipped out of ``RUNNING`` — e.g.
        ``AskUserQuestion``'s question dialog fires no Notification
        hook (cursor+silence fallback flips state up to 5 s later), and
        slash-command pickers (``/resume``, ``/mcp``, ``/agents``,
        ``/config``, ``/effort``, ``/model``, …) fire no hook at all
        and leave state ``RUNNING`` for the entire time they're open.

        Three checks, in order:

        0. **Generic selection-dialog** — ``provider.screen_shows_selection_dialog``
           (CLI-agnostic, base class): a numbered ``›``/``❯`` selection
           cursor (``› 1.``) or a confirm/cancel/navigate footer
           accompanied by a cursor.  Checked first and independent of
           ``dialog_patterns``, so it catches Codex (whose
           ``dialog_patterns`` is empty) and non-permission pickers in
           Gemini/Cursor.  Safe to be permissive: this method is used
           only for the ↑/↓ filter, so a false positive merely lets the
           arrow reach the CLI's native handling.

        1. **Permission-dialog footer** — strict ``is_dialog_certain``
           on the compact form of the last 5 non-blank rows.  Catches
           the standard ``Entertoselect`` + ``Esctocancel`` permission
           dialog and the ``❯1.`` numbered-menu cursor.  Kept strict
           because the same predicate gates state transitions where
           false positives stick state in ``needs_permission`` for 60 s.
           Skipped for providers with no ``dialog_patterns``.

        2. **Idle prompt absent** — provider's ``is_idle_prompt_visible``
           checks for the standard idle input-box structure at the
           bottom of the screen.  When that structure is gone,
           *something* is taking it over — a slash-command picker, the
           trust dialog at startup, a permission dialog that didn't
           match the strict footer, etc.  This is structural so new
           Claude pickers are caught without enumerating their footer
           text.  Providers that don't implement detection inherit the
           default True (assume idle visible) — for them this leg is a
           no-op and behaviour comes from checks #0 and #1.
        """
        with self._screen_lock:
            all_lines = self._get_display_lines()
        filled = [ln for ln in all_lines if ln.strip()]
        # Generic selection-dialog detection (footer / numbered cursor), checked
        # first and independent of dialog_patterns. This is what catches Codex
        # (empty dialog_patterns) and non-permission pickers in Gemini/Cursor,
        # so ↑/↓ navigate the dialog instead of being stolen for history recall.
        if self._provider.screen_shows_selection_dialog(filled):
            return True
        if not self._provider.dialog_patterns:
            return False
        tail_compact = ''.join(filled[-5:]).replace(' ', '')
        if self._provider.is_dialog_certain(tail_compact):
            return True
        return not self._provider.is_idle_prompt_visible(filled)

    def get_prompt_output(self) -> str:
        """Return PTY output from the last permission/input prompt.

        The pyte screen is reset at the moment of the running→prompt
        transition, so the live screen accumulates ONLY the dialog's own
        render bytes from that point on — making it a cleaner source
        than the pre-reset snapshot, which can mix overlapping TUI
        frames (Ink doesn't always clear-to-end-of-screen between
        renders, so cells from earlier frames bleed through).

        Preference order while in a prompt state:
        1. Live screen, if it certainly contains a full dialog (Claude
           has re-rendered the dialog after reset).  Uses the strict
           ``is_dialog_certain`` check rather than the lenient
           ``has_dialog_indicator`` — otherwise a stray footer fragment
           like ``'…Esc to cancel'`` left over after reset would override
           the captured snapshot with a near-empty screen, hiding the
           menu options.  Hit on the trust folder dialog at startup, where
           Claude doesn't re-render the menu rows after the reset.
        2. The snapshot captured at transition time (fallback for the
           moment between reset and Claude's first dialog bytes).
        3. Live screen, if the snapshot is empty (defensive fallback
           for paths that transitioned to a waiting state without
           capturing one).
        """
        with self._screen_lock:
            snapshot = self._prompt_snapshot
            if self._state in WAITING_STATES:
                live_lines = self._get_display_lines()
                live_compact = ''.join(live_lines).replace(' ', '')
                if self._provider.is_dialog_certain(live_compact):
                    snapshot = live_lines
                elif not snapshot:
                    snapshot = live_lines
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
