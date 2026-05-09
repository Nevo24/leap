"""Tests for ^^ capture mode input handling.

Covers:
1. Chars always forwarded to CLI (user can type in all states)
2. ^^ capture swallows input
3. Ctrl+U sent IMMEDIATELY in Enter handler to clear stale text
4. Output suppression scoped to sendline
5. Exception safety, cancel/re-enter
"""

import hashlib
import os
import threading
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
    srv.state._seen_user_input = False
    srv.state._query_in_flight = False
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
              '_queue_sending', '_capture_force_confirm',
              '_pending_sigwinch', '_capture_force_dispatch']:
        setattr(srv, a, False)
    for a in ['_capture_stale_visual_rows', '_capture_stale_logical_lines',
              '_capture_cursor_pos', '_capture_prev_lines',
              '_capture_image_counter', '_chars_sent_to_cli',
              '_capture_pre_chars_sent', '_preserved_chars_sent']:
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
    srv._paste_cursor_snapshot = 0
    srv._paste_chars_snapshot = 0
    srv._paste_text_map = {}
    srv._terminal_input_cursor = 0
    srv._capture_pre_input_cursor = 0
    srv._pending_caret_timer = None
    srv._last_output_time = 0.0
    srv._suppress_send_until = 0.0
    srv._saved_messages = []
    srv._saved_msg_index = -1
    srv._prev_filter_state = None
    srv._send_clear_queue = []
    srv._dispatch_wake = threading.Event()
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

    def test_capture_kitty_keyboard_release_between_carets(self):
        # Codex/Ratatui enables kitty keyboard protocol with key-release
        # reporting, so each "^" press is followed by a CSI-u release
        # event in its own input chunk before the next press arrives.
        # The held first "^" must survive the release event and combine
        # with the second "^" press to enter capture mode.
        srv = make_server(CLIState.RUNNING)
        srv._input_filter_impl(b'^')
        assert srv._pending_caret is True
        out_release = srv._input_filter_impl(b'\x1b[54:94;2:3u')
        assert srv._pending_caret is True, \
            "release event must not flush the held ^"
        assert b'^' not in out_release, \
            "held ^ must not leak to CLI on release event"
        srv._input_filter_impl(b'^')
        assert srv._queue_capture_mode is True
        srv._input_filter_impl(b'hi\r')
        assert srv.queue.add.called
        assert srv.queue.add.call_args[0][0] == 'hi'


