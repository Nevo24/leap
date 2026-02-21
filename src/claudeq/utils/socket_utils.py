"""
Shared Unix socket communication utilities for ClaudeQ.

Provides a common send/receive pattern used by session_manager, cq_sender,
and other components that communicate with ClaudeQ servers via Unix sockets.
"""

import json
import socket
from pathlib import Path
from typing import Any, Optional


def send_socket_request(
    socket_path: Path,
    data: dict[str, Any],
    timeout: float = 5.0
) -> Optional[dict[str, Any]]:
    """
    Send a JSON request to a Unix socket and return the parsed response.

    Args:
        socket_path: Path to the Unix socket file.
        data: Request payload dictionary (will be JSON-encoded).
        timeout: Socket timeout in seconds.

    Returns:
        Parsed JSON response dictionary, or None on any error.
    """
    client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client_socket.settimeout(timeout)
        client_socket.connect(str(socket_path))

        client_socket.sendall(json.dumps(data).encode('utf-8'))
        client_socket.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = client_socket.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)

        return json.loads(b''.join(chunks).decode('utf-8'))
    except (socket.error, json.JSONDecodeError, OSError):
        return None
    finally:
        client_socket.close()
