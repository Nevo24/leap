"""Tests for CLIStateTracker event-driven state machine."""

import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List

import pytest

from leap.cli_providers.claude import ClaudeProvider
from leap.cli_providers.codex import CodexProvider
from leap.cli_providers.states import CLIState, WAITING_STATES
from leap.server.state_tracker import CLIStateTracker as ClaudeStateTracker
from leap.utils.constants import SAFETY_SILENCE_TIMEOUT, SAFETY_WAITING_TIMEOUT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tracker(
    tmp_path: Path,
    t: List[float],
    auto_send_mode: str = 'pause',
    provider: object = None,
) -> ClaudeStateTracker:
    """Create a tracker with fake clock and a signal file in *tmp_path*.

    ``cwd`` is set to *tmp_path* so the transcript-aware "still running"
    check looks for transcripts under a unique slug with no real files —
    keeping unit tests hermetic from the developer's actual ``~/.claude``.
    """
    signal_file = tmp_path / "test.signal"
    kwargs = dict(
        signal_file=signal_file,
        auto_send_mode=auto_send_mode,
        clock=lambda: t[0],
        cwd=str(tmp_path),
        tag='test',
    )
    if provider is not None:
        kwargs['provider'] = provider
    return ClaudeStateTracker(**kwargs)


def write_signal(tracker: ClaudeStateTracker, state: str) -> None:
    """Write a JSON signal file that the tracker will read."""
    tracker._signal_file.write_text(json.dumps({"state": state}))


def feed_screen_text(tracker: ClaudeStateTracker, text: str) -> None:
    """Feed text into the tracker's pyte screen (simulates PTY output).

    Uses ANSI escape sequences to position and write text so it appears
    on the virtual screen for pattern matching.
    """
    # Move to top-left and clear screen, then write text
    esc = f'\x1b[H\x1b[2J{text}'
    tracker.on_output(esc.encode('utf-8'))


def feed_with_hidden_cursor(tracker: ClaudeStateTracker, text: str) -> None:
    """Feed text with cursor hidden (simulates TUI rendering)."""
    esc = f'\x1b[?25l\x1b[H\x1b[2J{text}'
    tracker.on_output(esc.encode('utf-8'))


