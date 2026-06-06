"""
Main Leap PTY Server.

Orchestrates PTY handling, socket server, and queue management.
"""

import atexit
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import termios
import threading
import time
import traceback
import unicodedata
from pathlib import Path
from typing import Any, Optional, Union

try:
    from AppKit import (
        NSBitmapImageRep,
        NSPasteboard,
        NSPasteboardTypePNG,
        NSPasteboardTypeTIFF,
        NSPNGFileType,
    )
    HAS_APPKIT = True
except ImportError:  # non-macOS or pyobjc missing
    HAS_APPKIT = False

from leap.cli_providers.base import CLIProvider
from leap.cli_providers.registry import get_display_name, get_provider
from leap.cli_providers.states import AutoSendMode, CLIState, PROMPT_STATES, WAITING_STATES
from leap.utils.atomic_write import atomic_write_json
from leap.utils.constants import (
    QUEUE_DIR, SOCKET_DIR, HISTORY_DIR, NOTE_IMAGES_DIR, QUEUE_IMAGES_DIR,
    STORAGE_DIR, POLL_INTERVAL, TITLE_RESET_INTERVAL,
    ensure_storage_dirs, load_settings,
)
from leap.utils.menu import extract_menu_options
from leap.utils.terminal import (
    _jetbrains_sweep_stale_tabs, set_terminal_title, print_banner,
)
from leap.server.pty_handler import PTYHandler
from leap.server.socket_handler import SocketHandler
from leap.server.queue_manager import QueueManager
from leap.server.metadata import SessionMetadata
from leap.server.state_tracker import CLIStateTracker
from leap.slack.output_capture import OutputCapture
from leap.server.validation import validate_pinned_session




