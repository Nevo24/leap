"""
Main ClaudeQ PTY Server.

Orchestrates PTY handling, socket server, and queue management.
"""

import atexit
import json
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
    QUEUE_DIR, SOCKET_DIR, HISTORY_DIR, STORAGE_DIR,
    MIN_BUSY_DURATION, OUTPUT_SETTLE_DURATION,
    POLL_INTERVAL, TITLE_RESET_INTERVAL, ensure_storage_dirs, load_settings
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

        # Validate against monitor pinned sessions (MR-pinned rows)
        self._validate_pinned_session()

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
        self._last_output_time: float = 0.0
        self._baseline_children: set[str] = set()
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

        elif msg_type == 'shutdown':
            # Use a thread so we can return the response before exiting.
            # sys.exit(0) triggers atexit cleanup (stops socket, terminates PTY, removes files).
            threading.Thread(target=lambda: sys.exit(0), daemon=True).start()
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
        self.last_sent_message = message
        self.last_send_time = time.time()

        if message.startswith('@'):
            self.pty.send_image_message(message)
        else:
            self.pty.sendline(message)

    def _prompt_load_old_queue(self) -> None:
        """Prompt user to load or discard old queued messages."""
        print(f"\n⚠️  Found {self.queue.size} unsent message{'s' if self.queue.size != 1 else ''} from previous session:\n")

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
            print("✓ Discarded old messages\n")
        else:
            # Load queue (default)
            print(f"✓ Loaded {self.queue.size} message{'s' if self.queue.size != 1 else ''}\n")

    def _show_queue_details(self) -> None:
        """Show full queue contents."""
        print("\n" + "=" * 70)
        print("Full message queue:")
        print("=" * 70)
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
        print("=" * 70)

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

    def _validate_pinned_session(self) -> None:
        """Validate current repo/branch against monitor pinned session data.

        If this tag corresponds to an MR-pinned row (has remote_project_path),
        verify that we're in the right repo, on the right branch, and not
        behind the remote. Exits with error if validation fails.
        """
        pinned_file = STORAGE_DIR / "pinned_sessions.json"
        if not pinned_file.exists():
            return

        try:
            with open(pinned_file, 'r') as f:
                pinned_sessions = json.load(f)
        except (json.JSONDecodeError, OSError):
            return

        entry = pinned_sessions.get(self.tag)
        if not entry:
            return

        pinned_project = entry.get('remote_project_path')
        if not pinned_project:
            return  # Auto-pinned row, no validation needed

        pinned_branch = entry.get('branch', '')

        # --- Repo match ---
        try:
            result = subprocess.run(
                ['git', 'config', '--get', 'remote.origin.url'],
                capture_output=True, text=True, timeout=5
            )
            remote_url = result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            remote_url = ''

        local_project = None
        if remote_url:
            # SSH: git@host:user/project.git
            m = re.match(r'git@[^:]+:(.+?)(?:\.git)?$', remote_url)
            if m:
                local_project = m.group(1)
            else:
                # HTTPS: https://host/user/project.git
                m = re.match(r'https?://[^/]+/(.+?)(?:\.git)?$', remote_url)
                if m:
                    local_project = m.group(1)

        if not local_project or local_project != pinned_project:
            local_desc = f"'{local_project}'" if local_project else 'not a matching git repo'
            print(
                f"\033[91mError: Tag '{self.tag}' is monitored for repo "
                f"'{pinned_project}', but current directory is {local_desc}.\033[0m"
            )
            sys.exit(1)

        # --- Branch match ---
        if pinned_branch and pinned_branch != 'N/A':
            try:
                result = subprocess.run(
                    ['git', 'branch', '--show-current'],
                    capture_output=True, text=True, timeout=5
                )
                local_branch = result.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                local_branch = ''

            if local_branch != pinned_branch:
                print(
                    f"\033[91mError: Tag '{self.tag}' is monitored for branch "
                    f"'{pinned_branch}', but current branch is "
                    f"'{local_branch or '(unknown)'}'.\033[0m"
                )
                sys.exit(1)

            # --- Commits synced ---
            try:
                subprocess.run(
                    ['git', 'fetch', 'origin', pinned_branch],
                    capture_output=True, timeout=15
                )
            except (subprocess.TimeoutExpired, OSError):
                pass  # Network issues shouldn't block startup

            try:
                result = subprocess.run(
                    ['git', 'merge-base', '--is-ancestor',
                     f'origin/{pinned_branch}', 'HEAD'],
                    capture_output=True, timeout=5
                )
                if result.returncode != 0:
                    print(
                        f"\033[91m✖ Tag '{self.tag}' is tracked by ClaudeQ Monitor "
                        f"for branch '{pinned_branch}', but the local repo is "
                        f"behind remote. Pull or rebase before starting.\033[0m"
                    )
                    sys.exit(1)
            except (subprocess.TimeoutExpired, OSError):
                pass  # Can't verify — allow startup

            # --- Yellow warnings for ahead / dirty state (non-fatal) ---
            ahead_count = 0
            has_uncommitted = False
            try:
                result = subprocess.run(
                    ['git', 'rev-list', f'origin/{pinned_branch}..HEAD', '--count'],
                    capture_output=True, text=True, timeout=5
                )
                ahead_count = int(result.stdout.strip()) if result.returncode == 0 else 0
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass

            try:
                result = subprocess.run(
                    ['git', 'status', '--porcelain'],
                    capture_output=True, text=True, timeout=5
                )
                has_uncommitted = result.returncode == 0 and bool(result.stdout.strip())
            except (subprocess.TimeoutExpired, OSError):
                pass

            if ahead_count > 0 and has_uncommitted:
                suffix = (
                    f"is {ahead_count} commit{'s' if ahead_count != 1 else ''} "
                    f"ahead of remote with uncommitted changes"
                )
            elif ahead_count > 0:
                suffix = (
                    f"is {ahead_count} commit{'s' if ahead_count != 1 else ''} "
                    f"ahead of remote"
                )
            elif has_uncommitted:
                suffix = "has uncommitted changes"
            else:
                suffix = ''

            if suffix:
                print(
                    f"\033[93m⚠ Tag '{self.tag}' is tracked by ClaudeQ Monitor "
                    f"for branch '{pinned_branch}', but the local repo {suffix}. "
                    f"Proceeding anyway.\033[0m"
                )

    def _is_busy(self) -> bool:
        """
        Check if Claude is busy processing.

        Uses three signals:
        1. MIN_BUSY_DURATION — too soon after sending a message.
        2. Output settle — PTY is still producing output (streaming response).
        3. Child process baseline — new children beyond the baseline indicate
           tool execution (e.g. bash).  Persistent children like caffeinate
           or MCP servers are absorbed into the baseline when idle.

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

        # Check if PTY is still producing output (Claude streaming).
        # Only counts as busy if we sent a message recently — idle prompt
        # redraws and escape sequences should not trigger busy state.
        if self._last_output_time and self.last_send_time:
            time_since_output = time.time() - self._last_output_time
            time_since_send = time.time() - self.last_send_time
            if time_since_output < OUTPUT_SETTLE_DURATION and time_since_send < 300:
                return True

        # Check for NEW child processes (tools being run)
        if self.pty.pid:
            try:
                result = subprocess.run(
                    ['pgrep', '-P', str(self.pty.pid)],
                    capture_output=True
                )
                children = set(c for c in result.stdout.decode().strip().split() if c)
                new_children = children - self._baseline_children
                if new_children:
                    # New children found.  If output has been silent for a
                    # long time, these are almost certainly persistent
                    # processes (caffeinate, MCP servers), not tools.
                    silence = (time.time() - self._last_output_time) if self._last_output_time else float('inf')
                    if silence <= 10.0:
                        return True
                    # Silent too long — absorb as persistent and fall through
                # Update baseline to absorb any persistent processes
                # spawned during the last processing cycle.
                self._baseline_children = children
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
            except Exception:
                # Re-queue on failure
                self.queue.requeue(message)

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

        # Track when PTY last produced output (used by _is_busy).
        self._last_output_time = time.time()

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
            self.pty.interact(output_filter=self._output_filter)
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
