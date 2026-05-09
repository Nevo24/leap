"""Provider-specific state-tracker behaviours.

Each CLI provider tunes the state machine via overridable properties:

* ``silence_timeout`` — Codex drops from 60s (default) to 8s.
* ``cursor_hidden_while_idle`` — Ratatui hides cursor permanently;
  auto-resume and cursor+silence paths are suppressed.
* ``running_indicator_patterns`` — only Claude currently declares one.
* ``dialog_patterns`` — Codex has none (relies purely on hooks).
* ``valid_signal_states`` — some providers may restrict.
* ``confirmed_interrupt_pattern`` — Codex's "Conversation interrupted".

These tests exercise each tuned knob via the shared ``PTYFixture``.
"""

from tests.conftest import PTYFixture


class TestCodexProvider:
    def test_codex_has_no_dialog_patterns(self) -> None:
        from leap.cli_providers.codex import CodexProvider
        assert CodexProvider().dialog_patterns == []

    def test_codex_has_no_running_indicator(self) -> None:
        from leap.cli_providers.codex import CodexProvider
        assert CodexProvider().running_indicator_patterns == []

    def test_codex_cursor_hidden_while_idle_true(self) -> None:
        from leap.cli_providers.codex import CodexProvider
        assert CodexProvider().cursor_hidden_while_idle is True

    def test_codex_silence_timeout_is_short(self) -> None:
        from leap.cli_providers.codex import CodexProvider
        assert CodexProvider().silence_timeout == 8.0

    def test_codex_silence_timeout_fires_running_to_idle(
        self, pty_factory,
    ) -> None:
        """Codex's 8s silence timeout must force idle earlier than the
        default 60s.  Uses a fake clock to avoid the real wait."""
        from leap.cli_providers.codex import CodexProvider

        pty = pty_factory(provider=CodexProvider(), tag='codex-silence')
        pty.tracker.on_send()
        pty.feed_output(b'working on it...')
        # Advance 9 seconds → past 8s silence timeout, under 60s.
        base = pty.tracker._clock()
        pty.tracker._clock = lambda: base + 9.0
        assert pty.get_state() == 'idle'

    def test_codex_cursor_auto_resume_disabled(
        self, pty_factory,
    ) -> None:
        """cursor_hidden_while_idle=True disables the auto-resume path —
        a cursor-hidden chunk at idle must NOT transition to running."""
        from leap.cli_providers.codex import CodexProvider

        pty = pty_factory(provider=CodexProvider(), tag='codex-idle')
        pty.tracker.on_input(b'x')
        # Cursor hidden (Ratatui's permanent state) — for Claude this
        # would trigger idle→running; for Codex it must not.
        pty.feed_output(b'\x1b[?25l ratatui tui frame')
        assert pty.get_state() == 'idle'


class TestClaudeProvider:
    def test_claude_has_indicator(self) -> None:
        from leap.cli_providers.claude import ClaudeProvider
        patterns = ClaudeProvider().running_indicator_patterns
        assert b'Compactingconversation' in patterns

    def test_claude_has_dialog_patterns(self) -> None:
        from leap.cli_providers.claude import ClaudeProvider
        patterns = ClaudeProvider().dialog_patterns
        assert b'Entertoselect' in patterns
        assert b'Esctocancel' in patterns

    def test_claude_numbered_menu_is_dialog_certain(self) -> None:
        from leap.cli_providers.claude import ClaudeProvider
        provider = ClaudeProvider()
        # The Ink TUI numbered menu cursor is itself a certain dialog.
        assert provider.is_dialog_certain('\u276f1.')
        assert provider.is_dialog_certain('\u203a1.')

    def test_claude_cursor_visible_while_idle(self) -> None:
        from leap.cli_providers.claude import ClaudeProvider
        assert ClaudeProvider().cursor_hidden_while_idle is False

    def test_claude_silence_timeout_default(self) -> None:
        from leap.cli_providers.claude import ClaudeProvider
        assert ClaudeProvider().silence_timeout is None


class TestCursorAgentProvider:
    def test_cursor_agent_basics(self) -> None:
        from leap.cli_providers.cursor_agent import CursorAgentProvider
        provider = CursorAgentProvider()
        assert provider.name == 'cursor-agent'
        # Defaults: no running indicator, inherits base behaviours.
        assert provider.running_indicator_patterns == []


