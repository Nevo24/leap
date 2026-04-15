"""Tests for ^^ capture mode input handling.

Covers:
1. Chars always forwarded to CLI (user can type in all states)
2. ^^ capture swallows input
3. Ctrl+U sent IMMEDIATELY in Enter handler to clear stale text
4. Output suppression scoped to sendline
5. Exception safety, cancel/re-enter
"""

from unittest.mock import MagicMock, patch

import pytest

from leap.cli_providers.states import CLIState
from leap.cli_providers.claude import ClaudeProvider
from leap.server.server import LeapServer


def make_server(state: str = CLIState.RUNNING) -> LeapServer:
    srv = object.__new__(LeapServer)
    srv.state = MagicMock()
    srv.state.current_state = state
    srv.state._state = state
    srv.state.on_input = MagicMock()
    srv.state.on_resize = MagicMock()
    srv.pty = MagicMock()
    srv.pty.process = MagicMock()
    srv.pty.process.child_fd = -1
    srv.queue = MagicMock()
    srv.queue.size = 0
    srv.queue.track_sent = MagicMock()
    srv._provider = ClaudeProvider()
    srv.output_capture = MagicMock()
    for a in ['_queue_capture_mode', '_capture_stale_caret',
              '_capture_cancel_pending', '_capture_show_hint',
              '_pending_caret', '_pending_caret_flush',
              '_in_bracketed_paste', '_user_has_typed',
              '_pending_resize', '_capture_show_saved_hint',
              '_stale_text_pending', '_queue_sending']:
        setattr(srv, a, False)
    for a in ['_capture_stale_char_count', '_capture_cursor_pos',
              '_capture_prev_lines', '_capture_image_counter',
              '_chars_sent_to_cli', '_capture_pre_chars_sent',
              '_preserved_chars_sent']:
        setattr(srv, a, 0)
    for a in ['_queue_capture_buf', '_capture_pre_input_buf',
              '_capture_utf8_buf', '_terminal_input_buf',
              '_preserved_input_buf', '_queue_sending_held']:
        setattr(srv, a, bytearray())
    srv._capture_image_map = {}
    srv._pending_paste_images = []  # list[tuple[int, str]]
    srv._capture_initial_text = ""
    srv._partial_escape = None
    srv._pending_caret_time = 0.0
    srv._pending_caret_timer = None
    srv._last_output_time = 0.0
    srv._suppress_send_until = 0.0
    srv._saved_messages = []
    srv._saved_msg_index = -1
    srv._prev_filter_state = None
    srv._send_clear_queue = []
    srv.running = True
    return srv


class TestNormalTyping:
    """Chars always go to CLI — user can type in ALL states."""

    def test_idle(self):
        srv = make_server(CLIState.IDLE)
        out = srv._input_filter_impl(b'hello')
        assert out == b'hello'

    def test_running(self):
        srv = make_server(CLIState.RUNNING)
        out = srv._input_filter_impl(b'hello')
        assert out == b'hello'

    def test_ctrlc(self):
        srv = make_server(CLIState.RUNNING)
        out = srv._input_filter_impl(b'\x03')
        assert b'\x03' in out

    def test_enter(self):
        srv = make_server(CLIState.RUNNING)
        out = srv._input_filter_impl(b'\r')
        assert b'\r' in out


class TestCaptureSwallows:

    def test_capture_running(self):
        srv = make_server(CLIState.RUNNING)
        out = srv._input_filter_impl(b'^^hello\r')
        assert b'hello' not in out
        assert srv.queue.add.called

    def test_capture_idle(self):
        srv = make_server(CLIState.IDLE)
        with patch('termios.tcflush'):
            out = srv._input_filter_impl(b'^^hello\r')
        assert b'hello' not in out


class TestStaleCleanup:
    """Ctrl+U sent IMMEDIATELY in Enter handler — no waiting for IDLE."""

    def test_running_sends_ctrlu(self):
        """During RUNNING, Ctrl+U only (no Ctrl+E)."""
        srv = make_server(CLIState.RUNNING)
        srv._input_filter_impl(b'hello')
        srv._input_filter_impl(b'^^')
        srv._input_filter_impl(b'\r')
        srv.pty.send.assert_called_with('\x15')

    def test_idle_sends_ctrle_then_ctrlu(self):
        """During IDLE, Ctrl+E then Ctrl+U (separate writes)."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._input_filter_impl(b'hello')
        with patch('termios.tcflush'):
            srv._input_filter_impl(b'^^')
        srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        assert '\x05' in calls, "Ctrl+E must be sent"
        assert '\x15' in calls, "Ctrl+U must be sent"

    def test_idle_sends_ctrlu(self):
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._input_filter_impl(b'hello')
        with patch('termios.tcflush'):
            srv._input_filter_impl(b'^^')
        srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        assert '\x05' in calls and '\x15' in calls

    def test_no_stale_no_ctrlu(self):
        """^^hello (no pre-typing) → no Ctrl+U needed."""
        srv = make_server(CLIState.RUNNING)
        srv._input_filter_impl(b'^^hello\r')
        srv.pty.send.assert_not_called()


class TestScenarioX:
    """Type during RUNNING → ^^ → Enter: stale text cleared by Ctrl+U."""

    def test_full_flow(self):
        srv = make_server(CLIState.RUNNING)
        # User types "hello" (goes to CLI — visible)
        out1 = srv._input_filter_impl(b'hello')
        assert out1 == b'hello'
        assert srv._chars_sent_to_cli == 5

        # ^^ enters capture
        srv._input_filter_impl(b'^^')
        assert srv._queue_capture_mode
        assert srv._capture_stale_char_count == 5

        # Enter → Ctrl+U clears stale, queues message
        srv._input_filter_impl(b'\r')
        assert srv.queue.add.called
        assert srv.queue.add.call_args[0][0] == 'hello'
        # Ctrl+U was sent to clear stale text (RUNNING → no Ctrl+E)
        srv.pty.send.assert_called_with('\x15')


class TestCancelReenter:

    def test_idle_cancel_reenter(self):
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._input_filter_impl(b'hello')
        with patch('termios.tcflush'):
            srv._input_filter_impl(b'^^')
        assert srv._capture_stale_char_count == 5

        # Escape cancel — restores chars_sent
        srv._input_filter_impl(b'\x1b')
        assert srv._chars_sent_to_cli == 5

        # Re-enter ^^
        with patch('termios.tcflush'):
            srv._input_filter_impl(b'^^')
        assert srv._capture_stale_char_count == 5

        # Enter → Ctrl+E + Ctrl+U
        srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        assert '\x05' in calls and '\x15' in calls


class TestExceptionSafety:

    def test_capture_returns_empty(self):
        srv = make_server(CLIState.RUNNING)
        srv._queue_capture_mode = True
        srv.state.current_state = None
        assert srv._input_filter(b'hello') == b''

    def test_normal_returns_data(self):
        srv = make_server(CLIState.IDLE)
        srv._queue_capture_mode = False
        srv.state.current_state = None
        assert srv._input_filter(b'hello') == b'hello'
