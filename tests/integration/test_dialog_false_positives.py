"""Dialog-pattern false positives.

Claude's response text may legitimately contain phrases like "Enter
to select" or "Esc to cancel" in code blocks or tutorials — the
cursor+silence → needs_permission path must not fire on those.

``is_dialog_certain`` requires the full standard footer
("Entertoselect" AND "Esctocancel" in compact form) OR a numbered
menu cursor (``\u276f1.``).  We test that partial matches, split
across lines, or appearing in body text don't trip detection.
"""

from tests.conftest import PTYFixture


def _advance(pty: PTYFixture, seconds: float) -> None:
    base = pty.tracker._clock()
    pty.tracker._clock = lambda: base + seconds


class TestDialogFalsePositives:
    def test_partial_pattern_in_response_ignored(
        self, pty: PTYFixture,
    ) -> None:
        """Only 'Enter to select' present — no 'Esc to cancel'.
        ``is_dialog_certain`` requires both."""
        pty.tracker.on_send()
        pty.feed_output(
            b'\x1b[?25h'
            b'To run this command press Enter to select the file.\r\n'
            b'> ',
        )
        _advance(pty, 6.0)
        assert pty.get_state() == 'idle'

    def test_phrases_pushed_out_of_tail_by_response_body(
        self, pty: PTYFixture,
    ) -> None:
        """Phrases appear early in a long response but the tail-check
        only looks at the last 5 non-blank rows.  As long as the
        phrases have scrolled out of that tail by the time the
        fallback fires, we stay idle."""
        header = (
            b'\x1b[?25h\x1b[H\x1b[2J'
            b'Enter to select triggers that.\r\n'
            b'Later: Esc to cancel aborts.\r\n'
        )
        filler = b''.join(
            f'Line of plain body content {i}.\r\n'.encode()
            for i in range(30)
        )
        pty.tracker.on_send()
        pty.feed_output(header + filler + b'> ')
        _advance(pty, 6.0)
        assert pty.get_state() == 'idle'

    def test_real_dialog_still_detected(
        self, pty: PTYFixture,
    ) -> None:
        """Baseline: when the phrases are adjacent and in the screen
        tail (where dialogs actually live), we DO enter
        needs_permission via the cursor+silence path."""
        pty.tracker.on_send()
        pty.feed_output(
            b'\x1b[?25h'
            b'Allow tool call?\r\n'
            b'1. Yes\r\n'
            b'2. No\r\n'
            b'Enter to select  Esc to cancel\r\n',
        )
        _advance(pty, 6.0)
        assert pty.get_state() == 'needs_permission'

    def test_numbered_menu_alone_detected(
        self, pty: PTYFixture,
    ) -> None:
        """The Ink TUI numbered-menu cursor ``❯ 1.`` is a strong
        dialog indicator on its own."""
        pty.tracker.on_send()
        pty.feed_output(
            b'\xe2\x9d\xaf 1. Yes\r\n'
            b'  2. No\r\n',
        )
        _advance(pty, 6.0)
        assert pty.get_state() == 'needs_permission'

    def test_numbered_list_without_cursor_is_not_a_dialog(
        self, pty: PTYFixture,
    ) -> None:
        """A plain numbered list in response text (no cursor) is not
        a dialog."""
        pty.tracker.on_send()
        pty.feed_output(
            b'\x1b[?25h'
            b'Here are your options:\r\n'
            b'1. Option A\r\n'
            b'2. Option B\r\n'
            b'3. Option C\r\n'
            b'> ',
        )
        _advance(pty, 6.0)
        assert pty.get_state() == 'idle'

    def test_code_block_with_keywords_stays_idle(
        self, pty: PTYFixture,
    ) -> None:
        """A fenced code block mentioning 'Enter' / 'Esc' in docstrings
        must not false-trip the detector."""
        pty.tracker.on_send()
        pty.feed_output(
            b'\x1b[?25h'
            b'```python\r\n'
            b'def prompt():\r\n'
            b'    """Press Enter to select; Esc to abort."""\r\n'
            b'```\r\n'
            + b'\r\n' * 10
            + b'> ',
        )
        _advance(pty, 6.0)
        assert pty.get_state() != 'needs_permission'


class TestCursorSilenceNeedsCursor:
    """The running→idle/needs_permission paths only run with cursor
    *visible*.  Output with cursor hidden (still processing) must not
    trigger either."""

    def test_hidden_cursor_blocks_fallback(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(
            b'\x1b[?25l'
            b'Allow tool?\r\n'
            b'1. Yes\r\n'
            b'Enter to select  Esc to cancel\r\n',
        )
        _advance(pty, 6.0)
        assert pty.get_state() == 'running'
