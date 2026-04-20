"""Interrupted-state wedge scenarios.

The interrupted state is the most fragile: it's entered via a PTY
pattern match (``Interrupted``) and can only exit via a Stop signal,
a cursor-hidden auto-resume (which requires ``_user_responded``), or
a safety timeout.  These tests document every way in and out and flag
the scenarios that can leave the session visibly wedged.

Wedge scenarios pin current behaviour so we notice if it changes —
whether the fix is in the state machine or in a follow-up PR.  They
use ``xfail(strict=True)`` where the current behaviour is a bug: if
the bug ever gets fixed, the test will ``XPASS`` and the strict
marker turns that into a failure, prompting us to flip the test.
"""

import time

import pytest

from tests.conftest import PTYFixture


class TestInterruptedExitPaths:
    """Happy paths out of interrupted."""

    def test_signal_idle_after_user_responded(
        self, pty: PTYFixture,
    ) -> None:
        """Stop hook → idle is honoured only once the user has typed
        *something* (printable or Escape) while in interrupted."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'interrupted'

        pty.tracker.on_input(b'y')
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

    def test_signal_idle_without_user_response_is_ignored(
        self, pty: PTYFixture,
    ) -> None:
        """Without user_responded, an idle signal is gated out — the
        session stays interrupted (prevents spurious Stop hooks from
        silently exiting an interrupt prompt)."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'interrupted'

        pty.write_signal('idle')
        # Gate: no user_responded → must stay interrupted
        assert pty.get_state() == 'interrupted'

    def test_escape_in_interrupted_sets_user_responded(
        self, pty: PTYFixture,
    ) -> None:
        """Pressing Escape while interrupted counts as a response —
        the next idle signal is honoured."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'interrupted'

        pty.tracker.on_input(b'\x1b')
        assert pty.tracker._user_responded is True

        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

    def test_cursor_hidden_resume_after_response(
        self, pty: PTYFixture,
    ) -> None:
        """After user_responded, a cursor-hidden output chunk moves
        interrupted → running (the CLI resumed processing the new
        input)."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'interrupted'

        pty.tracker.on_input(b'y')
        # Simulate the CLI resuming: cursor hidden render.
        pty.feed_output(b'\x1b[?25l\x1b[H\x1b[2JWorking...')
        assert pty.get_state() == 'running'

    def test_safety_timeout_exits_interrupted(
        self, pty: PTYFixture,
    ) -> None:
        """After SAFETY_WAITING_TIMEOUT of silence AND no confirming
        signal, the tracker force-exits to idle to prevent a permanent
        wedge.  Uses a fake clock to avoid a 60s real-time wait."""
        from leap.utils.constants import SAFETY_WAITING_TIMEOUT

        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'interrupted'

        # The interrupted→interrupted transition self-wrote an
        # 'interrupted' signal.  The safety-timeout path reads it and
        # keeps the state by design.  Simulate a lost signal (file
        # manually removed, e.g. by server cleanup) so the timeout
        # fallback actually fires.
        pty.clear_signal()

        original_clock = pty.tracker._clock
        frozen = time.time() + SAFETY_WAITING_TIMEOUT + 5.0
        pty.tracker._clock = lambda: frozen
        try:
            assert pty.get_state() == 'idle'
        finally:
            pty.tracker._clock = original_clock


class TestInterruptedWedges:
    """Behaviours that currently wedge the session — pinned so any
    future fix forces the test to flip."""

    @pytest.mark.xfail(
        strict=True,
        reason='Current behaviour: Enter while interrupted does NOT '
               'transition to running. Only Enter-in-IDLE fires the '
               'idle→running path. The user must send the new '
               'message via client (on_send) to escape interrupted.',
    )
    def test_enter_in_interrupted_moves_to_running(
        self, pty: PTYFixture,
    ) -> None:
        """User types a reply + Enter in the server terminal while
        interrupted.  Intuitive expectation: running.  Actual: stays
        interrupted (Enter handler is gated on state==IDLE)."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'interrupted'

        pty.tracker.on_input(b'fix the bug\r')
        assert pty.tracker.current_state == 'running'

    @pytest.mark.xfail(
        strict=True,
        reason='No cursor-visibility + silence path exits interrupted; '
               'if the user presses Escape to dismiss the prompt but '
               'nothing else changes on screen, state stays stuck '
               'until SAFETY_WAITING_TIMEOUT (60s).',
    )
    def test_double_escape_dismiss_returns_to_idle(
        self, pty: PTYFixture,
    ) -> None:
        """Escape #1 → interrupted, Escape #2 dismisses the TUI prompt
        and Claude returns to a normal idle prompt — but no hook fires
        and cursor isn't hidden, so Leap can't observe the transition
        without a 60s timeout."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'interrupted'

        pty.tracker.on_input(b'\x1b')
        # Simulate the TUI redrawing the idle prompt (cursor visible,
        # no Interrupted text).
        pty.feed_output(b'\x1b[?25h\x1b[H\x1b[2J> ')
        # Give it a full poll cycle.
        state = pty.wait_for_state('idle', timeout=1.5)
        assert state == 'idle'


class TestSuppressStaleInterrupt:
    """The ``_suppress_stale_interrupt`` flag gates re-entry into
    interrupted from stale 'Interrupted' text still on the TUI
    scrollback after the user has moved on."""

    def test_suppress_set_on_interrupted_to_idle(
        self, pty: PTYFixture,
    ) -> None:
        """Transitioning interrupted → idle sets the suppression flag
        so an ambient 'Interrupted' substring doesn't re-trigger."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'interrupted'

        pty.tracker.on_input(b'y')
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        assert pty.tracker._suppress_stale_interrupt is True

    def test_suppress_clears_when_pattern_leaves_screen(
        self, pty: PTYFixture,
    ) -> None:
        """Once the 'Interrupted' substring is no longer on screen,
        suppression clears so a *real* future interrupt can fire."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'interrupted'

        pty.tracker.on_input(b'y')
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'
        assert pty.tracker._suppress_stale_interrupt is True

        # Clear screen — no more "Interrupted" substring.
        pty.feed_output(b'\x1b[2J\x1b[H> ')
        # Trigger an _handle_idle_output pass.
        pty.get_state()
        assert pty.tracker._suppress_stale_interrupt is False

    def test_suppress_blocks_reentry_while_pattern_on_screen(
        self, pty: PTYFixture,
    ) -> None:
        """While the pattern is still on screen and suppress is set,
        a fresh Escape must not re-trigger interrupted."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        pty.tracker.on_input(b'y')
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'
        assert pty.tracker._suppress_stale_interrupt is True

        # User presses Escape again while 'Interrupted' is still in
        # scrollback — suppression must keep us in idle.
        pty.tracker.on_input(b'\x1b')
        # Replay the stale screen (same Interrupted text, no new I/O)
        pty.feed_output(b'stale output with Interrupted still visible')
        assert pty.get_state() == 'idle'
