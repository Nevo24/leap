"""Tests for ^^ capture mode input handling.

Covers:
1. Chars always forwarded to CLI (user can type in all states)
2. ^^ capture swallows input
3. Ctrl+U sent IMMEDIATELY in Enter handler to clear stale text
4. Output suppression scoped to sendline
5. Exception safety, cancel/re-enter
"""

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from leap.cli_providers.states import CLIState
from leap.cli_providers.claude import ClaudeProvider
from leap.server.server import LeapServer


def _paste_ph(content: str) -> str:
    """Compute expected placeholder for given paste content."""
    digest = hashlib.md5(content.encode('utf-8')).hexdigest()[:8]
    return f'[Paste #{digest}]'


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
    srv._paste_accumulator = None
    srv._paste_buf_snapshot_len = 0
    srv._paste_chars_snapshot = 0
    srv._paste_text_map = {}
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
    """Stale CLI input cleared in Enter handler via End + Ctrl+U + N backspaces."""

    def test_running_sends_full_clear_sequence(self):
        """During RUNNING: End + Ctrl+U + End + backspaces sequence."""
        srv = make_server(CLIState.RUNNING)
        srv._input_filter_impl(b'hello')
        srv._input_filter_impl(b'^^')
        srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        # End must be sent at least twice (belt-and-suspenders).
        assert calls.count('\x1b[F') >= 2, "End escape must be sent twice"
        assert '\x15' in calls, "Ctrl+U must be sent"
        assert '\x7f' * 5 in calls, "N backspaces must be sent as fallback"

    def test_idle_sends_end_then_ctrlu(self):
        """During IDLE, End then Ctrl+U (separate writes)."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._input_filter_impl(b'hello')
        with patch('termios.tcflush'):
            srv._input_filter_impl(b'^^')
        srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        assert '\x1b[F' in calls, "End escape must be sent"
        assert '\x15' in calls, "Ctrl+U must be sent"

    def test_idle_sends_ctrlu(self):
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._input_filter_impl(b'hello')
        with patch('termios.tcflush'):
            srv._input_filter_impl(b'^^')
        srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        assert '\x1b[F' in calls and '\x15' in calls

    def test_no_stale_no_clear(self):
        """^^hello (no pre-typing) → no clear sequence needed."""
        srv = make_server(CLIState.RUNNING)
        srv._input_filter_impl(b'^^hello\r')
        srv.pty.send.assert_not_called()


class TestScenarioX:
    """Type during RUNNING → ^^ → Enter: stale text cleared robustly."""

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

        # Enter → clear sequence sent, message queued
        srv._input_filter_impl(b'\r')
        assert srv.queue.add.called
        assert srv.queue.add.call_args[0][0] == 'hello'
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        # Full clear sequence: Ctrl+E + Ctrl+U + N backspaces fallback.
        assert '\x1b[F' in calls, "End escape must be sent"
        assert '\x15' in calls, "Ctrl+U must be sent"
        assert '\x7f' * 5 in calls, "N backspaces fallback must be sent"


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
        assert '\x1b[F' in calls and '\x15' in calls


class TestPasteCollapse:
    """Large bracketed pastes collapse to [Paste #N] in _terminal_input_buf."""

    _BP_START = b'\x1b[200~'
    _BP_END = b'\x1b[201~'

    def test_multiline_paste_collapses_to_placeholder(self):
        srv = make_server(CLIState.IDLE)
        content = b'line1\nline2\nline3'
        srv._input_filter_impl(self._BP_START + content + self._BP_END)
        ph = _paste_ph('line1\nline2\nline3')
        # Buf has a hash-based placeholder, not raw content.
        assert srv._terminal_input_buf == ph.encode('utf-8')
        assert srv._paste_text_map[ph] == 'line1\nline2\nline3'
        # Counter tracks the collapsed token as 1 visual char.
        assert srv._chars_sent_to_cli == 1

    def test_short_paste_stays_raw(self):
        srv = make_server(CLIState.IDLE)
        content = b'short url'
        srv._input_filter_impl(self._BP_START + content + self._BP_END)
        assert srv._terminal_input_buf == content
        assert srv._paste_text_map == {}

    def test_cr_inside_paste_does_not_trigger_enter(self):
        srv = make_server(CLIState.IDLE)
        # Windows-style line endings inside paste must not submit.
        content = b'line1\r\nline2'
        srv._input_filter_impl(self._BP_START + content + self._BP_END)
        ph = _paste_ph('line1\r\nline2')
        # Placeholder in buf (substantial due to \r\n).
        assert srv._terminal_input_buf == ph.encode('utf-8')
        assert srv._paste_text_map[ph] == 'line1\r\nline2'
        # queue.track_sent must NOT have been called (no spurious Enter).
        srv.queue.track_sent.assert_not_called()

    def test_capture_after_paste_sees_placeholder(self):
        srv = make_server(CLIState.RUNNING)
        content = b'line1\nline2\nline3\nline4'
        srv._input_filter_impl(self._BP_START + content + self._BP_END)
        srv._input_filter_impl(b'^^')
        ph = _paste_ph('line1\nline2\nline3\nline4')
        # Capture buf is pre-populated with the placeholder, not raw.
        assert srv._queue_capture_buf == ph.encode('utf-8')

    def test_same_content_produces_same_hash(self):
        """Pasting the same content twice dedupes to the same placeholder."""
        srv = make_server(CLIState.IDLE)
        content = b'same\nmultiline\ncontent'
        srv._input_filter_impl(self._BP_START + content + self._BP_END)
        ph1 = _paste_ph('same\nmultiline\ncontent')
        # Second paste of the same content → same placeholder, one map entry.
        srv._input_filter_impl(self._BP_START + content + self._BP_END)
        # Buf ends up with both placeholder tokens (same hash).
        assert srv._terminal_input_buf == (ph1 + ph1).encode('utf-8')
        # Still only one map entry because the hash is the same.
        assert list(srv._paste_text_map.keys()) == [ph1]

    def test_queue_resolves_paste_to_raw(self):
        srv = make_server(CLIState.RUNNING)
        content = b'line1\nline2'
        srv._input_filter_impl(self._BP_START + content + self._BP_END)
        srv._input_filter_impl(b'^^')
        srv._input_filter_impl(b'\r')
        # Queued message is the raw paste content, not the placeholder.
        assert srv.queue.add.called
        assert srv.queue.add.call_args[0][0] == 'line1\nline2'

    def test_save_resolves_paste_to_raw(self):
        """^^-save on a captured paste writes raw text to history."""
        srv = make_server(CLIState.RUNNING)
        content = b'line1\nline2\nline3'
        srv._input_filter_impl(self._BP_START + content + self._BP_END)
        srv._input_filter_impl(b'^^')
        # Simulate ^^-save by calling directly (bypass complex ^^^^ path).
        srv._persist_saved_messages = MagicMock()
        srv._save_capture_message()
        assert srv._saved_messages == ['line1\nline2\nline3']

    def test_recall_collapses_multiline_to_placeholder(self):
        """Recalled multi-line saved messages show a [Paste #<hash>] token."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._saved_messages = ['line1\nline2\nline3']
        srv._queue_capture_mode = True
        with patch.object(srv, '_capture_display'):
            srv._browse_saved_history(-1)
        ph = _paste_ph('line1\nline2\nline3')
        assert srv._queue_capture_buf == ph.encode('utf-8')
        assert srv._paste_text_map[ph] == 'line1\nline2\nline3'

    def test_recall_same_msg_twice_reuses_placeholder(self):
        """Recalling the same saved msg twice keeps the same [Paste #<hash>]."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._saved_messages = ['same\ncontent\nhere']
        srv._queue_capture_mode = True
        with patch.object(srv, '_capture_display'):
            srv._browse_saved_history(-1)
            ph_first = srv._queue_capture_buf.decode()
            # Simulate browsing away and back.
            srv._saved_msg_index = -1
            srv._queue_capture_buf.clear()
            srv._browse_saved_history(-1)
            ph_second = srv._queue_capture_buf.decode()
        assert ph_first == ph_second  # stable hash, no counter drift

    def test_recall_short_msg_stays_raw(self):
        """Short single-line saved messages are not collapsed."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._saved_messages = ['hello world']
        srv._queue_capture_mode = True
        with patch.object(srv, '_capture_display'):
            srv._browse_saved_history(-1)
        assert srv._queue_capture_buf == b'hello world'
        assert srv._paste_text_map == {}


class TestPasteCancelWithTypedText:
    """Paste → ^^ → type 'hello' → Esc: the typed 'hello' must reach the CLI."""

    _BP_START = b'\x1b[200~'
    _BP_END = b'\x1b[201~'

    def _run_flow(self, state):
        import time
        srv = make_server(state)
        srv.pty.process.child_fd = 999
        content = b'line1\nline2\nline3\nline4\nline5'
        with patch('termios.tcflush'):
            srv._input_filter_impl(self._BP_START + content + self._BP_END)
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'hello')
            srv._input_filter_impl(b'\x1b')  # Esc → cancel
            time.sleep(0.4)  # wait for cancel thread
        calls = []
        for c in srv.pty.send.call_args_list:
            if c[0]:
                arg = c[0][0]
                if isinstance(arg, bytes):
                    arg = arg.decode('utf-8', errors='replace')
                calls.append(arg)
        return calls

    def test_cancel_idle_preserves_typed_text(self):
        """Fast path: append-only edit types just the suffix, no re-paste."""
        calls = self._run_flow(CLIState.IDLE)
        joined = ''.join(calls)
        # Claude already shows the original paste — just type 'hello'.
        assert 'hello' in joined, 'typed text must be sent'
        # No clear or re-paste needed (Claude still has the original paste).
        assert '\x1b[200~' not in joined, 'fast path: no bracketed paste'

    def test_cancel_running_preserves_typed_text(self):
        """Regression: during RUNNING, cancel used to silently drop typed text."""
        calls = self._run_flow(CLIState.RUNNING)
        joined = ''.join(calls)
        assert 'hello' in joined, 'typed text must be sent even during RUNNING'
        assert '\x1b[200~' not in joined, 'fast path: no bracketed paste'

    def test_cancel_with_paste_inside_capture_preserves_original(self):
        """Regression: paste A → ^^ → paste B → Esc must not clobber A.

        Previously this went through the slow clear+re-paste path,
        which under RUNNING streaming could drop the bracketed-paste
        start marker for A and turn A's \\n bytes into submit-Enters —
        A vanished, only B's flattened chars survived. The fast path
        now wraps only the suffix in bracketed paste markers and
        leaves Claude's existing attachment for A untouched.
        """
        import time
        srv = make_server(CLIState.RUNNING)
        srv.pty.process.child_fd = 999
        content_a = b'aaa\naaa\naaa'
        content_b = b'bbb\nbbb\nbbb'
        with patch('termios.tcflush'):
            srv._input_filter_impl(self._BP_START + content_a + self._BP_END)
            srv._input_filter_impl(b'^^')
            # Paste B inside capture.
            srv._input_filter_impl(self._BP_START + content_b + self._BP_END)
            srv._input_filter_impl(b'\x1b')  # Esc
            time.sleep(0.5)
        calls = []
        for c in srv.pty.send.call_args_list:
            if c[0]:
                arg = c[0][0]
                if isinstance(arg, bytes):
                    arg = arg.decode('utf-8', errors='replace')
                calls.append(arg)
        joined = ''.join(calls)
        # No clear sent → no End + Ctrl+U + backspaces sequence.
        assert '\x15' not in joined, 'fast path must not clear the CLI'
        # Only B wrapped — A stays on Claude's CLI.
        assert '\x1b[200~bbb\nbbb\nbbb\x1b[201~' in joined
        assert content_a.decode() not in joined, (
            "A's content must not be re-sent (Claude still has it)"
        )

    def test_cancel_with_prepended_text_falls_back_to_slow_path(self):
        """Slow path: prepending before placeholder needs full round-trip."""
        import time
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        content = b'line1\nline2\nline3'
        with patch('termios.tcflush'):
            srv._input_filter_impl(self._BP_START + content + self._BP_END)
            srv._input_filter_impl(b'^^')
            # Move cursor to start, then type 'pre ' (prepended text).
            srv._input_filter_impl(b'\x1b[H')  # Home
            srv._input_filter_impl(b'pre ')
            srv._input_filter_impl(b'\x1b')  # Esc
            time.sleep(0.5)
        calls = []
        for c in srv.pty.send.call_args_list:
            if c[0]:
                arg = c[0][0]
                if isinstance(arg, bytes):
                    arg = arg.decode('utf-8', errors='replace')
                calls.append(arg)
        joined = ''.join(calls)
        # Slow path runs bracketed paste for the placeholder.
        assert '\x1b[200~line1\nline2\nline3\x1b[201~' in joined
        assert 'pre ' in joined


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