class TestStaleCleanup:
    """Stale CLI input cleared in Enter handler via two End + Ctrl+U
    rounds (no per-char backspace flood)."""

    def test_running_sends_full_clear_sequence(self):
        """During RUNNING: End + Ctrl+U + End + Ctrl+U (retry round)."""
        srv = make_server(CLIState.RUNNING)
        srv._input_filter_impl(b'hello')
        srv._input_filter_impl(b'^^')
        srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        # End sent at least twice (belt-and-suspenders).
        assert calls.count('\x1b[F') >= 2, "End escape must be sent twice"
        # Ctrl+U sent at least twice (drop-defense retry replaces the
        # old n-backspace fallback).  Count occurrences across all
        # send strings — the second batch is now ``'\\x15' * (rows+1)``
        # rather than a single byte.
        total_ctrlu = sum(
            c.count('\x15') for c in calls if isinstance(c, str)
        )
        assert total_ctrlu >= 2, \
            f"Ctrl+U must be sent twice, got {total_ctrlu}"
        # Crucially: NO per-char backspace flood.
        assert not any(
            isinstance(c, str) and c.startswith('\x7f') and len(c) > 1
            for c in calls
        ), "no n-backspace flood in RUNNING"

    def test_idle_sends_end_then_ctrlu(self):
        """During IDLE, End then Ctrl+Us (the Ctrl+Us are batched
        into a single ``pty.send`` call as ``'\\x15' * N``)."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._input_filter_impl(b'hello')
        with patch('termios.tcflush'):
            srv._input_filter_impl(b'^^')
        srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        assert '\x1b[F' in calls, "End escape must be sent"
        ctrlu = sum(c.count('\x15') for c in calls if isinstance(c, str))
        assert ctrlu >= 1, f"Ctrl+U must be sent, got count {ctrlu}"

    def test_idle_sends_ctrlu(self):
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._input_filter_impl(b'hello')
        with patch('termios.tcflush'):
            srv._input_filter_impl(b'^^')
        srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        assert '\x1b[F' in calls
        ctrlu = sum(c.count('\x15') for c in calls if isinstance(c, str))
        assert ctrlu >= 1

    def test_no_stale_no_clear(self):
        """^^hello (no pre-typing) → no clear sequence needed."""
        srv = make_server(CLIState.RUNNING)
        srv._input_filter_impl(b'^^hello\r')
        srv.pty.send.assert_not_called()

    def test_running_ctrlu_count_scales_with_rows(self):
        """In RUNNING, Ctrl+U is row-bound — clear sequence sends one
        Ctrl+U per visual row of typed input (plus a small safety
        margin), NOT a per-char backspace flood."""
        import os as _os
        srv = make_server(CLIState.RUNNING)
        # Use os.terminal_size so the deferred-resize thread in
        # _capture_flush can also unpack (cols, rows) without error.
        with patch('shutil.get_terminal_size',
                   return_value=_os.terminal_size((80, 24))):
            srv._input_filter_impl(b'x' * 250)  # ~4 rows at 80 cols
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        # Count total Ctrl+U bytes across all pty.send calls (the
        # row-batched send is one string of repeated '\x15').
        total_ctrlu = sum(
            c.count('\x15') for c in calls if isinstance(c, str)
        )
        # Expect at least 4 (one per row of 250 // 80 + 1 = 4 rows,
        # plus the initial single Ctrl+U → ≥ 5 in practice).
        assert total_ctrlu >= 4, \
            f"want >=4 Ctrl+U bytes for 4 rows, got {total_ctrlu}"
        # And no per-char backspace flood.
        assert not any(
            isinstance(c, str) and c.startswith('\x7f') and len(c) > 1
            for c in calls
        ), "no backspace flood for long input in RUNNING"

    def test_idle_single_logical_line_back_to_back_ctrlus(self):
        """IDLE with single-logical-line input (no ``\\n``): clear
        sends ``max(lines, rows) + 3`` Ctrl+Us.  Single line wraps
        to 4 visual rows on 80 cols → max(1, 4) + 3 = 7 Ctrl+Us.
        IDLE skips the RUNNING-style second End (drop-defense)."""
        import os as _os
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        with patch('termios.tcflush'), \
                patch('shutil.get_terminal_size',
                      return_value=_os.terminal_size((80, 24))):
            srv._input_filter_impl(b'x' * 250)  # 250 chars, no \n
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        total_ctrlu = sum(
            c.count('\x15') for c in calls if isinstance(c, str)
        )
        # 1 line + 4 wrapped rows → max(1, 4) + 3 = 7 Ctrl+Us.
        assert total_ctrlu >= 7, \
            f"want >=7 Ctrl+U bytes, got {total_ctrlu}"
        # IDLE skips the second End (drop-defense is RUNNING-only).
        assert calls.count('\x1b[F') == 1, \
            "IDLE sends End exactly once (no RUNNING-style retry)"
        assert not any(
            isinstance(c, str) and c.startswith('\x7f') and len(c) > 1
            for c in calls
        )


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
        # 5-char single-line "hello" wraps to 1 visual row on 80 cols.
        assert srv._capture_stale_visual_rows == 1

        # Enter → clear sequence sent, message queued
        srv._input_filter_impl(b'\r')
        assert srv.queue.add.called
        assert srv.queue.add.call_args[0][0] == 'hello'
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        # Full clear sequence in RUNNING: two rounds of End + Ctrl+U.
        assert calls.count('\x1b[F') >= 2, "End must be sent twice"
        # Count Ctrl+U occurrences across all sends — second batch is
        # ``'\\x15' * (rows+1)`` rather than a single byte.
        total_ctrlu = sum(
            c.count('\x15') for c in calls if isinstance(c, str)
        )
        assert total_ctrlu >= 2, \
            f"Ctrl+U must be sent twice, got {total_ctrlu}"
        # NO per-char backspace flood — same drop-defense at fixed cost.
        assert not any(
            isinstance(c, str) and c.startswith('\x7f') and len(c) > 1
            for c in calls
        )


class TestCancelReenter:

    def test_idle_cancel_reenter(self):
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._input_filter_impl(b'hello')
        with patch('termios.tcflush'):
            srv._input_filter_impl(b'^^')
        # 5-char "hello" wraps to 1 visual row on 80 cols.
        assert srv._capture_stale_visual_rows == 1

        # Escape cancel — restores chars_sent
        srv._input_filter_impl(b'\x1b')
        assert srv._chars_sent_to_cli == 5

        # Re-enter ^^
        with patch('termios.tcflush'):
            srv._input_filter_impl(b'^^')
        assert srv._capture_stale_visual_rows == 1

        # Enter → End + N Ctrl+Us
        srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        assert '\x1b[F' in calls
        ctrlu = sum(c.count('\x15') for c in calls if isinstance(c, str))
        assert ctrlu >= 1


class TestIdleSkipsBackspaceFlood:
    """In IDLE, _clear_stale_cli_input sends End + Ctrl+U only — no
    n-backspace fallback.  Ctrl+U is reliable when Ink isn't streaming,
    so the layered fallback (which floods Ink with re-renders for long
    messages) is unnecessary and skipped."""

    def test_idle_omits_backspace_fallback(self):
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._input_filter_impl(b'hello')
        with patch('termios.tcflush'):
            srv._input_filter_impl(b'^^')
        srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        assert '\x1b[F' in calls, "End must still be sent"
        ctrlu = sum(c.count('\x15') for c in calls if isinstance(c, str))
        assert ctrlu >= 1, f"Ctrl+U must still be sent, got count {ctrlu}"
        # Fast path: NO n-backspace flood, NO duplicated End.
        assert '\x7f' * 5 not in calls, \
            "n-backspace fallback must be skipped in IDLE"
        assert calls.count('\x1b[F') == 1, \
            "End sent only once in IDLE fast path (not twice like RUNNING)"

    def test_idle_skips_flood_for_long_text(self):
        """Long typed input in IDLE: still no backspace flood."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        long_text = b'x' * 500
        srv._input_filter_impl(long_text)
        with patch('termios.tcflush'):
            srv._input_filter_impl(b'^^')
        srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        # Crucially: no '\x7f' * 500 — the whole point of Lever 2.
        assert not any(
            isinstance(c, str) and c.startswith('\x7f') and len(c) > 1
            for c in calls
        ), "no backspace string sent in IDLE"