def feed_with_visible_cursor(tracker: ClaudeStateTracker, text: str) -> None:
    """Feed text with cursor visible (simulates idle prompt)."""
    esc = f'\x1b[?25h\x1b[H\x1b[2J{text}'
    tracker.on_output(esc.encode('utf-8'))


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

    def test_on_send_clears_interrupt_pending(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running (Esc only arms flag in non-IDLE)
        tracker.on_input(b'\x1b')  # Escape → interrupt pending
        assert tracker._interrupt_pending is True
        tracker.on_send()
        assert tracker._interrupt_pending is False


# ---------------------------------------------------------------------------
# on_input Enter → running (all providers)
# ---------------------------------------------------------------------------

class TestEnterInIdle:
    def test_enter_in_idle_triggers_running(self, tmp_path: Path) -> None:
        """Enter at idle prompt triggers running (server-terminal typing)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'

    def test_on_send_still_triggers_running(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        assert tracker.get_state(pty_alive=True) == 'running'


# ---------------------------------------------------------------------------
# Enter in waiting states → running (Fix 2)
# ---------------------------------------------------------------------------

class TestEnterInWaitingStates:
    """Enter in NEEDS_PERMISSION/NEEDS_INPUT immediately transitions to RUNNING.

    Before Fix 2, the monitor showed "Permission" for the entire duration
    of the subsequent task (until the Stop hook fired).
    """

    def test_needs_permission_enter_triggers_running_immediately(
        self, tmp_path: Path,
    ) -> None:
        """Enter at a permission dialog flips state to running without
        waiting for the Stop hook."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission

        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'

    def test_needs_input_enter_triggers_running_immediately(
        self, tmp_path: Path,
    ) -> None:
        """Enter at an input elicitation dialog flips state to running."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'What should I name this?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_input')
        tracker.get_state(pty_alive=True)  # → needs_input

        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'


# ---------------------------------------------------------------------------
# Signal file transitions
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
        # Dialog patterns must be on screen for the guard to accept
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_signal_file_needs_input(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'What should I name this?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'needs_input'

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
# Interrupt detection via _interrupt_pending flag
# ---------------------------------------------------------------------------

class TestInterruptPendingFlag:
    def test_escape_in_running_sets_interrupt_pending(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.on_input(b'\x1b')
        assert tracker._interrupt_pending is True

    def test_ctrl_c_in_running_sets_interrupt_pending(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.on_input(b'\x03')
        assert tracker._interrupt_pending is True

    def test_escape_in_idle_does_not_set_interrupt_pending(
        self, tmp_path: Path,
    ) -> None:
        """Esc in IDLE has no interrupt semantics — the CLI just clears
        its input box.  Without this guard, ambient ``Interrupted``
        substrings in conversational scrollback (e.g. the literal word
        in a previous reply) could combine with the sticky flag and
        false-trigger INTERRUPTED on the next on_output."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x1b')
        assert tracker._interrupt_pending is False
        assert tracker.current_state == 'idle'

    def test_ctrl_c_in_idle_does_not_set_interrupt_pending(
        self, tmp_path: Path,
    ) -> None:
        """Same as Esc — Ctrl+C in IDLE shouldn't arm interrupt detection."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x03')
        assert tracker._interrupt_pending is False
        assert tracker.current_state == 'idle'

    def test_idle_with_ambient_interrupted_text_stays_idle(
        self, tmp_path: Path,
    ) -> None:
        """Regression: the bug we're fixing — ambient text containing
        the substring ``Interrupted`` (capitalised, matching Claude's
        ``interrupted_pattern``) is on the pyte screen, the user
        accidentally presses Esc at the idle prompt, the next on_output
        runs ``_handle_idle_output`` which checks ``_interrupt_pending
        and pattern in compact``.  Under the old code the flag was set
        unconditionally and the false-trigger fired.  Under the new
        code Esc in IDLE leaves the flag at False so the transition
        is impossible.

        The wording mirrors the real bug report — a Claude reply that
        referred to "Interrupted state" / "Interrupted by user" while
        analysing this very issue."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Mark seen_user_input so _handle_idle_output runs the interrupt
        # path (the startup-dialog branch returns early otherwise).
        tracker.on_input(b'x')
        feed_screen_text(
            tracker,
            'Discussing Interrupted state and how the suppression flag '
            'gates re-entry from stale Interrupted text in scrollback.',
        )
        # Sanity: the substring is present at this point.
        with tracker._screen_lock:
            screen = tracker._get_screen_text()
        compact = screen.replace(' ', '').replace('\n', '')
        assert 'Interrupted' in compact

        tracker.on_input(b'\x1b')
        # Re-feed to trigger another _handle_idle_output pass.
        feed_screen_text(
            tracker,
            'Discussing Interrupted state and how the suppression flag '
            'gates re-entry from stale Interrupted text in scrollback.',
        )
        assert tracker.current_state == 'idle'
        assert tracker._interrupt_pending is False

    def test_regular_input_does_not_set_interrupt_pending(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        assert tracker._interrupt_pending is False

    def test_alt_b_in_running_does_not_set_interrupt_pending(
        self, tmp_path: Path,
    ) -> None:
        """Alt+B (\\x1bb, readline word-back) is a Meta-key combo, not a
        bare Escape keypress.  Terminals bundle the modifier + key into
        the same read; the second byte landing in the chunk is the
        disambiguator.  Old code only recognised 0x40-0x5f (uppercase)
        as two-byte ESC sequences, so lowercase letters fell through
        to the standalone-Escape branch and incorrectly armed
        ``_interrupt_pending``.  Live regression: a user navigating with
        Alt+B/Alt+F many times had the flag set, then minutes later a
        rendered screen happened to contain "Interrupted" anywhere
        (a Claude reply, code, commit message) and ``_handle_running_output``
        fired the running→interrupted transition.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.on_input(b'\x1bb')  # Alt+B
        assert tracker._interrupt_pending is False
        assert tracker.current_state == 'running'

    def test_alt_f_in_running_does_not_set_interrupt_pending(
        self, tmp_path: Path,
    ) -> None:
        """Same as Alt+B but for Alt+F (\\x1bf, readline word-forward)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.on_input(b'\x1bf')  # Alt+F
        assert tracker._interrupt_pending is False
        assert tracker.current_state == 'running'

    def test_esc_then_uppercase_letter_does_not_set_interrupt_pending(
        self, tmp_path: Path,
    ) -> None:
        """ESC + uppercase letter was always handled (0x40-0x5f branch);
        pin the existing behaviour so the range-widening fix doesn't
        regress it."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.on_input(b'\x1bM')  # ESC M — line-up control function
        assert tracker._interrupt_pending is False

    def test_esc_then_digit_does_not_set_interrupt_pending(
        self, tmp_path: Path,
    ) -> None:
        """Digits are also part of the printable-ASCII Meta range —
        Alt+1, Alt+2 etc. shouldn't read as interrupts."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.on_input(b'\x1b1')  # Alt+1
        assert tracker._interrupt_pending is False

    def test_double_esc_still_sets_interrupt_pending(
        self, tmp_path: Path,
    ) -> None:
        """\\x1b\\x1b — Esc Esc, second byte is 0x1b (NOT printable
        ASCII).  Should fall through to the standalone-Escape branch
        and still arm ``_interrupt_pending``.  Guards the fix from
        over-broadening: only printable second bytes are Meta combos."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.on_input(b'\x1b\x1b')
        assert tracker._interrupt_pending is True

    def test_alt_letter_with_ambient_interrupted_text_stays_running(
        self, tmp_path: Path,
    ) -> None:
        """End-to-end regression of the live bug: user is in RUNNING,
        rapidly Alt+B / Alt+F-navigates text in the input box, then
        the rendered screen happens to contain the literal substring
        "Interrupted" (e.g. Claude is generating text that references
        interrupt handling).  Old code: each Alt+letter armed
        ``_interrupt_pending``, the screen text matched
        ``interrupted_pattern``, and ``_handle_running_output`` fired
        running→interrupted.  Fixed: Alt+letter no longer arms the
        flag, so no transition fires no matter what's on screen."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        # User rapidly navigates with Alt+B / Alt+F (10× each).
        for _ in range(10):
            tracker.on_input(b'\x1bb')
            tracker.on_input(b'\x1bf')
        assert tracker._interrupt_pending is False
        # Claude renders a reply that mentions "Interrupted" somewhere.
        feed_screen_text(
            tracker,
            'Discussing how Interrupted state is detected via the flag.',
        )
        assert tracker.current_state == 'running'

    def test_idle_signal_with_interrupt_pending_and_pattern_goes_interrupted(
        self, tmp_path: Path,
    ) -> None:
        """Interrupt pending + pattern on screen → interrupted."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.on_input(b'\x1b')  # interrupt pending
        # CLI shows "Interrupted" on screen
        feed_screen_text(tracker, 'Interrupted')
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_idle_signal_with_interrupt_pending_but_no_pattern_goes_idle(
        self, tmp_path: Path,
    ) -> None:
        """Interrupt pending but NO pattern on screen → idle.

        The user pressed Escape but the CLI ignored it and finished
        normally.  No 'Interrupted' appeared.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.on_input(b'\x1b')  # interrupt pending
        # CLI finishes normally — no "Interrupted" on screen
        feed_screen_text(tracker, 'Done processing')
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_idle_signal_without_interrupt_pending_goes_idle(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_interrupt_pending_cleared_on_transition(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')
        assert tracker._interrupt_pending is True
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # triggers transition
        assert tracker._interrupt_pending is False

    def test_csi_u_ctrl_c_sets_interrupt_pending(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running (so CSI input is processed)
        # Kitty CSI u for Ctrl+C: \x1b[3u
        tracker.on_input(b'\x1b[3u')
        assert tracker._interrupt_pending is True

    def test_csi_u_escape_sets_interrupt_pending(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # Kitty CSI u for Escape: \x1b[27u
        tracker.on_input(b'\x1b[27u')
        assert tracker._interrupt_pending is True


# ---------------------------------------------------------------------------
# _user_responded flag (waiting state protection)
# ---------------------------------------------------------------------------

class TestUserRespondedFlag:
    def test_input_in_waiting_state_sets_user_responded(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'What should I do?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_input')
        tracker.get_state(pty_alive=True)  # → needs_input
        tracker.on_input(b'x')
        assert tracker._user_responded is True

    def test_idle_signal_blocked_without_user_responded(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'What should I do?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_input')
        tracker.get_state(pty_alive=True)  # → needs_input
        # Signal idle without user responding
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'needs_input'

    def test_idle_signal_accepted_with_user_responded(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        write_signal(tracker, 'needs_input')
        tracker.get_state(pty_alive=True)  # → needs_input
        tracker.on_input(b'x')  # user responded
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_interrupted_protected_from_idle_without_user_responded(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')  # pattern on screen
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → interrupted
        # Try to signal idle without responding
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_interrupted_yields_to_idle_with_user_responded(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → interrupted
        tracker.on_input(b'1')  # user responded
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_interrupted_protected_from_needs_input_without_user_responded(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')
        feed_screen_text(tracker, 'Interrupted')
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → interrupted
        # Notification hook writes needs_input (race)
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_user_responded_cursor_hidden_goes_running(
        self, tmp_path: Path,
    ) -> None:
        """When user answers a permission prompt in the terminal and the
        CLI starts processing (cursor hidden), state should transition
        from needs_permission → running without waiting for Stop hook."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # Feed dialog patterns so Late Notification Guard accepts signal
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission
        tracker.on_input(b'1')  # user answered
        # CLI starts processing — cursor hidden in output
        feed_with_hidden_cursor(tracker, 'Processing...')
        # Poll detects cursor hidden + user_responded → running
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_user_responded_cursor_visible_stays_waiting(
        self, tmp_path: Path,
    ) -> None:
        """If the user responded but cursor is still visible (dialog
        still showing), state should remain needs_permission."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission
        tracker.on_input(b'x')  # user typed something
        # Dialog still showing — cursor visible output
        feed_screen_text(tracker, 'Select an option')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_no_user_responded_cursor_hidden_stays_waiting(
        self, tmp_path: Path,
    ) -> None:
        """Cursor hidden without _user_responded should NOT trigger
        the transition — could be a TUI redraw during the dialog."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission
        # Cursor hidden output but user hasn't responded
        feed_with_hidden_cursor(tracker, 'Rendering...')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_user_responded_cursor_hidden_stays_waiting_while_dialog_shown(
        self, tmp_path: Path,
    ) -> None:
        """A multi-option AskUserQuestion HIDES the cursor the whole time
        it is open, and navigating it (Tab between questions, arrows
        between options) sets _user_responded on the first keypress.  The
        cursor-hidden poll heuristic must NOT flip to running while the
        dialog footer is still on screen: doing so drops the PROMPT-state
        arrow passthrough in the input filter and resets the live dialog
        out of pyte, so up/down get stolen for history recall and the
        dialog becomes un-navigable by arrow (the recurring 'arrows stuck
        in a multi-option question' report)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        dialog = (
            'How should the search find rulings?  '
            '1. AIText  2. ai-gate  3. both  '
            'Enter to select  Esc to cancel'
        )
        feed_screen_text(tracker, dialog)
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission
        # User NAVIGATES the dialog with Tab — sets _user_responded.
        tracker.on_input(b'\t')
        assert tracker._user_responded is True
        # Dialog is still on screen, but (being a selection dialog) it
        # hides the cursor while open.
        feed_with_hidden_cursor(tracker, dialog)
        # Must stay needs_permission: the user is navigating, not done.
        # (Contrast test_user_responded_cursor_hidden_goes_running, where
        # the dialog footer is gone and the flip to running is correct.)
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_interrupted_responded_cursor_hidden_goes_running(
        self, tmp_path: Path,
    ) -> None:
        """INTERRUPTED → running when user types and cursor hides.
        Must also set _suppress_stale_interrupt."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')
        # on_output detected interrupt_pending + pattern → interrupted
        # (state tracker wrote "interrupted" to signal file internally)
        assert tracker.current_state == 'interrupted'
        # Delete signal file — real scenario: the self-written
        # "interrupted" signal is ignored by get_state (line 768)
        tracker._signal_file.unlink(missing_ok=True)
        tracker.on_input(b'1')  # user types new input
        feed_with_hidden_cursor(tracker, 'Working...')
        assert tracker.get_state(pty_alive=True) == 'running'
        assert tracker._suppress_stale_interrupt is True


# ---------------------------------------------------------------------------
# Interrupt pattern on pyte screen
# ---------------------------------------------------------------------------

class TestInterruptPatternOnScreen:
    def test_interrupted_in_running_with_flag(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'interrupted'

    def test_interrupted_in_running_without_flag_stays_running(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # No escape/ctrl+c pressed
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'running'

    def test_idle_esc_does_not_trigger_interrupted(
        self, tmp_path: Path,
    ) -> None:
        """Under Option A, Esc in IDLE no longer arms the interrupt
        flag — so the pattern appearing afterward cannot drive a false
        idle→interrupted transition.  This is the exact regression
        path that bit users when conversational scrollback contained
        the substring ``interrupted`` (e.g. "Re: your interrupted
        question") and an accidental Esc was pressed at the prompt."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_input(b'\x1b')  # Esc in IDLE — flag stays False
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'idle'
        assert tracker._interrupt_pending is False

    def test_needs_input_corrected_to_interrupted(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        write_signal(tracker, 'needs_input')
        tracker.get_state(pty_alive=True)  # → needs_input
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'interrupted'

    def test_interrupted_pattern_split_across_lines(self, tmp_path: Path) -> None:
        """Pattern that wraps across pyte screen lines is still detected.

        On a narrow terminal, 'Interrupted' could end up split at a line
        boundary.  compact_full (spaces+newlines removed) handles this.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')  # interrupt pending
        # Resize to narrow screen so text wraps
        tracker.on_resize(24, 15)
        # Position cursor near end of line, write text that wraps
        # Col 10 + "Interrupted" (11 chars) → wraps at col 15
        tracker.on_output(b'\x1b[1;10HInterrupted')
        assert tracker.current_state == 'interrupted'


class TestConfirmedPatternCrossLine:
    """Confirmed interrupt pattern uses compact_lines (newlines preserved)
    to prevent false positives from cross-line text concatenation."""

    def test_cross_line_text_no_false_positive(self, tmp_path: Path) -> None:
        """'Conversation' on line 1 + 'interrupted' on line 2 should
        NOT form 'Conversationinterrupted' for confirmed pattern."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()
        # Two lines that would concatenate to "Conversationinterrupted"
        # if newlines were removed
        tracker.on_output(
            b'\x1b[1;1HConversation\x1b[2;1Hinterrupted the flow'
        )
        # Should stay running — cross-line match blocked
        assert tracker.current_state == 'running'

    def test_same_line_confirmed_pattern_detected(self, tmp_path: Path) -> None:
        """'Conversation interrupted' on SAME line should be detected."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_input(b'x')
        tracker.on_send()
        # Same line — after space removal: "Conversationinterrupted"
        tracker.on_output(
            b'\x1b[1;1HConversation interrupted - tell the model'
        )
        assert tracker.current_state == 'interrupted'


# ---------------------------------------------------------------------------
# Auto-resume via cursor visibility
# ---------------------------------------------------------------------------

class TestAutoResume:
    def test_cursor_hidden_triggers_running_at_poll(self, tmp_path: Path) -> None:
        """Auto-resume is detected at poll time (get_state), not on_output.

        This avoids false triggers from mid-render cursor-hidden state
        in brief TUI redraws.  By poll time (0.5s), brief redraws have
        completed and cursor is visible again.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        # Enter idle via signal (simulate: was running, now idle)
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle
        # _user_input_since_idle was cleared on transition
        # CLI auto-starts: cursor hidden output
        t[0] = 5.0
        feed_with_hidden_cursor(tracker, 'Processing...')
        # on_output doesn't trigger transition — check at poll time
        assert tracker.current_state == 'idle'
        # Poll detects cursor hidden → running
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_cursor_hidden_blocked_if_user_typed(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle
        # User types in idle (sets _user_input_since_idle)
        tracker.on_input(b'y')
        # Cursor hidden output should NOT trigger running (user typed)
        feed_with_hidden_cursor(tracker, 'Echo of typing')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_cursor_visible_does_not_trigger_running(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)
        feed_with_visible_cursor(tracker, 'Status bar update')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_auto_resume_needs_seen_user_input(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # No user input ever seen (startup)
        feed_with_hidden_cursor(tracker, 'Startup output')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_brief_redraw_does_not_false_trigger(self, tmp_path: Path) -> None:
        """A brief TUI redraw (cursor hide → content → cursor show)
        within one output chunk does not trigger auto-resume."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle
        # Brief redraw: hide cursor, render, show cursor — all in one chunk
        tracker.on_output(
            b'\x1b[?25l\x1b[H\x1b[2JStatus update\x1b[?25h'
        )
        # Cursor is visible after the complete render → no auto-resume
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# Safety fallback timeouts
# ---------------------------------------------------------------------------

class TestSafetyTimeouts:
    def test_silence_timeout_triggers_idle(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Cursor hidden (simulates processing that hung without output)
        tracker.on_output(b'\x1b[?25lsome output')
        t[0] = 1.0 + SAFETY_SILENCE_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_silence_timeout_not_before_deadline(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Hide cursor (simulates active processing with cursor hidden)
        tracker.on_output(b'\x1b[?25lsome output')
        t[0] = 1.0 + SAFETY_SILENCE_TIMEOUT - 1.0
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_cursor_visible_plus_silence_triggers_idle(
        self, tmp_path: Path,
    ) -> None:
        """Running with cursor visible + output silence > 5s → idle.

        Handles /clear sent from queue (on_send → running, but Stop
        hook doesn't fire).  Threshold bumped from 2s to 5s to avoid
        false-idle flicker during long streaming responses.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running (e.g., /clear from queue)
        t[0] = 1.0
        # TUI redraws with cursor visible at the end
        feed_with_visible_cursor(tracker, 'Cleared screen')
        # Wait > 5s of silence
        t[0] = 7.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_cursor_silence_idle_held_while_composing(
        self, tmp_path: Path,
    ) -> None:
        """The reported bug: composing a prompt into a RUNNING session and
        pausing must NOT flip to idle via cursor+silence (false 'finished'
        notification). Same setup as the test above, but with pending input."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(tracker, 'model output...')
        t[0] = 7.0  # > 5s silence, cursor visible
        # Control: without pending input it idles (as the test above).
        assert tracker.get_state(
            pty_alive=True, has_pending_input=False) == 'idle'
        # With the user composing, it stays running.
        t2 = [0.0]
        tr2 = make_tracker(tmp_path, t2)
        tr2.on_send()
        t2[0] = 1.0
        feed_with_visible_cursor(tr2, 'model output...')
        t2[0] = 7.0
        assert tr2.get_state(
            pty_alive=True, has_pending_input=True) == 'running'

    def test_safety_timeout_idle_held_while_composing(
        self, tmp_path: Path,
    ) -> None:
        """The safety-silence timeout (the Codex path, cursor hidden) must
        also not force-idle while the user is composing."""
        # Control: cursor hidden + long silence → safety timeout idles.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'\x1b[?25lworking...')  # cursor hidden
        t[0] = 1.0 + SAFETY_SILENCE_TIMEOUT + 5.0
        assert tracker.get_state(
            pty_alive=True, has_pending_input=False) == 'idle'
        # Composing → held running.
        t2 = [0.0]
        tr2 = make_tracker(tmp_path, t2)
        tr2.on_send()
        t2[0] = 1.0
        tr2.on_output(b'\x1b[?25lworking...')
        t2[0] = 1.0 + SAFETY_SILENCE_TIMEOUT + 5.0
        assert tr2.get_state(
            pty_alive=True, has_pending_input=True) == 'running'

    def test_interactive_ui_guard_is_capped_and_recovers(
        self, tmp_path: Path,
    ) -> None:
        """The on-screen-picker RUNNING-hold (interactive-UI guard: idle box
        absent + a selection cursor / nav footer present) is CAPPED too.  It
        preserves a live picker, but must not wedge RUNNING forever when the
        screen lingers with no output and no Stop hook - the nushi wedge,
        where a label drawn into the idle-box border made is_idle_prompt_visible
        False while the input box's own ❯ tripped has_selection_cursor."""
        # A picker with a bare ❯ cursor (no numbered option, so is_dialog_certain
        # does NOT promote it to needs_permission first) and ≥5 rows (so the
        # "too little content" idle fallback doesn't kick in).
        picker = (
            'Select a session to resume:\r\n'
            '❯ Fix the auth bug\r\n'
            '  Add more tests\r\n'
            '  Refactor the parser\r\n'
            '  Start a fresh session\r\n'
            '  (scroll for more)'
        )
        t = [0.0]
        tr = make_tracker(tmp_path, t)
        tr.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(tr, picker)
        # Sanity: this screen trips the guard, not is_dialog_certain.
        filled = [ln for ln in tr._get_display_lines() if ln.strip()]
        assert not tr._provider.is_idle_prompt_visible(filled)
        assert tr._provider.has_selection_cursor(filled)
        # Within the cap: the picker is preserved (held RUNNING).
        t[0] = 7.0
        assert tr.get_state(pty_alive=True) == 'running'
        # Past the cap: recovers to idle instead of wedging forever.
        t[0] = 1.0 + SAFETY_SILENCE_TIMEOUT + 5.0
        assert tr.get_state(pty_alive=True) == 'idle'

    def test_many_option_dialog_not_reset_so_arrows_stay_navigable(
        self, tmp_path: Path,
    ) -> None:
        """A tall AskUserQuestion / picker with MANY options must NOT be
        idled+reset by the cursor+silence fallback - otherwise the blanked
        screen makes screen_has_active_dialog() read 'no dialog' and the
        next ↑/↓ is stolen for history recall ("arrows dead on a many-option
        question, forced to type the number").

        The shape that broke: the focused ``❯ 1.`` cursor sits on a TOP
        option that has scrolled above the has_selection_cursor tail window,
        and the footer sits one row ABOVE the ``╰──╯`` bottom border, so
        has_interactive_footer (last row only) misses it too.  When
        is_dialog_certain ALSO misses (e.g. a footer that isn't the strict
        ``Enter to select`` form), both legacy positive signals are False and
        the cursor+silence fallback used to idle+reset the live UI - then
        screen_has_active_dialog() read the blanked screen as 'no dialog' and
        the next ↑/↓ was stolen for history recall.  The guard now uses the
        SAME prose-proof detector the ↑/↓ gate trusts (numbered cursor /
        real footer), so it holds RUNNING and the two stay consistent.
        """
        dialog = (
            '⏺ I have a question for you.\r\n'
            '╭────────────────────────────────────────────────╮\r\n'
            '│ Which file should I start with?                  │\r\n'
            '│ ❯ 1. server.py                                   │\r\n'
            '│   2. state_tracker.py                            │\r\n'
            '│   3. claude.py                                   │\r\n'
            '│   4. codex.py                                    │\r\n'
            '│   5. gemini.py                                   │\r\n'
            '│   6. cursor_agent.py                             │\r\n'
            '│   7. base.py                                     │\r\n'
            '│   8. registry.py                                 │\r\n'
            '│   9. client.py                                   │\r\n'
            '│   10. monitor_app.py                             │\r\n'
            '│   Enter to confirm · Esc to cancel               │\r\n'
            '╰────────────────────────────────────────────────╯'
        )
        t = [0.0]
        tr = make_tracker(tmp_path, t)
        tr.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(tr, dialog)
        filled = [ln for ln in tr._get_display_lines() if ln.strip()]
        # The exact failing shape: both legacy positive signals miss it, and
        # is_dialog_certain misses it (this synthetic footer is "Enter to
        # confirm", and the ❯1. cursor scrolled out of the last 5 rows).  Only
        # the prose-proof strict detector (== the ↑/↓ gate) still sees it.
        assert not tr._provider.has_selection_cursor(filled)
        assert not tr._provider.has_interactive_footer(filled)
        assert not tr._provider.is_dialog_certain(
            ''.join(filled[-5:]).replace(' ', ''))
        assert tr._provider.screen_shows_selection_dialog_strict(filled)
        # Past the 5s cursor+silence window: the guard holds RUNNING (screen
        # NOT reset) and the ↑/↓ gate keeps returning True, so arrows survive.
        t[0] = 7.0
        assert tr.get_state(pty_alive=True) == 'running'
        assert tr.screen_has_active_dialog()
        # Still capped: a genuinely abandoned screen recovers after the cap.
        t[0] = 1.0 + SAFETY_SILENCE_TIMEOUT + 5.0
        assert tr.get_state(pty_alive=True) == 'idle'

    def test_prose_affordance_line_still_idles_not_held_running(
        self, tmp_path: Path,
    ) -> None:
        """A hookless response that merely ENDS with a short keyboard-
        affordance line (e.g. ``- Press Enter to confirm``) must still idle -
        it is prose, not a dialog.  The interactive-UI hold uses the
        PROSE-PROOF strict detector, which (unlike the lenient ↑/↓-gate
        variant) rejects a short single-hint line: it requires a numbered
        ``❯ N.`` cursor or a real ``·``-separated / ≥2-hint footer.  Guards
        against the strict-fix regressing into a false 'still running'.
        """
        prose = (
            'Here is what I found in the code:\r\n'
            'The handler resets the screen on the silence path.\r\n'
            '- Press Esc to cancel the current operation\r\n'
            '- Press Enter to confirm\r\n'
            'Let me know if you want me to dig further.'
        )
        t = [0.0]
        tr = make_tracker(tmp_path, t)
        tr.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(tr, prose)
        filled = [ln for ln in tr._get_display_lines() if ln.strip()]
        assert not tr._provider.screen_shows_selection_dialog_strict(filled)
        t[0] = 8.0
        assert tr.get_state(pty_alive=True) == 'idle'

    def test_bullet_and_separator_rows_still_idle_not_held_running(
        self, tmp_path: Path,
    ) -> None:
        """A hookless response ending with ``•`` bullets and/or a
        ``·``-separated status row must still idle - a bare separator with
        NO hint token on the same row is prose (or Claude's own ``Using
        <model> · /model to change`` status line), not a dialog footer.

        Regression: the strict detector's separator leg used to fire with
        ZERO hint tokens (it dropped the lenient version's ``hits == 0``
        guard), so any bullet/middle-dot near the bottom held the session
        RUNNING for the full heuristic cap instead of idling in ~5s.
        """
        prose = (
            'Here is the summary of the changes:\r\n'
            '• state_tracker.py: guard reordered\r\n'
            '• base.py: separator leg requires a hint\r\n'
            '• claude.py: mode-line marker tightened\r\n'
            'Using Sonnet 4.6 · /model to change\r\n'
            'Let me know if you want the diff.'
        )
        t = [0.0]
        tr = make_tracker(tmp_path, t)
        tr.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(tr, prose)
        filled = [ln for ln in tr._get_display_lines() if ln.strip()]
        # No idle box on screen, so the interactive-UI guard consults the
        # strict detector - which must NOT see a dialog in separator-only rows.
        assert not tr._provider.is_idle_prompt_visible(filled)
        assert not tr._provider.screen_shows_selection_dialog_strict(filled)
        # A real footer (hint + separator on one row) still matches.
        assert tr._provider.screen_shows_selection_dialog_strict(
            filled + ['  Enter to set as default · Esc to cancel'])
        t[0] = 8.0
        assert tr.get_state(pty_alive=True) == 'idle'

    def test_codex_interrupted_recovers_via_safety_timeout(
        self, tmp_path: Path,
    ) -> None:
        """A cursor_hidden_while_idle provider (Codex) that enters INTERRUPTED
        must recover to idle via the safety-waiting-timeout - it can't
        self-dismiss INTERRUPTED through the cursor+silence path.  Previously
        the timeout's 'signal confirms' keep fired unconditionally for
        INTERRUPTED (which writes its OWN 'interrupted' signal), so the session
        stuck in INTERRUPTED forever (the 'interrupt sticks' bug).  The keep is
        now scoped to PROMPT_STATES, so the self-written signal no longer
        blocks recovery."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_send()
        t[0] = 1.0
        tracker.on_input(b'\x1b')  # user pressed Esc → interrupt pending
        feed_with_hidden_cursor(
            tracker, 'Conversation interrupted - tell the model what to do')
        assert tracker.current_state == 'interrupted'
        # Past the safety-waiting-timeout with no further output: recovers to
        # idle instead of sticking in INTERRUPTED forever.
        t[0] = 1.0 + SAFETY_WAITING_TIMEOUT + 5.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_hook_idle_not_gated_by_composing(
        self, tmp_path: Path,
    ) -> None:
        """Authoritative idle (the Stop-hook signal) must still idle even while
        the user composes — only the heuristic fallbacks are gated."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(
            pty_alive=True, has_pending_input=True) == 'idle'

    def test_is_ready_false_while_composing(
        self, tmp_path: Path,
    ) -> None:
        """is_ready (the auto-sender readiness check) must forward the
        composing gate, so a queued message isn't dispatched into a half-typed
        prompt — a cursor+silence state that would be 'ready' otherwise."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(tracker, 'output...')
        t[0] = 7.0
        assert tracker.is_ready(pty_alive=True, has_pending_input=False) is True
        t2 = [0.0]
        tr2 = make_tracker(tmp_path, t2)
        tr2.on_send()
        t2[0] = 1.0
        feed_with_visible_cursor(tr2, 'output...')
        t2[0] = 7.0
        assert tr2.is_ready(pty_alive=True, has_pending_input=True) is False

    def test_cursor_visible_during_streaming_stays_running(
        self, tmp_path: Path,
    ) -> None:
        """During streaming, output arrives constantly.  Even if cursor
        is visible between frames, silence < 2s keeps state running."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(tracker, 'Streaming token 1')
        # Only 0.5s since last output — not enough silence (need >2s)
        t[0] = 1.1
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_enter_from_waiting_does_not_flip_idle_on_stale_silence(
        self, tmp_path: Path,
    ) -> None:
        """Reproduces the AskUserQuestion / permission-dialog regression:
        when the user answers via Enter, state goes NEEDS_PERMISSION →
        RUNNING.  The running→idle cursor+silence heuristic must not
        fire on silence accumulated WHILE the dialog was on screen —
        otherwise the auto-sender flushes the queue between the Enter
        and Claude's first post-answer output.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # Dialog appears on screen with a visible cursor (so the live
        # screen carries dialog patterns when the Notification signal
        # arrives and the Late-Notification guard accepts it).
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker, 'Allow tool?  Enter to select  Esc to cancel',
        )
        t[0] = 2.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # User deliberates for ~10 seconds — _last_output_time stays at 1.0.
        t[0] = 12.0
        # Enter dismisses the dialog; state flips back to RUNNING.
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'
        assert tracker._running_since == 12.0
        # Without the ``_last_output_time > _running_since`` guard,
        # the cursor+silence heuristic would see ``silence = 11 s`` and
        # immediately flip RUNNING → IDLE.  With the guard, the stale
        # pre-dialog silence is ignored and state stays RUNNING.
        t[0] = 12.1
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_enter_from_waiting_stays_running_through_first_token_gap(
        self, tmp_path: Path,
    ) -> None:
        """After answering a mid-turn dialog, the 5 s cursor+silence idle
        fallback must NOT fire on the model's first-token latency.

        Reproduces the confirmed production bug: answering an
        AskUserQuestion / permission dialog moves the state
        NEEDS_PERMISSION → RUNNING, and the dialog-dismissal render emits
        a tiny burst of output a few ms after the Enter — moving
        ``_last_output_time`` past ``_running_since`` and so opening the
        ``max(...)`` rebase gate.  If Claude is then silent for >5 s
        before its first real output, the cursor+silence heuristic used
        to flip RUNNING → IDLE and the auto-sender flushed a queued
        message into the still-running turn.  With the post-answer resume
        grace the session stays RUNNING until a real end signal.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker, 'Question?  Enter to select  Esc to cancel',
        )
        t[0] = 2.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # User answers after deliberating.
        t[0] = 12.0
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'
        # Dialog-dismissal render: a tiny output burst right after Enter,
        # with the dialog footer GONE (Claude cleared it).  This moves
        # _last_output_time to 12.05 (> _running_since=12.0), defeating
        # the rebase that would otherwise hold us.
        t[0] = 12.05
        feed_with_visible_cursor(tracker, 'Working...')
        # >5 s of first-token silence.  Pre-fix this idled (and dispatched
        # the queue); post-fix it stays RUNNING.
        t[0] = 18.0
        assert tracker.get_state(pty_alive=True) == 'running'
        # The real end arrives via the Stop hook → idle (only NOW may the
        # auto-sender dispatch the queued message).
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_post_answer_grace_still_idles_via_safety_timeout(
        self, tmp_path: Path,
    ) -> None:
        """The post-answer grace suppresses only the 5 s cursor+silence
        fallback — the 60 s safety timeout still force-idles a genuinely
        hung turn, so a missing Stop hook can't strand the session.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker, 'Question?  Enter to select  Esc to cancel',
        )
        t[0] = 2.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        t[0] = 12.0
        tracker.on_input(b'\r')
        # Output burst after the answer, then total silence past the 60 s
        # safety window with no Stop hook ever firing.
        t[0] = 12.05
        feed_with_visible_cursor(tracker, 'Working...')
        t[0] = 12.05 + SAFETY_SILENCE_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_on_send_clears_post_answer_grace(self, tmp_path: Path) -> None:
        """A Leap-dispatched / direct message starts a fresh turn, so
        ``on_send`` must clear the post-answer grace (otherwise the next
        turn's cursor+silence idle would be wrongly suppressed)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker, 'Question?  Enter to select  Esc to cancel',
        )
        t[0] = 2.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        t[0] = 12.0
        tracker.on_input(b'\r')
        assert tracker._awaiting_resume_after_prompt is True
        tracker.on_send()
        assert tracker._awaiting_resume_after_prompt is False

    def test_post_answer_grace_clears_on_idle(self, tmp_path: Path) -> None:
        """The grace is scoped to the answered turn: once it ends at IDLE
        the flag clears on the next poll, so it can't leak into a later
        auto-resumed turn and suppress that turn's cursor+silence idle."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker, 'Question?  Enter to select  Esc to cancel',
        )
        t[0] = 2.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        t[0] = 12.0
        tracker.on_input(b'\r')
        assert tracker._awaiting_resume_after_prompt is True
        # Real end via Stop hook.
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Next poll observes IDLE at the top and drops the grace.
        tracker.get_state(pty_alive=True)
        assert tracker._awaiting_resume_after_prompt is False

    def test_post_answer_stale_footer_repromote_routes_to_running(
        self, tmp_path: Path,
    ) -> None:
        """Secondary path of the same bug, via the waiting→idle fallback.

        If the answered dialog's footer lingers on screen (Claude slow to
        repaint), the running→idle block re-promotes RUNNING→NEEDS_PERMISSION
        off that stale footer *before* its own grace check.  When the
        footer finally clears, the waiting→idle cursor+silence fallback
        would conclude idle and dispatch the queue into the live turn.
        With the post-answer grace, that fallback instead routes back to
        RUNNING so the Stop hook / running→idle grace decide the real end.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker, 'Question?  Enter to select  Esc to cancel',
        )
        t[0] = 2.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # User answers.
        t[0] = 12.0
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'
        # Stale footer still on screen (Claude hasn't repainted yet).
        t[0] = 12.1
        feed_with_visible_cursor(
            tracker, 'Question?  Enter to select  Esc to cancel',
        )
        # >5 s silent with footer present → running→idle re-promotes off
        # the stale footer.
        t[0] = 18.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # Footer finally clears; >5 s more silence would trip waiting→idle.
        t[0] = 19.0
        feed_with_visible_cursor(tracker, 'Working...')
        t[0] = 25.0
        # Pre-fix: 'idle' (and the queue flushes).  Post-fix: 'running'.
        assert tracker.get_state(pty_alive=True) == 'running'
        # Real end still idles via the hook.
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_waiting_self_dismiss_still_idles_without_an_answer(
        self, tmp_path: Path,
    ) -> None:
        """No-regression guard for the waiting→idle change: a dialog the
        user did NOT answer (flag unset) that the CLI self-dismisses must
        still idle via cursor+silence — the grace only applies after a
        real answer."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker, 'Allow tool?  Enter to select  Esc to cancel',
        )
        t[0] = 2.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        assert tracker._awaiting_resume_after_prompt is False
        # CLI self-dismisses the dialog (no user answer): footer gone.
        t[0] = 3.0
        feed_with_visible_cursor(tracker, 'tool auto-cancelled, moving on')
        t[0] = 9.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_post_answer_pending_second_question_stays_permission(
        self, tmp_path: Path,
    ) -> None:
        """Multi-question AskUserQuestion (the reported 2-question case):
        after answering Q1, Q2's footer is on screen.  The grace must NOT
        route that to RUNNING — the waiting→idle routing only fires when
        the indicator is GONE, so a genuinely pending Q2 stays
        NEEDS_PERMISSION for the user to answer (neither prematurely
        resumed nor idled)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker, 'Q1?  Enter to select  Esc to cancel',
        )
        t[0] = 2.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # Answer Q1.
        t[0] = 12.0
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'
        # Q2 renders (incremental repaint) — footer still present.
        t[0] = 12.1
        feed_with_visible_cursor(
            tracker, 'Q2?  Enter to select  Esc to cancel',
        )
        # >5 s silent with Q2 footer present → re-promote to needs_permission.
        t[0] = 18.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # Ink keeps Q2 rendered (output AFTER the re-promotion, so the
        # waiting→idle outer guard opens) — this exercises the
        # indicator-still-present branch: indicator_gone is False, so the
        # post-answer routing must NOT fire and Q2 stays NEEDS_PERMISSION.
        t[0] = 19.0
        feed_with_visible_cursor(
            tracker, 'Q2?  Enter to select  Esc to cancel',
        )
        t[0] = 25.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # User answers Q2 → resumes normally.
        t[0] = 26.0
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'

    def test_interrupt_clears_post_answer_grace(self, tmp_path: Path) -> None:
        """A user interrupt (Esc/Ctrl+C) cancels the post-answer resume
        grace — 'stop' is the opposite of 'resume the answered turn'."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker, 'Q?  Enter to select  Esc to cancel',
        )
        t[0] = 2.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        t[0] = 12.0
        tracker.on_input(b'\r')                      # answer → grace armed
        assert tracker._awaiting_resume_after_prompt is True
        tracker.on_input(b'\x1b')                    # interrupt → grace gone
        assert tracker._awaiting_resume_after_prompt is False

    def test_post_answer_grace_does_not_hijack_interrupt_dismissal(
        self, tmp_path: Path,
    ) -> None:
        """Defense-in-depth for the PROMPT_STATES guard on the waiting→idle
        routing: even if the grace flag is still set while the state is
        INTERRUPTED (e.g. a self-interrupt path that bypasses on_input's
        flag-clear), an interrupt dismissal must IDLE — never be hijacked
        into RUNNING by the post-answer grace."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # Drive into INTERRUPTED via the real mechanism.
        t[0] = 1.0
        tracker.on_input(b'\x1b')                    # interrupt pending
        feed_with_visible_cursor(tracker, 'Interrupted')
        assert tracker.current_state == 'interrupted'
        # Double-escape so the INTERRUPTED dismissal check can fire
        # (_user_responded), then simulate the grace flag surviving into
        # INTERRUPTED (a self-interrupt wouldn't route through on_input).
        tracker.on_input(b'\x1b')
        tracker._awaiting_resume_after_prompt = True
        # Dismissed: interrupt marker gone, cursor visible, then silence.
        t[0] = 5.0
        feed_with_visible_cursor(tracker, 'no marker here')
        t[0] = 11.0
        # PROMPT_STATES guard keeps this on the idle path, not resume.
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_safety_timeout_ignores_stale_pre_running_silence(
        self, tmp_path: Path,
    ) -> None:
        """Same shape as the cursor+silence regression but for the 60 s
        safety silence fallback: a user who answers AskUserQuestion 60 s
        after it appears must not force-idle the session the moment
        Enter lands.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        # Cursor hidden so the cursor+silence path is gated off and we
        # exercise the safety-silence path specifically.  Includes the
        # dialog footer so the Late-Notification guard accepts the
        # needs_permission signal.
        tracker.on_output(
            b'\x1b[?25lAllow tool?  Enter to select  Esc to cancel',
        )
        t[0] = 2.0
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # User deliberates for longer than the safety silence window.
        t[0] = 2.0 + SAFETY_SILENCE_TIMEOUT + 5.0
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_waiting_timeout_triggers_idle(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission
        t[0] = 1.0
        tracker.on_output(b'prompt text')
        # Remove signal file so timeout can fire
        tracker._signal_file.unlink(missing_ok=True)
        t[0] = 1.0 + SAFETY_WAITING_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_waiting_timeout_respects_signal_confirmation(
        self, tmp_path: Path,
    ) -> None:
        """When the signal still says needs_permission AND the dialog
        patterns are still visible on screen, the 60s safety timeout
        must keep us waiting (not force idle).  Refreshing the dialog
        text with every redraw is what a real Ink TUI does."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)
        t[0] = 1.0
        # Redraw the dialog (still live) so the waiting→idle
        # cursor+silence fallback doesn't see indicator-gone.
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        # Signal still confirms needs_permission
        t[0] = 1.0 + SAFETY_WAITING_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_cursor_silence_promotion_keeps_dialog_on_screen(
        self, tmp_path: Path,
    ) -> None:
        """The running→needs_permission cursor+silence promotion must NOT
        reset the pyte screen.  A hookless dialog (AskUserQuestion) has no
        permission signal, so the waiting→idle dismissal checks read the
        LIVE screen; if the promotion wiped it, a partial Ink repaint
        leaves no footer and the dialog falsely reads as dismissed.  The
        dialog must remain detectable on screen right after promotion."""
        t = [100.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running (baseline > 0 so cursor+silence arms)
        feed_screen_text(tracker, 'Pick one?  Enter to select  Esc to cancel')
        # cursor visible + >5s silence + dialog footer on the last rows.
        t[0] = 106.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # The promotion must have LEFT the dialog on the live screen.
        with tracker._screen_lock:
            compact = tracker._get_screen_text().replace(
                ' ', '').replace('\n', '')
        assert tracker._provider.has_dialog_indicator(compact)

    def test_waiting_timeout_keeps_dialog_still_on_screen(
        self, tmp_path: Path,
    ) -> None:
        """The 60s stuck-waiting safety timeout must not demote a dialog
        that is still rendered on screen.  A hookless AskUserQuestion
        writes no signal, so without this the status oscillates
        Permission<->Idle for as long as the dialog sits unanswered."""
        t = [100.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        footer = 'Pick one?  Enter to select  Esc to cancel'
        feed_screen_text(tracker, footer)
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # Dialog stays rendered (live), but the signal is gone.
        t[0] = 101.0
        feed_screen_text(tracker, footer)
        tracker._signal_file.unlink(missing_ok=True)
        t[0] = 101.0 + SAFETY_WAITING_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_waiting_timeout_demotes_when_no_dialog_on_screen(
        self, tmp_path: Path,
    ) -> None:
        """Counterpart: with no dialog on screen and no confirming signal,
        the 60s safety timeout must still force idle - the screen guard
        must not pin a genuinely stuck waiting state forever."""
        t = [100.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(tracker, 'thinking about it')
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # No dialog footer anywhere (signal path reset the screen); drop
        # the signal so only a visible dialog could justify keeping it.
        tracker._signal_file.unlink(missing_ok=True)
        t[0] = 100.0 + SAFETY_WAITING_TIMEOUT + 5.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_incremental_repaint_after_promotion_keeps_dialog(
        self, tmp_path: Path,
    ) -> None:
        """Faithful reproduction of the AskUserQuestion oscillation.

        Mimics Ink's real redraw: a full dialog render via cursor
        positioning, then an INCREMENTAL repaint that rewrites only the
        top line (no full clear, no footer rewrite).  With the old
        reset-on-promotion, the footer was wiped from pyte at promotion
        and the incremental repaint never restored it, so the dialog
        falsely read as dismissed and was demoted to idle.  The promotion
        must leave pyte intact so the footer survives the repaint and the
        prompt is held.  (Pre-fix this test demotes to idle; with the fix
        it stays needs_permission.)
        """
        t = [100.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # running
        # Full dialog render via cursor positioning (Ink-style), no hook.
        tracker.on_output(
            b"\x1b[2J\x1b[1;1HWhat's your preferred beverage?\x1b[K"
            b"\x1b[2;1H  1. Coffee\x1b[K\x1b[3;1H  2. Tea\x1b[K"
            b"\x1b[4;1HEnter to select  Esc to cancel\x1b[K"
        )
        # cursor+silence promotion (no permission signal, like the
        # AskUserQuestion-as-first-action case).
        t[0] = 106.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        # Incremental repaint of ONLY the top line - footer not rewritten.
        t[0] = 107.0
        tracker.on_output(
            b"\x1b[1;1HWhat's your preferred beverage?  \x1b[K"
        )
        with tracker._screen_lock:
            assert 'Esc to cancel' in tracker._get_screen_text()
        # Past the 60s stuck-waiting safety timeout: must still hold.
        t[0] = 107.0 + SAFETY_WAITING_TIMEOUT + 1.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'


# ---------------------------------------------------------------------------
# Trust dialog detection via pyte screen
# ---------------------------------------------------------------------------

class TestTrustDialog:
    def test_trust_dialog_detected_on_screen(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Feed trust dialog text (startup, no user input)
        feed_screen_text(tracker, 'Do you trust the contents of this directory?')
        assert tracker.current_state == 'needs_permission'

    def test_trust_dialog_resume_goes_to_idle(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        feed_screen_text(tracker, 'Do you trust the contents of this directory?')
        assert tracker.current_state == 'needs_permission'
        # User selects option → on_send → running
        tracker.on_send()
        assert tracker.current_state == 'running'
        # Trust dialog phase: output → idle
        t[0] = 1.0
        feed_screen_text(tracker, 'Welcome to Claude Code')
        assert tracker.current_state == 'idle'

    def test_normal_output_does_not_trigger_trust_dialog(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # user input seen → skip startup detection
        feed_screen_text(tracker, 'Do you trust the contents of this directory?')
        assert tracker.current_state == 'idle'

    def test_resumed_conversation_quoting_menu_is_not_a_startup_dialog(
        self, tmp_path: Path,
    ) -> None:
        # Regression: at startup (no user input yet) the dialog scan ran on
        # the FULL screen, so a --resume'd session whose replayed
        # conversation quoted a numbered menu ("❯ 1.") mid-screen flipped
        # straight to needs_permission at launch.  Only the last 5 non-blank
        # rows (where a real footer/menu renders) may decide.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        feed_screen_text(
            tracker,
            'Resuming previous conversation...\r\n'
            'Earlier you picked from this menu:\r\n'
            '❯ 1. Approve\r\n'
            '  2. Reject\r\n'
            'I went with option 1 as you asked.\r\n'
            'Then I updated the handler accordingly.\r\n'
            'All tests passed after the change.\r\n'
            '────────────────────────────────────────────────────\r\n'
            '❯ ',
        )
        assert tracker.current_state == 'idle'

    def test_real_startup_dialog_at_bottom_still_detected(
        self, tmp_path: Path,
    ) -> None:
        # A genuine startup dialog renders its menu/footer at the bottom of
        # the screen - the tail-restricted scan must still catch it.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        feed_screen_text(
            tracker,
            'Welcome to Claude Code\r\n'
            'Select login method:\r\n'
            '❯ 1. Claude account\r\n'
            '  2. Console account\r\n',
        )
        assert tracker.current_state == 'needs_permission'


# ---------------------------------------------------------------------------
# Stale interrupt suppression
# ---------------------------------------------------------------------------

class TestStaleInterruptSuppression:
    def test_resume_from_interrupted_suppresses_confirmed_pattern(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')
        feed_screen_text(tracker, 'Interrupted')
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → interrupted
        # Send from interrupted → running (sets suppression)
        tracker.on_send()
        assert tracker._suppress_stale_interrupt is True

    def test_suppression_cleared_on_normal_send(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Non-interrupted send
        tracker.on_send()
        assert tracker._suppress_stale_interrupt is False

    def test_suppression_auto_cleared_when_pattern_scrolls_off(
        self, tmp_path: Path,
    ) -> None:
        """_suppress_stale_interrupt is cleared when the interrupted
        pattern no longer appears on the pyte screen."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()
        tracker.on_input(b'\x1b')
        feed_screen_text(tracker, 'Interrupted')
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → interrupted
        tracker.on_send()  # → running, suppression=True
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle, suppression still True
        assert tracker._suppress_stale_interrupt is True

        # Output without "Interrupted" → suppression cleared
        feed_screen_text(tracker, 'Normal idle prompt')
        assert tracker._suppress_stale_interrupt is False

        # Now a real interrupt should be detected once the user is
        # actually running again (Esc only arms the flag in non-IDLE).
        tracker.on_send()  # → running
        tracker.on_input(b'\x1b')
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'interrupted'


# ---------------------------------------------------------------------------
# on_input filtering
# ---------------------------------------------------------------------------

class TestOnInputFiltering:
    def test_csi_sequences_filtered(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x1b[I')  # focus in
        assert tracker._interrupt_pending is False
        assert tracker._seen_user_input is False

    def test_single_escape_accepted(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running (Esc only arms flag in non-IDLE)
        tracker.on_input(b'\x1b')
        assert tracker._interrupt_pending is True

    def test_regular_keys_accepted(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'a')
        assert tracker._seen_user_input is True
        assert tracker._interrupt_pending is False

    def test_ctrl_c_in_multi_byte_data(self, tmp_path: Path) -> None:
        """Ctrl+C bundled with text in one on_input call."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running (Ctrl+C only arms flag in non-IDLE)
        tracker.on_input(b'hello\x03')
        assert tracker._interrupt_pending is True
        assert tracker._seen_user_input is True

    def test_embedded_csi_u_escape(self, tmp_path: Path) -> None:
        """CSI u Escape sequence embedded in multi-byte data (not at pos 0)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running (Esc only arms flag in non-IDLE)
        # Typed "hi" then pressed Escape via kitty protocol
        tracker.on_input(b'hi\x1b[27u')
        assert tracker._interrupt_pending is True

    def test_embedded_csi_focus_not_interrupt(self, tmp_path: Path) -> None:
        """Focus event CSI embedded in data should NOT set interrupt."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Text + focus in event (not an interrupt)
        tracker.on_input(b'hi\x1b[I')
        assert tracker._interrupt_pending is False
        # But _seen_user_input should be True (text was typed)
        assert tracker._seen_user_input is True

    def test_focus_event_plus_ctrl_c(self, tmp_path: Path) -> None:
        """Focus event followed by Ctrl+C — both must be handled."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running (Ctrl+C only arms flag in non-IDLE)
        tracker.on_input(b'\x1b[I\x03')
        assert tracker._interrupt_pending is True
        assert tracker._seen_user_input is True

    def test_pure_focus_event_filtered(self, tmp_path: Path) -> None:
        """Pure focus event (no real user input) is filtered entirely."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x1b[I')
        assert tracker._interrupt_pending is False
        assert tracker._seen_user_input is False

    def test_mixed_text_interrupt_enter(self, tmp_path: Path) -> None:
        """Text + Ctrl+C + Enter all in one chunk.

        Enter triggers running and clears interrupt_pending.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'hello\x03\r')
        assert tracker._interrupt_pending is False
        assert tracker.current_state == 'running'

    def test_text_with_interrupt_sets_user_input_since_idle(
        self, tmp_path: Path,
    ) -> None:
        """Printable text alongside interrupt should still set
        _user_input_since_idle (the text counts)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle
        # Text + Ctrl+C — has printable content
        tracker.on_input(b'hello\x03')
        assert tracker._user_input_since_idle is True

    def test_pure_interrupt_does_not_set_user_input_since_idle(
        self, tmp_path: Path,
    ) -> None:
        """Pure Ctrl+C without text should NOT set _user_input_since_idle."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle
        tracker.on_input(b'\x03')
        assert tracker._user_input_since_idle is False

    def test_null_bytes_ignored(self, tmp_path: Path) -> None:
        """Null bytes (terminal noise) should not set any flags."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'\x00\x00\x00')
        assert tracker._seen_user_input is False
        assert tracker._interrupt_pending is False
        assert tracker._user_input_since_idle is False


# ---------------------------------------------------------------------------
# is_ready
# ---------------------------------------------------------------------------

class TestIsReady:
    def test_is_ready_pause_mode(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t, auto_send_mode='pause')
        assert tracker.is_ready_for_state('idle') is True
        assert tracker.is_ready_for_state('running') is False
        assert tracker.is_ready_for_state('needs_permission') is False
        assert tracker.is_ready_for_state('needs_input') is False
        assert tracker.is_ready_for_state('interrupted') is False

    def test_is_ready_always_mode(self, tmp_path: Path) -> None:
        """In both modes, only IDLE triggers queue message sending.

        Permission auto-approve is handled by the server loop, not
        by is_ready_for_state.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t, auto_send_mode='always')
        assert tracker.is_ready_for_state('idle') is True
        assert tracker.is_ready_for_state('running') is False
        assert tracker.is_ready_for_state('needs_permission') is False
        assert tracker.is_ready_for_state('needs_input') is False
        assert tracker.is_ready_for_state('interrupted') is False


# ---------------------------------------------------------------------------
# on_resize
# ---------------------------------------------------------------------------

class TestResize:
    def test_resize_updates_screen(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_resize(30, 100)
        assert tracker._screen.lines == 30
        assert tracker._screen.columns == 100

    def test_resize_during_idle_stays_idle(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_resize(30, 100)
        # Output after resize (TUI redraw) should not trigger running
        feed_with_visible_cursor(tracker, 'Redrawn content')
        assert tracker.current_state == 'idle'


# ---------------------------------------------------------------------------
# Prompt output via pyte screen snapshot
# ---------------------------------------------------------------------------

class TestPromptOutput:
    def test_prompt_output_from_snapshot(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # Dialog patterns must be on screen for the guard to accept
        feed_screen_text(
            tracker,
            'Allow tool use?  Enter to select  Esc to cancel',
        )
        # Enter needs_permission via signal
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)
        # Feed prompt text while in needs_permission
        feed_screen_text(tracker, 'Allow tool use?\n1. Yes\n2. No')
        result = tracker.get_prompt_output()
        assert 'Allow tool use?' in result

    def test_empty_prompt_output(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        assert tracker.get_prompt_output() == ''


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_cleanup_deletes_signal_file(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        write_signal(tracker, 'idle')
        tracker.cleanup()
        assert not tracker._signal_file.exists()

    def test_cleanup_no_error_if_missing(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.cleanup()  # no error


# ---------------------------------------------------------------------------
# PTY dead resets flags
# ---------------------------------------------------------------------------

class TestPtyDead:
    def test_pty_dead_clears_flags(self, tmp_path: Path) -> None:
        """When PTY dies, all flags should be reset so a restart
        starts fresh (e.g. trust dialog detection works again)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        # Set up various flags
        tracker.on_input(b'x')  # _seen_user_input
        tracker.on_send()       # → running
        tracker.on_input(b'\x1b')  # _interrupt_pending
        # PTY dies
        assert tracker.get_state(pty_alive=False) == 'idle'
        # All flags reset
        assert tracker._interrupt_pending is False
        assert tracker._seen_user_input is False
        assert tracker._user_responded is False
        assert tracker._trust_dialog_phase is False
        assert tracker._suppress_stale_interrupt is False

    def test_pty_dead_repeated_calls_no_redundant_reset(
        self, tmp_path: Path,
    ) -> None:
        """Repeated pty_alive=False calls after already idle should
        not keep resetting the screen."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running
        tracker.get_state(pty_alive=False)  # first call: resets
        # Second call: already idle, should be fast no-op
        tracker.get_state(pty_alive=False)
        assert tracker.current_state == 'idle'


# ---------------------------------------------------------------------------
# Escape correction from NEEDS_PERMISSION
# ---------------------------------------------------------------------------

class TestEscapeCorrectionFromPermission:
    def test_escape_in_needs_permission_goes_interrupted(
        self, tmp_path: Path,
    ) -> None:
        """Escape at a permission prompt should detect interrupted
        pattern and transition to interrupted (not just NEEDS_INPUT)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        write_signal(tracker, 'needs_permission')
        tracker.get_state(pty_alive=True)  # → needs_permission
        tracker.on_input(b'\x1b')  # interrupt pending
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'interrupted'


# ---------------------------------------------------------------------------
# CLIState enum
# ---------------------------------------------------------------------------

class TestClaudeDialogIndicator:
    """Claude's has_dialog_indicator() must detect both standard footer
    patterns and numbered menu prompts."""

    def test_standard_dialog_detected(self) -> None:
        p = ClaudeProvider()
        # Both patterns present
        assert p.has_dialog_indicator('AllowReadEntertoselectEsctocancel')
        # Only one (edit-confirmation style)
        assert p.has_dialog_indicator('MakethiseditEsctocancel')
        assert p.has_dialog_indicator('Entertoselectoption')

    def test_numbered_menu_detected(self) -> None:
        p = ClaudeProvider()
        # ❯ cursor (U+276F)
        assert p.has_dialog_indicator('\u276f1.Yes2.No(esc)')
        # › cursor (U+203A)
        assert p.has_dialog_indicator('\u203a1.Yes2.No(esc)')

    def test_no_dialog_not_detected(self) -> None:
        p = ClaudeProvider()
        assert not p.has_dialog_indicator('Taskcompletehereareresults')
        assert not p.has_dialog_indicator('Processingfiles...')

    def test_strict_rejects_partial_pattern_in_response_text(self) -> None:
        """is_dialog_certain must NOT match response text that happens
        to mention 'Esc to cancel' — only all() of standard patterns
        or a numbered menu cursor qualifies."""
        p = ClaudeProvider()
        # Response text explaining keyboard shortcuts
        response = 'pressEsctocanceltheoperation'
        assert p.has_dialog_indicator(response)  # lenient: matches
        assert not p.is_dialog_certain(response)  # strict: rejects

    def test_strict_detects_numbered_menu(self) -> None:
        p = ClaudeProvider()
        assert p.is_dialog_certain('\u276f1.Yes2.No(esc)')
        assert p.is_dialog_certain('\u203a1.Yes2.No(esc)')

    def test_strict_detects_full_standard_dialog(self) -> None:
        p = ClaudeProvider()
        assert p.is_dialog_certain('AllowReadEntertoselectEsctocancel')
        # Partial (only Esc to cancel) — strict rejects
        assert not p.is_dialog_certain('MakethiseditEsctocancel')


class TestCLIStateEnum:
    def test_cli_state_string_comparison(self) -> None:
        assert CLIState.IDLE == 'idle'
        assert CLIState.RUNNING == 'running'

    def test_waiting_states_membership(self) -> None:
        assert CLIState.NEEDS_PERMISSION in WAITING_STATES
        assert CLIState.NEEDS_INPUT in WAITING_STATES
        assert CLIState.INTERRUPTED in WAITING_STATES
        assert CLIState.IDLE not in WAITING_STATES

    def test_backward_compat_signal_alias(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'What should I do?  Enter to select  Esc to cancel',
        )
        tracker._signal_file.write_text(
            json.dumps({"state": "has_question"}),
        )
        assert tracker.get_state(pty_alive=True) == 'needs_input'


# ---------------------------------------------------------------------------
# Codex-specific
# ---------------------------------------------------------------------------

class TestCodexSpecific:
    def test_codex_enter_triggers_running(self, tmp_path: Path) -> None:
        """Codex Enter in idle triggers running — same as all providers."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'

    def test_codex_silence_timeout(self, tmp_path: Path) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_send()
        t[0] = 1.0
        tracker.on_output(b'output')
        codex_timeout = CodexProvider().silence_timeout
        t[0] = 1.0 + codex_timeout + 1.0
        assert tracker.get_state(pty_alive=True) == 'idle'


class TestCodexTranscriptPinning:
    """``read_transcript_completion`` must read THIS session's rollout, not
    the machine-wide newest one.  With two concurrent Codex sessions, the
    "most recently modified file in today's dir" routinely belongs to the
    OTHER session - its ``task_complete`` used to flip this session to a
    false idle mid-turn (dispatching the queue into a live turn) and
    surface the other session's last message in the signal file."""

    @staticmethod
    def _write_rollout(day_dir: Path, name: str, entries: list) -> Path:
        day_dir.mkdir(parents=True, exist_ok=True)
        f = day_dir / name
        f.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
        return f

    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import leap.cli_providers.codex as codex_mod
        monkeypatch.setattr(
            codex_mod, 'CODEX_CONFIG_DIR', tmp_path / 'codex')
        day_dir = (tmp_path / 'codex' / 'sessions'
                   / date.today().strftime('%Y/%m/%d'))
        storage = tmp_path / 'storage'
        (storage / 'sockets').mkdir(parents=True)
        t = [100.0]
        tracker = ClaudeStateTracker(
            signal_file=storage / 'sockets' / 'codex1.signal',
            auto_send_mode='pause',
            clock=lambda: t[0],
            cwd=str(tmp_path),
            tag='codex1',
            provider=CodexProvider(),
        )
        return tracker, t, day_dir, storage

    @staticmethod
    def _fresh_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    def test_other_sessions_completion_does_not_idle_this_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tracker, t, day_dir, storage = self._setup(tmp_path, monkeypatch)
        ts = self._fresh_ts()
        mine = self._write_rollout(day_dir, 'rollout-mine.jsonl', [
            {'timestamp': ts, 'payload': {'type': 'task_started'}},
        ])
        self._write_rollout(day_dir, 'rollout-other.jsonl', [
            {'timestamp': ts, 'payload': {
                'type': 'task_complete',
                'last_agent_message': 'OTHER session finished',
            }},
        ])
        # The hook recorded MY transcript for this tag.
        rec_dir = storage / 'cli_sessions' / 'codex'
        rec_dir.mkdir(parents=True)
        (rec_dir / 'codex1.json').write_text(json.dumps([{
            'session_id': 'mine',
            'transcript_path': str(mine),
            'cwd': str(tmp_path),
            'last_seen': time.time(),
        }]))
        # Make the OTHER session's rollout the newest file on disk - the
        # exact shape during my session's silent stretch (long tool call).
        now = time.time()
        os.utime(mine, (now - 10, now - 10))

        tracker.on_send()  # running, _running_since = fake 100.0
        assert tracker.get_state(pty_alive=True) == 'running'

        # My own completion still idles - and carries MY message.
        self._write_rollout(day_dir, 'rollout-mine.jsonl', [
            {'timestamp': self._fresh_ts(), 'payload': {
                'type': 'task_complete',
                'last_agent_message': 'MINE finished',
            }},
        ])
        assert tracker.get_state(pty_alive=True) == 'idle'
        signal = json.loads(tracker._signal_file.read_text())
        assert signal.get('last_assistant_message') == 'MINE finished'

    def test_mtime_fallback_without_a_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Before the first hook fire there is no record - the legacy
        # newest-file fallback still detects completion (degraded but
        # better than nothing for single-session use).
        tracker, t, day_dir, storage = self._setup(tmp_path, monkeypatch)
        self._write_rollout(day_dir, 'rollout-only.jsonl', [
            {'timestamp': self._fresh_ts(), 'payload': {
                'type': 'task_complete',
                'last_agent_message': 'done',
            }},
        ])
        tracker.on_send()
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# /clear scenario (the original bug)
# ---------------------------------------------------------------------------

class TestSlashClear:
    def test_clear_resolves_via_cursor_silence(self, tmp_path: Path) -> None:
        """The original bug: /clear caused persistent running (60s).

        Fix: Enter triggers running, but cursor visible + 2s silence
        resolves to idle quickly.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input

        # User types /clear + Enter → running
        for ch in b'/clear':
            tracker.on_input(bytes([ch]))
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'

        # TUI redraws with cursor visible
        t[0] = 0.5
        feed_with_visible_cursor(tracker, 'Cleared screen')

        # After 5s silence + cursor visible → idle
        t[0] = 6.5
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_clear_vs_real_message(self, tmp_path: Path) -> None:
        """Real messages resolve via Stop hook.
        /clear resolves via cursor+silence check (~5s)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input

        # Real message: Enter → running → Stop hook → idle
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# Stale screen content after state transitions
# ---------------------------------------------------------------------------

class TestStaleScreenContent:
    def test_stale_interrupted_on_screen_no_false_trigger(
        self, tmp_path: Path,
    ) -> None:
        """After resolving an interrupt, stale 'Interrupted' text on
        the pyte screen must not cause false interrupted state when
        user presses Escape later.

        Under the IDLE-state-gate design Esc in IDLE never even arms
        ``_interrupt_pending``, so the false-trigger window is closed
        regardless of what's on the pyte screen.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input

        # Phase 1: Real interrupt cycle
        tracker.on_send()  # → running
        tracker.on_input(b'\x1b')  # interrupt pending (state RUNNING)
        feed_screen_text(tracker, 'Interrupted')
        assert tracker.current_state == 'interrupted'

        # Phase 2: Resolve interrupt — send new message
        tracker.on_send()  # → running (clears screen)
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle (clears screen)

        # Phase 3: Accidental Escape in idle — flag stays False (Option A
        # gates flag-set on state != IDLE).  Even if the screen still
        # carried the stale "Interrupted" substring, no transition would
        # fire because the gate is unarmed.
        tracker.on_input(b'\x1b')
        assert tracker._interrupt_pending is False

        t[0] = 5.0
        feed_screen_text(tracker, 'Normal idle output')
        assert tracker.current_state == 'idle'

    def test_screen_reset_on_running_to_idle(self, tmp_path: Path) -> None:
        """Screen is cleared when transitioning running→idle via hook."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(tracker, 'Some running output')
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle

        # Screen should be cleared
        with tracker._screen_lock:
            screen_text = tracker._get_screen_text()
        assert 'Some running output' not in screen_text

    def test_needs_permission_no_false_idle_after_screen_reset(
        self, tmp_path: Path,
    ) -> None:
        """Entering NEEDS_PERMISSION resets the pyte screen.  The Ink TUI
        does not re-render the dialog after the reset (it is already
        displayed from its own perspective).  The indicator-gone fallback
        must NOT fire on the very next poll — an empty fresh screen is not
        evidence that the dialog was dismissed.

        Regression test for the bug observed in the state log:
            19:29:51.559 running→needs_permission (dialog on screen)
            19:29:52.065 NEEDS_PERMISSION→idle (indicator gone + cursor)
            19:29:52.575 signal=needs_permission ignored (stale)
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()       # → running

        # Dialog appears after running for a while; last output was at t=0.
        # Simulate 5.1 seconds of silence with the dialog on screen.
        # Cursor must be visible for the cursor+silence path to fire.
        t[0] = 0.1
        feed_with_visible_cursor(tracker, '❯ 1. Yes\n2. No\nEsc to cancel')
        t[0] = 5.2  # 5.1s of silence since last output

        # get_state detects dialog via cursor+silence → NEEDS_PERMISSION.
        # Screen is reset immediately on entering the state.
        state = tracker.get_state(pty_alive=True)
        assert state == CLIState.NEEDS_PERMISSION

        # 0.5s later (next poll cycle): NO new output has arrived.
        # _last_output_time (0.1) < _waiting_since (5.2) so the
        # indicator-gone check must be suppressed.
        t[0] = 5.7
        state = tracker.get_state(pty_alive=True)
        assert state == CLIState.NEEDS_PERMISSION, (
            'indicator-gone check fired too early on empty post-reset screen'
        )

    def test_needs_permission_self_dismiss_detected_after_new_output(
        self, tmp_path: Path,
    ) -> None:
        """When the CLI genuinely self-dismisses the dialog it sends new
        PTY output to update the screen.  After that output settles, the
        indicator-gone check is allowed to fire and return IDLE.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()

        # Dialog appears with visible cursor (required for cursor+silence path).
        t[0] = 0.1
        feed_with_visible_cursor(tracker, '❯ 1. Yes\n2. No\nEsc to cancel')
        t[0] = 5.2

        # Enter NEEDS_PERMISSION via cursor+silence.
        state = tracker.get_state(pty_alive=True)
        assert state == CLIState.NEEDS_PERMISSION
        waiting_since = tracker._waiting_since
        assert waiting_since is not None

        # CLI self-dismisses: new output arrives AFTER _waiting_since,
        # replacing the dialog with a plain idle prompt (no dialog indicator).
        t[0] = 5.5
        feed_screen_text(tracker, 'Claude is ready')   # no dialog patterns

        # After 5s of silence since the new output, indicator-gone fires.
        t[0] = 10.6  # 5.1s after the post-state output at t=5.5
        state = tracker.get_state(pty_alive=True)
        assert state == CLIState.IDLE, (
            'indicator-gone should fire after new output shows dialog is gone'
        )


# ---------------------------------------------------------------------------
# Pasted text with Enter (bundled bytes)
# ---------------------------------------------------------------------------

class TestPastedEnter:
    def test_pasted_enter_triggers_running(self, tmp_path: Path) -> None:
        """Enter in idle (even pasted) triggers running."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_input(b'hello\r')
        assert tracker.current_state == 'running'

    def test_cr_inside_bracketed_paste_is_not_enter(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_input(b'\x1b[200~line1\rline2\x1b[201~')
        assert tracker.current_state == 'idle'
        assert tracker._in_bracketed_paste is False


class TestSplitPasteMarkersTracker:
    """Paste markers split across two on_input chunks must still be
    tracked.  Pre-fix, a split END marker latched ``_in_bracketed_paste``
    True: the submit Enter after the paste was classified as paste content
    (no idle→running - the session read Idle through a real turn), and a
    split START marker let pasted ``\\r`` bytes fire phantom Enters."""

    def test_split_end_marker_unlatches_paste_mode(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_input(b'\x1b[200~line1\rline2\x1b[20')
        assert tracker._in_bracketed_paste is True
        t[0] = 0.1
        tracker.on_input(b'1~')
        assert tracker._in_bracketed_paste is False
        # The submit Enter after the paste fires idle→running again.
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'

    def test_split_end_at_escape_byte_unlatches_paste_mode(
        self, tmp_path: Path,
    ) -> None:
        # Worst split point: the lone ESC byte ends the chunk.  Inside a
        # paste it is held (no interrupt semantics there) and re-attached
        # to the next chunk.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_input(b'\x1b[200~content\x1b')
        assert tracker._in_bracketed_paste is True
        assert tracker._interrupt_pending is False  # held, not interrupt
        t[0] = 0.1
        tracker.on_input(b'[201~')
        assert tracker._in_bracketed_paste is False
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'

    def test_split_start_marker_suppresses_pasted_enter(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_input(b'\x1b[20')
        t[0] = 0.1
        tracker.on_input(b'0~line1\rline2\x1b[201~')
        # The \r was paste content - no phantom idle→running.
        assert tracker.current_state == 'idle'
        assert tracker._in_bracketed_paste is False

    def test_stale_held_tail_is_dropped(self, tmp_path: Path) -> None:
        # A held marker head older than the carry window is dropped (it
        # was an incomplete escape sequence; splicing it into unrelated
        # later input would corrupt classification).  A subsequent
        # complete marker still resolves the paste state.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_input(b'\x1b[200~content\x1b')
        t[0] = 5.0  # way past _INPUT_TAIL_MAX_AGE
        tracker.on_input(b'typed')
        assert tracker._in_bracketed_paste is True  # still inside paste
        tracker.on_input(b'\x1b[201~')
        assert tracker._in_bracketed_paste is False


class TestSplitEscapeReassembly:
    """A chunk-split arrow key (lone ESC in one read, ``[A`` in the next)
    must not leave ``_interrupt_pending`` armed mid-turn - combined with
    the word "Interrupted" anywhere on screen it false-flipped the session
    to INTERRUPTED."""

    def test_split_arrow_disarms_interrupt_pending(
        self, tmp_path: Path,
    ) -> None:
        t = [10.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # running
        tracker.on_input(b'\x1b')  # first half of a split ↑
        assert tracker._interrupt_pending is True  # provisional
        t[0] = 10.05  # continuation lands within the reassembly window
        tracker.on_input(b'[A')
        assert tracker._interrupt_pending is False

    def test_real_escape_then_late_arrow_stays_armed(
        self, tmp_path: Path,
    ) -> None:
        # Two deliberate keypresses (Esc, then ↑ later) are NOT a split
        # sequence - the interrupt must stay armed.
        t = [10.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')
        t[0] = 11.0  # well past the reassembly window
        tracker.on_input(b'\x1b[A')
        assert tracker._interrupt_pending is True

    def test_lone_escape_still_interrupts_immediately(
        self, tmp_path: Path,
    ) -> None:
        t = [10.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')
        assert tracker._interrupt_pending is True

    def test_split_at_csi_introducer_never_arms(
        self, tmp_path: Path,
    ) -> None:
        # Split point after "\x1b[": the head is a paste-marker prefix, so
        # it is held and re-attached - the reassembled "\x1b[A" parses as
        # one CSI and never arms the flag at all.
        t = [10.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b[')
        assert tracker._interrupt_pending is False
        t[0] = 10.05
        tracker.on_input(b'A')
        assert tracker._interrupt_pending is False


# ---------------------------------------------------------------------------
# Escape doesn't block auto-resume
# ---------------------------------------------------------------------------

class TestEscapeDoesNotBlockAutoResume:
    def test_escape_does_not_set_user_input_since_idle(
        self, tmp_path: Path,
    ) -> None:
        """Escape/Ctrl+C should not set _user_input_since_idle,
        so auto-resume cursor detection is not blocked.

        Under Option A, Esc in IDLE also leaves ``_interrupt_pending``
        at False — so neither flag interferes with auto-resume.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle, clears _user_input_since_idle
        assert tracker._user_input_since_idle is False

        # Escape in IDLE: neither flag should be touched.
        tracker.on_input(b'\x1b')
        assert tracker._user_input_since_idle is False
        assert tracker._interrupt_pending is False

        # Auto-resume should still work (detected at poll time)
        t[0] = 5.0
        feed_with_hidden_cursor(tracker, 'Auto processing')
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_ctrl_c_does_not_block_auto_resume(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle

        tracker.on_input(b'\x03')  # Ctrl+C
        assert tracker._user_input_since_idle is False

    def test_regular_input_does_block_auto_resume(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)  # → idle

        tracker.on_input(b'a')  # regular input
        assert tracker._user_input_since_idle is True

        # Auto-resume blocked (even at poll time)
        t[0] = 5.0
        feed_with_hidden_cursor(tracker, 'Some output')
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# Late Notification hook guard
# ---------------------------------------------------------------------------

class TestLateNotificationGuard:
    """The Notification hook can arrive seconds after a permission dialog
    appears — by then the cursor+silence heuristic has already moved
    running→idle and the dialog may have been auto-accepted (bypass
    permissions) or Claude may have finished.

    The guard verifies dialog patterns are visible on the pyte screen
    (or in the saved running snapshot) before accepting an idle→prompt
    signal transition.
    """

    def test_stale_notification_rejected_no_dialog_on_screen(
        self, tmp_path: Path,
    ) -> None:
        """Late Notification with no dialog on screen is rejected."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Claude finishes — cursor+silence fires running→idle
        feed_screen_text(tracker, 'Task complete. Here are the results.')
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

        # Late Notification arrives — no dialog patterns on screen
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Signal file should be deleted to avoid repeated checks
        assert not tracker._signal_file.exists()

    def test_response_mentioning_esc_to_cancel_not_false_positive(
        self, tmp_path: Path,
    ) -> None:
        """Response text explaining keyboard shortcuts (e.g. 'press Esc
        to cancel') must NOT false-trigger dialog detection at the
        running→idle cursor+silence check."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()

        # Response text with "Esc to cancel" in last rows
        feed_with_visible_cursor(
            tracker,
            'Here are the keyboard shortcuts:\n'
            '- Press Esc to cancel the current operation\n'
            '- Press Enter to confirm',
        )
        t[0] = 8.0
        tracker._last_output_time = 2.0
        # cursor visible + 5s silence → should go to IDLE, not
        # needs_permission (would be a false positive)
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_legitimate_notification_accepted_dialog_in_snapshot(
        self, tmp_path: Path,
    ) -> None:
        """When the Stop hook fires while a dialog is on screen, the
        signal-based running→idle handler's proactive check routes the
        transition directly to needs_permission — no idle flash.  A
        subsequent (redundant) Notification keeps the state stable."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Permission dialog appears, then Stop hook fires.
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        # Direct transition to needs_permission (dialog detected on tail).
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        assert tracker._prompt_snapshot

        # Redundant Notification — state stays needs_permission.
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_legitimate_notification_accepted_dialog_on_live_screen(
        self, tmp_path: Path,
    ) -> None:
        """Notification accepted when dialog patterns are on the live
        pyte screen (new output arrived after screen reset)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # cursor+silence fires with non-dialog content
        feed_screen_text(tracker, 'Processing...')
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

        # New output renders the dialog on the live screen
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )

        # Late Notification arrives — dialog on live screen
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_legitimate_notification_accepted_partial_dialog_patterns(
        self, tmp_path: Path,
    ) -> None:
        """Notification accepted when only SOME dialog patterns are
        present (e.g., 'Esc to cancel' without 'Enter to select').
        Claude Code's edit-confirmation dialogs show numbered options
        with 'Esc to cancel' but no 'Enter to select'."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Permission dialog with only "Esc to cancel"
        feed_screen_text(
            tracker,
            'Do you want to make this edit?\n'
            '1. Yes\n'
            '2. Yes, allow all\n'
            '3. No\n'
            'Esc to cancel',
        )
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'
        assert tracker._last_running_snapshot

        # Late Notification arrives — partial dialog patterns in snapshot
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_numbered_menu_permission_accepted_from_running(
        self, tmp_path: Path,
    ) -> None:
        """Notification hook accepted when a numbered menu permission prompt
        (e.g., 'Network request outside of sandbox') is visible.
        These prompts use ❯ cursor indicator + numbered options instead
        of the standard 'Enter to select / Esc to cancel' footer."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Numbered menu permission prompt appears while still RUNNING
        feed_screen_text(
            tracker,
            'Network request outside of sandbox\n'
            '    Host: mcp-proxy.anthropic.com\n'
            'Do you want to allow this connection?\n'
            '\u276f 1. Yes\n'
            '  2. Yes, and don\'t ask again\n'
            '  3. No, and tell Claude what to do differently (esc)',
        )

        # Hook signal arrives while still running (cursor may be hidden)
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_numbered_menu_permission_accepted_from_snapshot(
        self, tmp_path: Path,
    ) -> None:
        """Numbered menu dialog visible at Stop-hook time routes directly
        to needs_permission via the signal-handler proactive check."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Numbered menu permission prompt.
        feed_screen_text(
            tracker,
            'Do you want to allow this connection?\n'
            '\u276f 1. Yes\n'
            '  2. Yes, and don\'t ask again\n'
            '  3. No (esc)',
        )
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        assert tracker._prompt_snapshot

        # Redundant Notification — state stays needs_permission.
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_numbered_menu_detected_at_running_to_idle(
        self, tmp_path: Path,
    ) -> None:
        """Running→idle cursor+silence check detects numbered menu
        prompts and goes directly to needs_permission."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Cursor visible + numbered menu prompt on last 5 rows
        feed_with_visible_cursor(
            tracker,
            'Do you want to allow?\n'
            '\u203a 1. Yes\n'
            '  2. No (esc)',
        )
        t[0] = 8.0
        tracker._last_output_time = 2.0
        # No signal yet — proactive detection via cursor+silence (>5s)
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_stale_snapshot_cleared_on_waiting_to_idle(
        self, tmp_path: Path,
    ) -> None:
        """After answering a dialog (waiting→idle), the running snapshot
        must be cleared so a late Notification hook doesn't false-match
        the old dialog content in the snapshot."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()

        # Numbered menu detected via hook → needs_permission
        feed_screen_text(
            tracker,
            '\u276f 1. Yes\n  2. No (esc)',
        )
        write_signal(tracker, 'needs_permission')
        t[0] = 1.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

        # User answers → user_responded
        tracker.on_input(b'1\r')

        # Stop hook → idle (snapshot must not retain stale dialog content)
        write_signal(tracker, 'idle')
        t[0] = 5.0
        assert tracker.get_state(pty_alive=True) == 'idle'
        # Snapshot may be empty list or list of blank rows — either way
        # no dialog indicator should be present in it.
        snapshot_text = ''.join(tracker._last_running_snapshot).replace(' ', '')
        assert not tracker._provider.has_dialog_indicator(snapshot_text)

        # Late stale Notification — must be rejected
        write_signal(tracker, 'needs_permission')
        t[0] = 8.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_stale_needs_input_rejected_no_dialog_on_screen(
        self, tmp_path: Path,
    ) -> None:
        """Late Notification for elicitation_dialog (needs_input) with
        no dialog on screen is rejected — same guard as needs_permission."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        # Claude finishes — cursor+silence fires running→idle
        feed_screen_text(tracker, 'Task complete.')
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

        # Late elicitation Notification arrives — no dialog on screen
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'idle'
        assert not tracker._signal_file.exists()

    def test_legitimate_needs_input_accepted_with_dialog(
        self, tmp_path: Path,
    ) -> None:
        """Elicitation dialog with patterns on screen at Stop-hook time:
        signal-handler proactive check routes to needs_permission, then
        a needs_input signal correctly downgrades to needs_input."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()  # → running

        feed_screen_text(
            tracker,
            'What should I name this?\n'
            '1. Type something\n'
            'Enter to select  Esc to cancel',
        )
        t[0] = 5.0
        tracker._last_output_time = 2.0
        write_signal(tracker, 'idle')
        # Dialog visible at Stop time → directly routed to
        # needs_permission (we don't yet know it's input vs. permission).
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        assert tracker._prompt_snapshot

        # Notification with elicitation_dialog matcher arrives — refines
        # the kind of waiting state to needs_input.
        write_signal(tracker, 'needs_input')
        assert tracker.get_state(pty_alive=True) == 'needs_input'

    def test_stale_notification_rejected_after_enter_from_permission(
        self, tmp_path: Path,
    ) -> None:
        """After Enter answers a permission dialog (→ RUNNING via Fix 2),
        a stale Notification hook signal is rejected by the Late Notification
        Guard.

        The Enter transition clears _last_running_snapshot and resets the
        pyte screen, so the guard finds no dialog patterns and blocks the
        stale needs_permission signal.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()  # → running

        # Permission dialog appears via hook signal
        feed_screen_text(
            tracker,
            'Allow tool?  Enter to select  Esc to cancel',
        )
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

        # User presses Enter → immediately RUNNING, screen+snapshot cleared
        tracker.on_input(b'\r')
        assert tracker.current_state == 'running'
        assert tracker._last_running_snapshot == []

        # Stale Notification hook fires for the dialog that was already answered
        write_signal(tracker, 'needs_permission')

        # Guard rejects: live screen is empty (reset) and snapshot is empty
        assert tracker.get_state(pty_alive=True) == 'running'
        assert not tracker._signal_file.exists()

    def test_running_signal_accepted_when_screen_has_content(
        self, tmp_path: Path,
    ) -> None:
        """Multi-agent subagent regression test.

        During a Task-tool subagent run, the parent stays RUNNING for the
        entire turn (Claude's ``Stop`` hook does not fire for subagents,
        so ``_last_running_snapshot`` never gets populated).  When the
        subagent's tool call triggers ``Notification(permission_prompt)``,
        the hook can fire before the dialog footer is pyte-rendered — the
        screen has the subagent's prior output (Read indicators, status
        text, etc.) but no ``Enter to select / Esc to cancel`` yet.

        The OLD pattern-only guard rejected those valid signals as
        "stale" because dialog patterns weren't visible AND snapshot was
        empty — auto-approve was then stuck waiting for the 5s
        cursor+silence fallback (or indefinitely, when TUI redraws kept
        refreshing ``_last_output_time``).

        The fix: when ``current==RUNNING``, only reject if BOTH the
        screen AND the snapshot are empty (the freshly-answered-via-Enter
        signature, see the test above).  Non-empty screen → accept the
        signal as fresh, even without dialog patterns on it yet — the
        dialog is incoming, and ``_try_auto_approve`` will retry until
        it can read the menu.
        """
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()  # → running

        # Subagent has been producing output for a while — accumulates
        # on the pyte screen.  No dialog patterns yet (footer hasn't
        # rendered).  Snapshot is empty (no idle transition during the
        # subagent's run).
        feed_screen_text(
            tracker,
            '● Read(/path/to/file)\n'
            '● Edit(/path/to/other)\n'
            'Token usage: 12345',
        )
        assert tracker._last_running_snapshot == []

        # Notification fires for the subagent's pending Edit permission.
        # Old behavior: rejected (no dialog patterns + empty snapshot).
        # New behavior: accepted (screen has subagent output → fresh).
        write_signal(tracker, 'needs_permission')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'


# ---------------------------------------------------------------------------
# Mid-session proactive dialog detection (AskUserQuestion / "Proceed?")
# ---------------------------------------------------------------------------

class TestProactiveIdleDialogDetection:
    """Some Claude tools (notably AskUserQuestion / "Proceed?") do NOT fire
    PreToolUse — only Stop, so the state tracker transitions running→idle
    while the dialog is still visible.  Without proactive detection,
    auto-approve never fires.  We use the strict ``is_dialog_certain``
    check (all dialog_patterns must appear) so single-pattern prose
    doesn't false-trigger.
    """

    def test_idle_to_needs_permission_when_full_dialog_on_screen(
        self, tmp_path: Path,
    ) -> None:
        # User has typed at least once (the gating condition that the
        # original startup-only proactive check excluded us from).
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)
        assert tracker._state == 'idle'
        assert tracker._seen_user_input is True

        # AskUserQuestion-shaped dialog: contains BOTH "Enter to select"
        # AND "Esc to cancel" — the strict patterns Claude uses.
        feed_screen_text(
            tracker,
            'Do you want to proceed?\n'
            '> 1. Yes\n'
            '  2. No\n'
            'Enter to select · Esc to cancel',
        )
        assert tracker._state == 'needs_permission'

    def test_idle_stays_when_only_one_pattern_present(
        self, tmp_path: Path,
    ) -> None:
        # Conversational text mentioning ONE shortcut (not a real dialog)
        # must NOT trigger the proactive transition.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)

        feed_screen_text(
            tracker,
            'The shortcut is Esc to cancel — useful for aborting.',
        )
        assert tracker._state == 'idle'

    def test_signal_idle_with_dialog_on_screen_routes_to_needs_permission(
        self, tmp_path: Path,
    ) -> None:
        # AskUserQuestion / "Proceed?" tools fire the Stop hook (signal
        # "idle") even though a dialog is on screen awaiting an answer.
        # The signal-based running→idle path must check for a dialog
        # footer in the tail and route to needs_permission instead —
        # otherwise the screen gets reset and no further output ever
        # arrives, leaving the state stuck at idle forever.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        # Claude renders the dialog while still running (output arrives
        # before the Stop hook).
        feed_screen_text(
            tracker,
            '□ Coffee\n'
            'Do you like coffee?\n'
            '> 1. Yes\n'
            '  2. No\n'
            'Enter to select · Esc to cancel',
        )
        # Stop hook fires.
        write_signal(tracker, 'idle')
        # The signal-based running→idle handler must see the dialog
        # and route to needs_permission rather than idle.
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_signal_path_transition_survives_dismissal_check(
        self, tmp_path: Path,
    ) -> None:
        # Companion to ``test_proactive_transition_survives_idle_heartbeat``
        # but for the signal-handler path.  After the Stop-hook signal
        # routes to needs_permission, an idle TUI heartbeat advances
        # ``_last_output_time``.  5+ seconds later, the waiting→idle
        # self-dismissal check at get_state ~line 1207 fires.  The
        # dialog must remain in the live buffer (we deliberately don't
        # reset) so the check sees the indicator and does NOT revert.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        feed_screen_text(
            tracker,
            '□ Cats vs dogs\n'
            'Are cats better than dogs?\n'
            '> 1. Yes\n'
            '  2. No\n'
            'Enter to select · Esc to cancel',
        )
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

        # Idle heartbeat — advances _last_output_time without redrawing
        # the dialog cells.
        tracker.on_output(b'\x1b[?25h')

        # 5+ seconds later, dismissal check runs but should see the
        # dialog still in the live buffer and keep needs_permission.
        t[0] = 10.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_proactive_transition_survives_idle_heartbeat(
        self, tmp_path: Path,
    ) -> None:
        # Regression: an earlier version called _reset_screen() right
        # after the proactive transition, wiping the dialog from the
        # live pyte buffer.  Then a Claude TUI heartbeat (cursor blink
        # / partial repaint) would update _last_output_time without
        # re-rendering the full dialog, so the waiting→idle self-
        # dismissal check at get_state would see "no dialog patterns"
        # on the empty screen and revert to idle — even though the
        # dialog is still on the user's actual terminal.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)

        # Full AskUserQuestion-style dialog renders.
        feed_screen_text(
            tracker,
            '□ Cats vs dogs\n'
            'Are cats better than dogs?\n'
            '> 1. Yes\n'
            '  2. No\n'
            'Enter to select · Esc to cancel',
        )
        assert tracker._state == 'needs_permission'

        # Idle TUI heartbeat: just toggle cursor visibility.  This is
        # output (advances _last_output_time) but does NOT redraw the
        # dialog cells.  Without the fix, the dialog would be lost
        # because _reset_screen() had wiped it.
        tracker.on_output(b'\x1b[?25h')

        # 5+ seconds later, get_state runs the self-dismissal check.
        # Live screen still has the dialog → has_dialog_indicator
        # returns True → state must remain needs_permission.
        t[0] = 10.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_idle_stays_when_patterns_quoted_above_tail(
        self, tmp_path: Path,
    ) -> None:
        # Both dialog patterns appear above the footer region, but the
        # last 5 non-blank rows are plain prose (no patterns).  The
        # tail-only check must reject this — patterns must appear in
        # the dialog-footer region (last 5 lines) to count.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')
        tracker.on_send()
        write_signal(tracker, 'idle')
        tracker.get_state(pty_alive=True)

        feed_screen_text(
            tracker,
            'When Claude shows a dialog, you see\n'
            '"Enter to select · Esc to cancel" in the footer.\n'
            'It is the standard pattern across all Ink TUIs.\n'
            'Line four of the response.\n'
            'Line five of the response.\n'
            'Line six of the response.\n'
            'Line seven, well past the tail window.\n'
            'Line eight is also clean prose.\n'
            'End of explanation here.\n',
        )
        assert tracker._state == 'idle'


# ---------------------------------------------------------------------------
# screen_has_active_dialog — used by the server's ↑/↓ input filter to
# skip history-recall interception when a dialog is visible on screen
# but the state tracker hasn't yet flipped to NEEDS_PERMISSION (notably
# AskUserQuestion, which fires no Notification hook so the state stays
# RUNNING until the 5 s cursor+silence fallback fires).  Without this
# check, arrows pressed during that window would be stolen for history
# recall instead of navigating the dialog.
# ---------------------------------------------------------------------------

class TestScreenHasActiveDialog:
    """The real-world trigger is mid-RUNNING (state=running, dialog on
    screen, no Notification fired).  Use ``on_send`` to enter RUNNING
    cleanly before feeding output so the startup-dialog detector doesn't
    consume the dialog out from under us before we can query the screen.
    """

    def test_returns_true_when_dialog_in_bottom_5_rows(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running, _seen_user_input=True
        feed_screen_text(
            tracker,
            'Working on the task...\n'
            'Do you want to proceed?\n'
            '> 1. Yes\n'
            '  2. No\n'
            'Enter to select · Esc to cancel',
        )
        assert tracker.screen_has_active_dialog() is True

    def test_returns_false_when_screen_is_empty(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        assert tracker.screen_has_active_dialog() is False

    def test_returns_false_when_only_one_pattern_present(
        self, tmp_path: Path,
    ) -> None:
        # Conversational text mentioning a SINGLE shortcut must not
        # false-trigger — same safety property as the proactive idle
        # detection.  Without this, response text that quotes one of
        # the dialog footer phrases would block arrow keys from being
        # captured for history recall.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'The keyboard shortcut is Esc to cancel — useful for '
            'aborting a long-running operation.',
        )
        assert tracker.screen_has_active_dialog() is False

    def test_returns_false_when_dialog_pushed_out_of_bottom_5(
        self, tmp_path: Path,
    ) -> None:
        # Tail-only restriction: a dialog that scrolled out of the
        # bottom 5 rows is no longer "active" (the user is past it).
        # The realistic screen state during this scenario is that
        # Claude's idle input box is back at the bottom — without it,
        # the structural fallback would correctly flag "something
        # interactive is up" because there's no anchor.
        # \r\n separators are required because real Claude output uses
        # CRLF; the test helper's bare \n would otherwise leave each
        # subsequent row indented past the previous one's end column.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        hr = '─' * 100
        feed_screen_text(
            tracker,
            'Enter to select  Esc to cancel\r\n'
            'Now working on step one\r\n'
            'Now working on step two\r\n'
            'Now working on step three\r\n'
            'Now working on step four\r\n'
            'Now working on step five\r\n'
            'Now working on step six\r\n'
            f'{hr}\r\n'
            '❯\r\n'
            f'{hr}\r\n'
            '? for shortcuts',
        )
        assert tracker.screen_has_active_dialog() is False

    def test_codex_selection_dialog_detected_despite_empty_patterns(
        self, tmp_path: Path,
    ) -> None:
        # Codex has empty dialog_patterns, but the generic selection-dialog
        # detector (numbered ›/❯ cursor + confirm/cancel footer) must still
        # detect its arrow-navigable dialogs so ↑/↓ navigate the dialog instead
        # of being stolen for history recall. Real Codex trust dialog (uses the
        # › U+203A cursor):
        from leap.cli_providers.codex import CodexProvider
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Do you trust the authors of this folder and want to let them '
            'run hooks?\n'
            '› 1. Review hooks\n'
            '  2. Trust all and continue\n'
            "  3. Continue without trusting (hooks won't run)\n"
            'Press enter to confirm or esc to go back\n',
        )
        assert tracker.screen_has_active_dialog() is True

    def test_codex_idle_prompt_is_not_a_dialog(
        self, tmp_path: Path,
    ) -> None:
        # Codex's idle input box also uses the › glyph (ghost-text hint) and a
        # model/status footer — it must NOT be read as a selection dialog, or
        # ↑/↓ history recall would never work for Codex.
        from leap.cli_providers.codex import CodexProvider
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_send()
        feed_screen_text(
            tracker,
            '› Use /skills to list available skills\n'
            'gpt-5.5   default   fast   · ~\n',
        )
        assert tracker.screen_has_active_dialog() is False

    def test_cursor_does_not_cross_match_across_rows(
        self, tmp_path: Path,
    ) -> None:
        # A row ending in › followed by a row starting "1." must not be read as
        # a "› 1." selection cursor (the regex is applied per row, not on the
        # joined screen text).
        from leap.cli_providers.codex import CodexProvider
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_send()
        feed_screen_text(
            tracker,
            'see the quote that ends with ›\n'
            '1. this is just response prose, not an option\n',
        )
        assert tracker.screen_has_active_dialog() is False

    def test_picker_detected_without_a_cursor_glyph(
        self, tmp_path: Path,
    ) -> None:
        # Glyph-independent: a picker whose selection marker isn't ›/❯ (here a
        # plain `*`) is still detected via its footer line (>=2 nav hints).
        from leap.cli_providers.codex import CodexProvider
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Pick a model\n'
            '* gpt-5\n'
            '  opus\n'
            'enter to confirm   esc to cancel\n',
        )
        assert tracker.screen_has_active_dialog() is True

    def test_short_single_hint_footer_detected(
        self, tmp_path: Path,
    ) -> None:
        # A short hint-only footer line is a footer; a long prose sentence
        # quoting the same phrase is not (covered by the one-pattern test).
        from leap.cli_providers.codex import CodexProvider
        t = [0.0]
        tracker = make_tracker(tmp_path, t, provider=CodexProvider())
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Delete this file?\n'
            'Yes\n'
            'No\n'
            'Esc to cancel\n',
        )
        assert tracker.screen_has_active_dialog() is True

    def test_returns_false_when_idle_prompt_box_visible(
        self, tmp_path: Path,
    ) -> None:
        # During normal RUNNING with response flowing, Claude's idle
        # input box stays anchored at the bottom of the screen.  Even
        # if some upstream text mentioned ``Esc to cancel`` (e.g.
        # response prose) the structural check should see the
        # ``HR / ❯ / HR`` sandwich and treat the screen as idle.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        hr = '─' * 100
        feed_screen_text(
            tracker,
            'Some response prose mentioning Esc to cancel here.\r\n'
            'More response text on the next line.\r\n'
            'And one more line of prose.\r\n'
            'Even more prose.\r\n'
            'Last line of prose.\r\n'
            f'{hr}\r\n'
            '❯\r\n'
            f'{hr}\r\n'
            '? for shortcuts',
        )
        assert tracker.screen_has_active_dialog() is False

    # ------------------------------------------------------------------
    # Slash-command pickers (/resume, /mcp, /agents, /config, /effort,
    # /model, /memory, /login, /doctor, /usage, /bug, /permissions, …)
    # — these stay in RUNNING state forever while the picker is open
    # (no hook fires) and replace Claude's idle input box with the
    # picker UI.  Detection is structural: the ``HR / ❯ / HR`` sandwich
    # is gone from the bottom of the screen.
    # ------------------------------------------------------------------

    def test_returns_true_for_resume_picker_shape(
        self, tmp_path: Path,
    ) -> None:
        # /resume footer pairs ``Type to search`` with ``Esc to cancel``
        # — no ``Enter to select`` at all.  Strict dialog check misses
        # it, but the idle prompt box is absent so the structural
        # fallback fires.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Resume session (1 of 50)\r\n'
            'Search…\r\n'
            '  leap\r\n'
            '> Investigate JetBrains remove LPS mechanism\r\n'
            '    24 seconds ago · main · 774.9KB\r\n'
            '  Debug image display interruption issue\r\n'
            '    12 hours ago · main · 1MB\r\n'
            'Ctrl+A to show all projects · Type to search · Esc to '
            'cancel',
        )
        assert tracker.screen_has_active_dialog() is True

    def test_returns_true_for_mcp_picker_shape(
        self, tmp_path: Path,
    ) -> None:
        # /mcp footer: ``Enter to confirm`` (not ``select``) + ``Esc to
        # cancel`` — also outside the strict dialog footer pair.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Manage MCP servers\r\n'
            '13 servers\r\n'
            'User MCPs\r\n'
            '> cmd-executor · connected · 1 tool\r\n'
            '  claude.ai Atlassian · connected · 31 tools\r\n'
            '  claude.ai Slack · connected · 13 tools\r\n'
            '  https://code.claude.com/docs/en/mcp for help\r\n'
            '↑/↓ to navigate · Enter to confirm · Esc to cancel',
        )
        assert tracker.screen_has_active_dialog() is True

    def test_returns_true_for_agents_picker_shape(
        self, tmp_path: Path,
    ) -> None:
        # /agents footer: ``Esc to close`` (not ``cancel``) + ``Enter
        # to select`` — strict dialog check requires ``Esctocancel``,
        # so this picker formerly slipped through entirely.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(
            tracker,
            'Agents  Running   Library\r\n'
            'Filler row to push past the small-screen threshold.\r\n'
            'Another filler row for the same reason.\r\n'
            'And one more.\r\n'
            'No subagents are currently running.\r\n'
            '←/→ to switch · ↑/↓ to navigate · '
            'Enter to select · Esc to close',
        )
        assert tracker.screen_has_active_dialog() is True


class TestInteractiveUiKeepsRunningOnSilence:
    """An ``is_dialog_certain`` miss must NOT be read as "idle" when an
    interactive UI owns the bottom of the screen (idle input box gone).

    Slash-command pickers (``/model``, ``/resume``, ``/mcp``, …) fire no
    hook and sit in RUNNING.  Their footers are NOT the strict
    ``Enter to select`` + ``Esc to cancel`` form, and the cursor on a
    later option means the ``❯1.`` numbered-menu fallback misses too — so
    ``is_dialog_certain`` is False.  Older / alternate dialog footers
    (``Esc to close``, ``Enter to approve``, multi-select) miss the same
    way.  Before the fix, after >5 s of user deliberation the
    cursor+silence running→idle fallback fired, flipped RUNNING→IDLE and
    ``_reset_screen()``-ed the live UI; then ``screen_has_active_dialog()``
    read the blanked screen as "no dialog" and ↑/↓ got stolen for history
    recall — the reported "arrows get stuck in a picker after a few
    seconds" bug (and the false-idle let the auto-sender inject a queued
    message into the open UI).

    The guard: while the idle prompt is absent (the same structural
    signal ``screen_has_active_dialog`` already trusts), stay RUNNING
    without resetting.  Reproduced live against ``/model`` and ``/resume``.
    """

    # Realistic /model-style picker: 7 non-blank rows, no idle input-box
    # sandwich, footer lacks "Enter to select", cursor on option 5 (so the
    # "❯1." numbered-menu fallback also misses) → is_dialog_certain False.
    _PICKER = (
        'Select model\r\n'
        '  1. Default\r\n'
        '  2. Sonnet\r\n'
        '  3. Opus\r\n'
        '  4. Haiku\r\n'
        '❯ 5. Sonnet 4.6\r\n'
        'Enter to set as default · s to use this session only · '
        'Esc to cancel'
    )

    def test_picker_open_with_silence_stays_running(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()  # → running (e.g. user typed /model + Enter)
        t[0] = 1.0
        feed_with_visible_cursor(tracker, self._PICKER)
        # Sanity: the footer really does miss the strict dialog check.
        filled = [ln for ln in tracker._get_display_lines() if ln.strip()]
        compact = ''.join(filled[-5:]).replace(' ', '')
        assert tracker._provider.is_dialog_certain(compact) is False
        # >5 s of user deliberation with no keypress.
        t[0] = 8.0
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_picker_silence_keeps_arrows_navigable(
        self, tmp_path: Path,
    ) -> None:
        # The user-facing symptom: after the silence poll, the ↑/↓ input
        # filter must still route arrows to the picker (not history
        # recall) — i.e. the screen was NOT blanked.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(tracker, self._PICKER)
        t[0] = 8.0
        tracker.get_state(pty_alive=True)
        assert tracker.screen_has_active_dialog() is True

    def test_genuine_idle_box_still_idles(self, tmp_path: Path) -> None:
        # No-regression: when the real idle input box IS on screen (>=5
        # rows so the small-screen shortcut doesn't apply), silence still
        # flips to idle — the guard only holds RUNNING for an interactive
        # UI, not for the genuinely idle prompt.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        hr = '─' * 80
        feed_with_visible_cursor(
            tracker,
            'I have finished the task.\r\n'
            'Here is a short summary of what changed.\r\n'
            'Let me know if you need anything else.\r\n'
            f'{hr}\r\n'
            '❯ \r\n'
            f'{hr}\r\n'
            '? for shortcuts',
        )
        # Confirm the idle box is detected (otherwise the test proves
        # nothing about the guard).
        filled = [ln for ln in tracker._get_display_lines() if ln.strip()]
        assert tracker._provider.is_idle_prompt_visible(filled) is True
        t[0] = 8.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_plain_text_without_cursor_idles(self, tmp_path: Path) -> None:
        # The narrowing: an absent idle box is NOT enough on its own to hold
        # RUNNING.  Plain response text - a numbered list with no ❯ selection
        # cursor - has no idle box yet is not an interactive UI, so it must
        # idle rather than get stuck RUNNING (the false positive the broad
        # "box absent -> running" guard caused).
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker,
            'Here are your options:\r\n'
            '1. Option A\r\n'
            '2. Option B\r\n'
            '3. Option C\r\n'
            '> ',
        )
        filled = [ln for ln in tracker._get_display_lines() if ln.strip()]
        # Box absent (the old broad guard would have held RUNNING) but no
        # selection cursor -> not a picker.
        assert tracker._provider.is_idle_prompt_visible(filled) is False
        assert tracker._provider.has_selection_cursor(filled) is False
        t[0] = 8.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_footer_only_dialog_without_cursor_stays_running(
        self, tmp_path: Path,
    ) -> None:
        # Footer fallback: a tabbed view (e.g. /agents) shows no ❯/› cursor
        # but DOES render a nav/dismiss footer.  With the idle box absent it
        # must stay RUNNING so ↑/↓ keep reaching it - the case
        # has_selection_cursor alone would miss.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker,
            'Using Sonnet 4.6 · /model to change\r\n'
            'Agents   Running   Library\r\n'
            'No subagents are currently running.\r\n'
            'Create one with the Task tool.\r\n'
            'Switch tabs to see your library.\r\n'
            '←/→ to switch · ↑/↓ to navigate · Esc to close',
        )
        filled = [ln for ln in tracker._get_display_lines() if ln.strip()]
        assert tracker._provider.is_idle_prompt_visible(filled) is False
        assert tracker._provider.has_selection_cursor(filled) is False
        assert tracker._provider.has_interactive_footer(filled) is True
        t[0] = 8.0
        assert tracker.get_state(pty_alive=True) == 'running'


# ---------------------------------------------------------------------------
# Answering a dialog must NOT reset the pyte screen.  A multi-question
# AskUserQuestion advances to the next question via an Ink INCREMENTAL
# repaint that never re-emits the unchanged footer.  If the answer-Enter
# reset wipes the footer, then for the ~5 s until Claude's next full
# re-render the live screen has no dialog footer, which drives two bugs
# (both confirmed against a real Claude session log):
#   * the cursor+silence check reads "no dialog" and flips RUNNING->idle,
#     falsely marking the still-pending question as done, and
#   * the up/down input filter steals the arrows for history recall.
# Leaving pyte intact for PROMPT-state answers keeps the footer, so both
# stay correct; IDLE/INTERRUPTED answers still reset (stale scrollback).
# ---------------------------------------------------------------------------

class TestDialogAnswerKeepsScreen:
    # Real Ink AskUserQuestion shape: cursor VISIBLE (the real terminal
    # promotes via cursor+silence, which needs a visible cursor) + footer.
    @staticmethod
    def _render_q1(tracker: ClaudeStateTracker) -> None:
        tracker.on_output(
            b"\x1b[?25h\x1b[2J"
            b"\x1b[1;1HWhat is your favorite color?\x1b[K"
            b"\x1b[2;1H  1. Red\x1b[K"
            b"\x1b[3;1H  2. Green\x1b[K"
            b"\x1b[4;1H  3. Blue\x1b[K"
            b"\x1b[5;1HEnter to select \xc2\xb7 Esc to cancel\x1b[K"
        )

    @staticmethod
    def _render_incremental_q2(tracker: ClaudeStateTracker) -> None:
        # Advancing to the next question: only the changed cells (question
        # text + option labels) are rewritten.  The footer is identical, so
        # Ink never re-emits it - with the reset skipped it survives from Q1.
        tracker.on_output(
            b"\x1b[1;1HWhat is your favorite food? \x1b[K"
            b"\x1b[2;6HPizza\x1b[K"
            b"\x1b[3;6HSushi\x1b[K"
            b"\x1b[4;6HTacos\x1b[K"
        )

    def _answer_q1(self, tracker: ClaudeStateTracker, t: List[float]) -> None:
        # Q1 reaches needs_permission via cursor+silence (cursor visible),
        # exactly as the real log shows; then the user answers with Enter.
        tracker.on_send()
        self._render_q1(tracker)
        t[0] = 106.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'
        tracker.on_input(b'\r')   # answer -> running, NO reset (the fix)
        assert tracker.current_state == 'running'

    def test_answer_keeps_footer_so_next_question_is_navigable(
        self, tmp_path: Path,
    ) -> None:
        """Arrow regression: after answering Q1, the next question
        (incremental repaint) keeps its footer because pyte is not reset,
        so screen_has_active_dialog() stays True and up/down navigate it.
        Pre-fix the reset wiped the footer and this returned False."""
        t = [100.0]
        tracker = make_tracker(tmp_path, t)
        self._answer_q1(tracker, t)
        t[0] = 106.3
        self._render_incremental_q2(tracker)
        with tracker._screen_lock:
            assert 'Esc to cancel' in tracker._get_screen_text()  # preserved
        assert tracker.screen_has_active_dialog() is True

    def test_no_false_idle_after_answering_multi_question_dialog(
        self, tmp_path: Path,
    ) -> None:
        """False-idle regression (the reported bug: the 2nd question showed
        Idle).  The preserved footer keeps is_dialog_certain True, so the
        cursor+silence check promotes the next question to needs_permission
        instead of falsely flipping to idle.  Pre-fix (reset) this idled."""
        t = [100.0]
        tracker = make_tracker(tmp_path, t)
        self._answer_q1(tracker, t)
        t[0] = 106.3
        self._render_incremental_q2(tracker)   # footer survives
        # 5s+ of post-answer silence with the cursor visible: must promote
        # back to needs_permission (dialog still on screen), NOT idle.
        t[0] = 112.0
        assert tracker.get_state(pty_alive=True) == 'needs_permission'

    def test_new_prompt_from_idle_still_resets_screen(
        self, tmp_path: Path,
    ) -> None:
        """Regression guard: a fresh prompt (Enter from IDLE) MUST still
        reset, clearing the previous turn's scrollback."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)   # starts IDLE
        tracker.on_output(
            b"\x1b[2J\x1b[1;1Hstale scrollback from a previous turn")
        tracker.on_input(b'\r')   # IDLE -> running, MUST reset
        with tracker._screen_lock:
            assert 'stale scrollback' not in tracker._get_screen_text()

    def test_interrupt_reply_still_resets_screen(
        self, tmp_path: Path,
    ) -> None:
        """Regression guard: replying to the interrupt prompt (Enter from
        INTERRUPTED) MUST still reset, clearing the 'Interrupted' marker so
        stale-interrupt detection doesn't re-fire."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        tracker.on_input(b'\x1b')   # Esc -> interrupt pending
        feed_screen_text(tracker, 'Interrupted')
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'interrupted'
        tracker.on_input(b'fix it\r')   # INTERRUPTED -> running, MUST reset
        with tracker._screen_lock:
            assert 'Interrupted' not in tracker._get_screen_text()


# ---------------------------------------------------------------------------
# Claude conversation-compaction detection
# ---------------------------------------------------------------------------

class TestClaudeCompactingIndicator:
    """Claude Code runs /compact and auto-compact without firing any
    hook for the compaction itself.  Between-turns auto-compact starts
    right after a Stop hook wrote 'idle' — without running-indicator
    detection the session would read as idle for the full duration."""

    def test_idle_transitions_to_running_when_compacting_appears(
        self, tmp_path: Path,
    ) -> None:
        """Auto-compact fires right after Stop → indicator moves idle→running."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_input(b'x')  # seen user input
        tracker.on_send()
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

        t[0] = 1.0
        feed_screen_text(tracker, '* Compacting conversation...')
        assert tracker.current_state == 'running'

    def test_idle_to_running_needs_seen_user_input(
        self, tmp_path: Path,
    ) -> None:
        """Before any user input, indicator-based transition is suppressed
        (matches the general gating for post-startup checks)."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        feed_screen_text(tracker, '* Compacting conversation...')
        assert tracker.current_state == 'idle'

    def test_running_idle_signal_held_while_compacting(
        self, tmp_path: Path,
    ) -> None:
        """Stop hook writing idle during an on-screen compaction must not
        flip the state to idle - and the signal must be PRESERVED, not
        consumed: no further Stop fires after a between-turns auto-compact,
        so this signal is the only authoritative idle left.  Once the
        indicator clears it applies."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(tracker, '* Compacting conversation... (12s)')
        assert tracker.current_state == 'running'

        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'running'
        # Preserved for re-evaluation on the next poll.
        assert tracker._signal_file.exists()
        # Compaction keeps repainting (live spinner) - still held.
        t[0] = 8.0
        feed_screen_text(tracker, '* Compacting conversation... (20s)')
        assert tracker.get_state(pty_alive=True) == 'running'
        # Indicator clears - the preserved signal applies.
        t[0] = 9.0
        feed_screen_text(tracker, '> ')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_quoted_compacting_text_does_not_eat_the_stop_signal(
        self, tmp_path: Path,
    ) -> None:
        """Regression: a response whose text merely QUOTES the busy phrase
        near the bottom used to consume-and-drop the Stop signal while every
        heuristic fallback stayed gated on the same indicator - wedging the
        session in RUNNING with no cap.  A live spinner repaints every
        second; static quoted text does not, so after a silent stretch the
        preserved signal must apply."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_screen_text(
            tracker,
            'The status line then shows\r\n'
            'Compacting conversation while it works.\r\n'
            '> ',
        )
        write_signal(tracker, 'idle')
        # Within the live-spinner window: held (could still be a real
        # compaction that just started).
        t[0] = 2.0
        assert tracker.get_state(pty_alive=True) == 'running'
        assert tracker._signal_file.exists()
        # Output stays silent past the live-spinner window: static text,
        # the preserved signal applies.
        t[0] = 15.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_quoted_compacting_text_above_tail_window_idles_immediately(
        self, tmp_path: Path,
    ) -> None:
        """The indicator is only matched near the bottom of the screen -
        a quote that scrolled above the tail window must not read as busy
        at all, so the Stop signal applies on the first poll."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        filler = '\r\n'.join(f'response line {i}' for i in range(16))
        feed_screen_text(
            tracker,
            'Compacting conversation is what the spinner says.\r\n'
            + filler + '\r\n> ',
        )
        write_signal(tracker, 'idle')
        t[0] = 2.0
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_cursor_silence_fallback_skipped_while_compacting(
        self, tmp_path: Path,
    ) -> None:
        """The running→idle cursor+silence fallback must not fire while
        the compaction indicator is on screen."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        # Cursor visible + indicator on screen.
        feed_with_visible_cursor(
            tracker, '* Compacting conversation... (3s)',
        )
        # Advance past the 5s silence window without any new output.
        t[0] = 10.0
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_silence_safety_timeout_skipped_while_compacting(
        self, tmp_path: Path,
    ) -> None:
        """Safety silence timeout must not force-idle while compacting."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(tracker, '* Compacting conversation...')
        t[0] = SAFETY_SILENCE_TIMEOUT + 10.0
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_compaction_end_allows_idle_transition(
        self, tmp_path: Path,
    ) -> None:
        """Once the indicator is gone (compaction finished), a subsequent
        idle signal is honoured normally."""
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        feed_screen_text(tracker, '* Compacting conversation...')
        assert tracker.current_state == 'running'

        # Compaction finishes — indicator replaced by the normal prompt.
        feed_screen_text(tracker, '> ')
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'

    def test_other_providers_unaffected(
        self, tmp_path: Path,
    ) -> None:
        """Providers without running indicators keep the default behaviour."""
        t = [0.0]
        tracker = make_tracker(
            tmp_path, t, provider=CodexProvider(),
        )
        assert tracker._provider.running_indicator_patterns == []
        tracker.on_input(b'x')
        feed_screen_text(tracker, 'Compacting conversation...')
        # No idle→running transition — the pattern is provider-specific.
        assert tracker.current_state == 'idle'


# ---------------------------------------------------------------------------
# Transcript classification: running vs interrupted vs idle
# ---------------------------------------------------------------------------

class TestTranscriptClassification:
    """Direct unit tests for ``ClaudeProvider.transcript_says_running`` and
    ``transcript_says_interrupted``.

    Exercises the JSONL walk that gates the three "would idle" code paths
    in ``CLIStateTracker.get_state``.  Without correct interrupt detection,
    a user-cancelled tool_use leaves the state machine stuck in RUNNING
    until the safety timeout — observed live with a session sitting at
    "Interrupted · What should Claude do instead?" while the monitor
    showed Running.
    """

    @staticmethod
    def _setup(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        entries: list[dict],
    ) -> tuple[ClaudeProvider, dict]:
        """Build a fake projects root + transcript + cli_sessions record,
        then return ``(provider, kwargs)`` ready to call the classifier."""
        from leap.cli_providers import claude as claude_mod
        from leap.utils.claude_session_move import slugify

        root = tmp_path / 'projects'
        root.mkdir()
        monkeypatch.setattr(claude_mod, '_TRANSCRIPT_PROJECTS_ROOT', root)

        cwd = str(tmp_path / 'project')
        sid = 'abc-def-123'
        slug_dir = root / slugify(cwd)
        slug_dir.mkdir(parents=True)
        transcript = slug_dir / f'{sid}.jsonl'
        transcript.write_text(
            '\n'.join(json.dumps(e) for e in entries) + '\n',
        )

        # Record the session_id so the resolver finds the file via the
        # precise tag→jsonl path rather than the mtime fallback.
        storage = tmp_path / '.storage'
        (storage / 'cli_sessions' / 'claude').mkdir(parents=True)
        (storage / 'cli_sessions' / 'claude' / 'test.json').write_text(
            json.dumps([{
                'session_id': sid,
                'transcript_path': str(transcript),
                'cwd': cwd,
                'last_seen': 0.0,
            }]),
        )

        return ClaudeProvider(), dict(
            since=1000.0,
            cwd=cwd,
            tag='test',
            storage_dir=storage,
        )

    def _ts(self, epoch: float) -> str:
        """Format an epoch as the ISO-8601 Z string the Claude CLI writes."""
        from datetime import datetime, timezone
        return datetime.fromtimestamp(
            epoch, tz=timezone.utc,
        ).isoformat().replace('+00:00', 'Z')

    # ---- running classification -----------------------------------------

    def test_running_when_tool_use_pending(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Latest assistant entry in the current turn has stop_reason
        'tool_use' and no tool_result has been written → running."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(2000.0),
                'message': {'stop_reason': 'tool_use'},
            },
        ])
        assert p.transcript_says_running(**kw) is True
        assert p.transcript_says_interrupted(**kw) is False

    def test_running_after_tool_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user tool_result entry following a tool_use does NOT
        terminate the loop — Claude is processing the result."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(2000.0),
                'message': {'stop_reason': 'tool_use'},
            },
            {
                'type': 'user',
                'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': 'toolu_x'},
                ]},
            },
        ])
        # Still running: the latest assistant entry is tool_use and
        # the only user entry between it and the tail is a tool_result.
        assert p.transcript_says_running(**kw) is True
        assert p.transcript_says_interrupted(**kw) is False

    def test_idle_on_end_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Assistant stop_reason 'end_turn' → neither running nor
        interrupted (the loop ended cleanly)."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(2000.0),
                'message': {'stop_reason': 'end_turn'},
            },
        ])
        assert p.transcript_says_running(**kw) is False
        assert p.transcript_says_interrupted(**kw) is False

    # ---- interrupted classification -------------------------------------

    def test_interrupted_after_tool_use(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """User cancelled a tool_use call — the assistant entry shows
        stop_reason 'tool_use' but a [Request interrupted by user] user
        entry was written above it.  This is the bug repro: previously
        ``transcript_says_running`` ignored the user-interrupt entry and
        returned True forever, jamming the state machine in RUNNING."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(2000.0),
                'message': {'stop_reason': 'tool_use'},
            },
            {
                'type': 'system',
                'subtype': 'api_error',
                'timestamp': self._ts(2010.0),
            },
            {
                'type': 'user',
                'timestamp': self._ts(2020.0),
                'message': {'content': [
                    {'type': 'text',
                     'text': '[Request interrupted by user]'},
                ]},
            },
        ])
        assert p.transcript_says_running(**kw) is False
        assert p.transcript_says_interrupted(**kw) is True

    def test_interrupted_with_intervening_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Metadata-style entries with no ``type``/``timestamp`` between
        the assistant tool_use and the user-interrupt entry are skipped
        cleanly (real transcripts have permission-mode / agent-name /
        ai-title / last-prompt blocks here)."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(2000.0),
                'message': {'stop_reason': 'tool_use'},
            },
            {'permission-mode': 'auto'},
            {'agent-name': 'main'},
            {
                'type': 'user',
                'timestamp': self._ts(2020.0),
                'message': {'content': [
                    {'type': 'text',
                     'text': '[Request interrupted by user]'},
                ]},
            },
        ])
        assert p.transcript_says_running(**kw) is False
        assert p.transcript_says_interrupted(**kw) is True

    def test_old_interrupt_does_not_carry_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A [Request interrupted by user] entry from a previous turn,
        followed by a fresh assistant tool_use, is not a current
        interrupt — running."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            # Previous turn — interrupted
            {
                'type': 'user',
                'timestamp': self._ts(1500.0),
                'message': {'content': [
                    {'type': 'text',
                     'text': '[Request interrupted by user]'},
                ]},
            },
            # Fresh turn — assistant resumed
            {
                'type': 'user',
                'timestamp': self._ts(1800.0),
                'message': {'content': [
                    {'type': 'text', 'text': 'please continue'},
                ]},
            },
            {
                'type': 'assistant',
                'timestamp': self._ts(2000.0),
                'message': {'stop_reason': 'tool_use'},
            },
        ])
        assert p.transcript_says_running(**kw) is True
        assert p.transcript_says_interrupted(**kw) is False

    # ---- timestamp guard ------------------------------------------------

    def test_stale_assistant_entry_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the most recent assistant entry predates ``since`` (the
        last on_send time), it belongs to a previous turn — neither
        running nor interrupted from the current turn's perspective."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(500.0),  # < since=1000.0
                'message': {'stop_reason': 'tool_use'},
            },
        ])
        assert p.transcript_says_running(**kw) is False
        assert p.transcript_says_interrupted(**kw) is False

    def test_stale_interrupt_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user-interrupt entry above a *stale* assistant entry is
        still considered no-current-turn — returns False for both
        predicates."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(500.0),  # < since
                'message': {'stop_reason': 'tool_use'},
            },
            {
                'type': 'user',
                'timestamp': self._ts(600.0),
                'message': {'content': [
                    {'type': 'text',
                     'text': '[Request interrupted by user]'},
                ]},
            },
        ])
        assert p.transcript_says_running(**kw) is False
        assert p.transcript_says_interrupted(**kw) is False

    # ---- error / absent transcript -------------------------------------

    def test_missing_transcript_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No transcript on disk → both predicates return False rather
        than raising."""
        from leap.cli_providers import claude as claude_mod
        root = tmp_path / 'projects'
        root.mkdir()
        monkeypatch.setattr(claude_mod, '_TRANSCRIPT_PROJECTS_ROOT', root)

        p = ClaudeProvider()
        kwargs = dict(
            since=1000.0,
            cwd=str(tmp_path / 'project'),  # slug dir does not exist
            tag='test',
            storage_dir=tmp_path / '.storage',
        )
        assert p.transcript_says_running(**kwargs) is False
        assert p.transcript_says_interrupted(**kwargs) is False

    def test_other_providers_default_to_false(self) -> None:
        """Base implementation (e.g. CodexProvider) must always return
        False — no transcript awareness."""
        c = CodexProvider()
        kw = dict(since=0.0, cwd='/', tag='x', storage_dir=None)
        assert c.transcript_says_running(**kw) is False
        assert c.transcript_says_interrupted(**kw) is False

    # ---- content-shape coverage ----------------------------------------

    def test_interrupt_with_string_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Older transcripts store ``message.content`` as a plain string
        rather than a list of typed blocks.  The classifier must accept
        both shapes; missing the string form would let an old-format
        transcript wedge the state machine."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(2000.0),
                'message': {'stop_reason': 'tool_use'},
            },
            {
                'type': 'user',
                'timestamp': self._ts(2020.0),
                'message': {'content': '[Request interrupted by user]'},
            },
        ])
        assert p.transcript_says_running(**kw) is False
        assert p.transcript_says_interrupted(**kw) is True

    def test_interrupt_text_block_mixed_with_other_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user entry with multiple content blocks (e.g. text +
        attached image) where one block is the interrupt marker still
        registers as an interrupt."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(2000.0),
                'message': {'stop_reason': 'tool_use'},
            },
            {
                'type': 'user',
                'timestamp': self._ts(2020.0),
                'message': {'content': [
                    {'type': 'image', 'source': {'data': '...'}},
                    {'type': 'text',
                     'text': '[Request interrupted by user]'},
                ]},
            },
        ])
        assert p.transcript_says_interrupted(**kw) is True

    def test_text_block_with_user_text_not_interrupt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user text block whose payload is the user's actual prompt
        — even one quoting the marker phrase elsewhere — must NOT
        trigger interrupt detection.  Exact string equality only."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(2000.0),
                'message': {'stop_reason': 'tool_use'},
            },
            {
                'type': 'user',
                'timestamp': self._ts(2020.0),
                'message': {'content': [
                    {'type': 'text',
                     'text': 'why does it say [Request interrupted by '
                             'user]?'},
                ]},
            },
        ])
        assert p.transcript_says_interrupted(**kw) is False
        # Running classification: there's a user entry between the
        # tool_use and the tail (not a tool_result, not an interrupt),
        # which means the user submitted a fresh prompt — the tool_use
        # is from the previous step of the same turn, agent is still
        # processing.  Returns True.
        assert p.transcript_says_running(**kw) is True

    # ---- multi-interrupt and tool_result interaction --------------------

    def test_double_interrupt_same_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two ``[Request interrupted by user]`` entries in a row (user
        pressed Esc twice without typing anything in between).  Same
        classification — interrupted."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(2000.0),
                'message': {'stop_reason': 'tool_use'},
            },
            {
                'type': 'user',
                'timestamp': self._ts(2010.0),
                'message': {'content': [
                    {'type': 'text',
                     'text': '[Request interrupted by user]'},
                ]},
            },
            {
                'type': 'user',
                'timestamp': self._ts(2020.0),
                'message': {'content': [
                    {'type': 'text',
                     'text': '[Request interrupted by user]'},
                ]},
            },
        ])
        assert p.transcript_says_interrupted(**kw) is True

    def test_interrupt_after_tool_result_in_same_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tool ran (tool_result written), then user interrupted before
        the next API call.  Classifier walks past the tool_result
        (which doesn't trigger the marker check), past the assistant
        tool_use, and returns 'interrupted'."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(2000.0),
                'message': {'stop_reason': 'tool_use'},
            },
            {
                'type': 'user',
                'timestamp': self._ts(2005.0),
                'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': 'toolu_x',
                     'content': 'ok'},
                ]},
            },
            {
                'type': 'user',
                'timestamp': self._ts(2020.0),
                'message': {'content': [
                    {'type': 'text',
                     'text': '[Request interrupted by user]'},
                ]},
            },
        ])
        assert p.transcript_says_running(**kw) is False
        assert p.transcript_says_interrupted(**kw) is True

    # ---- malformed input ------------------------------------------------

    def test_missing_assistant_timestamp_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Assistant entry without a ``timestamp`` field — we can't
        decide if it belongs to the current turn, so neither predicate
        fires.  Defensive: must not raise."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'message': {'stop_reason': 'tool_use'},
            },
        ])
        assert p.transcript_says_running(**kw) is False
        assert p.transcript_says_interrupted(**kw) is False

    def test_malformed_timestamp_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Garbled timestamp string — same defensive behaviour as
        missing timestamp."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': 'not-a-timestamp',
                'message': {'stop_reason': 'tool_use'},
            },
        ])
        assert p.transcript_says_running(**kw) is False
        assert p.transcript_says_interrupted(**kw) is False

    def test_garbled_json_lines_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-JSON line (truncated write, log spillover) is skipped
        — must not derail the walk."""
        from leap.cli_providers import claude as claude_mod
        from leap.utils.claude_session_move import slugify

        root = tmp_path / 'projects'
        root.mkdir()
        monkeypatch.setattr(claude_mod, '_TRANSCRIPT_PROJECTS_ROOT', root)

        cwd = str(tmp_path / 'project')
        sid = 'abc-def'
        slug_dir = root / slugify(cwd)
        slug_dir.mkdir(parents=True)
        good = {
            'type': 'assistant',
            'timestamp': self._ts(2000.0),
            'message': {'stop_reason': 'tool_use'},
        }
        (slug_dir / f'{sid}.jsonl').write_text(
            'this is not valid json\n'
            + json.dumps(good) + '\n'
            + '{"truncated":\n',
        )

        storage = tmp_path / '.storage'
        (storage / 'cli_sessions' / 'claude').mkdir(parents=True)
        (storage / 'cli_sessions' / 'claude' / 'test.json').write_text(
            json.dumps([{
                'session_id': sid,
                'transcript_path': str(slug_dir / f'{sid}.jsonl'),
                'cwd': cwd,
                'last_seen': 0.0,
            }]),
        )

        p = ClaudeProvider()
        kw = dict(
            since=1000.0, cwd=cwd, tag='test', storage_dir=storage,
        )
        assert p.transcript_says_running(**kw) is True

    def test_empty_user_content_list_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user entry with ``content: []`` (degenerate) doesn't trip
        the interrupt detector and doesn't stop the walk."""
        p, kw = self._setup(tmp_path, monkeypatch, [
            {
                'type': 'assistant',
                'timestamp': self._ts(2000.0),
                'message': {'stop_reason': 'tool_use'},
            },
            {
                'type': 'user',
                'timestamp': self._ts(2010.0),
                'message': {'content': []},
            },
        ])
        # User entry skipped — walk continues to the assistant entry.
        assert p.transcript_says_running(**kw) is True
        assert p.transcript_says_interrupted(**kw) is False


