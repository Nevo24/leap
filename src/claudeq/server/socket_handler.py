"""
Socket handling for ClaudeQ server.

Manages Unix socket server for client connections.
"""

import json
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Callable


class SocketHandler:
    """Handles Unix socket server for client connections."""

    def __init__(
        self,
        socket_path: Path,
        message_handler: Callable[[dict[str, Any]], dict[str, Any]]
    ):
        """
        Initialize socket handler.

        Args:
            socket_path: Path to the Unix socket file.
            message_handler: Callback function to handle incoming messages.
        """
        self.socket_path = socket_path
        self.message_handler = message_handler
        self.server_socket: Optional[socket.socket] = None
        self.running = True
        self._ready_event = threading.Event()

    def start(self) -> None:
        """Start the socket server in the background."""
        threading.Thread(target=self._run_server, daemon=True).start()

    def _run_server(self) -> None:
        """Run the Unix socket server."""
        # Remove old socket if exists
        if self.socket_path.exists():
            self.socket_path.unlink()

        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(str(self.socket_path))
        self.server_socket.listen(5)
        self.server_socket.settimeout(1.0)
        self._ready_event.set()

        while self.running:
            try:
                conn, _ = self.server_socket.accept()
                threading.Thread(
                    target=self._handle_client,
                    args=(conn,),
                    daemon=True
                ).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_client(self, conn: socket.socket) -> None:
        """
        Handle a client connection.

        Args:
            conn: Client socket connection.
        """
        response: dict[str, Any] = {'status': 'error', 'message': 'Unknown error'}
        try:
            conn.settimeout(5.0)
            chunks = []
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                # Try to parse — if valid JSON, we have the full message
                try:
                    json.loads(b''.join(chunks))
                    break
                except json.JSONDecodeError:
                    continue
            data = b''.join(chunks).decode('utf-8')

            # Check if data is empty (client disconnected)
            if not data or not data.strip():
                return

            msg = json.loads(data)
            response = self.message_handler(msg)

        except json.JSONDecodeError as e:
            response = {'status': 'error', 'message': 'Invalid JSON'}
            print(f"Error: Received invalid JSON from client: {e}", file=sys.stderr, flush=True)
        except Exception as e:
            response = {'status': 'error', 'message': str(e)}
            print(f"Error handling client: {e}", file=sys.stderr, flush=True)

        try:
            conn.sendall(json.dumps(response).encode('utf-8'))
        except BrokenPipeError:
            # Client disconnected - normal, suppress error
            pass
        except Exception as e:
            print(f"Error sending response: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Wait until the socket is bound and listening.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            True if the socket is ready, False if timed out.
        """
        return self._ready_event.wait(timeout)

    def stop(self) -> None:
        """Stop the socket server."""
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except OSError:
                pass

    def cleanup(self) -> None:
        """Clean up socket file."""
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except OSError:
            pass
