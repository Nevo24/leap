"""Conversation-compaction scenarios (/compact + auto-compact).

Claude fires no hook for compaction, and between-turns auto-compact
starts right after a Stop hook has already written ``idle``.  The
tracker's running-indicator support should:

* move idle → running when ``Compacting conversation`` appears
* ignore a ``running→idle`` signal while the indicator is visible
* skip the cursor+silence and safety-silence ``running→idle`` fallbacks
* resume the normal ``running→idle`` flow once the indicator leaves
"""

import time

from leap.utils.constants import SAFETY_SILENCE_TIMEOUT

from tests.conftest import PTYFixture


_INDICATOR = b'\xe2\x9c\xbb Compacting conversation... (12s)'
_CLEAR = b'\x1b[2J\x1b[H'


def _advance(pty: PTYFixture, seconds: float) -> None:
    base = pty.tracker._clock()
    pty.tracker._clock = lambda: base + seconds


class TestCompactSlashCommand:
    """User typed /compact — state is already running via Enter, the
    indicator keeps it there until compaction finishes."""

    def test_stays_running_during_compact(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_input(b'/compact\r')
        assert pty.tracker.current_state == 'running'

        pty.feed_output(_CLEAR + _INDICATOR)
        # Simulate a long compaction by advancing the clock.
        _advance(pty, 30.0)
        assert pty.get_state() == 'running'

    def test_returns_to_idle_after_indicator_gone(
        self, pty: PTYFixture,
    ) -> None:
        """Once compaction completes and the indicator is replaced by
        the normal idle prompt, the cursor+silence fallback takes over."""
        pty.tracker.on_input(b'/compact\r')
        pty.feed_output(_CLEAR + _INDICATOR)
        assert pty.get_state() == 'running'

        # Compaction ends — screen repaints to a plain idle prompt.
        pty.feed_output(_CLEAR + b'\x1b[?25h> ')
        # 5s of silence now, no indicator.
        _advance(pty, 6.0)
        assert pty.get_state() == 'idle'


class TestAutoCompactBetweenTurns:
    """The canonical wedge scenario: Stop hook writes idle, then
    auto-compact begins on its own."""

    def test_idle_to_running_when_indicator_appears(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        pty.feed_output(_CLEAR + _INDICATOR)
        assert pty.tracker.current_state == 'running'

    def test_indicator_requires_seen_user_input(
        self, pty: PTYFixture,
    ) -> None:
        """Pre-startup (no user input yet) we don't flip on the
        indicator — avoids noise from startup banner anomalies."""
        pty.feed_output(_CLEAR + _INDICATOR)
        assert pty.tracker.current_state == 'idle'


class TestCompactGuards:
    """Each of the four idle paths must stay blocked while the
    indicator is on screen."""

    def test_idle_signal_suppressed_while_compacting(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_CLEAR + _INDICATOR)
        assert pty.get_state() == 'running'

        pty.write_signal('idle')
        assert pty.get_state() == 'running'
        assert not pty.signal_file.exists()

    def test_cursor_silence_fallback_skipped_while_compacting(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        # Visible cursor + silence would normally trigger running→idle
        pty.feed_output(
            b'\x1b[?25h' + _CLEAR + _INDICATOR,
        )
        _advance(pty, 10.0)
        assert pty.get_state() == 'running'

    def test_safety_silence_timeout_skipped_while_compacting(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_CLEAR + _INDICATOR)
        _advance(pty, SAFETY_SILENCE_TIMEOUT + 10.0)
        assert pty.get_state() == 'running'


class TestCompactAndInterrupts:
    """Pressing Escape during compaction: Claude aborts the compact
    and prints 'Interrupted'.  The interrupted pattern must still
    win over the running indicator."""

    def test_escape_during_compact_goes_interrupted(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(_CLEAR + _INDICATOR)
        assert pty.get_state() == 'running'

        pty.tracker.on_input(b'\x1b')
        # Claude aborts; screen now shows Interrupted — no more indicator.
        pty.feed_output(_CLEAR + b'Interrupted')
        assert pty.get_state() == 'interrupted'


class TestCompactAndOtherProviders:
    """Other providers must not pick up Claude's indicator."""

    def test_codex_provider_does_not_transition(
        self, pty_factory,
    ) -> None:
        from leap.cli_providers.codex import CodexProvider

        pty = pty_factory(provider=CodexProvider(), tag='codex')
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        pty.write_signal('idle')
        # Codex's silence_timeout is 8s so wait_for_state with real sleep
        # is fine here; we've already written the idle signal.
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        pty.feed_output(_CLEAR + _INDICATOR)
        # Codex provider has no running_indicator_patterns — stays idle.
        assert pty.tracker.current_state == 'idle'


class TestCompactIndicatorLiveRender:
    """Smoke test against a real bash echo of the indicator string —
    verifies pyte actually renders the substring when fed via PTY."""

    def test_indicator_matches_via_real_pty(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.send_line(r'printf "\xe2\x9c\xbb Compacting conversation... (1s)"')
        time.sleep(0.3)
        pty.drain_to_tracker(timeout=0.8)
        assert pty.tracker._screen_has_running_indicator()


class TestCompactIndicatorBrittleness:
    """The indicator is a literal substring match on compact (no-space)
    screen text.  These tests pin the fragility so we notice if Claude
    ever renames the spinner — fix would be to broaden the pattern
    list, not change tracker logic.
    """

    def test_different_wording_does_not_match(
        self, pty: PTYFixture,
    ) -> None:
        """If Anthropic renames it to "Summarizing conversation…" the
        pattern silently stops firing.  Pinned as a canary."""
        pty.tracker.on_input(b'x')
        pty.tracker.on_send()
        pty.write_signal('idle')
        assert pty.wait_for_state('idle', timeout=1.0) == 'idle'

        pty.feed_output(_CLEAR + b'Summarizing conversation...')
        # With the current pattern this stays idle — this is a canary,
        # not a bug.  Flip to an indicator transition if Claude's text
        # ever changes to this form.
        assert pty.tracker.current_state == 'idle'

    def test_indicator_is_case_sensitive(
        self, pty: PTYFixture,
    ) -> None:
        """Lower-case 'compacting conversation' in response text must
        NOT match the pattern (it's case-sensitive 'Compacting')."""
        pty.tracker.on_send()
        pty.feed_output(_CLEAR + b'compacting conversation state')
        assert not pty.tracker._screen_has_running_indicator()

    def test_indicator_requires_adjacency(
        self, pty: PTYFixture,
    ) -> None:
        """'Compacting' on one screen row and 'conversation' nowhere
        near it on the screen must not match.  Tests the compact form:
        it's spaces+newlines-stripped, so they must be adjacent in
        concatenation order."""
        pty.tracker.on_send()
        # Two words far apart: "Compacting" at row 1, then unrelated
        # filler, then "conversation" much later.  Compact form will
        # have other chars between them.
        msg = (
            _CLEAR + b'Compacting the index.\r\n'
            + b'Line of filler.\r\n' * 10
            + b'Then we parse the conversation log.\r\n'
        )
        pty.feed_output(msg)
        assert not pty.tracker._screen_has_running_indicator()

    def test_response_text_mentioning_compacting_conversation(
        self, pty: PTYFixture,
    ) -> None:
        """KNOWN FALSE POSITIVE: if an assistant response literally
        contains the words 'Compacting conversation' adjacent and the
        text is still on pyte's 50-row screen, the pattern fires.

        Acceptable tradeoff — self-heals when the text scrolls off.
        This test *documents* the false positive so we notice if the
        specificity ever changes."""
        pty.tracker.on_send()
        pty.feed_output(_CLEAR + b'Note: Compacting conversation is a '
                        b'feature in Claude Code.\r\n')
        assert pty.tracker._screen_has_running_indicator()
