"""
Main Leap PTY Client.

Interactive client for sending messages to a Leap server.
"""

import atexit
import fcntl
import hashlib
import json
import os
import signal
import sys
import threading
import time
from typing import Optional

from leap.cli_providers.registry import DEFAULT_PROVIDER, get_provider
from leap.cli_providers.states import AutoSendMode, CLIState
from leap.utils.constants import (
    QUEUE_DIR, SOCKET_DIR, HISTORY_DIR, SLACK_DIR, SLACK_BOT_LOCK,
    ensure_storage_dirs, load_settings, save_settings,
)
from leap.utils.terminal import set_terminal_title, print_banner
from leap.utils.ide_detection import detect_ide
from leap.client.socket_client import SocketClient
from leap.client.image_handler import (
    check_clipboard_has_image,
    save_clipboard_image,
)
from leap.client.input_handler import InputHandler



class LeapClient:
    """
    Leap PTY Client.

    Interactive client for sending messages to a Leap server session.
    """

    def __init__(self, tag: str) -> None:
        """
        Initialize Leap client.

        Args:
            tag: Session tag name to connect to.
        """
        self.tag = tag
        self.running = True
        self.pending_image_path: Optional[str] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self.temp_image_files: list[str] = []  # Track temp files for cleanup
        self._image_counter = 0
        self._image_placeholders: dict[str, str] = {}  # "[Image #N]" → path

        # Ensure storage directories exist
        ensure_storage_dirs()

        # Initialize paths
        self.socket_path = SOCKET_DIR / f"{tag}.sock"
        self.queue_file = QUEUE_DIR / f"{tag}.queue"
        self.history_file = HISTORY_DIR / f"{tag}.history"
        self.lock_file = SOCKET_DIR / f"{tag}.client.lock"

        # Load settings from file (persistent across all clients)
        self.show_auto_sent_notifications = load_settings().get('show_auto_sent_notifications', True)

        # Acquire exclusive lock and register cleanup immediately so
        # the lock is always released even if __init__ fails later.
        self._acquire_lock()
        atexit.register(self._cleanup_lock)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGHUP, self._signal_handler)  # Terminal close (Cmd+W)

        # Error tracking for rate limiting
        self._last_socket_error_time = 0
        self._socket_error_cooldown = 5.0  # Only show error once per 5 seconds

        # Initialize components
        self.socket = SocketClient(self.socket_path, error_callback=self._should_print_socket_error)
        self.input_handler = InputHandler(
            self.history_file, self._get_prompt,
            on_paste_image=self._paste_clipboard_image,
        )

    def _acquire_lock(self) -> None:
        """Acquire exclusive client lock to prevent multiple clients.

        Uses flock + inode verification to handle the case where the
        monitor deletes the lock file while the old client still holds
        an flock on the (now orphaned) fd.  After locking, we verify
        the on-disk file is the same inode we locked; if not, another
        process replaced it, so we retry.
        """
        max_attempts = 3
        for _attempt in range(max_attempts):
            try:
                fd = open(self.lock_file, 'w')
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                # Verify the file on disk is the one we locked.
                # If the monitor deleted the file between our open() and
                # flock(), another client may have created a new file.
                try:
                    disk_ino = self.lock_file.stat().st_ino
                except OSError:
                    disk_ino = None
                locked_ino = os.fstat(fd.fileno()).st_ino

                if disk_ino != locked_ino:
                    # We locked an orphaned file — close and retry.
                    fd.close()
                    continue

                fd.write(str(os.getpid()))
                fd.flush()
                os.fsync(fd.fileno())
                self.lock_fd = fd
                self._lock_ino = locked_ino
                return
            except BlockingIOError:
                try:
                    fd.close()
                except (OSError, UnboundLocalError):
                    pass
                break
            except OSError as e:
                print(f"Error creating lock file: {e}")
                sys.exit(1)

        # If we get here, flock failed (BlockingIOError) or inode retries exhausted.
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

        # Only delete the lock file if it's still ours (same inode).
        # Another client may have replaced it after the monitor removed ours.
        try:
            if self.lock_file.exists():
                current_ino = self.lock_file.stat().st_ino
                if hasattr(self, '_lock_ino') and current_ino != self._lock_ino:
                    return  # File belongs to a different client
                self.lock_file.unlink()
        except OSError:
            pass

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

    def _signal_handler(self, signum: int, frame: object) -> None:
        """Handle termination signals."""
        self.running = False
        self._cleanup_lock()
        os._exit(0)

    def _paste_clipboard_image(self) -> Optional[str]:
        """Check clipboard for an image, save it, and return display text.

        Returns:
            Placeholder text like ``[Image #1] `` to insert into the input
            buffer, or None if clipboard has no image.
        """
        if not check_clipboard_has_image():
            return None
        image_path = save_clipboard_image()
        if not image_path:
            return None

        # Check if this image is identical to one we already saved
        try:
            with open(image_path, 'rb') as f:
                new_hash = hashlib.md5(f.read()).hexdigest()
            for placeholder, existing_path in self._image_placeholders.items():
                try:
                    with open(existing_path, 'rb') as f:
                        existing_hash = hashlib.md5(f.read()).hexdigest()
                    if new_hash == existing_hash:
                        # Same image — discard duplicate temp file, reuse placeholder
                        os.unlink(image_path)
                        return f'{placeholder} '
                except OSError:
                    continue
        except OSError:
            pass

        if image_path.startswith('/tmp/'):
            self.temp_image_files.append(image_path)
        self._image_counter += 1
        placeholder = f'[Image #{self._image_counter}]'
        self._image_placeholders[placeholder] = image_path
        return f'{placeholder} '

    def _resolve_image_placeholders(self, message: str) -> str:
        """Replace ``[Image #N]`` placeholders with ``@path`` references.

        Resets the counter and placeholder map so the next prompt starts
        from ``[Image #1]`` again.
        """
        for placeholder, path in self._image_placeholders.items():
            if placeholder in message:
                message = message.replace(placeholder, f'@{path}')
        self._image_counter = 0
        self._image_placeholders.clear()
        return message

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
        last_bot_running: Optional[bool] = None

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

            # Detect Slack bot start/stop transitions
            slack_enabled = response.get('slack_enabled', False)
            if slack_enabled:
                bot_running = SLACK_BOT_LOCK.is_dir()
                if last_bot_running is not None and bot_running != last_bot_running:
                    if not bot_running:
                        print("\n⚠ Slack bot stopped — integration paused "
                              "for this session", flush=True)
                        print("  (will reconnect automatically "
                              "when the bot starts)", flush=True)
                    else:
                        print("\n✓ Slack bot reconnected — integration "
                              "resumed for this session", flush=True)
                last_bot_running = bot_running
            else:
                last_bot_running = None

            # Detect externally added messages (e.g. from GitLab via monitor)
            if poll_count > 1 and self.show_auto_sent_notifications:
                last_ids = {self._extract_queue_id(m) for m in last_queue_contents}
                for entry in queue_contents:
                    entry_id = self._extract_queue_id(entry)
                    if entry_id and entry_id not in last_ids:
                        # Extract message body after the ID tag
                        msg_body = entry.split('> ', 1)[1] if '> ' in entry else entry
                        if msg_body.startswith('[scm] '):
                            preview = msg_body[6:66] + '...' if len(msg_body) > 72 else msg_body[6:]
                            print(f"\n📩 SCM review queued: {preview}", flush=True)
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

    def _queue_add(self, message: str) -> None:
        """Add message to queue with any pending image."""
        message = self._resolve_image_placeholders(message)
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
        """Send message directly to the CLI."""
        message = self._resolve_image_placeholders(message)
        full_message = self._build_message_with_image(message)

        response = self.socket.send_direct(full_message)
        if response and response.get('status') == 'sent':
            print(f"✓ Sent to '{self.tag}'")
            print("   See response in server tab\n")
        else:
            print("✗ Failed to send message\n")

    def _show_status(self) -> None:
        """Show server status."""
        response = self.socket.get_status()
        if response:
            queue_size = response.get('queue_size', 0)
            queue_contents = response.get('queue_contents', [])
            cli_state = response.get('cli_state', CLIState.IDLE)
            auto_send_mode = response.get('auto_send_mode', AutoSendMode.PAUSE)

            state_display = {
                CLIState.IDLE: '\u2713 Idle \u2014 will accept next message',
                CLIState.RUNNING: '\u23f3 Running \u2014 processing',
                CLIState.NEEDS_PERMISSION: '\u26a0\ufe0f Needs Permission \u2014 waiting for tool approval',
                CLIState.NEEDS_INPUT: '\u2753 Needs Input \u2014 asking you something',
                CLIState.INTERRUPTED: '\u26a1 Interrupted \u2014 will auto-send next message',
            }
            mode_display = {
                AutoSendMode.PAUSE: 'Pause on input',
                AutoSendMode.ALWAYS: 'Always send',
            }

            print("\n\U0001f4ca Server status:")
            print(f"  {state_display.get(cli_state, cli_state)}")
            print(f"  Auto-send: {mode_display.get(auto_send_mode, auto_send_mode)}")
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
            print("\u2717 Could not get server status\n")

    def _handle_clear(self) -> None:
        """Clear all queued messages without sending them."""
        confirm = input("Delete all queued messages? [y/N] ").strip().lower()
        if confirm not in ('y', 'yes'):
            print("Cancelled\n")
            return
        response = self.socket.clear_queue()
        if response and response.get('status') == 'ok':
            print("\u2713 Queue cleared\n")
        else:
            print("\u2717 Could not clear queue\n")

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
        print("  \U0001F5BC  Ctrl+V pastes clipboard image as [Image #N]")
        print()
        # Fetch current auto-send mode from server
        response = self.socket.get_status(silent=True)
        as_mode = response.get('auto_send_mode', AutoSendMode.PAUSE) if response else AutoSendMode.PAUSE
        as_label = 'pause' if as_mode == AutoSendMode.PAUSE else 'always'
        notif_label = 'on' if self.show_auto_sent_notifications else 'off'

        print(f"  \U0001F916 Auto-send mode: !autosend always/pause      (or !as)   [current: {as_label}]")
        print(f"  \U0001F514 Auto-sent notifications: !auto-sent on/off  (or !asm)  [current: {notif_label}]")
        if self._is_slack_installed():
            print()
            slack_enabled = response.get('slack_enabled', False) if response else False
            bot_running = SLACK_BOT_LOCK.is_dir()
            if not bot_running:
                slack_label = 'bot not running'
            else:
                slack_label = 'on' if slack_enabled else 'off'
            print(f"  \U0001F4EC Slack integration: !slack on/off                       [current: {slack_label}]")
        print()
        print("=" * 80)
        print()

    def _read_cli_name(self) -> str:
        """Read CLI display name from server metadata file."""
        try:
            meta_file = SOCKET_DIR / f"{self.tag}.meta"
            with open(meta_file, 'r') as f:
                data = json.load(f)
            provider_name = data.get('cli_provider', DEFAULT_PROVIDER)
            return get_provider(provider_name).display_name
        except Exception:
            return ''

    def _print_startup_banner(self) -> None:
        """Print client startup banner."""
        set_terminal_title(f"lpc {self.tag}")
        print_banner('client', self.tag, cli_name=self._read_cli_name())

        print(f"  Sending messages to Leap PTY server '{self.tag}'")
        print("  Watch responses in server tab")
        print()

        self._print_commands_help()

    def _handle_direct_command(self, line: str) -> None:
        """
        Handle !d / !direct command.

        Args:
            line: Full input line starting with !d or !direct.
        """
        line_lower = line.lower()

        if line_lower.startswith('!d '):
            rest = line[3:].strip()
        else:
            rest = line[8:].strip()  # !direct

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
            settings = load_settings()
            settings['show_auto_sent_notifications'] = True
            save_settings(settings)
            print("✓ Auto-sent notifications enabled (saved globally)\n")
        elif toggle in ['off', 'false', '0', 'no']:
            self.show_auto_sent_notifications = False
            settings = load_settings()
            settings['show_auto_sent_notifications'] = False
            save_settings(settings)
            print("✓ Auto-sent notifications disabled (saved globally)\n")
        else:
            print("✗ Invalid option. Use: on/off\n")

    def _handle_auto_send_mode(self, line_lower: str) -> None:
        """
        Handle !autosend / !as mode command.

        Args:
            line_lower: Lowercased input line.
        """
        parts = line_lower.split(None, 1)
        if len(parts) < 2:
            # Show current mode from server
            response = self.socket.get_status(silent=True)
            mode = response.get('auto_send_mode', AutoSendMode.PAUSE) if response else AutoSendMode.PAUSE
            mode_display = {AutoSendMode.PAUSE: 'Pause on input', AutoSendMode.ALWAYS: 'Always send'}
            print(f"Auto-send mode: {mode_display.get(mode, mode)}")
            print("Usage: !autosend pause/always  (or !as pause/always)\n")
            return

        mode = parts[1].strip()
        if mode not in (AutoSendMode.PAUSE, AutoSendMode.ALWAYS):
            print("\u2717 Invalid mode. Use: pause or always\n")
            return

        response = self.socket.set_auto_send_mode(mode)
        if response and response.get('status') == 'ok':
            mode_display = {AutoSendMode.PAUSE: 'Pause on input', AutoSendMode.ALWAYS: 'Always send'}
            print(f"\u2713 Auto-send mode: {mode_display.get(mode, mode)}\n")
        else:
            print("\u2717 Failed to set auto-send mode\n")

    def _is_slack_installed(self) -> bool:
        """Check if the Slack app has been configured."""
        return (SLACK_DIR / "config.json").exists()

    def _handle_slack_command(self, line: str) -> None:
        """Handle !slack command to show status or toggle Slack integration.

        Args:
            line: Full input line starting with !slack.
        """
        if not self._is_slack_installed():
            print("Slack app not installed. Run: make install-slack-app\n")
            return

        bot_running = SLACK_BOT_LOCK.is_dir()

        parts = line.lower().split(None, 1)
        if len(parts) < 2:
            # Show current Slack status
            response = self.socket.get_status(silent=True)
            if response:
                enabled = response.get('slack_enabled', False)
                if not bot_running:
                    print("\nSlack bot is not running.")
                    print("Start it from the monitor or run: leap --slack")
                    if enabled:
                        print("(Session will reconnect automatically "
                              "when the bot starts)")
                else:
                    status = "on" if enabled else "off"
                    print(f"\nSlack integration: {status}")
                print("Usage: !slack on/off\n")
            else:
                print("Could not get server status\n")
            return

        toggle = parts[1].strip()
        if toggle in ('on', 'true', '1', 'yes'):
            if not bot_running:
                print("Slack bot is not running. "
                      "Start it from the monitor or run: leap --slack\n")
                return
            response = self.socket._send_request({
                'type': 'set_slack', 'enabled': True,
            })
            if response and response.get('status') == 'ok':
                print("Slack integration enabled for this session\n")
            else:
                print("Failed to enable Slack integration\n")
        elif toggle in ('off', 'false', '0', 'no'):
            response = self.socket._send_request({
                'type': 'set_slack', 'enabled': False,
            })
            if response and response.get('status') == 'ok':
                print("Slack integration disabled for this session\n")
            else:
                print("Failed to disable Slack integration\n")
        else:
            print("Invalid option. Use: !slack on/off\n")

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

        # !d / !direct
        if line_lower in ['!d', '!direct']:
            print("Usage: !d <msg>  (e.g., !d fix the bug)\n")
            return True
        if line_lower.startswith('!d ') or line_lower.startswith('!direct '):
            self._handle_direct_command(line)
            return True

        # !l / !list
        if line_lower in ['!l', '!list']:
            self._show_status()
            return True

        # !c / !clear
        if line_lower in ['!c', '!clear']:
            self._handle_clear()
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

        # !autosend / !as
        if line_lower in ['!autosend', '!as']:
            self._handle_auto_send_mode(line_lower)
            return True
        if line_lower.startswith('!autosend ') or line_lower.startswith('!as '):
            self._handle_auto_send_mode(line_lower)
            return True

        # !slack
        if line_lower == '!slack':
            self._handle_slack_command(line)
            return True
        if line_lower.startswith('!slack '):
            self._handle_slack_command(line)
            return True

        # !x / !quit / !exit
        if line_lower in ['!x', '!quit', '!exit']:
            return False

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
            print(f"  Tab 1: leap {self.tag}")
            print(f"  Tab 2: leap {self.tag} 'your message'")
            print()
            sys.exit(1)

        self._print_startup_banner()
        print("Ready! Type your messages:")

        # Show newline hint for terminals where Shift+Enter doesn't work
        _SHIFT_ENTER_TERMINALS = {'VS Code', 'iTerm2', 'Warp', 'Kitty', 'Ghostty'}
        ide = detect_ide()
        if ide not in _SHIFT_ENTER_TERMINALS:
            if ide == 'Terminal.app':
                print("Tip: Press Escape then Enter for newline (Shift+Enter not supported in Terminal.app)")
            else:
                print("Tip: Use Alt+Enter for newline (Shift+Enter not supported in this terminal)")
        print()

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
    """Entry point for leap-client command."""
    if len(sys.argv) < 2:
        print("Usage: leap-client <tag>")
        print()
        print("Example:")
        print("  Tab 1: leap my-feature")
        print("  Tab 2: leap my-feature")
        print()
        sys.exit(1)

    tag = sys.argv[1]

    if tag.startswith('-'):
        print("Error: Tag cannot start with '-'")
        print("Usage: leap-client <tag>")
        sys.exit(1)

    client = LeapClient(tag)
    client.run()


if __name__ == '__main__':
    main()
