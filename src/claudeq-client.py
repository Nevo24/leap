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
from collections import deque

QUEUE_DIR = os.path.expanduser("~/.claude-queues")

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

            for line in last_20_lines:
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

            if self.debug:
                print(f"\n[DEBUG] ✓ No busy indicators found - checking for prompt...\n", end='', flush=True)

            # PRIORITY 3: Check for ready prompt (❯) as confirmation
            # Search from the END to find the most recent prompt
            for i in range(len(last_20_lines) - 1, -1, -1):
                line = last_20_lines[i]
                stripped = line.strip()

                if stripped.startswith('❯'):
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
            # Send message literally (without interpreting special keys)
            subprocess.run([
                'tmux', 'send-keys', '-t', self.session_name,
                '-l', message
            ])

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
                print(f"   See response in Tab 1\n")

                if self.message_queue:
                    prompt = f"[Queue:{len(self.message_queue)}] You: "
                else:
                    prompt = "You: "
                print(prompt, end='', flush=True)

            except Exception as e:
                if self.debug:
                    print(f"\n[DEBUG] Error: {e}\n", end='', flush=True)
                pass  # Silently continue on errors

    def queue_add(self, message):
        """Add to queue"""
        self.message_queue.append(message)
        self.save_queue()
        print(f"📝 Queued: {message} ({len(self.message_queue)} total)\n")

    def queue_send(self):
        """Send next queued"""
        if not self.message_queue:
            print("✓ Queue is empty\n")
            return

        msg = self.message_queue.popleft()
        self.save_queue()
        remaining = len(self.message_queue)

        print(f"📤 Sending: {msg}")
        if remaining > 0:
            print(f"   ({remaining} remaining)")

        self.send_to_claude(msg)
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
            print(f"  → {msg}")
            self.send_to_claude(msg)
            import time
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
        print("  Type message        → Send to Claude")
        print("  q:<message>         → Queue for later (auto-sends when ready)")
        print("  :s or :send         → Send next queued")
        print("  :sa or :sendall     → Send all queued")
        print("  :l or :list         → Show queue")
        print("  :clear              → Clear queue")
        print("  :quit or Ctrl+D     → Exit client")
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
                    prompt = f"[Queue:{len(self.message_queue)}] You: "
                else:
                    prompt = "You: "

                try:
                    line = input(prompt).strip()
                except EOFError:
                    break

                if not line:
                    continue

                # Handle commands
                if line.startswith('q:'):
                    msg = line[2:].strip()
                    if msg:
                        self.queue_add(msg)
                    continue

                if line in [':s', ':send']:
                    self.queue_send()
                    continue

                if line in [':sa', ':sendall']:
                    self.queue_sendall()
                    continue

                if line in [':l', ':list']:
                    self.queue_list()
                    continue

                if line == ':clear':
                    self.queue_clear()
                    continue

                if line in [':quit', ':exit', ':q']:
                    break

                # Regular message - send immediately
                self.send_to_claude(line)
                print()

        except KeyboardInterrupt:
            print("\n\nExiting...")

        finally:
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
        print("  Tab 1: claude backend")
        print("  Tab 2: claude_client backend")
        print("  Tab 2: claude_client backend --debug  (with debug output)")
        print()
        sys.exit(1)

    client = ClaudeClient(tag, debug=debug)
    client.run()

if __name__ == '__main__':
    main()
