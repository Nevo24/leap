"""PTY lifecycle: alive/dead transitions, state reset on death.

When the underlying CLI process dies (Claude crash, SIGKILL, parent
disconnect) ``get_state(pty_alive=False)`` must force the state back
to idle and clear all in-flight flags / snapshots so a restart lands
cleanly.
"""

from tests.conftest import PTYFixture


_DIALOG = (
    b'Allow tool?\r\n1. Yes\r\n2. No\r\n'
    b'Enter to select  Esc to cancel\r\n'
)


class TestPTYDeathResetsState:
    def test_dead_pty_from_running_goes_idle(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        assert pty.get_state() == 'running'
        assert pty.tracker.get_state(pty_alive=False) == 'idle'

    def test_dead_pty_from_needs_permission_goes_idle(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'
        assert pty.tracker.get_state(pty_alive=False) == 'idle'

    def test_dead_pty_from_interrupted_goes_idle(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')
        pty.feed_output(b'Interrupted')
        assert pty.get_state() == 'interrupted'
        assert pty.tracker.get_state(pty_alive=False) == 'idle'

    def test_dead_pty_clears_flags(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_input(b'x')  # _seen_user_input
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b')  # _interrupt_pending
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        pty.get_state()
        pty.tracker.on_input(b'1')  # _user_responded

        # PTY dies — all flags reset, snapshots cleared.
        assert pty.tracker.get_state(pty_alive=False) == 'idle'
        assert pty.tracker._interrupt_pending is False
        assert pty.tracker._user_responded is False
        assert pty.tracker._user_input_since_idle is False
        assert pty.tracker._seen_user_input is False
        assert pty.tracker._prompt_snapshot == []
        assert pty.tracker._last_running_snapshot == []


class TestPTYDeadReentry:
    def test_repeated_dead_polls_are_idempotent(
        self, pty: PTYFixture,
    ) -> None:
        """Auto-sender keeps polling once the PTY is dead — each poll
        must be a stable no-op."""
        pty.tracker.on_send()
        assert pty.tracker.get_state(pty_alive=False) == 'idle'
        for _ in range(5):
            assert pty.tracker.get_state(pty_alive=False) == 'idle'

    def test_alive_again_after_dead_does_not_leak_state(
        self, pty: PTYFixture,
    ) -> None:
        """Edge case: a fixture that goes dead then alive again (e.g.
        test logic reusing the tracker) must start clean from idle
        with no stale flags."""
        pty.tracker.on_send()
        pty.tracker.get_state(pty_alive=False)  # death
        # Now pretend PTY is alive again.
        assert pty.tracker.get_state(pty_alive=True) == 'idle'
        assert pty.tracker._interrupt_pending is False


class TestPTYEOFDuringCompact:
    def test_dead_pty_overrides_compacting_indicator(
        self, pty: PTYFixture,
    ) -> None:
        """Even with a live compacting indicator on screen, a dead PTY
        forces idle — running-indicator guards only run while alive."""
        from tests.integration.test_compact_scenarios import _INDICATOR

        pty.tracker.on_send()
        pty.feed_output(_INDICATOR)
        assert pty.get_state() == 'running'
        assert pty.tracker.get_state(pty_alive=False) == 'idle'


class TestSafetySilenceTimeoutRunning:
    """Positive test for the global 60s running-silence safety fallback.
    We already test it gets *skipped* while compacting; here we prove
    it *fires* in the normal case so a stuck-running session recovers."""

    def test_running_silence_timeout_forces_idle(
        self, pty: PTYFixture,
    ) -> None:
        from leap.utils.constants import SAFETY_SILENCE_TIMEOUT

        pty.tracker.on_send()
        # One output event so _last_output_time is populated.
        pty.feed_output(b'working')
        assert pty.get_state() == 'running'

        base = pty.tracker._clock()
        pty.tracker._clock = lambda: base + SAFETY_SILENCE_TIMEOUT + 5.0
        assert pty.get_state() == 'idle'