# ---------------------------------------------------------------------------
# State machine wiring: transcript-driven RUNNING → INTERRUPTED
# ---------------------------------------------------------------------------

class _StubTranscriptProvider(ClaudeProvider):
    """ClaudeProvider with the two transcript predicates stubbed out.

    Lets the wiring tests drive the three transcript-guarded fallback
    paths in ``get_state`` without standing up real ``.jsonl`` files
    under a slug directory — that's already exercised by
    :class:`TestTranscriptClassification`.
    """

    def __init__(self, running: bool = False, interrupted: bool = False) -> None:
        super().__init__()
        self._running = running
        self._interrupted = interrupted

    def transcript_says_running(self, **_kwargs: object) -> bool:
        return self._running

    def transcript_says_interrupted(self, **_kwargs: object) -> bool:
        return self._interrupted


class TestTranscriptInterruptWiring:
    """Verify the three transcript-guarded paths in ``get_state`` flip
    RUNNING → INTERRUPTED (not IDLE) when the transcript records a
    user-interrupt marker.

    Before the fix, all three paths fell through to "→ idle" once the
    transcript_says_running guard cleared, and the auto-sender would
    immediately dispatch the next queued message into Claude's
    "What should Claude do instead?" prompt — silently swallowing the
    user's interrupt intent.
    """

    def test_signal_idle_with_transcript_interrupt_goes_to_interrupted(
        self, tmp_path: Path,
    ) -> None:
        """Stop hook fires (signal=idle) and the transcript records the
        user-interrupt marker → INTERRUPTED, even though pyte never
        saw "Interrupted" on screen (this is the real-world Ink TUI
        redraw scenario)."""
        t = [0.0]
        provider = _StubTranscriptProvider(running=False, interrupted=True)
        tracker = make_tracker(tmp_path, t, provider=provider)
        tracker.on_send()
        assert tracker.current_state == 'running'

        # Stop hook signal arrives.  No "Interrupted" pattern was
        # written to pyte (provider.interrupted_pattern is 'Interrupted'
        # but the screen is blank), and _interrupt_pending is False
        # (e.g. cleared by a brief pty_alive=False blip).
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_signal_idle_with_pending_and_transcript_marker(
        self, tmp_path: Path,
    ) -> None:
        """Both the pending flag and the transcript agree → INTERRUPTED.
        Same outcome as either source alone — just confirms the OR
        logic doesn't accidentally fall through."""
        t = [0.0]
        provider = _StubTranscriptProvider(running=False, interrupted=True)
        tracker = make_tracker(tmp_path, t, provider=provider)
        tracker.on_send()
        tracker.on_input(b'\x1b')
        assert tracker._interrupt_pending is True

        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'interrupted'
        # Flag is cleared by the INTERRUPTED transition.
        assert tracker._interrupt_pending is False

    def test_cursor_silence_with_transcript_interrupt_goes_to_interrupted(
        self, tmp_path: Path,
    ) -> None:
        """5 s of silence + cursor visible + transcript interrupt marker
        → INTERRUPTED.  This is the path that wedged in the live bug
        (Stop hook hadn't fired yet, just steady output silence).

        Note: the cursor+silence path is gated by ``silence_baseline > 0``
        (``max(_last_output_time, _running_since)``), so we must start
        the clock at a non-zero value.
        """
        t = [1.0]
        provider = _StubTranscriptProvider(running=False, interrupted=True)
        tracker = make_tracker(tmp_path, t, provider=provider)
        tracker.on_send()
        # Cursor visible, screen otherwise quiet.
        feed_with_visible_cursor(tracker, '> ')
        # Advance past the 5 s silence threshold.
        t[0] = 7.0
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_cursor_silence_transcript_running_keeps_running(
        self, tmp_path: Path,
    ) -> None:
        """Regression: when the transcript still shows tool_use (no
        interrupt), cursor+silence must keep RUNNING — the new
        interrupt branch must not steal cases the existing tool_use
        guard already handles."""
        t = [1.0]
        provider = _StubTranscriptProvider(running=True, interrupted=False)
        tracker = make_tracker(tmp_path, t, provider=provider)
        tracker.on_send()
        feed_with_visible_cursor(tracker, '> ')
        t[0] = 7.0
        assert tracker.get_state(pty_alive=True) == 'running'

    def test_safety_timeout_with_transcript_interrupt_goes_to_interrupted(
        self, tmp_path: Path,
    ) -> None:
        """At the safety_timeout boundary (60 s) the interrupt marker
        also wins over the unconditional IDLE fallback.  Covers the
        case where 5 s cursor+silence somehow didn't fire (e.g. cursor
        was hidden the whole time)."""
        t = [1.0]
        provider = _StubTranscriptProvider(running=False, interrupted=True)
        tracker = make_tracker(tmp_path, t, provider=provider)
        tracker.on_send()
        # Cursor hidden → cursor+silence path is skipped, only the
        # safety fallback can fire.
        feed_with_hidden_cursor(tracker, 'thinking...')
        t[0] = SAFETY_SILENCE_TIMEOUT + 2.0
        assert tracker.get_state(pty_alive=True) == 'interrupted'

    def test_no_interrupt_signals_still_go_idle(
        self, tmp_path: Path,
    ) -> None:
        """Final regression: no pending flag, no transcript interrupt,
        no on-screen pattern — the existing IDLE transitions still
        work unchanged."""
        t = [0.0]
        provider = _StubTranscriptProvider(running=False, interrupted=False)
        tracker = make_tracker(tmp_path, t, provider=provider)
        tracker.on_send()
        write_signal(tracker, 'idle')
        assert tracker.get_state(pty_alive=True) == 'idle'


