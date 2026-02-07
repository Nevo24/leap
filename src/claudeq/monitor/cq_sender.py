"""Lightweight socket sender for queuing messages to CQ sessions."""

import logging

from claudeq.utils.constants import SOCKET_DIR
from claudeq.utils.socket_utils import send_socket_request

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

    result = send_socket_request(
        socket_path, {'type': 'queue', 'message': '[gitlab] ' + message}
    )
    if result is None:
        logger.error("Failed to send to session %s", tag)
        return False

    return result.get('status') in ('ok', 'queued')
