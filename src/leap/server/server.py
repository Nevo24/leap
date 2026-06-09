"""
Main Leap PTY Server.

Orchestrates PTY handling, socket server, and queue management.
"""

import atexit
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.registry import get_display_name, get_provider
from leap.cli_providers.states import AutoSendMode, ChurnQueueMode, CLIState, PROMPT_STATES
from leap.utils.atomic_write import atomic_write_json
from leap.utils.constants import (
    QUEUE_DIR, SOCKET_DIR, HISTORY_DIR, NOTE_IMAGES_DIR, QUEUE_IMAGES_DIR,
    STORAGE_DIR,
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
from leap.server._mixins.capture_input_mixin import CaptureInputMixin
from leap.server._mixins.io_filter_mixin import IOFilterMixin
from leap.server._mixins.background_loops_mixin import BackgroundLoopsMixin




class LeapServer(CaptureInputMixin, IOFilterMixin, BackgroundLoopsMixin):
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

        # Remove stale context file from a previous session with the same tag
        # so the monitor never briefly shows an old window/usage before the
        # status line fires for the first time.  Applies to both Claude and
        # Copilot (any CLI that writes <tag>.context via its status line).
        _context_file = SOCKET_DIR / f"{tag}.context"
        if _context_file.exists():
            try:
                _context_file.unlink()
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
        # Churn-queue mode (whether to auto-send while a background monitor is
        # active): global default -> per-session pin, the same snapshot model
        # as auto_send_mode so a later global change can't retroactively flip
        # an already-open session.  Coerced to a valid value defensively.
        global_churn = load_settings().get('churn_queue_mode', ChurnQueueMode.WAIT)
        if global_churn not in (ChurnQueueMode.SEND, ChurnQueueMode.WAIT):
            global_churn = ChurnQueueMode.WAIT
        pinned_churn = self._load_pinned_churn_queue_mode(tag, global_churn)
        if pinned_churn not in (ChurnQueueMode.SEND, ChurnQueueMode.WAIT):
            pinned_churn = ChurnQueueMode.WAIT
        self._save_pinned_churn_queue_mode(tag, pinned_churn)
        self.state = CLIStateTracker(
            signal_file=SOCKET_DIR / f"{tag}.signal",
            auto_send_mode=pinned_mode,
            churn_queue_mode=pinned_churn,
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

    @staticmethod
    def _load_pinned_churn_queue_mode(tag: str, default: str) -> str:
        """Read churn_queue_mode from pinned sessions for this tag.

        Twin of ``_load_pinned_auto_send_mode`` (same corruption-tolerance on
        the ``__init__`` snapshot path); see it for the rationale.
        """
        pinned_file = STORAGE_DIR / "pinned_sessions.json"
        try:
            if pinned_file.exists():
                with open(pinned_file, 'r') as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    return default
                entry = loaded.get(tag, {})
                if not isinstance(entry, dict):
                    return default
                mode = entry.get('churn_queue_mode', default)
                return mode if isinstance(mode, str) and mode else default
        except (OSError, ValueError):
            pass
        return default

    @staticmethod
    def _save_pinned_churn_queue_mode(tag: str, mode: str) -> None:
        """Persist churn_queue_mode in pinned sessions for this tag.

        Twin of ``_save_pinned_auto_send_mode`` (creates the file/entry and
        self-heals a corrupt pin file); see it for the rationale.
        """
        pinned_file = STORAGE_DIR / "pinned_sessions.json"
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
            if entry.get('churn_queue_mode') == mode:
                return
            entry['churn_queue_mode'] = mode
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
            state = self.state.get_state(
                self.pty.is_alive(),
                has_pending_input=bool(self._terminal_input_buf)
                or self._queue_capture_mode,
            )
            recently_sent, total_sent = self.queue.get_recently_sent()
            return {
                'queue_size': self.queue.size,
                'queue_contents': self.queue.get_contents(),
                'recently_sent': recently_sent,
                'total_sent': total_sent,
                'ready': self.state.is_ready_for_state(state),
                'cli_state': state,
                'auto_send_mode': self.state.auto_send_mode,
                'churn_queue_mode': self.state.churn_queue_mode,
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

        elif msg_type == 'set_churn_queue_mode':
            mode = msg.get('mode', '')
            if mode not in (ChurnQueueMode.SEND, ChurnQueueMode.WAIT):
                return {'status': 'error',
                        'message': f"Invalid mode: {mode}. Use 'send' or 'wait'."}
            self.state.churn_queue_mode = mode
            # Per-session toggle only; the global default is written solely by
            # the Settings dialog (mirrors set_auto_send_mode above).  No
            # immediate side effect - the auto-sender re-reads readiness each
            # poll, so a switch to SEND dispatches on the next loop iteration.
            self._save_pinned_churn_queue_mode(self.tag, mode)
            return {'status': 'ok', 'churn_queue_mode': mode}

        elif msg_type == 'interrupt':
            # Interrupt key is provider-specific: Escape cancels in
            # Claude/Codex/Cursor/Gemini, but GitHub Copilot ignores
            # Escape mid-turn and cancels on Ctrl+C.  on_input arms
            # _interrupt_pending; pty.send delivers the keystroke.
            interrupt_key = self._provider.interrupt_key
            self.state.on_input(interrupt_key)
            self.pty.send(interrupt_key.decode('latin-1'))
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