# ---------------------------------------------------------------------------
# Concurrent-transition safety (compare-and-set commits)
# ---------------------------------------------------------------------------

class TestConcurrentTransitionNotClobbered:
    """``get_state`` helpers snapshot ``current`` once, then decide over
    several lock-free reads - meanwhile the PTY thread's ``on_output`` can
    move the state (e.g. RUNNING→INTERRUPTED on a confirmed interrupt).
    Pre-fix, the heuristic then committed its stale decision on top,
    silently erasing the fresher transition - a heuristic IDLE clobbering
    INTERRUPTED let the auto-sender dispatch into the "What should Claude
    do instead?" prompt.  Commits now compare-and-set and abort."""

    def test_heuristic_idle_does_not_clobber_concurrent_interrupt(
        self, tmp_path: Path,
    ) -> None:
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker,
            'response line one\r\n'
            'response line two\r\n'
            'response line three\r\n'
            '> ',
        )
        # Past the 5s cursor+silence window - the heuristic will decide
        # RUNNING→IDLE.  Simulate the PTY thread committing a confirmed
        # interrupt in the middle of that decision (between the snapshot
        # of ``current`` and the commit) by flipping the state from
        # inside one of the lock-free decision reads.
        t[0] = 8.0

        def flip_to_interrupted() -> bool:
            with tracker._lock:
                tracker._state = CLIState.INTERRUPTED
                tracker._waiting_since = t[0]
            return False

        tracker._transcript_says_running = flip_to_interrupted
        tracker.get_state(pty_alive=True)
        # The concurrent INTERRUPTED must survive - pre-fix the stale
        # IDLE commit overwrote it.
        assert tracker.current_state == CLIState.INTERRUPTED

    def test_clean_heuristic_idle_still_commits(self, tmp_path: Path) -> None:
        # No concurrent movement: the cursor+silence idle works as before.
        t = [0.0]
        tracker = make_tracker(tmp_path, t)
        tracker.on_send()
        t[0] = 1.0
        feed_with_visible_cursor(
            tracker,
            'response line one\r\n'
            'response line two\r\n'
            'response line three\r\n'
            '> ',
        )
        t[0] = 8.0
        assert tracker.get_state(pty_alive=True) == 'idle'