class TestGeminiProvider:
    def test_gemini_basics(self) -> None:
        from leap.cli_providers.gemini import GeminiProvider
        provider = GeminiProvider()
        assert provider.name == 'gemini'
        assert provider.running_indicator_patterns == []


class TestProviderIsolation:
    def test_claude_pattern_does_not_leak_into_codex(
        self, pty_factory,
    ) -> None:
        """Feeding 'Compacting conversation' to a Codex tracker must
        not flip its state — the pattern is Claude-specific."""
        from leap.cli_providers.codex import CodexProvider

        pty = pty_factory(provider=CodexProvider(), tag='codex-iso')
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        pty.write_signal('idle')
        pty.wait_for_state('idle', timeout=1.0)

        pty.feed_output(b'Compacting conversation...')
        assert pty.tracker.current_state == 'idle'

    def test_confirmed_interrupt_pattern_codex_specific(
        self, pty_factory,
    ) -> None:
        """Codex's confirmed interrupt pattern ('Conversationinterrupted')
        fires even without _interrupt_pending.  Claude's is None so the
        fallback doesn't apply."""
        from leap.cli_providers.codex import CodexProvider

        pty = pty_factory(provider=CodexProvider(), tag='codex-int')
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        pty.feed_output(b'\x1b[31mConversation interrupted\x1b[0m')
        assert pty.get_state() == 'interrupted'


class TestCodexTranscriptDetection:
    """Codex emits a ``task_complete`` event into its JSONL transcript.
    The tracker polls the transcript once per cycle and moves
    running → idle when it sees a fresh completion — much earlier than
    the 8s silence timeout would allow."""

    def test_task_complete_triggers_running_to_idle(
        self,
        pty_factory,
        tmp_path,
    ) -> None:
        import json
        from datetime import datetime, timezone

        from leap.cli_providers.codex import CodexProvider

        provider = CodexProvider()
        # Redirect transcript dir to tmp.
        today = datetime.now(timezone.utc).strftime('%Y/%m/%d')
        session_dir = tmp_path / 'codex' / today
        session_dir.mkdir(parents=True)

        class _TestCodex(CodexProvider):
            @property
            def transcript_sessions_dir(self):
                return tmp_path / 'codex'

        pty = pty_factory(provider=_TestCodex(), tag='codex-transcript')
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        assert pty.get_state() == 'running'

        # Write a JSONL with a fresh task_complete event.
        transcript = session_dir / 'session.jsonl'
        entry = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'payload': {
                'type': 'task_complete',
                'last_agent_message': 'All done!',
            },
        }
        transcript.write_text(json.dumps(entry) + '\n')

        # Next poll should see the completion and move to idle.
        assert pty.get_state() == 'idle'

    def test_stale_task_complete_ignored(
        self,
        pty_factory,
        tmp_path,
    ) -> None:
        """task_complete entries from before on_send (stale) must not
        trigger an immediate idle — the tracker compares timestamps
        against ``_running_since``."""
        import json
        import time
        from datetime import datetime, timezone

        from leap.cli_providers.codex import CodexProvider

        today = datetime.now(timezone.utc).strftime('%Y/%m/%d')
        session_dir = tmp_path / 'codex' / today
        session_dir.mkdir(parents=True)

        class _TestCodex(CodexProvider):
            @property
            def transcript_sessions_dir(self):
                return tmp_path / 'codex'

        # Write a transcript with a task_complete that's older than
        # the upcoming on_send().
        old_ts = datetime.fromtimestamp(
            time.time() - 1.0, tz=timezone.utc,
        ).isoformat()
        entry = {
            'timestamp': old_ts,
            'payload': {
                'type': 'task_complete',
                'last_agent_message': 'stale',
            },
        }
        transcript = session_dir / 'session.jsonl'
        transcript.write_text(json.dumps(entry) + '\n')

        pty = pty_factory(provider=_TestCodex(), tag='codex-stale')
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()  # running_since = now (> old_ts)
        assert pty.get_state() == 'running'


