"""Tests for the GitHub Copilot CLI provider.

Copilot is Leap's first *hookless* provider (no Stop/Notification
lifecycle hooks), so its state machine leans entirely on PTY output:
an on-screen "running" footer indicator, a dialog-footer pattern, and
the interrupt banner.  The footer strings below were captured from a
live ``copilot`` v1.0.60 PTY session, rendered through pyte exactly as
the state tracker sees them.
"""

import json
from pathlib import Path
from typing import Callable, List

from leap.cli_providers.copilot import CopilotProvider
from leap.cli_providers.registry import get_provider, list_providers
from leap.cli_providers.states import CLIState
from leap.server.state_tracker import CLIStateTracker


# -- Verified on-screen footers (copilot v1.0.60) ------------------------
RUNNING_FOOTER = "◎ Working    esc cancel"
DIALOG_FOOTER = "↑/↓ to navigate · enter to select · esc to cancel"
QUESTION_FOOTER = "↑/↓ to select · enter to confirm · esc to cancel"
QUESTION_FREETEXT_FOOTER = "❯ type your answer...                    enter to submit · esc to cancel"
IDLE_FOOTER = "❯                                   / commands · ? help"
INTERRUPT_BANNER = "● Operation cancelled by user"


# -- Helpers -------------------------------------------------------------

def _make_tracker(
    tmp_path: Path, t: List[float], provider: object,
) -> CLIStateTracker:
    """Tracker with a fake clock and a tmp-dir cwd (hermetic)."""
    return CLIStateTracker(
        signal_file=tmp_path / "test.signal",
        clock=lambda: t[0],
        cwd=str(tmp_path),
        tag='test',
        provider=provider,
    )


def _feed_visible(tracker: CLIStateTracker, text: str) -> None:
    """Feed PTY output with the cursor visible (Copilot's idle/running
    rendering - cursor stays visible, unlike Codex's Ratatui)."""
    tracker.on_output(f'\x1b[?25h\x1b[H\x1b[2J{text}'.encode('utf-8'))


def _feed_hidden(tracker: CLIStateTracker, text: str) -> None:
    """Feed PTY output with the cursor HIDDEN - Copilot hides the cursor
    while a menu dialog (trust / permission) is on screen."""
    tracker.on_output(f'\x1b[?25l\x1b[H\x1b[2J{text}'.encode('utf-8'))


def _compact(s: str) -> str:
    return s.replace(' ', '').replace('\n', '')


# -- Identity / hookless contract ----------------------------------------

class TestCopilotIdentity:
    def test_registered_and_identity(self) -> None:
        p = get_provider('copilot')
        assert isinstance(p, CopilotProvider)
        assert p.name == 'copilot'
        assert p.command == 'copilot'
        assert p.display_name == 'GitHub Copilot'
        assert p.base_type == 'copilot'
        assert 'copilot' in list_providers()

    def test_hookless_contract(self) -> None:
        p = CopilotProvider()
        # No hook system: hooks_installed() must stay True so the
        # session-start gate never blocks Copilot, and configure_hooks
        # must be a harmless no-op that doesn't raise.
        assert p.hooks_installed() is True
        p.configure_hooks('/whatever/leap-hook.sh')
        assert p.hooks_installed() is True
        # Resume recording is hook-driven, so it's off for Copilot.
        assert p.supports_resume is False
        # Cursor is visible while idle (verified), so cursor-hidden is
        # NOT a busy signal and the cursor+silence idle fallback applies.
        assert p.cursor_hidden_while_idle is False

    def test_expected_patterns(self) -> None:
        p = CopilotProvider()
        assert p.running_indicator_patterns == [b'esccancel']
        assert p.dialog_patterns == [b'entertoselect', b'esctocancel']
        assert p.input_dialog_patterns == [b'entertoconfirm', b'entertosubmit']
        assert p.idle_indicator_patterns == [b'/commands']
        assert p.interrupted_pattern == b'Operationcancelledbyuser'
        # confirmed pattern disabled (the banner lingers in scrollback).
        assert p.confirmed_interrupt_pattern is None
        # Copilot cancels on Ctrl+C, not Escape (verified live: Escape is
        # ignored mid-turn).  The monitor's Interrupt sends this key.
        assert p.interrupt_key == b'\x03'


