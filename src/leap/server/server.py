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
import traceback
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.base import CLIProvider
from leap.cli_providers.registry import get_display_name, get_provider
from leap.cli_providers.states import AutoSendMode, CLIState, PROMPT_STATES, WAITING_STATES
from leap.utils.constants import (
    QUEUE_DIR, SOCKET_DIR, HISTORY_DIR, QUEUE_IMAGES_DIR, STORAGE_DIR,
    POLL_INTERVAL, TITLE_RESET_INTERVAL,
    atomic_json_write, ensure_storage_dirs, load_settings, save_settings,
)
from leap.utils.terminal import set_terminal_title, print_banner
from leap.server.pty_handler import PTYHandler
from leap.server.socket_handler import SocketHandler
from leap.server.queue_manager import QueueManager
from leap.server.metadata import SessionMetadata
from leap.server.state_tracker import CLIStateTracker
from leap.slack.output_capture import OutputCapture
from leap.server.validation import validate_pinned_session


def _extract_menu_options(
    prompt_output: str,
    provider: Optional[CLIProvider] = None,
) -> list[tuple[int, str]]:
    """Extract numbered menu options from prompt output.

    The prompt may contain numbered content (e.g. plan steps) above the
    actual TUI options.  Both match the ``N. label`` pattern, so we
    return only the **last** contiguous 1..n sequence — the real menu.

    Args:
        prompt_output: Rendered prompt text.
        provider: CLI provider (for custom regex). Defaults to default provider.
    """
    if provider and not provider.has_numbered_menus:
        return []

    pattern = (
        provider.menu_option_regex
        if provider and provider.menu_option_regex
        else re.compile(r'\s*(?:[❯›]\s*)?(\d+)\.\s+(.+)')
    )

    all_matches: list[tuple[int, str]] = []
    for line in prompt_output.split('\n'):
        m = pattern.match(line)
        if m:
            all_matches.append((int(m.group(1)), m.group(2).strip()))

    if not all_matches:
        return []

    # Walk backwards to the last match numbered "1".
    last_one_idx = -1
    for i in range(len(all_matches) - 1, -1, -1):
        if all_matches[i][0] == 1:
            last_one_idx = i
            break

    if last_one_idx == -1:
        return all_matches  # no "1" found — return all as fallback

    # Take the contiguous ascending sequence from that point.
    result: list[tuple[int, str]] = []
    expected = 1
    for i in range(last_one_idx, len(all_matches)):
        num, label = all_matches[i]
        if num == expected:
            result.append((num, label))
            expected += 1
        else:
            break

    return result


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
            cli: CLI provider name ('claude', 'codex', 'cursor-agent', 'gemini'). Defaults to 'claude'.
        """
        self.tag = tag
        self.running = True
        self._provider = get_provider(cli)

        # Ensure storage directories exist
        ensure_storage_dirs()
        self._cleanup_old_images()

        # Validate against monitor pinned sessions (PR-pinned rows).
        # Release the startup lock on failure so another server can start.
        lock_dir = SOCKET_DIR / f"{tag}.server.lock"
        try:
            validate_pinned_session(tag, STORAGE_DIR)
        except SystemExit:
            try:
                lock_dir.rmdir()
            except OSError:
                pass
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

        # State tracking — per-session pinned mode overrides global default
        global_mode = load_settings().get('auto_send_mode', AutoSendMode.PAUSE)
        pinned_mode = self._load_pinned_auto_send_mode(tag, global_mode)
        self.state = CLIStateTracker(
            signal_file=SOCKET_DIR / f"{tag}.signal",
            auto_send_mode=pinned_mode,
            provider=self._provider,
        )
        self.output_capture = OutputCapture(tag, cli_provider=self._provider.name)
        self._terminal_input_buf: bytearray = bytearray()
        # Tracks incomplete escape sequences split across os.read() chunks.
        # None = no partial.  'esc' = bare \x1b at end (need type byte).
        # 'csi' = \x1b[ + optional params at end (need final byte 0x40-0x7e).
        self._partial_escape: Optional[str] = None
        self._user_has_typed: bool = False  # True after first Enter in the terminal
        # Previous state seen by _input_filter — used to clear stale bytes
        # when transitioning from running to idle (prevents keyboard-layout
        # artefacts from leaking into the tracked "last message").
        self._prev_filter_state: Optional[CLIState] = None
        # Queue-from-server: "^" prefix capture mode.
        # When "^" is the first char on a line we enter capture mode
        # and swallow all subsequent input until Enter → queue.
        self._queue_capture_mode: bool = False
        self._queue_capture_buf: bytearray = bytearray()
        self._capture_output_buf: bytearray = bytearray()  # buffered CLI output
        self._capture_stale_cli_input: bool = False  # CLI has stale text from ^^
        self._capture_cursor_pos: int = 0  # character cursor in capture text
        self._capture_show_hint: bool = True  # show hint until first keystroke
        self._capture_prev_lines: int = 0  # wrapped line count from last display
        self._capture_utf8_buf: bytearray = bytearray()  # incomplete UTF-8 bytes
        self._capture_image_counter: int = 0
        self._capture_image_map: dict[str, str] = {}  # "[Image #N]" → path
        # True when a single "^" was typed mid-text, waiting to see
        # if the next byte is also "^" (double-caret → capture mode).
        self._pending_caret: bool = False
        # Saved message history (^^ inside capture mode saves + clears).
        # Browsed with arrow up/down.  Persisted to .storage/.
        self._saved_messages: list[str] = self._load_saved_messages()
        self._saved_msg_index: int = -1  # -1 = not browsing
        self._capture_show_saved_hint: bool = False  # "Saved!" hint active

        # Clean up old history files
        self._cleanup_old_history_files()

        # Load existing queue and save metadata (include CLI provider)
        self.queue.load()
        self.metadata.save(cli_provider=self._provider.name)

        # Prompt user about old queue messages
        if not self.queue.is_empty:
            self._prompt_load_old_queue()

        # Register cleanup
        atexit.register(self.cleanup)

    @staticmethod
    def _load_pinned_auto_send_mode(tag: str, default: str) -> str:
        """Read auto_send_mode from pinned sessions if set for this tag."""
        pinned_file = STORAGE_DIR / "pinned_sessions.json"
        try:
            if pinned_file.exists():
                with open(pinned_file, 'r') as f:
                    pinned = json.load(f)
                entry = pinned.get(tag, {})
                return entry.get('auto_send_mode', default)
        except (json.JSONDecodeError, OSError):
            pass
        return default

    @staticmethod
    def _save_pinned_auto_send_mode(tag: str, mode: str) -> None:
        """Persist auto_send_mode in pinned sessions for this tag."""
        pinned_file = STORAGE_DIR / "pinned_sessions.json"
        try:
            if pinned_file.exists():
                with open(pinned_file, 'r') as f:
                    pinned = json.load(f)
                if tag in pinned:
                    pinned[tag]['auto_send_mode'] = mode
                    atomic_json_write(pinned_file, pinned)
        except (json.JSONDecodeError, OSError):
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
            options = _extract_menu_options(prompt, self._provider)
            options_dict = {num: label for num, label in options}

            # Delegate option selection to the provider (handles
            # CLI-specific behaviors like arrow-key nav, y/n, etc.)
            # Call on_send() only after the provider confirms it will
            # actually send something — on_send() irreversibly clears
            # state tracker buffers.
            result = self._provider.select_option(
                option_num, options_dict,
                self.pty.send, self.pty.sendline,
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
            options = _extract_menu_options(prompt, self._provider)
            options_dict = {num: label for num, label in options}

            result = self._provider.send_custom_answer(
                message, options_dict, self.pty.send,
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
            settings = load_settings()
            settings['auto_send_mode'] = mode
            save_settings(settings)
            self._save_pinned_auto_send_mode(self.tag, mode)
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
        # If ^^ capture left stale text in the CLI's input, clear it
        # with Ctrl+C before sending.  At idle prompt, Ctrl+C just
        # clears the input line without side effects.
        if self._capture_stale_cli_input:
            self._capture_stale_cli_input = False
            self.pty.send('\x03')
            time.sleep(0.2)

        self.state.on_send()

        is_img = self._provider.is_image_message(message) or self._has_image_ref(message)
        if is_img:
            self.pty.send_image_message(message)
        else:
            self.pty.sendline(message)

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

        Called once on server startup. Scans queue files (``id|message``
        format) and the presets JSON for image paths, then removes any
        image file not found in either.
        """
        if not QUEUE_IMAGES_DIR.is_dir():
            return

        images_dir_str = str(QUEUE_IMAGES_DIR)

        def _collect_refs(text: str) -> None:
            """Find all @<QUEUE_IMAGES_DIR>/... references in *text*."""
            for token in text.split():
                # Token may be bare @path or embedded after a | separator
                at_idx = token.find('@')
                if at_idx < 0:
                    continue
                path_part = token[at_idx + 1:]
                if path_part.startswith(images_dir_str):
                    referenced.add(path_part)

        referenced: set[str] = set()

        # Scan queue files (format: "id|message\n")
        if QUEUE_DIR.is_dir():
            for queue_file in QUEUE_DIR.iterdir():
                try:
                    _collect_refs(queue_file.read_text())
                except OSError:
                    pass

        # Scan presets JSON (format: {"name": ["msg1", ...], ...})
        presets_file = STORAGE_DIR / 'leap_presets.json'
        if presets_file.is_file():
            try:
                import json as _json
                data = _json.loads(presets_file.read_text())
                if isinstance(data, dict):
                    for messages in data.values():
                        if isinstance(messages, list):
                            for msg in messages:
                                if isinstance(msg, str):
                                    _collect_refs(msg)
            except (OSError, ValueError):
                pass

        # Delete unreferenced images
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
            time.sleep(POLL_INTERVAL)

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

                if self.queue.is_empty or not self.state.is_ready_for_state(current_state):
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
        """Background thread to maintain terminal title."""
        while self.running:
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
        """Handle terminal resize signal."""
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
                os.write(sys.stdout.fileno(),
                         (clear or '\r\x1b[K').encode())
                self._capture_prev_lines = 0
            else:
                q_size = self.queue.size
                q_part = f' \u2022 {q_size} queued'
                prefix = '[Leap Q] '
                hint = (f' \x1b[2m(Enter=queue \u2022 Esc=cancel \u2022 ^^=save'
                        f' \u2022 \u2191\u2193=history \u2022 Ctrl+V=image'
                        f'{q_part} \u2022 CLI runs in bg)\x1b[33m'
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
            atomic_json_write(
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
        # Show a "Saved!" hint on the capture line
        try:
            payload = (
                '\r\x1b[K'
                '\x1b[33m[Leap Q] \x1b[32mSaved!'
                ' \x1b[2m(any key to continue)\x1b[0m'
            ).encode()
            os.write(sys.stdout.fileno(), payload)
        except OSError:
            pass
        self._capture_show_saved_hint = True

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

        # Load the message at current index
        msg = self._saved_messages[self._saved_msg_index]
        self._queue_capture_buf = bytearray(msg.encode('utf-8'))
        self._capture_cursor_pos = len(msg)
        self._capture_display(self._capture_text())

    @staticmethod
    def _is_csi_u_cancel(seq: bytes) -> bool:
        """Check if a CSI sequence is Ctrl+C in kitty/xterm encoding."""
        from leap.server.state_tracker import CLIStateTracker
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

    def _capture_backspace(self) -> bool:
        """Delete character before cursor. Returns False if at start."""
        if self._capture_cursor_pos <= 0:
            return False
        self._saved_msg_index = -1  # editing resets history browsing
        text = self._capture_text()
        text = text[:self._capture_cursor_pos - 1] + text[self._capture_cursor_pos:]
        self._queue_capture_buf = bytearray(text.encode('utf-8'))
        self._capture_cursor_pos -= 1
        return True

    def _capture_delete(self) -> None:
        """Delete character at cursor (forward delete)."""
        text = self._capture_text()
        if self._capture_cursor_pos < len(text):
            text = text[:self._capture_cursor_pos] + text[self._capture_cursor_pos + 1:]
            self._queue_capture_buf = bytearray(text.encode('utf-8'))

    def _capture_flush(self, cancel: bool = False) -> None:
        """End capture mode: discard buffered output, handle stale input."""
        buf_size = len(self._capture_output_buf)
        if cancel and self._capture_stale_cli_input:
            self.pty.send('\x7f')
            self._capture_stale_cli_input = False
        # Discard buffered output — flushing stale TUI escape sequences
        # corrupts the display (especially after image paste which
        # accumulates thousands of bytes during the osascript block).
        # The TUI redraws naturally when it receives the next message.
        if self._capture_output_buf:
            self._capture_output_buf.clear()

    def _capture_paste_image(self) -> bool:
        """Try to paste a clipboard image into the capture buffer.

        Uses PyObjC (AppKit) to read the clipboard directly — no
        subprocess, so terminal raw mode settings are not corrupted.
        """
        import hashlib
        from leap.utils.constants import QUEUE_IMAGES_DIR
        try:
            from AppKit import NSPasteboard, NSPasteboardTypePNG, NSPasteboardTypeTIFF
        except ImportError:
            return False
        pb = NSPasteboard.generalPasteboard()
        # Check for PNG first, then TIFF (screenshots are often TIFF)
        png_data = pb.dataForType_(NSPasteboardTypePNG)
        if png_data is None:
            tiff_data = pb.dataForType_(NSPasteboardTypeTIFF)
            if tiff_data is None:
                return False
            # Convert TIFF to PNG via NSBitmapImageRep
            try:
                from AppKit import NSBitmapImageRep
                rep = NSBitmapImageRep.imageRepWithData_(tiff_data)
                if rep is None:
                    return False
                from AppKit import NSPNGFileType
                png_data = rep.representationUsingType_properties_(NSPNGFileType, None)
                if png_data is None:
                    return False
            except Exception as e:
                return False
        # Save with MD5 dedup (same logic as image_handler.save_clipboard_image)
        raw_bytes = bytes(png_data)
        content_hash = hashlib.md5(raw_bytes).hexdigest()[:12]
        QUEUE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        dest = QUEUE_IMAGES_DIR / f'{content_hash}.png'
        if not dest.is_file():
            dest.write_bytes(raw_bytes)
        path = str(dest)
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
        """Replace [Image #N] placeholders with @path references."""
        image_parts: list[str] = []
        for placeholder, path in self._capture_image_map.items():
            if placeholder in message:
                message = message.replace(placeholder, '')
                image_parts.append(f'@{path}')
        if image_parts:
            text = message.strip()
            result = (' '.join(image_parts) + ' ' + text).strip() if text else ' '.join(image_parts)
            return result
        return message

    def _capture_reset_images(self) -> None:
        """Reset image state for the next capture session."""
        self._capture_image_counter = 0
        self._capture_image_map.clear()

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
        # On crash, pass through raw data so the CLI at least receives it.
        try:
            return self._input_filter_impl(data)
        except Exception:
            return data

    def _input_filter_impl(self, data: bytes) -> bytes:
        """Implementation of _input_filter (separated for crash protection)."""
        # Note: on_input() is called AFTER the byte loop (see end of
        # method) with only the bytes that reach the CLI.  This prevents
        # capture-mode keystrokes from affecting state tracker flags
        # (e.g. false idle→running on Enter, or false _user_responded).

        current_state = self.state.current_state
        in_prompt = current_state in PROMPT_STATES

        self._prev_filter_state = current_state

        out = bytearray()
        i = 0

        # Check if the very first byte is "^" and _pending_caret is set
        # from the previous chunk → double-caret capture trigger.
        if (not self._queue_capture_mode
                and i < len(data)
                and data[i] == 0x5e
                and self._pending_caret):
            # Second "^" arrived in a new chunk.  Enter capture mode.
            # Don't erase CLI input now — it causes display corruption.
            # The stale input is cleaned up at send time via Ctrl+C.
            self._partial_escape = None
            if (self._terminal_input_buf
                    and self._terminal_input_buf[-1] == 0x5e):
                self._terminal_input_buf.pop()
            self._queue_capture_buf = bytearray(
                self._terminal_input_buf)
            self._capture_cursor_pos = len(self._capture_text())
            self._terminal_input_buf.clear()
            self._queue_capture_mode = True; self._capture_show_hint = True
            self._capture_output_buf.clear()
            self._capture_stale_cli_input = True
            self._pending_caret = False
            self._capture_prev_lines = 0
            self._saved_msg_index = -1
            self._capture_show_saved_hint = False
            self._capture_display(self._capture_text())
            i += 1

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
                        self._capture_display()
                        self._queue_capture_buf.clear()
                        self._queue_capture_mode = False
                        self._capture_flush(cancel=True)
                        self._capture_reset_images()
                        self._terminal_input_buf.clear()
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

                if self._queue_capture_mode:
                    # In capture mode: handle editing keys, cancel on
                    # Escape, drop everything else.
                    if self._capture_show_saved_hint:
                        self._capture_show_saved_hint = False
                        self._capture_display(self._capture_text())
                    seq = data[esc_start:i]
                    if seq in (b'\x1bb', b'\x1bf'):
                        # ESC-b / ESC-f (Option+arrows as Meta prefix).
                        # Handle as word movement, not standalone Escape.
                        text = self._capture_text()
                        p = self._capture_cursor_pos
                        if seq == b'\x1bb':  # word left
                            while p > 0 and text[p - 1] == ' ':
                                p -= 1
                            while p > 0 and text[p - 1] != ' ':
                                p -= 1
                        else:  # word right
                            while p < len(text) and text[p] != ' ':
                                p += 1
                            while p < len(text) and text[p] == ' ':
                                p += 1
                        self._capture_cursor_pos = p
                        self._capture_display(text)
                    elif is_standalone_esc:
                        self._capture_display()
                        self._queue_capture_buf.clear()
                        self._capture_cursor_pos = 0; self._capture_utf8_buf.clear()
                        self._queue_capture_mode = False
                        self._capture_flush(cancel=True)
                        self._capture_reset_images()
                        self._terminal_input_buf.clear()
                    elif self._is_csi_u_cancel(seq):
                        # CSI u Ctrl+C (e.g. \x1b[3u) — cancel capture
                        self._capture_display()
                        self._queue_capture_buf.clear()
                        self._capture_cursor_pos = 0; self._capture_utf8_buf.clear()
                        self._queue_capture_mode = False
                        self._capture_flush(cancel=True)
                        self._capture_reset_images()
                        self._terminal_input_buf.clear()
                    elif seq == b'\x1b[D':  # Left arrow
                        if self._capture_cursor_pos > 0:
                            self._capture_cursor_pos -= 1
                        self._capture_display(self._capture_text())
                    elif seq == b'\x1b[C':  # Right arrow
                        if self._capture_cursor_pos < len(self._capture_text()):
                            self._capture_cursor_pos += 1
                        self._capture_display(self._capture_text())
                    elif seq in (b'\x1b[1;3D', b'\x1bb'):  # Opt+Left (word left)
                        text = self._capture_text()
                        p = self._capture_cursor_pos
                        while p > 0 and text[p - 1] == ' ':
                            p -= 1
                        while p > 0 and text[p - 1] != ' ':
                            p -= 1
                        self._capture_cursor_pos = p
                        self._capture_display(text)
                    elif seq in (b'\x1b[1;3C', b'\x1bf'):  # Opt+Right (word right)
                        text = self._capture_text()
                        p = self._capture_cursor_pos
                        while p < len(text) and text[p] != ' ':
                            p += 1
                        while p < len(text) and text[p] == ' ':
                            p += 1
                        self._capture_cursor_pos = p
                        self._capture_display(text)
                    elif seq in (b'\x1b[H', b'\x1b[1~'):  # Home
                        self._capture_cursor_pos = 0; self._capture_utf8_buf.clear()
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
                else:
                    out.extend(data[esc_start:i])
                continue

            # --- Queue-capture mode: swallow input, queue on Enter ---
            # The terminal title bar shows "[Leap Q] <text>" as live feedback
            # since we can't echo into the TUI content area.
            # Must be checked before in_prompt so capture works in any state.
            if self._queue_capture_mode:
                # Dismiss "Saved!" hint on any key
                if self._capture_show_saved_hint and b != 0x5e:
                    self._capture_show_saved_hint = False
                    self._capture_display(self._capture_text())
                if b in (0x0d, 0x0a):  # Enter (CR or LF — LF after subprocess)
                    self._user_has_typed = True
                    self._capture_display()  # clear
                    msg = self._capture_text().strip()
                    if self._capture_image_map:
                        msg = self._capture_resolve_images(msg)
                    if msg:
                        self.queue.add(msg)
                        self._capture_flush()  # keep stale for Ctrl+C cleanup
                    else:
                        # Empty message — treat as cancel, erase stale ^
                        self._capture_flush(cancel=True)
                    self._queue_capture_buf.clear()
                    self._capture_cursor_pos = 0; self._capture_utf8_buf.clear()
                    self._queue_capture_mode = False
                    self._capture_reset_images()
                    self._terminal_input_buf.clear()
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
                    # If at start, just ignore (can't backspace past start)
                elif b == 0x03:  # Ctrl+C — cancel capture (discard)
                    self._capture_display()  # clear
                    self._queue_capture_buf.clear()
                    self._capture_cursor_pos = 0; self._capture_utf8_buf.clear()
                    self._queue_capture_mode = False
                    self._capture_flush(cancel=True)
                    self._capture_reset_images()
                    self._terminal_input_buf.clear()
                elif b == 0x5e:  # "^" in capture mode
                    if self._capture_show_saved_hint:
                        self._capture_show_saved_hint = False
                    if self._pending_caret:
                        # Double "^" inside capture → save message
                        self._pending_caret = False
                        # Remove the first "^" that was inserted
                        text = self._capture_text()
                        p = self._capture_cursor_pos
                        if p > 0 and text[p - 1] == '^':
                            text = text[:p - 1] + text[p:]
                            self._queue_capture_buf = bytearray(
                                text.encode('utf-8'))
                            self._capture_cursor_pos = p - 1
                        self._save_capture_message()
                    else:
                        # First "^" — insert it, wait for second
                        self._pending_caret = True
                        self._capture_show_hint = False
                        self._capture_insert('^')
                        self._capture_display(self._capture_text())
                elif 0x20 <= b < 0x7f:
                    # ASCII printable (non-caret)
                    if self._pending_caret:
                        self._pending_caret = False
                    if self._capture_show_saved_hint:
                        self._capture_show_saved_hint = False
                        self._capture_display(self._capture_text())
                    self._capture_show_hint = False
                    self._capture_insert(chr(b))
                    self._capture_display(self._capture_text())
                elif b >= 0x80:
                    # Multi-byte UTF-8
                    if self._pending_caret:
                        self._pending_caret = False
                    self._capture_show_hint = False
                    self._capture_utf8_buf.append(b)
                    try:
                        char = self._capture_utf8_buf.decode('utf-8')
                        self._capture_insert(char)
                        self._capture_utf8_buf.clear()
                    except UnicodeDecodeError:
                        pass  # incomplete, wait for more bytes
                    self._capture_display(self._capture_text())
                else:
                    if self._pending_caret:
                        self._pending_caret = False
                # Swallowed — nothing goes to out.
                i += 1
                continue

            # "^^" (double caret) → queue capture mode.
            # First "^" is held as literal.  If the next byte is also
            # "^", capture triggers.  Otherwise the first "^" stays
            # as a literal character.
            if b == 0x5e:
                if self._pending_caret:
                    # Second "^" → capture (same chunk).
                    # Remove the first "^" from buffer and out.
                    if (self._terminal_input_buf
                            and self._terminal_input_buf[-1] == 0x5e):
                        self._terminal_input_buf.pop()
                    if out and out[-1] == 0x5e:
                        out.pop()
                    self._queue_capture_buf = bytearray(
                        self._terminal_input_buf)
                    self._capture_cursor_pos = len(self._capture_text())
                    self._terminal_input_buf.clear()
                    self._queue_capture_mode = True; self._capture_show_hint = True
                    self._capture_output_buf.clear()
                    # Only flag stale input if the CLI received text from
                    # previous chunks.  In same-chunk ^^, the first "^"
                    # was popped from out — CLI never got it.
                    self._capture_stale_cli_input = bool(
                        self._queue_capture_buf)
                    self._pending_caret = False
                    self._capture_prev_lines = 0
                    self._saved_msg_index = -1
                    self._capture_show_saved_hint = False
                    self._capture_display(self._capture_text())
                    i += 1
                    continue
                else:
                    # First "^" — hold it, wait for second.
                    self._pending_caret = True
                    # Fall through to normal handling (adds to buffer
                    # and out as literal "^").

            # If we were waiting for a second "^" but got something
            # else, the pending caret was a literal — clear the flag.
            elif self._pending_caret:
                self._pending_caret = False

            if in_prompt:
                out.append(b)
                i += 1
                continue

            # --- Normal handling ---
            if b == 0x0d:  # Enter
                self._user_has_typed = True
                if self._terminal_input_buf:
                    msg = self._terminal_input_buf.decode(
                        'utf-8', errors='replace').strip()
                    if msg:
                        self.queue.track_sent(msg)
                    self._terminal_input_buf.clear()
                out.append(b)
            elif b == 0x7f:  # Backspace
                if self._terminal_input_buf:
                    # Pop a full UTF-8 character (1–4 bytes), not just
                    # one byte.  The TUI deletes one character per
                    # backspace, so the buffer must stay in sync.
                    # First strip continuation bytes (10xxxxxx), then
                    # the lead byte.
                    while (self._terminal_input_buf
                           and self._terminal_input_buf[-1] & 0xC0 == 0x80):
                        self._terminal_input_buf.pop()
                    if self._terminal_input_buf:
                        self._terminal_input_buf.pop()
                out.append(b)
            elif b == 0x03:  # Ctrl+C — discard buffer
                self._terminal_input_buf.clear()
                out.append(b)
            elif 0x20 <= b < 0x7f or b >= 0x80:
                self._terminal_input_buf.append(b)
                out.append(b)
            else:
                out.append(b)
            i += 1

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
            return data

    def _output_filter_impl(self, data: bytes) -> bytes:
        """Implementation of _output_filter (separated for crash protection)."""
        # Strip OSC title-change sequences so the CLI cannot override
        # the "lps <tag>" tab name used by the monitor for navigation.
        data = self._OSC_TITLE_RE.sub(b'', data)

        # Delegate state detection to the state tracker.
        self.state.on_output(data)

        # Signal PTY handler that output was received (used by
        # send_image_message to replace fixed sleeps with event waits).
        self.pty.notify_output_received()

        # In capture mode, buffer CLI output instead of discarding it.
        # The buffered output is flushed when capture ends (Enter/Esc/Ctrl+C)
        # so the user doesn't miss anything that happened while typing.
        if self._queue_capture_mode:
            self._capture_output_buf.extend(data)
            # Don't redraw over the "Saved!" hint
            if not self._capture_show_saved_hint:
                self._capture_display(
                    self._queue_capture_buf.decode(
                        'utf-8', errors='replace'))
            return b''  # suppress CLI output (buffered for later)

        return data

    def _print_startup_banner(self) -> None:
        """Print the startup banner with help information."""
        print_banner('server', self.tag, cli_name=get_display_name(self._provider.name))
        print("  All responses will appear HERE in this window.")
        print("")
        print("  To send messages from another tab, run:")
        print(f"    leap {self.tag}")
        print("")
        print("  \u2705 Native scrolling in IntelliJ")
        print("  \u2705 Full terminal width")
        print("  \u2705 No tmux needed!")
        print("  \u2705 Type ^^ to queue from here")
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
        self._write_cli_pid_map()

        # Start background threads
        self.socket_handler.start()
        threading.Thread(target=self._auto_sender_loop, daemon=True).start()
        threading.Thread(target=self._title_keeper_loop, daemon=True).start()
        threading.Thread(target=self._stdin_watchdog_loop, daemon=True).start()

        # Wait for the socket to be bound before releasing the startup lock,
        # so concurrent `leap <tag>` invocations see the socket and connect as
        # clients instead of trying to start a second server.
        self.socket_handler.wait_ready()

        # Release the shell startup lock now that the socket is listening.
        # The lock dir was created by leap-main.sh to prevent duplicate
        # servers; the shell trap can't clean it because exec replaced the
        # shell with this Python process.
        self._release_startup_lock()

        # Handle signals
        signal.signal(signal.SIGWINCH, self._handle_resize)
        signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
        signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
        signal.signal(signal.SIGHUP, lambda s, f: sys.exit(0))

        # Reset title (CLI may have changed it)
        set_terminal_title(f"lps {self.tag}")

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
        subprocesses.  This mapping file in /tmp lets the hook discover
        the session by walking up its parent PID chain.
        """
        if not self.pty.process:
            return
        cli_pid = self.pty.process.pid
        self._cli_pid_map_file = Path(f"/tmp/leap_cli_pid_{cli_pid}.json")
        try:
            atomic_json_write(self._cli_pid_map_file, {
                'tag': self.tag,
                'signal_dir': str(SOCKET_DIR),
                'python': sys.executable,
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
        self.metadata.cleanup()
        self.pty.terminate()
        self.state.cleanup()
        self.output_capture.cleanup()
        self._cleanup_cli_pid_map()
        # Remove queue file if empty (no pending messages).
        self.queue.delete_file_if_empty()


def main() -> None:
    """Entry point for leap-server command."""
    if len(sys.argv) < 2:
        print("Usage: leap-server <tag> [--cli claude|codex|cursor-agent|gemini] [--flags...]")
        sys.exit(1)

    tag = sys.argv[1]

    if tag.startswith('-'):
        print("Error: Tag cannot start with '-'")
        print("Usage: leap-server <tag> [--cli claude|codex|cursor-agent|gemini] [--flags...]")
        sys.exit(1)

    # Extract --cli option (consumed by Leap, not passed to the CLI)
    cli_name = None
    remaining_args = sys.argv[2:]
    filtered_args: list[str] = []
    i = 0
    while i < len(remaining_args):
        if remaining_args[i] == '--cli' and i + 1 < len(remaining_args):
            cli_name = remaining_args[i + 1]
            i += 2
        elif remaining_args[i].startswith('--cli='):
            cli_name = remaining_args[i].split('=', 1)[1]
            i += 1
        else:
            filtered_args.append(remaining_args[i])
            i += 1

    flags = [arg for arg in filtered_args if arg.startswith('--')]

    server = LeapServer(tag, flags=flags, cli=cli_name)
    server.run()


if __name__ == "__main__":
    main()