class TestClearWrapRowsAtCaptureEntry:
    """At ^^ entry, the wrap rows the CLI rendered above the cursor get
    cleared so [Leap Q] doesn't sit on top of leftover rendering."""

    def test_no_walk_up_for_short_text(self):
        """Single-row typed text: cursor_pos // cols == 0, no walk-up."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        with patch('shutil.get_terminal_size') as mock_size, \
                patch('os.write') as mock_write, \
                patch('termios.tcflush'):
            mock_size.return_value.columns = 80
            srv._input_filter_impl(b'hello')
            srv._input_filter_impl(b'^^')
        all_writes = b''.join(c[0][1] for c in mock_write.call_args_list)
        assert b'\x1b[A\r\x1b[K' not in all_writes, \
            "no walk-up for short single-row text"

    def test_walk_up_walk_down_for_wrapped_text(self):
        """Long typed text wraps across rows: walk_up + walk_down emitted
        as a contiguous payload at capture entry."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        long_text = b'x' * 250  # 250 chars on 80-col → 4 visual rows
        with patch('shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 24))), \
                patch('os.write') as mock_write, \
                patch('termios.tcflush'):
            srv._input_filter_impl(long_text)
            srv._input_filter_impl(b'^^')
        all_writes = b''.join(c[0][1] for c in mock_write.call_args_list)
        # 4 visual rows total → 3 rows above the cursor row.
        # Contiguous walk_up (3x) + walk_down: \x1b[A\r\x1b[K * 3 + \x1b[3B
        walk_seq = b'\x1b[A\r\x1b[K' * 3 + b'\x1b[3B'
        assert walk_seq in all_writes, \
            "walk_up + walk_down sequence missing at capture entry"

    def test_no_walk_up_when_no_stale_text(self):
        """^^ on empty input (no pre-typing): no walk-up emitted."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        with patch('shutil.get_terminal_size') as mock_size, \
                patch('os.write') as mock_write, \
                patch('termios.tcflush'):
            mock_size.return_value.columns = 80
            srv._input_filter_impl(b'^^')
        all_writes = b''.join(c[0][1] for c in mock_write.call_args_list)
        assert b'\x1b[A\r\x1b[K' not in all_writes, \
            "no walk-up when there was no pre-capture CLI text"


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

    def test_paste_with_control_chars_preserves_them(self):
        """Content with ^ and \\t inside a paste must round-trip intact."""
        srv = make_server(CLIState.IDLE)
        # Paste content contains '^', tab, and newline.
        content = b'foo^bar\tbaz\nline2'
        srv._input_filter_impl(self._BP_START + content + self._BP_END)
        ph = _paste_ph('foo^bar\tbaz\nline2')
        # Placeholder created, raw content in map matches exactly.
        assert srv._terminal_input_buf == ph.encode('utf-8')
        assert srv._paste_text_map[ph] == 'foo^bar\tbaz\nline2'

    def test_paste_with_double_caret_preserves_it(self):
        """Content with '^^' inside a paste must not trigger capture."""
        srv = make_server(CLIState.IDLE)
        content = b'prefix^^suffix\nline2'
        srv._input_filter_impl(self._BP_START + content + self._BP_END)
        # Must not have entered capture mode.
        assert srv._queue_capture_mode is False
        ph = _paste_ph('prefix^^suffix\nline2')
        assert srv._paste_text_map[ph] == 'prefix^^suffix\nline2'

    def test_paste_with_backspace_byte_preserves_it(self):
        """Pasting content with a raw backspace byte must not erase
        previously accumulated bytes."""
        srv = make_server(CLIState.IDLE)
        # Raw backspace byte (0x7f) inside paste content.
        content = b'abc\x7fdef\nmore'
        srv._input_filter_impl(self._BP_START + content + self._BP_END)
        ph = _paste_ph('abc\x7fdef\nmore')
        assert srv._paste_text_map[ph] == 'abc\x7fdef\nmore'

    def test_paste_inside_empty_capture_esc_sends_bracketed_paste(self):
        """^^ on empty CLI → paste big text → Esc must wrap in bracketed
        paste markers (not flatten \\n→space)."""
        import time
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        with patch('termios.tcflush'):
            # ^^ on an EMPTY CLI.
            srv._input_filter_impl(b'^^')
            assert srv._queue_capture_mode is True
            assert srv._capture_initial_text == ''
            # Paste multi-line content inside capture.
            content = b'line1\nline2\nline3\nline4'
            srv._input_filter_impl(self._BP_START + content + self._BP_END)
            # Esc to cancel.
            srv._input_filter_impl(b'\x1b')
            time.sleep(0.5)
        calls = []
        for c in srv.pty.send.call_args_list:
            if c[0]:
                arg = c[0][0]
                if isinstance(arg, bytes):
                    arg = arg.decode('utf-8', errors='replace')
                calls.append(arg)
        joined = ''.join(calls)
        # Must be wrapped in bracketed paste so Claude shows a clean
        # [Pasted text #N] attachment, not flattened.
        assert '\x1b[200~line1\nline2\nline3\nline4\x1b[201~' in joined, (
            f'expected bracketed paste wrap, got: {joined!r}'
        )
        # Must not flatten \n to space.
        assert 'line1 line2 line3 line4' not in joined

    def test_terminal_backspace_after_placeholder_removes_whole_token(self):
        """Backspace in CLI right after a [Paste #N] removes the whole token."""
        srv = make_server(CLIState.IDLE)
        # Paste creates placeholder in terminal buf.
        srv._input_filter_impl(self._BP_START + b'line1\nline2' + self._BP_END)
        buf_before = bytes(srv._terminal_input_buf)
        assert b'[Paste #' in buf_before
        # Backspace at end of the placeholder.
        srv._input_filter_impl(b'\x7f')
        # Whole placeholder gone, not just the ']'.
        assert srv._terminal_input_buf == b''
        assert srv._terminal_input_cursor == 0

    def test_terminal_delete_key_on_placeholder_removes_whole_token(self):
        """Forward Delete at start of a [Paste #N] removes the whole token."""
        srv = make_server(CLIState.IDLE)
        srv._input_filter_impl(self._BP_START + b'line1\nline2' + self._BP_END)
        # Cursor at end of placeholder. Home to start.
        srv._input_filter_impl(b'\x1b[H')
        assert srv._terminal_input_cursor == 0
        # Delete key → whole placeholder.
        srv._input_filter_impl(b'\x1b[3~')
        assert srv._terminal_input_buf == b''
        assert srv._terminal_input_cursor == 0

    def test_cursor_skips_multibyte_utf8_atomically(self):
        """Left/Right arrow must not land the cursor mid-UTF-8-char."""
        srv = make_server(CLIState.IDLE)
        # Hebrew aleph (2 bytes) + ASCII.
        srv._input_filter_impl('\u05d0'.encode('utf-8'))  # א
        srv._input_filter_impl(b'hi')
        # Now buf = b'\xd7\x90hi', cursor at end = 4.
        assert srv._terminal_input_cursor == 4
        srv._input_filter_impl(b'\x1b[D')  # Left: over 'i' (1 byte)
        assert srv._terminal_input_cursor == 3
        srv._input_filter_impl(b'\x1b[D')  # Left: over 'h' (1 byte)
        assert srv._terminal_input_cursor == 2
        srv._input_filter_impl(b'\x1b[D')  # Left: over aleph (2 bytes)
        assert srv._terminal_input_cursor == 0, (
            f'must skip multi-byte char atomically, got {srv._terminal_input_cursor}'
        )

    def test_type_between_two_pastes_preserves_order_in_capture(self):
        """User's reported flow: paste A, paste B, Left-arrow between them,
        type 'hello', then ^^.  Capture buf must reflect Claude's actual
        display order (A, hello, B) — not our byte-arrival order (A, B, hello)."""
        srv = make_server(CLIState.IDLE)
        # Paste A.
        srv._input_filter_impl(
            self._BP_START + b'aaa\naaa\naaa' + self._BP_END)
        # Paste B.
        srv._input_filter_impl(
            self._BP_START + b'bbb\nbbb\nbbb' + self._BP_END)
        # Left arrow N times to move cursor before [Paste #B].
        ph_b_len = len(bytes(srv._terminal_input_buf)) // 2
        srv._input_filter_impl(b'\x1b[D')  # one Left jumps over placeholder
        # Type 'hello' (should insert between placeholders).
        srv._input_filter_impl(b'hello')
        # Verify order in buf: [Paste #A] hello [Paste #B]
        buf_str = bytes(srv._terminal_input_buf).decode('utf-8')
        assert buf_str.count('[Paste #') == 2, (
            f'expected 2 placeholders, got buf: {buf_str!r}'
        )
        # 'hello' is between the two placeholders.
        idx_hello = buf_str.find('hello')
        ph1_end = buf_str.find(']') + 1
        ph2_start = buf_str.rfind('[Paste #')
        assert ph1_end <= idx_hello < ph2_start, (
            f'hello must be between the two placeholders, got: {buf_str!r}'
        )

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

    def test_idle_clear_sends_n_back_to_back_ctrlus(self):
        """In IDLE, ``_clear_stale_cli_input`` must send enough
        Ctrl+Us to clear multi-line content.  Empirical question:
        does Ink's IDLE Ctrl+U progress the cursor up after killing
        a line?  If yes, N Ctrl+Us clear N logical lines.  We
        send ``max(lines, rows) + 3`` for safety in either case."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        with patch('termios.tcflush'), \
                patch('shutil.get_terminal_size',
                      return_value=os.terminal_size((80, 24))):
            srv._input_filter_impl(
                b'\x1b[200~line1\nline2\nline3\x1b[201~')
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        ctrlu = sum(c.count('\x15') for c in calls if isinstance(c, str))
        # 3-line paste → max(3 lines, 3 rows) + 3 = 6 Ctrl+Us.
        assert ctrlu >= 6, \
            f"want >=6 Ctrl+Us for 3-line paste in IDLE, got {ctrlu}"

    def test_clear_after_multiline_paste_kills_all_rows_idle(self):
        """Multi-line paste (4 lines) → ^^ → Enter: in IDLE, the clear
        sends ``max(lines, rows) + 3`` back-to-back Ctrl+Us.  Whether
        Ink's Ctrl+U is line-bound (one per logical line, progressing
        cursor up) or row-bound (one per visual row), the count
        covers both interpretations.
        """
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        with patch('termios.tcflush'), \
                patch('shutil.get_terminal_size',
                      return_value=os.terminal_size((80, 24))):
            srv._input_filter_impl(
                b'\x1b[200~line1\nline2\nline3\nline4\x1b[201~')
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        ctrlu = sum(c.count('\x15') for c in calls if isinstance(c, str))
        # 4 lines paste → max(4, 4) + 3 = 7 Ctrl+Us.
        assert ctrlu >= 7, \
            f"want >=7 Ctrl+Us for 4-line paste in IDLE, got {ctrlu}"

    def test_clear_after_multiline_paste_kills_all_rows_running(self):
        """Same regression in RUNNING — Ctrl+U is row-bound, so the
        paste-row count drives correctness here."""
        srv = make_server(CLIState.RUNNING)
        with patch('shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 24))):
            srv._input_filter_impl(
                b'\x1b[200~line1\nline2\nline3\nline4\x1b[201~')
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        total = sum(
            c.count('\x15') for c in calls if isinstance(c, str)
        )
        assert total >= 5, \
            f"want >=5 Ctrl+U for 4-row paste in RUNNING, got {total}"

    def test_clear_after_wide_char_paste_counts_cells_in_running(self):
        """Multi-line paste containing CJK Wide characters in RUNNING:
        visual-row count must use cell width (CJK = 2 cells) not char
        count, otherwise long CJK lines undercount wraps and the
        topmost row is left on the CLI input box.

        RUNNING-mode clear is row-bound, so the Ctrl+U count == row
        count.  IDLE-mode is line-bound (Ctrl+U + Backspace pattern,
        N Ctrl+Us for N logical lines) which wouldn't exercise the
        cell-width math the same way."""
        srv = make_server(CLIState.RUNNING)
        # Each Hiragana char is 2 cells.  41 wide chars × 2 cells =
        # 82 cells → wraps to 2 visual rows on 80 cols.  Two such
        # lines pasted → 4 visual rows total.  With the old
        # len()-based math: 41 chars < 80 cols → 1 row per line → 2
        # visual rows total — undercount.
        wide_line = ('あ' * 41).encode('utf-8')
        content = wide_line + b'\n' + wide_line
        with patch('shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 24))):
            srv._input_filter_impl(b'\x1b[200~' + content + b'\x1b[201~')
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'\r')
        calls = [c[0][0] for c in srv.pty.send.call_args_list]
        total = sum(
            c.count('\x15') for c in calls if isinstance(c, str)
        )
        # 4 visual rows → at least 5 Ctrl+Us (rows + 1 safety + 1 initial).
        assert total >= 5, \
            f"want >=5 Ctrl+U for 4-row CJK paste in RUNNING, got {total}"

    def test_queue_add_in_capture_signals_dispatch_wake(self):
        """^^ + Enter must result in the dispatch-wake Event being
        set so the auto-sender wakes immediately instead of waiting
        out the current POLL_INTERVAL sleep.  The SIGWINCH thread
        (not the Enter handler directly) is the producer of this
        signal — that ordering ensures the CLI has repainted before
        the dispatch writes paste content."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv._dispatch_wake.clear()
        with patch('termios.tcflush'), \
                patch('shutil.get_terminal_size',
                      return_value=os.terminal_size((80, 24))):
            srv._input_filter_impl(b'\x1b[200~line1\nline2\x1b[201~')
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'\r')
            # Wait long enough for the SIGWINCH thread to fire and
            # set the wake-event.
            import time as _time
            _time.sleep(0.2)
        assert srv._dispatch_wake.is_set(), \
            "SIGWINCH thread must signal dispatch-wake post-resize"

    def test_paste_text_map_gc_drops_orphans_on_enter(self):
        """`_paste_text_map` must shed entries on Enter outside capture
        when their placeholders are no longer referenced anywhere —
        otherwise the dict grows for the lifetime of the server.
        """
        srv = make_server(CLIState.IDLE)
        srv._input_filter_impl(b'\x1b[200~line1\nline2\nline3\x1b[201~')
        # Sanity: paste collapsed to placeholder, map has the entry.
        assert srv._paste_text_map, \
            "paste must populate the text map"
        assert b'[Paste #' in bytes(srv._terminal_input_buf)
        # User submits: map should be GC'd since no live buffer
        # references the placeholder anymore.
        srv._input_filter_impl(b'\r')
        assert srv._paste_text_map == {}, \
            "Enter outside capture must GC orphan paste-map entries"

    def test_paste_text_map_gc_keeps_referenced_entries(self):
        """GC must NOT drop entries whose placeholder is still in a
        live buffer (preserved input, capture buf, etc.)."""
        srv = make_server(CLIState.IDLE)
        srv._input_filter_impl(b'\x1b[200~line1\nline2\x1b[201~')
        ph_bytes = bytes(srv._terminal_input_buf)
        # Force the live buffer to retain a reference, then GC.
        srv._gc_paste_text_map()
        assert srv._paste_text_map, \
            "still-referenced entries must survive GC"
        assert ph_bytes.decode('utf-8') in srv._paste_text_map

    def test_with_msg_enter_signals_dispatch_wake_directly(self):
        """With-msg Enter must signal ``_dispatch_wake`` directly so
        the auto-sender wakes within ~10 ms.  SIGWINCH still fires
        (in a separate thread) for visual cleanup — Ink needs the
        resize to maintain its full-screen layout, otherwise the TUI
        fragments over the Leap server welcome screen."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        with patch('termios.tcflush'), \
                patch('shutil.get_terminal_size',
                      return_value=os.terminal_size((80, 24))):
            srv._input_filter_impl(b'\x1b[200~line1\nline2\x1b[201~')
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'\r')
        # Dispatch wake must be set immediately, not via SIGWINCH.
        assert srv._dispatch_wake.is_set(), \
            "with-msg Enter must signal _dispatch_wake directly"

    def test_empty_enter_still_triggers_sigwinch_repaint(self):
        """Empty ^^ + Enter (no queued message) must still SIGWINCH —
        no dispatch follows, so the CLI needs an explicit repaint to
        clean any visual residue from the [Leap Q] overlay."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        srv.pty.resize = MagicMock()
        with patch('termios.tcflush'), \
                patch('shutil.get_terminal_size',
                      return_value=os.terminal_size((80, 24))):
            # ^^ on empty CLI, then ^^ inside (saves), then Enter.
            srv._input_filter_impl(b'hello')  # so something is "stale"
            srv._input_filter_impl(b'^^')
            # Clear the buffer so Enter takes the empty path.
            srv._queue_capture_buf.clear()
            srv._capture_cursor_pos = 0
            srv._input_filter_impl(b'\r')
            import time as _time
            _time.sleep(0.2)
        # Two resize calls (shrink + restore) make up the SIGWINCH.
        assert srv.pty.resize.call_count >= 1, \
            "empty Enter must trigger SIGWINCH repaint"

    def test_image_then_csi_u_newline_then_image_counts_all_rows(self):
        """User's reported flow: paste image, Cmd/Shift+Enter for a
        newline, paste another image, then ^^ + Enter.  Visual-row
        count must include all 3 rows (image1 + newline + image2).

        Pre-fix: Shift+Enter was an opaque escape sequence, no trace
        in ``_terminal_input_buf``.  Image rows were counted via
        ``len(_pending_paste_images)`` only.  Result: 2 rows
        (2 images) — undercount by 1.  CLI input box would retain
        the top image row after Enter clear.
        """
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        # Save_clipboard_image returns None on this test runner
        # (no pyobjc/macOS), so simulate by directly populating
        # _pending_paste_images at the same offsets the real flow
        # would.  The newline path is fully exercised via CSI u.
        srv._pending_paste_images = [(0, '/tmp/img1.png')]
        # Cmd+Enter (kitty CSI u: codepoint 13, modifier 9 = Meta).
        with patch('shutil.get_terminal_size',
                   return_value=os.terminal_size((80, 24))):
            srv._input_filter_impl(b'\x1b[13;9u')
        # Confirm the newline was mirrored into the buf.
        assert b'\n' in bytes(srv._terminal_input_buf), \
            "CSI u newline must be mirrored into _terminal_input_buf"
        # Now simulate the second image and capture entry.
        srv._pending_paste_images.append(
            (len(srv._terminal_input_buf), '/tmp/img2.png'))
        with patch('termios.tcflush'), \
                patch('shutil.get_terminal_size',
                      return_value=os.terminal_size((80, 24))):
            srv._input_filter_impl(b'^^')
        # 2 image rows + 2 lines from the buf split on \n (one before
        # the \n, one after — even if both are empty content-wise,
        # they each occupy a visual row in Ink's input box).
        assert srv._capture_stale_visual_rows >= 3, (
            f"want >=3 visual rows for img+\\n+img flow, "
            f"got {srv._capture_stale_visual_rows}"
        )

    def test_csi_u_newline_detection(self):
        """`_is_csi_u_newline` must accept Kitty (\\x1b[13;<mod>u) and
        legacy xterm (\\x1b[27;<mod>;13~) encodings, with any non-1
        modifier (1 = no modifier = plain Enter, must NOT match)."""
        from leap.server.server import LeapServer
        # Kitty encoding
        assert LeapServer._is_csi_u_newline(b'\x1b[13;2u')   # Shift
        assert LeapServer._is_csi_u_newline(b'\x1b[13;9u')   # Cmd / Meta
        assert LeapServer._is_csi_u_newline(b'\x1b[13;3u')   # Alt
        assert not LeapServer._is_csi_u_newline(b'\x1b[13;1u')  # plain
        assert not LeapServer._is_csi_u_newline(b'\x1b[13u')    # no mod
        # Legacy xterm encoding
        assert LeapServer._is_csi_u_newline(b'\x1b[27;2;13~')
        assert not LeapServer._is_csi_u_newline(b'\x1b[27;1;13~')
        # Wrong codepoint / final byte
        assert not LeapServer._is_csi_u_newline(b'\x1b[14;2u')
        assert not LeapServer._is_csi_u_newline(b'\x1b[200~')

    def test_pending_image_rows_added_to_stale_visual_rows(self):
        """Ctrl+V outside capture stores the image in
        ``_pending_paste_images`` without inserting bytes into
        ``_terminal_input_buf``, but the CLI still renders an
        image-attachment row.  ``_capture_stale_visual_rows`` at
        capture entry must include those rows so the post-Enter
        Ctrl+U sequence clears them too."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        # Simulate two Ctrl+V images already pending and a single-line
        # of plain text typed.
        srv._terminal_input_buf = bytearray(b'hello')
        srv._terminal_input_cursor = 5
        srv._chars_sent_to_cli = 5
        srv._pending_paste_images = [(5, '/tmp/a.png'), (5, '/tmp/b.png')]
        with patch('termios.tcflush'), \
                patch('shutil.get_terminal_size',
                      return_value=os.terminal_size((80, 24))):
            srv._input_filter_impl(b'^^')
        # 1 row for "hello" + 2 rows for the two image attachments.
        assert srv._capture_stale_visual_rows == 3, (
            f"want 3 visual rows (1 text + 2 images), "
            f"got {srv._capture_stale_visual_rows}"
        )

    def test_walkup_safety_cap_half_screen(self):
        """A huge multi-line paste must not walk up more than half the
        terminal viewport — bounds the worst-case cosmetic blank-out
        before SIGWINCH self-heals."""
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        writes: list[bytes] = []
        with patch('termios.tcflush'), \
                patch('os.write',
                      side_effect=lambda fd, data: writes.append(data)), \
                patch('shutil.get_terminal_size',
                      return_value=os.terminal_size((80, 24))):
            content = b'\n'.join(
                f'line{i}'.encode() for i in range(100))
            srv._input_filter_impl(b'\x1b[200~' + content + b'\x1b[201~')
            srv._input_filter_impl(b'^^')
        joined = b''.join(writes)
        up_count = joined.count(b'\x1b[A')
        # Cap is term.lines // 2 = 12 for 24-row viewport.
        assert up_count <= 12, \
            f"walk-up exceeded half-screen cap, got {up_count}"


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

    def test_cancel_with_prepended_text_uses_fast_path(self):
        """Fast path: prepending types Home + payload + End, no clear."""
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
        # Fast path: Home + "pre " + End, no clear, no re-paste of A.
        assert '\x1b[Hpre \x1b[F' in joined, (
            'prefix must be typed with Home/End around it'
        )
        assert '\x15' not in joined, 'fast path must not Ctrl+U clear'
        assert content.decode() not in joined, (
            "original paste content must not be re-sent"
        )

    def test_cancel_with_paste_placeholder_in_suffix_uses_fast_path(self):
        """Suffix containing [Paste #N] resolves to its bracketed-paste block."""
        import time
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        content_a = b'aaa\naaa\naaa'
        content_b = b'bbb\nbbb'
        with patch('termios.tcflush'):
            # Paste A, ^^, move cursor to end, paste B (raw, in-capture).
            srv._input_filter_impl(self._BP_START + content_a + self._BP_END)
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(
                self._BP_START + content_b + self._BP_END)
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
        # Suffix B wrapped; no clear, no re-paste of A.
        assert '\x1b[200~bbb\nbbb\x1b[201~' in joined
        assert '\x15' not in joined
        assert content_a.decode() not in joined

    def test_cancel_with_image_in_suffix_resolves_to_path(self):
        """Fast path with image in the suffix: resolves [Image #N] → @path."""
        import time
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        content = b'line1\nline2'
        # Pre-populate an image map entry to simulate Ctrl+V in capture.
        with patch('termios.tcflush'):
            srv._input_filter_impl(self._BP_START + content + self._BP_END)
            srv._input_filter_impl(b'^^')
            # Manually insert an image placeholder into the capture buf
            # to simulate Ctrl+V (avoids needing clipboard mocks).
            srv._capture_image_counter = 1
            srv._capture_image_map['[Image #1]'] = '/tmp/foo.png'
            srv._queue_capture_buf.extend(b'[Image #1]')
            srv._capture_cursor_pos = len(srv._queue_capture_buf)
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
        # Image should be sent as @path, not literal [Image #1].
        assert '@/tmp/foo.png' in joined, (
            f'@path must be resolved in fast path, got: {joined!r}'
        )
        assert '[Image #1]' not in joined, (
            'literal placeholder must not leak'
        )

    def test_cancel_with_prepended_paste_uses_fast_path(self):
        """Prepending a multi-line paste wraps the prefix in markers."""
        import time
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        content_a = b'aaa\naaa\naaa'
        content_b = b'bbb\nbbb\nbbb'
        with patch('termios.tcflush'):
            srv._input_filter_impl(self._BP_START + content_a + self._BP_END)
            srv._input_filter_impl(b'^^')
            # Move cursor to start, then paste B inside capture.
            srv._input_filter_impl(b'\x1b[H')
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
        # Home + bracketed paste of B + End. Original A untouched.
        assert '\x1b[H\x1b[200~bbb\nbbb\nbbb\x1b[201~\x1b[F' in joined
        assert '\x15' not in joined, 'fast path must not clear'
        assert content_a.decode() not in joined, (
            "A's content must stay on Claude's CLI, not be re-sent"
        )


class TestSaveThenEsc:
    """After ^^-save, Esc should restore pre-capture state."""

    _BP_START = b'\x1b[200~'
    _BP_END = b'\x1b[201~'

    def test_save_then_esc_clears_cli(self):
        """paste → ^^ → save → Esc: user committed to history, clear Claude's CLI."""
        import time
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        content = b'line1\nline2\nline3'
        with patch('termios.tcflush'):
            srv._input_filter_impl(self._BP_START + content + self._BP_END)
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'^^')  # save
            time.sleep(0.05)
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
        # Must send the clear sequence (End + Ctrl+U + End + backspace).
        assert '\x15' in joined, 'Ctrl+U must be sent to clear'
        # No re-paste of the original content.
        assert '\x1b[200~' not in joined
        # Our buf is empty after save+Esc.
        assert bytes(srv._terminal_input_buf) == b''

    def test_recall_then_type_then_esc_uses_fast_path(self):
        """Recall + type after save uses the fast path (initial updated)."""
        import time
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        content = b'line1\nline2'
        with patch('termios.tcflush'):
            srv._input_filter_impl(self._BP_START + content + self._BP_END)
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'^^')  # save
            time.sleep(0.05)
            srv._input_filter_impl(b'\x1b[A')  # up: recall
            time.sleep(0.05)
            srv._input_filter_impl(b'hi')
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
        # Fast path: just 'hi' sent. No clear, no re-paste of original.
        assert 'hi' in joined
        assert '\x15' not in joined, 'must not clear'
        assert '\x1b[200~' not in joined, 'must not re-paste'