class TestClaudeTranscriptBlocksPrematureIdle:
    """Claude's per-session JSONL transcript is the source of truth for
    whether the agent loop is still running.  When a hook signal or
    screen heuristic claims idle but the transcript shows an unanswered
    ``tool_use`` from the current turn, the IDLE flip must be blocked —
    these tests pin that contract."""

    @staticmethod
    def _make_provider(projects_root):
        from leap.cli_providers.claude import ClaudeProvider

        class _TestClaude(ClaudeProvider):
            @property
            def transcript_projects_root(self):
                return projects_root

        return _TestClaude()

    @staticmethod
    def _write_assistant_entry(
        path,
        stop_reason: str,
        ts_offset_seconds: float,
    ) -> None:
        """Append an ``assistant`` entry whose timestamp is ``time.time()
        + ts_offset_seconds``."""
        import json
        import time
        from datetime import datetime, timezone

        ts = datetime.fromtimestamp(
            time.time() + ts_offset_seconds, tz=timezone.utc,
        ).isoformat().replace('+00:00', 'Z')
        entry = {
            'type': 'assistant',
            'timestamp': ts,
            'message': {
                'role': 'assistant',
                'stop_reason': stop_reason,
                'content': [],
            },
        }
        with open(path, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    def _setup(self, pty_factory, tmp_path, cwd_path):
        """Create a tracker bound to a tmp transcript root for ``cwd_path``."""
        from leap.utils.claude_session_move import slugify

        projects_root = tmp_path / 'projects'
        slug_dir = projects_root / slugify(str(cwd_path))
        slug_dir.mkdir(parents=True)
        transcript = slug_dir / 'session.jsonl'
        transcript.touch()

        provider = self._make_provider(projects_root)
        pty = pty_factory(provider=provider, tag='claude-transcript')
        # The fixture's cwd is signal_file.parent (= tmp_path); rebind
        # the tracker's cwd to the path whose slug matches our setup.
        pty.tracker._cwd = str(cwd_path)
        return pty, transcript

    def test_signal_idle_blocked_when_transcript_shows_tool_use(
        self, pty_factory, tmp_path,
    ) -> None:
        cwd = tmp_path / 'project'
        cwd.mkdir()
        pty, transcript = self._setup(pty_factory, tmp_path, cwd)
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        # Transcript writes a fresh assistant entry (post-on_send) with
        # an unanswered tool_use — agent is still in the loop.
        self._write_assistant_entry(transcript, 'tool_use', ts_offset_seconds=1.0)
        pty.write_signal('idle')
        # Signal alone would flip running→idle; transcript blocks it.
        assert pty.get_state() == 'running'
        # And the stale signal is cleaned up so it can't fire again.
        assert not pty.signal_file.exists()

    def test_signal_idle_allowed_when_transcript_shows_end_turn(
        self, pty_factory, tmp_path,
    ) -> None:
        cwd = tmp_path / 'project'
        cwd.mkdir()
        pty, transcript = self._setup(pty_factory, tmp_path, cwd)
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        # Fresh end_turn entry: agent is genuinely done.
        self._write_assistant_entry(transcript, 'end_turn', ts_offset_seconds=1.0)
        pty.write_signal('idle')
        assert pty.get_state() == 'idle'

    def test_stale_tool_use_does_not_block_idle(
        self, pty_factory, tmp_path,
    ) -> None:
        """A tool_use entry from BEFORE on_send (previous turn) must not
        block the current turn's idle transition."""
        cwd = tmp_path / 'project'
        cwd.mkdir()
        pty, transcript = self._setup(pty_factory, tmp_path, cwd)
        # Stale entry first, BEFORE on_send moves _running_since forward.
        self._write_assistant_entry(transcript, 'tool_use', ts_offset_seconds=-30.0)
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        pty.write_signal('idle')
        # No fresh entry → transcript guard returns False → idle proceeds.
        assert pty.get_state() == 'idle'

    def test_missing_transcript_falls_through(
        self, pty_factory, tmp_path,
    ) -> None:
        """If the slug directory doesn't exist (fresh session before
        first hook fire), the guard must return False so existing
        behaviour is unchanged."""
        from leap.utils.claude_session_move import slugify

        cwd = tmp_path / 'fresh-project'
        cwd.mkdir()
        # NOTE: we deliberately do NOT create the slug dir.
        provider = self._make_provider(tmp_path / 'projects')
        pty = pty_factory(provider=provider, tag='claude-fresh')
        pty.tracker._cwd = str(cwd)
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        pty.write_signal('idle')
        # Transcript guard returns False → idle proceeds.
        assert pty.get_state() == 'idle'

    def test_safety_silence_timeout_blocked_by_tool_use(
        self, pty_factory, tmp_path,
    ) -> None:
        """A long silent tool call (60 s safety timeout) is blocked
        when the transcript proves the agent is mid-tool_use."""
        import time as _t

        cwd = tmp_path / 'project'
        cwd.mkdir()
        pty, transcript = self._setup(pty_factory, tmp_path, cwd)
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        self._write_assistant_entry(transcript, 'tool_use', ts_offset_seconds=1.0)
        # Force a silent gap > SAFETY_SILENCE_TIMEOUT (60 s) without
        # touching the actual clock — set _last_output_time to "long ago".
        pty.tracker._last_output_time = _t.time() - 120.0
        # No signal — purely the safety silence path firing.
        assert pty.get_state() == 'running'

    def test_session_id_lookup_picks_right_jsonl(
        self, pty_factory, tmp_path,
    ) -> None:
        """When ``cli_sessions/claude/<tag>.json`` records a session_id,
        the provider must read THAT file even if a different .jsonl was
        modified more recently (cross-session bleed protection)."""
        import json
        import os

        from leap.utils.claude_session_move import slugify

        cwd = tmp_path / 'project'
        cwd.mkdir()
        projects_root = tmp_path / 'projects'
        slug_dir = projects_root / slugify(str(cwd))
        slug_dir.mkdir(parents=True)

        # Two transcripts in the same slug dir: ours (older) and a
        # different session (newer).  The recorded session_id must win.
        ours = slug_dir / 'aaa-our-session.jsonl'
        ours.touch()
        other = slug_dir / 'bbb-other-session.jsonl'
        other.touch()
        # Make 'other' the newest by mtime.
        os.utime(ours, (1, 1))
        os.utime(other, None)
        # Write tool_use into 'other' (would falsely block) and end_turn
        # into 'ours' (correctly allows idle).
        self._write_assistant_entry(other, 'tool_use', ts_offset_seconds=1.0)
        self._write_assistant_entry(ours, 'end_turn', ts_offset_seconds=1.0)

        # Record our session_id in cli_sessions.
        storage_dir = tmp_path / 'storage'
        sessions = storage_dir / 'cli_sessions' / 'claude'
        sessions.mkdir(parents=True)
        (sessions / 'claude-sid.json').write_text(json.dumps([
            {'session_id': 'aaa-our-session', 'transcript_path': str(ours)},
        ]))

        # Wire the tracker manually so storage_dir matches.
        provider = self._make_provider(projects_root)
        pty = pty_factory(provider=provider, tag='claude-sid')
        pty.tracker._cwd = str(cwd)
        pty.tracker._tag = 'claude-sid'
        pty.tracker._storage_dir = storage_dir
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        pty.write_signal('idle')
        # Provider reads OUR transcript (end_turn) → idle allowed.
        assert pty.get_state() == 'idle'


class TestSignalStateVocabulary:
    def test_all_providers_accept_idle(self) -> None:
        from leap.cli_providers.claude import ClaudeProvider
        from leap.cli_providers.codex import CodexProvider
        from leap.cli_providers.gemini import GeminiProvider
        from leap.cli_providers.cursor_agent import CursorAgentProvider

        for provider in (
            ClaudeProvider(), CodexProvider(),
            GeminiProvider(), CursorAgentProvider(),
        ):
            assert 'idle' in provider.valid_signal_states

    def test_all_providers_accept_waiting_states(self) -> None:
        from leap.cli_providers.claude import ClaudeProvider
        from leap.cli_providers.codex import CodexProvider
        from leap.cli_providers.gemini import GeminiProvider
        from leap.cli_providers.cursor_agent import CursorAgentProvider

        for provider in (
            ClaudeProvider(), CodexProvider(),
            GeminiProvider(), CursorAgentProvider(),
        ):
            assert 'needs_permission' in provider.valid_signal_states
            assert 'needs_input' in provider.valid_signal_states
