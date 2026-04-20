"""Slash commands that fire no Stop hook.

``/clear``, ``/help``, and other in-TUI slash commands don't round-trip
through the agent, so the Stop hook never fires.  The tracker has to
return to idle via the running→idle cursor-visible + 5s-silence
fallback.  Also guards against the post-command screen accidentally
looking like a dialog.
"""

import pytest

from tests.conftest import PTYFixture


def _advance_clock(pty: PTYFixture, seconds: float) -> None:
    """Move the tracker's clock forward without actually sleeping."""
    base = pty.tracker._clock()
    pty.tracker._clock = lambda: base + seconds


class TestSlashCommandIdleRecovery:
    def test_clear_returns_to_idle_via_cursor_silence(
        self, pty: PTYFixture,
    ) -> None:
        """/clear: user hits Enter → running.  Output settles.  With
        cursor visible and 5s silence, the fallback fires → idle."""
        pty.tracker.on_input(b'/clear\r')
        assert pty.tracker.current_state == 'running'

        # /clear resets the screen and leaves a fresh idle prompt.
        pty.feed_output(b'\x1b[?25h\x1b[2J\x1b[H> ')

        # 5s silence with cursor visible → idle.
        _advance_clock(pty, 6.0)
        assert pty.get_state() == 'idle'

    def test_help_returns_to_idle_via_cursor_silence(
        self, pty: PTYFixture,
    ) -> None:
        """/help prints a help block, then returns to idle — same
        cursor+silence path."""
        pty.tracker.on_input(b'/help\r')
        assert pty.tracker.current_state == 'running'

        pty.feed_output(
            b'\x1b[?25h'
            b'/clear  clear conversation\r\n'
            b'/help   show help\r\n'
            b'/quit   exit\r\n'
            b'> ')

        _advance_clock(pty, 6.0)
        assert pty.get_state() == 'idle'

    def test_silence_under_five_seconds_stays_running(
        self, pty: PTYFixture,
    ) -> None:
        """The cursor+silence fallback has a 5s hysteresis — shorter
        silence must not flip us to idle."""
        pty.tracker.on_input(b'/clear\r')
        pty.feed_output(b'\x1b[?25h> ')

        _advance_clock(pty, 3.0)
        assert pty.get_state() == 'running'


class TestSlashCommandFalseDialog:
    """The cursor+silence → needs_permission path also checks for
    dialog patterns in the tail of the screen.  Slash-command output
    must not trip those by accident."""

    def test_help_output_not_mistaken_for_dialog(
        self, pty: PTYFixture,
    ) -> None:
        """/help lists commands with things like 'Enter' or 'Esc'
        mentioned — must not trigger needs_permission."""
        pty.tracker.on_input(b'/help\r')
        pty.feed_output(
            b'\x1b[?25h'
            b'Press Enter to run a command\r\n'
            b'Press Esc to cancel input\r\n'
            b'> ')

        _advance_clock(pty, 6.0)
        # ``is_dialog_certain`` needs BOTH phrases in compact form; with
        # spaces removed the substrings DO appear here, but on separate
        # content lines that don't match the full dialog shape.
        # Assert: we don't end up in needs_permission.
        assert pty.get_state() != 'needs_permission'


class TestSlashCommandAfterResponse:
    def test_clear_after_idle_allows_new_running(
        self, pty: PTYFixture,
    ) -> None:
        """After a real response (Stop→idle), a follow-up /clear still
        enters running via Enter-in-idle."""
        pty.tracker.on_send()
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        pty.tracker.on_input(b'/clear\r')
        assert pty.tracker.current_state == 'running'


class TestSlashCommandExtras:
    @pytest.mark.parametrize('cmd', [b'/clear', b'/help', b'/cost'])
    def test_any_slash_command_enters_running(
        self, pty: PTYFixture, cmd: bytes,
    ) -> None:
        """Every slash command that ends in Enter enters running —
        regardless of which one."""
        pty.tracker.on_input(cmd + b'\r')
        assert pty.tracker.current_state == 'running'
