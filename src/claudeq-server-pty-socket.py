#!/usr/bin/env python3
"""
ClaudeQ PTY Server with Socket - Works in IntelliJ with scrolling!
Receives messages from client via Unix socket and sends to Claude.
"""
import sys
import os
import shutil
import pexpect
import threading
import time
import signal
import socket
import json
import subprocess
from collections import deque
from pathlib import Path

QUEUE_DIR = Path.home() / ".claude-queues"
SOCKET_DIR = Path.home() / ".claude-sockets"

class ClaudePTYServer:
    def __init__(self, tag):
        self.tag = tag
        self.queue_file = QUEUE_DIR / f"{tag}.queue"
        self.socket_path = SOCKET_DIR / f"{tag}.sock"
        self.message_queue = deque()
        self.claude_process = None
        self.running = True
        self.server_socket = None
        self.last_sent_message = None  # Track last auto-sent message

        # Ensure directories exist
        QUEUE_DIR.mkdir(exist_ok=True)
        SOCKET_DIR.mkdir(exist_ok=True)

        # Remove stale socket file if it exists
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except:
                pass

        # Load existing queue
        self.load_queue()

    def load_queue(self):
        """Load queue from file"""
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

    def print_banner(self):
        """Print startup banner"""
        print("""
   _____ _                 _       ___
  / ____| |               | |     / _ \\
 | |    | | __ _ _   _  __| | ___| | | |
 | |    | |/ _` | | | |/ _` |/ _ \\ | | |
 | |____| | (_| | |_| | (_| |  __/ |_| |
  \\_____|_|\\__,_|\\__,_|\\__,_|\\___|\\___\\
""")
        print("="*70)
        print(f"  PTY SERVER - Session: {self.tag}")
        print("="*70)
        print("  All responses will appear HERE in this window.")
        print("")
        print("  To send messages from another tab, run:")
        print(f"    cq {self.tag}")
        print("")
        print("  ✅ Native scrolling in IntelliJ")
        print("  ✅ Full terminal width")
        print("  ✅ No tmux needed!")
        print("")
        print("  💡 JetBrains Users: For automatic tab titles, enable:")
        print("     Settings > Advanced Settings > search 'term' >")
        print("     check ☑️  'Show application title' under Terminal")
        print("")
        print("  Ctrl+C to exit")
        print("="*70)
        print()

        if self.message_queue:
            print(f"📝 Queue has {len(self.message_queue)} messages\n")

    def spawn_claude(self):
        """Spawn Claude CLI with pexpect"""
        # Get terminal size
        cols, rows = shutil.get_terminal_size(fallback=(80, 24))
        print(f"📏 Terminal: {cols}x{rows}\n")

        # Find Claude CLI
        claude_path = None
        for path_dir in os.environ.get('PATH', '').split(':'):
            candidate = os.path.join(path_dir, 'claude')
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                claude_path = candidate
                break

        if not claude_path:
            print("Error: 'claude' command not found")
            sys.exit(1)

        # Spawn Claude with standard settings
        self.claude_process = pexpect.spawn(
            claude_path,
            ['--dangerously-skip-permissions'],
            dimensions=(rows, cols)
        )

    def socket_server(self):
        """Run Unix socket server for clients"""
        # Remove old socket if exists
        if self.socket_path.exists():
            self.socket_path.unlink()

        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(str(self.socket_path))
        self.server_socket.listen(5)
        self.server_socket.settimeout(1.0)

        while self.running:
            try:
                conn, _ = self.server_socket.accept()
                threading.Thread(target=self.handle_client, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except:
                break

    def handle_client(self, conn):
        """Handle client connection"""
        response = {'status': 'error', 'message': 'Unknown error'}
        try:
            data = conn.recv(4096).decode('utf-8')
            msg = json.loads(data)

            if msg['type'] == 'queue':
                self.message_queue.append(msg['message'])
                self.save_queue()
                response = {'status': 'queued', 'queue_size': len(self.message_queue)}
            elif msg['type'] == 'direct':
                # Send directly to Claude
                message = msg['message']
                self.last_sent_message = message
                self.claude_process.send(message)

                # If message starts with @, it's an attachment - give Claude time to recognize it
                if message.startswith('@'):
                    time.sleep(0.5)

                self.claude_process.send('\r')
                response = {'status': 'sent'}
            elif msg['type'] == 'status':
                response = {
                    'queue_size': len(self.message_queue),
                    'ready': True
                }
            elif msg['type'] == 'force_send':
                # Force-send next message from queue
                if self.message_queue:
                    message = self.message_queue.popleft()
                    self.save_queue()
                    self.last_sent_message = message

                    # Print notification
                    remaining = len(self.message_queue)
                    print(f"\n⚡ Force-sent message from queue ({remaining} remaining)\n", flush=True)

                    # Send to Claude
                    self.claude_process.send(message)
                    if message.startswith('@'):
                        time.sleep(0.5)
                    self.claude_process.send('\r')

                    response = {
                        'status': 'sent',
                        'message': message,
                        'queue_size': len(self.message_queue)
                    }
                else:
                    response = {'status': 'empty', 'queue_size': 0}
            else:
                response = {'status': 'error', 'message': f"Unknown message type: {msg.get('type', 'none')}"}

        except Exception as e:
            response = {'status': 'error', 'message': str(e)}
            print(f"Error handling client: {e}", file=sys.stderr, flush=True)

        try:
            conn.send(json.dumps(response).encode('utf-8'))
        except Exception as e:
            print(f"Error sending response: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()

    def is_claude_executing_tools(self):
        """Check if Claude process has active child processes (tools running)"""
        try:
            if not self.claude_process or not self.claude_process.isalive():
                return False

            # Get PID of the Claude process
            claude_pid = str(self.claude_process.pid)

            # Check for children (tools being run)
            child_cmd = ['pgrep', '-P', claude_pid]
            result = subprocess.run(child_cmd, capture_output=True)
            children = result.stdout.decode().strip().split()

            # Filter out empty strings
            children = [c for c in children if c]

            return len(children) > 0
        except:
            return False

    def title_keeper(self):
        """Background thread to keep terminal title set"""
        while self.running:
            try:
                sys.stdout.write(f"\033]0;cq-server {self.tag}\007")
                sys.stdout.flush()
            except:
                pass
            time.sleep(2)  # Reset title every 2 seconds

    def auto_sender(self):
        """Background thread to auto-send from queue"""
        while self.running:
            time.sleep(0.5)

            if not self.message_queue:
                continue

            # Check if Claude is busy executing tools
            if self.is_claude_executing_tools():
                continue

            # Send next message
            msg = self.message_queue.popleft()
            self.save_queue()

            try:
                self.last_sent_message = msg
                # Print notification before sending (to stdout for visibility)
                remaining = len(self.message_queue)
                print(f"\n🤖 Auto-sent message from queue ({remaining} remaining)\n", flush=True)
                self.claude_process.send(msg)

                # If message starts with @, it's an attachment - give Claude time to recognize it
                if msg.startswith('@'):
                    time.sleep(0.5)

                self.claude_process.send('\r')
            except:
                # Re-queue if failed
                self.message_queue.appendleft(msg)
                self.save_queue()

            # Small delay after sending to let Claude start processing
            time.sleep(1)

    def handle_resize(self, sig, frame):
        """Handle terminal resize"""
        try:
            cols, rows = shutil.get_terminal_size(fallback=(80, 24))
            self.claude_process.setwinsize(rows, cols)
        except:
            pass

    def run(self):
        """Main run loop"""
        # Set terminal tab name
        sys.stdout.write(f"\033]0;cq-server {self.tag}\007")
        sys.stdout.flush()

        self.print_banner()
        self.spawn_claude()

        # Start socket server
        threading.Thread(target=self.socket_server, daemon=True).start()

        # Start auto-sender
        threading.Thread(target=self.auto_sender, daemon=True).start()

        # Start title keeper
        threading.Thread(target=self.title_keeper, daemon=True).start()

        # Handle resize signal
        signal.signal(signal.SIGWINCH, self.handle_resize)

        # Set terminal title again (Claude CLI may have changed it)
        sys.stdout.write(f"\033]0;cq-server {self.tag}\007")
        sys.stdout.flush()

        # Interact with Claude
        try:
            self.claude_process.interact()
        except (KeyboardInterrupt, SystemExit):
            pass
        except Exception as e:
            print(f"\nError in interact: {e}", file=sys.stderr)
        finally:
            # Always cleanup, no matter how we exit
            self.cleanup()

    def cleanup(self):
        """Cleanup resources on exit"""
        print("\n🧹 Cleaning up...", file=sys.stderr, flush=True)

        self.running = False

        # Close server socket
        if self.server_socket:
            try:
                self.server_socket.close()
                print("  ✓ Closed server socket", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"  ✗ Error closing socket: {e}", file=sys.stderr, flush=True)

        # Always remove socket file, even if there are errors
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
                print(f"  ✓ Removed socket file: {self.socket_path}", file=sys.stderr, flush=True)
            else:
                print(f"  ℹ Socket file already gone: {self.socket_path}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"  ✗ Could not remove socket file: {e}", file=sys.stderr, flush=True)

        # Terminate Claude process
        if self.claude_process and self.claude_process.isalive():
            try:
                self.claude_process.terminate(force=True)
                print("  ✓ Terminated Claude process", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"  ✗ Error terminating Claude: {e}", file=sys.stderr, flush=True)

        print("\n\nGoodbye!")
        if self.message_queue:
            print(f"📝 Queue has {len(self.message_queue)} messages remaining")

def main():
    if len(sys.argv) < 2:
        print("Usage: claudeq-server-pty-socket <tag>")
        sys.exit(1)

    tag = sys.argv[1]

    # Validate tag doesn't start with "-"
    if tag.startswith('-'):
        print("Error: Tag cannot start with '-'")
        print("Usage: claudeq-server-pty-socket <tag>")
        sys.exit(1)

    server = ClaudePTYServer(tag)
    server.run()

if __name__ == "__main__":
    main()
