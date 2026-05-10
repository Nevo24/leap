"""Tests for ``LeapServer._run_dialog_action``.

Verifies that the user's typed-but-unsubmitted text in
``_terminal_input_buf`` survives every path that answers a permission
or question dialog from outside the CLI keystream:

  * Always-send mode auto-approve (``_try_auto_approve``)
  * Mode-switch-to-Always while a dialog is up (same path)
  * Slack digit reply / typed answer (``select_option`` /
    ``custom_answer`` socket messages)
  * Monitor right-click "Approve" / "Type your answer"

Two wrap modes:

  * ``target_is_composer=False`` — menu-targeted (auto-approve,
    Claude's numbered ``select_option``, Claude's "Type something"
    nav).  Post-clear after the action handles both the no-leak
    case (composer still has user's text) and the leak case
    (trailing CR submitted it; composer is empty).

  * ``target_is_composer=True`` — composer-targeted (Codex/
    Gemini/Cursor's ``send_custom_answer``).  Pre-clear before
    the action so the answer doesn't concatenate onto the user's
    typed text.
"""

import threading
from unittest.mock import MagicMock

import pytest

from leap.cli_providers.states import CLIState
from leap.cli_providers.claude import ClaudeProvider
from leap.cli_providers.codex import CodexProvider
from leap.server.server import LeapServer


def make_server(state: str = CLIState.NEEDS_PERMISSION) -> LeapServer:
    """Build a bare ``LeapServer`` with the attributes the helper touches."""
    srv = object.__new__(LeapServer)
    srv.state = MagicMock()
    srv.state.current_state = state
    srv.state._state = state
    srv.state.on_send = MagicMock()
    srv.pty = MagicMock()
    srv.pty.process = MagicMock()
    srv.pty.process.child_fd = -1
    srv.pty.send = MagicMock()
    srv.pty.sendline = MagicMock()
    srv._provider = ClaudeProvider()

    # Every flag/buffer the helper or the methods it calls touch.
    srv._action_lock = threading.Lock()
    srv._queue_sending = False
    srv._queue_sending_held = bytearray()
    srv._terminal_input_buf = bytearray()
    srv._terminal_input_cursor = 0
    srv._chars_sent_to_cli = 0
    srv._preserved_input_buf = bytearray()
    srv._preserved_chars_sent = 0
    srv._paste_text_map = {}
    return srv


def fn_sends(server: LeapServer, status: str = 'sent'):
    """Build an *fn* that just records the queue_sending state at call time."""
    record = {}

    def _fn():
        record['queue_sending_during_fn'] = server._queue_sending
        record['mirror_during_fn'] = bytes(server._terminal_input_buf)
        return {'status': status}

    return _fn, record


class TestSnapshot:
    def test_snapshot_taken_when_buf_nonempty(self) -> None:
        srv = make_server()
        srv._terminal_input_buf = bytearray(b'hello')
        srv._chars_sent_to_cli = 5

        captured = {}
        def _fn():
            captured['preserved_at_fn'] = bytes(srv._preserved_input_buf)
            captured['preserved_chars_at_fn'] = srv._preserved_chars_sent
            return {'status': 'sent'}

        srv._run_dialog_action(_fn, target_is_composer=False)

        assert captured['preserved_at_fn'] == b'hello'
        assert captured['preserved_chars_at_fn'] == 5

    def test_snapshot_skipped_when_buf_empty(self) -> None:
        srv = make_server()
        captured = {}
        def _fn():
            captured['preserved_at_fn'] = bytes(srv._preserved_input_buf)
            return {'status': 'sent'}

        srv._run_dialog_action(_fn, target_is_composer=False)
        assert captured['preserved_at_fn'] == b''

    def test_existing_snapshot_not_overwritten(self) -> None:
        """Pre-existing snapshot from a prior failed dispatch is
        preserved (consistent with ``_send_to_cli`` behavior)."""
        srv = make_server()
        srv._terminal_input_buf = bytearray(b'newtext')
        srv._chars_sent_to_cli = 7
        srv._preserved_input_buf = bytearray(b'oldsnap')
        srv._preserved_chars_sent = 7

        captured = {}
        def _fn():
            captured['preserved_at_fn'] = bytes(srv._preserved_input_buf)
            return {'status': 'sent'}

        srv._run_dialog_action(_fn, target_is_composer=False)
        assert captured['preserved_at_fn'] == b'oldsnap'


