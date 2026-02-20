"""
Output capture for Slack integration.

Uses Claude Code's hook system to capture clean response text.
The hook script writes ``last_assistant_message`` to a signal file,
and this module reads it to produce ``.last_response`` files that
the Slack bot polls.
"""

import json
import time
from typing import Optional

from claudeq.utils.constants import SLACK_DIR, SOCKET_DIR, atomic_json_write


class OutputCapture:
    """Reads hook-provided response text and writes Slack-ready snapshots.

    The hook script (``claudeq-hook.sh``) writes the ``last_assistant_message``
    from Claude Code's stdin JSON into the signal file. This class reads that
    on state transitions and writes a ``.last_response`` file for the Slack bot.

    Enabled/disabled state is persisted in ``.storage/slack/sessions.json``
    so it survives server restarts.

    Args:
        tag: Session tag name.
    """

    def __init__(self, tag: str) -> None:
        self._tag = tag
        self._enabled = False
        self._response_file = SOCKET_DIR / f"{tag}.last_response"
        self._signal_file = SOCKET_DIR / f"{tag}.signal"
        self._sessions_file = SLACK_DIR / "sessions.json"

        # Restore persisted enabled state
        self._load_enabled()

    # -- Public API ----------------------------------------------------------

    def on_state_change(
        self,
        new_state: str,
        prev_state: str,
        queue_has_next: bool,
        prompt_output: str = '',
    ) -> None:
        """Write a .last_response file when Claude finishes a turn.

        Reads ``last_assistant_message`` from the signal file (written by
        the hook script) and packages it for the Slack bot.

        Args:
            new_state: The state Claude just transitioned to.
            prev_state: The state Claude was in before the transition.
            queue_has_next: Whether the auto-sender will pick up the
                next queued message.
            prompt_output: ANSI-stripped PTY output from the permission
                or question prompt (includes the question text and
                numbered options).
        """
        if not self._enabled:
            return
        if new_state == prev_state:
            return
        if new_state not in ('idle', 'needs_permission', 'has_question'):
            return

        # Read the response text and notification message from the signal file
        signal_data = self._read_signal_data()
        output = signal_data.get('output', '')
        notification_message = signal_data.get('notification_message', '')

        # For idle, skip if no meaningful output
        if new_state == 'idle' and not output:
            return

        payload = {
            'timestamp': time.time(),
            'output': output,
            'tag': self._tag,
            'state': new_state,
            'queue_has_next': queue_has_next,
            'notification_message': notification_message,
            'prompt_output': prompt_output,
        }
        try:
            atomic_json_write(self._response_file, payload)
        except OSError:
            pass

    def is_enabled(self) -> bool:
        """Return whether output capture is enabled for this session."""
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable output capture.

        Persists the setting to ``sessions.json``.

        Args:
            enabled: Whether to enable output capture.
        """
        self._enabled = enabled
        self._save_enabled()

    def cleanup(self) -> None:
        """Remove the .last_response file."""
        try:
            self._response_file.unlink(missing_ok=True)
        except OSError:
            pass

    # -- Signal file reading -------------------------------------------------

    def _read_signal_data(self) -> dict[str, str]:
        """Read assistant message and notification message from the signal file.

        Returns:
            Dict with ``output`` (assistant text) and
            ``notification_message`` (hook notification text) keys.
        """
        result: dict[str, str] = {'output': '', 'notification_message': ''}
        try:
            if not self._signal_file.exists():
                return result
            data = json.loads(self._signal_file.read_text())
            msg = data.get('last_assistant_message', '')
            if msg:
                result['output'] = msg.strip()
            notif = data.get('notification_message', '')
            if notif:
                result['notification_message'] = notif.strip()
        except (json.JSONDecodeError, OSError):
            pass
        return result

    # -- Persistence ---------------------------------------------------------

    def _load_enabled(self) -> None:
        """Restore enabled state from sessions.json."""
        try:
            if self._sessions_file.exists():
                data = json.loads(self._sessions_file.read_text())
                session = data.get(self._tag, {})
                self._enabled = session.get('enabled', False)
        except (json.JSONDecodeError, OSError):
            pass

    def _save_enabled(self) -> None:
        """Persist enabled state to sessions.json."""
        try:
            data: dict = {}
            if self._sessions_file.exists():
                data = json.loads(self._sessions_file.read_text())
            if self._tag not in data:
                data[self._tag] = {}
            data[self._tag]['enabled'] = self._enabled
            atomic_json_write(self._sessions_file, data)
        except (json.JSONDecodeError, OSError):
            pass
