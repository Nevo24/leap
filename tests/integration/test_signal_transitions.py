"""Signal-file transitions with real PTY I/O.

Covers the primary happy path: a hook writes a state to the signal
file and the tracker picks it up on the next poll.  Verifies that the
late-notification guard requires dialog patterns on screen before
accepting needs_permission / needs_input signals.
"""

import json
import time
from datetime import datetime, timezone

from leap.utils.constants import SAFETY_SILENCE_TIMEOUT

from tests.conftest import PTYFixture


class TestSignalFile:
    """Signal file transitions with real file I/O."""

    def test_on_send_then_signal_idle(self, pty: PTYFixture) -> None:
        """on_send → running, then signal file → idle."""
        assert pty.get_state() == 'idle'
        pty.tracker.on_send()
        assert pty.get_state() == 'running'
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

    def test_signal_needs_permission(self, pty: PTYFixture) -> None:
        pty.tracker.on_send()
        # Late-notification guard (a821533) requires dialog patterns
        # visible on screen before accepting the signal, to reject
        # late-arriving Notification hooks that fire after the CLI
        # already finished.
        pty.feed_output(
            b'Allow tool?  Enter to select  Esc to cancel\n')
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

    def test_signal_needs_input(self, pty: PTYFixture) -> None:
        pty.tracker.on_send()
        # See test_signal_needs_permission — dialog patterns required.
        pty.feed_output(
            b'Allow tool?  Enter to select  Esc to cancel\n')
        pty.write_signal('needs_input')
        assert pty.get_state() == 'needs_input'


class TestSafetyTimeoutWithTranscript:
    """The 60 s safety silence timeout is the canonical fallback for a
    long silent tool call (Bash, WebFetch with no progress output).
    The transcript guard must keep the session in RUNNING when the
    transcript shows the agent is still mid-tool_use — otherwise the
    auto-sender fires queued messages into a still-busy CLI."""

    def _setup(self, pty_factory, tmp_path):
        from leap.cli_providers.claude import ClaudeProvider
        from leap.utils.claude_session_move import slugify

        cwd = tmp_path / 'project'
        cwd.mkdir()
        projects_root = tmp_path / 'projects'
        slug_dir = projects_root / slugify(str(cwd))
        slug_dir.mkdir(parents=True)
        transcript = slug_dir / 'session.jsonl'
        transcript.touch()

        class _TestClaude(ClaudeProvider):
            @property
            def transcript_projects_root(self):
                return projects_root

        pty = pty_factory(provider=_TestClaude(), tag='safety-tx')
        pty.tracker._cwd = str(cwd)
        return pty, transcript

    @staticmethod
    def _write_assistant(transcript, stop_reason: str, ts_offset: float) -> None:
        ts = datetime.fromtimestamp(
            time.time() + ts_offset, tz=timezone.utc,
        ).isoformat().replace('+00:00', 'Z')
        with open(transcript, 'a') as f:
            f.write(json.dumps({
                'type': 'assistant',
                'timestamp': ts,
                'message': {'stop_reason': stop_reason, 'content': []},
            }) + '\n')

    def test_safety_silence_blocked_when_tool_use_in_transcript(
        self, pty_factory, tmp_path,
    ) -> None:
        pty, transcript = self._setup(pty_factory, tmp_path)
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        self._write_assistant(transcript, 'tool_use', ts_offset=1.0)
        # Force > 60 s output silence (default SAFETY_SILENCE_TIMEOUT).
        # Both ``_last_output_time`` and ``_running_since`` are aged
        # because silence is measured from ``max(_last_output_time,
        # _running_since)`` so that pre-RUNNING silence does not count.
        past = time.time() - (SAFETY_SILENCE_TIMEOUT + 10)
        pty.tracker._last_output_time = past
        pty.tracker._running_since = past
        # Silence alone would trigger running→idle; transcript blocks it.
        assert pty.get_state() == 'running'

    def test_safety_silence_proceeds_when_transcript_silent(
        self, pty_factory, tmp_path,
    ) -> None:
        """No transcript activity for the current turn → guard returns
        False → silence-timeout proceeds normally to IDLE."""
        pty, transcript = self._setup(pty_factory, tmp_path)
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        # No fresh entries written.  Both ``_last_output_time`` and
        # ``_running_since`` are aged because silence is measured from
        # ``max(_last_output_time, _running_since)`` so pre-RUNNING
        # silence (e.g. from a long dialog wait) does not count.
        past = time.time() - (SAFETY_SILENCE_TIMEOUT + 10)
        pty.tracker._last_output_time = past
        pty.tracker._running_since = past
        assert pty.get_state() == 'idle'
