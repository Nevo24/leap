"""Lightweight socket sender for queuing messages to Leap sessions."""

import logging
from typing import Optional

from leap.utils.constants import SOCKET_DIR
from leap.utils.socket_utils import send_socket_request
from leap.monitor.pr_tracking.config import load_leap_preset

logger = logging.getLogger(__name__)


def _send_to_socket(
    tag: str,
    msg_type: str,
    message: str,
    success_statuses: tuple[str, ...] = ('ok', 'queued'),
) -> bool:
    """Send a message to a Leap session socket and check the result.

    Args:
        tag: Session tag name.
        msg_type: Socket message type ('queue' or 'direct').
        message: Message body.
        success_statuses: Response status values considered successful.

    Returns:
        True on success, False on failure.
    """
    socket_path = SOCKET_DIR / f"{tag}.sock"
    if not socket_path.exists():
        logger.debug("Socket not found for session: %s", tag)
        return False

    result = send_socket_request(socket_path, {'type': msg_type, 'message': message})
    if result is None:
        logger.debug("Failed to send to session %s", tag)
        return False

    return result.get('status') in success_statuses


def send_to_leap_session(tag: str, message: str) -> bool:
    """Send a queued message to a Leap session via Unix socket.

    The selected preset (from leap_selected_preset) is prepended to the
    message if set.

    Args:
        tag: Session tag name.
        message: Message to queue.

    Returns:
        True on success, False on failure.
    """
    preset = load_leap_preset()
    if preset:
        message = preset + '\n' + message

    return _send_to_socket(tag, 'queue', '[scm] ' + message)


def send_to_leap_session_raw(tag: str, message: str) -> bool:
    """Send a message to a Leap session without prepending any preset.

    Args:
        tag: Session tag name.
        message: Message to queue as-is.

    Returns:
        True on success, False on failure.
    """
    return _send_to_socket(tag, 'queue', message)


def prepend_to_leap_queue(tag: str, messages: list[str]) -> bool:
    """Prepend messages to the front of a Leap session's queue.

    Messages are inserted in order so the first element will be sent next.

    Args:
        tag: Session tag name.
        messages: Ordered list of messages to prepend.

    Returns:
        True on success, False on failure.
    """
    socket_path = SOCKET_DIR / f"{tag}.sock"
    if not socket_path.exists():
        logger.debug("Socket not found for session: %s", tag)
        return False

    result = send_socket_request(
        socket_path, {'type': 'queue_prepend', 'messages': messages})
    if result is None:
        logger.debug("Failed to prepend to session %s", tag)
        return False

    return result.get('status') in ('ok', 'queued')


def send_to_leap_session_direct(tag: str, message: str) -> bool:
    """Send a message directly to a Leap session, bypassing the queue.

    The message is sent immediately to the CLI via the PTY, regardless of
    whether the CLI is currently busy.

    Args:
        tag: Session tag name.
        message: Message to send directly.

    Returns:
        True on success, False on failure.
    """
    return _send_to_socket(tag, 'direct', message, success_statuses=('sent',))
