"""
Main ClaudeQ PTY Client.

Interactive client for sending messages to a ClaudeQ server.
"""

import atexit
import fcntl
import json
import os
import signal
import sys
import threading
import time
from typing import Optional

from claudeq.utils.constants import QUEUE_DIR, SOCKET_DIR, HISTORY_DIR, SETTINGS_FILE, ensure_storage_dirs
from claudeq.utils.terminal import set_terminal_title, print_banner
from claudeq.client.socket_client import SocketClient
from claudeq.client.image_handler import (
    check_clipboard_has_image,
    save_clipboard_image,
    is_image_file
)
from claudeq.client.input_handler import InputHandler



class ClaudeQClient:
    """
    ClaudeQ PTY Client.

    Interactive client for sending messages to a ClaudeQ server session.
    """

    def __init__(self, tag: str):
        """
        Initialize ClaudeQ client.

        Args:
            tag: Session tag name to connect to.
        """
        self.tag = tag
        self.running = True
        self.pending_image_path: Optional[str] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self.temp_image_files: list[str] = []  # Track temp files for cleanup

        # Ensure storage directories exist
        ensure_storage_dirs()

        # Initialize paths
        self.socket_path = SOCKET_DIR / f"{tag}.sock"
        self.queue_file = QUEUE_DIR / f"{tag}.queue"
        self.history_file = HISTORY_DIR / f"{tag}.history"
        self.lock_file = SOCKET_DIR / f"{tag}.client.lock"

        # Load settings from file (persistent across all clients)
        self.show_auto_sent_notifications = self._load_settings().get('show_auto_sent_notifications', True)

        # Acquire exclusive lock
        self._acquire_lock()

        # Error tracking for rate limiting
        self._last_socket_error_time = 0
        self._socket_error_cooldown = 5.0  # Only show error once per 5 seconds

        # Initialize components
        self.socket = SocketClient(self.socket_path, error_callback=self._should_print_socket_error)
        self.input_handler = InputHandler(self.history_file, self._get_prompt)

        # Register cleanup
        atexit.register(self._cleanup_lock)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGHUP, self._signal_handler)  # Terminal close (Cmd+W)

    def _acquire_lock(self) -> None:
        """Acquire exclusive client lock to prevent multiple clients."""
        try:
            self.lock_fd = open(self.lock_file, 'w')
            fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_fd.write(str(os.getpid()))
            self.lock_fd.flush()
            os.fsync(self.lock_fd.fileno())
        except BlockingIOError:
            pid_info = ""
            try:
                with open(self.lock_file, 'r') as f:
                    pid = f.read().strip()
                    pid_info = f" (PID: {pid})"
            except OSError:
                pass

            print(f"\n❌ Error: Another client is already connected to server '{self.tag}'{pid_info}")
            print("Only one interactive client per server is allowed.\n")
            print(f"If you're sure no other client is running, remove:")
            print(f"  {self.lock_file}\n")
            sys.exit(1)
        except OSError as e:
            print(f"Error creating lock file: {e}")
            sys.exit(1)

    def _cleanup_lock(self) -> None:
        """Release and remove client lock file."""
        # Try to unlock and close file descriptor
        if hasattr(self, 'lock_fd') and self.lock_fd:
            try:
                fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass  # Unlock might fail if already closed
            try:
                self.lock_fd.close()
            except (OSError, ValueError):
                pass  # Close might fail if already closed

        # Always try to delete lock file, even if unlock/close failed
        try:
            if self.lock_file.exists():
                self.lock_file.unlink()
        except OSError:
            pass  # File might be locked by another process

        # Clean up temp image files
        self._cleanup_temp_images()

    def _cleanup_temp_images(self) -> None:
        """Clean up temporary image files."""
        if not hasattr(self, 'temp_image_files'):
            return

        for image_path in self.temp_image_files:
            try:
                if os.path.exists(image_path):
                    os.unlink(image_path)
            except OSError:
                pass

    def _load_settings(self) -> dict:
        """
        Load settings from JSON file.

        Returns:
            Dictionary of settings, or empty dict if file doesn't exist.
        """
        try:
            if SETTINGS_FILE.exists():
                with open(SETTINGS_FILE, 'r') as f:
                    return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_settings(self, settings: dict) -> None:
        """
        Save settings to JSON file.

        Args:
            settings: Dictionary of settings to save.
        """
        try:
            # Ensure directory exists
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(settings, f, indent=2)
        except OSError as e:
            print(f"⚠️  Warning: Could not save settings: {e}")

    def _signal_handler(self, signum: int, frame: object) -> None:
        """Handle termination signals."""
        print("\n\nExiting...")
        self._cleanup_lock()
        sys.exit(0)

    def _get_prompt(self) -> str:
        """Generate current prompt text based on state."""
        if self.pending_image_path:
            return "[📸] You: "
        return "You: "

    def _queue_monitor_loop(self) -> None:
        """Background thread to monitor queue changes."""
        last_recently_sent: list[str] = []
        last_queue_contents: list[str] = []
        poll_count = 0

        while self.running:
            time.sleep(0.3)

            # Use silent mode for background polling (errors are rate-limited via callback)
            response = self.socket.get_status(silent=False)
            if not response:
                continue

            poll_count += 1
            new_size = response.get('queue_size', 0)
            recently_sent = response.get('recently_sent', [])
            queue_contents = response.get('queue_contents', [])

            # Detect externally added messages (e.g. from GitLab via monitor)
            if poll_count > 1 and self.show_auto_sent_notifications:
                last_ids = {self._extract_queue_id(m) for m in last_queue_contents}
                for entry in queue_contents:
                    entry_id = self._extract_queue_id(entry)
                    if entry_id and entry_id not in last_ids:
                        # Extract message body after the ID tag
                        msg_body = entry.split('> ', 1)[1] if '> ' in entry else entry
                        if msg_body.startswith('[gitlab] '):
                            preview = msg_body[9:69] + '...' if len(msg_body) > 78 else msg_body[9:]
                            print(f"\n📩 GitLab review queued: {preview}", flush=True)
                            print(f"   ({new_size} in queue)", flush=True)

            # Detect newly sent messages
            new_sent_messages: list[str] = []
            if recently_sent:
                if last_recently_sent:
                    if len(recently_sent) >= len(last_recently_sent):
                        if recently_sent[:len(last_recently_sent)] == last_recently_sent:
                            new_sent_messages = recently_sent[len(last_recently_sent):]
                elif poll_count > 1:
                    new_sent_messages = recently_sent[:]

            # Print notifications for new sent messages (if enabled)
            if new_sent_messages and self.show_auto_sent_notifications:
                for msg in new_sent_messages:
                    msg_preview = msg[:60] + '...' if len(msg) > 60 else msg
                    print(f"\n🤖 Server auto-sent: {msg_preview}", flush=True)

                if new_size > 0:
                    print(f"   ({new_size} remaining in queue)", flush=True)
                else:
                    print("   (queue empty)", flush=True)

            last_recently_sent = list(recently_sent)
            last_queue_contents = list(queue_contents)

    def _should_print_socket_error(self) -> bool:
        """Check if enough time has passed to print another socket error."""
        current_time = time.time()
        if current_time - self._last_socket_error_time >= self._socket_error_cooldown:
            self._last_socket_error_time = current_time
            return True
        return False

    @staticmethod
    def _extract_queue_id(entry: str) -> Optional[str]:
        """Extract the message ID from a queue entry like '<a1b2c3> message'."""
        if entry.startswith('<') and '>' in entry:
            return entry[1:entry.index('>')]
        return None

    def _process_image_in_line(self, line: str) -> str:
        """
        Check if line contains an image file path and extract it.

        Args:
            line: User input line.

        Returns:
            Line with image path removed.
        """
        words = line.split()
        for word in words:
            if is_image_file(word):
                self.pending_image_path = word
                line = line.replace(word, '').strip()
                print(f"📎 Image attached: {os.path.basename(word)}")
                break
        return line

    def _build_message_with_image(self, message: str) -> str:
        """
        Build message with pending image attachment.

        Args:
            message: User message.

        Returns:
            Message with image path prefix.
        """
        if self.pending_image_path:
            if message:
                full_message = f"@{self.pending_image_path} {message}"
            else:
                full_message = f"@{self.pending_image_path} "
            self.pending_image_path = None
            return full_message
        return message

    def _handle_imagepaste(self, msg: Optional[str], is_direct: bool = False) -> bool:
        """
        Handle !ip or !imagepaste command.

        Args:
            msg: Optional message to send with image.
            is_direct: Whether to send directly.

        Returns:
            True if image was handled.
        """
        if check_clipboard_has_image():
            image_path = save_clipboard_image()
            if image_path:
                self.pending_image_path = image_path
                # Track temp file for cleanup if it's in /tmp
                if image_path.startswith('/tmp/'):
                    self.temp_image_files.append(image_path)
                if msg:
                    if is_direct:
                        self._send_direct(msg)
                    else:
                        self._queue_add(msg)
                else:
                    print("🖼️  Image attached! Type message and press Enter "
                          "(or just Enter to queue image alone)")
                return True
            else:
                print("✗ Failed to save image from clipboard\n")
        else:
            print("✗ No image in clipboard\n")
        return False

    def _queue_add(self, message: str) -> None:
        """Add message to queue with any pending image."""
        full_message = self._build_message_with_image(message)
        has_image = full_message != message

        response = self.socket.queue_message(full_message)
        if response:
            queue_size = response.get('queue_size', 0)
            if has_image:
                print(f"📝 Queued with image ({queue_size} total)\n")
            else:
                print(f"📝 Queued ({queue_size} total)\n")
        else:
            print("✗ Failed to queue message\n")

    def _send_direct(self, message: str) -> None:
        """Send message directly to Claude."""
        full_message = self._build_message_with_image(message)

        response = self.socket.send_direct(full_message)
        if response and response.get('status') == 'sent':
            print(f"✓ Sent to Claude '{self.tag}'")
            print("   See response in server tab\n")
        else:
            print("✗ Failed to send message\n")

    def _show_status(self) -> None:
        """Show server status."""
        response = self.socket.get_status()
        if response:
            queue_size = response.get('queue_size', 0)
            queue_contents = response.get('queue_contents', [])
            ready = response.get('ready', False)

            print("\n📊 Server status:")
            print(f"  Ready: {'✓' if ready else '✗'}")
            print(f"  Queue: {queue_size} message{'s' if queue_size != 1 else ''}")

            if queue_contents:
                print("\n  Messages in queue (0=first, use '!e <index>' to edit):")
                for i, msg in enumerate(queue_contents):
                    msg_preview = msg[:70] + '...' if len(msg) > 70 else msg
                    print(f"    [{i}] {msg_preview}")
            else:
                print("  (queue is empty)")
            print()
        else:
            print("✗ Could not get server status\n")

    def _force_send(self) -> None:
        """Force send next queued message."""
        response = self.socket.force_send_next()
        if response:
            if response.get('status') == 'sent':
                msg = response.get('message', '')
                remaining = response.get('queue_size', 0)
                msg_preview = msg[:60] + '...' if len(msg) > 60 else msg
                print(f"⚡ Force-sent: {msg_preview} ({remaining} remaining)\n")
            elif response.get('status') == 'empty':
                print("✓ Queue is empty - nothing to send\n")
        else:
            print("✗ Could not force-send message\n")

    def _edit_message(self, index: int) -> None:
        """
        Edit a queued message by index.

        Args:
            index: Queue index (0-based) of message to edit.
        """
        # Get the message by index
        response = self.socket.get_message_for_edit(index)
        if not response or response.get('status') != 'ok':
            print(f"✗ Invalid index: {index}\n")
            return

        msg_id = response.get('id', '')
        original_message = response.get('message', '')

        # Display what we're editing
        print(f"\nEditing <{msg_id}>:")
        msg_preview = original_message[:80] + '...' if len(original_message) > 80 else original_message
        print(f'"{msg_preview}"')
        print()

        # Prompt for new message
        try:
            new_message = input("New message (or Ctrl+D to cancel): ").strip()

            if not new_message:
                print("✗ Edit cancelled (empty message)\n")
                return

            # Send edit request
            edit_response = self.socket.edit_message(msg_id, new_message)
            if edit_response and edit_response.get('status') == 'ok':
                print(f"✓ Edited message <{msg_id}>\n")
            else:
                error_msg = edit_response.get('message', 'Unknown error') if edit_response else 'Communication error'
                print(f"✗ {error_msg}\n")

        except EOFError:
            print("\n✗ Edit cancelled\n")

    def _print_commands_help(self) -> None:
        """Print the commands help section.

        All emojis must be 2 display columns wide for consistent alignment.
        Avoid single-width emojis (e.g. ⚡ U+26A1) as they render inconsistently.
        """
        CMD_WIDTH = 35

        commands = [
            ("\U0001F4D6", "!h or !help",                     "Show this help"),
            ("\U0001F4AC", "Type message",                    "Queue message (auto-sends)"),
            ("\U0001F4F7", "!ip <msg> or !imagepaste <msg>",  "Queue with clipboard image"),
            ("\U0001F4E4", "!d <msg> or !direct <msg>",       "Send directly (bypass queue)"),
            ("\U0001F4CB", "!l or !list",                     "Show queue"),
            ("\U0001F4DD", "!e <index> or !edit <index>",     "Edit queued message by index"),
            ("\U0001F9F9", "!c or !clear",                    "Clear queue"),
            ("\U0001F525", "!f or !force",                    "Force-send next queued message"),
            ("\U0001F44B", "!x or !quit (Ctrl+D)",            "Exit client"),
        ]

        for emoji, cmd, desc in commands:
            print(f"  {emoji}  {cmd.ljust(CMD_WIDTH)} \u2192 {desc}")
        print()
        print("  \U0001F916 Auto-queue: Server handles auto-sending")
        print()
        print("  \U0001F514 Toggle auto-sent notifications: !auto-sent on/off  (or !asm on/off)")
        print("=" * 70)
        print()

    def _print_startup_banner(self) -> None:
        """Print client startup banner."""
        set_terminal_title(f"cq-client {self.tag}")
        print_banner('client', self.tag)

        print(f"  Sending messages to ClaudeQ PTY server '{self.tag}'")
        print("  Watch responses in server tab")
        print()

        self._print_commands_help()

    def _handle_direct_command(self, line: str) -> None:
        """
        Handle !d / !direct command with optional image attachment.

        Args:
            line: Full input line starting with !d or !direct.
        """
        line_lower = line.lower()

        if line_lower.startswith('!d!ip'):
            rest = line[2:].strip()
        elif line_lower.startswith('!d '):
            rest = line[3:].strip()
        else:
            rest = line[8:].strip()  # !direct

        rest_lower = rest.lower()

        if rest_lower.startswith('!ip') or rest_lower.startswith('!imagepaste'):
            if rest_lower.startswith('!ip '):
                msg = rest[4:].strip()
            elif rest_lower.startswith('!ip'):
                msg = rest[3:].strip()
            elif rest_lower.startswith('!imagepaste '):
                msg = rest[12:].strip()
            else:
                msg = rest[11:].strip()

            self._handle_imagepaste(msg if msg else "", is_direct=True)
        else:
            if rest:
                self._send_direct(rest)
            else:
                print("✗ No message provided\n")

    def _handle_auto_sent_toggle(self, line_lower: str) -> None:
        """
        Handle !auto-sent / !asm toggle command.

        Args:
            line_lower: Lowercased input line.
        """
        parts = line_lower.split(None, 1)
        if len(parts) < 2:
            status = "on" if self.show_auto_sent_notifications else "off"
            print(f"Auto-sent notifications: {status}")
            print("Usage: !auto-sent on/off  or  !asm on/off\n")
            return

        toggle = parts[1].strip()
        if toggle in ['on', 'true', '1', 'yes']:
            self.show_auto_sent_notifications = True
            settings = self._load_settings()
            settings['show_auto_sent_notifications'] = True
            self._save_settings(settings)
            print("✓ Auto-sent notifications enabled (saved globally)\n")
        elif toggle in ['off', 'false', '0', 'no']:
            self.show_auto_sent_notifications = False
            settings = self._load_settings()
            settings['show_auto_sent_notifications'] = False
            self._save_settings(settings)
            print("✓ Auto-sent notifications disabled (saved globally)\n")
        else:
            print("✗ Invalid option. Use: on/off\n")

    def _handle_edit_command(self, line: str) -> None:
        """
        Handle !e / !edit command.

        Args:
            line: Full input line starting with !e or !edit.
        """
        parts = line.split(None, 1)
        if len(parts) < 2:
            print("Usage: !e <index>  (e.g., !e 0 to edit first message)\n")
            return

        try:
            index = int(parts[1])
            self._edit_message(index)
        except ValueError:
            print("✗ Invalid index - must be a number\n")

    def _handle_imagepaste_command(self, line: str) -> None:
        """
        Handle !ip / !imagepaste command parsing.

        Args:
            line: Full input line starting with !ip or !imagepaste.
        """
        line_lower = line.lower()

        if line_lower.startswith('!ip '):
            msg = line[4:].strip()
        elif line_lower.startswith('!imagepaste '):
            msg = line[12:].strip()
        elif line_lower in ['!ip', '!imagepaste']:
            msg = None
        else:
            return

        self._handle_imagepaste(msg)

    def _process_command(self, line: str) -> bool:
        """
        Process a command line.

        Args:
            line: User input line.

        Returns:
            True to continue, False to exit.
        """
        line_lower = line.lower()

        # Empty line with pending image
        if not line and self.pending_image_path:
            self._queue_add("")
            return True

        if not line:
            return True

        # !h / !help
        if line_lower in ['!h', '!help']:
            print()
            self._print_commands_help()
            return True

        # Check for image file in line
        if not self.pending_image_path and not line_lower.startswith('!'):
            line = self._process_image_in_line(line)

        # !ip / !imagepaste
        if line_lower.startswith('!ip') or line_lower.startswith('!imagepaste'):
            self._handle_imagepaste_command(line)
            return True

        # !d / !direct
        if line_lower in ['!d', '!direct']:
            print("Usage: !d <msg>  (e.g., !d fix the bug)\n")
            return True
        if (line_lower.startswith('!d ') or line_lower.startswith('!d!ip')
                or line_lower.startswith('!direct ')):
            self._handle_direct_command(line)
            return True

        # !l / !list
        if line_lower in ['!l', '!list']:
            self._show_status()
            return True

        # !c / !clear
        if line_lower in ['!c', '!clear']:
            print("⚠ Queue is managed by server\n")
            return True

        # !f / !force
        if line_lower in ['!f', '!force']:
            self._force_send()
            return True

        # !e / !edit
        if line_lower in ['!e', '!edit']:
            print("Usage: !e <index>  (e.g., !e 0 to edit first message)\n")
            return True
        if line_lower.startswith('!e ') or line_lower.startswith('!edit '):
            self._handle_edit_command(line)
            return True

        # !auto-sent / !asm
        if line_lower in ['!auto-sent', '!asm']:
            status = "on" if self.show_auto_sent_notifications else "off"
            print(f"Auto-sent notifications: {status}")
            print("Usage: !auto-sent on/off  (or !asm on/off)\n")
            return True
        if line_lower.startswith('!auto-sent ') or line_lower.startswith('!asm '):
            self._handle_auto_sent_toggle(line_lower)
            return True

        # !x / !quit / !exit
        if line_lower in ['!x', '!quit', '!exit']:
            return False

        # Trailing !ip
        if line_lower.endswith(' !ip') or line_lower.endswith(' !imagepaste'):
            if line_lower.endswith(' !ip'):
                msg = line[:-4].strip()
            else:
                msg = line[:-12].strip()

            self._handle_imagepaste(msg)
            return True

        # Unknown ! command
        if line_lower.startswith('!'):
            print(f"Unknown command: {line.split()[0]}\n")
            self._print_commands_help()
            return True

        # Regular message - queue it
        self._queue_add(line)
        return True

    def run(self) -> None:
        """Run the client main loop."""
        if not self.socket.is_server_running():
            print(f"Error: PTY server '{self.tag}' is not running")
            print()
            print("Start it first:")
            print(f"  Tab 1: cq {self.tag}")
            print(f"  Tab 2: cq {self.tag} 'your message'")
            print()
            sys.exit(1)

        self._print_startup_banner()
        print("Ready! Type your messages:\n")

        # Get initial queue status
        response = self.socket.get_status()
        if response:
            queue_size = response.get('queue_size', 0)
            if queue_size > 0:
                print(f"📝 Queue has {queue_size} messages\n")

        # Start queue monitor
        self.monitor_thread = threading.Thread(
            target=self._queue_monitor_loop,
            daemon=True
        )
        self.monitor_thread.start()

        context_manager = self.input_handler.get_context_manager()

        try:
            if context_manager:
                context_manager.__enter__()

            while True:
                try:
                    line = self.input_handler.get_input().strip()
                    if not self._process_command(line):
                        break
                except EOFError:
                    break

        except KeyboardInterrupt:
            print("\n\nExiting...")

        finally:
            if context_manager:
                try:
                    context_manager.__exit__(None, None, None)
                except Exception:
                    pass

            self.running = False
            if self.monitor_thread:
                self.monitor_thread.join(timeout=1)

            self.input_handler.save_history()

        print("\nGoodbye!")
        response = self.socket.get_status()
        if response:
            queue_size = response.get('queue_size', 0)
            if queue_size > 0:
                print(f"📝 Queue has {queue_size} messages remaining")
        print(f"PTY server '{self.tag}' is still running.\n")


def main() -> None:
    """Entry point for claudeq-client command."""
    if len(sys.argv) < 2:
        print("Usage: claudeq-client <tag>")
        print()
        print("Example:")
        print("  Tab 1: cq my-feature")
        print("  Tab 2: cq my-feature")
        print()
        sys.exit(1)

    tag = sys.argv[1]

    if tag.startswith('-'):
        print("Error: Tag cannot start with '-'")
        print("Usage: claudeq-client <tag>")
        sys.exit(1)

    client = ClaudeQClient(tag)
    client.run()


if __name__ == '__main__':
    main()
