"""Lightweight socket sender for queuing messages to CQ sessions."""

import json
import logging
import socket

from claudeq.utils.constants import SOCKET_DIR

logger = logging.getLogger(__name__)


def send_to_cq_session(tag: str, message: str) -> bool:
    """Send a queued message to a CQ session via Unix socket.

    Args:
        tag: Session tag name.
        message: Message to queue.

    Returns:
        True on success, False on failure.
    """
    socket_path = SOCKET_DIR / f"{tag}.sock"
    if not socket_path.exists():
        logger.warning("Socket not found for session: %s", tag)
        return False

    try:
        client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client_socket.settimeout(5.0)
        client_socket.connect(str(socket_path))

        data = {'type': 'queue', 'message': message}
        client_socket.sendall(json.dumps(data).encode('utf-8'))
        chunks = []
        while True:
            chunk = client_socket.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        client_socket.close()

        result = json.loads(b''.join(chunks).decode('utf-8'))
        return result.get('status') in ('ok', 'queued')
    except (socket.error, json.JSONDecodeError, OSError) as e:
        logger.error("Failed to send to session %s: %s", tag, e)
        return False
