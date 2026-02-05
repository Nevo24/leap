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
import atexit
from collections import deque
from pathlib import Path

QUEUE_DIR = Path.home() / ".claude-queues"
SOCKET_DIR = Path.home() / ".claude-sockets"

class ClaudePTYServer:
    def __init__(self, tag):
        self.tag = tag
        self.queue_file = QUEUE_DIR / f"{tag}.queue"
        self.socket_path = SOCKET_DIR / f"{tag}.sock"
        self.metadata_file = SOCKET_DIR / f"{tag}.meta"
        self.message_queue = deque()
        self.claude_process = None
        self.running = True
        self.server_socket = None
        self.last_sent_message = None  # Track last auto-sent message
        self.last_send_time = None  # Track when we last sent a message
        self.min_busy_duration = 3.0  # Minimum seconds to consider busy after sending
        self.pending_notifications = []  # Buffer for notifications
        self.notification_lock = threading.Lock()  # Protect notification buffer
        self.recently_sent = []  # Track recently auto-sent messages for client notifications
        self.recently_sent_lock = threading.Lock()  # Protect recently_sent list

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

        # Save metadata about terminal environment
        self.save_metadata()

        # Register cleanup to always run on exit
        atexit.register(self.cleanup)

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

    def detect_ide(self):
        """Detect which IDE/terminal this is running in"""
        # Check environment variables set by JetBrains IDEs
        terminal_emulator = os.environ.get('TERMINAL_EMULATOR', '')

        # Check for JetBrains IDE - traverse up process tree to find IDE
        if 'JetBrains' in terminal_emulator or 'jetbrains' in terminal_emulator.lower():
            try:
                # Walk up the process tree to find the IDE process
                current_pid = os.getpid()
                for _ in range(10):  # Check up to 10 levels up
                    result = subprocess.run(
                        ['ps', '-p', str(current_pid), '-o', 'ppid=,comm='],
                        capture_output=True,
                        text=True,
                        timeout=1
                    )
                    if result.returncode != 0:
                        break

                    output = result.stdout.strip().split(None, 1)
                    if len(output) < 2:
                        break

                    ppid = output[0]
                    process_name = output[1].lower()

                    # Check if this process is a JetBrains IDE
                    if 'pycharm' in process_name:
                        return 'PyCharm'
                    elif 'goland' in process_name:
                        return 'GoLand'
                    elif 'webstorm' in process_name:
                        return 'WebStorm'
                    elif 'phpstorm' in process_name:
                        return 'PhpStorm'
                    elif 'rubymine' in process_name:
                        return 'RubyMine'
                    elif 'clion' in process_name:
                        return 'CLion'
                    elif 'datagrip' in process_name:
                        return 'DataGrip'
                    elif 'idea' in process_name and 'pycharm' not in process_name:
                        return 'IntelliJ IDEA'

                    # Move to parent
                    current_pid = int(ppid)
            except:
                pass
            return 'JetBrains IDE'

        # Check for VS Code
        if 'vscode' in terminal_emulator.lower() or os.environ.get('TERM_PROGRAM') == 'vscode':
            return 'VS Code'

        # Check for iTerm2
        if os.environ.get('TERM_PROGRAM') == 'iTerm.app':
            return 'iTerm2'

        # Check for Terminal.app
        if os.environ.get('TERM_PROGRAM') == 'Apple_Terminal':
            return 'Terminal.app'

        # Default
        return 'Unknown'

    def save_metadata(self):
        """Save metadata about the session"""
        ide = self.detect_ide()
        terminal_title = f"cq-server {self.tag}"

        # Get current working directory (project path)
        project_path = os.getcwd()

        # Get git branch name
        branch_name = None
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                capture_output=True,
                text=True,
                timeout=1,
                cwd=project_path
            )
            if result.returncode == 0:
                branch_name = result.stdout.strip()
        except:
            pass

        metadata = {
            'ide': ide,
            'terminal_title': terminal_title,
            'tag': self.tag,
            'pid': os.getpid(),
            'project_path': project_path,
            'branch': branch_name
        }

        with open(self.metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)

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
        print("  💡 JetBrains Users - Enable CQ to name your tabs:")
        print("     1. Settings > Tools > Terminal > Engine: Classic")
        print("     2. Advanced Settings > Terminal > ☑️ 'Show application title'")
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

            # Check if data is empty (client disconnected or sent nothing)
            if not data or not data.strip():
                # Silent return for empty data - likely client disconnect
                return

            msg = json.loads(data)

            if msg['type'] == 'queue':
                self.message_queue.append(msg['message'])
                self.save_queue()
                response = {
                    'status': 'queued',
                    'queue_size': len(self.message_queue),
                    'queue_contents': list(self.message_queue)
                }
            elif msg['type'] == 'direct':
                # Send directly to Claude
                message = msg['message']
                self.last_sent_message = message
                self.last_send_time = time.time()
                self.claude_process.send(message)

                # If message starts with @, it's an attachment - give Claude time to recognize it
                if message.startswith('@'):
                    time.sleep(0.5)

                self.claude_process.send('\r')
                response = {'status': 'sent'}
            elif msg['type'] == 'status':
                claude_alive = self.claude_process and self.claude_process.isalive()
                with self.recently_sent_lock:
                    recently_sent_copy = list(self.recently_sent)
                response = {
                    'queue_size': len(self.message_queue),
                    'queue_contents': list(self.message_queue),  # Include actual queue
                    'recently_sent': recently_sent_copy,  # Include recently auto-sent messages
                    'ready': not self.is_claude_busy(),
                    'claude_running': claude_alive
                }
            elif msg['type'] == 'force_send':
                # Force-send next message from queue
                if self.message_queue:
                    message = self.message_queue.popleft()
                    self.save_queue()
                    self.last_sent_message = message
                    self.last_send_time = time.time()

                    # Queue notification (don't print directly to avoid terminal mess)
                    remaining = len(self.message_queue)
                    with self.notification_lock:
                        self.pending_notifications.append(f"⚡ Force-sent from queue ({remaining} remaining)")

                    # Send to Claude
                    self.claude_process.send(message)
                    if message.startswith('@'):
                        time.sleep(0.5)
                    self.claude_process.send('\r')

                    # Track for client notifications
                    with self.recently_sent_lock:
                        self.recently_sent.append(message)
                        # Keep only last 20 messages
                        if len(self.recently_sent) > 20:
                            self.recently_sent.pop(0)

                    response = {
                        'status': 'sent',
                        'message': message,
                        'queue_size': len(self.message_queue)
                    }
                else:
                    response = {'status': 'empty', 'queue_size': 0}
            else:
                response = {'status': 'error', 'message': f"Unknown message type: {msg.get('type', 'none')}"}

        except json.JSONDecodeError as e:
            response = {'status': 'error', 'message': 'Invalid JSON'}
            print(f"Error: Received invalid JSON from client: {e}", file=sys.stderr, flush=True)
        except Exception as e:
            response = {'status': 'error', 'message': str(e)}
            print(f"Error handling client: {e}", file=sys.stderr, flush=True)

        try:
            conn.send(json.dumps(response).encode('utf-8'))
        except BrokenPipeError:
            # Client disconnected - this is normal, suppress error
            pass
        except Exception as e:
            print(f"Error sending response: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()

    def is_claude_busy(self):
        """Check if Claude is busy (typing response or executing tools)"""
        try:
            if not self.claude_process or not self.claude_process.isalive():
                return False

            # Check if we recently sent a message (Claude is likely typing)
            if self.last_send_time:
                time_since_send = time.time() - self.last_send_time
                if time_since_send < self.min_busy_duration:
                    return True

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

    def is_claude_executing_tools(self):
        """Alias for backwards compatibility"""
        return self.is_claude_busy()

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
                self.last_send_time = time.time()
                # Queue notification (don't print directly to avoid terminal mess)
                remaining = len(self.message_queue)
                with self.notification_lock:
                    self.pending_notifications.append(f"🤖 Auto-sent from queue ({remaining} remaining)")
                self.claude_process.send(msg)

                # If message starts with @, it's an attachment - give Claude time to recognize it
                if msg.startswith('@'):
                    time.sleep(0.5)

                self.claude_process.send('\r')

                # Track for client notifications
                with self.recently_sent_lock:
                    self.recently_sent.append(msg)
                    # Keep only last 20 messages
                    if len(self.recently_sent) > 20:
                        self.recently_sent.pop(0)
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

        # Handle signals
        signal.signal(signal.SIGWINCH, self.handle_resize)
        signal.signal(signal.SIGTERM, lambda sig, frame: sys.exit(0))
        signal.signal(signal.SIGINT, lambda sig, frame: sys.exit(0))
        signal.signal(signal.SIGHUP, lambda sig, frame: sys.exit(0))  # Terminal closed

        # Set terminal title again (Claude CLI may have changed it)
        sys.stdout.write(f"\033]0;cq-server {self.tag}\007")
        sys.stdout.flush()

        # Interact with Claude using output filter to show notifications cleanly
        def output_filter(data):
            """Filter to inject notifications at safe points"""
            # Only inject after a complete line ending with newline
            with self.notification_lock:
                if self.pending_notifications and data.endswith(b'\n'):
                    # This is a complete line - safe to inject notification
                    notifications = '   '.join(self.pending_notifications)  # Join on same line
                    self.pending_notifications.clear()
                    # Inject notification AFTER the complete line, in yellow
                    return data + f"\033[33m🤖 {notifications}\033[0m\n".encode()
            return data

        try:
            self.claude_process.interact(output_filter=output_filter)
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

        # Remove metadata file
        try:
            if self.metadata_file.exists():
                self.metadata_file.unlink()
                print(f"  ✓ Removed metadata file", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"  ✗ Could not remove metadata file: {e}", file=sys.stderr, flush=True)

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
