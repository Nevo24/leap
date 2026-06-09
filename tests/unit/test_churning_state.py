"""Tests for the CHURNING state.

CHURNING surfaces a session whose turn has ended (idle prompt shown, ready for
input) while a background task - Claude Code's ``Monitor`` - is still active and
will re-invoke it.  It is a *presentation* refinement of IDLE computed at the
``get_state`` boundary: the internal ``_state`` stays IDLE, the detector is
Claude-only, and CHURNING is held out of WAITING/PROMPT/SIGNAL so the existing
machinery is untouched.
"""
from pathlib import Path
from typing import List

from leap.cli_providers.claude import ClaudeProvider
from leap.cli_providers.codex import CodexProvider
from leap.cli_providers.states import (
    ChurnQueueMode,
    CLIState,
    PROMPT_STATES,
    WAITING_STATES,
)
from leap.server.state_tracker import CLIStateTracker


def _tracker(tmp_path: Path, t: List[float]) -> CLIStateTracker:
    return CLIStateTracker(
        signal_file=tmp_path / 'test.signal',
        auto_send_mode='pause',
        clock=lambda: t[0],
        cwd=str(tmp_path),
        tag='test',
    )


class TestBackgroundWorkDetector:
    """``background_work_state`` is tri-state: True (marker present), False
    (clean idle prompt, no marker -> Monitor done), None (ambiguous screen ->
    leave the tracker's sticky flag unchanged)."""

    def test_claude_detects_monitor_markers(self) -> None:
        # Real lines captured from a live churn (Claude Code v2.1.162).  The
        # activity word is randomized, so detection keys off the "<n> monitor"
        # count (present in both the activity line and the persistent mode
        # line), never the word.
        p = ClaudeProvider()
        for word in ('Cogitated', 'Cooked', 'Brewed', 'Baked', 'Churned', 'Crunched'):
            assert p.background_work_state(
                [f'✻ {word} for 3s · 1 monitor still running']) is True, word
        # The persistent mode line (shown even when the spinner is gone).
        assert p.background_work_state(
            ['⏵⏵ bypass permissions on · 1 monitor · ← for agents']) is True
        assert p.background_work_state(
            ['⏵⏵ bypass permissions on · 2 monitors · ← for agents']) is True

    def test_count_found_even_when_trailing_hint_displaces_mode_line(self) -> None:
        # Live failure: a "ctrl+v to paste" hint rendered below the mode line
        # made the mode line no longer the last non-blank row, and in a tall
        # terminal the "N monitor still running" activity line is above the
        # tail window.  The count must still be found on the mode-line ROW
        # (identified by its markers), not on a fixed last row.
        p = ClaudeProvider()
        assert p.background_work_state([
            '● Still running - tick 1. I will wait for DONE.',
            '❯ ',
            '⏵⏵ bypass permissions on · 1 monitor · ← for agents',
            'Image in clipboard · ctrl+v to paste',
        ]) is True

    def test_clean_idle_footer_clears(self) -> None:
        # The normal idle footer (no monitor, but the mode line IS rendered)
        # is the positive "Monitor finished" signal -> False, clearing the flag.
        p = ClaudeProvider()
        assert p.background_work_state(
            ['❯ ', 'bypass permissions on (shift+tab to cycle) · ← for agents']) is False

    def test_ambiguous_screen_is_none(self) -> None:
        # Blank/partial screens carry no mode-line marker -> None, so the
        # sticky flag is left unchanged (the fix for queue-dispatch-during-churn).
        p = ClaudeProvider()
        assert p.background_work_state([]) is None
        assert p.background_work_state(['just a normal response line']) is None
        assert p.background_work_state(['', '', '  ']) is None

    def test_response_text_mentioning_monitors_is_not_a_marker(self) -> None:
        # The bare "N monitor(s)" count is only honored on the last non-blank
        # row (the mode line).  A response body mentioning monitors, with the
        # normal idle footer below it, must NOT flag churning - here the footer
        # IS rendered with no marker, so it correctly reads False (Monitor done).
        p = ClaudeProvider()
        assert p.background_work_state([
            'I checked the dashboard across 3 monitors and all looked fine.',
            '',
            '❯ ',
            'bypass permissions on (shift+tab to cycle) · ← for agents',
        ]) is False

    def test_marker_found_with_trailing_blank_padding(self) -> None:
        # Live failure: a tall terminal with a short conversation renders the
        # footer mid-screen and pads many blank rows below it.  The tail must
        # anchor to the last non-blank row, not grab the trailing blanks.
        p = ClaudeProvider()
        lines = [
            '● Still running - tick 1. I will wait for DONE.',
            '❯ ',
            '⏵⏵ bypass permissions on · 1 monitor · ← for agents',
        ] + [''] * 40  # 40 blank rows padded below (72-row terminal)
        assert p.background_work_state(lines) is True

    def test_other_providers_never_report_churn(self) -> None:
        # None (not False) -> base providers never touch the sticky flag.
        assert CodexProvider().background_work_state(['1 monitor still running']) is None


