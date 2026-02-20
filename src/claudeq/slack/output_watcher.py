"""
Poll .last_response files and post Claude's output to Slack threads.

Runs in a background thread, checking for new output from all
Slack-enabled CQ sessions every 2 seconds.
"""

import json
import logging
import re
import threading
import time
from typing import Any, Callable, Optional

from claudeq.utils.constants import SOCKET_DIR
from claudeq.slack.config import load_slack_sessions, save_slack_sessions

logger = logging.getLogger(__name__)

# Slack message limit is ~4000 chars for best rendering
_MAX_MESSAGE_LEN: int = 3900
_POLL_INTERVAL: float = 0.5


class OutputWatcher:
    """Polls ``.last_response`` files and posts output to Slack.

    Args:
        post_fn: Callable that posts a message to Slack.
            Signature: ``post_fn(channel, text, thread_ts) -> Optional[str]``
            Returns the thread_ts (for new threads) or None.
        channel_id: Slack DM channel ID.
    """

    def __init__(
        self,
        post_fn: Callable[[str, str, Optional[str]], Optional[str]],
        channel_id: str,
    ) -> None:
        self._post_fn = post_fn
        self._channel_id = channel_id
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Track last-seen timestamp per tag to avoid re-posting
        self._last_seen_ts: dict[str, float] = {}

    def start(self) -> None:
        """Start the background polling thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name='output-watcher',
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _poll_loop(self) -> None:
        """Main polling loop: check all enabled sessions for new output."""
        while self._running:
            try:
                self._poll_once()
            except Exception:
                logger.exception('Error in output watcher poll')
            time.sleep(_POLL_INTERVAL)

    def _poll_once(self) -> None:
        """Check each enabled session for a new .last_response file."""
        sessions = load_slack_sessions()

        for tag, session_data in sessions.items():
            if not session_data.get('enabled', False):
                continue

            # Auto-create thread for newly enabled sessions
            if not session_data.get('thread_ts'):
                self._send_greeting(tag)
                continue

            response_file = SOCKET_DIR / f"{tag}.last_response"
            if not response_file.exists():
                continue

            try:
                payload = json.loads(response_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            ts = payload.get('timestamp', 0)
            if ts <= self._last_seen_ts.get(tag, 0):
                continue  # Already posted

            self._last_seen_ts[tag] = ts
            self._post_output(tag, session_data, payload)

    def _send_greeting(self, tag: str) -> None:
        """Post a greeting message to create the Slack thread for a session.

        Called once when a session is first enabled. The message is not sent
        to the CQ server — it only creates the thread so the user can
        reply from Slack immediately.

        Args:
            tag: Session tag.
        """
        text = (
            f"*[{tag}]*\n"
            ":large_green_circle: *Slack tracking enabled.*\n"
            "Claude's output will appear in this thread.\n"
            "Reply here to send messages to this session."
        )
        result_ts = self._post_fn(self._channel_id, text, None)
        if result_ts:
            sessions = load_slack_sessions()
            if tag in sessions:
                sessions[tag]['thread_ts'] = result_ts
                save_slack_sessions(sessions)

    def _post_output(
        self,
        tag: str,
        session_data: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """Format and post output to the session's Slack thread.

        Creates a new thread if this is the first message for the session.

        Args:
            tag: Session tag.
            session_data: Persisted session data (thread_ts, etc.).
            payload: Parsed .last_response content.
        """
        output = payload.get('output', '')
        state = payload.get('state', 'idle')
        queue_has_next = payload.get('queue_has_next', False)

        notification_message = payload.get('notification_message', '')
        prompt_output = payload.get('prompt_output', '')

        # Build footer based on state
        footer = self._build_footer(
            state, queue_has_next, notification_message, prompt_output,
        )

        thread_ts = session_data.get('thread_ts')

        # Skip code block wrapper when output is empty (e.g. permission/
        # question prompts that have no assistant message yet).
        if not output:
            text = f"*[{tag}]*\n{footer}" if footer else f"*[{tag}]*"
            result_ts = self._post_fn(self._channel_id, text, thread_ts)

            if thread_ts is None and result_ts:
                thread_ts = result_ts
                sessions = load_slack_sessions()
                if tag in sessions:
                    sessions[tag]['thread_ts'] = thread_ts
                    save_slack_sessions(sessions)
            return

        # Split long output into multiple messages
        chunks = self._split_output(output, footer)

        for i, chunk in enumerate(chunks):
            # Only add footer to the last chunk
            if i == len(chunks) - 1 and footer:
                text = f"*[{tag}]*\n```\n{chunk}\n```\n{footer}"
            else:
                text = f"*[{tag}]*\n```\n{chunk}\n```"

            result_ts = self._post_fn(self._channel_id, text, thread_ts)

            # Save thread_ts from first message (creates the thread)
            if thread_ts is None and result_ts:
                thread_ts = result_ts
                sessions = load_slack_sessions()
                if tag in sessions:
                    sessions[tag]['thread_ts'] = thread_ts
                    save_slack_sessions(sessions)

    def _build_footer(
        self,
        state: str,
        queue_has_next: bool,
        notification_message: str = '',
        prompt_output: str = '',
    ) -> str:
        """Build the footer text based on Claude's state.

        Args:
            state: Claude's current state.
            queue_has_next: Whether auto-send will send the next message.
            notification_message: Notification text from the hook
                (e.g. the permission question).
            prompt_output: ANSI-stripped PTY output showing the actual
                prompt text and numbered options.

        Returns:
            Footer text string.
        """
        if state == 'idle' and not queue_has_next:
            return ':speech_balloon: *Claude is waiting for your input*'
        elif state == 'idle' and queue_has_next:
            return ':arrow_forward: _Auto-sending next message..._'
        elif state in ('needs_permission', 'has_question'):
            header = ':warning: *Claude Code needs your attention*'
            if notification_message:
                header = f':warning: *{notification_message}*'
            if prompt_output:
                cleaned = self._strip_meta_options(prompt_output)
                return (
                    f'{header}\n```\n{cleaned}\n```\n'
                    '_Reply with a number or type your answer._'
                )
            return (
                f'{header}\n'
                'Reply with a number to select an option, '
                'or check the terminal for details.'
            )
        return ''

    @staticmethod
    def _strip_meta_options(prompt: str) -> str:
        """Strip TUI meta-options from rendered prompt output.

        Removes "Type something.", the separator line, "Chat about this",
        and the "Enter to select" help line — these options don't work
        via Slack's text-only interface.
        """
        lines: list[str] = []
        for line in prompt.split('\n'):
            s = line.strip()
            if s and all(c in '─━' for c in s):
                continue
            if 'Chat about this' in s:
                continue
            # Keep "Type something" visible — Slack users can select it
            if s.startswith('Enter to select') or s.startswith('Esc to cancel'):
                continue
            if s.startswith('Tab/Arrow') or s.startswith('Tab to amend'):
                continue
            lines.append(line)
        # Remove trailing blank lines left by stripping
        while lines and not lines[-1].strip():
            lines.pop()
        return '\n'.join(lines)

    @staticmethod
    def _split_output(output: str, footer: str) -> list[str]:
        """Split output into chunks that fit Slack's message limit.

        Args:
            output: Full cleaned output text.
            footer: Footer text (reserved from message budget).

        Returns:
            List of output chunks.
        """
        # Reserve space for formatting: header + code fence + footer
        overhead = 100 + len(footer)
        max_chunk = _MAX_MESSAGE_LEN - overhead

        if len(output) <= max_chunk:
            return [output]

        chunks: list[str] = []
        while output:
            chunks.append(output[:max_chunk])
            output = output[max_chunk:]
        return chunks
