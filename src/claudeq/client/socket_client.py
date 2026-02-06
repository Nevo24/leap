"""
Socket client for ClaudeQ.

Handles communication with the ClaudeQ server via Unix socket.
"""

import json
import socket
from pathlib import Path
from typing import Any, Optional


class SocketClient:
    """Client for communicating with ClaudeQ server via Unix socket."""

    def __init__(self, socket_path: Path):
        """
        Initialize socket client.

        Args:
            socket_path: Path to the server's Unix socket.
        """
        self.socket_path = socket_path

    def send(self, msg_type: str, message: str = "") -> Optional[dict[str, Any]]:
        """
        Send a message to the server and get response.

        Args:
            msg_type: Message type ('queue', 'direct', 'status', 'force_send').
            message: Message content.

        Returns:
            Server response dictionary, or None on error.
        """
        try:
            client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_socket.connect(str(self.socket_path))

            data = {'type': msg_type, 'message': message}
            client_socket.send(json.dumps(data).encode('utf-8'))
            response = client_socket.recv(4096).decode('utf-8')
            client_socket.close()

            return json.loads(response)
        except (socket.error, json.JSONDecodeError, OSError) as e:
            print(f"Error communicating with server: {e}")
            return None

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

    def get_status(self) -> Optional[dict[str, Any]]:
        """Get server status."""
        return self.send('status')

    def force_send_next(self) -> Optional[dict[str, Any]]:
        """Force send the next queued message."""
        return self.send('force_send')

    def get_message_for_edit(self, index: int) -> Optional[dict[str, Any]]:
        """
        Get a message by index for editing.

        Args:
            index: Queue index (0-based).

        Returns:
            Dictionary with 'id' and 'message', or None on error.
        """
        try:
            client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_socket.connect(str(self.socket_path))

            data = {'type': 'get_message', 'index': index}
            client_socket.send(json.dumps(data).encode('utf-8'))
            response = client_socket.recv(4096).decode('utf-8')
            client_socket.close()

            return json.loads(response)
        except (socket.error, json.JSONDecodeError, OSError) as e:
            print(f"Error communicating with server: {e}")
            return None

    def edit_message(self, msg_id: str, new_message: str) -> Optional[dict[str, Any]]:
        """
        Edit a message by its ID.

        Args:
            msg_id: Message ID to edit.
            new_message: New message content.

        Returns:
            Server response dictionary, or None on error.
        """
        try:
            client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client_socket.connect(str(self.socket_path))

            data = {'type': 'edit_message', 'id': msg_id, 'new_message': new_message}
            client_socket.send(json.dumps(data).encode('utf-8'))
            response = client_socket.recv(4096).decode('utf-8')
            client_socket.close()

            return json.loads(response)
        except (socket.error, json.JSONDecodeError, OSError) as e:
            print(f"Error communicating with server: {e}")
            return None