class TestChurningRefine:
    def test_idle_with_background_work_becomes_churning(self, tmp_path: Path) -> None:
        tr = _tracker(tmp_path, [1000.0])
        assert tr.get_state(pty_alive=True) == CLIState.IDLE  # baseline
        tr._background_active = True
        assert tr.get_state(pty_alive=True) == CLIState.CHURNING
        # Compute-only: the internal state remains IDLE, never CHURNING.
        assert tr.current_state == CLIState.IDLE

    def test_idle_without_background_work_stays_idle(self, tmp_path: Path) -> None:
        tr = _tracker(tmp_path, [1000.0])
        tr._background_active = False
        assert tr.get_state(pty_alive=True) == CLIState.IDLE

    def test_dead_pty_is_idle_not_churning(self, tmp_path: Path) -> None:
        tr = _tracker(tmp_path, [1000.0])
        tr._background_active = True
        # A dead CLI is genuinely gone - never churning.
        assert tr.get_state(pty_alive=False) == CLIState.IDLE

    def test_running_is_never_refined_to_churning(self, tmp_path: Path) -> None:
        tr = _tracker(tmp_path, [1000.0])
        tr.on_input(b'hi')
        tr.on_input(b'\r')  # Enter from idle -> running
        tr._background_active = True
        assert tr.get_state(pty_alive=True) == CLIState.RUNNING


class TestStickyBackgroundFlag:
    """The flag must NOT drop to False on an ambiguous render mid-churn - that
    was the bug that dispatched a queued message into a churning session."""

    @staticmethod
    def _feed_at_bottom(tr: CLIStateTracker, line: str) -> None:
        # The CLI's footer is always the bottom rows of the screen; the detector
        # reads the last-N-row tail.  Scroll the line to the bottom (no trailing
        # newline so the cursor stays on it) instead of leaving it at row 0.
        tr.on_output(('\n' * (tr._screen.lines + 5) + line).encode())

    def test_marker_sets_flag(self, tmp_path: Path) -> None:
        tr = _tracker(tmp_path, [1000.0])
        self._feed_at_bottom(tr, '✻ Cooked for 3s · 1 monitor still running')
        assert tr._background_active is True

    def test_ambiguous_render_leaves_flag_set(self, tmp_path: Path) -> None:
        tr = _tracker(tmp_path, [1000.0])
        tr._background_active = True  # mid-churn
        # A blank/partial repaint (e.g. just after get_state reset the screen).
        tr.on_output(b'\x1b[2J')  # clear screen -> no mode-line marker
        assert tr._background_active is True, 'must stay sticky on a blank screen'

    def test_clean_idle_clears_flag(self, tmp_path: Path) -> None:
        tr = _tracker(tmp_path, [1000.0])
        tr._background_active = True
        self._feed_at_bottom(
            tr, 'bypass permissions on (shift+tab to cycle) · ← for agents')
        assert tr._background_active is False, 'clean idle footer clears it'


