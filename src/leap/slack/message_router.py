"""
Route Slack messages to Leap sessions via Unix sockets.

Looks up the Leap session tag from the Slack thread_ts, queries the
server's current state, and sends the message as either a queued
message or a direct PTY input.

Routing rules:
- Digit reply in any WAITING state → select_option (numbered dialog choice)
- Free-form text in NEEDS_INPUT → custom_answer (direct typed input)
- Free-form text in NEEDS_PERMISSION/INTERRUPTED → queue (do not auto-select
  "Type something." — the user likely wants to send a regular message)
"""

import time
from typing import Any, Optional

from leap.cli_providers.states import CLIState, WAITING_STATES
from leap.utils.constants import SOCKET_DIR
from leap.utils.socket_utils import send_socket_request
from leap.slack.config import load_slack_sessions

# Cache sessions for up to 5 seconds to avoid reading disk on every message
_SESSIONS_CACHE_TTL: float = 5.0


class MessageRouter:
    """Routes incoming Slack messages to the correct Leap session."""

    def __init__(self) -> None:
        self._sessions_cache: Optional[dict[str, Any]] = None
        self._sessions_cache_ts: float = 0.0

    def route_message(self, thread_ts: str, text: str) -> Optional[str]:
        """Route a Slack thread reply to the matching Leap session.

        Determines the session tag from the thread timestamp, checks
        the session's current state, and sends the message via the
        appropriate socket message type.

        Args:
            thread_ts: Slack thread timestamp identifying the session.
            text: Message text from the user.

        Returns:
            Status string (``'queued'``, ``'sent'``, ``'offline'``,
            ``'no_session'``), or None on communication error.
        """
        tag = self._find_tag_for_thread(thread_ts)
        if not tag:
            return 'no_session'

        socket_path = SOCKET_DIR / f"{tag}.sock"
        if not socket_path.exists():
            return 'offline'

        # Check current state to decide routing
        status = send_socket_request(socket_path, {'type': 'status'})
        if not status:
            return 'offline'

        cli_state = status.get('cli_state', CLIState.IDLE)

        if cli_state in WAITING_STATES:
            normalized = text.strip()
            if normalized.isdigit():
                # Digit reply — select a numbered option in the dialog.
                response = send_socket_request(
                    socket_path,
                    {'type': 'select_option', 'message': normalized},
                )
                if response and response.get('status') == 'sent':
                    return 'sent'
                if response and response.get('status') == 'error':
                    if 'type your answer' in response.get('error', ''):
                        return 'type_text_instead'
                    return 'invalid_permission'
                return None
            elif cli_state == CLIState.NEEDS_INPUT:
                # Free-form text only goes directly when Claude Code is
                # explicitly waiting for typed input (not a numbered menu).
                response = send_socket_request(
                    socket_path,
                    {'type': 'custom_answer', 'message': text},
                )
                if response and response.get('status') == 'sent':
                    return 'sent'
                if response and response.get('status') == 'error':
                    return 'invalid_permission'
                return None
            else:
                # NEEDS_PERMISSION / INTERRUPTED: queue free-form text rather
                # than auto-selecting "Type something." in the dialog. The user
                # can reply with a digit to pick an option explicitly.
                response = send_socket_request(
                    socket_path, {'type': 'queue', 'message': text},
                )
                if response and response.get('status') == 'queued':
                    return 'queued'
                return None
        else:
            # Queue the message
            response = send_socket_request(
                socket_path, {'type': 'queue', 'message': text},
            )
            if response and response.get('status') == 'queued':
                return 'queued'
            return None

    def _get_sessions(self) -> dict[str, Any]:
        """Return cached sessions, refreshing from disk if stale."""
        now = time.monotonic()
        if (self._sessions_cache is None
                or now - self._sessions_cache_ts > _SESSIONS_CACHE_TTL):
            self._sessions_cache = load_slack_sessions()
            self._sessions_cache_ts = now
        return self._sessions_cache

    def _find_tag_for_thread(self, thread_ts: str) -> Optional[str]:
        """Look up the Leap session tag for a Slack thread.

        Args:
            thread_ts: Slack thread timestamp.

        Returns:
            Session tag, or None if not found.
        """
        sessions = self._get_sessions()
        for tag, data in sessions.items():
            if data.get('thread_ts') == thread_ts:
                return tag
        return None
