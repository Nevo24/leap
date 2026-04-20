"""Signal-file parsing: robustness against malformed hook payloads.

The hook script is well-tested but third-party hook scripts or bugs
could write empty files, invalid JSON, unknown state values, or legacy
aliases.  None of these should crash the tracker — unparseable content
is treated as "no signal" and the state stays put.
"""

from tests.conftest import PTYFixture


class TestMalformedSignalFile:
    def test_empty_file(self, pty: PTYFixture) -> None:
        pty.tracker.on_send()
        pty.signal_file.write_text('')
        assert pty.get_state() == 'running'

    def test_invalid_json(self, pty: PTYFixture) -> None:
        pty.tracker.on_send()
        pty.signal_file.write_text('{this is not json}')
        assert pty.get_state() == 'running'

    def test_json_array_instead_of_object(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.signal_file.write_text('["idle"]')
        assert pty.get_state() == 'running'

    def test_missing_state_key(self, pty: PTYFixture) -> None:
        pty.tracker.on_send()
        pty.signal_file.write_text('{"other_key": "idle"}')
        assert pty.get_state() == 'running'

    def test_unknown_state_value(self, pty: PTYFixture) -> None:
        pty.tracker.on_send()
        pty.signal_file.write_text('{"state": "foobar"}')
        assert pty.get_state() == 'running'

    def test_null_state(self, pty: PTYFixture) -> None:
        pty.tracker.on_send()
        pty.signal_file.write_text('{"state": null}')
        assert pty.get_state() == 'running'


class TestLegacyAliases:
    def test_has_question_alias_accepted(
        self, pty: PTYFixture,
    ) -> None:
        """Older hooks may still write 'has_question' — must be
        normalised to 'needs_input'."""
        pty.tracker.on_send()
        from tests.integration.test_waiting_wedge import _DIALOG_BYTES
        pty.feed_output(_DIALOG_BYTES)
        pty.signal_file.write_text('{"state": "has_question"}')
        assert pty.get_state() == 'needs_input'


class TestSignalFileTurnover:
    def test_stale_signal_deleted_on_init(
        self, pty_factory,
    ) -> None:
        """The tracker init removes any pre-existing signal file from
        a previous server so it can't replay into the new session."""
        from pathlib import Path

        fixture = pty_factory(tag='stale')
        # Write a signal before the next tracker is created.
        stale_path = fixture.signal_file.parent / 'second.signal'
        Path(stale_path).write_text('{"state": "needs_permission"}')
        assert stale_path.exists()

        # Spawn a second fixture that points at that file.
        second = pty_factory(tag='second')
        # The new tracker's init removed the file.
        assert not second.signal_file.exists()


class TestSignalFileConcurrentWrites:
    def test_overwritten_signal_reads_latest(
        self, pty: PTYFixture,
    ) -> None:
        """A hook overwrites the file multiple times — we read the
        latest content on the next poll."""
        pty.tracker.on_send()
        pty.signal_file.write_text('{"state": "needs_permission"}')
        pty.signal_file.write_text('{"state": "idle"}')
        # Without dialog patterns on screen the needs_permission read
        # would be rejected by the late-notification guard anyway; the
        # second write means we read 'idle' directly.
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

    def test_whitespace_around_payload_tolerated(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.signal_file.write_text(
            '\n\n  {"state": "idle"}  \n\n',
        )
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'


class TestUnlinkedMidPoll:
    def test_signal_deleted_between_existence_and_read(
        self, pty: PTYFixture,
    ) -> None:
        """Race where the signal is deleted after exists() but before
        read_text() — OSError is swallowed, current state returned."""
        import os

        pty.tracker.on_send()
        pty.signal_file.write_text('{"state": "idle"}')
        os.unlink(pty.signal_file)
        # Next poll finds no file and returns current state.
        assert pty.get_state() == 'running'
