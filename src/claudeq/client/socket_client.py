"""
Socket client for ClaudeQ.

Handles communication with the ClaudeQ server via Unix socket.
"""

import json
import socket
from pathlib import Path
from typing import Any, Callable, Optional


class SocketClient:
    """Client for communicating with ClaudeQ server via Unix socket."""

    def __init__(
        self,
        socket_path: Path,
        error_callback: Optional[Callable[[], bool]] = None
    ) -> None:
        """
        Initialize socket client.

        Args:
            socket_path: Path to the server's Unix socket.
            error_callback: Optional callback to check if errors should be printed.
                          Should return True to print, False to suppress.
        """
        self.socket_path = socket_path
        self.tag: str = socket_path.stem
        self._error_callback = error_callback

    def _should_print_error(self) -> bool:
        """Check if errors should be printed (respects rate limiting)."""
        if self._error_callback:
            return self._error_callback()
        return True

    def _send_request(
        self, data: dict[str, Any], silent: bool = False
    ) -> Optional[dict[str, Any]]:
        """
        Send a request to the server and return the parsed response.

        Args:
            data: Request payload dictionary.
            silent: If True, suppress error messages.

        Returns:
            Server response dictionary, or None on error.
        """
        client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client_socket.settimeout(5.0)
            client_socket.connect(str(self.socket_path))

            client_socket.sendall(json.dumps(data).encode('utf-8'))
            client_socket.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                chunk = client_socket.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            response = b''.join(chunks).decode('utf-8')

            return json.loads(response)
        except socket.error as e:
            if not silent and self._should_print_error():
                print(f"Error: Cannot connect to ClaudeQ server")
                print(f"  → Check if there's a terminal running: cq-server {self.tag}")
                print(f"  → Details: {e}")
            return None
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            if not silent and self._should_print_error():
                print(f"Error: Server sent malformed response")
                print(
                    f"  → The ClaudeQ server (tag: {self.tag}) may be in a bad state"
                )
                print(
                    f"  → Try restarting the server by closing and reopening:"
                    f" cq {self.tag}"
                )
                print(f"  → Details: {e}")
            return None
        except OSError as e:
            if not silent and self._should_print_error():
                print(f"Error communicating with server: {e}")
            return None
        finally:
            try:
                client_socket.close()
            except OSError:
                pass

    def send(
        self, msg_type: str, message: str = "", silent: bool = False
    ) -> Optional[dict[str, Any]]:
        """
        Send a typed message to the server and get response.

        Args:
            msg_type: Message type ('queue', 'direct', 'status', 'force_send').
            message: Message content.
            silent: If True, suppress error messages.

        Returns:
            Server response dictionary, or None on error.
        """
        return self._send_request(
            {'type': msg_type, 'message': message}, silent=silent
        )

    def is_server_running(self) -> bool:
        """
        Check if the server is running.

        Returns:
            True if server socket exists.
        """
        return self.socket_path.exists()

    def queue_message(self, message: str) -> Optional[dict[str, Any]]:
        """Queue a message for auto-sending."""
        return self.send('queue', message)

    def send_direct(self, message: str) -> Optional[dict[str, Any]]:
        """Send a message directly to Claude."""
        return self.send('direct', message)

    def get_status(self, silent: bool = False) -> Optional[dict[str, Any]]:
        """Get server status."""
        return self.send('status', silent=silent)

    def force_send_next(self) -> Optional[dict[str, Any]]:
        """Force send the next queued message."""
        return self.send('force_send')

    def get_message_for_edit(
        self, index: int, silent: bool = False
    ) -> Optional[dict[str, Any]]:
        """
        Get a message by index for editing.

        Args:
            index: Queue index (0-based).
            silent: If True, suppress error messages.

        Returns:
            Dictionary with 'id' and 'message', or None on error.
        """
        return self._send_request(
            {'type': 'get_message', 'index': index}, silent=silent
        )

    def edit_message(
        self, msg_id: str, new_message: str, silent: bool = False
    ) -> Optional[dict[str, Any]]:
        """
        Edit a message by its ID.

        Args:
            msg_id: Message ID to edit.
            new_message: New message content.
            silent: If True, suppress error messages.

        Returns:
            Server response dictionary, or None on error.
        """
        return self._send_request(
            {'type': 'edit_message', 'id': msg_id, 'new_message': new_message},
            silent=silent,
        )

    def set_auto_send_mode(
        self, mode: str, silent: bool = False
    ) -> Optional[dict[str, Any]]:
        """
        Set the auto-send mode on the server.

        Args:
            mode: 'pause' or 'always'.
            silent: If True, suppress error messages.

        Returns:
            Server response dictionary, or None on error.
        """
        return self._send_request(
            {'type': 'set_auto_send_mode', 'mode': mode},
            silent=silent,
        )