class TestAtomicPlaceholderEditing:
    """Placeholders are edited as atomic tokens — no breakage by char-edits."""

    _BP_START = b'\x1b[200~'
    _BP_END = b'\x1b[201~'

    def test_backspace_at_end_of_placeholder_removes_whole_token(self):
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        content = b'line1\nline2'
        with patch('termios.tcflush'):
            srv._input_filter_impl(self._BP_START + content + self._BP_END)
            srv._input_filter_impl(b'^^')
            # Cursor is at end of placeholder. Backspace should delete it whole.
            srv._input_filter_impl(b'\x7f')  # Backspace
        assert srv._queue_capture_buf == b''
        assert srv._capture_cursor_pos == 0

    def test_delete_at_start_of_placeholder_removes_whole_token(self):
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        content = b'line1\nline2'
        with patch('termios.tcflush'):
            srv._input_filter_impl(self._BP_START + content + self._BP_END)
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'\x1b[H')  # Home
            srv._input_filter_impl(b'\x1b[3~')  # Delete
        assert srv._queue_capture_buf == b''

    def test_left_arrow_jumps_over_placeholder(self):
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        content = b'line1\nline2'
        with patch('termios.tcflush'):
            srv._input_filter_impl(self._BP_START + content + self._BP_END)
            srv._input_filter_impl(b'^^')
            cursor_before = srv._capture_cursor_pos
            srv._input_filter_impl(b'\x1b[D')  # Left arrow
        # One Left jumps past entire placeholder to position 0.
        assert srv._capture_cursor_pos == 0
        assert cursor_before > 0

    def test_right_arrow_jumps_over_placeholder(self):
        srv = make_server(CLIState.IDLE)
        srv.pty.process.child_fd = 999
        content = b'line1\nline2'
        with patch('termios.tcflush'):
            srv._input_filter_impl(self._BP_START + content + self._BP_END)
            srv._input_filter_impl(b'^^')
            srv._input_filter_impl(b'\x1b[H')  # Home → pos 0
            assert srv._capture_cursor_pos == 0
            srv._input_filter_impl(b'\x1b[C')  # Right arrow
        # One Right jumps past entire placeholder to end.
        assert srv._capture_cursor_pos == len(srv._queue_capture_buf.decode())


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
