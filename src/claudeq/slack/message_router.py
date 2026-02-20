"""
Route Slack messages to CQ sessions via Unix sockets.

Looks up the CQ session tag from the Slack thread_ts, queries the
server's current state, and sends the message as either a queue
message (idle) or a direct PTY input (needs_permission / has_question).
"""

from pathlib import Path
from typing import Optional

from claudeq.utils.constants import SOCKET_DIR
from claudeq.utils.socket_utils import send_socket_request
from claudeq.slack.config import load_slack_sessions


class MessageRouter:
    """Routes incoming Slack messages to the correct CQ session."""

    def route_message(self, thread_ts: str, text: str) -> Optional[str]:
        """Route a Slack thread reply to the matching CQ session.

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

        claude_state = status.get('claude_state', 'idle')

        if claude_state in ('needs_permission', 'has_question'):
            normalized = text.strip()
            if normalized.isdigit():
                # Numeric answer — select the numbered option.
                response = send_socket_request(
                    socket_path,
                    {'type': 'select_option', 'message': normalized},
                )
                # If the option was "Type something", server rejects it.
                # Tell the user to type their answer as text.
                if (
                    response
                    and response.get('status') == 'error'
                    and 'type your answer' in response.get('error', '')
                ):
                    return 'type_text_instead'
            else:
                # Free-form text — select "Type something." option
                # and enter the user's text.  Falls back to error if
                # the dialog has no "Type something." option (e.g.
                # a real permission prompt).
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
            # Queue the message
            response = send_socket_request(
                socket_path, {'type': 'queue', 'message': text},
            )
            if response and response.get('status') == 'queued':
                return 'queued'
            return None

    def _find_tag_for_thread(self, thread_ts: str) -> Optional[str]:
        """Look up the CQ session tag for a Slack thread.

        Args:
            thread_ts: Slack thread timestamp.

        Returns:
            Session tag, or None if not found.
        """
        sessions = load_slack_sessions()
        for tag, data in sessions.items():
            if data.get('thread_ts') == thread_ts:
                return tag
        return None