# -- Footer disambiguation (the load-bearing design property) ------------

class TestCopilotFooterPatterns:
    """The running / dialog / idle footers must never be confused -
    that's what keeps a hookless session correctly RUNNING vs waiting
    vs idle."""

    def _matches_running(self, p: CopilotProvider, compact: str) -> bool:
        return any(
            pat.decode() in compact for pat in p.running_indicator_patterns
        )

    def test_dialog_footer_is_certain_dialog(self) -> None:
        assert CopilotProvider().is_dialog_certain(_compact(DIALOG_FOOTER))

    def test_running_footer_is_not_a_dialog(self) -> None:
        # "esc cancel" lacks "enter to select", so it must NOT read as a
        # permission dialog.
        assert not CopilotProvider().is_dialog_certain(_compact(RUNNING_FOOTER))

    def test_idle_footer_is_not_a_dialog(self) -> None:
        assert not CopilotProvider().is_dialog_certain(_compact(IDLE_FOOTER))

    def test_running_indicator_only_matches_running_footer(self) -> None:
        p = CopilotProvider()
        # "esccancel" (running) is NOT a substring of "esctocancel"
        # (dialog) - the "to" in between is what keeps them disjoint.
        assert self._matches_running(p, _compact(RUNNING_FOOTER)) is True
        assert self._matches_running(p, _compact(DIALOG_FOOTER)) is False
        assert self._matches_running(p, _compact(IDLE_FOOTER)) is False


# -- Input history (newest-first on disk → oldest-first for Leap) --------

class TestCopilotInputHistory:
    def test_history_reversed_to_oldest_first(
        self, tmp_path: Path, monkeypatch: object,
    ) -> None:
        cfg = tmp_path / ".copilot"
        cfg.mkdir()
        (cfg / "command-history-state.json").write_text(json.dumps({
            "commandHistory": ["newest", "middle", "oldest"],
        }))
        monkeypatch.setattr(
            'leap.cli_providers.copilot.COPILOT_CONFIG_DIR', cfg,
        )
        # Leap wants oldest → newest (last element = what ↑ selects first).
        assert CopilotProvider().input_history(str(tmp_path)) == [
            "oldest", "middle", "newest",
        ]

    def test_history_missing_file_returns_none(
        self, tmp_path: Path, monkeypatch: object,
    ) -> None:
        monkeypatch.setattr(
            'leap.cli_providers.copilot.COPILOT_CONFIG_DIR', tmp_path / "nope",
        )
        assert CopilotProvider().input_history(str(tmp_path)) is None

    def test_history_malformed_returns_none(
        self, tmp_path: Path, monkeypatch: object,
    ) -> None:
        cfg = tmp_path / ".copilot"
        cfg.mkdir()
        (cfg / "command-history-state.json").write_text("not json {")
        monkeypatch.setattr(
            'leap.cli_providers.copilot.COPILOT_CONFIG_DIR', cfg,
        )
        assert CopilotProvider().input_history(str(tmp_path)) is None


# -- Hookless state machine ----------------------------------------------

# Clock base must be nonzero: the tracker only accrues "silence" once
# its baseline (max of _last_output_time, _running_since) is > 0, which
# in production is always true (clock is time.time()).  Starting at 0
# would skip the silence fallbacks entirely and pass for the wrong
# reason.
_BASE = 1000.0


