"""
Main ClaudeQ PTY Server.

Orchestrates PTY handling, socket server, and queue management.
"""

import atexit
import os
import re
import shutil
import signal
import sys
import threading
import time
from typing import Any, Optional

from claudeq.utils.constants import (
    QUEUE_DIR, SOCKET_DIR, HISTORY_DIR, STORAGE_DIR,
    POLL_INTERVAL, TITLE_RESET_INTERVAL,
    ensure_storage_dirs, load_settings, save_settings,
)
from claudeq.utils.terminal import set_terminal_title, print_banner
from claudeq.server.pty_handler import PTYHandler
from claudeq.server.socket_handler import SocketHandler
from claudeq.server.queue_manager import QueueManager
from claudeq.server.metadata import SessionMetadata
from claudeq.server.state_tracker import ClaudeStateTracker
from claudeq.slack.output_capture import OutputCapture
from claudeq.server.validation import validate_pinned_session


class ClaudeQServer:
    """
    ClaudeQ PTY Server.

    Manages a Claude CLI session with message queueing and socket-based
    client communication.
    """

    # Matches OSC escape sequences that set the terminal title (params 0, 1, 2),
    # terminated by BEL (\x07) or ST (\x1b\\).  Stripped from PTY output so
    # Claude CLI cannot override the "cq-server <tag>" tab name.
    _OSC_TITLE_RE: re.Pattern[bytes] = re.compile(
        rb'\x1b\][012];[^\x07\x1b]*(?:\x07|\x1b\\)'
    )

    def __init__(self, tag: str, flags: Optional[list[str]] = None):
        """
        Initialize ClaudeQ server.

        Args:
            tag: Session tag name.
            flags: Optional flags to pass to Claude CLI.
        """
        self.tag = tag
        self.running = True

        # Ensure storage directories exist
        ensure_storage_dirs()

        # Validate against monitor pinned sessions (MR-pinned rows).
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
        self.pty = PTYHandler(flags, tag=tag, signal_dir=SOCKET_DIR)
        self.queue = QueueManager(self.queue_file)
        self.metadata = SessionMetadata(tag, SOCKET_DIR)
        self.socket_handler = SocketHandler(self.socket_path, self._handle_message)

        # State tracking
        self.state = ClaudeStateTracker(
            signal_file=SOCKET_DIR / f"{tag}.signal",
            auto_send_mode=load_settings().get('auto_send_mode', 'pause'),
        )
        self.output_capture = OutputCapture(tag)
        self.pending_notifications: list[str] = []
        self._notification_lock = threading.Lock()

        # Clean up old history files
        self._cleanup_old_history_files()

        # Load existing queue and save metadata
        self.queue.load()
        self.metadata.save()

        # Prompt user about old queue messages
        if not self.queue.is_empty:
            self._prompt_load_old_queue()

        # Register cleanup
        atexit.register(self.cleanup)

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

        elif msg_type == 'direct':
            self._send_to_claude(message)
            return {'status': 'sent'}

        elif msg_type == 'select_option':
            # Select a numbered option in a permission/question dialog.
            try:
                option_num = int(message)
            except (ValueError, TypeError):
                return {'status': 'error', 'error': 'invalid option number'}
            if option_num < 1:
                return {'status': 'error', 'error': 'option must be >= 1'}
            # Reject "Type something." — selecting it without text causes
            # "User declined".  Slack users should type free text instead.
            prompt = self.state.get_prompt_output()
            for line in prompt.split('\n'):
                m = re.match(r'\s*(\d+)\.\s+Type something', line)
                if m and int(m.group(1)) == option_num:
                    return {
                        'status': 'error',
                        'error': 'type your answer as text instead',
                    }
            self.state.on_send()
            self.pty.sendline(str(option_num))
            return {'status': 'sent'}

        elif msg_type == 'custom_answer':
            # Select "Type something." in a question dialog, then enter
            # the user's free-form text.
            prompt = self.state.get_prompt_output()
            type_option = None
            for line in prompt.split('\n'):
                m = re.match(r'\s*(\d+)\.\s+Type something', line)
                if m:
                    type_option = m.group(1)
                    break
            if not type_option:
                return {
                    'status': 'error',
                    'error': 'no "Type something" option found',
                }
            self.state.on_send()
            # Step 1: Send digit to navigate to "Type something."
            self.pty.send(type_option)
            time.sleep(0.5)
            # Step 2: Start typing directly — Ink switches to text
            # input mode when you type on the "Type something." option
            # (no Enter needed to "open" it).  Type char-by-char for
            # Ink raw-mode compatibility.
            for ch in message:
                self.pty.send(ch)
                time.sleep(0.02)
            time.sleep(0.1)
            # Step 3: Submit the text.
            self.pty.send('\r')
            return {'status': 'sent'}

        elif msg_type == 'status':
            state = self.state.get_state(self.pty.is_alive())
            return {
                'queue_size': self.queue.size,
                'queue_contents': self.queue.get_contents(),
                'recently_sent': self.queue.get_recently_sent(),
                'ready': self.state.is_ready(self.pty.is_alive()),
                'claude_state': state,
                'auto_send_mode': self.state.auto_send_mode,
                'claude_running': self.pty.is_alive(),
                'slack_enabled': self.output_capture.is_enabled(),
            }

        elif msg_type == 'set_slack':
            enabled = msg.get('enabled', False)
            self.output_capture.set_enabled(enabled)
            return {'status': 'ok', 'slack_enabled': enabled}

        elif msg_type == 'force_send':
            message = self.queue.pop()
            if message:
                self._send_to_claude(message)
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

        elif msg_type == 'set_auto_send_mode':
            mode = msg.get('mode', '')
            if mode not in ('pause', 'always'):
                return {'status': 'error', 'message': f"Invalid mode: {mode}. Use 'pause' or 'always'."}
            self.state.auto_send_mode = mode
            settings = load_settings()
            settings['auto_send_mode'] = mode
            save_settings(settings)
            return {'status': 'ok', 'auto_send_mode': mode}

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

    def _send_to_claude(self, message: str) -> None:
        """
        Send a message to Claude CLI.

        Uses atomic sendline() for regular messages (message + CR in one
        locked write sequence) and event-driven send_image_message() for
        @-prefixed image attachments.

        Args:
            message: Message to send.
        """
        self.state.on_send()

        if message.startswith('@'):
            self.pty.send_image_message(message)
        else:
            self.pty.sendline(message)

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

    def _auto_sender_loop(self) -> None:
        """Background thread to auto-send queued messages."""
        prev_state = 'idle'
        # Delayed write for prompt states: wait for TUI to finish rendering
        # before capturing prompt output for Slack.
        prompt_write_due: float = 0.0
        prompt_prev_state: str = ''
        prompt_queue_has_next: bool = False
        while self.running:
            time.sleep(POLL_INTERVAL)

            current_state = self.state.get_state(self.pty.is_alive())

            # Detect state transitions for Slack output capture
            if current_state != prev_state:
                # Cancel any pending prompt write on state change
                prompt_write_due = 0.0
                queue_has_next = (
                    not self.queue.is_empty
                    and current_state == 'idle'
                    and self.state.auto_send_mode in ('pause', 'always')
                )
                if current_state in ('needs_permission', 'has_question'):
                    # Delay writing: let PTY output accumulate so the
                    # full permission dialog / question is captured.
                    prompt_write_due = time.time() + 0.2
                    prompt_prev_state = prev_state
                    prompt_queue_has_next = queue_has_next
                else:
                    self.output_capture.on_state_change(
                        current_state, prev_state, queue_has_next,
                    )
                prev_state = current_state

            # Delayed prompt output write
            if prompt_write_due and time.time() >= prompt_write_due:
                cs = self.state.current_state
                if cs in ('needs_permission', 'has_question'):
                    prompt_output = self.state.get_prompt_output()
                    self.output_capture.on_state_change(
                        cs, prompt_prev_state,
                        prompt_queue_has_next, prompt_output,
                    )
                prompt_write_due = 0.0

            if self.queue.is_empty or not self.state.is_ready(self.pty.is_alive()):
                continue

            message = self.queue.pop()
            if not message:
                continue

            try:
                self._send_to_claude(message)
                self.queue.track_sent(message)
            except Exception:
                self.queue.requeue(message)

    def _title_keeper_loop(self) -> None:
        """Background thread to maintain terminal title."""
        while self.running:
            try:
                set_terminal_title(f"cq-server {self.tag}")
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

    def _input_filter(self, data: bytes) -> bytes:
        """Track user keyboard input for state detection.

        Args:
            data: Raw input bytes from keyboard.

        Returns:
            Input bytes unchanged (pass-through).
        """
        self.state.on_input(data)
        return data

    def _output_filter(self, data: bytes) -> bytes:
        """
        Filter PTY output to inject notifications and strip title escapes.

        Args:
            data: Raw output bytes.

        Returns:
            Filtered output bytes with title sequences removed and
            notifications injected.
        """
        # Strip OSC title-change sequences so Claude CLI cannot override
        # the "cq-server <tag>" tab name used by the monitor for navigation.
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
        print_banner('server', self.tag)
        print("  All responses will appear HERE in this window.")
        print("")
        print("  To send messages from another tab, run:")
        print(f"    cq {self.tag}")
        print("")
        print("  \u2705 Native scrolling in IntelliJ")
        print("  \u2705 Full terminal width")
        print("  \u2705 No tmux needed!")
        print("")
        print("  Ctrl+C to exit")
        print("=" * 80)
        print()

        if not self.queue.is_empty:
            print(f"\U0001f4dd Queue has {self.queue.size} messages\n")

    def run(self) -> None:
        """Run the server main loop."""
        set_terminal_title(f"cq-server {self.tag}")
        self._print_startup_banner()
        self.pty.spawn()

        # Start background threads
        self.socket_handler.start()
        threading.Thread(target=self._auto_sender_loop, daemon=True).start()
        threading.Thread(target=self._title_keeper_loop, daemon=True).start()
        threading.Thread(target=self._stdin_watchdog_loop, daemon=True).start()

        # Wait for the socket to be bound before releasing the startup lock,
        # so concurrent `cq <tag>` invocations see the socket and connect as
        # clients instead of trying to start a second server.
        self.socket_handler.wait_ready()

        # Release the shell startup lock now that the socket is listening.
        # The lock dir was created by claudeq-main.sh to prevent duplicate
        # servers; the shell trap can't clean it because exec replaced the
        # shell with this Python process.
        self._release_startup_lock()

        # Handle signals
        signal.signal(signal.SIGWINCH, self._handle_resize)
        signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
        signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
        signal.signal(signal.SIGHUP, lambda s, f: sys.exit(0))

        # Reset title (Claude CLI may have changed it)
        set_terminal_title(f"cq-server {self.tag}")

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

        The lock dir is created by claudeq-main.sh (mkdir) to prevent
        duplicate servers.  Because the shell uses ``exec`` to hand off
        to Python, the shell trap never fires, so we clean it up here.
        """
        lock_dir = SOCKET_DIR / f"{self.tag}.server.lock"
        try:
            lock_dir.rmdir()
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
        # Remove queue file if empty (no pending messages).
        # Hold the queue lock so no message can be added between the
        # emptiness check and the unlink.
        with self.queue._lock:
            if not self.queue.queue and self.queue_file.exists():
                try:
                    self.queue_file.unlink()
                except OSError:
                    pass


def main() -> None:
    """Entry point for claudeq-server command."""
    if len(sys.argv) < 2:
        print("Usage: claudeq-server <tag> [--flags...]")
        sys.exit(1)

    tag = sys.argv[1]

    if tag.startswith('-'):
        print("Error: Tag cannot start with '-'")
        print("Usage: claudeq-server <tag> [--flags...]")
        sys.exit(1)

    flags = [arg for arg in sys.argv[2:] if arg.startswith('--')]

    server = ClaudeQServer(tag, flags=flags)
    server.run()


if __name__ == "__main__":
    main()
