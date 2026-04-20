"""Resume detection: leaving a waiting state after the user responds.

The tracker uses cursor visibility + ``_user_responded`` to detect
that the CLI has moved past a dialog, even when no explicit signal
fires.  These tests check both the positive path (responded → resume)
and the negative path (stale TUI rendering must not false-resume).
"""

import time

from tests.conftest import PTYFixture


class TestResumeFromWait:
    def test_tui_rendering_after_interrupted_stays_interrupted(
        self, pty: PTYFixture,
    ) -> None:
        """After 'Interrupted' → interrupted, TUI status bar rendering
        without user input must NOT falsely trigger resume to running."""
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        pty.send_input(b'\x1b')
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'interrupted'

        time.sleep(2.5)

        pty.send_line(
            r'printf "\033[24;1H\033[KNevo.Mashiach 10%% Opus\n"')
        pty.drain_to_tracker(timeout=1.0)

        assert pty.tracker.current_state == 'interrupted'

    def test_resume_after_user_responds_and_signal(
        self, pty: PTYFixture,
    ) -> None:
        """After interrupted, user typing sets _user_responded, then
        a signal file idle transition returns to idle."""
        pty.tracker.on_send()
        pty.send_input(b'\x1b')
        pty.send_line('echo Interrupted')
        pty.drain_to_tracker(timeout=1.0)
        assert pty.tracker.current_state == 'interrupted'

        pty.tracker.on_input(b'y')
        assert pty.tracker._user_responded

        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'
