"""Workspace trust dialog detection at startup.

Claude Code shows a "Do you trust this folder?" dialog on first run.
The tracker detects it via startup trust-dialog pattern matching and
moves idle → needs_permission *before* the user ever types.
"""

import time

from tests.conftest import PTYFixture


class TestTrustDialog:
    def test_plain_text(self, pty: PTYFixture) -> None:
        """PTY output with literal spaces → needs_permission."""
        assert pty.get_state() == 'idle'
        assert not pty.tracker._seen_user_input

        pty.send_line(
            'printf "Is this a project you created or one you trust?\\n'
            '> 1. Yes, I trust this folder\\n'
            '  2. No, exit\\n"'
        )
        pty.drain_to_tracker(timeout=1.0)

        assert pty.tracker.current_state == 'needs_permission'

    def test_cursor_positioned(self, pty: PTYFixture) -> None:
        """TUI output with cursor positioning (no literal spaces)."""
        assert pty.get_state() == 'idle'

        pty.send_line(
            r'printf "\033[10;1HIs\033[10;4Hthis\033[10;9Ha'
            r'\033[10;11Hproject\033[10;19Hyou\033[10;23Htrust?\n'
            r'\033[11;3H1.\033[11;6HYes,\033[11;11HI'
            r'\033[11;13Htrust\033[11;19Hthis\033[11;24Hfolder\n"'
        )
        pty.drain_to_tracker(timeout=1.0)

        assert pty.tracker.current_state == 'needs_permission'

    def test_recovers_via_signal(self, pty: PTYFixture) -> None:
        """After trust dialog resolves (signal idle) we return to idle."""
        pty.send_line(
            'printf "Is this a project you trust?\\n'
            '> 1. Yes, I trust this folder\\n"'
        )
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'needs_permission'

        pty.tracker.on_input(b'\r')
        time.sleep(2.5)
        pty.write_signal('idle')
        assert pty.get_state() == 'idle'

    def test_startup_output_goes_to_idle(self, pty: PTYFixture) -> None:
        """After user answers, Claude's startup banner must not flip
        state to running."""
        pty.send_line(
            'printf "Is this a project you trust?\\n'
            '> 1. Yes, I trust this folder\\n"'
        )
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'needs_permission'

        pty.tracker.on_input(b'\r')
        time.sleep(2.5)

        pty.send_line(
            r'printf "\033[2J\033[HClaude Code v2.1.41\n'
            r'Opus 4.6 \xc2\xb7 Claude API\n/Users/test\n"'
        )
        pty.drain_to_tracker(timeout=1.0)

        assert pty.tracker.current_state == 'idle'