class LeapServer:
    """
    Leap PTY Server.

    Manages a CLI session with message queueing and socket-based
    client communication.  Supports multiple CLI backends via the
    CLIProvider abstraction.
    """

    # Matches OSC escape sequences that set the terminal title (params 0, 1, 2),
    # terminated by BEL (\x07) or ST (\x1b\\).  Stripped from PTY output so
    # the CLI cannot override the "lps <tag>" tab name.
    _OSC_TITLE_RE: re.Pattern[bytes] = re.compile(
        rb'\x1b\][012];[^\x07\x1b]*(?:\x07|\x1b\\)'
    )

    def __init__(
        self,
        tag: str,
        flags: Optional[list[str]] = None,
        cli: Optional[str] = None,
    ) -> None:
        """
        Initialize Leap server.

        Args:
            tag: Session tag name.
            flags: Optional flags to pass to the CLI.
            cli: CLI provider name ('claude', 'codex', 'copilot', 'cursor-agent', 'gemini'). Defaults to 'claude'.
        """
        self.tag = tag
        self.running = True
        self._provider = get_provider(cli)

        # Ensure storage directories exist
        ensure_storage_dirs()
        self._cleanup_old_images()

        # Validate against monitor pinned sessions (PR-pinned rows).
        # Release the startup lock on failure so another server can start.
        try:
            validate_pinned_session(tag, STORAGE_DIR)
        except SystemExit:
            self._release_startup_lock()
            raise

        # Initialize paths
        self.queue_file = QUEUE_DIR / f"{tag}.queue"
        self.socket_path = SOCKET_DIR / f"{tag}.sock"

        # Remove stale socket file
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        # Initialize components
        self.pty = PTYHandler(
            flags, tag=tag, signal_dir=SOCKET_DIR,
            provider=self._provider,
        )
        self.queue = QueueManager(self.queue_file)
        self.metadata = SessionMetadata(tag, SOCKET_DIR)
        self.socket_handler = SocketHandler(self.socket_path, self._handle_message)

        # State tracking — per-session pinned mode overrides global default.
        # Snapshot the resolved mode back to the pin so subsequent changes
        # to the global default (via the Settings dialog) don't retroactively
        # flip this session's auto-approve behavior at the hook layer — the
        # Claude PermissionRequest hook re-reads from disk every tool call
        # and falls back to global when the pin lacks ``auto_send_mode``,
        # so an unsnapshotted session would silently inherit later global
        # writes.  After the snapshot, only ``set_auto_send_mode`` for this
        # tag (per-session toggle) can change this session's mode.
        global_mode = load_settings().get('auto_send_mode', AutoSendMode.PAUSE)
        # Defensive coerce — a hand-edited settings.json with a non-string
        # value (e.g. ``"auto_send_mode": 42``) would otherwise propagate
        # through ``_load_pinned_auto_send_mode``'s ``default`` parameter
        # straight into ``CLIStateTracker.auto_send_mode``, leaving the
        # whole auto-send subsystem in a state where neither PAUSE nor
        # ALWAYS comparisons ever match.
        if global_mode not in (AutoSendMode.PAUSE, AutoSendMode.ALWAYS):
            global_mode = AutoSendMode.PAUSE
        pinned_mode = self._load_pinned_auto_send_mode(tag, global_mode)
        if pinned_mode not in (AutoSendMode.PAUSE, AutoSendMode.ALWAYS):
            pinned_mode = AutoSendMode.PAUSE
        self._save_pinned_auto_send_mode(tag, pinned_mode)
        self.state = CLIStateTracker(
            signal_file=SOCKET_DIR / f"{tag}.signal",
            auto_send_mode=pinned_mode,
            provider=self._provider,
            cwd=os.getcwd(),
            tag=tag,
        )
        self.output_capture = OutputCapture(tag, cli_provider=self._provider.name)
        self._terminal_input_buf: bytearray = bytearray()
        # Byte offset of the insertion cursor within ``_terminal_input_buf``
        # so we mirror Claude's actual input-line state when the user
        # moves the cursor (arrows / Home / End) and inserts text in
        # the middle.  Without tracking this, typing between two
        # pastes would append at the end of our buf — and ^^ capture
        # would show the pastes and the text in the wrong order
        # relative to what Claude displays.
        self._terminal_input_cursor: int = 0
        # Tracks incomplete escape sequences split across os.read() chunks.
        # None = no partial.  'esc' = bare \x1b at end (need type byte).
        # 'csi' = \x1b[ + optional params at end (need final byte 0x40-0x7e).
        self._partial_escape: Optional[str] = None
        self._user_has_typed: bool = False  # True after first Enter in the terminal
        # Previous state seen by _input_filter — used to clear stale bytes
        # when transitioning from running to idle (prevents keyboard-layout
        # artefacts from leaking into the tracked "last message").
        self._prev_filter_state: Optional[CLIState] = None
        self._pending_resize: bool = False
        # Queue-from-server: "^" prefix capture mode.
        # When "^" is the first char on a line we enter capture mode
        # and swallow all subsequent input until Enter → queue.
        self._queue_capture_mode: bool = False
        self._queue_capture_buf: bytearray = bytearray()
        self._capture_stale_caret: bool = False  # cross-chunk ^^ left a literal ^ in CLI
        # Visual rows + logical lines occupied by pre-capture CLI
        # input (placeholders expanded via _paste_text_map, plus
        # Ctrl+V image attachments).  > 0 on either field means
        # "the CLI has stale text that needs clearing".  Visual rows
        # drive the row-bound Ctrl+U sequence in RUNNING mode;
        # logical lines drive the line-bound Ctrl+U + Backspace
        # pattern in IDLE mode.
        self._capture_stale_visual_rows: int = 0
        self._capture_stale_logical_lines: int = 0
        self._chars_sent_to_cli: int = 0  # printable chars actually on CLI's input
        self._capture_pre_input_buf: bytearray = bytearray()  # snapshot for cancel
        self._capture_pre_chars_sent: int = 0  # snapshot for cancel
        self._capture_pre_input_cursor: int = 0  # cursor snapshot for cancel restore
        self._capture_cancel_pending: bool = False  # bg thread sending text
        self._capture_cursor_pos: int = 0  # character cursor in capture text
        self._capture_show_hint: bool = True  # show hint until first keystroke
        self._capture_prev_lines: int = 0  # wrapped line count from last display
        self._capture_utf8_buf: bytearray = bytearray()  # incomplete UTF-8 bytes
        self._capture_image_counter: int = 0
        self._capture_image_map: dict[str, str] = {}  # "[Image #N]" → path
        # Clipboard images saved by Ctrl+V outside capture mode —
        # picked up automatically when ^^ enters capture.
        # Each entry is (char_offset, path).  char_offset is the
        # position in _terminal_input_buf at paste time so that ^^
        # injects the image at the right place.  -1 means "append
        # at end" (used for images saved back from cancel).
        self._pending_paste_images: list[tuple[int, str]] = []
        # Snapshot of capture buffer right after entering capture mode
        # (including any auto-injected image).  Used by _capture_cancel
        # to detect whether the user actually edited the text.
        self._capture_initial_text: str = ""
        # True when a single "^" was typed mid-text, waiting to see
        # if the next byte is also "^" (double-caret → capture mode).
        self._pending_caret: bool = False
        self._pending_caret_time: float = 0.0  # when ^ was held
        self._pending_caret_timer: Optional[threading.Timer] = None
        self._pending_caret_flush: bool = False  # paste cleared held ^
        # Saved message history (^^ inside capture mode saves + clears).
        # Browsed with arrow up/down.  Persisted to .storage/.
        self._saved_messages: list[str] = self._load_saved_messages()
        self._saved_msg_index: int = -1  # -1 = not browsing
        self._capture_show_saved_hint: bool = False  # "Saved!" hint active
        self._pending_bang: bool = False          # first '!' held in capture, waiting for second
        self._pending_bang_time: float = 0.0     # monotonic time of first '!'
        self._capture_force_confirm: bool = False # showing force-send confirmation, awaiting Enter
        # Bracketed paste detection — terminals wrap pasted text in
        # ESC[200~ ... ESC[201~.  While inside a paste, ^^ is treated
        # as literal text so pasted tracebacks (which contain ^^^^)
        # don't accidentally trigger capture mode.
        self._in_bracketed_paste: bool = False
        # Bracketed paste capture — large pastes are collapsed into
        # a [Paste #N] placeholder in _terminal_input_buf so that ^^
        # capture shows a short token instead of the full sprawl.
        # The placeholder is resolved back to raw text when the
        # queued message is sent.  CLIs like Claude Code already do
        # their own paste collapsing on display; this mirrors that
        # into our internal view.
        self._paste_accumulator: Optional[bytearray] = None
        self._paste_buf_snapshot_len: int = 0
        self._paste_cursor_snapshot: int = 0
        self._paste_chars_snapshot: int = 0
        # Map of ``[Paste #<hash>]`` → full pasted text.  The hash is
        # derived from the content (first 8 hex chars of md5) so the
        # same paste always produces the same placeholder — dedupes
        # repeats and keeps the ID stable across save/recall cycles.
        self._paste_text_map: dict[str, str] = {}
        self._last_output_time: float = 0.0  # timestamp of last CLI output
        # Wake-up signal for the auto-sender — set when a message
        # is queued so dispatch is near-instant instead of waiting
        # out the current POLL_INTERVAL sleep.
        self._dispatch_wake: threading.Event = threading.Event()
        # When the with-msg ^^ + Enter path defers SIGWINCH so the
        # Ink full repaint doesn't block dispatch, this flag tells
        # ``_send_to_cli`` to fire the resize itself once the
        # paste-and-submit is done.
        self._pending_sigwinch: bool = False
        # Bypass the auto-sender's "IDLE-only" dispatch gate for the
        # next dispatch.  Set by capture-mode ^^ + Enter so the user's
        # explicit message goes through even when the state tracker
        # is mis-classifying RUNNING (Ink cursor blink / render
        # afterglow keeps emitting low-rate output, which can hold
        # state in RUNNING for 10+ seconds on long transcripts even
        # when Claude itself is idle).  Still gated on WAITING_STATES
        # — never dispatch through a permission / input prompt.
        self._capture_force_dispatch: bool = False
        self._send_clear_queue: list[bool] = []  # per-message clear flags (FIFO)
        self._suppress_send_until: float = 0.0  # suppress output until monotonic time
        # Preserved user input: when a queue message interrupts typing,
        # the partial text is saved here and restored after the CLI
        # returns to idle and the queue is empty.
        self._preserved_input_buf: bytearray = bytearray()
        self._preserved_chars_sent: int = 0
        # CLI input-history recall: Leap intercepts ↑/↓ outside capture
        # mode and drives recall itself by reading the CLI's own
        # history file (see provider.input_history).  Without this,
        # the CLI's TUI re-paints its input box with the recalled text
        # but ``_terminal_input_buf`` stays at its pre-↑ state, so a
        # subsequent ``^^`` snapshots an empty / stale buffer.
        #
        # Cache lifecycle: ``None`` means "not loaded for this recall
        # session" — we re-fetch on the first ↑/↓ after every reset
        # (Enter, Ctrl+C, queue dispatch) so just-submitted messages
        # show up immediately.  Once loaded, ↑↑↑ stays in-memory.
        #
        # Index semantics: ``-1`` is the "idle / not browsing" sentinel.
        # On the first ↑ after reset we snapshot the live buffer into
        # ``_pre_recall_text`` and seed the index at ``len(cache)`` —
        # the "after-newest" position, same as readline.  ↓ past
        # ``len(cache)-1`` returns to the snapshot (mirrors bash/Ink
        # behavior of restoring the partial pre-recall text).
        self._cli_history_cache: Optional[list[str]] = None
        self._cli_history_index: int = -1
        self._pre_recall_text: bytes = b''
        self._cwd: str = os.getcwd()
        self._queue_sending: bool = False  # blocks input filter during send
        self._queue_sending_held: bytearray = bytearray()  # keystrokes buffered during send
        # Serializes all paths that send keystrokes to the CLI on behalf
        # of a queued message, an auto-approve, a remote select_option
        # (Slack / Monitor), or a custom_answer.  Without it, two of
        # these paths running concurrently would each toggle
        # ``_queue_sending`` independently — the first to finish flips
        # it back to False and unblocks the input filter while the
        # second is still mid-send, allowing user keystrokes to
        # interleave with our writes and corrupt the dialog/composer.
        self._action_lock = threading.Lock()

        # Clean up old history files
        self._cleanup_old_history_files()

        # Load existing queue and save metadata (include CLI provider)
        self.queue.load()
        self.metadata.save(cli_provider=self._provider.name)

        # Sweep stale ``lps <tag>``/``lpc <tag>`` JetBrains tabs left
        # over by force-quit / kernel panic / power loss — see
        # ``_jetbrains_sweep_stale_tabs`` for the contract.
        #
        # Synchronous (not a daemon thread) because the function MUST
        # finish before the ``lps <tag>`` OSC fires in ``_run()`` —
        # otherwise the Groovy rename can race the OSC and either
        # leave the new tab bare (sweep wins) or leave the old
        # duplicate tab as ``lps <tag>`` (OSC wins after sweep already
        # passed it).  ``exclude_tag=self.tag`` removes our own
        # about-to-start tag from the live-tags allow-list, so any
        # pre-existing ``lps <ourTag>`` tab is treated as a stale
        # duplicate and renamed to bare — the OSC then claims the
        # name on our brand-new tab a moment later.  Cost is
        # ~200-1000ms (warm JetBrains) and entirely no-op for
        # iTerm2/Terminal.app/Warp/WezTerm/cmux (function self-gates on
        # ``_resolve_jetbrains_cli``).
        ide_now = self.metadata.ide or ''
        proj_now = self.metadata.project_path or ''
        if ide_now and proj_now:
            _jetbrains_sweep_stale_tabs(
                STORAGE_DIR, ide_now, proj_now, exclude_tag=self.tag,
            )

        # Prompt user about old queue messages
        if not self.queue.is_empty:
            self._prompt_load_old_queue()

        # Register cleanup
        atexit.register(self.cleanup)

    @staticmethod
    def _load_pinned_auto_send_mode(tag: str, default: str) -> str:
        """Read auto_send_mode from pinned sessions if set for this tag.

        Symmetric with ``_save_pinned_auto_send_mode`` — tolerates a
        non-dict root (corrupt file or future schema migration) and
        non-dict tag entries (hand-edited file).  Important because
        this is on the server's ``__init__`` snapshot path: a crash
        here would block session startup, not just lose the per-tag
        mode.
        """
        pinned_file = STORAGE_DIR / "pinned_sessions.json"
        # Catch ``ValueError`` (covers ``UnicodeDecodeError`` and
        # ``json.JSONDecodeError``) and ``OSError`` so a corrupt or
        # mis-encoded pin file can't crash the server's ``__init__``
        # snapshot path — falling back to ``default`` is fine here.
        try:
            if pinned_file.exists():
                with open(pinned_file, 'r') as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    return default
                entry = loaded.get(tag, {})
                if not isinstance(entry, dict):
                    return default
                mode = entry.get('auto_send_mode', default)
                return mode if isinstance(mode, str) and mode else default
        except (OSError, ValueError):
            pass
        return default

    @staticmethod
    def _save_pinned_auto_send_mode(tag: str, mode: str) -> None:
        """Persist auto_send_mode in pinned sessions for this tag.

        Creates the file / tag entry if missing — without this, a server
        started before the monitor (or used from the CLI client with no
        monitor running) would silently drop the per-session value and
        the hook would fall back to global, defeating the snapshot.
        """
        pinned_file = STORAGE_DIR / "pinned_sessions.json"
        # Inner try/except around the json.load — if the file is corrupt
        # or mis-encoded, treat it as empty and proceed with the write.
        # Without this, a corrupt pin file would stay corrupt forever:
        # the snapshot at __init__ would silently fail, the hook would
        # read the same corrupt file and fall back to global, and no
        # subsequent toggle could heal the disk (every write would
        # re-read the corrupt file and bail out).  Pre-fix
        # ``save_pinned_sessions`` overwrote unconditionally, so corrupt
        # files self-healed after one write — this preserves that
        # recovery semantic at the targeted-write granularity.  The
        # outer ``except OSError`` still swallows write-side failures
        # (disk full, permission denied) without crashing __init__.
        try:
            pinned: dict[str, Any] = {}
            if pinned_file.exists():
                try:
                    with open(pinned_file, 'r') as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        pinned = loaded
                except (OSError, ValueError):
                    pass
            entry = pinned.get(tag, {})
            if not isinstance(entry, dict):
                entry = {}
            if entry.get('auto_send_mode') == mode:
                return
            entry['auto_send_mode'] = mode
            pinned[tag] = entry
            atomic_write_json(pinned_file, pinned)
        except (OSError, ValueError):
            pass

    def _handle_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        """
        Handle incoming client message.

        Args:
            msg: Message dictionary from client.

        Returns:
            Response dictionary.
        """
        msg_type = msg.get('type')
        message = msg.get('message', '')

        if msg_type == 'queue':
            size = self.queue.add(message)
            # Wake the auto-sender immediately so socket-queued
            # messages dispatch near-instantly instead of waiting
            # out the current POLL_INTERVAL sleep.
            self._dispatch_wake.set()
            return {
                'status': 'queued',
                'queue_size': size,
                'queue_contents': self.queue.get_contents()
            }

        elif msg_type == 'queue_prepend':
            messages = msg.get('messages', [])
            if not messages:
                return {'status': 'error', 'error': 'no messages'}
            size = self.queue.prepend(messages)
            self._dispatch_wake.set()
            return {
                'status': 'queued',
                'queue_size': size,
                'queue_contents': self.queue.get_contents()
            }

        elif msg_type == 'direct':
            self._send_to_cli(message)
            self.queue.track_sent(message)
            return {'status': 'sent'}

        elif msg_type == 'select_option':
            # Select an option in a permission/question dialog.
            current = self.state.current_state
            if current not in PROMPT_STATES:
                return {
                    'status': 'error',
                    'error': f'not in permission/input state (state={current})',
                }
            try:
                option_num = int(message)
            except (ValueError, TypeError):
                return {'status': 'error', 'error': 'invalid option number'}
            if option_num < 1:
                return {'status': 'error', 'error': 'option must be >= 1'}

            # Parse the actual menu options using the provider's regex.
            prompt = self.state.get_prompt_output()
            options = extract_menu_options(prompt, self._provider)
            options_dict = {num: label for num, label in options}

            # Delegate option selection to the provider (handles
            # CLI-specific behaviors like arrow-key nav, y/n, etc.)
            # Call on_send() only after the provider confirms it will
            # actually send something — on_send() irreversibly clears
            # state tracker buffers.
            # Wrap in ``_run_dialog_action`` so the user's typed-but-
            # unsubmitted text in the composer is preserved across the
            # action — see the helper's docstring for the leak vectors
            # this covers.
            result = self._run_dialog_action(
                lambda: self._provider.select_option(
                    option_num, options_dict,
                    self.pty.send, self.pty.sendline,
                ),
                target_is_composer=False,
            )
            if result.get('status') != 'error':
                self.state.on_send()
            return result

        elif msg_type == 'custom_answer':
            # Send free-form text to a question dialog.
            current = self.state.current_state
            if current not in PROMPT_STATES:
                return {
                    'status': 'error',
                    'error': f'not in permission/input state (state={current})',
                }
            prompt = self.state.get_prompt_output()
            options = extract_menu_options(prompt, self._provider)
            options_dict = {num: label for num, label in options}

            # ``custom_answer_targets_composer`` is True for CLIs that
            # type the answer directly into the composer (Codex /
            # Gemini / Cursor) — pre-clear the composer so the answer
            # doesn't concatenate onto the user's typed text.  False
            # for Claude (navigates to "Type something" menu option
            # whose Ink subdialog absorbs the chars) — post-clear
            # mode handles any trailing-CR leak defensively.
            result = self._run_dialog_action(
                lambda: self._provider.send_custom_answer(
                    message, options_dict, self.pty.send,
                ),
                target_is_composer=self._provider.custom_answer_targets_composer,
            )
            if result.get('status') != 'error':
                self.state.on_send()
            return result

        elif msg_type == 'status':
            state = self.state.get_state(self.pty.is_alive())
            recently_sent, total_sent = self.queue.get_recently_sent()
            return {
                'queue_size': self.queue.size,
                'queue_contents': self.queue.get_contents(),
                'recently_sent': recently_sent,
                'total_sent': total_sent,
                'ready': self.state.is_ready_for_state(state),
                'cli_state': state,
                'auto_send_mode': self.state.auto_send_mode,
                'cli_running': self.pty.is_alive(),
                'slack_enabled': self.output_capture.is_enabled(),
                'cli_provider': self._provider.name,
            }

        elif msg_type == 'set_slack':
            enabled = msg.get('enabled', False)
            self.output_capture.set_enabled(enabled)
            if enabled:
                # Write current state so the Slack watcher can post context
                current_state = self.state.current_state
                prompt_output = self.state.get_prompt_output()
                self.output_capture.write_current_state(
                    current_state, not self.queue.is_empty, prompt_output,
                )
            return {'status': 'ok', 'slack_enabled': enabled}

        elif msg_type == 'force_send':
            message = self.queue.pop()
            if message:
                self._send_to_cli(message)
                self.queue.track_sent(message)
                return {
                    'status': 'sent',
                    'message': message,
                    'queue_size': self.queue.size
                }
            return {'status': 'empty', 'queue_size': 0}

        elif msg_type == 'get_message':
            index = msg.get('index', -1)
            msg_data = self.queue.get_message_by_index(index)
            if msg_data:
                return {
                    'status': 'ok',
                    'id': msg_data['id'],
                    'message': msg_data['msg']
                }
            return {'status': 'error', 'message': 'Invalid index'}

        elif msg_type == 'edit_message':
            msg_id = msg.get('id', '')
            new_message = msg.get('new_message', '')
            if self.queue.edit_message_by_id(msg_id, new_message):
                return {'status': 'ok', 'message': 'Message edited'}
            return {'status': 'error', 'message': 'Message not found (already sent or invalid ID)'}

        elif msg_type == 'delete_message':
            msg_id = msg.get('id', '')
            if self.queue.delete_message_by_id(msg_id):
                return {'status': 'ok', 'message': 'Message deleted'}
            return {'status': 'error', 'message': 'Message not found (already sent or invalid ID)'}

        elif msg_type == 'reorder_queue':
            ordered_ids = msg.get('ordered_ids', [])
            if not ordered_ids or not isinstance(ordered_ids, list):
                return {'status': 'error', 'message': 'ordered_ids list required'}
            self.queue.reorder_by_ids(ordered_ids)
            return {'status': 'ok', 'message': 'Queue reordered'}

        elif msg_type == 'get_queue_details':
            return {'status': 'ok', 'messages': self.queue.get_details()}

        elif msg_type == 'clear_queue':
            self.queue.clear()
            return {
                'status': 'ok',
                'queue_size': 0,
                'queue_contents': [],
            }

        elif msg_type == 'set_auto_send_mode':
            mode = msg.get('mode', '')
            if mode not in (AutoSendMode.PAUSE, AutoSendMode.ALWAYS):
                return {'status': 'error', 'message': f"Invalid mode: {mode}. Use 'pause' or 'always'."}
            self.state.auto_send_mode = mode
            # Per-session toggle MUST NOT touch the global default —
            # the Claude PermissionRequest hook re-reads from disk on
            # every tool call and falls back to global when the pin
            # lacks ``auto_send_mode``, so writing global here would
            # silently flip auto-approve behavior in every other open
            # session that hadn't been individually toggled.  Global
            # writes are reserved for the Settings dialog ("Default for
            # new sessions").
            self._save_pinned_auto_send_mode(self.tag, mode)
            # If switching to ALWAYS while already at a permission
            # prompt, auto-approve immediately rather than waiting
            # for the next auto-sender loop iteration.
            if (
                mode == AutoSendMode.ALWAYS
                and self.state.current_state == CLIState.NEEDS_PERMISSION
            ):
                self._try_auto_approve()
            return {'status': 'ok', 'auto_send_mode': mode}

        elif msg_type == 'interrupt':
            self.state.on_input(b'\x1b')
            self.pty.send('\x1b')
            return {'status': 'sent'}

        elif msg_type == 'get_prompt':
            return {
                'status': 'ok',
                'prompt_output': self.state.get_prompt_output(),
            }

        elif msg_type == 'shutdown':
            # Use a thread so we can return the response before exiting.
            # Sends SIGTERM to our own process, which the main thread catches
            # and triggers atexit cleanup (stops socket, terminates PTY, removes files).
            threading.Thread(
                target=lambda: (time.sleep(0.1), os.kill(os.getpid(), signal.SIGTERM)),
                daemon=True,
            ).start()
            return {'status': 'ok'}

        return {'status': 'error', 'message': f"Unknown message type: {msg_type}"}

    def _send_to_cli(self, message: str) -> None:
        """
        Send a message to the CLI.

        Uses the provider to determine whether a message needs special
        handling (e.g. image attachments).

        Args:
            message: Message to send.
        """
        # Only reset outside capture mode — if _send_to_cli fires for an
        # older queued message while the user is still in capture mode
        # (auto-sender + Enter held in _queue_sending_held), clobbering
        # this count would prevent _clear_stale_cli_input from running
        # when the Enter is replayed, leaving pre-capture text on the CLI.
        if not self._queue_capture_mode:
            self._capture_stale_visual_rows = 0
            self._capture_stale_logical_lines = 0

        # Pop per-message clear flag (set by Enter handler when
        # stale text exists during RUNNING).  This fixes the bug
        # where a global flag was consumed by the wrong message.
        needs_clear = (self._send_clear_queue.pop(0)
                       if self._send_clear_queue else False)
        # Block the input filter for the entire send sequence so user
        # keystrokes can't interleave with the clear/paste/Enter writes
        # and can't modify _terminal_input_buf while we snapshot it.
        # ``_action_lock`` serializes against ``_try_auto_approve`` and
        # the remote ``select_option`` / ``custom_answer`` paths so
        # concurrent paths don't toggle ``_queue_sending`` independently.
        self._action_lock.acquire()
        self._queue_sending = True
        try:
            # Also clear if user typed after capture exit.
            if self._terminal_input_buf:
                needs_clear = True
                # Preserve the user's partial input so it can be restored
                # after the queued message is sent.  Only snapshot on
                # the first interruption — subsequent queue sends should
                # not overwrite the original text.
                if not self._preserved_input_buf:
                    self._preserved_input_buf = bytearray(
                        self._terminal_input_buf)
                    self._preserved_chars_sent = self._chars_sent_to_cli
            self._terminal_input_buf.clear()
            self._terminal_input_cursor = 0

            if needs_clear:
                # Compute logical lines + visual rows of the user's
                # preserved text so multi-line pastes are fully
                # cleared from the CLI input box (placeholders
                # expanded via _paste_text_map).  Fall back to 1/1
                # when no preserved buffer is available — extra
                # Ctrl+Us / Backspaces on empty input are no-ops.
                buf = self._preserved_input_buf
                lines = self._stale_logical_lines(buf) or 1
                rows = self._stale_visual_rows(buf) or 1
                self._clear_stale_cli_input(lines, rows)
                time.sleep(0.1)

            # Suppress from here — hides message echo only.
            self._suppress_send_until = float('inf')

            self.state.on_send()

            is_img = self._provider.is_image_message(message) or self._has_image_ref(message)
            try:
                if is_img:
                    self.pty.send_image_message(message)
                elif '\n' in message or '\r' in message:
                    # Multi-line content — wrap in bracketed paste
                    # markers so the CLI treats \n as literal paste
                    # content, not a submit-Enter per line.  Strip any
                    # embedded paste markers first so nested wraps
                    # don't confuse Claude's Ink parser.  The combined
                    # send-paste-and-submit waits for Ink's placeholder
                    # render to settle before \r — otherwise the
                    # post-submit chat-history render can race the
                    # suppression lift and disappear from the user's
                    # view (model still gets the message).
                    sanitized = message.replace(
                        '\x1b[200~', '').replace('\x1b[201~', '')
                    self.pty.send_paste_and_submit(
                        '\x1b[200~' + sanitized + '\x1b[201~')
                else:
                    self.pty.sendline(message)
            finally:
                self._suppress_send_until = 0.0

            # Restore user's partial text immediately after the send so
            # they can keep editing while the CLI processes the message.
            if self._preserved_input_buf:
                self._restore_preserved_input()
        finally:
            self._queue_sending = False
            self._action_lock.release()
        # Drop any paste-map entries the message and live buffers
        # no longer reference — caps the dict's memory footprint
        # over long-running sessions.
        self._gc_paste_text_map()
        # Reset history-recall state — the just-dispatched message is
        # now on disk, so the next ↑ must re-read the provider's
        # history file to pick it up.  The pre-recall snapshot also
        # needs to be re-taken from whatever ``_restore_preserved_input``
        # just put back (or from the empty buf when no partial text
        # was preserved).
        self._reset_history_recall()
        # Clear the capture-mode gate-bypass flag — it was only
        # meant to cover the dispatch we just performed.
        self._capture_force_dispatch = False
        # Fire the deferred capture-exit SIGWINCH now that the
        # message is dispatched — ``_capture_flush(defer_sigwinch=True)``
        # set this flag to keep the resize-driven Ink full repaint
        # out of the paste-and-submit window.
        if self._pending_sigwinch:
            self._pending_sigwinch = False
            self._trigger_sigwinch_repaint()

    def _restore_preserved_input(self) -> None:
        """Restore user's partial input that was interrupted by a queue send.

        Types the preserved text back into the CLI's input line right
        after the queue message is sent, so the user can continue
        editing while the CLI processes.

        Multi-line text is wrapped in bracketed-paste markers so Ink
        treats the whole payload as a single paste rather than
        submitting once per ``\\n``.  Pre-existing markers are stripped
        first so a payload that already contains them doesn't break
        the framing.  This mirrors the technique ``_send_to_cli``
        already uses for multi-line queue messages.
        """
        text = self._preserved_input_buf.decode('utf-8', errors='replace')
        chars = self._preserved_chars_sent
        self._preserved_input_buf.clear()
        self._preserved_chars_sent = 0
        if not text:
            return
        self._terminal_input_buf = bytearray(text.encode('utf-8'))
        self._terminal_input_cursor = len(self._terminal_input_buf)
        self._chars_sent_to_cli = chars
        if '\n' in text or '\r' in text:
            sanitized = text.replace(
                '\x1b[200~', '').replace('\x1b[201~', '')
            payload = '\x1b[200~' + sanitized + '\x1b[201~'
        else:
            payload = text
        try:
            self.pty.send(payload)
        except OSError:
            pass

    def _reset_history_recall(self) -> None:
        """Reset CLI history-recall state.

        Called on Enter, Ctrl+C, queue-message dispatch, and other
        paths that clear ``_terminal_input_buf`` back to a fresh
        state.  Forces the next ↑ to re-read the provider's history
        file (so a just-submitted message shows up immediately) and
        to re-snapshot the live buffer as the new pre-recall anchor.
        """
        self._cli_history_cache = None
        self._cli_history_index = -1
        self._pre_recall_text = b''

    def _handle_history_recall(self, direction: int) -> bool:
        """Drive CLI input-history recall in response to a ↑/↓ keypress.

        Returns ``True`` when Leap has fully taken over — the caller
        must NOT forward the original ↑/↓ escape to the CLI.  Returns
        ``False`` to fall through to the legacy passthrough (CLI
        handles recall natively, but ``_terminal_input_buf`` will be
        out of sync — a subsequent ``^^`` snapshots an empty buffer).

        The cache is loaded lazily on the first ↑/↓ after every reset
        (``_reset_history_recall``).  The first ↑ also snapshots the
        live buffer into ``_pre_recall_text`` so ↓ past the newest
        entry restores it — same behavior as readline / Ink / Ratatui.

        Clearing + injection reuses ``_clear_stale_cli_input`` and the
        bracketed-paste-wrap technique from ``_restore_preserved_input``,
        so multi-line history entries (e.g. Claude's recalled pasted
        snippets) re-enter the input box as a single paste rather
        than triggering one submit per ``\\n``.

        Args:
            direction: ``-1`` for ↑ (older), ``+1`` for ↓ (newer).
        """
        # Bail out if a capture-cancel bg thread is mid-send: it's
        # about to write pty bytes (and to flip ``_queue_sending``),
        # and racing it from here would interleave our clear+inject
        # with its re-type of the cancelled text — the CLI would
        # see a garbled mix.  Consume the keypress so the CLI
        # doesn't see it either; the user can re-press ↑ after the
        # cancel settles (typically <300ms).
        if self._capture_cancel_pending:
            return True

        # A held ``^`` was waiting for a second caret to enter
        # capture mode.  The user pressed ↑ instead — their intent
        # is to recall history, not to keep the lone ``^``.  Cancel
        # the timer so it doesn't fire later and inject a stray
        # ``^`` after our recalled text lands on the CLI input line.
        if self._pending_caret:
            if self._pending_caret_timer is not None:
                self._pending_caret_timer.cancel()
                self._pending_caret_timer = None
            self._pending_caret = False

        if self._cli_history_cache is None:
            try:
                history = self._provider.input_history(self._cwd)
            except Exception:
                history = None
            if history is None:
                return False  # provider opted out — passthrough
            # Defensive: a custom provider that returns a str (or any
            # non-list sequence) would slip through ``cache[idx]``
            # giving single characters as "history entries".  Reject
            # anything that isn't a list and fall back to passthrough.
            if not isinstance(history, list):
                return False
            self._cli_history_cache = history
        cache = self._cli_history_cache
        if not cache:
            # Provider supports history but cwd has none — match the
            # CLI's own no-op behavior (consume so ↑↓ don't reach it
            # and trigger something unrelated).
            return True

        if self._cli_history_index < 0:
            # First recall after reset.  ↓ at idle has nowhere newer
            # to go — consume silently.  ↑ snapshots the live buffer
            # as the "after-newest" anchor and seeds the index.
            if direction > 0:
                return True
            self._pre_recall_text = bytes(self._terminal_input_buf)
            self._cli_history_index = len(cache)

        new_index = max(0, min(self._cli_history_index + direction, len(cache)))
        if new_index == self._cli_history_index:
            return True  # hit boundary — consume, no visible change
        self._cli_history_index = new_index

        if new_index == len(cache):
            new_text = self._pre_recall_text.decode('utf-8', errors='replace')
        else:
            new_text = cache[new_index]

        # Strip embedded bracketed-paste markers BEFORE the mirror /
        # CLI updates.  A history entry containing literal
        # ``\x1b[200~ ... \x1b[201~`` would otherwise (a) confuse
        # the CLI's TUI by triggering an unintended paste mode and
        # (b) leave the mirror with the literal markers while the
        # CLI's input box only shows the inner text — visually
        # divergent, and ``^^`` would then snapshot a buffer that
        # doesn't match what the user sees.
        new_text = new_text.replace(
            '\x1b[200~', '').replace('\x1b[201~', '')

        # Clear whatever's currently in the CLI's input box.  Use the
        # CURRENT mirror — passing it explicitly because the helpers
        # default to ``_capture_pre_input_buf`` (a snapshot taken
        # only inside capture mode), which is unrelated to what's
        # on the input line outside capture.  Without the explicit
        # arg, a multi-line recalled entry held in the mirror would
        # be undercounted (0 → defaulted to 1), leaving residue on
        # subsequent ↑↓ navigation.  Placeholders are expanded via
        # ``_paste_text_map`` inside ``_stale_buf_text``.  Fall back
        # to 1/1 for empty input (extra clears are no-ops).
        lines = self._stale_logical_lines(self._terminal_input_buf) or 1
        rows = self._stale_visual_rows(self._terminal_input_buf) or 1
        self._clear_stale_cli_input(lines, rows)

        # Update the mirror — cursor at end matches what the CLI shows
        # after its own ↑ recall (cursor parks at end of recalled text).
        text_bytes = new_text.encode('utf-8')
        self._terminal_input_buf = bytearray(text_bytes)
        self._terminal_input_cursor = len(self._terminal_input_buf)
        # Match the printable-byte convention used by the normal
        # keystroke path (line ~3499): only bytes 0x20-0x7e and 0x80+
        # bump ``_chars_sent_to_cli``.  Newlines aren't counted —
        # they don't occupy a horizontal cell on the CLI input line.
        self._chars_sent_to_cli = sum(
            1 for b in text_bytes
            if (0x20 <= b < 0x7f) or b >= 0x80
        )

        if new_text:
            # Multi-line content wraps in bracketed-paste markers so
            # the CLI treats ``\n`` as paste content, not a submit
            # per line.  Markers were already stripped from
            # ``new_text`` above, so the wrap can't nest.
            if '\n' in new_text or '\r' in new_text:
                payload = '\x1b[200~' + new_text + '\x1b[201~'
            else:
                payload = new_text
            try:
                self.pty.send(payload)
            except OSError:
                pass
        return True

    def _run_dialog_action(
        self,
        fn: Any,
        *,
        target_is_composer: bool,
    ) -> Any:
        """Run a dialog-answering action with full input preservation.

        Wraps ``fn`` (which sends keystrokes to answer a permission /
        question dialog) so the user's typed-but-unsubmitted text in
        ``_terminal_input_buf`` survives the action — even in the
        worst case where the action's keystrokes leak past the dialog
        and submit the user's text as a message.

        Two modes, controlled by *target_is_composer*:

        * **target_is_composer=False** (menu-targeted answer — auto-
          approve, Claude's numbered ``select_option``, Slack/Monitor
          digit reply, Claude's "Type something" navigation):
          The dialog has focus, so a pre-clear (Ctrl+E + Ctrl+U) would
          be absorbed by the dialog and not affect the composer.
          Snapshot first, run *fn*, then **post-clear** the composer
          (now reactivated after the dialog dismissed) and re-type the
          snapshot.  Works in both leak and no-leak cases:

          - No leak: composer still has the user's text → post-clear
            empties it → re-type restores.  Net: user's text intact.
          - Leak: trailing ``\\r`` submitted the text → composer is
            empty → post-clear is a no-op → re-type restores.  Net:
            agent received the message once (unrecoverable), but the
            composer view is restored so the user can re-edit.

        * **target_is_composer=True** (composer-targeted answer —
          Codex/Gemini/Cursor's ``send_custom_answer`` types directly
          into the composer):
          The composer has focus.  **Pre-clear** removes the user's
          typed text BEFORE *fn* types its answer, so the answer is
          submitted cleanly without prepending to the user's input.
          Re-type the snapshot afterwards.

        ``_action_lock`` serializes against ``_send_to_cli`` so two
        paths don't toggle ``_queue_sending`` independently.

        Error path (``result['status'] == 'error'``): providers signal
        this *before* writing keystrokes, so the composer is unchanged.
        For ``target_is_composer=True`` we already pre-cleared the
        composer (the answer was supposed to type into it), so we
        physically restore.  For ``target_is_composer=False`` we never
        touched the composer, so we just sync the mirror back to what
        we snapshotted — no flash, no re-type.
        """
        with self._action_lock:
            self._queue_sending = True
            try:
                snapshot_taken = False
                if (self._terminal_input_buf
                        and not self._preserved_input_buf):
                    self._preserved_input_buf = bytearray(
                        self._terminal_input_buf)
                    self._preserved_chars_sent = self._chars_sent_to_cli
                    snapshot_taken = True

                if target_is_composer and self._preserved_input_buf:
                    buf = self._preserved_input_buf
                    lines = self._stale_logical_lines(buf) or 1
                    rows = self._stale_visual_rows(buf) or 1
                    self._clear_stale_cli_input(lines, rows)
                    time.sleep(0.1)

                self._terminal_input_buf.clear()
                self._terminal_input_cursor = 0

                result = fn()

                if result.get('status') == 'error':
                    if target_is_composer:
                        # Pre-clear ran; composer is empty.  Restore
                        # physically so the user's text reappears.
                        if self._preserved_input_buf:
                            self._restore_preserved_input()
                    elif snapshot_taken and self._preserved_input_buf:
                        # Menu-target error: composer is unchanged
                        # (we never sent any keystrokes that could
                        # affect it), but our mirror was cleared.
                        # Sync mirror back to the snapshot — no flash.
                        self._terminal_input_buf = bytearray(
                            self._preserved_input_buf)
                        self._terminal_input_cursor = len(
                            self._terminal_input_buf)
                        self._chars_sent_to_cli = self._preserved_chars_sent
                        self._preserved_input_buf.clear()
                        self._preserved_chars_sent = 0
                    return result

                if (not target_is_composer
                        and snapshot_taken
                        and self._preserved_input_buf):
                    # Post-clear: handles both the no-leak case
                    # (composer still holds the user's text) and the
                    # leak case (trailing CR submitted it; composer is
                    # empty).  Either way, clear + retype yields a
                    # consistent final state.
                    #
                    # Small delay first: ``fn`` returns as soon as the
                    # last byte hits the PTY, but the CLI may still be
                    # processing the dismissal — Ctrl+E + Ctrl+U fired
                    # too early would be absorbed by the still-focused
                    # dialog (no-op for the composer), then the dialog
                    # would dismiss with the composer's text intact,
                    # and our subsequent re-type would land on top of
                    # it → "hellohello".  150 ms is comfortably longer
                    # than typical Ink dismissal (~30–80 ms).
                    time.sleep(0.15)
                    buf = self._preserved_input_buf
                    lines = self._stale_logical_lines(buf) or 1
                    rows = self._stale_visual_rows(buf) or 1
                    self._clear_stale_cli_input(lines, rows)
                    time.sleep(0.1)

                if self._preserved_input_buf:
                    self._restore_preserved_input()

                return result
            finally:
                self._queue_sending = False

    def _try_auto_approve(self) -> bool:
        """Try to auto-approve a permission prompt (Always-send mode).

        For CLIs with numbered menus (Claude), finds an exact ``Yes`` or
        ``yes`` option label.  For CLIs without numbered menus (Codex,
        Gemini, Cursor Agent), selects option 1 (the approve action).

        Returns:
            True if the permission was successfully approved.
        """
        prompt = self.state.get_prompt_output()
        options = extract_menu_options(prompt, self._provider)

        if options:
            # Numbered menu: pick the option whose label is "Yes" when
            # reduced to letters only.  Tolerates pyte snapshots where
            # overlapping TUI frames inject non-letter junk into the
            # cells around the label (spaces, punctuation, box-drawing
            # chars).  Critically, broader options like "Yes, allow all
            # edits during this session" reduce to "Yesallowall…" — NOT
            # "yes" — so they are correctly rejected: auto-approve must
            # only pick the narrow-scope "Yes".
            yes_num: Optional[int] = None
            for num, label in options:
                letters = ''.join(c for c in label if c.isalpha())
                if letters.lower() == 'yes':
                    yes_num = num
                    break
            if yes_num is None:
                return False
            options_dict = {num: lbl for num, lbl in options}
        elif not self._provider.has_numbered_menus:
            # Non-menu CLI (Codex y/n, Gemini/Cursor radio): option 1 = approve.
            yes_num = 1
            options_dict = {}
        else:
            # Has menus but none found yet (prompt still rendering).
            return False

        result = self._run_dialog_action(
            lambda: self._provider.select_option(
                yes_num, options_dict, self.pty.send, self.pty.sendline,
            ),
            target_is_composer=False,
        )
        if result.get('status') != 'error':
            self.state.on_send()
            return True
        return False

    def _has_image_ref(self, message: str) -> bool:
        """Check if the message contains any @path refs to .storage/queue_images/.

        Catches image references that aren't at the start of the message
        (which ``is_image_message`` would miss). Checked for all providers
        so that image messages always use the fixed-sleep send protocol.
        """
        prefix = self._provider.image_prefix
        images_dir = str(QUEUE_IMAGES_DIR)
        for token in message.split():
            if not token.startswith(prefix):
                continue
            path_part = token[len(prefix):]
            try:
                if os.path.realpath(path_part).startswith(images_dir):
                    return True
            except (OSError, ValueError):
                pass
        return False

    @staticmethod
    def _cleanup_old_images() -> None:
        """Delete images in .storage/queue_images/ not referenced anywhere.

        Called once on server startup.  Also migrates legacy
        ``@note_images/`` references in presets and queue files to
        ``@queue_images/`` (copies the file, rewrites the path) so that
        note-image cleanup never breaks presets or queued messages.
        """
        QUEUE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

        queue_dir_str = str(QUEUE_IMAGES_DIR)
        note_dir_str = str(NOTE_IMAGES_DIR)

        referenced: set[str] = set()

        def _collect_refs(text: str) -> None:
            """Find all @<QUEUE_IMAGES_DIR>/... references in *text*."""
            for token in text.split():
                at_idx = token.find('@')
                if at_idx < 0:
                    continue
                path_part = token[at_idx + 1:]
                if path_part.startswith(queue_dir_str):
                    referenced.add(path_part)

        def _migrate_note_refs(text: str) -> str:
            """Rewrite ``@<NOTE_IMAGES_DIR>/file`` → ``@<QUEUE_IMAGES_DIR>/file``.

            Copies each image file on first encounter, then does a bulk
            string replacement.  Returns the original *text* object (same
            identity) when no legacy references are found so callers can
            use ``is not`` for a cheap changed-check.
            """
            note_prefix = '@' + note_dir_str + '/'
            if note_prefix not in text:
                return text
            # Copy referenced image files before rewriting paths
            search_start = 0
            while True:
                pos = text.find(note_prefix, search_start)
                if pos < 0:
                    break
                # Extract the full path (everything after @ until whitespace)
                path_start = pos + 1  # skip @
                path_end = path_start
                while path_end < len(text) and not text[path_end].isspace():
                    path_end += 1
                full_path = text[path_start:path_end]
                filename = full_path[len(note_dir_str) + 1:]
                src = Path(full_path)
                dst = QUEUE_IMAGES_DIR / filename
                if src.is_file() and not dst.exists():
                    try:
                        shutil.copy2(str(src), str(dst))
                    except OSError:
                        pass
                search_start = path_end
            # Bulk-replace the directory prefix (preserves all whitespace)
            queue_prefix = '@' + queue_dir_str + '/'
            return text.replace(note_prefix, queue_prefix)

        # ── Migrate + collect refs from queue files ──────────────────
        if QUEUE_DIR.is_dir():
            for queue_file in QUEUE_DIR.iterdir():
                if not queue_file.suffix == '.queue':
                    continue  # skip .tmp and other non-queue files
                try:
                    content = queue_file.read_text()
                    migrated = _migrate_note_refs(content)
                    if migrated is not content:
                        queue_file.write_text(migrated)
                    _collect_refs(migrated)
                except OSError:
                    pass

        # ── Migrate + collect refs from presets JSON ─────────────────
        presets_file = STORAGE_DIR / 'leap_presets.json'
        if presets_file.is_file():
            try:
                data = json.loads(presets_file.read_text())
                presets_changed = False
                if isinstance(data, dict):
                    for name, messages in data.items():
                        if not isinstance(messages, list):
                            continue
                        for j, msg in enumerate(messages):
                            if not isinstance(msg, str):
                                continue
                            migrated = _migrate_note_refs(msg)
                            if migrated is not msg:
                                messages[j] = migrated
                                presets_changed = True
                            _collect_refs(migrated)
                    if presets_changed:
                        atomic_write_json(presets_file, data,
                                          ensure_ascii=False)
            except (OSError, ValueError):
                pass

        # ── Collect refs from saved messages history ───────────────────
        saved_file = STORAGE_DIR / 'saved_messages.json'
        if saved_file.is_file():
            try:
                saved = json.loads(saved_file.read_text())
                if isinstance(saved, list):
                    for msg in saved:
                        if isinstance(msg, str):
                            _collect_refs(msg)
            except (OSError, ValueError):
                pass

        # ── Delete unreferenced images ───────────────────────────────
        for entry in QUEUE_IMAGES_DIR.iterdir():
            try:
                if entry.is_file() and str(entry) not in referenced:
                    entry.unlink()
            except OSError:
                pass

    def _prompt_load_old_queue(self) -> None:
        """Prompt user to load or discard old queued messages."""
        print(f"\n\u26a0\ufe0f  Found {self.queue.size} unsent message{'s' if self.queue.size != 1 else ''} from previous session:\n")

        # Show preview (first 60 chars of each message)
        preview_count = min(5, self.queue.size)
        contents = self.queue.get_contents()

        for i in range(preview_count):
            msg_with_id = contents[i]
            # Extract message part (after "id> ")
            msg_start = msg_with_id.find('> ')
            if msg_start != -1:
                msg = msg_with_id[msg_start + 2:]
            else:
                msg = msg_with_id

            msg_preview = msg[:60]
            if len(msg) > 60:
                msg_preview += "..."
            print(f"  [{i}] {msg_preview}")

        if self.queue.size > preview_count:
            print(f"  ... and {self.queue.size - preview_count} more")

        print("\nLoad these messages? [Y/n/d] (Y=load, n=discard, d=show full): ", end='', flush=True)

        try:
            response = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            response = 'y'  # Default to loading

        if response == 'd':
            # Show full details
            self._show_queue_details()
            print("\nLoad these messages? [Y/n]: ", end='', flush=True)
            try:
                response = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                response = 'y'

        if response == 'n':
            # Discard queue
            self.queue.clear()
            print("\u2713 Discarded old messages\n")
        else:
            # Load queue (default)
            print(f"\u2713 Loaded {self.queue.size} message{'s' if self.queue.size != 1 else ''}\n")

    def _show_queue_details(self) -> None:
        """Show full queue contents."""
        print("\n" + "=" * 70)
        print("Full message queue:")
        print("=" * 80)
        contents = self.queue.get_contents()
        for i, msg_with_id in enumerate(contents):
            # Extract ID and message
            msg_start = msg_with_id.find('> ')
            if msg_start != -1:
                msg_id = msg_with_id[1:msg_start]  # Skip leading '<'
                msg = msg_with_id[msg_start + 2:]
            else:
                msg_id = "unknown"
                msg = msg_with_id

            print(f"\n[{i}] <{msg_id}>")
            print(f"    {msg}")
        print("=" * 80)

    def _cleanup_old_history_files(self) -> None:
        """Clean up history files older than configured TTL."""
        try:
            settings = load_settings()
            ttl_days = settings.get('history_ttl_days', 3)
            ttl_seconds = ttl_days * 24 * 60 * 60
            current_time = time.time()

            # Find all .history files
            if not HISTORY_DIR.exists():
                return

            for history_file in HISTORY_DIR.glob('*.history'):
                try:
                    # Check file age
                    file_mtime = history_file.stat().st_mtime
                    age_seconds = current_time - file_mtime

                    if age_seconds > ttl_seconds:
                        history_file.unlink()
                except OSError:
                    # Skip files we can't access
                    pass
        except Exception:
            # Don't fail startup if cleanup fails
            pass

    def _signal_file_has_response(self) -> bool:
        """Check if the signal file already contains last_assistant_message."""
        signal_file = SOCKET_DIR / f"{self.tag}.signal"
        try:
            if signal_file.exists():
                data = json.loads(signal_file.read_text())
                return bool(data.get('last_assistant_message'))
        except (json.JSONDecodeError, OSError):
            pass
        return False

    def _auto_sender_loop(self) -> None:
        """Background thread to auto-send queued messages."""
        prev_state = CLIState.IDLE
        # Delayed write for prompt/idle states: wait for TUI to finish
        # rendering (prompts) or for the hook to update the signal file
        # with the assistant message text (idle, for Slack).
        delayed_write_due: float = 0.0
        delayed_prev_state: str = ''
        delayed_queue_has_next: bool = False
        delayed_target_state: str = ''
        while self.running:
            # Wait for a producer's wake-up signal or for the periodic
            # state-poll timeout — whichever comes first.  Short wait
            # (200 ms) when we have queued work, long wait
            # (POLL_INTERVAL) when fully idle.  The short wait bounds
            # the worst-case impact of a wake-event race (a
            # producer's ``set()`` consumed by ``clear()`` between
            # ``wait()`` returning and ``clear()`` running) so
            # back-to-back queued messages still dispatch reasonably
            # fast.  200 ms is chosen to comfortably exceed the
            # ~60 ms SIGWINCH-completion delay used by capture-mode
            # ``signal_dispatch`` — without that margin, the
            # auto-sender's short-poll timeout can race-fire BEFORE
            # the SIGWINCH thread sets the wake-event, restoring the
            # paste-write/Ink-repaint race that the SIGWINCH-first
            # ordering exists to prevent.
            wait = 0.2 if not self.queue.is_empty else POLL_INTERVAL
            self._dispatch_wake.clear()
            self._dispatch_wake.wait(timeout=wait)

            try:
                current_state = self.state.get_state(self.pty.is_alive())

                # Detect state transitions for Slack output capture
                if current_state != prev_state:
                    # Cancel any pending delayed write on state change
                    delayed_write_due = 0.0
                    queue_has_next = (
                        not self.queue.is_empty
                        and current_state == CLIState.IDLE
                        and self.state.auto_send_mode in (AutoSendMode.PAUSE, AutoSendMode.ALWAYS)
                    )
                    # Any transition to IDLE means whatever real query
                    # was running (if any) has finished — clear the
                    # ``_query_in_flight`` flag so the dispatcher's
                    # next phantom-RUNNING check correctly reads as
                    # "no real query in flight".  Centralising the
                    # reset here avoids touching all ~10 call sites
                    # in state_tracker that flip to IDLE.
                    if current_state == CLIState.IDLE:
                        self.state._query_in_flight = False
                    if current_state in WAITING_STATES:
                        # Delay writing: let PTY output accumulate so the
                        # full permission dialog / input prompt is captured.
                        delayed_write_due = time.time() + 0.2
                        delayed_prev_state = prev_state
                        delayed_queue_has_next = queue_has_next
                        delayed_target_state = current_state
                    elif (
                        current_state == CLIState.IDLE
                        and prev_state == CLIState.RUNNING
                    ):
                        # Delay writing so the hook can populate the signal
                        # file with last_assistant_message.  If the signal
                        # file already has the response (e.g. transcript-
                        # based detection wrote it), use a short delay.
                        signal_has_response = self._signal_file_has_response()
                        delay = 0.2 if signal_has_response else 2.0
                        delayed_write_due = time.time() + delay
                        delayed_prev_state = prev_state
                        delayed_queue_has_next = queue_has_next
                        delayed_target_state = CLIState.IDLE
                    else:
                        self.output_capture.on_state_change(
                            current_state, prev_state, queue_has_next,
                        )
                    prev_state = current_state

                # Delayed Slack output write
                if delayed_write_due and time.time() >= delayed_write_due:
                    try:
                        cs = self.state.current_state
                        if delayed_target_state in WAITING_STATES and cs in WAITING_STATES:
                            prompt_output = self.state.get_prompt_output()
                            self.output_capture.on_state_change(
                                cs, delayed_prev_state,
                                delayed_queue_has_next, prompt_output,
                            )
                        elif delayed_target_state == CLIState.IDLE:
                            self.output_capture.on_state_change(
                                delayed_target_state, delayed_prev_state,
                                delayed_queue_has_next,
                            )
                    finally:
                        delayed_write_due = 0.0

                # Auto-approve permissions in Always-send mode.
                # Wait until delayed Slack write is flushed so the
                # prompt output is captured before on_send() clears it.
                if (
                    current_state == CLIState.NEEDS_PERMISSION
                    and self.state.auto_send_mode == AutoSendMode.ALWAYS
                    and not delayed_write_due
                ):
                    if self._try_auto_approve():
                        # on_send() moved state to RUNNING — update
                        # prev_state so the next idle transition is
                        # seen as running→idle (needed for Slack
                        # delayed-write to capture the response).
                        prev_state = CLIState.RUNNING
                    continue

                if self.queue.is_empty:
                    continue
                # Never dispatch through a permission / input prompt
                # or an interrupted state — those are real "waiting
                # for the user" states.  RUNNING is allowed when
                # ``_capture_force_dispatch`` is set (user typed
                # ^^ + Enter); otherwise stick to IDLE-only.
                if current_state in WAITING_STATES:
                    continue
                if (not self._capture_force_dispatch
                        and not self.state.is_ready_for_state(current_state)):
                    continue

                # Flush pending Slack write BEFORE sending the next
                # message — on_send() deletes the signal file, so the
                # output text would be lost if we wait.  The hook may
                # not have written last_assistant_message yet (< 2s),
                # but a partial capture is better than losing it.
                if delayed_write_due:
                    try:
                        if delayed_target_state == CLIState.IDLE:
                            self.output_capture.on_state_change(
                                delayed_target_state, delayed_prev_state,
                                delayed_queue_has_next,
                            )
                        elif delayed_target_state in WAITING_STATES:
                            cs = self.state.current_state
                            if cs in WAITING_STATES:
                                prompt_output = self.state.get_prompt_output()
                                self.output_capture.on_state_change(
                                    cs, delayed_prev_state,
                                    delayed_queue_has_next, prompt_output,
                                )
                    except Exception:
                        pass
                    delayed_write_due = 0.0

                message = self.queue.pop()
                if not message:
                    continue

                try:
                    self._send_to_cli(message)
                    self.queue.track_sent(message)
                except Exception as e:
                    print(f"Error sending to CLI, requeuing: {e}", file=sys.stderr, flush=True)
                    self.queue.requeue(message)
            except Exception:
                print(
                    "Error in auto-sender loop iteration:",
                    file=sys.stderr, flush=True,
                )
                traceback.print_exc(file=sys.stderr)

    def _title_keeper_loop(self) -> None:
        """Background thread to maintain terminal title.

        Skips the write when CLI output was received recently to avoid
        interleaving OSC escape sequences with the TUI rendering, which
        can corrupt colors and produce visual artefacts.
        """
        while self.running:
            if time.time() - self._last_output_time > 0.2:
                try:
                    set_terminal_title(f"lps {self.tag}", vscode_rename=False)
                except Exception:
                    pass
            time.sleep(TITLE_RESET_INTERVAL)

    def _stdin_watchdog_loop(self) -> None:
        """Background thread to detect when the terminal is closed.

        pexpect.spawn() creates a new PTY session, so the server may
        not receive SIGHUP when the original terminal tab is closed.
        Poll the original terminal fd to detect the loss and trigger
        a clean shutdown.
        """
        try:
            stdin_fd = sys.stdin.fileno()
        except (AttributeError, ValueError):
            return  # Not a real fd — nothing to watch
        while self.running:
            time.sleep(2)
            try:
                # tcgetpgrp raises OSError/EIO when the terminal is gone
                os.tcgetpgrp(stdin_fd)
            except OSError:
                os.kill(os.getpid(), signal.SIGTERM)
                return

    def _handle_resize(self, sig: int, frame: Any) -> None:
        """Handle terminal resize signal.

        Resize the PTY immediately so the child process (Claude, etc.)
        receives its own ``SIGWINCH`` right away and can redraw its TUI
        without waiting for I/O.  ``ioctl`` is async-signal-safe, so
        calling ``setwinsize`` from a signal handler is fine.

        The pyte virtual-terminal update (``state.on_resize``) is
        *deferred* because it acquires ``_screen_lock``, which may
        already be held by the main thread — acquiring a non-reentrant
        ``threading.Lock`` from the same thread deadlocks permanently.
        The flag is picked up by the next input/output filter cycle
        (which fires immediately once the child redraws).
        """
        # Resize the PTY immediately — the child gets SIGWINCH at once.
        try:
            cols, rows = shutil.get_terminal_size(fallback=(80, 24))
            if self.pty and self.pty.process:
                self.pty.process.setwinsize(rows, cols)
        except Exception:
            pass
        # Defer pyte screen update (needs lock).
        self._pending_resize = True

    def _apply_pending_resize(self) -> None:
        """Apply a deferred terminal resize (called outside signal context)."""
        if not self._pending_resize:
            return
        self._pending_resize = False
        try:
            cols, rows = shutil.get_terminal_size(fallback=(80, 24))
            self.state.on_resize(rows, cols)
            self.pty.resize(rows, cols)
        except Exception:
            pass

    def _capture_display(self, text: Optional[str] = None) -> None:
        """Show queue-capture buffer on the TUI's input line.

        Writes the text and positions the terminal cursor at the
        capture cursor position so the user sees where they're editing.
        Handles multi-line wrapping: tracks how many terminal lines the
        previous render occupied and clears them before redrawing.
        """
        try:
            # Move up and clear any wrapped lines from previous render
            clear = ''
            if self._capture_prev_lines > 0:
                clear = (f'\r\x1b[K'
                         + (f'\x1b[A\r\x1b[K' * self._capture_prev_lines))

            if text is None:
                # Hide cursor to prevent ghost cursors during the gap
                # between capture-end and the CLI's TUI repaint.
                hide = '\x1b[?25l'
                # The generic `clear` built above walks UP from the
                # cursor position, so it misses wrapped lines that lie
                # BELOW the cursor (e.g. after the user pressed Home).
                # Move down to the last wrapped line first so every
                # overlay line is erased.
                if self._capture_prev_lines > 0:
                    try:
                        cols = shutil.get_terminal_size(
                            fallback=(80, 24)).columns
                        cursor_abs = len('[Leap Q] ') + self._capture_cursor_pos
                        cursor_line = (cursor_abs // cols
                                       if cols > 0 else 0)
                        down_lines = self._capture_prev_lines - cursor_line
                        if down_lines > 0:
                            clear = (f'\x1b[{down_lines}B\r\x1b[K'
                                     + f'\x1b[A\r\x1b[K'
                                     * self._capture_prev_lines)
                    except Exception:
                        pass
                os.write(sys.stdout.fileno(),
                         (hide + (clear or '\r\x1b[K')).encode())
                self._capture_prev_lines = 0
            else:
                # Replace newlines (from pasted multi-line text) with a
                # visual marker for the single-line display.  The actual
                # capture buffer retains real newlines for the queued msg.
                text = text.replace('\n', '\u23ce')
                q_size = self.queue.size
                prefix = '[Leap Q] '
                hint = (f' \x1b[2m({q_size} queued \u2022 Enter=queue'
                        f' \u2022 !!=force-send next'
                        f' \u2022 Esc=cancel \u2022 ^^=save'
                        f' \u2022 \u2191\u2193=history \u2022 Ctrl+V=image'
                        f' \u2022 CLI runs in bg)\x1b[33m'
                        if self._capture_show_hint else '')
                full_line = f'{prefix}{text}{hint}'
                visible_len = len(re.sub(r'\x1b\[[0-9;]*m', '', full_line))
                cols = shutil.get_terminal_size(fallback=(80, 24)).columns
                wrapped = max(0, (visible_len - 1) // cols) if cols > 0 else 0
                # Position cursor correctly within wrapped text.
                # After writing the full line, the terminal cursor is
                # on the last wrapped line.  Move up to the cursor's
                # line and set the column within that line.
                cursor_abs = len(prefix) + self._capture_cursor_pos
                if cols > 0:
                    cursor_line = cursor_abs // cols
                    cursor_col = cursor_abs % cols
                else:
                    cursor_line = 0
                    cursor_col = cursor_abs
                lines_up = wrapped - cursor_line
                move_up = f'\x1b[{lines_up}A' if lines_up > 0 else ''
                move_right = f'\x1b[{cursor_col}C' if cursor_col > 0 else ''
                payload = (
                    f"{clear}\r\x1b[K"
                    f"\x1b[33m{prefix}{text}{hint}\x1b[0m"
                    f"{move_up}\r{move_right}"
                    f"\x1b[?25h"
                ).encode()
                os.write(sys.stdout.fileno(), payload)
                self._capture_prev_lines = wrapped
        except OSError:
            pass

    def _capture_text(self) -> str:
        """Decode the capture buffer as a string."""
        return self._queue_capture_buf.decode('utf-8', errors='replace')

    def _capture_insert(self, ch: str) -> None:
        """Insert character(s) at the cursor position."""
        self._saved_msg_index = -1  # editing resets history browsing
        text = self._capture_text()
        text = text[:self._capture_cursor_pos] + ch + text[self._capture_cursor_pos:]
        self._queue_capture_buf = bytearray(text.encode('utf-8'))
        self._capture_cursor_pos += len(ch)

    # -- Saved message history ------------------------------------------------

    _SAVED_MESSAGES_FILE = STORAGE_DIR / 'saved_messages.json'
    _SAVED_MESSAGES_MAX = 100

    def _load_saved_messages(self) -> list[str]:
        """Load saved messages from disk."""
        try:
            if self._SAVED_MESSAGES_FILE.exists():
                data = json.loads(self._SAVED_MESSAGES_FILE.read_text())
                if isinstance(data, list):
                    return data[-self._SAVED_MESSAGES_MAX:]
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _persist_saved_messages(self) -> None:
        """Write saved messages to disk."""
        try:
            atomic_write_json(
                self._SAVED_MESSAGES_FILE,
                self._saved_messages[-self._SAVED_MESSAGES_MAX:],
            )
        except OSError:
            pass

    def _save_capture_message(self) -> None:
        """Save current capture buffer to history, clear buffer."""
        msg = self._capture_text().strip()
        if not msg:
            return
        # Expand [Paste #N] placeholders FIRST — a recalled paste may
        # contain [Image #M] tokens inside its raw content, which the
        # subsequent image resolution must see.
        if self._paste_text_map:
            msg = self._capture_resolve_pastes(msg)
        # Resolve image placeholders → @path refs, preserving the
        # text-image interleaving so recalled messages read the way
        # the user typed them.
        if self._capture_image_map:
            msg = self._capture_resolve_images(msg)
        # Remove duplicate if already at the end
        if self._saved_messages and self._saved_messages[-1] == msg:
            pass
        else:
            self._saved_messages.append(msg)
            if len(self._saved_messages) > self._SAVED_MESSAGES_MAX:
                self._saved_messages = self._saved_messages[
                    -self._SAVED_MESSAGES_MAX:]
        self._persist_saved_messages()
        # Clear buffer and show saved hint
        self._queue_capture_buf.clear()
        self._capture_cursor_pos = 0
        self._capture_utf8_buf.clear()
        self._saved_msg_index = -1
        self._capture_show_hint = False
        self._capture_display()  # clear old wrapped lines
        self._capture_prev_lines = 0
        # NOTE: intentionally do NOT reset _capture_initial_text here.
        # After save, the buffer is empty but initial still holds the
        # pre-capture content.  cancel's ``was_edited`` check will see
        # capture != initial and run the slow path, which clears
        # Claude's CLI + sends the (empty / newly-typed) content —
        # exactly what the user wants for save+Esc and save+type+Esc.
        # Show a "Saved!" hint on the capture line
        try:
            payload = (
                '\r\x1b[K'
                '\x1b[33m[Leap Q] \x1b[32mSaved!'
                ' \x1b[2m(any key to continue \u2022 \u2191\u2193 to browse)\x1b[0m'
            ).encode()
            os.write(sys.stdout.fileno(), payload)
        except OSError:
            pass
        self._capture_show_saved_hint = True

    def _capture_display_force_confirm(self) -> None:
        """Display the force-send confirmation prompt."""
        try:
            payload = (
                '\r\x1b[K'
                '\x1b[33m[Leap Q] \x1b[0m'
                'Force-send next queued message'
                ' \x1b[2m- Enter to confirm • any key to cancel\x1b[0m'
            ).encode()
            os.write(sys.stdout.fileno(), payload)
        except OSError:
            pass

    def _browse_saved_history(self, direction: int) -> None:
        """Browse saved messages. direction: -1=up (older), +1=down (newer)."""
        if not self._saved_messages:
            return
        count = len(self._saved_messages)
        if self._saved_msg_index == -1:
            # Not browsing yet
            if direction == -1:
                # Start at most recent
                self._saved_msg_index = count - 1
            else:
                return  # Already past end, nothing to do
        else:
            new_idx = self._saved_msg_index + direction
            if new_idx < 0:
                return  # Already at oldest
            if new_idx >= count:
                # Past newest → back to empty buffer
                self._saved_msg_index = -1
                self._queue_capture_buf.clear()
                self._capture_cursor_pos = 0
                self._capture_display(self._capture_text())
                return
            self._saved_msg_index = new_idx

        # Load the message at current index, converting @path refs
        # back to [Image #N] placeholders and substantial multi-line
        # text into a [Paste #N] placeholder so browsing stays
        # scannable.  Original paste boundaries aren't preserved, so
        # a saved message containing multiple pastes collapses into a
        # single placeholder on recall.
        msg = self._saved_messages[self._saved_msg_index]
        msg = self._capture_unresolve_images(msg)
        msg = self._capture_unresolve_pastes(msg)
        self._queue_capture_buf = bytearray(msg.encode('utf-8'))
        self._capture_cursor_pos = len(msg)
        # Update initial text to the recalled content so cancel's
        # fast-path edit detection (``capture_text vs initial_text``)
        # compares against what the user sees after recall, not the
        # stale pre-recall state.  Without this, editing a recalled
        # message always falls to the slow clear+re-paste round-trip.
        self._capture_initial_text = self._capture_text()
        self._capture_display(self._capture_text())

    @staticmethod
    def _is_csi_u_cancel(seq: bytes) -> bool:
        """Check if a CSI sequence is Ctrl+C in kitty/xterm encoding."""
        return CLIStateTracker._is_csi_u_interrupt(seq)

    @staticmethod
    def _is_csi_u_paste(seq: bytes) -> bool:
        """Check if a CSI sequence is Ctrl+V in any known encoding."""
        if len(seq) < 4:
            return False
        final = seq[-1]
        params = seq[2:-1]
        parts = params.split(b';')
        try:
            if final == 0x75:  # Kitty: \x1b[118;5u
                cp = int(parts[0].split(b':')[0])
                mod = int(parts[1].split(b':')[0]) if len(parts) > 1 else 1
                return cp == 118 and (mod - 1) & 0x04 != 0
            if final == 0x7e and len(parts) >= 3:  # Legacy: \x1b[27;5;118~
                prefix = int(parts[0].split(b':')[0])
                mod = int(parts[1].split(b':')[0])
                keycode = int(parts[2].split(b':')[0])
                return prefix == 27 and keycode == 118 and (mod - 1) & 0x04 != 0
        except (ValueError, IndexError):
            pass
        return False

    @staticmethod
    def _is_csi_u_newline(seq: bytes) -> bool:
        """Check if a CSI sequence is Shift/Cmd+Enter (newline-in-input).

        Kitty: ``\\x1b[13;<mod>u`` with mod != 1 (mod 1 = no modifier
        i.e. plain Enter).  Legacy xterm: ``\\x1b[27;<mod>;13~``.
        These sequences are emitted by terminals (iTerm2, WezTerm,
        VS Code via the Leap extension) when CSI u keyboard
        encoding is active and the user wants to insert a newline
        in the CLI's input box without submitting.
        """
        if len(seq) < 4:
            return False
        final = seq[-1]
        params = seq[2:-1]
        parts = params.split(b';')
        try:
            if final == 0x75:  # Kitty: \x1b[13;<mod>u
                cp = int(parts[0].split(b':')[0])
                mod = int(parts[1].split(b':')[0]) if len(parts) > 1 else 1
                return cp == 13 and mod != 1
            if final == 0x7e and len(parts) >= 3:  # \x1b[27;<mod>;13~
                prefix = int(parts[0].split(b':')[0])
                mod = int(parts[1].split(b':')[0])
                keycode = int(parts[2].split(b':')[0])
                return prefix == 27 and keycode == 13 and mod != 1
        except (ValueError, IndexError):
            pass
        return False

    def _capture_backspace(self) -> bool:
        """Delete character before cursor. Returns False if at start.

        Treats ``[Paste #N]`` / ``[Image #N]`` placeholders atomically:
        if the cursor sits immediately after one, the whole token is
        removed in a single backspace — preventing users from breaking
        a placeholder by editing inside it.
        """
        if self._capture_cursor_pos <= 0:
            return False
        self._saved_msg_index = -1  # editing resets history browsing
        text = self._capture_text()
        ph_end = self._capture_cursor_pos
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                ph_start = ph_end - len(ph)
                if ph_start >= 0 and text[ph_start:ph_end] == ph:
                    text = text[:ph_start] + text[ph_end:]
                    self._queue_capture_buf = bytearray(
                        text.encode('utf-8'))
                    self._capture_cursor_pos = ph_start
                    return True
        text = text[:self._capture_cursor_pos - 1] + text[self._capture_cursor_pos:]
        self._queue_capture_buf = bytearray(text.encode('utf-8'))
        self._capture_cursor_pos -= 1
        return True

    def _capture_delete(self) -> None:
        """Delete character at cursor (forward delete).

        Atomic placeholder handling: if the cursor sits at the start
        of a ``[Paste #N]`` / ``[Image #N]`` token, delete the whole
        token as one operation.
        """
        text = self._capture_text()
        if self._capture_cursor_pos < len(text):
            self._saved_msg_index = -1  # editing resets history browsing
            ph_start = self._capture_cursor_pos
            for ph_map in (self._paste_text_map, self._capture_image_map):
                for ph in ph_map:
                    ph_end = ph_start + len(ph)
                    if text[ph_start:ph_end] == ph:
                        text = text[:ph_start] + text[ph_end:]
                        self._queue_capture_buf = bytearray(
                            text.encode('utf-8'))
                        return
            text = text[:self._capture_cursor_pos] + text[self._capture_cursor_pos + 1:]
            self._queue_capture_buf = bytearray(text.encode('utf-8'))

    def _gc_paste_text_map(self) -> None:
        """Drop ``_paste_text_map`` entries no longer referenced by
        any live buffer.

        Without this, the dict accumulates entries for the lifetime of
        the server — every paste >200 chars or with embedded newlines
        gets a permanent entry, so a long-running session leaks paste
        content.  Called at known-safe points (Enter / Ctrl+C outside
        capture, after each ``_send_to_cli``) where the live buffers
        have just settled, so anything not referenced is genuinely
        orphaned.
        """
        if not self._paste_text_map:
            return
        live = b''.join(
            bytes(b)
            for b in (
                self._terminal_input_buf,
                self._capture_pre_input_buf,
                self._preserved_input_buf,
                self._queue_capture_buf,
            )
        )
        for ph in list(self._paste_text_map.keys()):
            if ph.encode('utf-8') not in live:
                del self._paste_text_map[ph]

    @staticmethod
    def _line_cells(line: str) -> int:
        """Approximate terminal cell width of a single line of text.

        Uses ``unicodedata.east_asian_width`` to give CJK Wide/Fullwidth
        characters two cells.  Most emoji are classified Neutral and
        return 1 cell — that under-counts in the safe direction (the
        Ctrl+U clear over-shoots, extra presses are no-ops on an empty
        line), so a stdlib-only approximation is fine here without
        pulling in the ``wcwidth`` package.
        """
        cells = 0
        for ch in line:
            eaw = unicodedata.east_asian_width(ch)
            cells += 2 if eaw in ('F', 'W') else 1
        return cells

    def _stale_buf_text(
        self, buf: Optional[Union[bytes, bytearray]] = None,
    ) -> str:
        """Decoded buffer with ``[Paste #N]`` placeholders expanded.

        Defaults to ``_capture_pre_input_buf``.  Returns ``''`` for
        an empty buffer.  Snapshots ``_paste_text_map`` via ``list()``
        because other threads (input thread via
        ``_finalize_paste_capture``, auto-sender via
        ``_gc_paste_text_map``) can mutate it concurrently and
        iterating during mutation raises RuntimeError.
        """
        if buf is None:
            buf = self._capture_pre_input_buf
        if not buf:
            return ''
        text = bytes(buf).decode('utf-8', errors='replace')
        for placeholder, raw in list(self._paste_text_map.items()):
            text = text.replace(placeholder, raw)
        return text

    def _stale_visual_rows(
        self, buf: Optional[Union[bytes, bytearray]] = None,
    ) -> int:
        """Visual rows the CLI is rendering for the given input buffer.

        Counts wrapped rows per ``\\n``-separated line at the current
        terminal width.  Cell widths come from ``_line_cells`` so
        CJK Wide/Fullwidth chars contribute two cells each.

        Returns 0 for an empty buffer.
        """
        text = self._stale_buf_text(buf)
        if not text:
            return 0
        try:
            cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        except OSError:
            cols = 80
        cols = max(1, cols)
        rows = 0
        for line in text.split('\n'):
            line = line.rstrip('\r')
            cells = self._line_cells(line)
            rows += 1 + max(0, (cells - 1) // cols)
        return rows

    def _stale_logical_lines(
        self, buf: Optional[Union[bytes, bytearray]] = None,
    ) -> int:
        """Logical line count of stale CLI input (``\\n``-separated).

        Drives the Ctrl+U + Backspace pattern in
        ``_clear_stale_cli_input``: in IDLE, Ink's Ctrl+U is line-bound
        and does NOT cross newlines, so we need one Ctrl+U per logical
        line plus a Backspace between each pair to delete the joining
        ``\\n`` and place the cursor at the end of the previous line.

        Returns 0 for an empty buffer.
        """
        text = self._stale_buf_text(buf)
        if not text:
            return 0
        return text.count('\n') + 1

    def _clear_stale_cli_input(self, lines: int, rows: int) -> None:
        """Clear stale CLI input left on the TUI before ``^^`` entry.

        Sends ``End`` (cursor to end of input) followed by N
        back-to-back Ctrl+Us, where N covers both interpretations
        of Ink's Ctrl+U behavior plus safety:

        * If Ctrl+U is **line-bound and progresses cursor up after
          each kill**: N Ctrl+Us clears N logical lines.
        * If Ctrl+U is **row-bound** (RUNNING-mode streaming): N
          Ctrl+Us kills N visual rows.

        Either way, ``max(lines, rows) + 3`` Ctrl+Us is enough to
        clear everything plus margin.  Extra Ctrl+Us on empty input
        are no-ops in both Ink and Ratatui.

        RUNNING gets a second ``End`` for drop-defense against the
        streaming render race that can swallow the first.
        """
        if lines <= 0 and rows <= 0:
            return
        self.pty.send('\x1b[F')  # End: cursor to end of input
        time.sleep(0.02)
        if self.state.current_state != CLIState.IDLE:
            self.pty.send('\x1b[F')  # second End: drop-defense
            time.sleep(0.02)
        n = max(lines, rows) + 3
        self.pty.send('\x15' * n)
        time.sleep(0.03)

    def _trigger_sigwinch_repaint(self) -> None:
        """Force Ink to do an immediate full-screen repaint via a
        same-cycle terminal resize.  macOS only sends SIGWINCH when
        the size actually changes, so we shrink by one row, let the
        child handle it, then restore.

        Required for Ink to clear visual residue from the [Leap Q]
        overlay AND to maintain the alternate-screen / full-screen
        layout — without a SIGWINCH after capture exit, Claude's TUI
        fragments over the Leap server welcome screen.
        """
        def _deferred_resize() -> None:
            try:
                cols, rows = shutil.get_terminal_size(fallback=(80, 24))
                self.pty.resize(max(1, rows - 1), cols)
                time.sleep(0.05)
                self.pty.resize(rows, cols)
            except OSError:
                pass
        threading.Thread(target=_deferred_resize, daemon=True).start()

    def _capture_flush(
        self, cancel: bool = False, defer_sigwinch: bool = False,
    ) -> None:
        """End capture mode: handle stale CLI input, force TUI redraw.

        When ``defer_sigwinch=True`` the SIGWINCH-driven Ink full
        repaint is NOT fired here.  Instead, ``_send_to_cli`` fires
        it after the auto-sender's paste-and-submit completes.  This
        keeps the SIGWINCH-induced render output out of the dispatch
        window — without that, the render storm keeps Ink emitting
        bytes that hold ``_wait_for_output_settled`` busy and flip
        Leap's state tracker to RUNNING (gating dispatch), which
        adds many seconds of latency to every queued message on a
        long conversation transcript.
        """
        # Handle stale ^ from cross-chunk ^^ entry.
        if self._capture_stale_caret:
            self._capture_stale_caret = False
            if cancel:
                self.pty.send('\x7f')  # best-effort backspace
            # On send: _send_to_cli's Ctrl+C clears the full line.
        # On cancel (Escape/Ctrl+C), discard the stale count so the
        # text stays on the CLI — the user wants to keep it.
        if cancel:
            self._capture_stale_visual_rows = 0
            self._capture_stale_logical_lines = 0
        # Clear pending caret so a single ^ after exit doesn't
        # accidentally trigger capture mode.
        self._pending_caret = False
        self._queue_capture_mode = False
        # Reset history-recall — the buf is now in a fresh state (empty
        # on submit, restored cancel_text on cancel), and on cancel the
        # ``_capture_cancel`` background thread re-types the text into
        # the CLI; the next ↑ should snapshot whatever ends up on the
        # input line, not whatever was there before ``^^``.
        self._reset_history_recall()
        if defer_sigwinch:
            self._pending_sigwinch = True
            return
        self._trigger_sigwinch_repaint()

    def _save_clipboard_image(self) -> Optional[str]:
        """Save clipboard image to disk and return its path.

        Returns ``None`` when the clipboard has no image or on failure.
        Uses PyObjC (AppKit) directly — no subprocess, so terminal raw
        mode settings are not corrupted.
        """
        if not HAS_APPKIT:
            return None
        pb = NSPasteboard.generalPasteboard()
        png_data = pb.dataForType_(NSPasteboardTypePNG)
        if png_data is None:
            tiff_data = pb.dataForType_(NSPasteboardTypeTIFF)
            if tiff_data is None:
                return None
            try:
                rep = NSBitmapImageRep.imageRepWithData_(tiff_data)
                if rep is None:
                    return None
                png_data = rep.representationUsingType_properties_(NSPNGFileType, None)
                if png_data is None:
                    return None
            except Exception:
                return None
        raw_bytes = bytes(png_data)
        content_hash = hashlib.md5(raw_bytes).hexdigest()[:12]
        QUEUE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        dest = QUEUE_IMAGES_DIR / f'{content_hash}.png'
        if not dest.is_file():
            dest.write_bytes(raw_bytes)
        return str(dest)

    def _capture_paste_image(self) -> bool:
        """Try to paste a clipboard image into the capture buffer."""
        path = self._save_clipboard_image()
        if not path:
            return False
        # Reuse existing placeholder if same image was already pasted
        for existing_ph, existing_path in self._capture_image_map.items():
            if existing_path == path:
                self._capture_insert(existing_ph)
                return True
        self._capture_image_counter += 1
        placeholder = f'[Image #{self._capture_image_counter}]'
        self._capture_image_map[placeholder] = path
        self._capture_insert(placeholder)
        return True

    def _capture_resolve_images(self, message: str) -> str:
        """Replace ``[Image #N]`` placeholders with ``@path`` references.

        Replacement is in-place so the text-image interleaving the
        user typed is preserved on send.  ``_has_image_ref`` detects
        ``@path`` tokens anywhere in the message, so routing through
        the image send protocol is unaffected by position.
        """
        for placeholder, path in self._capture_image_map.items():
            message = message.replace(placeholder, f'@{path}')
        return message

    def _capture_resolve_pastes(self, message: str) -> str:
        """Replace ``[Paste #N]`` placeholders with the raw pasted text.

        Collapsed-paste placeholders stored in ``_paste_text_map`` are
        expanded back to their original multi-line content before the
        message is queued or saved, so downstream consumers (queue,
        dispatcher, history) see the full text.  In-place replacement
        preserves ordering with surrounding text and image refs.
        """
        # Snapshot via list() — auto-sender thread may GC the dict
        # concurrently; iterating during mutation raises RuntimeError.
        for placeholder, text in list(self._paste_text_map.items()):
            message = message.replace(placeholder, text)
        return message

    def _finalize_paste_capture(self) -> None:
        """Called at ``\\x1b[201~`` — collapse large pastes to a placeholder.

        The raw paste bytes have already been accumulated in
        ``_paste_accumulator`` and forwarded to the CLI in real time
        (so Claude's TUI has the full content).  If the paste is
        substantial (has newlines or is long), truncate the printable
        bytes we added to ``_terminal_input_buf`` during the paste
        and replace them with a short ``[Paste #N]`` placeholder —
        ^^ will then capture the placeholder instead of a sprawling
        raw-text buffer.  Short pastes are left as raw text.
        """
        if self._paste_accumulator is None:
            return
        content = bytes(self._paste_accumulator).decode(
            'utf-8', errors='replace')
        self._paste_accumulator = None
        # Sanitize any stray bracketed-paste markers inside the content
        # (e.g. user pasted bracketed-paste output from another TUI).
        # If we re-wrap this content on send, nested markers would
        # confuse Claude's Ink parser and corrupt the message.
        content = content.replace('\x1b[200~', '').replace('\x1b[201~', '')
        is_substantial = (
            '\n' in content or '\r' in content or len(content) > 200
        )
        if not is_substantial:
            return  # leave raw text in buf
        # Remove the paste bytes we inserted at the cursor during
        # the paste, then substitute a placeholder at that position.
        snap_cursor = self._paste_cursor_snapshot
        cur_cursor = self._terminal_input_cursor
        if cur_cursor > snap_cursor:
            del self._terminal_input_buf[snap_cursor:cur_cursor]
            self._terminal_input_cursor = snap_cursor
        self._chars_sent_to_cli = self._paste_chars_snapshot
        placeholder = self._paste_placeholder_for(content)
        self._paste_text_map[placeholder] = content
        ph_bytes = placeholder.encode('utf-8')
        # Insert placeholder at cursor and advance cursor past it.
        self._terminal_input_buf[snap_cursor:snap_cursor] = ph_bytes
        self._terminal_input_cursor = snap_cursor + len(ph_bytes)
        # Count placeholder as 1 visual token on the CLI (matches
        # Claude's own collapsed [Pasted text #N] rendering).
        self._chars_sent_to_cli += 1

    def _capture_unresolve_pastes(self, message: str) -> str:
        """Collapse substantial raw text into a ``[Paste #N]`` placeholder.

        Used when recalling a saved history message: if the message
        has newlines or is long, wrap the whole thing into a fresh
        placeholder stored in ``_paste_text_map`` — so capture display
        shows a short token instead of a sprawling block, keeping the
        browse (↑↓) experience scannable.  Original paste boundaries
        are not preserved in history, so multi-paste saves collapse
        into a single placeholder.  Short single-line messages pass
        through unchanged.
        """
        is_substantial = (
            '\n' in message or '\r' in message or len(message) > 200
        )
        if not is_substantial:
            return message
        # Sanitize any stray bracketed-paste markers — the re-send
        # will wrap in our own markers and nested pairs would confuse
        # Claude's Ink parser.
        message = message.replace(
            '\x1b[200~', '').replace('\x1b[201~', '')
        placeholder = self._paste_placeholder_for(message)
        self._paste_text_map[placeholder] = message
        return placeholder

    @staticmethod
    def _paste_placeholder_for(content: str) -> str:
        """Stable placeholder for a paste: ``[Paste #<hash8>]``.

        The ID is the first 8 hex chars of md5(content) so the same
        content always produces the same placeholder — deduplicating
        repeat pastes and surviving save/recall cycles.
        """
        digest = hashlib.md5(content.encode('utf-8')).hexdigest()[:8]
        return f'[Paste #{digest}]'

    def _capture_unresolve_images(self, message: str) -> str:
        """Replace ``@path`` image refs with ``[Image #N]`` placeholders.

        The reverse of :meth:`_capture_resolve_images`.  Populates
        ``_capture_image_map`` so the placeholders can be resolved back
        when the message is sent or saved.
        """
        images_dir = str(QUEUE_IMAGES_DIR)
        tokens = message.split()
        changed = False
        for i, token in enumerate(tokens):
            if not token.startswith('@'):
                continue
            path_part = token[1:]
            try:
                if not os.path.realpath(path_part).startswith(images_dir):
                    continue
            except (OSError, ValueError):
                continue
            # Check if already mapped (same path from a previous recall)
            existing_ph = None
            for ph, p in self._capture_image_map.items():
                if p == path_part:
                    existing_ph = ph
                    break
            if existing_ph:
                tokens[i] = existing_ph
            else:
                self._capture_image_counter += 1
                placeholder = f'[Image #{self._capture_image_counter}]'
                self._capture_image_map[placeholder] = path_part
                tokens[i] = placeholder
            changed = True
        return ' '.join(tokens) if changed else message

    def _capture_reset_images(self) -> None:
        """Reset image state for the next capture session."""
        self._capture_image_counter = 0
        self._capture_image_map.clear()

    def _terminal_cursor_left(self) -> None:
        """Move the mirrored CLI cursor one step left, atomic over placeholders.

        Skips back over UTF-8 continuation bytes so cursor never lands
        in the middle of a multi-byte character.
        """
        buf = self._terminal_input_buf
        pos = self._terminal_input_cursor
        if pos <= 0:
            self._terminal_input_cursor = 0
            return
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                ph_bytes = ph.encode('utf-8')
                start = pos - len(ph_bytes)
                if start >= 0 and bytes(buf[start:pos]) == ph_bytes:
                    self._terminal_input_cursor = start
                    return
        # Step back one char — may be multiple bytes for UTF-8.
        new_pos = pos - 1
        while new_pos > 0 and (buf[new_pos] & 0xC0) == 0x80:
            new_pos -= 1
        self._terminal_input_cursor = new_pos

    def _terminal_cursor_right(self) -> None:
        """Move the mirrored CLI cursor one step right, atomic over placeholders.

        Skips forward over UTF-8 continuation bytes.
        """
        buf = self._terminal_input_buf
        buf_len = len(buf)
        pos = self._terminal_input_cursor
        if pos >= buf_len:
            self._terminal_input_cursor = buf_len
            return
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                ph_bytes = ph.encode('utf-8')
                end = pos + len(ph_bytes)
                if end <= buf_len and bytes(buf[pos:end]) == ph_bytes:
                    self._terminal_input_cursor = end
                    return
        # Step forward one char — handle UTF-8 lead byte → skip continuations.
        lead = buf[pos]
        if lead < 0x80:
            char_len = 1
        elif lead & 0xE0 == 0xC0:
            char_len = 2
        elif lead & 0xF0 == 0xE0:
            char_len = 3
        elif lead & 0xF8 == 0xF0:
            char_len = 4
        else:
            char_len = 1  # invalid lead byte, bail
        self._terminal_input_cursor = min(buf_len, pos + char_len)

    def _terminal_buf_insert(self, b: int) -> None:
        """Insert a byte at the mirrored cursor position."""
        pos = self._terminal_input_cursor
        self._terminal_input_buf.insert(pos, b)
        self._terminal_input_cursor = pos + 1

    def _terminal_buf_delete_forward(self) -> None:
        """Delete the char at the cursor (forward Delete key).

        Atomic over placeholders: if the cursor is at the start of a
        token, the whole token is removed.
        """
        buf = self._terminal_input_buf
        pos = self._terminal_input_cursor
        if pos >= len(buf):
            return
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                ph_bytes = ph.encode('utf-8')
                end = pos + len(ph_bytes)
                if end <= len(buf) and bytes(buf[pos:end]) == ph_bytes:
                    del buf[pos:end]
                    return
        # Single char delete — find UTF-8 char length.
        lead = buf[pos]
        if lead < 0x80:
            char_len = 1
        elif lead & 0xE0 == 0xC0:
            char_len = 2
        elif lead & 0xF0 == 0xE0:
            char_len = 3
        elif lead & 0xF8 == 0xF0:
            char_len = 4
        else:
            char_len = 1
        del buf[pos:pos + char_len]

    def _terminal_buf_backspace(self) -> None:
        """Delete the char before the cursor (UTF-8-aware, placeholder-atomic).

        If the cursor sits immediately after a ``[Paste #N]`` or
        ``[Image #N]`` placeholder, the whole placeholder is deleted
        as one unit so the token can't be corrupted by a stray
        backspace.
        """
        buf = self._terminal_input_buf
        pos = self._terminal_input_cursor
        if pos <= 0:
            return
        # Atomic placeholder check — if cursor ends a placeholder,
        # remove the whole token.
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                ph_bytes = ph.encode('utf-8')
                start = pos - len(ph_bytes)
                if start >= 0 and bytes(buf[start:pos]) == ph_bytes:
                    del buf[start:pos]
                    self._terminal_input_cursor = start
                    return
        # Strip trailing UTF-8 continuation bytes before the cursor.
        while (self._terminal_input_cursor > 0
               and (buf[self._terminal_input_cursor - 1]
                    & 0xC0) == 0x80):
            del buf[self._terminal_input_cursor - 1]
            self._terminal_input_cursor -= 1
        if self._terminal_input_cursor > 0:
            del buf[self._terminal_input_cursor - 1]
            self._terminal_input_cursor -= 1

    def _resolve_chunk_for_cancel(
        self,
        chunk: str,
        cancel_paste_map: dict,
        cancel_image_map: Optional[dict] = None,
    ) -> str:
        """Render a prefix/suffix chunk as ready-to-send PTY bytes.

        Each ``[Paste #N]`` placeholder is replaced with its own
        bracketed-paste marker block.  Each ``[Image #N]`` placeholder
        is replaced with its ``@path`` string.  Plain text runs
        between placeholders wrap in bracketed-paste markers when
        they contain ``\\n``/``\\r`` so Claude's Ink treats those
        bytes as paste content, not submit-Enters.

        ``cancel_image_map`` is a snapshot taken BEFORE the caller
        resets the live image map — without it the image map would
        already be empty by the time this helper runs.
        """
        if not chunk:
            return ''
        image_map = (cancel_image_map
                     if cancel_image_map is not None
                     else self._capture_image_map)
        # Collect all placeholder spans in order of appearance.
        spans: list[tuple[int, int, str]] = []  # (start, end, payload)
        for ph, content in cancel_paste_map.items():
            start = 0
            while True:
                i = chunk.find(ph, start)
                if i < 0:
                    break
                spans.append(
                    (i, i + len(ph),
                     '\x1b[200~' + content + '\x1b[201~'),
                )
                start = i + len(ph)
        for ph, path in image_map.items():
            start = 0
            while True:
                i = chunk.find(ph, start)
                if i < 0:
                    break
                spans.append((i, i + len(ph), '@' + path))
                start = i + len(ph)
        spans.sort(key=lambda s: s[0])
        # Emit text between spans, wrapping multi-line runs.
        def _wrap_run(run: str) -> str:
            # Strip any embedded bracketed-paste markers so a wrap
            # here doesn't produce nested pairs that confuse Ink.
            safe = run.replace(
                '\x1b[200~', '').replace('\x1b[201~', '')
            if '\n' in safe or '\r' in safe:
                return '\x1b[200~' + safe + '\x1b[201~'
            return safe
        result: list[str] = []
        cursor = 0
        for start, end, payload in spans:
            if start > cursor:
                result.append(_wrap_run(chunk[cursor:start]))
            result.append(payload)
            cursor = end
        if cursor < len(chunk):
            result.append(_wrap_run(chunk[cursor:]))
        return ''.join(result)

    def _capture_cancel(self) -> None:
        """Cancel capture mode — transfer text back to CLI input."""
        self._capture_display()
        capture_text = self._capture_text()
        pre_text = self._capture_pre_input_buf.decode(
            'utf-8', errors='replace')
        # Detect edits using the placeholder-form text (what the user
        # saw in capture), not the resolved text — otherwise an
        # unchanged paste-placeholder would look "different" after
        # expansion to its raw content.
        was_edited = capture_text != self._capture_initial_text
        # For cancel we resolve images (→ @path) but leave
        # [Paste #N] placeholders intact.  At send time below, each
        # placeholder is replaced by its OWN bracketed-paste marker
        # block so every paste re-appears as a separate collapsed
        # label on Claude's side, with typed text between them
        # appearing as literal chars.  (Wrapping the whole re-type
        # in a single pair of markers — as we used to — caused
        # typed text like "hello" to vanish into the paste label.)
        resolved_text = capture_text
        # Snapshot paste map entries that are actually referenced by
        # the current capture text.  We need these even though we
        # reset the image map below — the expansion happens inside
        # the background thread, after _capture_reset_images runs.
        cancel_paste_map = {
            ph: text
            for ph, text in self._paste_text_map.items()
            if ph in capture_text
        }
        had_pastes = bool(cancel_paste_map)
        # Snapshot the image map BEFORE _capture_reset_images clears
        # it below — the fast path's chunk resolver runs later and
        # needs to see the mappings that existed at cancel time.
        cancel_image_map = dict(self._capture_image_map)
        if self._capture_image_map:
            resolved_text = self._capture_resolve_images(resolved_text)
        has_images = bool(self._capture_image_map)
        self._pending_bang = False
        self._capture_force_confirm = False
        self._queue_capture_buf.clear()
        self._capture_cursor_pos = 0
        self._capture_utf8_buf.clear()
        self._queue_capture_mode = False
        self._capture_flush(cancel=True)
        self._capture_reset_images()
        # Typed text between placeholders must not contain \n (would
        # auto-submit); pastes' \n is safe because it lives inside
        # the placeholder and gets wrapped in paste markers below.
        safe_text = resolved_text.replace('\n', ' ')
        if has_images:
            # Images present — send resolved text to CLI.
            # But if resolved text is empty (user deleted all images
            # and text), just restore the pre-capture state.
            if not safe_text:
                self._terminal_input_buf = bytearray(
                    self._capture_pre_input_buf)
                self._terminal_input_cursor = min(
                    self._capture_pre_input_cursor,
                    len(self._terminal_input_buf),
                )
                self._chars_sent_to_cli = self._capture_pre_chars_sent
                return
            cancel_text = safe_text
        else:
            # No images — skip re-type when the capture buffer matches
            # the initial state (user didn't edit after ^^).
            if not was_edited:
                self._terminal_input_buf = bytearray(
                    self._capture_pre_input_buf)
                self._terminal_input_cursor = min(
                    self._capture_pre_input_cursor,
                    len(self._terminal_input_buf),
                )
                self._chars_sent_to_cli = self._capture_pre_chars_sent
                return
            cancel_text = safe_text
        self._capture_cancel_pending = True
        # Hold user keystrokes during the cancel send so they can't
        # interleave with our clear + bracketed-paste bytes on the
        # PTY — held bytes replay on the next filter call after the
        # send completes (see _queue_sending_held).  Only flip the
        # flag if it wasn't already set (the queue dispatcher uses
        # the same flag), so we don't clobber its reset.
        held_queue_sending = not self._queue_sending
        if held_queue_sending:
            self._queue_sending = True

        pre_chars = self._capture_pre_chars_sent
        # Logical lines + visual rows for the cancel slow-path clear.
        # Compute here (before the thread spawns) so the closure
        # captures stable values — _capture_pre_input_buf is still
        # intact at this point (only re-set on the next capture entry).
        pre_lines = self._stale_logical_lines(self._capture_pre_input_buf)
        pre_rows = self._stale_visual_rows(self._capture_pre_input_buf)
        # Fast path: if the capture buffer is the initial text
        # surrounded by new prefix/suffix chunks (initial text
        # itself untouched), transfer ONLY the added chunks to the
        # CLI and leave Claude's existing input untouched.  Avoids
        # the clear + re-paste round-trip whose bracketed-paste
        # start markers can race-drop under streaming and cause the
        # original paste to vanish.
        #
        # Each chunk is resolved in place:
        #   [Image #N]   → @path (Claude treats as attachment ref)
        #   [Paste #N]   → bracketed-paste block wrapping raw content
        #   plain \n/\r  → the run of plain text between placeholders
        #                  wraps in bracketed-paste markers so its
        #                  newlines don't submit-Enter.
        # Prefix chunks are bracketed by Home (\x1b[H) and End
        # (\x1b[F) so Claude inserts them before its existing
        # attachment and leaves cursor at end of line.
        initial = self._capture_initial_text
        fast_path_payload: Optional[str] = None
        if (initial in capture_text
                and capture_text != initial):
            # Works even when initial == "" (user entered Leap Q on an
            # empty CLI): before == "" and after == capture_text, so
            # the whole content becomes a clean bracketed-paste block
            # instead of the slow path's \n→space flattening.
            idx = capture_text.find(initial) if initial else 0
            before = capture_text[:idx]
            after = capture_text[idx + len(initial):]
            before_payload = self._resolve_chunk_for_cancel(
                before, cancel_paste_map, cancel_image_map)
            after_payload = self._resolve_chunk_for_cancel(
                after, cancel_paste_map, cancel_image_map)
            parts: list[str] = []
            if before_payload:
                # Home → insert payload before original → End.
                parts.append('\x1b[H' + before_payload + '\x1b[F')
            if after_payload:
                parts.append(after_payload)
            if parts:
                fast_path_payload = ''.join(parts)

        def _apply_cancel_text() -> None:
            try:
                if fast_path_payload is not None:
                    # Claude's input already shows the original content.
                    # Just type the new prefix/suffix chunks around it;
                    # no clear, no re-paste of the original.
                    self.pty.send(fast_path_payload)
                    return
                # Slow path: full clear + re-paste round-trip.
                # Clear Claude's CLI input regardless of state.  During
                # RUNNING, Ctrl+U alone can race with Ink's render loop,
                # so _clear_stale_cli_input adds N backspaces as an
                # idempotent fallback.
                if pre_chars > 0:
                    self._clear_stale_cli_input(pre_lines, pre_rows)
                    time.sleep(0.1)
                if cancel_text:
                    text_to_send = cancel_text
                    if had_pastes:
                        # Replace each [Paste #N] individually with
                        # its own bracketed-paste marker block so
                        # Claude re-collapses each paste as its own
                        # label and preserves typed text in between.
                        for ph, paste_content in cancel_paste_map.items():
                            text_to_send = text_to_send.replace(
                                ph,
                                '\x1b[200~' + paste_content + '\x1b[201~',
                            )
                    self.pty.send(text_to_send)
            except OSError:
                pass
            finally:
                if held_queue_sending:
                    self._queue_sending = False
                self._capture_cancel_pending = False
        self._terminal_input_buf = bytearray(
            cancel_text.encode('utf-8'))
        self._terminal_input_cursor = len(self._terminal_input_buf)
        self._chars_sent_to_cli = len(cancel_text)
        threading.Thread(
            target=_apply_cancel_text, daemon=True).start()

    def _enter_capture_mode(self, stale_cli_input: bool,
                            stale_caret: bool) -> None:
        """Enter queue-capture mode with the current input buffer."""
        # Wait for any pending cancel-text send to finish so the CLI
        # has the correct text before we snapshot it.
        if self._capture_cancel_pending:
            deadline = time.time() + 0.3
            while self._capture_cancel_pending and time.time() < deadline:
                time.sleep(0.01)
        # Clean slate for image tracking — previous capture sessions
        # (especially cancelled ones or exceptions) may have left
        # stale entries in the map.
        self._capture_reset_images()
        # Snapshot the pre-capture input so _capture_cancel can restore
        # it if the user toggles back out without sending.
        self._capture_pre_input_buf = bytearray(self._terminal_input_buf)
        self._capture_pre_chars_sent = self._chars_sent_to_cli  # for cancel restore
        # Snapshot the terminal cursor so a no-edit Esc can restore it.
        self._capture_pre_input_cursor = self._terminal_input_cursor
        self._queue_capture_buf = bytearray(self._terminal_input_buf)
        # Map the terminal-buf byte cursor onto the decoded
        # capture-text char cursor so Leap Q opens with the cursor
        # at the same position Claude was showing.
        try:
            prefix = self._terminal_input_buf[
                :self._terminal_input_cursor]
            self._capture_cursor_pos = len(
                prefix.decode('utf-8', errors='replace'))
        except Exception:
            self._capture_cursor_pos = len(self._capture_text())
        self._terminal_input_buf.clear()
        self._terminal_input_cursor = 0
        self._queue_capture_mode = True
        self._capture_show_hint = True
        self._capture_stale_caret = stale_caret
        # Only count chars actually sent to CLI (not held during RUNNING).
        # Snapshot pre-capture row + line counts.  Visual rows drive
        # the walk-up below + RUNNING-mode Ctrl+U sequence; logical
        # lines drive the IDLE-mode Ctrl+U + Backspace pattern.
        # Add one per pending Ctrl+V image: those don't appear in
        # _terminal_input_buf (Ctrl+V outside capture saves the image
        # to _pending_paste_images without inserting bytes), but the
        # CLI still rendered an image-attachment row+line for each.
        n_images = len(self._pending_paste_images)
        self._capture_stale_visual_rows = (
            (self._stale_visual_rows() + n_images)
            if stale_cli_input else 0
        )
        self._capture_stale_logical_lines = (
            (self._stale_logical_lines() + n_images)
            if stale_cli_input else 0
        )
        self._chars_sent_to_cli = 0
        # tcflush to discard any stale text still in the PTY buffer.
        if self._capture_stale_visual_rows > 0:
            try:
                termios.tcflush(self.pty.process.child_fd,
                                termios.TCOFLUSH)
            except Exception:
                pass
        self._pending_caret = False
        self._capture_prev_lines = 0
        # Clear the wrap rows that the CLI rendered for the pre-capture
        # text — without this, a long typed or pasted message that
        # spans multiple terminal rows leaves its upper rows visible
        # above the [Leap Q] line (which only clears its own current
        # row).  Walk up clearing, then walk back down so
        # _capture_display below draws on the original cursor row,
        # not the top of the cleared region.
        #
        # ``rows_above`` is the count of visual rows ABOVE the cursor
        # row, so we subtract 1 from the total visual-row count of the
        # stale input (the cursor sits on the bottom row).  The
        # half-viewport safety cap bounds the worst case: a 1000-line
        # paste can't blank more than half the visible terminal — any
        # cosmetic over-clear self-heals via the SIGWINCH-triggered
        # full repaint that ``_capture_flush`` schedules on submit /
        # cancel.
        rows_above = max(0, self._capture_stale_visual_rows - 1)
        if rows_above > 0:
            try:
                term = shutil.get_terminal_size(fallback=(80, 24))
                rows_above = min(rows_above, max(1, term.lines // 2))
                walk_up = '\x1b[A\r\x1b[K' * rows_above
                walk_down = f'\x1b[{rows_above}B'
                os.write(
                    sys.stdout.fileno(),
                    (walk_up + walk_down).encode(),
                )
            except OSError:
                pass
        self._saved_msg_index = -1
        self._capture_show_saved_hint = False
        # Inject clipboard images saved by prior Ctrl+V presses.
        # Each entry carries the BYTE offset from ``_terminal_input_buf``
        # at the time Ctrl+V fired — we convert to CHAR offset in the
        # decoded capture text so images land at the right position
        # for multi-byte UTF-8 content.
        # Entries with pos=-1 go at the end.
        if self._pending_paste_images:
            text = self._capture_text()
            text_len = len(text)
            # Build byte→char mapping from the capture buffer (which is
            # a copy of terminal_input_buf pre-clear, so positions
            # still line up with what was recorded at Ctrl+V time).
            buf_bytes = bytes(self._queue_capture_buf)

            def _byte_to_char(byte_pos: int) -> int:
                byte_pos = max(0, min(byte_pos, len(buf_bytes)))
                try:
                    return len(buf_bytes[:byte_pos].decode(
                        'utf-8', errors='replace'))
                except Exception:
                    return byte_pos

            # Split into positioned (pos >= 0) and end-append (pos < 0).
            # Track original index for stable ordering at same position.
            positioned: list[tuple[int, int, str]] = []
            at_end: list[str] = []
            for idx, (pos, path) in enumerate(self._pending_paste_images):
                if pos >= 0:
                    char_pos = _byte_to_char(pos)
                    positioned.append(
                        (min(char_pos, text_len), idx, path))
                else:
                    at_end.append(path)

            # Two-pass injection: assign counters left-to-right so
            # #1 is the leftmost image, then insert right-to-left so
            # earlier offsets stay valid after each insertion.
            positioned.sort(key=lambda x: (x[0], x[1]))
            placeholders: list[tuple[int, str]] = []
            for pos, _, path in positioned:
                ph = None
                for k, v in self._capture_image_map.items():
                    if v == path:
                        ph = k
                        break
                if not ph:
                    self._capture_image_counter += 1
                    ph = f'[Image #{self._capture_image_counter}]'
                    self._capture_image_map[ph] = path
                placeholders.append((pos, ph))
            for pos, ph in reversed(placeholders):
                text = text[:pos] + ph + text[pos:]

            # Append end-positioned images (from cancel round-trip).
            for path in at_end:
                ph = None
                for k, v in self._capture_image_map.items():
                    if v == path:
                        ph = k
                        break
                if not ph:
                    self._capture_image_counter += 1
                    ph = f'[Image #{self._capture_image_counter}]'
                    self._capture_image_map[ph] = path
                text += ph

            self._queue_capture_buf = bytearray(text.encode('utf-8'))
            self._capture_cursor_pos = len(text)
            self._pending_paste_images.clear()
            self._capture_show_hint = False
        self._capture_initial_text = self._capture_text()
        self._capture_display(self._capture_initial_text)

    def _capture_cursor_left(self, pos: int) -> int:
        """One-step Left that skips over a placeholder as one unit."""
        if pos <= 0:
            return 0
        text = self._capture_text()
        # If cursor is right after a placeholder, jump to before it.
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                start = pos - len(ph)
                if start >= 0 and text[start:pos] == ph:
                    return start
        return pos - 1

    def _capture_cursor_right(self, pos: int) -> int:
        """One-step Right that skips over a placeholder as one unit."""
        text = self._capture_text()
        if pos >= len(text):
            return pos
        # If cursor is at the start of a placeholder, jump past it.
        for ph_map in (self._paste_text_map, self._capture_image_map):
            for ph in ph_map:
                end = pos + len(ph)
                if text[pos:end] == ph:
                    return end
        return pos + 1

    def _capture_word_move(self, direction: int) -> None:
        """Move capture cursor by one word. direction: -1=left, +1=right.

        Placeholders (``[Paste #N]``, ``[Image #N]``) are treated as
        single atomic word-units so Opt+Left/Right never lands the
        cursor inside a placeholder (which would otherwise happen
        because placeholders contain a space between ``Paste``/``#``).
        """
        text = self._capture_text()
        p = self._capture_cursor_pos

        def _placeholder_at(pos: int, rev: bool) -> Optional[int]:
            """Return opposite end of a placeholder adjacent to pos."""
            for ph_map in (self._paste_text_map,
                           self._capture_image_map):
                for ph in ph_map:
                    if rev:
                        start = pos - len(ph)
                        if start >= 0 and text[start:pos] == ph:
                            return start
                    else:
                        end = pos + len(ph)
                        if text[pos:end] == ph:
                            return end
            return None

        if direction < 0:
            # Skip trailing spaces.
            while p > 0 and text[p - 1] == ' ':
                p -= 1
            # Jump over placeholder if one ends here, else skip word.
            ph_start = _placeholder_at(p, rev=True)
            if ph_start is not None:
                p = ph_start
            else:
                while p > 0 and text[p - 1] != ' ':
                    p -= 1
                    # But don't stop inside a placeholder.
                    ph_start2 = _placeholder_at(p, rev=True)
                    if ph_start2 is not None and ph_start2 < p:
                        p = ph_start2
                        break
        else:
            # Jump over placeholder if one starts here, else skip word.
            ph_end = _placeholder_at(p, rev=False)
            if ph_end is not None:
                p = ph_end
            else:
                while p < len(text) and text[p] != ' ':
                    p += 1
                    ph_end2 = _placeholder_at(p, rev=False)
                    if ph_end2 is not None:
                        p = ph_end2
                        break
            # Skip trailing spaces.
            while p < len(text) and text[p] == ' ':
                p += 1
        self._capture_cursor_pos = p
        self._capture_display(text)

    def _capture_handle_escape(self, seq: bytes,
                               is_standalone_esc: bool) -> None:
        """Handle an escape sequence while in capture mode.

        Dispatches editing keys (arrows, Home/End, Delete, word
        movement), cancels on standalone Escape or CSI-u Ctrl+C,
        and silently drops unrecognized sequences.
        """
        if self._capture_show_saved_hint:
            self._capture_show_saved_hint = False
            self._capture_display(self._capture_text())
        if self._capture_force_confirm:
            self._capture_force_confirm = False
            self._pending_bang = False
            self._capture_display(self._capture_text())
            return
        if seq in (b'\x1bb', b'\x1bf'):
            # Meta word movement (ESC-b / ESC-f)
            self._capture_word_move(-1 if seq == b'\x1bb' else 1)
        elif is_standalone_esc:
            self._capture_cancel()
        elif self._is_csi_u_cancel(seq):
            self._capture_cancel()
        elif seq == b'\x1b[D':  # Left arrow — jumps over placeholders
            self._capture_cursor_pos = self._capture_cursor_left(
                self._capture_cursor_pos)
            self._capture_display(self._capture_text())
        elif seq == b'\x1b[C':  # Right arrow — jumps over placeholders
            self._capture_cursor_pos = self._capture_cursor_right(
                self._capture_cursor_pos)
            self._capture_display(self._capture_text())
        elif seq == b'\x1b[1;3D':  # Opt+Left
            self._capture_word_move(-1)
        elif seq == b'\x1b[1;3C':  # Opt+Right
            self._capture_word_move(1)
        elif seq in (b'\x1b[H', b'\x1b[1~'):  # Home
            self._capture_cursor_pos = 0
            self._capture_utf8_buf.clear()
            self._capture_display(self._capture_text())
        elif seq in (b'\x1b[F', b'\x1b[4~'):  # End
            self._capture_cursor_pos = len(self._capture_text())
            self._capture_display(self._capture_text())
        elif seq == b'\x1b[3~':  # Delete
            self._capture_show_hint = False
            self._capture_delete()
            self._capture_display(self._capture_text())
        elif seq == b'\x1b[A':  # Up arrow — browse saved msgs
            self._capture_show_hint = False
            self._browse_saved_history(-1)
        elif seq == b'\x1b[B':  # Down arrow — browse saved msgs
            self._capture_show_hint = False
            self._browse_saved_history(1)
        elif self._is_csi_u_paste(seq):  # CSI u Ctrl+V
            self._capture_show_hint = False
            if self._capture_paste_image():
                self._capture_display(self._capture_text())
        # Other CSI/OSC/SS3 sequences silently dropped.

    def _flush_pending_caret(self) -> None:
        """Timer callback: flush the held ``^`` to the CLI.

        Called from a background thread after ~200ms if no second
        ``^`` arrived.  Writes the ``^`` directly to the PTY so it
        appears on the CLI's input line.
        """
        if not self._pending_caret:
            return
        self._pending_caret = False
        try:
            self.pty.send('^')
        except OSError:
            pass
        self._terminal_buf_insert(0x5e)
        self._chars_sent_to_cli += 1

    def _detect_paste(self, data: bytes) -> bool:
        """Detect bracketed paste markers in input data.

        Returns True if this chunk contains pasted content.  Also
        updates ``_in_bracketed_paste`` for cross-chunk tracking and
        clears ``_pending_caret`` to prevent a stale ``^`` typed
        before the paste from combining with ``^`` inside it.
        """
        _BP_START = b'\x1b[200~'
        _BP_END = b'\x1b[201~'
        # Use rfind so ``_in_bracketed_paste`` reflects the LAST
        # marker in the chunk — a chunk with ``start…end…start``
        # ends inside a new paste (True), and ``end…start…end``
        # ends outside (False).
        bp_start = data.rfind(_BP_START)
        bp_end = data.rfind(_BP_END)
        chunk_has_paste = (
            self._in_bracketed_paste
            or bp_start >= 0
            or bp_end >= 0
        )
        if bp_start > bp_end:
            self._in_bracketed_paste = True
        elif bp_end > bp_start:
            self._in_bracketed_paste = False
        # (both -1 → no markers, leave flag as-is)
        if chunk_has_paste and self._pending_caret:
            # Flush the held "^" — it was a literal, not a capture
            # trigger.  We can't add to `out` here (no access), so
            # set a flag for the caller to handle.
            self._pending_caret_flush = True
            self._pending_caret = False
        return chunk_has_paste

    def _capture_handle_char(self, b: int, data: bytes, i: int,
                             chunk_has_paste: bool) -> tuple[int, bool]:
        """Process one byte in capture mode.

        Returns ``(new_i, display_dirty)`` — the caller should set
        ``capture_dirty |= display_dirty`` and ``continue``.
        """
        dirty = False

        def _display_or_defer() -> None:
            nonlocal dirty
            if chunk_has_paste:
                dirty = True
            else:
                self._capture_display(self._capture_text())

        # Dismiss "Saved!" hint on any key
        if self._capture_show_saved_hint and b != 0x5e:
            self._capture_show_saved_hint = False
            self._capture_display(self._capture_text())

        # Dismiss force-send confirm on any non-Enter key; clear pending
        # bang on any non-'!' key so it only fires on an immediate double.
        if b not in (0x0d, 0x0a):
            if self._capture_force_confirm:
                self._capture_force_confirm = False
                self._capture_display(self._capture_text())
            if b != 0x21:
                self._pending_bang = False

        if b in (0x0d, 0x0a):  # Enter / LF
            # Detect pasted newlines: bracketed paste markers or
            # fallback — a typed Enter is a tiny chunk (1–2 bytes);
            # pasted multi-line text arrives as a large chunk.
            # Use (len - i) so pre-capture bytes (e.g. "hello^^")
            # don't inflate the count when ^^ and Enter share a chunk.
            if chunk_has_paste or (len(data) - i) > 4:
                # Insert literal newline; skip \n after \r to avoid
                # doubles from \r\n pairs.
                if b == 0x0d:
                    self._capture_insert('\n')
                    dirty = True
                elif b == 0x0a:
                    if not (i > 0 and data[i - 1] == 0x0d):
                        self._capture_insert('\n')
                        dirty = True
            else:
                self._user_has_typed = True
                self._capture_display()  # clear
                if self._capture_force_confirm:
                    # !! confirmed — force-send next queued message
                    self._capture_force_confirm = False
                    self._pending_bang = False
                    message = self.queue.pop()
                    if message:
                        self._send_to_cli(message)
                        self.queue.track_sent(message)
                    self._capture_flush()
                    self._queue_capture_buf.clear()
                    self._capture_cursor_pos = 0
                    self._capture_utf8_buf.clear()
                    self._queue_capture_mode = False
                    self._capture_reset_images()
                    self._terminal_input_buf.clear()
                    self._terminal_input_cursor = 0
                    return i + 1, dirty
                msg = self._capture_text().strip()
                # Pastes first — a recalled paste may embed image
                # placeholders that the subsequent image resolution
                # must see.
                if self._paste_text_map:
                    msg = self._capture_resolve_pastes(msg)
                if self._capture_image_map:
                    msg = self._capture_resolve_images(msg)
                if msg:
                    # Detect REAL RUNNING (an in-flight query the
                    # user submitted) vs PHANTOM RUNNING (state
                    # tracker is RUNNING because of paste-echo /
                    # Ctrl+U render / cursor blink, but no actual
                    # query is being processed).  ``_query_in_flight``
                    # is set True only on ``on_send`` (Leap-dispatched
                    # message) and ``on_input`` Enter (real Enter into
                    # Claude's input) — NOT on paste echoes.  The
                    # auto-sender resets it whenever it observes a
                    # transition to IDLE, so its value here is the
                    # clean "is there a real query running?" signal.
                    has_real_query = (
                        self.state.current_state != CLIState.IDLE
                        and self.state._query_in_flight
                    )
                    # Clear stale text typed before ^^.
                    if self._capture_stale_visual_rows > 0:
                        self._clear_stale_cli_input(
                            self._capture_stale_logical_lines,
                            self._capture_stale_visual_rows)
                        self._capture_stale_visual_rows = 0
                        self._capture_stale_logical_lines = 0
                    self._send_clear_queue.append(False)
                    self.queue.add(msg)
                    if not has_real_query:
                        self._capture_force_dispatch = True
                    self._dispatch_wake.set()
                    # Defer SIGWINCH so its Ink full repaint doesn't
                    # block the dispatch's paste-and-submit; ``_send_to_cli``
                    # fires the resize itself once the message is on its way.
                    self._capture_flush(defer_sigwinch=True)
                else:
                    # Empty Enter — clear stale text unconditionally.
                    # This path is reached when the user saved their
                    # message via ^^ inside capture mode (which empties
                    # the buffer): their original typed text is already
                    # in history, so leaving it on the CLI input line
                    # would just be misleading.
                    if self._capture_stale_visual_rows > 0:
                        try:
                            termios.tcflush(self.pty.process.child_fd,
                                            termios.TCOFLUSH)
                        except Exception:
                            pass
                        self._clear_stale_cli_input(
                            self._capture_stale_logical_lines,
                            self._capture_stale_visual_rows)
                        self._capture_stale_visual_rows = 0
                        self._capture_stale_logical_lines = 0
                    self._capture_flush()
                self._queue_capture_buf.clear()
                self._capture_cursor_pos = 0
                self._capture_utf8_buf.clear()
                self._queue_capture_mode = False
                self._capture_reset_images()
                self._terminal_input_buf.clear()
                self._terminal_input_cursor = 0
        elif b == 0x16:  # Ctrl+V — paste clipboard image
            if self._pending_caret:
                self._pending_caret = False
            self._capture_show_hint = False
            if self._capture_paste_image():
                self._capture_display(self._capture_text())
        elif b == 0x7f:  # Backspace
            self._capture_show_hint = False
            if self._capture_backspace():
                self._capture_display(self._capture_text())
        elif b == 0x03:  # Ctrl+C — cancel capture
            self._capture_cancel()
        elif b == 0x5e:  # "^" in capture mode
            if self._capture_show_saved_hint:
                self._capture_show_saved_hint = False
            if self._pending_caret and not chunk_has_paste:
                # Double "^" → save message
                self._pending_caret = False
                text = self._capture_text()
                p = self._capture_cursor_pos
                if p > 0 and text[p - 1] == '^':
                    text = text[:p - 1] + text[p:]
                    self._queue_capture_buf = bytearray(
                        text.encode('utf-8'))
                    self._capture_cursor_pos = p - 1
                self._save_capture_message()
                if not self._capture_show_saved_hint:
                    _display_or_defer()
            else:
                self._pending_caret = True
                self._capture_show_hint = False
                self._capture_insert('^')
                _display_or_defer()
        elif b == 0x21:  # '!' — fast !! triggers force-send confirm (empty buffer only)
            if self._pending_caret:
                self._pending_caret = False
            self._capture_show_hint = False
            if (self._pending_bang
                    and not chunk_has_paste
                    and time.time() - self._pending_bang_time < 0.2
                    and self._queue_capture_buf == bytearray(b'!')):
                # Second '!' arrived fast with only '!' in buffer → confirm mode
                self._pending_bang = False
                self._queue_capture_buf.clear()
                self._capture_cursor_pos = 0
                self._capture_force_confirm = True
                self._capture_display_force_confirm()
            else:
                # Start pending-bang only when buffer was empty before this '!'
                if not self._queue_capture_buf:
                    self._pending_bang = True
                    self._pending_bang_time = time.time()
                else:
                    self._pending_bang = False
                self._capture_insert('!')
                _display_or_defer()
        elif 0x20 <= b < 0x7f:  # ASCII printable
            if self._pending_caret:
                self._pending_caret = False
            if self._capture_show_saved_hint:
                self._capture_show_saved_hint = False
            self._capture_show_hint = False
            self._capture_insert(chr(b))
            _display_or_defer()
        elif b >= 0x80:  # Multi-byte UTF-8
            if self._pending_caret:
                self._pending_caret = False
            self._capture_show_hint = False
            self._capture_utf8_buf.append(b)
            try:
                char = self._capture_utf8_buf.decode('utf-8')
                self._capture_insert(char)
                self._capture_utf8_buf.clear()
            except UnicodeDecodeError:
                pass
            _display_or_defer()
        else:
            if self._pending_caret:
                self._pending_caret = False

        return i + 1, dirty

    def _input_filter(self, data: bytes) -> bytes:
        """Track user keyboard input for state detection.

        Also accumulates typed text so that messages entered directly
        in the server terminal are captured as the current task.

        **Queue from server**: When the user types ``^`` at the start of
        a line, capture mode activates — subsequent chars are swallowed
        (the CLI never sees them).  On Enter the message is added to
        the queue.  A notification is injected into the output stream
        as confirmation.

        Args:
            data: Raw input bytes from keyboard.

        Returns:
            Input bytes to forward to the CLI (swallowed in capture mode).
        """
        # Wrap the entire filter in try/except — any unhandled exception
        # here propagates to pexpect's interact loop and kills the PTY.
        try:
            return self._input_filter_impl(data)
        except Exception:
            if self._queue_capture_mode:
                return b''  # don't leak capture text to CLI
            return data

    def _input_filter_impl(self, data: bytes) -> bytes:
        """Implementation of _input_filter (separated for crash protection)."""
        # Block all input while a queue message is being sent to the CLI.
        # Without this, user keystrokes could interleave with the
        # Ctrl+E/Ctrl+U clear and the message paste, corrupting the send.
        # Buffer the raw bytes so they can be replayed after the send.
        if self._queue_sending:
            self._queue_sending_held.extend(data)
            return b''

        # Flush any keystrokes that were held during a queue send.
        # Prepend them so they're processed through the full filter
        # (tracking, escape handling, ^^ detection, etc.).
        if self._queue_sending_held:
            data = bytes(self._queue_sending_held) + data
            self._queue_sending_held.clear()

        # Apply deferred SIGWINCH resize outside the signal context.
        self._apply_pending_resize()

        # Safety net: if a held "^" has been pending for >200ms and
        # the timer hasn't flushed it yet, treat it as a literal now.
        # This prevents a stale _pending_caret from combining with a
        # "^" typed much later.
        if (self._pending_caret
                and not self._queue_capture_mode
                and time.time() - self._pending_caret_time > 0.2):
            if self._pending_caret_timer is not None:
                self._pending_caret_timer.cancel()
                self._pending_caret_timer = None
            self._pending_caret = False
            # The timer may have already flushed via pty.send — check
            # if the buf already has the ^.  If not, the ^ was lost
            # (timer raced), so we skip the buf append.  The CLI may
            # or may not have it depending on timer timing — either
            # way, clearing _pending_caret is the safe thing to do.

        # Note: on_input() is called AFTER the byte loop (see end of
        # method) with only the bytes that reach the CLI.  This prevents
        # capture-mode keystrokes from affecting state tracker flags
        # (e.g. false idle→running on Enter, or false _user_responded).

        current_state = self.state.current_state
        in_prompt = current_state in PROMPT_STATES

        self._prev_filter_state = current_state

        out = bytearray()
        i = 0
        capture_dirty = False  # deferred display update for pastes
        chunk_has_paste = self._detect_paste(data)
        # Flush held "^" that was dropped by paste detection.
        if self._pending_caret_flush:
            self._pending_caret_flush = False
            out.append(0x5e)
            self._terminal_buf_insert(0x5e)
            self._chars_sent_to_cli += 1

        # Check if the very first byte is "^" and _pending_caret is set
        # from the previous chunk → double-caret capture trigger.
        # Skip if we're inside a bracketed paste.
        if (not self._queue_capture_mode
                and not chunk_has_paste
                and i < len(data)
                and data[i] == 0x5e
                and self._pending_caret):
            # Second "^" arrived in a new chunk.  Enter capture mode.
            # The first "^" was held (never sent to CLI), so there is
            # no stale caret to clean up.
            if self._pending_caret_timer is not None:
                self._pending_caret_timer.cancel()
                self._pending_caret_timer = None
            self._partial_escape = None
            self._enter_capture_mode(
                stale_cli_input=bool(self._terminal_input_buf),
                stale_caret=False)
            i += 1
        # Note: we used to eagerly flush a held "^" here when the new
        # chunk didn't start with "^".  That broke ^^ detection under
        # kitty keyboard protocol (e.g. Codex/Ratatui), where each "^"
        # press is followed by a CSI-u key-release escape sequence in
        # its own chunk — the flush ran before the second "^" press
        # could arrive.  The byte loop's own ``elif self._pending_caret``
        # at line ~2926 already flushes correctly when a real non-"^"
        # byte (not an escape sequence) is encountered, and the 200ms
        # timer + the >0.2s safety-net above handle the
        # nothing-came-after case.

        # If a previous call ended mid-escape, skip continuation bytes.
        if self._partial_escape == 'csi':
            # CSI was already started (\x1b[ consumed in previous chunk).
            # Continue consuming parameter bytes and the final byte.
            self._partial_escape = None
            while i < len(data) and 0x20 <= data[i] <= 0x3f:
                out.append(data[i])
                i += 1
            if i < len(data):
                out.append(data[i])  # final byte (0x40-0x7e)
                i += 1
            else:
                # Still truncated — remain in CSI state
                self._partial_escape = 'csi'
        elif self._partial_escape == 'esc':
            # Bare \x1b was at end of previous chunk — need type byte.
            self._partial_escape = None
            if i < len(data) and data[i] == 0x5b:
                # CSI: skip introducer, parameter bytes, and final byte
                out.append(data[i])  # '['
                i += 1
                while i < len(data) and 0x20 <= data[i] <= 0x3f:
                    out.append(data[i])
                    i += 1
                if i < len(data):
                    out.append(data[i])  # final byte
                    i += 1
                else:
                    # CSI truncated — switch to csi state
                    self._partial_escape = 'csi'
            elif i < len(data) and data[i] == 0x4f:
                # SS3: skip 'O' + one final byte
                out.append(data[i])
                i += 1
                if i < len(data):
                    out.append(data[i])
                    i += 1
            else:
                # Two-byte escape: only consume if the byte is a valid
                # final byte (0x40-0x5F, e.g. ESC M for reverse index).
                # Otherwise the \x1b was a standalone Escape key press
                # and the current byte is new input — leave it alone.
                if i < len(data) and 0x40 <= data[i] <= 0x5f:
                    out.append(data[i])
                    i += 1

        while i < len(data):
            b = data[i]

            # --- Escape sequences ---
            if b == 0x1b:
                esc_start = i
                is_standalone_esc = False
                i += 1
                if i >= len(data):
                    # ESC at end of chunk — mark partial, pass through
                    is_standalone_esc = True
                    if not self._queue_capture_mode:
                        self._partial_escape = 'esc'
                        out.append(b)
                    else:
                        # In capture mode: Escape cancels capture
                        self._capture_cancel()
                    continue
                kind = data[i]
                if kind == 0x5b:  # CSI
                    i += 1
                    while i < len(data) and 0x20 <= data[i] <= 0x3f:
                        i += 1
                    if i < len(data):
                        i += 1
                    else:
                        # CSI truncated at end of chunk
                        if not self._queue_capture_mode:
                            self._partial_escape = 'csi'
                elif kind in (0x5d, 0x50, 0x58, 0x5e, 0x5f):
                    i += 1
                    while i < len(data):
                        if data[i] == 0x07:
                            i += 1
                            break
                        if data[i] == 0x1b and i + 1 < len(data) and data[i + 1] == 0x5c:
                            i += 2
                            break
                        i += 1
                elif kind == 0x4f:  # SS3 (e.g. \x1bOP for F1)
                    i += 1
                    if i < len(data):
                        i += 1  # consume the final byte
                elif 0x40 <= kind <= 0x5f:
                    # Valid two-byte escape (e.g. ESC M = reverse index).
                    i += 1
                elif kind in (0x62, 0x66):
                    # ESC-b / ESC-f (Meta word left/right).
                    # Consume the byte so it's included in seq.
                    i += 1
                else:
                    # Not a recognized escape introducer — treat \x1b as
                    # a standalone Escape key press.
                    is_standalone_esc = True

                esc_seq = data[esc_start:i]
                if self._queue_capture_mode:
                    self._capture_handle_escape(
                        esc_seq, is_standalone_esc)
                elif esc_seq == b'\x1b[200~':
                    # Bracketed paste start — begin accumulating so we
                    # can collapse large pastes to a placeholder.  If
                    # a previous paste never received its end marker
                    # (malformed stream), force-finalize it first so
                    # its accumulated bytes aren't silently dropped
                    # and _in_bracketed_paste doesn't stay stuck.
                    if self._paste_accumulator is not None:
                        self._finalize_paste_capture()
                    self._paste_accumulator = bytearray()
                    self._paste_buf_snapshot_len = len(
                        self._terminal_input_buf)
                    self._paste_cursor_snapshot = (
                        self._terminal_input_cursor)
                    self._paste_chars_snapshot = self._chars_sent_to_cli
                    out.extend(esc_seq)
                elif esc_seq == b'\x1b[201~':
                    # Bracketed paste end — finalize (maybe collapse).
                    self._finalize_paste_capture()
                    out.extend(esc_seq)
                elif (not in_prompt
                      and self._is_csi_u_cancel(esc_seq)):
                    # CSI-u Ctrl+C outside capture — clear input
                    # buf just like the raw 0x03 handler does.
                    self._terminal_input_buf.clear()
                    self._terminal_input_cursor = 0
                    self._chars_sent_to_cli = 0
                    self._preserved_input_buf.clear()
                    self._preserved_chars_sent = 0
                    self._pending_paste_images.clear()
                    self._reset_history_recall()
                    out.extend(esc_seq)
                elif (not in_prompt
                      and not chunk_has_paste
                      and self._is_csi_u_paste(esc_seq)):
                    # CSI-u Ctrl+V outside capture — save clipboard
                    # image so the next ^^ picks it up at the right
                    # position (cursor position, not end-of-buf).
                    path = self._save_clipboard_image()
                    if path:
                        self._pending_paste_images.append(
                            (self._terminal_input_cursor, path))
                    out.extend(esc_seq)
                elif (not in_prompt
                      and not chunk_has_paste
                      and self._is_csi_u_newline(esc_seq)):
                    # CSI-u Shift/Cmd+Enter outside capture — the
                    # CLI's TUI inserts a newline in its input box,
                    # but the raw escape leaves no trace in
                    # ``_terminal_input_buf``.  Mirror it as a literal
                    # ``\n`` at the cursor so ``_stale_visual_rows``
                    # counts the wrap correctly when the user later
                    # types ^^ — otherwise multi-line typed input
                    # (especially mixed with Ctrl+V images) under-
                    # counts visual rows and leaves residue on Enter.
                    self._terminal_buf_insert(0x0a)
                    self._chars_sent_to_cli += 1
                    out.extend(esc_seq)
                elif (not in_prompt
                      and not chunk_has_paste
                      and self._paste_accumulator is None
                      and esc_seq in (b'\x1b[A', b'\x1bOA',
                                      b'\x1b[B', b'\x1bOB')
                      and not self.state.screen_has_active_dialog()):
                    # ↑/↓ outside capture — try Leap-managed history
                    # recall.  Reads the CLI's own persistent history
                    # via the provider and injects the recalled text
                    # back into the input box so a subsequent ``^^``
                    # captures the recalled message instead of an
                    # empty buffer.
                    #
                    # The ``screen_has_active_dialog`` gate covers the
                    # window where a dialog is rendered on screen but
                    # the state tracker hasn't yet flipped to
                    # ``NEEDS_PERMISSION`` (notably ``AskUserQuestion``,
                    # which fires no Notification hook and only flips
                    # via the 5 s cursor+silence fallback).  Without
                    # this check, arrows pressed during that window
                    # would be stolen for history recall and the user
                    # couldn't navigate the dialog for several seconds
                    # ("stuck for a moment, then unstuck" reports).
                    #
                    # Pre-flush: if the same chunk carried typed
                    # bytes ahead of the arrow (e.g. ``hello\x1b[A``
                    # arrives in one ``read()``), those bytes are
                    # still sitting in ``out`` waiting for pexpect
                    # to write them AFTER ``_input_filter`` returns.
                    # If we then call ``pty.send`` for our clear +
                    # inject, the CLI receives our writes first and
                    # the typed bytes land AFTER the recalled text
                    # — input box ends up as ``newesthello`` while
                    # the mirror correctly holds ``newest``.  Flush
                    # ``out`` directly via ``pty.send`` to lock in
                    # the [typed → clear → inject] order.
                    #
                    # The flushed bytes also need to be visible to
                    # ``state.on_input`` — without it, an Enter or
                    # Ctrl+C arriving in the same chunk before the
                    # arrow would slip past the state tracker (the
                    # end-of-filter ``state.on_input(out)`` only
                    # sees what's still in ``out``, and we just
                    # cleared it).  Fire on_input on the flushed
                    # slice so state transitions (idle→running on
                    # Enter, interrupt-pending on Ctrl+C) still fire.
                    if out:
                        flushed = bytes(out)
                        try:
                            self.pty.send(flushed)
                        except OSError:
                            pass
                        self.state.on_input(flushed)
                        out.clear()
                    direction = -1 if esc_seq in (b'\x1b[A', b'\x1bOA') else 1
                    if not self._handle_history_recall(direction):
                        # Provider opted out — preserve passthrough
                        # by emitting the escape to the CLI.  Typed
                        # bytes were already flushed above, so the
                        # CLI sees them before the arrow.
                        out.extend(esc_seq)
                else:
                    # Mirror cursor motion escapes so our
                    # _terminal_input_buf stays in sync with Claude.
                    if esc_seq == b'\x1b[D':  # Left
                        self._terminal_cursor_left()
                    elif esc_seq == b'\x1b[C':  # Right
                        self._terminal_cursor_right()
                    elif esc_seq in (b'\x1b[H', b'\x1b[1~'):  # Home
                        self._terminal_input_cursor = 0
                    elif esc_seq in (b'\x1b[F', b'\x1b[4~'):  # End
                        self._terminal_input_cursor = len(
                            self._terminal_input_buf)
                    elif esc_seq == b'\x1b[3~':  # Delete (forward)
                        self._terminal_buf_delete_forward()
                    out.extend(esc_seq)
                continue

            # --- Queue-capture mode: swallow input, queue on Enter ---
            if self._queue_capture_mode:
                i, dirty = self._capture_handle_char(
                    b, data, i, chunk_has_paste)
                capture_dirty |= dirty
                continue

            # --- Active bracketed paste: short-circuit all special-key
            # handlers so the pasted content reaches the accumulator
            # byte-for-byte.  Without this, characters like ``^``,
            # backspace, Ctrl+C, and other control bytes inside a
            # paste trigger their normal semantics (delete, clear buf,
            # etc.) and the raw content in the accumulator ends up
            # missing those bytes — the saved/resolved paste no
            # longer matches what the user actually pasted.
            if self._paste_accumulator is not None:
                self._paste_accumulator.append(b)
                out.append(b)
                # Track only printable chars in the terminal buf for
                # later truncation-to-snapshot in _finalize.  Control
                # chars that Claude renders as invisible (e.g. \t, \r)
                # don't bump visible-char counters.  Insert at the
                # mirrored cursor so pastes placed mid-line end up
                # in the correct position in our buf.
                if 0x20 <= b < 0x7f or b >= 0x80:
                    self._terminal_buf_insert(b)
                    self._chars_sent_to_cli += 1
                i += 1
                continue

            # "^^" (double caret) → queue capture mode.
            # First "^" is held as literal.  If the next byte is also
            # "^", capture triggers.  Otherwise the first "^" stays
            # as a literal character.
            # Skip trigger inside bracketed paste to prevent accidental
            # activation from pasted text containing "^^".
            if b == 0x5e:
                if chunk_has_paste:
                    # Inside bracketed paste — emit "^" literally and
                    # bypass the pending-caret state machine so pasted
                    # "^^" isn't mangled into a single "^".
                    out.append(0x5e)
                    self._terminal_buf_insert(0x5e)
                    self._chars_sent_to_cli += 1
                    i += 1
                    continue
                if self._pending_caret:
                    # Second "^" → capture (same chunk).
                    # The first "^" was held (never added to out or
                    # buf), so no stale caret on CLI.
                    if self._pending_caret_timer is not None:
                        self._pending_caret_timer.cancel()
                        self._pending_caret_timer = None
                    self._enter_capture_mode(
                        stale_cli_input=bool(self._terminal_input_buf),
                        stale_caret=False,
                    )
                    i += 1
                    continue
                else:
                    # First "^" — hold it, wait for second.
                    # Do NOT add to out or buf yet — if the next byte
                    # is also "^", capture triggers and the CLI never
                    # sees the "^" (no stale caret to clean up).
                    # Start a timer to flush as literal after 200ms.
                    self._pending_caret = True
                    self._pending_caret_time = time.time()
                    if self._pending_caret_timer is not None:
                        self._pending_caret_timer.cancel()
                    self._pending_caret_timer = threading.Timer(
                        0.2, self._flush_pending_caret)
                    self._pending_caret_timer.daemon = True
                    self._pending_caret_timer.start()
                    i += 1
                    continue

            # If we were waiting for a second "^" but got something
            # else, the pending caret was a literal — flush it now.
            elif self._pending_caret:
                if self._pending_caret_timer is not None:
                    self._pending_caret_timer.cancel()
                    self._pending_caret_timer = None
                self._pending_caret = False
                out.append(0x5e)
                self._terminal_buf_insert(0x5e)
                self._chars_sent_to_cli += 1

            if in_prompt:
                out.append(b)
                i += 1
                continue

            # --- Normal handling ---
            # Paste-mode bytes were handled earlier by the
            # active-paste short-circuit, so any \r here is a real
            # Enter keypress outside a paste.
            if b == 0x0d:  # Enter
                self._user_has_typed = True
                if self._terminal_input_buf:
                    msg = self._terminal_input_buf.decode(
                        'utf-8', errors='replace').strip()
                    if msg:
                        self.queue.track_sent(msg)
                    self._terminal_input_buf.clear()
                self._terminal_input_cursor = 0
                self._chars_sent_to_cli = 0
                # User committed input — discard any preserved text.
                self._preserved_input_buf.clear()
                self._preserved_chars_sent = 0
                # Clear pending paste images — the user committed
                # the current input to the CLI.  Keeping stale images
                # across Enter presses causes them to silently
                # accumulate and get injected into a later ^^ message.
                self._pending_paste_images.clear()
                self._gc_paste_text_map()
                # Reset CLI history-recall so the next ↑ re-reads the
                # provider's history file (the just-submitted message
                # is now on disk) and re-snapshots the live buffer.
                self._reset_history_recall()
                out.append(b)
            elif b == 0x7f:  # Backspace
                self._terminal_buf_backspace()
                out.append(b)
                self._chars_sent_to_cli = max(
                    0, self._chars_sent_to_cli - 1)
            elif b == 0x03:  # Ctrl+C — discard buffer
                self._terminal_input_buf.clear()
                self._terminal_input_cursor = 0
                self._chars_sent_to_cli = 0
                self._preserved_input_buf.clear()
                self._preserved_chars_sent = 0
                self._pending_paste_images.clear()
                self._gc_paste_text_map()
                self._reset_history_recall()
                out.append(b)
            elif b == 0x16:  # Ctrl+V — save clipboard image for next ^^
                if not chunk_has_paste:
                    path = self._save_clipboard_image()
                    if path:
                        pos = self._terminal_input_cursor
                        self._pending_paste_images.append((pos, path))
                out.append(b)
            elif b == 0x15:  # Ctrl+U — kill line from cursor to start
                # Mirror Claude's kill-line behavior.  Only drops
                # the chars before the cursor; anything after stays.
                if self._terminal_input_cursor > 0:
                    del self._terminal_input_buf[
                        :self._terminal_input_cursor]
                    self._chars_sent_to_cli = max(
                        0,
                        self._chars_sent_to_cli
                        - self._terminal_input_cursor,
                    )
                    self._terminal_input_cursor = 0
                out.append(b)
            elif b == 0x01:  # Ctrl+A — cursor to start of line
                self._terminal_input_cursor = 0
                out.append(b)
            elif b == 0x05:  # Ctrl+E — cursor to end of line
                self._terminal_input_cursor = len(
                    self._terminal_input_buf)
                out.append(b)
            elif 0x20 <= b < 0x7f or b >= 0x80:
                # Insert at cursor so text typed between placeholders
                # appears in the right order in our mirror of Claude's
                # input line.
                self._terminal_buf_insert(b)
                out.append(b)
                self._chars_sent_to_cli += 1
            else:
                out.append(b)
            i += 1

        # Deferred display update after paste in capture mode — one
        # refresh instead of one per character.
        if capture_dirty and self._queue_capture_mode:
            self._capture_display(self._capture_text())

        # Track input for state detection using only the bytes that
        # actually reach the CLI.  Capture-mode keystrokes are excluded
        # so they don't affect interrupt detection or trigger
        # idle→running on Enter.
        if out:
            self.state.on_input(bytes(out))

        return bytes(out)

    def _output_filter(self, data: bytes) -> bytes:
        """
        Filter PTY output to inject notifications and strip title escapes.

        Wrapped in try/except — any crash here kills pexpect's interact
        loop and terminates the PTY.

        Args:
            data: Raw output bytes.

        Returns:
            Filtered output bytes with title sequences removed and
            notifications injected.
        """
        try:
            return self._output_filter_impl(data)
        except Exception:
            if self._queue_capture_mode or time.monotonic() < self._suppress_send_until:
                return b''
            return data

    def _output_filter_impl(self, data: bytes) -> bytes:
        """Implementation of _output_filter (separated for crash protection)."""
        # Apply deferred SIGWINCH resize outside the signal context
        # (signal handlers must not acquire locks).
        self._apply_pending_resize()

        # Strip OSC title-change sequences so the CLI cannot override
        # the "lps <tag>" tab name used by the monitor for navigation.
        data = self._OSC_TITLE_RE.sub(b'', data)

        # Delegate state detection to the state tracker.
        self.state.on_output(data)

        # Signal PTY handler that output was received (used by
        # send_image_message to replace fixed sleeps with event waits).
        self.pty.notify_output_received()

        # Suppress during capture (TUI redraws on exit) and during
        # message send (hides echo so delivery is invisible).
        if self._queue_capture_mode or time.monotonic() < self._suppress_send_until:
            return b''

        # Track last output time so _title_keeper_loop can avoid
        # writing to stdout while the CLI is actively rendering.
        self._last_output_time = time.time()

        return data

    def _print_startup_banner(self) -> None:
        """Print the startup banner with help information."""
        print_banner('server', self.tag, cli_name=get_display_name(self._provider.name))
        print("  All responses will appear HERE in this window.")
        print("")
        print("  To open a client session for queue control, run in another tab:")
        print(f"    leap {self.tag}")
        print("  (all client features are also available in Leap Monitor)")
        print("")
        print("  Ctrl+C to exit")
        print("=" * 80)
        print()

        if not self.queue.is_empty:
            print(f"\U0001f4dd Queue has {self.queue.size} messages\n")

    def run(self) -> None:
        """Run the server main loop."""
        set_terminal_title(f"lps {self.tag}")
        self._print_startup_banner()
        self.pty.spawn()

        # Start background threads
        self.socket_handler.start()
        threading.Thread(target=self._auto_sender_loop, daemon=True).start()
        threading.Thread(target=self._title_keeper_loop, daemon=True).start()
        threading.Thread(target=self._stdin_watchdog_loop, daemon=True).start()

        # Wait for the socket to be bound before releasing the startup lock,
        # so concurrent `leap <tag>` invocations see the socket and connect as
        # clients instead of trying to start a second server.
        self.socket_handler.wait_ready()

        # Write the pid_map only AFTER the socket is listening so the
        # hook's PPID-walker can treat ``pid_map exists ⟹ socket is bound``
        # as a hard invariant.  Without this ordering, a hook firing in
        # the narrow window between map-write and bind would fail the
        # socket-existence staleness guard and drop the signal.
        self._write_cli_pid_map()

        # Release the shell startup lock now that the socket is listening.
        # The lock dir was created by leap-main.sh to prevent duplicate
        # servers; the shell trap can't clean it because exec replaced the
        # shell with this Python process.
        self._release_startup_lock()

        # Sync pyte screen dimensions with the actual terminal.
        # The pyte virtual terminal starts at a default size (200x50),
        # but the real terminal may be larger (e.g. 362x75).  SIGWINCH
        # only fires on subsequent resizes, not at startup — without
        # this initial sync, content rendered beyond pyte's default
        # rows is clamped to the last row and garbled, making
        # permission dialog options at the bottom of the screen
        # invisible.
        cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        self.state.on_resize(rows, cols)

        # Handle signals
        signal.signal(signal.SIGWINCH, self._handle_resize)
        signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
        signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
        signal.signal(signal.SIGHUP, lambda s, f: sys.exit(0))

        # Reset title (CLI may have changed it).
        # Skip JetBrains ideScript rename — the user may have switched
        # tabs during CLI startup, so getSelectedContent() would be wrong.
        # The initial set_terminal_title() call already renamed the tab.
        set_terminal_title(f"lps {self.tag}", vscode_rename=False)

        try:
            self.pty.interact(
                output_filter=self._output_filter,
                input_filter=self._input_filter,
            )
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception as e:
            print(f"\nError in interact: {e}", file=sys.stderr)
        finally:
            self.cleanup()

    def _release_startup_lock(self) -> None:
        """Remove the shell startup lock directory.

        The lock dir is created by leap-main.sh (mkdir) to prevent
        duplicate servers.  Because the shell uses ``exec`` to hand off
        to Python, the shell trap never fires, so we clean it up here.
        """
        lock_dir = SOCKET_DIR / f"{self.tag}.server.lock"
        try:
            lock_dir.rmdir()
        except OSError:
            pass

    def _write_cli_pid_map(self) -> None:
        """Write a PID-to-session mapping file for the spawned CLI process.

        Hook scripts use LEAP_TAG/LEAP_SIGNAL_DIR env vars, but some CLIs
        (e.g. Codex) may not pass the parent environment to hook
        subprocesses.  This mapping file under ``.storage/pid_maps/``
        lets the hook discover the session by walking up its parent PID
        chain.
        """
        if not self.pty.process:
            return
        cli_pid = self.pty.process.pid
        # Keep all state under `.storage/` so the project is self-contained
        # and normal uninstall cleans up after itself.  The hook walks up
        # its PPID chain looking for ``<project>/.storage/pid_maps/<ppid>.json``
        # — it resolves ``<project>`` from ``$LEAP_PROJECT_DIR`` if set or
        # regex-reads ``~/.zshrc`` / ``~/.bashrc`` (the install-time
        # anchor).
        pid_map_dir = STORAGE_DIR / 'pid_maps'
        pid_map_dir.mkdir(parents=True, exist_ok=True)
        self._cli_pid_map_file = pid_map_dir / f'{cli_pid}.json'
        try:
            atomic_write_json(self._cli_pid_map_file, {
                'tag': self.tag,
                'signal_dir': str(SOCKET_DIR),
                'python': sys.executable,
                # The hook needs to know which provider fired it so it can
                # route session-id recording to `.storage/cli_sessions/<cli>/`.
                # Codex (and potentially others) strips env vars from hook
                # subprocesses, so the hook's env-var fallback here is the
                # only way to recover LEAP_CLI_PROVIDER.
                'cli_provider': self.pty.provider.name,
            })
        except OSError:
            pass

    def _cleanup_cli_pid_map(self) -> None:
        """Remove the CLI PID mapping file."""
        pid_file = getattr(self, '_cli_pid_map_file', None)
        if pid_file:
            try:
                pid_file.unlink(missing_ok=True)
            except OSError:
                pass

    def cleanup(self) -> None:
        """Clean up all resources."""
        self.running = False
        self._release_startup_lock()
        self.socket_handler.stop()
        self.socket_handler.cleanup()
        # Kill the CLI BEFORE unlinking ``<tag>.meta`` — the hook
        # subprocess reads the meta to capture ``terminal_app`` and
        # dedups by session_id, so a hook fire that races our
        # ``metadata.cleanup()`` and reads a missing file would
        # overwrite the prior good record with an empty entry (the
        # ``record_session`` defense-in-depth picks the prior up in
        # that case, but narrower windows are still better).
        self.pty.terminate()
        self.metadata.cleanup()
        # Strip the ``lps <tag>`` tab name back to just ``<tag>`` so a
        # post-server shell prompt (or whatever takes over the tab next)
        # doesn't carry the Leap label.  Done AFTER ``pty.terminate()``
        # so a CLI that emits its own OSC title sequence on exit (most
        # TUIs do, to restore the parent shell's title) can't overwrite
        # ours.  Best-effort: a stdout-closed terminal can't be helped.
        try:
            set_terminal_title(self.tag)
        except Exception:
            pass
        self.state.cleanup()
        self.output_capture.cleanup()
        self._cleanup_cli_pid_map()
        # Remove queue file if empty (no pending messages).
        self.queue.delete_file_if_empty()


def main() -> None:
    """Entry point for leap-server command."""
    if len(sys.argv) < 2:
        print("Usage: leap-server <tag> [--cli claude|codex|copilot|cursor-agent|gemini] [--flags...]")
        sys.exit(1)

    tag = sys.argv[1]

    if tag.startswith('-'):
        print("Error: Tag cannot start with '-'")
        print("Usage: leap-server <tag> [--cli claude|codex|copilot|cursor-agent|gemini] [--flags...]")
        sys.exit(1)

    # Extract --cli option (consumed by Leap, not passed to the CLI)
    cli_name = None
    remaining_args = sys.argv[2:]
    flags: list[str] = []
    i = 0
    while i < len(remaining_args):
        tok = remaining_args[i]
        if tok == '--cli':
            # Bare ``--cli`` (or ``--cli`` followed by another ``--flag``,
            # or ``--cli ""``) used to leak through as a literal flag on
            # the CLI / silently fall back to the default provider.
            # Treat all three as hard errors so the misuse surfaces
            # immediately instead of as an opaque "unknown flag" or a
            # surprise default-provider session.
            if (
                i + 1 >= len(remaining_args)
                or not remaining_args[i + 1]
                or remaining_args[i + 1].startswith('--')
            ):
                _release_server_lock(tag)
                print(
                    "Error: --cli requires a value (e.g. --cli claude)",
                    file=sys.stderr,
                )
                sys.exit(1)
            cli_name = remaining_args[i + 1]
            i += 2
        elif tok.startswith('--cli='):
            cli_name = tok.split('=', 1)[1]
            if not cli_name:
                _release_server_lock(tag)
                print(
                    "Error: --cli= requires a value (e.g. --cli=claude)",
                    file=sys.stderr,
                )
                sys.exit(1)
            i += 1
        else:
            # Forward every other token to the CLI, not just `--*` prefixed
            # ones — `--flag value` pairs and subcommand forms (e.g. Codex
            # `resume <uuid>`) need the value to come through intact.
            flags.append(tok)
            i += 1

    # `leap --resume` hands us a session id + CLI via env vars.  Ask the
    # provider for its resume-argv (prepended so positional subcommand
    # forms like Codex `resume <id>` stay in the front) and then strip
    # those env vars so they don't leak into the CLI process.
    #
    # If LEAP_RESUME_SESSION_ID is set we MUST honor it — silently
    # falling through to a fresh session would look like a normal
    # startup and the user would never realise their resume was lost.
    # Every "can't honor" case below exits non-zero with a clear
    # stderr message.
    resume_id = os.environ.pop('LEAP_RESUME_SESSION_ID', '')
    resume_cli = os.environ.pop('LEAP_RESUME_CLI', '')
    if resume_id:
        _apply_resume_or_fail(resume_id, resume_cli, cli_name, tag)
        # _apply_resume_or_fail either exits or returns the (flags, cli_name)
        # that should be used.  We re-fetch via locals because Python doesn't
        # have output params; re-implement inline for clarity:
        try:
            provider = get_provider(resume_cli)
        except ValueError:
            provider = None  # _apply_resume_or_fail already exited; defensive
        # mypy: provider is non-None here because _apply_resume_or_fail
        # exited if it was None.
        assert provider is not None
        flags = provider.resume_args(resume_id) + flags
        if not cli_name:
            cli_name = resume_cli

    # Gate: refuse to start if Leap's hooks aren't wired up for this CLI.
    # Hooks drive session tracking, /resume, Slack output capture, and
    # permission detection — without them the session would silently
    # misbehave.  Custom CLIs are variants of one of the five base CLIs
    # (claude / codex / copilot / cursor-agent / gemini); we check the *base*
    # provider's hooks_installed() since they share its hook-config dir.
    _enforce_hooks_installed_or_exit(cli_name, tag=tag)

    server = LeapServer(tag, flags=flags, cli=cli_name)
    server.run()


def _release_server_lock(tag: Optional[str]) -> None:
    """Best-effort release of ``<tag>.server.lock`` from ``SOCKET_DIR``.

    Used by every code path in ``main()`` that exits non-zero before
    ``LeapServer(...)`` is instantiated.  ``leap-main.sh`` acquires the
    lock dir and registers a bash trap to clean it up on exit, but
    that trap is gone after ``exec`` replaces the bash process with
    Python — so any ``sys.exit(1)`` here would leak the lock without
    this manual rmdir.  ``LeapServer.__init__`` does the same on
    ``validate_pinned_session`` failures.
    """
    if not tag:
        return
    try:
        (SOCKET_DIR / f"{tag}.server.lock").rmdir()
    except OSError:
        pass


def _enforce_hooks_installed_or_exit(
    cli_name: Optional[str], *, tag: Optional[str] = None,
) -> None:
    """Block server start when the picked CLI's hooks aren't wired up.

    Typically means the CLI was installed after Leap (so install-time
    hook configuration silently skipped it).  Print a clear remediation
    pointing at ``leap --reconfigure`` and exit with code 1.
    """
    try:
        provider = get_provider(cli_name)
        base = get_provider(provider.base_type)
    except ValueError:
        # Unknown provider (or a custom CLI whose base_type points at a
        # missing built-in — should be impossible if the registry is
        # consistent, but cheap to guard).  Let LeapServer surface the
        # canonical error so we don't double-emit the same diagnostic.
        return
    if base.hooks_installed():
        return
    _release_server_lock(tag)
    yellow = "\033[33m"
    bold = "\033[1m"
    reset = "\033[0m"
    name = provider.display_name
    sys.stderr.write(
        f"\n  {yellow}✗ Leap's hooks aren't configured for {name}.{reset}\n\n"
        f"  This usually means {name} was installed after Leap.\n"
        f"  Without hooks, session tracking, /resume, Slack output, and\n"
        f"  permission detection won't work for this CLI.\n\n"
        f"  Fix:\n"
        f"      {bold}leap --reconfigure{reset}\n\n"
    )
    sys.exit(1)


def _apply_resume_or_fail(
    resume_id: str, resume_cli: str, cli_name: Optional[str], tag: str,
) -> None:
    """Validate that we can honor a ``LEAP_RESUME_*`` hand-off.

    Exits with a clear stderr message and exit code 1 in any case
    where the resume would be silently dropped — see Question 3 of
    the resume bug-hunt: silent drops are confusing because the user
    sees a normal-looking session whose history is gone.

    Cases that exit:

    * ``LEAP_RESUME_CLI`` is empty — the picker should always set
      both, so an unset value is a misuse.
    * Provider name is unknown to the registry.
    * Provider exists but doesn't implement ``supports_resume``.
    * ``--cli <X>`` and ``LEAP_RESUME_CLI <Y>`` disagree — we'd be
      asked to apply CLI X's resume args against CLI Y's argv.
    """
    yellow = "\033[33m"
    reset = "\033[0m"
    short = resume_id[:8] + "…" if len(resume_id) > 8 else resume_id

    if not resume_cli:
        print(
            f"\n  {yellow}✗ Refusing to start: LEAP_RESUME_SESSION_ID is set "
            f"but LEAP_RESUME_CLI is empty.{reset}\n"
            f"  Cannot apply resume {short} without a CLI provider name.\n"
            f"  This usually means the resume hand-off was constructed "
            f"manually - re-run `leap --resume` to pick a session.\n",
            file=sys.stderr,
        )
        _release_server_lock(tag)
        sys.exit(1)

    if cli_name and cli_name != resume_cli:
        print(
            f"\n  {yellow}✗ Refusing to start: --cli='{cli_name}' "
            f"conflicts with LEAP_RESUME_CLI='{resume_cli}'.{reset}\n"
            f"  Resume session {short} was recorded for "
            f"'{resume_cli}'; can't apply it to '{cli_name}'.\n"
            f"  Either drop --cli or unset LEAP_RESUME_*.\n",
            file=sys.stderr,
        )
        _release_server_lock(tag)
        sys.exit(1)

    try:
        provider = get_provider(resume_cli)
    except ValueError:
        print(
            f"\n  {yellow}✗ Refusing to start: unknown CLI provider "
            f"'{resume_cli}' from LEAP_RESUME_CLI.{reset}\n"
            f"  Cannot apply resume {short}.\n"
            f"  This may mean the provider was removed or renamed in "
            f"a recent Leap update.\n",
            file=sys.stderr,
        )
        _release_server_lock(tag)
        sys.exit(1)

    if not provider.supports_resume:
        print(
            f"\n  {yellow}✗ Refusing to start: provider '{resume_cli}' "
            f"does not support resume.{reset}\n"
            f"  Cannot apply resume {short} for tag '{tag}'.\n",
            file=sys.stderr,
        )
        _release_server_lock(tag)
        sys.exit(1)


if __name__ == "__main__":
    main()
