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
