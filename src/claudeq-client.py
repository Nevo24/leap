#!/usr/bin/env python3
"""
ClaudeQ PTY Client - Send messages to PTY server via Unix socket
Usage: claudeq-client-pty <tag>
"""

import sys
import os
import socket
import json
import threading
import time
import readline
import subprocess
import tempfile
import signal
import fcntl
from collections import deque
from pathlib import Path

# Try to import prompt_toolkit for better input handling
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.history import FileHistory
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    HAS_PROMPT_TOOLKIT = False

QUEUE_DIR = Path.home() / ".claude-queues"
SOCKET_DIR = Path.home() / ".claude-sockets"

def check_clipboard_has_image():
    """Check if clipboard contains an image (macOS)"""
    try:
        result = subprocess.run(
            ['osascript', '-e', 'clipboard info'],
            capture_output=True, text=True, timeout=1
        )
        return 'picture' in result.stdout.lower()
    except:
        return False

def save_clipboard_image():
    """Save clipboard image to temp file (macOS)"""
    try:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
        temp_path = temp_file.name
        temp_file.close()

        script = f'''
        set png_data to the clipboard as «class PNGf»
        set the_file to open for access POSIX file "{temp_path}" with write permission
        write png_data to the_file
        close access the_file
        '''

        result = subprocess.run(['osascript', '-e', script], capture_output=True, timeout=5)
        if result.returncode == 0 and os.path.exists(temp_path):
            return temp_path
        return None
    except:
        return None

