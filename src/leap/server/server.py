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
    QUEUE_DIR, SOCKET_DIR, HISTORY_DIR, IMAGES_DIR, STORAGE_DIR,
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
        self.pending_notifications: list[str] = []
        self._notification_lock = threading.Lock()
        self._terminal_input_buf: bytearray = bytearray()
        self._partial_escape: bool = False  # True when last data ended mid-escape
        self._user_has_typed: bool = False  # True after first Enter in the terminal
        # Queue-from-server: "^" prefix capture mode.
        # When "^" is the first char on a line we enter capture mode
        # and swallow all subsequent input until Enter → queue.
        self._queue_capture_mode: bool = False
        self._queue_capture_buf: bytearray = bytearray()

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
                with self._notification_lock:
                    remaining = self.queue.size
                    self.pending_notifications.append(
                        f"\U0001f525 Force-sent from queue ({remaining} remaining)"
                    )
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
        self.state.on_send()

        if self._provider.is_image_message(message) or self._has_image_ref(message):
            self.pty.send_image_message(message)
        else:
            self.pty.sendline(message)

    def _has_image_ref(self, message: str) -> bool:
        """Check if the message contains any @path refs to .storage/images/.

        Catches image references that aren't at the start of the message
        (which ``is_image_message`` would miss). Checked for all providers
        so that image messages always use the fixed-sleep send protocol.
        """
        prefix = self._provider.image_prefix
        images_dir = str(IMAGES_DIR)
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
        """Delete images in .storage/images/ not referenced anywhere.

        Called once on server startup. Scans queue files (``id|message``
        format) and the presets JSON for image paths, then removes any
        image file not found in either.
        """
        if not IMAGES_DIR.is_dir():
            return

        images_dir_str = str(IMAGES_DIR)

        def _collect_refs(text: str) -> None:
            """Find all @<IMAGES_DIR>/... references in *text*."""
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
        for entry in IMAGES_DIR.iterdir():
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
            self.pty.resize(rows, cols)
        except Exception:
            pass

    def _capture_display(self, text: Optional[str] = None) -> None:
        """Show queue-capture buffer on the TUI's input line.

        Writes ``\\r`` + erase-line + the text in a single ``os.write``
        call.  Since we are not feeding input to the Ink TUI while in
        capture mode, it has no reason to repaint, so the text persists
        on screen until the next keystroke updates it.
        """
        try:
            if text is None:
                # Clear the capture line
                os.write(sys.stdout.fileno(), b'\r\x1b[K')
            else:
                payload = f"\r\x1b[K\x1b[33m[Q] {text}\x1b[0m".encode()
                os.write(sys.stdout.fileno(), payload)
        except OSError:
            pass

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
        # Note: on_input() is called AFTER the byte loop (see end of
        # method) with only the bytes that reach the CLI.  This prevents
        # capture-mode keystrokes from corrupting state tracker timing
        # (e.g. false idle→running on Enter for Ratatui CLIs, or false
        # resume from waiting states due to _last_input_time updates).

        in_prompt = self.state.current_state in PROMPT_STATES

        out = bytearray()
        i = 0

        # Check if the very first byte is "^" for queue capture mode
        # BEFORE escape handling and the in_prompt bypass, so neither
        # a stale _partial_escape nor in_prompt can swallow the caret.
        if (not self._queue_capture_mode
                and i < len(data)
                and data[i] == 0x5e
                and (not self._terminal_input_buf
                     or not self._user_has_typed)):
            self._partial_escape = False
            self._terminal_input_buf.clear()
            self._queue_capture_mode = True
            self._queue_capture_buf.clear()
            self._capture_display("")
            i += 1  # consume the "^"

        # If a previous call ended mid-escape, skip continuation bytes.
        if self._partial_escape:
            self._partial_escape = False
            # Consume remaining escape bytes: parameter/intermediate (0x20-0x3f)
            # then one final byte (0x40-0x7e), pass them all through.
            while i < len(data) and 0x20 <= data[i] <= 0x3f:
                out.append(data[i])
                i += 1
            if i < len(data):
                out.append(data[i])  # final byte
                i += 1

        while i < len(data):
            b = data[i]

            # --- Escape sequences: always pass through ---
            if b == 0x1b:
                esc_start = i
                i += 1
                if i >= len(data):
                    # ESC at end of chunk — mark partial, pass through
                    self._partial_escape = True
                    out.append(b)
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
                        self._partial_escape = True
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
                else:
                    i += 1
                out.extend(data[esc_start:i])
                continue

            # --- Queue-capture mode: swallow input, queue on Enter ---
            # The terminal title bar shows "[Q] <text>" as live feedback
            # since we can't echo into the TUI content area.
            # Must be checked before in_prompt so capture works in any state.
            if self._queue_capture_mode:
                if b == 0x0d:  # Enter — queue the message
                    self._user_has_typed = True
                    self._capture_display()  # clear the line
                    msg = self._queue_capture_buf.decode(
                        'utf-8', errors='replace').strip()
                    if msg:
                        size = self.queue.add(msg)
                        with self._notification_lock:
                            self.pending_notifications.append(
                                f"[queued #{size}] {msg[:60]}"
                                + ("\u2026" if len(msg) > 60 else ""))
                    self._queue_capture_buf.clear()
                    self._queue_capture_mode = False
                    self._terminal_input_buf.clear()
                elif b == 0x7f:  # Backspace
                    if self._queue_capture_buf:
                        self._queue_capture_buf.pop()
                        self._capture_display(
                            self._queue_capture_buf.decode(
                                'utf-8', errors='replace'))
                    else:
                        # Backspaced past all content — exit capture
                        self._capture_display()  # clear
                        self._queue_capture_mode = False
                elif b == 0x03:  # Ctrl+C — cancel capture
                    self._capture_display()  # clear
                    self._queue_capture_buf.clear()
                    self._queue_capture_mode = False
                    self._terminal_input_buf.clear()
                elif 0x20 <= b < 0x7f or b >= 0x80:
                    self._queue_capture_buf.append(b)
                    self._capture_display(
                        self._queue_capture_buf.decode(
                            'utf-8', errors='replace'))
                # Swallowed — nothing goes to out.
                i += 1
                continue

            # "^" at start of line → enter queue capture mode.
            # Checked before in_prompt so it works in any CLI state.
            if (b == 0x5e
                    and (not self._terminal_input_buf
                         or not self._user_has_typed)):
                self._terminal_input_buf.clear()
                self._queue_capture_mode = True
                self._queue_capture_buf.clear()
                self._capture_display("")
                i += 1
                continue

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
        # so they don't affect _last_input_time, _last_escape_time, or
        # trigger enter_triggers_running.
        if out:
            self.state.on_input(bytes(out))

        return bytes(out)

    def _output_filter(self, data: bytes) -> bytes:
        """
        Filter PTY output to inject notifications and strip title escapes.

        Args:
            data: Raw output bytes.

        Returns:
            Filtered output bytes with title sequences removed and
            notifications injected.
        """
        # Strip OSC title-change sequences so the CLI cannot override
        # the "lps <tag>" tab name used by the monitor for navigation.
        data = self._OSC_TITLE_RE.sub(b'', data)

        # Delegate state detection to the state tracker
        self.state.on_output(data)

        # Signal PTY handler that output was received (used by
        # send_image_message to replace fixed sleeps with event waits).
        self.pty.notify_output_received()

        with self._notification_lock:
            if self.pending_notifications and data.endswith(b'\n'):
                notifications = '   '.join(self.pending_notifications)
                self.pending_notifications.clear()
                return data + f"\033[33m{notifications}\033[0m\n".encode()
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
        print("  \u2705 Type ^msg to queue from here")
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
