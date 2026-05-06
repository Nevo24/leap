"""Prompt snapshot lifecycle.

The tracker keeps two screen captures used to render and recover
dialogs:

* ``_prompt_snapshot`` — lines captured when entering a waiting state.
  Served to the client via ``get_prompt_output`` so it can show the
  dialog text even after the screen has scrolled.
* ``_last_running_snapshot`` — lines captured at the running→idle
  boundary.  Feeds the late-notification guard when a hook arrives
  after the screen was reset.

These tests verify both snapshots are populated, preserved, fallback-
replaced, and cleared at the right moments.
"""

from tests.conftest import PTYFixture


_DIALOG = (
    b'Allow tool?\r\n'
    b'1. Yes\r\n2. No\r\n'
    b'Enter to select  Esc to cancel\r\n'
)

# Pushes the dialog footer out of the bottom-5 filled rows so the
# Stop-hook-time dialog guard at state_tracker.py:948 doesn't fire.
# That guard now (post 8b43750) intercepts running→idle and routes
# directly to needs_permission when the bottom of the screen still
# shows a dialog footer.  These tests want to verify the
# _last_running_snapshot path which only fires when the guard does
# NOT — i.e. when the dialog has scrolled out of bottom-5.  The
# dialog still ends up in _last_running_snapshot (which captures
# the full screen buffer, not just the tail).
_PUSH_OUT = (
    b'Working...\r\n'
    b'Step one done\r\n'
    b'Step two done\r\n'
    b'Step three done\r\n'
    b'Step four done\r\n'
    b'Done.\r\n'
)


class TestPromptSnapshotCapture:
    def test_snapshot_captured_on_needs_permission(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'
        assert pty.tracker._prompt_snapshot
        # The snapshot contains the dialog text.
        rendered = '\n'.join(pty.tracker._prompt_snapshot)
        assert 'Allow tool?' in rendered

    def test_get_prompt_output_returns_snapshot(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        output = pty.tracker.get_prompt_output()
        assert 'Allow tool?' in output
        assert '1. Yes' in output
        assert '2. No' in output

    def test_snapshot_strips_box_borders(
        self, pty: PTYFixture,
    ) -> None:
        """get_prompt_output strips leading/trailing box-drawing lines
        so the returned output is useful to clients."""
        pty.tracker.on_send()
        boxed = (
            b'\xe2\x94\x8c\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\r\n'  # ┌───
            b'Allow tool?\r\n'
            b'1. Yes\r\n2. No\r\n'
            b'Enter to select  Esc to cancel\r\n'
            b'\xe2\x94\x94\xe2\x94\x80\xe2\x94\x80\xe2\x94\x80\r\n'  # └───
        )
        pty.feed_output(boxed)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        out = pty.tracker.get_prompt_output()
        # Strip box borders → first/last lines shouldn't be pure box chars.
        lines = out.splitlines()
        assert lines[0].strip().startswith('Allow')


class TestLastRunningSnapshot:
    def test_captured_on_running_to_idle(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.feed_output(_PUSH_OUT)  # roll dialog out of bottom-5
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'
        # The dialog was on screen at the transition — snapshot kept.
        assert pty.tracker._last_running_snapshot
        assert any('Allow tool?' in ln
                   for ln in pty.tracker._last_running_snapshot)

    def test_consumed_by_late_notification_guard(
        self, pty: PTYFixture,
    ) -> None:
        """After Stop→idle clears the screen, a late Notification can
        still be accepted if the snapshot contains dialog patterns."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.feed_output(_PUSH_OUT)  # roll dialog out of bottom-5
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'
        assert pty.tracker._last_running_snapshot

        # Screen is now empty (reset by transition), but snapshot has
        # the dialog — late Notification should succeed.
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

    def test_cleared_on_fresh_send(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.feed_output(_PUSH_OUT)  # roll dialog out of bottom-5
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'
        assert pty.tracker._last_running_snapshot

        pty.tracker.on_send()
        assert pty.tracker._last_running_snapshot == []


class TestSnapshotClearing:
    def test_prompt_snapshot_cleared_on_waiting_to_idle(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'
        assert pty.tracker._prompt_snapshot

        pty.tracker.on_input(b'1')  # user_responded
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'
        assert pty.tracker._prompt_snapshot == []

    def test_prompt_snapshot_cleared_on_interrupted(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'
        assert pty.tracker._prompt_snapshot

        pty.tracker.on_input(b'\x1b')
        pty.feed_output(b'Interrupted')
        assert pty.get_state() == 'interrupted'
        # Interrupted clears the waiting snapshot.
        assert pty.tracker._prompt_snapshot == []


class TestSnapshotFallback:
    def test_live_screen_used_when_snapshot_empty(
        self, pty: PTYFixture,
    ) -> None:
        """get_prompt_output falls back to the live screen for waiting
        states if the snapshot is empty and the live screen has been
        repainted since the transition (e.g. TUI redraw)."""
        pty.tracker.on_send()
        pty.feed_output(_DIALOG)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        # Forcibly blank the stored snapshot AND repaint the screen.
        pty.tracker._prompt_snapshot = []
        pty.feed_output(_DIALOG)

        out = pty.tracker.get_prompt_output()
        assert 'Allow' in out


class TestSnapshotGrowthPolicy:
    def test_snapshot_replaced_when_new_content_is_larger(
        self, pty: PTYFixture,
    ) -> None:
        """``_handle_waiting_output`` replaces the snapshot when new
        output has at least as many non-blank lines."""
        pty.tracker.on_send()
        # Initial small dialog.
        pty.feed_output(
            b'Allow?\r\n'
            b'Enter to select  Esc to cancel\r\n',
        )
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'
        initial = list(pty.tracker._prompt_snapshot)

        # Fuller redraw arrives.
        pty.feed_output(_DIALOG)
        assert pty.tracker._prompt_snapshot != initial
        rendered = '\n'.join(pty.tracker._prompt_snapshot)
        assert '1. Yes' in rendered