def is_image_file(path):
    """Check if path points to an image file"""
    if not os.path.exists(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    return ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp']

class ClaudePTYClient:
    def __init__(self, tag):
        self.tag = tag
        self.socket_path = SOCKET_DIR / f"{tag}.sock"
        self.queue_file = QUEUE_DIR / f"{tag}.queue"
        self.history_file = QUEUE_DIR / f"{tag}.history"
        self.lock_file = SOCKET_DIR / f"{tag}.client.lock"
        self.message_queue = deque()
        self.running = True
        self.queue_monitor_thread = None
        self.pending_image_path = None  # Track pending image attachment

        # Use exclusive file locking to prevent multiple clients
        # Open/create lock file in write mode
        try:
            self.lock_fd = open(self.lock_file, 'w')
            # Try to acquire exclusive lock (non-blocking)
            fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # We got the lock! Write our PID
            self.lock_fd.write(str(os.getpid()))
            self.lock_fd.flush()
        except BlockingIOError:
            # Lock is held by another process
            # Try to read the PID to show in error message
            try:
                with open(self.lock_file, 'r') as f:
                    pid = f.read().strip()
                    pid_info = f" (PID: {pid})"
            except:
                pid_info = ""

            print(f"\n❌ Error: Another client is already connected to server '{tag}'{pid_info}")
            print(f"Only one interactive client per server is allowed.\n")
            print(f"If you're sure no other client is running, remove:")
            print(f"  {self.lock_file}\n")
            sys.exit(1)
        except Exception as e:
            print(f"Error creating lock file: {e}")
            sys.exit(1)

        # Register cleanup for normal exit and signals
        import atexit
        atexit.register(self.cleanup_lock)

        # Handle termination signals to ensure cleanup
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Setup prompt_toolkit if available
        if HAS_PROMPT_TOOLKIT:
            self.prompt_session = PromptSession(history=FileHistory(str(self.history_file)))
        else:
            self.prompt_session = None
            self.load_history()

        self.load_queue()

    def load_queue(self):
        """Load queue from file"""
        # Clear existing queue first
        self.message_queue.clear()

        if self.queue_file.exists():
            with open(self.queue_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.message_queue.append(line)

    def save_queue(self):
        """Save queue to file"""
        with open(self.queue_file, 'w') as f:
            for msg in self.message_queue:
                f.write(msg + '\n')

    def load_history(self):
        """Load command history"""
        if not HAS_PROMPT_TOOLKIT and self.history_file.exists():
            try:
                readline.read_history_file(str(self.history_file))
            except:
                pass

    def save_history(self):
        """Save command history"""
        if not HAS_PROMPT_TOOLKIT:
            try:
                readline.write_history_file(str(self.history_file))
            except:
                pass

    def _get_prompt(self):
        """Generate current prompt text based on queue state"""
        if self.pending_image_path:
            return "[📸] You: "
        else:
            return "You: "

    def _custom_input(self):
        """Custom input using prompt_toolkit if available"""
        if HAS_PROMPT_TOOLKIT and self.prompt_session:
            # Use callable prompt for dynamic updates
            return self.prompt_session.prompt(self._get_prompt)
        else:
            # Static prompt for basic input()
            return input(self._get_prompt())

    def check_server_exists(self):
        """Check if PTY server is running by checking socket"""
        return self.socket_path.exists()

    def send_to_server(self, msg_type, message=""):
        """Send message to PTY server via socket"""
        try:
            client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_socket.connect(str(self.socket_path))

            data = {
                'type': msg_type,
                'message': message
            }

            client_socket.send(json.dumps(data).encode('utf-8'))
            response = client_socket.recv(4096).decode('utf-8')
            client_socket.close()

            return json.loads(response)
        except Exception as e:
            print(f"Error communicating with server: {e}")
            return None

    def queue_add(self, message):
        """Add to queue"""
        # Check if there's a pending image
        full_message = message
        has_image = False
        if self.pending_image_path:
            # Add space after image path even if message is empty
            # Claude CLI needs the space to recognize the attachment
            if message:
                full_message = f"@{self.pending_image_path} {message}"
            else:
                full_message = f"@{self.pending_image_path} "
            self.pending_image_path = None
            has_image = True

        # Send to server and sync local queue
        response = self.send_to_server('queue', full_message)
        if response:
            # Sync local queue with server's authoritative state
            queue_contents = response.get('queue_contents', [])
            queue_size = response.get('queue_size', 0)

            self.message_queue.clear()
            self.message_queue.extend(queue_contents)

            if has_image:
                print(f"📝 Queued with image ({queue_size} total)\n")
            else:
                print(f"📝 Queued ({queue_size} total)\n")
        else:
            print(f"✗ Failed to queue message\n")

    def send_direct(self, message):
        """Send directly to Claude"""
        # Check if there's a pending image
        full_message = message
        if self.pending_image_path:
            # Add space after image path even if message is empty
            # Claude CLI needs the space to recognize the attachment
            if message:
                full_message = f"@{self.pending_image_path} {message}"
            else:
                full_message = f"@{self.pending_image_path} "
            self.pending_image_path = None

        response = self.send_to_server('direct', full_message)
        if response and response.get('status') == 'sent':
            print(f"✓ Sent to Claude '{self.tag}'")
            print(f"   See response in server tab\n")
        else:
            print("✗ Failed to send message\n")

    def queue_list(self):
        """List queue"""
        # Refresh from file first
        self.load_queue()

        if not self.message_queue:
            print("✓ Queue is empty\n")
        else:
            print(f"📝 Queue ({len(self.message_queue)} messages):")
            for i, msg in enumerate(self.message_queue, 1):
                print(f"   {i}. {msg}")
            print()

    def queue_clear(self):
        """Clear queue"""
        # Client should not manage queue file - just reload from server
        self.load_queue()
        if not self.message_queue:
            print("✓ Queue is empty\n")
        else:
            print(f"⚠ Queue has {len(self.message_queue)} messages (managed by server)\n")

    def get_status(self):
        """Get server status"""
        response = self.send_to_server('status')
        if response:
            print(f"Server status:")
            print(f"  Queue size: {response.get('queue_size', '?')}")
            print(f"  Ready: {response.get('ready', False)}\n")
        else:
            print("✗ Could not get server status\n")

    def force_send_next(self):
        """Force send next message from queue"""
        response = self.send_to_server('force_send')
        if response:
            if response.get('status') == 'sent':
                msg = response.get('message', '')
                remaining = response.get('queue_size', 0)
                print(f"⚡ Force-sent: {msg[:60]}{'...' if len(msg) > 60 else ''} ({remaining} remaining)\n")
            elif response.get('status') == 'empty':
                print("✓ Queue is empty - nothing to send\n")
        else:
            print("✗ Could not force-send message\n")

    def queue_monitor(self):
        """Background thread to monitor queue changes"""
        last_recently_sent = []  # Track last snapshot of recently_sent list
        while self.running:
            time.sleep(0.3)  # Poll 3x per second to catch fast queue changes

            # Query server for authoritative queue state
            response = self.send_to_server('status')
            if not response:
                continue

            new_size = response.get('queue_size', 0)
            current_queue = response.get('queue_contents', [])
            recently_sent = response.get('recently_sent', [])

            # Detect newly sent messages by comparing with last snapshot
            # Find messages added to the end of recently_sent list
            new_sent_messages = []
            if recently_sent and last_recently_sent:
                # Find the longest common suffix to identify new messages
                # Start from the end of both lists and work backwards
                common_len = 0
                for i in range(1, min(len(recently_sent), len(last_recently_sent)) + 1):
                    if recently_sent[-i] == last_recently_sent[-i]:
                        common_len = i
                    else:
                        break

                # Everything before the common suffix in recently_sent is new
                num_new = len(recently_sent) - common_len
                if num_new > 0:
                    new_sent_messages = recently_sent[:num_new]

            # Print notifications for new sent messages
            if new_sent_messages:
                for msg in new_sent_messages:
                    msg_preview = msg[:60] + '...' if len(msg) > 60 else msg
                    print(f"\n🤖 Server auto-sent: {msg_preview}", flush=True)

                # Show queue status after sending
                if new_size > 0:
                    print(f"   ({new_size} remaining in queue)", flush=True)
                else:
                    print(f"   (queue empty)", flush=True)

            # Update snapshot for next iteration
            last_recently_sent = list(recently_sent)

            # Sync local queue with server's authoritative state
            # Do this AFTER printing notifications so prompt timing is correct
            self.message_queue.clear()
            self.message_queue.extend(current_queue)

    def print_banner(self):
        """Print banner"""
        # Set terminal tab title
        print(f"\033]0;cq-client {self.tag}\007", end='', flush=True)

        print("""
   _____ _                 _       ___
  / ____| |               | |     / _ \\
 | |    | | __ _ _   _  __| | ___| | | |
 | |    | |/ _` | | | |/ _` |/ _ \\ | | |
 | |____| | (_| | |_| | (_| |  __/ |_| |
  \\_____|_|\\__,_|\\__,_|\\__,_|\\___|\\___\\
""")
        print("="*70)
        print(f"  PTY CLIENT - Session: {self.tag}")
        print("="*70)
        print(f"  Sending messages to ClaudeQ PTY server '{self.tag}'")
        print(f"  Watch responses in server tab")
        print()
        # Hardcoded spacing for perfect arrow alignment (emojis render with varying widths)
        # 🖼️ and 🗑️ render wider, so they need slightly MORE spaces to align
        print("  💬 Type message                        → Queue message (auto-sends)")
        print("  🖼️ :ip <msg> or :imagepaste <msg>      → Queue with clipboard image")
        print("  ⚡ :d <msg> or :direct <msg>           → Send directly (bypass queue)")
        print("  📋 :l or :list                         → Show queue")
        print("  🗑️ :c or :clear                        → Clear queue")
        print("  📊 :status                             → Server status")
        print("  ⚡ :f or :force                        → Force-send next queued message")
        print("  👋 :x or :quit (Ctrl+D)                → Exit client")
        print()
        print("  🤖 Auto-queue: Server handles auto-sending")
        print("="*70)
        print()
        print("  💡 JetBrains Users - Enable CQ to name your tabs:")
        print("     1. Settings > Tools > Terminal > Engine: Classic")
        print("     2. Advanced Settings > Terminal > ☑️ 'Show application title'")
        print("="*70)
        print()

    def run(self):
        """Run client"""
        if not self.check_server_exists():
            print(f"Error: PTY server '{self.tag}' is not running")
            print()
            print(f"Start it first:")
            print(f"  Tab 1: cq {self.tag}")
            print(f"  Tab 2: cq {self.tag} 'your message'")
            print()
            sys.exit(1)

        self.print_banner()
        print("Ready! Type your messages:\n")

        if self.message_queue:
            print(f"📝 Queue has {len(self.message_queue)} messages\n")

        # Start queue monitor thread
        self.queue_monitor_thread = threading.Thread(target=self.queue_monitor, daemon=True)
        self.queue_monitor_thread.start()

        context_manager = patch_stdout() if HAS_PROMPT_TOOLKIT else None

        try:
            if context_manager:
                context_manager.__enter__()

            while True:
                try:
                    line = self._custom_input().strip()
                except EOFError:
                    break

                # If empty line but has pending image, queue just the image
                if not line and self.pending_image_path:
                    self.queue_add("")
                    continue

                if not line:
                    continue

                # Make commands case-insensitive
                line_lower = line.lower()

                # Check if line contains an image file path (before command processing)
                if line and not self.pending_image_path and not line_lower.startswith(':'):
                    words = line.split()
                    for word in words:
                        if is_image_file(word):
                            self.pending_image_path = word
                            line = line.replace(word, '').strip()
                            print(f"📎 Image attached: {os.path.basename(word)}")
                            break

                # Handle :ip or :imagepaste command
                if line_lower.startswith(':ip') or line_lower.startswith(':imagepaste'):
                    if line_lower.startswith(':ip '):
                        msg = line[4:].strip()
                    elif line_lower.startswith(':imagepaste '):
                        msg = line[12:].strip()
                    elif line_lower in [':ip', ':imagepaste']:
                        msg = None
                    else:
                        continue

                    if check_clipboard_has_image():
                        image_path = save_clipboard_image()
                        if image_path:
                            self.pending_image_path = image_path
                            if msg:
                                self.queue_add(msg)
                            else:
                                print(f"🖼️  Image attached! Type message and press Enter (or just Enter to queue image alone)")
                        else:
                            print("✗ Failed to save image from clipboard\n")
                    else:
                        print("✗ No image in clipboard\n")
                    continue

                # Handle :d or :direct command (including :d :ip)
                if line_lower.startswith(':d ') or line_lower.startswith(':d:ip') or line_lower.startswith(':direct '):
                    if line_lower.startswith(':d:ip'):
                        rest = line[2:].strip()
                    elif line_lower.startswith(':d '):
                        rest = line[3:].strip()
                    else:  # :direct
                        rest = line[8:].strip()

                    rest_lower = rest.lower()

                    # Check for :ip in the rest
                    if rest_lower.startswith(':ip') or rest_lower.startswith(':imagepaste'):
                        if rest_lower.startswith(':ip '):
                            msg = rest[4:].strip()
                        elif rest_lower.startswith(':ip'):
                            msg = rest[3:].strip()
                        elif rest_lower.startswith(':imagepaste '):
                            msg = rest[12:].strip()
                        else:
                            msg = rest[11:].strip()

                        # Grab image from clipboard
                        if check_clipboard_has_image():
                            image_path = save_clipboard_image()
                            if image_path:
                                self.pending_image_path = image_path
                                print(f"🖼️ Image grabbed from clipboard!")
                            else:
                                print("✗ Failed to save image from clipboard\n")
                        else:
                            print("✗ No image in clipboard\n")

                        # Send directly with or without message
                        self.send_direct(msg if msg else "")
                    else:
                        # Send directly without image command
                        if rest:
                            self.send_direct(rest)
                        else:
                            print("✗ No message provided\n")
                    continue

                if line_lower in [':l', ':list']:
                    self.queue_list()
                    continue

                if line_lower in [':c', ':clear']:
                    self.queue_clear()
                    continue

                if line_lower == ':status':
                    self.get_status()
                    continue

                if line_lower in [':f', ':force']:
                    self.force_send_next()
                    continue

                if line_lower in [':x', ':quit', ':exit']:
                    break

                # Handle trailing :ip (message followed by :ip to attach image)
                if line_lower.endswith(' :ip') or line_lower.endswith(' :imagepaste'):
                    if line_lower.endswith(' :ip'):
                        msg = line[:-4].strip()
                    else:  # ends with :imagepaste
                        msg = line[:-12].strip()

                    if check_clipboard_has_image():
                        image_path = save_clipboard_image()
                        if image_path:
                            self.pending_image_path = image_path
                            if msg:
                                self.queue_add(msg)
                            else:
                                print(f"🖼️  Image attached! Type message and press Enter (or just Enter to queue image alone)")
                        else:
                            print("✗ Failed to save image from clipboard\n")
                    else:
                        print("✗ No image in clipboard\n")
                    continue

                # Regular message - queue by default
                self.queue_add(line)

        except KeyboardInterrupt:
            print("\n\nExiting...")

        finally:
            if context_manager:
                try:
                    context_manager.__exit__(None, None, None)
                except:
                    pass

            # Stop queue monitor
            self.running = False
            if self.queue_monitor_thread:
                self.queue_monitor_thread.join(timeout=1)

            self.save_history()

        print("\nGoodbye!")
        if self.message_queue:
            print(f"📝 Queue has {len(self.message_queue)} messages remaining")
        print(f"PTY server '{self.tag}' is still running.\n")

    def cleanup_lock(self):
        """Remove client lock file and release lock"""
        try:
            # Release the file lock
            if hasattr(self, 'lock_fd') and self.lock_fd:
                fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_UN)
                self.lock_fd.close()
            # Remove the lock file
            if self.lock_file.exists():
                self.lock_file.unlink()
        except:
            pass

    def _signal_handler(self, signum, frame):
        """Handle termination signals"""
        print("\n\nExiting...")
        self.cleanup_lock()
        sys.exit(0)

def main():
    if len(sys.argv) < 2:
        print("Usage: claudeq-client-pty <tag>")
        print()
        print("Example:")
        print("  Tab 1: cq my-feature")
        print("  Tab 2: cq my-feature")
        print()
        sys.exit(1)

    tag = sys.argv[1]

    # Validate tag doesn't start with "-"
    if tag.startswith('-'):
        print("Error: Tag cannot start with '-'")
        print("Usage: claudeq-client-pty <tag>")
        sys.exit(1)

    client = ClaudePTYClient(tag)
    client.run()

if __name__ == '__main__':
    main()
