"""Tests for ``ClaudeProvider.select_option`` send protocol.

The byte sequence sent to the PTY for a regular numbered option must
keep the digit and the trailing ``\\r`` close enough together that
Claude's Ink permission menu absorbs both bytes — without an
output-settle wait between them, which would otherwise let the menu
auto-confirm on the digit and dismiss BEFORE the CR arrived,
allowing the CR to land in the now-active composer and submit any
typed-but-unsubmitted text.

Manual digit + Enter from the keyboard arrives ~10–30 ms apart and
is observed not to leak; we mimic that by typing digit char-by-char
with 20 ms gaps and the CR immediately after — explicitly avoiding
``pty_sendline`` (which writes the digit, runs an output-settle wait
of 50–200 ms, then writes CR).
"""

from unittest.mock import MagicMock

import pytest

from leap.cli_providers.claude import ClaudeProvider


class TestSelectOption:
    def test_single_digit_uses_pty_send_not_sendline(self) -> None:
        provider = ClaudeProvider()
        pty_send = MagicMock()
        pty_sendline = MagicMock()
        result = provider.select_option(
            1, {1: "Yes"}, pty_send, pty_sendline,
        )
        assert result == {'status': 'sent'}
        pty_sendline.assert_not_called()
        sent = [call.args[0] for call in pty_send.call_args_list]
        assert '1' in sent
        assert '\r' in sent

    def test_single_digit_order(self) -> None:
        provider = ClaudeProvider()
        sends: list[str] = []
        pty_send = MagicMock(side_effect=lambda d: sends.append(d))
        pty_sendline = MagicMock()
        provider.select_option(2, {1: "Yes", 2: "No"}, pty_send, pty_sendline)
        assert sends.index('2') < sends.index('\r')

    def test_multi_digit_each_char_sent_individually(self) -> None:
        provider = ClaudeProvider()
        sends: list[str] = []
        pty_send = MagicMock(side_effect=lambda d: sends.append(d))
        pty_sendline = MagicMock()
        options = {n: f"Option {n}" for n in range(1, 11)}
        provider.select_option(10, options, pty_send, pty_sendline)
        assert sends == ['1', '0', '\r']
        pty_sendline.assert_not_called()

    def test_invalid_option_returns_error(self) -> None:
        provider = ClaudeProvider()
        pty_send = MagicMock()
        pty_sendline = MagicMock()
        result = provider.select_option(
            99, {1: "Yes"}, pty_send, pty_sendline,
        )
        assert result['status'] == 'error'
        pty_send.assert_not_called()
        pty_sendline.assert_not_called()

    def test_type_something_returns_error(self) -> None:
        provider = ClaudeProvider()
        pty_send = MagicMock()
        pty_sendline = MagicMock()
        result = provider.select_option(
            4, {4: "Type something else"}, pty_send, pty_sendline,
        )
        assert result['status'] == 'error'
        assert 'type your answer' in result['error']
        pty_send.assert_not_called()
        pty_sendline.assert_not_called()
