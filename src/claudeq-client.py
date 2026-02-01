#!/usr/bin/env python3
"""
ClaudeQ Client - Send messages to a tagged Claude session
Usage: claude_client <tag>
"""

import sys
import os
import subprocess
import threading
import time
import readline
import tempfile
import base64
from collections import deque

QUEUE_DIR = os.path.expanduser("~/.claude-queues")

def is_image_file(path):
    """Check if path points to an image file"""
    if not os.path.exists(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    return ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp']

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

class ClaudeClient:
    def __init__(self, tag, debug=False):
        self.tag = tag
        self.session_name = f"claude-{tag}"
        self.debug = debug

        # Queue file
        os.makedirs(QUEUE_DIR, exist_ok=True)
        self.queue_file = os.path.join(QUEUE_DIR, f"{tag}.queue")
        self.message_queue = deque()
        self.load_queue()

        # History file for arrow key navigation
        self.history_file = os.path.join(QUEUE_DIR, f"{tag}.history")
        self.load_history()

        # Auto-queue processing
        self.auto_process = True
        self.processing_lock = threading.Lock()
        self.is_sending = False
        self.auto_thread = None

        # Image handling
        self.pending_image_path = None

        # Enable bracketed paste mode
        sys.stdout.write('\033[?2004h')
        sys.stdout.flush()

    def load_queue(self):
        """Load queue from file"""
        if os.path.exists(self.queue_file):
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
        """Load command history from file"""
        if os.path.exists(self.history_file):
            try:
                readline.read_history_file(self.history_file)
            except:
                pass  # Ignore errors loading history


    def save_history(self):
        """Save command history to file"""
        try:
            readline.write_history_file(self.history_file)
        except:
            pass  # Ignore errors saving history

    def _custom_input(self, prompt):
        """Custom input - just use regular input for now"""
        return input(prompt)

    def check_session_exists(self):
        """Check if tmux session exists"""
        result = subprocess.run(
            ['tmux', 'has-session', '-t', self.session_name],
            capture_output=True
        )
        return result.returncode == 0

    def is_claude_executing_tools(self):
        """Check if the claude process in tmux has active child processes (tools)"""
        try:
            # Get the PID of the main process in the tmux pane
            pid_cmd = ['tmux', 'display-message', '-t', self.session_name, '-p', '#{pane_pid}']
            pane_pid = subprocess.check_output(pid_cmd).decode().strip()

            # Check for children (tools being run)
            child_cmd = ['pgrep', '-P', pane_pid]
            children = subprocess.run(child_cmd, capture_output=True).stdout.decode().split()

            if self.debug and len(children) > 0:
                print(f"\n[DEBUG] Found {len(children)} child processes: {children}\n", end='', flush=True)

            return len(children) > 0
        except:
            return False

    def is_claude_ready(self):
        """Check if Claude is ready for next message using spinner detection"""
        if not self.check_session_exists():
            return False

        try:
            # Capture the entire visible pane (no scroll offset)
            result = subprocess.run(
                ['tmux', 'capture-pane', '-t', self.session_name, '-p'],
                capture_output=True,
                text=True,
                timeout=1
            )

            if result.returncode != 0:
                return False

            output = result.stdout

            if self.debug:
                # Show what we captured (first time only to avoid spam)
                if not hasattr(self, '_debug_shown_once'):
                    self._debug_shown_once = True
                    print(f"\n[DEBUG] Full pane capture (last 500 chars):\n{repr(output[-500:])}\n", end='', flush=True)

            # Split into lines
            lines = output.split('\n')

            # Get non-empty lines from the end
            non_empty_lines = [l for l in lines if l.strip()]

            if not non_empty_lines:
                return False

            # Look at more lines to find spinners or prompts
            last_20_lines = non_empty_lines[-20:]

            if self.debug and not hasattr(self, '_debug_shown_lines'):
                self._debug_shown_lines = True
                print(f"\n[DEBUG] Last 20 non-empty lines:\n", end='', flush=True)
                for i, line in enumerate(last_20_lines):
                    print(f"  {i}: {repr(line[:80])}\n", end='', flush=True)
                print(flush=True)

            # PRIORITY 1: Check if Claude is executing tools (subprocesses)
            if self.is_claude_executing_tools():
                if self.debug:
                    print(f"\n[DEBUG] ✗ Claude is executing tools/subprocesses - BUSY\n", end='', flush=True)
                return False

            # PRIORITY 2: Check for busy indicators (spinners)
            # Braille unicode spinners that Claude Code uses while processing
            busy_spinners = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
            # Other busy symbols (✽ ✢ ✻)
            busy_symbols = ['✽', '✢', '✻']
            # Text patterns indicating busy state
            busy_patterns = ['(thinking)', '(writing)', '(reading)']

            import re
            # Pattern for Claude Code spinner verbs: word ending in "ing" followed by "..."
            # e.g., "Zesting...", "Thinking...", "Processing...", etc.
            spinner_verb_pattern = re.compile(r'\w+ing\.\.\.', re.IGNORECASE)

            for line in last_20_lines:
                stripped = line.strip()

                # Check for Braille spinners
                for spinner in busy_spinners:
                    if spinner in line:
                        if self.debug:
                            print(f"\n[DEBUG] ✗ Found spinner {repr(spinner)} - Claude is BUSY: {repr(line[:80])}\n", end='', flush=True)
                        return False

                # Check for other busy symbols
                for symbol in busy_symbols:
                    if symbol in line:
                        if self.debug:
                            print(f"\n[DEBUG] ✗ Found symbol {symbol} - Claude is BUSY: {repr(line[:80])}\n", end='', flush=True)
                        return False

                # Check for busy text patterns
                for pattern in busy_patterns:
                    if pattern in stripped.lower():
                        if self.debug:
                            print(f"\n[DEBUG] ✗ Found busy pattern {repr(pattern)} - Claude is BUSY: {repr(line[:80])}\n", end='', flush=True)
                        return False

                # Check for spinner verb pattern (e.g., "Zesting...", "Thinking...", etc.)
                if spinner_verb_pattern.search(stripped):
                    if self.debug:
                        print(f"\n[DEBUG] ✗ Found spinner verb pattern - Claude is BUSY: {repr(line[:80])}\n", end='', flush=True)
                    return False

            if self.debug:
                print(f"\n[DEBUG] ✓ No busy indicators found - checking for prompt...\n", end='', flush=True)

            # PRIORITY 3: Check for ready prompt (❯ or >) as confirmation
            # Search from the END to find the most recent prompt
            for i in range(len(last_20_lines) - 1, -1, -1):
                line = last_20_lines[i]
                stripped = line.strip()

                # Check for both ❯ (triangle) and > (greater-than) prompts
                if stripped.startswith('❯') or stripped.startswith('>'):
                    # Remove the prompt character and any whitespace
                    after_prompt = stripped[1:].strip().strip('\xa0').strip()

                    # Ready if prompt is empty or has suggestion text
                    if len(after_prompt) == 0 or len(after_prompt) < 3:
                        if self.debug:
                            print(f"\n[DEBUG] ✓ Found empty prompt - Claude is READY: {repr(stripped[:50])}\n", end='', flush=True)
                        return True

                    # Check for suggestion prompts
                    suggestion_patterns = ['Try ', 'edit ', 'refactor ', '? for', 'Type ']
                    if any(after_prompt.startswith(pat) for pat in suggestion_patterns):
                        if self.debug:
                            print(f"\n[DEBUG] ✓ Found suggestion prompt - Claude is READY: {repr(stripped[:50])}\n", end='', flush=True)
                        return True

            # No specific prompt found, but no busy indicators either
            # This handles permission screens, welcome messages, and other ready states
            # that don't show the standard ❯ prompt
            if self.debug:
                print(f"\n[DEBUG] ✓ No prompt found but no busy indicators - assuming READY\n", end='', flush=True)
            return True

        except Exception as e:
            if self.debug:
                print(f"\n[DEBUG] Exception in is_claude_ready: {e}\n", end='', flush=True)
            return False

    def send_to_claude(self, message, auto=False):
        """Send message to Claude via tmux"""
        if not self.check_session_exists():
            if not auto:
                print(f"Error: Claude session '{self.tag}' is not running")
                print(f"Start it with: claude {self.tag}")
            return False

        with self.processing_lock:
            self.is_sending = True

        try:
            # Check if we have a pending image to send
            if self.pending_image_path:
                image_path = self.pending_image_path
                self.pending_image_path = None  # Clear pending image

                if not auto:
                    print(f"📎 Sending image: {os.path.basename(image_path)}")

                if image_path and os.path.exists(image_path):
                    # Send the image file path to Claude
                    # Claude CLI should accept file paths as attachments
                    subprocess.run([
                        'tmux', 'send-keys', '-t', self.session_name,
                        '-l', f'@{image_path}'
                    ])

                    # Add space after image path
                    subprocess.run([
                        'tmux', 'send-keys', '-t', self.session_name,
                        'Space'
                    ])

                    if not auto:
                        print(f"   📸 Image attached")

                    # Small delay for image to be recognized
                    time.sleep(0.3)

            # Send message literally (without interpreting special keys)
            if message:
                subprocess.run([
                    'tmux', 'send-keys', '-t', self.session_name,
                    '-l', message
                ])

            # Delay before Enter to let Claude process the image
            time.sleep(0.2)

            # Then send Enter separately
            subprocess.run([
                'tmux', 'send-keys', '-t', self.session_name,
                'Enter'
            ])

            if not auto:
                print(f"✓ Sent to Claude '{self.tag}'")
                print(f"   See response in Tab 1")

            # Mark as not sending after a brief delay (let Claude start processing)
            time.sleep(0.3)
            return True

        finally:
            with self.processing_lock:
                self.is_sending = False

    def auto_process_queue(self):
        """Background thread that auto-sends queued messages when Claude is ready"""
        while self.auto_process:
            try:
                time.sleep(0.5)

                # Skip if currently sending
                with self.processing_lock:
                    if self.is_sending:
                        if self.debug:
                            print(f"\n[DEBUG] Skipping - currently sending\n", end='', flush=True)
                        continue

                # Check if there's something in queue
                if not self.message_queue:
                    continue

                # Check if Claude is ready
                claude_ready = self.is_claude_ready()

                if self.debug and self.message_queue:
                    status = "READY ✓" if claude_ready else "BUSY ✗"
                    print(f"\n[DEBUG] Claude status: {status}, Queue: {len(self.message_queue)}\n", end='', flush=True)

                if not claude_ready:
                    continue

                # Send next message from queue
                msg = self.message_queue.popleft()
                self.save_queue()

                remaining = len(self.message_queue)
                print(f"\n🤖 Auto-sending from queue: {msg}")
                if remaining > 0:
                    print(f"   ({remaining} remaining in queue)")

                self.send_to_claude(msg, auto=True)
                print(f"   See response in Tab 1\n", flush=True)

            except Exception as e:
                if self.debug:
                    print(f"\n[DEBUG] Error: {e}\n", end='', flush=True)
                pass  # Silently continue on errors

    def queue_add(self, message):
        """Add to queue"""
        # If there's a pending image, include it with the message
        if self.pending_image_path:
            # Move image to a permanent location in queue dir
            import shutil
            image_ext = os.path.splitext(self.pending_image_path)[1]
            perm_path = os.path.join(QUEUE_DIR, f"img_{int(time.time())}_{len(self.message_queue)}{image_ext}")
            shutil.copy(self.pending_image_path, perm_path)

            # Store with image marker
            queued_msg = f"@IMG:{perm_path}|{message}"
            self.message_queue.append(queued_msg)

            # Clean up temp image
            try:
                if self.pending_image_path.startswith('/tmp/'):
                    os.unlink(self.pending_image_path)
            except:
                pass
            self.pending_image_path = None

            print(f"📝 Queued with image: {message} ({len(self.message_queue)} total)\n")
        else:
            self.message_queue.append(message)
            print(f"📝 Queued: {message} ({len(self.message_queue)} total)\n")

        self.save_queue()

    def queue_send(self):
        """Send next queued"""
        if not self.message_queue:
            print("✓ Queue is empty\n")
            return

        msg = self.message_queue.popleft()
        self.save_queue()
        remaining = len(self.message_queue)

        # Check if message has an image
        if msg.startswith('@IMG:'):
            parts = msg.split('|', 1)
            if len(parts) == 2:
                image_path = parts[0][5:]  # Remove @IMG: prefix
                actual_msg = parts[1]
                self.pending_image_path = image_path
                print(f"📤 Sending with image: {actual_msg}")
            else:
                actual_msg = msg
                print(f"📤 Sending: {actual_msg}")
        else:
            actual_msg = msg
            print(f"📤 Sending: {actual_msg}")

        if remaining > 0:
            print(f"   ({remaining} remaining)")

        self.send_to_claude(actual_msg)
        print()

    def queue_sendall(self):
        """Send all queued"""
        if not self.message_queue:
            print("✓ Queue is empty\n")
            return

        count = len(self.message_queue)
        print(f"📤 Sending {count} messages...\n")

        while self.message_queue:
            msg = self.message_queue.popleft()

            # Check if message has an image
            if msg.startswith('@IMG:'):
                parts = msg.split('|', 1)
                if len(parts) == 2:
                    image_path = parts[0][5:]  # Remove @IMG: prefix
                    actual_msg = parts[1]
                    self.pending_image_path = image_path
                    print(f"  → [📸] {actual_msg}")
                else:
                    actual_msg = msg
                    print(f"  → {actual_msg}")
            else:
                actual_msg = msg
                print(f"  → {actual_msg}")

            self.send_to_claude(actual_msg)
            time.sleep(0.5)

        self.save_queue()
        print()

    def queue_list(self):
        """List queue"""
        if not self.message_queue:
            print("✓ Queue is empty\n")
        else:
            print(f"📝 Queue ({len(self.message_queue)} messages):")
            for i, msg in enumerate(self.message_queue, 1):
                # Check if message has an image
                if msg.startswith('@IMG:'):
                    parts = msg.split('|', 1)
                    if len(parts) == 2:
                        actual_msg = parts[1]
                        print(f"   {i}. [📸] {actual_msg}")
                    else:
                        print(f"   {i}. {msg}")
                else:
                    print(f"   {i}. {msg}")
            print()

    def queue_clear(self):
        """Clear queue"""
        self.message_queue.clear()
        self.save_queue()
        print("✓ Queue cleared\n")

    def print_banner(self):
        """Print banner"""
        # Set terminal tab title
        print("\033]0;claude-client {}\007".format(self.tag), end='', flush=True)

        # Print ASCII art
        print("""
   _____ _                 _       ___
  / ____| |               | |     / _ \\
 | |    | | __ _ _   _  __| | ___| | | |
 | |    | |/ _` | | | |/ _` |/ _ \\ | | |
 | |____| | (_| | |_| | (_| |  __/ |_| |
  \\_____|_|\\__,_|\\__,_|\\__,_|\\___|\\___\\Q
""")
        print("="*70)
        print(f"  CLIENT - Session: {self.tag}")
        if self.debug:
            print("  [DEBUG MODE ENABLED]")
        print("="*70)
        print(f"  Sending messages to ClaudeQ session '{self.tag}'")
        print(f"  Watch responses in server tab where 'claudeq {self.tag}' started")
        print()
        print("  💬 Type message                        → Queue message (auto-sends when ready)")
        print("  🖼️ :ip <msg> or :imagepaste <msg>       → Queue with image from clipboard")
        print("  ⚡ :d <msg> or :direct <msg>           → Send directly (bypass queue)")
        print("  📤 :s or :send                         → Send next queued")
        print("  📨 :sa or :sendall                     → Send all queued")
        print("  📋 :l or :list                         → Show queue")
        print("  🗑️ :c or :clear                        → Clear queue")
        print("  👋 :x or :quit (Ctrl+D)                → Exit client")
        print()
        print("  🤖 Auto-queue: ENABLED (sends when Claude is ready)")
        if self.debug:
            print("  🐛 Debug: Shows Claude readiness checks every 0.5s")
        print("="*70)
        print()

    def run(self):
        """Run client"""
        if not self.check_session_exists():
            print(f"Error: Claude session '{self.tag}' is not running")
            print()
            print(f"Start it first:")
            print(f"  Tab 1: claude {self.tag}")
            print(f"  Tab 2: claude_client {self.tag}")
            print()
            sys.exit(1)

        self.print_banner()

        # Start auto-processing thread
        self.auto_thread = threading.Thread(target=self.auto_process_queue, daemon=True)
        self.auto_thread.start()

        print("Ready! Type your messages:\n")

        if self.message_queue:
            print(f"📝 Queue has {len(self.message_queue)} messages - will auto-send when Claude is ready\n")

        try:
            while True:
                # Prompt
                if self.message_queue:
                    prompt_prefix = f"[Queue:{len(self.message_queue)}]"
                else:
                    prompt_prefix = ""

                if self.pending_image_path:
                    prompt = f"{prompt_prefix}[📸] You: " if prompt_prefix else "[📸] You: "
                else:
                    prompt = f"{prompt_prefix} You: " if prompt_prefix else "You: "

                try:
                    line = self._custom_input(prompt).strip()
                except EOFError:
                    break

                if not line and not self.pending_image_path:
                    continue

                # Make commands case-insensitive
                line_lower = line.lower()

                # Check if line contains an image file path
                if line and not self.pending_image_path:
                    # Check if entire line is a path
                    if is_image_file(line):
                        self.pending_image_path = line
                        print(f"📎 Image attached: {os.path.basename(line)}")
                        print("[📸] You: ", end='', flush=True)
                        # Get the actual message
                        try:
                            message = input().strip()
                            line = message
                        except EOFError:
                            line = ""
                    else:
                        # Check if line contains a path (might have text before/after)
                        words = line.split()
                        for word in words:
                            if is_image_file(word):
                                self.pending_image_path = word
                                # Remove the path from the message
                                line = line.replace(word, '').strip()
                                print(f"📎 Image attached: {os.path.basename(word)}")
                                break

                # Handle :ip or :imagepaste command (queues by default)
                if line_lower.startswith(':ip') or line_lower.startswith(':imagepaste'):
                    # Check if there's a message after the command
                    if line_lower.startswith(':ip '):
                        msg = line[4:].strip()  # Get message after ':ip '
                    elif line_lower.startswith(':imagepaste '):
                        msg = line[12:].strip()  # Get message after ':imagepaste '
                    elif line_lower in [':ip', ':imagepaste']:
                        msg = None  # No message, will prompt for it
                    else:
                        # Handle cases like ':ipSomething' which shouldn't match
                        msg = None
                        if line_lower not in [':ip', ':imagepaste']:
                            continue  # Not a valid command

                    if check_clipboard_has_image():
                        image_path = save_clipboard_image()
                        if image_path:
                            self.pending_image_path = image_path
                            if msg:
                                # Queue immediately with the message
                                self.queue_add(msg)
                            else:
                                # Wait for message on next prompt
                                print(f"🖼️  Image attached! Type message or press Enter to queue")
                        else:
                            print("✗ Failed to save image from clipboard")
                    else:
                        print("✗ No image in clipboard")
                    continue

                # Handle commands (note: most commands will discard pending image except :ip and :d)
                if line_lower.startswith(':') and line_lower not in [':ip', ':imagepaste'] and not (line_lower.startswith(':d ') or line_lower.startswith(':d:ip') or line_lower.startswith(':direct ')):
                    # Clean up pending image if using a command (except queue commands)
                    if self.pending_image_path:
                        try:
                            if self.pending_image_path.startswith('/tmp/'):
                                os.unlink(self.pending_image_path)
                        except:
                            pass
                        self.pending_image_path = None
                        print("📎 Pending image discarded")

                # Handle :d or :direct command (send directly, bypass queue)
                if line_lower.startswith(':d ') or line_lower.startswith(':d:ip') or line_lower.startswith(':direct '):
                    # Handle different formats
                    if line_lower.startswith(':d:ip'):
                        prefix_len = 2
                        rest = line[prefix_len:].strip()
                    elif line_lower.startswith(':d '):
                        prefix_len = 3
                        rest = line[prefix_len:].strip()
                    else:  # :direct
                        prefix_len = 8
                        rest = line[prefix_len:].strip()

                    rest_lower = rest.lower()

                    # Check if it starts with :ip or :imagepaste (with or without space)
                    if rest_lower.startswith(':ip') or rest_lower.startswith(':imagepaste'):
                        if rest_lower.startswith(':ip '):
                            img_prefix = 4
                        elif rest_lower.startswith(':ip'):
                            img_prefix = 3
                        elif rest_lower.startswith(':imagepaste '):
                            img_prefix = 12
                        else:
                            img_prefix = 11
                        msg = rest[img_prefix:].strip()

                        # Grab image from clipboard
                        if check_clipboard_has_image():
                            image_path = save_clipboard_image()
                            if image_path:
                                self.pending_image_path = image_path
                                print(f"🖼️ Image grabbed from clipboard!")
                            else:
                                print("✗ Failed to save image from clipboard")
                        else:
                            print("✗ No image in clipboard")

                        # Send directly with or without message
                        self.send_to_claude(msg if msg else "")
                        print()
                    else:
                        # Send directly without image
                        if rest:
                            self.send_to_claude(rest)
                            print()
                        else:
                            print("✗ No message provided")
                    continue

                if line_lower in [':s', ':send']:
                    self.queue_send()
                    continue

                if line_lower in [':sa', ':sendall']:
                    self.queue_sendall()
                    continue

                if line_lower in [':l', ':list']:
                    self.queue_list()
                    continue

                if line_lower in [':c', ':clear']:
                    self.queue_clear()
                    continue

                if line_lower in [':x', ':quit', ':exit']:
                    break

                # Don't send bare :ip or :imagepaste as messages (shouldn't reach here)
                if line_lower in [':ip', ':imagepaste']:
                    print("✗ Use :ip <msg> to queue with image, or :d :ip <msg> to send directly")
                    continue

                # Regular message - queue by default
                self.queue_add(line)

        except KeyboardInterrupt:
            print("\n\nExiting...")

        finally:
            # Disable bracketed paste mode
            sys.stdout.write('\033[?2004l')
            sys.stdout.flush()

            # Stop auto-processing
            self.auto_process = False
            if self.auto_thread:
                self.auto_thread.join(timeout=1)
            # Save command history
            self.save_history()

        print("\nGoodbye!")
        if self.message_queue:
            print(f"📝 Queue has {len(self.message_queue)} messages remaining")
        print(f"Claude session '{self.tag}' is still running in Tab 1.\n")

def main():
    debug = False
    tag = None

    # Parse arguments
    args = sys.argv[1:]
    for arg in args:
        if arg == '--debug':
            debug = True
        elif not arg.startswith('-'):
            tag = arg

    if not tag:
        print("Usage: claude_client <tag> [--debug]")
        print()
        print("Example:")
        print("  Tab 1: claude my-cool-feature")
        print("  Tab 2: claude_client my-cool-feature")
        print("  Tab 2: claude_client my-cool-feature --debug  (with debug output)")
        print()
        sys.exit(1)

    client = ClaudeClient(tag, debug=debug)
    client.run()

if __name__ == '__main__':
    main()