class TestChurningReadinessAndSets:
    def test_churning_not_ready_by_default(self, tmp_path: Path) -> None:
        # Phase-1 default: CHURNING != IDLE so the auto-sender holds the queue.
        tr = _tracker(tmp_path, [1000.0])
        assert tr.is_ready_for_state(CLIState.IDLE) is True
        assert tr.is_ready_for_state(CLIState.CHURNING) is False

    def test_churning_excluded_from_state_sets(self) -> None:
        assert CLIState.CHURNING not in WAITING_STATES
        assert CLIState.CHURNING not in PROMPT_STATES


class TestChurnQueueMode:
    def _tracker_with(self, tmp_path: Path, mode: str) -> CLIStateTracker:
        return CLIStateTracker(
            signal_file=tmp_path / 'test.signal',
            auto_send_mode='pause',
            churn_queue_mode=mode,
            clock=lambda: 1000.0,
            cwd=str(tmp_path),
            tag='test',
        )

    def test_default_is_wait(self, tmp_path: Path) -> None:
        tr = _tracker(tmp_path, [1000.0])
        assert tr.churn_queue_mode == ChurnQueueMode.WAIT

    def test_send_mode_makes_churning_ready(self, tmp_path: Path) -> None:
        tr = self._tracker_with(tmp_path, ChurnQueueMode.SEND)
        assert tr.is_ready_for_state(CLIState.CHURNING) is True
        assert tr.is_ready_for_state(CLIState.IDLE) is True  # idle unaffected

    def test_wait_mode_holds_churning(self, tmp_path: Path) -> None:
        tr = self._tracker_with(tmp_path, ChurnQueueMode.WAIT)
        assert tr.is_ready_for_state(CLIState.CHURNING) is False

    def test_setter_flips_readiness(self, tmp_path: Path) -> None:
        tr = _tracker(tmp_path, [1000.0])  # default WAIT
        assert tr.is_ready_for_state(CLIState.CHURNING) is False
        tr.churn_queue_mode = ChurnQueueMode.SEND
        assert tr.is_ready_for_state(CLIState.CHURNING) is True


class TestSlackTreatsChurningAsIdle:
    """A turn that ends while a Monitor is active (RUNNING -> CHURNING) must
    still post its response to Slack - regression guard for the allowlist that
    only knew IDLE/PERMISSION/INPUT/INTERRUPTED."""

    def _capture(self, tmp_path: Path) -> object:
        from leap.slack.output_capture import OutputCapture
        oc = OutputCapture.__new__(OutputCapture)
        oc._tag = 't'
        oc._enabled = True
        oc._response_file = tmp_path / 't.last_response'
        oc._signal_file = tmp_path / 't.signal'
        oc._sessions_file = tmp_path / 'sessions.json'
        oc._read_signal_data = lambda: {'output': 'the answer', 'notification_message': ''}
        return oc

    def test_churn_turn_end_posts_response_as_idle(self, tmp_path: Path) -> None:
        import json
        oc = self._capture(tmp_path)
        oc.on_state_change(CLIState.CHURNING, CLIState.RUNNING, queue_has_next=False)
        assert oc._response_file.exists(), 'churn turn-end must post to Slack'
        payload = json.loads(oc._response_file.read_text())
        assert payload['state'] == CLIState.IDLE  # churning mapped to idle
        assert payload['output'] == 'the answer'

    def test_running_is_not_posted(self, tmp_path: Path) -> None:
        # The CHURNING->IDLE map must be specific: RUNNING is still ignored.
        oc = self._capture(tmp_path)
        oc.on_state_change(CLIState.RUNNING, CLIState.IDLE, queue_has_next=False)
        assert not oc._response_file.exists()
