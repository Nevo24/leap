"""Output-side robustness.

Pathological byte streams must not crash the tracker or wedge state:
invalid UTF-8, malformed ANSI escape sequences, very large chunks,
null bytes, CLI processes that output nothing but control codes.
"""

import time

from tests.conftest import PTYFixture


class TestInvalidUTF8:
    def test_lone_continuation_byte_does_not_crash(
        self, pty: PTYFixture,
    ) -> None:
        """A stray UTF-8 continuation byte (0x80-0xBF with no starter)
        must be tolerated — pyte.feed decodes with errors='replace'."""
        pty.tracker.on_send()
        pty.feed_output(b'prefix\x80\x81\x82suffix')
        assert pty.get_state() == 'running'

    def test_partial_multibyte_across_chunks(
        self, pty: PTYFixture,
    ) -> None:
        """A UTF-8 sequence split across two on_output calls — the
        second chunk's continuation byte mustn't crash."""
        pty.tracker.on_send()
        pty.feed_output(b'\xe2\x9c')  # starter for ✻ (U+273B) missing tail
        pty.feed_output(b'\xbb rest')
        assert pty.get_state() == 'running'

    def test_invalid_utf8_does_not_change_state(
        self, pty: PTYFixture,
    ) -> None:
        before = pty.tracker.current_state
        pty.feed_output(b'\xff\xff\xff\xff')
        assert pty.tracker.current_state == before


class TestMalformedANSI:
    def test_unterminated_csi_recovers(
        self, pty: PTYFixture,
    ) -> None:
        """Unterminated CSI sequence followed by normal output — pyte
        may reset the stream but we must not wedge."""
        pty.tracker.on_send()
        # CSI with no final byte, then clean output.
        pty.feed_output(b'\x1b[31m\x1b[')
        pty.feed_output(b'normal text')
        assert pty.get_state() == 'running'

    def test_osc_without_terminator_recovers(
        self, pty: PTYFixture,
    ) -> None:
        """OSC sequence (\\x1b]...\\x07) missing terminator."""
        pty.tracker.on_send()
        pty.feed_output(b'\x1b]0;no terminator')
        assert pty.get_state() == 'running'

    def test_nested_escape_sequences(
        self, pty: PTYFixture,
    ) -> None:
        """Escape mid-escape — adversarial but seen in the wild."""
        pty.tracker.on_send()
        pty.feed_output(b'\x1b[\x1b[31mred')
        assert pty.get_state() == 'running'


class TestLargeOutput:
    def test_huge_chunk_is_handled(
        self, pty: PTYFixture,
    ) -> None:
        """A ~64 KB chunk of plain text must not crash or stall."""
        pty.tracker.on_send()
        pty.feed_output(b'A' * 65536)
        assert pty.get_state() == 'running'

    def test_many_small_chunks(
        self, pty: PTYFixture,
    ) -> None:
        """Many 1-byte chunks (worst case for pyte overhead)."""
        pty.tracker.on_send()
        for ch in b'streaming output byte by byte' * 20:
            pty.feed_output(bytes([ch]))
        assert pty.get_state() == 'running'


class TestControlBytes:
    def test_null_bytes_in_output(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(b'hello\x00world\x00')
        assert pty.get_state() == 'running'

    def test_bell_character(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(b'alert\x07done')
        assert pty.get_state() == 'running'

    def test_backspace_in_output(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.feed_output(b'typo\x08\x08ext')
        assert pty.get_state() == 'running'


class TestClearScreenVariants:
    def test_clear_via_erase_display(
        self, pty: PTYFixture,
    ) -> None:
        """\\x1b[2J clears the visible screen; state must persist."""
        pty.tracker.on_send()
        pty.feed_output(b'content\x1b[2Jafter')
        assert pty.get_state() == 'running'

    def test_clear_and_home(
        self, pty: PTYFixture,
    ) -> None:
        """The common ESC[H ESC[2J clear-and-home pair must not affect
        state."""
        pty.tracker.on_send()
        pty.feed_output(b'\x1b[H\x1b[2J')
        assert pty.get_state() == 'running'


class TestScreenIntegrityAfterFailures:
    def test_state_unchanged_after_pyte_crashes(
        self, pty: PTYFixture,
    ) -> None:
        """Hand pyte a known-bad sequence that has caused IndexError
        in the past (wide-char in constrained buffer).  State should
        not regress."""
        pty.tracker.on_send()
        pty.feed_output(b'\xef\xbf\xbd' * 100)  # U+FFFD replacement chars
        assert pty.get_state() == 'running'

    def test_screen_reset_does_not_lose_running_state(
        self, pty: PTYFixture,
    ) -> None:
        """After pyte resets its screen (internal error), state stays
        running if nothing else changed."""
        pty.tracker.on_send()
        with pty.tracker._screen_lock:
            pty.tracker._reset_screen()
        assert pty.tracker.current_state == 'running'


class TestRealPTYPrintfEdgeCases:
    def test_real_ansi_color_output(
        self, pty: PTYFixture,
    ) -> None:
        """Real bash printf with ANSI colors — sanity check."""
        pty.tracker.on_send()
        pty.send_line(r'printf "\033[32mgreen\033[0m output"')
        time.sleep(0.2)
        pty.drain_to_tracker(timeout=0.5)
        assert pty.get_state() == 'running'

    def test_real_cursor_move_output(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_send()
        pty.send_line(r'printf "\033[10;20Hpositioned"')
        time.sleep(0.2)
        pty.drain_to_tracker(timeout=0.5)
        assert pty.get_state() == 'running'
