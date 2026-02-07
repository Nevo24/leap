"""
Main ClaudeQ PTY Server.

Orchestrates PTY handling, socket server, and queue management.
"""

import atexit
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from claudeq.utils.constants import (
    QUEUE_DIR, SOCKET_DIR, MIN_BUSY_DURATION, POLL_INTERVAL, TITLE_RESET_INTERVAL,
    ensure_storage_dirs
)
from claudeq.utils.terminal import set_terminal_title, print_banner
from claudeq.server.pty_handler import PTYHandler
from claudeq.server.socket_handler import SocketHandler
from claudeq.server.queue_manager import QueueManager
from claudeq.server.metadata import SessionMetadata


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
        self.pty = PTYHandler(flags)
        self.queue = QueueManager(self.queue_file)
        self.metadata = SessionMetadata(tag, SOCKET_DIR)
        self.socket_handler = SocketHandler(self.socket_path, self._handle_message)

        # State tracking
        self.last_sent_message: Optional[str] = None
        self.last_send_time: Optional[float] = None
        self.pending_notifications: list[str] = []
        self._notification_lock = threading.Lock()

        # Load existing queue and save metadata
        self.queue.load()
        self.metadata.save()

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

        elif msg_type == 'status':
            return {
                'queue_size': self.queue.size,
                'queue_contents': self.queue.get_contents(),
                'recently_sent': self.queue.get_recently_sent(),
                'ready': not self._is_busy(),
                'claude_running': self.pty.is_alive()
            }

        elif msg_type == 'force_send':
            message = self.queue.pop()
            if message:
                self._send_to_claude(message)
                self.queue.track_sent(message)
                with self._notification_lock:
                    remaining = self.queue.size
                    self.pending_notifications.append(
                        f"🔥 Force-sent from queue ({remaining} remaining)"
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

        return {'status': 'error', 'message': f"Unknown message type: {msg_type}"}

    def _send_to_claude(self, message: str) -> None:
        """
        Send a message to Claude CLI.

        Args:
            message: Message to send.
        """
        self.last_sent_message = message
        self.last_send_time = time.time()
        self.pty.send(message)

        # Image attachments: '@' triggers Claude CLI's file-mention mode.
        # First \r confirms the file selection, second \r submits the message.
        if message.startswith('@'):
            time.sleep(0.5)
            self.pty.send('\r')

        self.pty.send('\r')

    def _is_busy(self) -> bool:
        """
        Check if Claude is busy processing.

        Returns:
            True if Claude is busy, False otherwise.
        """
        if not self.pty.is_alive():
            return False

        # Check if we recently sent a message
        if self.last_send_time:
            time_since_send = time.time() - self.last_send_time
            if time_since_send < MIN_BUSY_DURATION:
                return True

        # Check for child processes (tools being run)
        if self.pty.pid:
            try:
                result = subprocess.run(
                    ['pgrep', '-P', str(self.pty.pid)],
                    capture_output=True
                )
                children = [c for c in result.stdout.decode().strip().split() if c]
                return len(children) > 0
            except (subprocess.SubprocessError, OSError):
                pass

        return False

    def _auto_sender_loop(self) -> None:
        """Background thread to auto-send queued messages."""
        while self.running:
            time.sleep(POLL_INTERVAL)

            if self.queue.is_empty or self._is_busy():
                continue

            message = self.queue.pop()
            if not message:
                continue

            try:
                self._send_to_claude(message)
                self.queue.track_sent(message)

                with self._notification_lock:
                    remaining = self.queue.size
                    self.pending_notifications.append(
                        f"🤖 Auto-sent from queue ({remaining} remaining)"
                    )
            except Exception:
                # Re-queue on failure
                self.queue.requeue(message)

            # Let Claude start processing
            time.sleep(1)

    def _title_keeper_loop(self) -> None:
        """Background thread to maintain terminal title."""
        while self.running:
            try:
                set_terminal_title(f"cq-server {self.tag}")
            except Exception:
                pass
            time.sleep(TITLE_RESET_INTERVAL)

    def _handle_resize(self, sig: int, frame: Any) -> None:
        """Handle terminal resize signal."""
        try:
            cols, rows = shutil.get_terminal_size(fallback=(80, 24))
            self.pty.resize(rows, cols)
        except Exception:
            pass

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
        print("  ✅ Native scrolling in IntelliJ")
        print("  ✅ Full terminal width")
        print("  ✅ No tmux needed!")
        print("")
        print("  Ctrl+C to exit")
        print("=" * 70)
        print()

        if not self.queue.is_empty:
            print(f"📝 Queue has {self.queue.size} messages\n")

    def run(self) -> None:
        """Run the server main loop."""
        set_terminal_title(f"cq-server {self.tag}")
        self._print_startup_banner()
        self.pty.spawn()

        # Start background threads
        self.socket_handler.start()
        threading.Thread(target=self._auto_sender_loop, daemon=True).start()
        threading.Thread(target=self._title_keeper_loop, daemon=True).start()

        # Handle signals
        signal.signal(signal.SIGWINCH, self._handle_resize)
        signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
        signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
        signal.signal(signal.SIGHUP, lambda s, f: sys.exit(0))

        # Reset title (Claude CLI may have changed it)
        set_terminal_title(f"cq-server {self.tag}")

        try:
            self.pty.interact(output_filter=self._output_filter)
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception as e:
            print(f"\nError in interact: {e}", file=sys.stderr)
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        """Clean up all resources."""
        self.running = False
        self.socket_handler.stop()
        self.socket_handler.cleanup()
        self.metadata.cleanup()
        self.pty.terminate()
        # Remove queue file if empty (no pending messages)
        if self.queue.is_empty and self.queue_file.exists():
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