class TestQueueSendingBlock:
    def test_queue_sending_set_during_fn(self) -> None:
        srv = make_server()
        fn, record = fn_sends(srv)
        srv._run_dialog_action(fn, target_is_composer=False)
        assert record['queue_sending_during_fn'] is True

    def test_queue_sending_unset_after(self) -> None:
        srv = make_server()
        fn, _ = fn_sends(srv)
        srv._run_dialog_action(fn, target_is_composer=False)
        assert srv._queue_sending is False

    def test_queue_sending_unset_on_exception(self) -> None:
        srv = make_server()
        def _fn():
            raise RuntimeError('boom')
        with pytest.raises(RuntimeError):
            srv._run_dialog_action(_fn, target_is_composer=False)
        assert srv._queue_sending is False

    def test_action_lock_released_on_exception(self) -> None:
        srv = make_server()
        def _fn():
            raise RuntimeError('boom')
        with pytest.raises(RuntimeError):
            srv._run_dialog_action(_fn, target_is_composer=False)
        # Lock must be releasable now (would block if leaked).
        assert srv._action_lock.acquire(blocking=False)
        srv._action_lock.release()


class TestMirrorClearedDuringFn:
    """During ``fn``, the mirror is empty so any concurrent input filter
    pass (after ``_queue_sending`` is unset) doesn't see stale data."""

    def test_mirror_empty_during_fn(self) -> None:
        srv = make_server()
        srv._terminal_input_buf = bytearray(b'hello')
        fn, record = fn_sends(srv)
        srv._run_dialog_action(fn, target_is_composer=False)
        assert record['mirror_during_fn'] == b''


class TestMenuTargetSuccess:
    """target_is_composer=False, fn returns 'sent': post-clear + retype."""

    def test_post_clear_sent_after_fn(self) -> None:
        srv = make_server()
        srv._terminal_input_buf = bytearray(b'hello')
        sends = []
        srv.pty.send = lambda data: sends.append(('send', data))
        srv.pty.sendline = lambda data: sends.append(('sendline', data))
        def _fn():
            sends.append(('fn', None))
            return {'status': 'sent'}

        srv._run_dialog_action(_fn, target_is_composer=False)

        fn_idx = next(i for i, (kind, _) in enumerate(sends) if kind == 'fn')
        end_indices = [i for i, (kind, d) in enumerate(sends)
                       if kind == 'send' and d == '\x1b[F']
        u_indices = [i for i, (kind, d) in enumerate(sends)
                     if kind == 'send' and d and '\x15' in d]
        retype_indices = [i for i, (kind, d) in enumerate(sends)
                          if kind == 'send' and d == 'hello']
        assert end_indices, 'no End key sent (post-clear missing)'
        assert u_indices, 'no Ctrl+U sent (post-clear missing)'
        assert retype_indices, 'no retype of preserved text'
        assert fn_idx < min(end_indices), 'post-clear ran before fn'
        assert max(u_indices) < retype_indices[0], 'retype ran before clear'

    def test_no_clear_or_retype_when_buf_empty(self) -> None:
        srv = make_server()
        sends = []
        srv.pty.send = lambda data: sends.append(data)
        srv.pty.sendline = lambda data: sends.append(('line', data))
        def _fn():
            return {'status': 'sent'}

        srv._run_dialog_action(_fn, target_is_composer=False)

        assert '\x1b[F' not in sends
        assert not any(s == 'hello' for s in sends if isinstance(s, str))


