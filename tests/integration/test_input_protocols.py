"""Input byte protocols: what users' terminals actually send.

``on_input`` parses terminal-protocol byte streams (not just plain
ASCII).  iTerm2 / kitty / WezTerm send Ctrl+C and Escape via CSI u,
legacy xterms send them via the 27;mod;key tilde form, and various
passive events (focus-in/out, mouse) must be filtered out entirely
so they don't spuriously set flags or transition state.
"""

import pytest

from tests.conftest import PTYFixture


class TestCSIUProtocol:
    """Kitty / CSI u keyboard protocol — used by iTerm2, WezTerm."""

    def test_csi_u_ctrl_c(self, pty: PTYFixture) -> None:
        """\\x1b[3;5u = Ctrl+C in CSI u (codepoint 3 = raw ETX)."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b[3;5u')
        assert pty.tracker._interrupt_pending is True

    def test_csi_u_escape_standalone(self, pty: PTYFixture) -> None:
        """\\x1b[27;1u = standalone Escape."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b[27;1u')
        assert pty.tracker._interrupt_pending is True

    def test_csi_u_ctrl_c_via_keycode_99(self, pty: PTYFixture) -> None:
        """\\x1b[99;5u = 'c' + Ctrl modifier."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b[99;5u')
        assert pty.tracker._interrupt_pending is True

    def test_csi_u_plain_c_is_not_ctrl_c(self, pty: PTYFixture) -> None:
        """\\x1b[99;1u = plain 'c' without Ctrl modifier."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b[99;1u')
        assert pty.tracker._interrupt_pending is False


class TestLegacyXtermProtocol:
    """Older xterm format: \\x1b[27;<mod>;<keycode>~ for modifiers."""

    def test_legacy_ctrl_c(self, pty: PTYFixture) -> None:
        """\\x1b[27;5;99~ = Ctrl+C in legacy xterm."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b[27;5;99~')
        assert pty.tracker._interrupt_pending is True

    def test_legacy_escape(self, pty: PTYFixture) -> None:
        """\\x1b[27;1;27~ = standalone Escape (legacy)."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b[27;1;27~')
        assert pty.tracker._interrupt_pending is True

    def test_legacy_non_interrupt_tilde_is_filtered(
        self, pty: PTYFixture,
    ) -> None:
        """\\x1b[27;1;65~ = plain 'A' via legacy — not an interrupt."""
        pty.tracker.on_send()
        pty.tracker.on_input(b'\x1b[27;1;65~')
        assert pty.tracker._interrupt_pending is False


class TestTerminalEventsFiltered:
    """Terminal-generated events must not update any flags."""

    @pytest.mark.parametrize('seq', [
        b'\x1b[I',        # focus in
        b'\x1b[O',        # focus out
        b'\x1b[<0;5;5M',  # SGR mouse click
        b'\x1b[<0;5;5m',  # SGR mouse release
    ])
    def test_filtered_events_do_not_set_flags(
        self, pty: PTYFixture, seq: bytes,
    ) -> None:
        before = (
            pty.tracker._seen_user_input,
            pty.tracker._interrupt_pending,
            pty.tracker._user_input_since_idle,
        )
        pty.tracker.on_input(seq)
        after = (
            pty.tracker._seen_user_input,
            pty.tracker._interrupt_pending,
            pty.tracker._user_input_since_idle,
        )
        assert before == after


class TestBracketedPaste:
    """Bracketed paste sequences (\\x1b[200~ ... \\x1b[201~) wrap real
    content.  The content inside counts as real input — the wrappers
    themselves don't need special treatment but must not flip flags
    by themselves."""

    def test_pasted_content_counts_as_input(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_input(b'\x1b[200~hello world\x1b[201~')
        assert pty.tracker._seen_user_input is True


class TestUnicodeInput:
    def test_multibyte_utf8_sets_seen_user_input(
        self, pty: PTYFixture,
    ) -> None:
        """Emoji / CJK / accents — arrive as multi-byte UTF-8."""
        pty.tracker.on_input('שלום 🌍 café'.encode('utf-8'))
        assert pty.tracker._seen_user_input is True
        assert pty.tracker._user_input_since_idle is True

    def test_null_byte_alone_is_ignored(
        self, pty: PTYFixture,
    ) -> None:
        """\\x00 is terminal noise, not user input."""
        pty.tracker.on_input(b'\x00')
        assert pty.tracker._seen_user_input is False


class TestLongInputBursts:
    def test_paste_of_multi_line_text_does_not_trigger_running(
        self, pty: PTYFixture,
    ) -> None:
        """A paste of multiple lines at idle must not false-fire
        idle→running — Enter handler only triggers on an actual
        CR byte at the end (which Claude will receive via the client,
        not via paste)."""
        payload = b'line1\r\nline2\r\nline3'
        pty.tracker.on_input(payload)
        # has_enter is True (CR present) so state DID transition.
        assert pty.tracker.current_state == 'running'

    def test_paste_without_enter_stays_idle(
        self, pty: PTYFixture,
    ) -> None:
        pty.tracker.on_input(b'just typing, no newline')
        assert pty.tracker.current_state == 'idle'


class TestInputInWaitingStates:
    def test_any_printable_in_needs_permission_sets_user_responded(
        self, pty: PTYFixture,
    ) -> None:
        from tests.integration.test_waiting_wedge import _DIALOG_BYTES

        pty.tracker.on_send()
        pty.feed_output(_DIALOG_BYTES)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.tracker.on_input(b'1')
        assert pty.tracker._user_responded is True

    def test_escape_in_waiting_sets_user_responded(
        self, pty: PTYFixture,
    ) -> None:
        from tests.integration.test_waiting_wedge import _DIALOG_BYTES

        pty.tracker.on_send()
        pty.feed_output(_DIALOG_BYTES)
        pty.write_signal('needs_permission')
        assert pty.get_state() == 'needs_permission'

        pty.tracker.on_input(b'\x1b')
        assert pty.tracker._user_responded is True