class TestCopilotStateDetection:
    def test_running_indicator_holds_running_through_silence(
        self, tmp_path: Path,
    ) -> None:
        """With no Stop hook, the "esc cancel" footer is what keeps the
        session RUNNING - even past the 5s cursor+silence and 15s safety
        timeouts (covers long silent tool calls)."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        assert tr.current_state == CLIState.RUNNING
        _feed_visible(tr, RUNNING_FOOTER)
        t[0] = _BASE + 100.0  # way past every silence threshold
        assert tr.get_state(pty_alive=True) == CLIState.RUNNING

    def test_idle_footer_lets_session_idle(self, tmp_path: Path) -> None:
        """When the turn ends the footer reverts to the idle prompt
        (no "esc cancel"); the cursor+silence fallback then idles."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        _feed_visible(tr, RUNNING_FOOTER)
        t[0] = _BASE + 10.0
        assert tr.get_state(pty_alive=True) == CLIState.RUNNING
        _feed_visible(tr, IDLE_FOOTER)  # turn ended; output time advances
        t[0] = _BASE + 20.0             # >5s of silence since then
        assert tr.get_state(pty_alive=True) == CLIState.IDLE

    def test_dialog_footer_promotes_to_needs_permission(
        self, tmp_path: Path,
    ) -> None:
        """A permission dialog stops the spinner (no running indicator);
        after the silence settles, the dialog footer promotes RUNNING →
        NEEDS_PERMISSION."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        _feed_visible(tr, DIALOG_FOOTER)
        t[0] = _BASE + 10.0  # >5s silence, cursor visible
        assert tr.get_state(pty_alive=True) == CLIState.NEEDS_PERMISSION

    def test_question_footer_promotes_to_needs_input(
        self, tmp_path: Path,
    ) -> None:
        """Copilot's ask_user QUESTION dialog ("enter to confirm" footer)
        must read as NEEDS_INPUT - not Running (the reported bug), and not
        NEEDS_PERMISSION (which ALWAYS-mode auto-approve would auto-answer
        for the user)."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        _feed_visible(tr, QUESTION_FOOTER)
        t[0] = _BASE + 2.5   # past the post-send footer grace
        assert tr.get_state(pty_alive=True) == CLIState.NEEDS_INPUT

    def test_freetext_question_footer_promotes_to_needs_input(
        self, tmp_path: Path,
    ) -> None:
        """Copilot's *free-text* ask_user question ("enter to submit"
        footer, with a visible text-input cursor) must read as
        NEEDS_INPUT - not the false IDLE the cursor+silence path would
        otherwise conclude from the visible cursor."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        _feed_visible(tr, QUESTION_FREETEXT_FOOTER)   # visible cursor (text field)
        t[0] = _BASE + 2.5   # past the post-send footer grace
        assert tr.get_state(pty_alive=True) == CLIState.NEEDS_INPUT

    def test_on_send_keeps_screen_so_followup_dialog_stays_visible(
        self, tmp_path: Path,
    ) -> None:
        """Regression (confirmed live): answering a dialog calls on_send,
        and for footer-driven providers on_send must NOT blank the screen.
        Copilot repaints the next dialog only incrementally, so a blanked
        screen never refills and a follow-up question (e.g. a free-text
        'which file?' right after a menu question) is invisible to the
        footer-detector - the session sticks reading RUNNING/IDLE.  Here a
        question footer is on screen; a second on_send (the answer) fires;
        WITHOUT re-feeding the footer it must still be detectable, proving
        on_send left the screen intact."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        _feed_visible(tr, QUESTION_FREETEXT_FOOTER)
        t[0] = _BASE + 2.5
        assert tr.get_state(pty_alive=True) == CLIState.NEEDS_INPUT   # Q1 seen
        tr.on_send()                       # answering -> RUNNING, screen KEPT
        t[0] = _BASE + 5.0                 # past the post-send grace again
        # No re-feed: the retained footer must still drive needs_input.
        # (Old on_send blanked the screen here -> stuck RUNNING.)
        assert tr.get_state(pty_alive=True) == CLIState.NEEDS_INPUT

    def test_dialog_promotion_held_during_post_send_grace(
        self, tmp_path: Path,
    ) -> None:
        """The post-send grace must HOLD dialog promotion: right after a
        send the screen is kept (footer-driven providers), so a
        just-answered dialog's footer still lingers.  Promoting on it would
        re-detect the answered dialog - a needs_permission would then get
        auto-approved a second time.  So within the grace it stays RUNNING;
        only after the grace (CLI has repainted the real next state) does it
        promote.  Guards the phantom-auto-approve prevention."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        _feed_visible(tr, QUESTION_FOOTER)
        t[0] = _BASE + 1.0                 # within the 2s grace
        assert tr.get_state(pty_alive=True) == CLIState.RUNNING
        t[0] = _BASE + 2.5                 # past the grace
        assert tr.get_state(pty_alive=True) == CLIState.NEEDS_INPUT

    def test_permission_codisplayed_with_working_footer_promotes(
        self, tmp_path: Path,
    ) -> None:
        """Regression (caught in a live auto-approve run): Copilot renders a
        per-step permission prompt ("Do you want to edit a.txt? ... enter to
        select") with a "● Creating files  esc cancel" WORKING line
        co-displayed below the box.  The working "esc cancel" must NOT win -
        the dialog footer does - or the prompt is never detected and an
        Always-mode auto-approve sequence sticks RUNNING after the first
        file.  This is the exact screen captured from the stuck session."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        _feed_visible(
            tr,
            "Do you want to edit a.txt?\n"
            "❯ 1. Yes\n"
            "  3. No, and tell Copilot what to do differently (Esc to stop)\n"
            + DIALOG_FOOTER + "\n"
            "● Creating files    esc cancel",
        )
        # The dialog footer must win over the co-displayed running footer.
        with tr._screen_lock:
            assert tr._screen_has_running_indicator() is False
        t[0] = _BASE + 2.5                 # past the post-send grace
        assert tr.get_state(pty_alive=True) == CLIState.NEEDS_PERMISSION

    def test_permission_dialog_detected_even_with_cursor_hidden(
        self, tmp_path: Path,
    ) -> None:
        """Regression guard for a bug caught in live testing: Copilot
        HIDES the cursor while a permission menu is up, and the dialog
        promotion used to live inside an `if cursor_visible:` guard - so
        the session got trapped in RUNNING for the entire dialog.  A
        certain dialog footer must promote regardless of cursor state."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        _feed_hidden(tr, DIALOG_FOOTER)   # cursor hidden, as real copilot does
        t[0] = _BASE + 10.0               # >5s of silence
        assert tr.get_state(pty_alive=True) == CLIState.NEEDS_PERMISSION

    def test_typing_in_dialog_does_not_flip_to_running(
        self, tmp_path: Path,
    ) -> None:
        """Copilot keeps the cursor hidden *during* its permission menu, so
        a printable keystroke in the dialog (which sets _user_responded)
        must NOT be read as 'moved past the dialog' by the cursor-hidden
        waiting->running heuristic - the prompt is still pending until
        Enter.  (Regression guard: without the dialogs_hide_cursor gate the
        session flips to RUNNING the instant the user types.)"""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        _feed_hidden(tr, DIALOG_FOOTER)
        t[0] = _BASE + 10.0
        assert tr.get_state(pty_alive=True) == CLIState.NEEDS_PERMISSION
        tr.on_input(b'x')                 # printable char -> _user_responded
        t[0] = _BASE + 11.0
        # Dialog still up + cursor still hidden: stay NEEDS_PERMISSION.
        assert tr.get_state(pty_alive=True) == CLIState.NEEDS_PERMISSION

    def test_idles_via_footer_despite_continuous_output(
        self, tmp_path: Path,
    ) -> None:
        """The shipped bug: Copilot emits PTY output continuously even while
        idle, so _last_output_time never goes stale and every silence-based
        fallback is defeated (the session sticks in RUNNING forever).
        Footer-driven detection must still idle the session once the idle
        footer is on screen, with no period of silence at all."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        final = None
        # Re-feed the idle footer every 0.5s (output never stops) and poll;
        # a purely silence-based detector would stay RUNNING here forever.
        for i in range(1, 12):
            t[0] = _BASE + i * 0.5
            _feed_visible(tr, IDLE_FOOTER)
            final = tr.get_state(pty_alive=True)
        assert final == CLIState.IDLE

    def test_idle_stays_idle_despite_cursor_toggle(
        self, tmp_path: Path,
    ) -> None:
        """Copilot's idle animation toggles the cursor; the cursor-hidden
        auto-resume heuristic must be disabled for footer-idle providers,
        or the session spontaneously flips IDLE->RUNNING (the oscillation
        the user observed)."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        t[0] = _BASE + 3.0
        _feed_visible(tr, IDLE_FOOTER)
        assert tr.get_state(pty_alive=True) == CLIState.IDLE
        t[0] = _BASE + 4.0
        _feed_hidden(tr, IDLE_FOOTER)     # idle repaint with cursor hidden
        assert tr.get_state(pty_alive=True) == CLIState.IDLE   # NOT running

    def test_idle_to_running_via_running_footer(
        self, tmp_path: Path,
    ) -> None:
        """idle->running for Copilot is footer-driven: the "esc cancel"
        running footer reappearing resumes RUNNING (cursor auto-resume is
        off for footer-idle providers)."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        t[0] = _BASE + 3.0
        _feed_visible(tr, IDLE_FOOTER)
        assert tr.get_state(pty_alive=True) == CLIState.IDLE
        t[0] = _BASE + 4.0
        _feed_visible(tr, RUNNING_FOOTER)   # copilot starts working again
        assert tr.current_state == CLIState.RUNNING

    def test_lingering_esc_cancel_status_idles_and_stays_idle(
        self, tmp_path: Path,
    ) -> None:
        """After a question is answered, Copilot leaves a stale
        '● Asking question  esc cancel' status line on screen *next to* the
        idle footer.  The idle footer must win: the session idles (not
        stuck on RUNNING - the 'answering a question sticks in Running'
        bug), and a re-render of that lingering status must not flip it
        back to RUNNING."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        # Idle prompt is back, but a stale "esc cancel" status lingers
        # below it (matches running_indicator_patterns).
        stale = IDLE_FOOTER + "\r\n● Asking question   esc cancel        GPT-5 mini"
        _feed_visible(tr, stale)
        t[0] = _BASE + 5.0
        assert tr.get_state(pty_alive=True) == CLIState.IDLE   # not stuck RUNNING
        _feed_visible(tr, stale)                                # Copilot re-renders
        assert tr.get_state(pty_alive=True) == CLIState.IDLE    # no oscillation back

    def test_monitor_interrupt_key_drives_interrupted(
        self, tmp_path: Path,
    ) -> None:
        """End-to-end of the monitor's Interrupt for Copilot: the server
        sends provider.interrupt_key (Ctrl+C for Copilot) through
        on_input (arming _interrupt_pending) and the PTY; the resulting
        "Operation cancelled by user" banner then drives
        RUNNING → INTERRUPTED.  (Escape, the old hardcoded key, does
        nothing mid-turn in Copilot - verified live.)"""
        p = CopilotProvider()
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, p)
        tr.on_send()
        tr.on_input(p.interrupt_key)          # what server.py feeds on interrupt
        _feed_visible(tr, INTERRUPT_BANNER)   # Copilot's response to Ctrl+C
        assert tr.current_state == CLIState.INTERRUPTED

    def test_interrupted_idles_via_footer_after_cancel(
        self, tmp_path: Path,
    ) -> None:
        """After Ctrl+C, Copilot returns to its (continuously-animated)
        idle prompt.  INTERRUPTED must recover to IDLE via the footer too
        (same continuous-output problem as RUNNING) so queued messages can
        flow - otherwise the session would stick on 'Interrupted'."""
        p = CopilotProvider()
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, p)
        tr.on_send()
        tr.on_input(p.interrupt_key)
        _feed_visible(tr, INTERRUPT_BANNER)
        assert tr.current_state == CLIState.INTERRUPTED
        # Copilot is back at the idle prompt; footer-detector idles it
        # (continuous output -> re-feed to show silence is irrelevant).
        for i in range(1, 5):
            t[0] = _BASE + i * 0.5
            _feed_visible(tr, IDLE_FOOTER)
            state = tr.get_state(pty_alive=True)
        assert state == CLIState.IDLE

    def test_interrupt_keeps_screen_so_idle_footer_survives(
        self, tmp_path: Path,
    ) -> None:
        """Regression: the interrupt path must NOT _reset_screen() for
        footer-driven providers.  In a real terminal the cancel banner and
        the returned idle footer arrive together, and Copilot then repaints
        only incrementally - so a reset would wipe the just-arrived idle
        footer for good and the footer-detector could never leave
        INTERRUPTED (the session stuck in INTERRUPTED, confirmed live)."""
        t = [_BASE]
        tr = _make_tracker(tmp_path, t, CopilotProvider())
        tr.on_send()
        _feed_visible(tr, RUNNING_FOOTER)
        tr.on_input(b'\x03')                  # Ctrl+C -> _interrupt_pending
        # Cancel banner + idle prompt in ONE render; the old code reset the
        # screen AFTER this, wiping the idle footer that just arrived.
        _feed_visible(tr, INTERRUPT_BANNER + "\r\n" + IDLE_FOOTER)
        assert tr.current_state == CLIState.INTERRUPTED
        t[0] = _BASE + 1.0
        assert tr.get_state(pty_alive=True) == CLIState.IDLE   # leaves INTERRUPTED