class TestComposerTargetSuccess:
    """target_is_composer=True, fn returns 'sent': pre-clear + retype."""

    def test_pre_clear_sent_before_fn(self) -> None:
        srv = make_server()
        srv._provider = CodexProvider()
        srv._terminal_input_buf = bytearray(b'hello')
        sends = []
        srv.pty.send = lambda data: sends.append(('send', data))

        def _fn():
            sends.append(('fn', None))
            return {'status': 'sent'}

        srv._run_dialog_action(_fn, target_is_composer=True)

        fn_idx = next(i for i, (kind, _) in enumerate(sends) if kind == 'fn')
        end_indices = [i for i, (kind, d) in enumerate(sends)
                       if kind == 'send' and d == '\x1b[F']
        u_indices = [i for i, (kind, d) in enumerate(sends)
                     if kind == 'send' and d and '\x15' in d]
        retype_indices = [i for i, (kind, d) in enumerate(sends)
                          if kind == 'send' and d == 'hello']
        assert end_indices, 'no End key sent (pre-clear missing)'
        assert u_indices, 'no Ctrl+U sent (pre-clear missing)'
        assert max(u_indices, default=-1) < fn_idx, 'pre-clear ran after fn'
        assert retype_indices and retype_indices[0] > fn_idx


class TestErrorPath:
    """When fn returns 'error', the wrap should not double-up on the
    composer (which fn never touched)."""

    def test_menu_target_error_only_syncs_mirror(self) -> None:
        srv = make_server()
        srv._terminal_input_buf = bytearray(b'hello')
        srv._chars_sent_to_cli = 5
        sends = []
        srv.pty.send = lambda data: sends.append(data)

        def _fn():
            return {'status': 'error', 'error': 'option not found'}

        srv._run_dialog_action(_fn, target_is_composer=False)

        assert '\x1b[F' not in sends
        assert not any('\x15' in s for s in sends if isinstance(s, str))
        assert 'hello' not in sends
        assert bytes(srv._terminal_input_buf) == b'hello'
        assert srv._chars_sent_to_cli == 5
        assert bytes(srv._preserved_input_buf) == b''

    def test_composer_target_error_retypes(self) -> None:
        """target_is_composer=True implies pre-clear ran; on error the
        composer is empty so we must physically restore."""
        srv = make_server()
        srv._provider = CodexProvider()
        srv._terminal_input_buf = bytearray(b'hello')
        sends = []
        srv.pty.send = lambda data: sends.append(data)

        def _fn():
            return {'status': 'error', 'error': 'fake error'}

        srv._run_dialog_action(_fn, target_is_composer=True)
        assert 'hello' in sends


class TestProviderFlag:
    def test_claude_routes_through_menu(self) -> None:
        assert ClaudeProvider().custom_answer_targets_composer is False

    def test_codex_targets_composer(self) -> None:
        assert CodexProvider().custom_answer_targets_composer is True


class TestRestoreMultiline:
    """``_restore_preserved_input`` must wrap multi-line text in
    bracketed-paste markers so Ink doesn't submit per-line."""

    def test_multiline_uses_bracketed_paste(self) -> None:
        srv = make_server()
        sends: list[str] = []
        srv.pty.send = lambda data: sends.append(data)
        srv._preserved_input_buf = bytearray(b'line1\nline2\nline3')
        srv._preserved_chars_sent = 17
        srv._restore_preserved_input()
        assert len(sends) == 1
        payload = sends[0]
        assert payload.startswith('\x1b[200~')
        assert payload.endswith('\x1b[201~')
        assert 'line1\nline2\nline3' in payload

    def test_singleline_no_bracketed_paste(self) -> None:
        srv = make_server()
        sends: list[str] = []
        srv.pty.send = lambda data: sends.append(data)
        srv._preserved_input_buf = bytearray(b'hello')
        srv._preserved_chars_sent = 5
        srv._restore_preserved_input()
        assert sends == ['hello']

    def test_strips_existing_paste_markers(self) -> None:
        """If the preserved text already contains paste markers, strip
        them before wrapping so the framing doesn't break."""
        srv = make_server()
        sends: list[str] = []
        srv.pty.send = lambda data: sends.append(data)
        srv._preserved_input_buf = bytearray(b'a\x1b[200~b\nc\x1b[201~d')
        srv._preserved_chars_sent = 10
        srv._restore_preserved_input()
        payload = sends[0]
        assert payload.startswith('\x1b[200~')
        assert payload.endswith('\x1b[201~')
        inner = payload[len('\x1b[200~'):-len('\x1b[201~')]
        assert '\x1b[200~' not in inner
        assert '\x1b[201~' not in inner
