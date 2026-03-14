"""
Output capture for Slack integration.

Uses CLI hook systems to capture clean response text.
The hook script writes ``last_assistant_message`` to a signal file,
and this module reads it to produce ``.last_response`` files that
the Slack bot polls.

For CLIs whose hooks don't fire (e.g. Codex with unstable
``codex_hooks``), falls back to reading the CLI's own transcript
files (e.g. ``~/.codex/sessions/``).
"""

import glob
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.states import CLIState
from leap.utils.constants import SLACK_DIR, SOCKET_DIR, atomic_json_write


class OutputCapture:
    """Reads hook-provided response text and writes Slack-ready snapshots.

    The hook script (``leap-hook.sh``) writes the ``last_assistant_message``
    from Claude Code's stdin JSON into the signal file. This class reads that
    on state transitions and writes a ``.last_response`` file for the Slack bot.

    Enabled/disabled state is persisted in ``.storage/slack/sessions.json``
    so it survives server restarts.

    Args:
        tag: Session tag name.
    """

    def __init__(self, tag: str, cli_provider: str = '') -> None:
        self._tag = tag
        self._cli_provider = cli_provider
        self._enabled = False
        self._response_file = SOCKET_DIR / f"{tag}.last_response"
        self._signal_file = SOCKET_DIR / f"{tag}.signal"
        self._sessions_file = SLACK_DIR / "sessions.json"
        # Dedup: prevent writing identical payloads with new timestamps
        self._last_written: tuple[str, str] = ('', '')  # (output, state)

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
        if new_state not in (CLIState.IDLE, CLIState.NEEDS_PERMISSION, CLIState.NEEDS_INPUT, CLIState.INTERRUPTED):
            return

        # Read the response text and notification message from the signal file
        signal_data = self._read_signal_data()
        output = signal_data.get('output', '')
        notification_message = signal_data.get('notification_message', '')

        # For idle, skip if no meaningful output — unless the CLI just
        # finished a turn (prev was running), in which case Slack should
        # still get the state change so the user sees "Waiting for input".
        if new_state == CLIState.IDLE and not output and prev_state != CLIState.RUNNING:
            return

        # Dedup: skip if output and state are identical to the last write.
        # This prevents duplicate Slack posts when multiple code paths
        # trigger a write for the same logical event.
        key = (output, new_state)
        if key == self._last_written:
            return
        self._last_written = key

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

    def write_current_state(
        self,
        current_state: str,
        queue_has_next: bool,
        prompt_output: str = '',
    ) -> None:
        """Write a .last_response snapshot of the current state.

        Unlike ``on_state_change``, this does not require a state transition.
        Used when Slack is enabled mid-session so the watcher can post
        the current context immediately.

        Args:
            current_state: The current Claude state.
            queue_has_next: Whether the auto-sender has a queued message.
            prompt_output: ANSI-stripped PTY prompt text (for permissions).
        """
        if not self._enabled:
            return
        if current_state not in (CLIState.IDLE, CLIState.NEEDS_PERMISSION, CLIState.NEEDS_INPUT, CLIState.INTERRUPTED):
            return

        signal_data = self._read_signal_data()
        output = signal_data.get('output', '')
        notification_message = signal_data.get('notification_message', '')

        # Dedup: skip if output and state are identical to the last write.
        key = (output, current_state)
        if key == self._last_written:
            return
        self._last_written = key

        payload = {
            'timestamp': time.time(),
            'output': output,
            'tag': self._tag,
            'state': current_state,
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

        Falls back to reading the CLI's own transcript if the signal
        file has no ``last_assistant_message`` (e.g. when the hook
        doesn't fire).

        Returns:
            Dict with ``output`` (assistant text) and
            ``notification_message`` (hook notification text) keys.
        """
        result: dict[str, str] = {'output': '', 'notification_message': ''}
        try:
            if self._signal_file.exists():
                data = json.loads(self._signal_file.read_text())
                msg = data.get('last_assistant_message', '')
                if msg:
                    result['output'] = msg.strip()
                notif = data.get('notification_message', '')
                if notif:
                    result['notification_message'] = notif.strip()
        except (json.JSONDecodeError, OSError):
            pass

        # Fallback: read from CLI transcript when hook didn't provide
        # the assistant message (e.g. Codex with broken codex_hooks).
        if not result['output']:
            fallback = self._read_transcript_fallback()
            if fallback:
                result['output'] = fallback

        return result

    def _read_transcript_fallback(self) -> str:
        """Read last assistant message from the CLI's own transcript.

        Currently supports Codex transcripts at
        ``~/.codex/sessions/<date>/<session>.jsonl``.

        Returns:
            The last assistant message text, or empty string.
        """
        if self._cli_provider != 'codex':
            return ''
        try:
            sessions_dir = Path.home() / '.codex' / 'sessions'
            if not sessions_dir.exists():
                return ''
            # Find the most recently modified transcript file
            files = sorted(
                sessions_dir.rglob('*.jsonl'),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not files:
                return ''
            transcript = files[0]
            # Only use if modified within the last 30s (avoid stale data)
            if time.time() - transcript.stat().st_mtime > 30:
                return ''
            # Read from the end — look for task_complete event
            file_size = transcript.stat().st_size
            chunk_size = 32768
            with open(transcript, 'rb') as f:
                start = max(0, file_size - chunk_size)
                f.seek(start)
                tail = f.read()
            for raw_line in reversed(tail.split(b'\n')):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                    payload = entry.get('payload', {})
                    if payload.get('type') == 'task_complete':
                        msg = payload.get('last_agent_message', '')
                        if msg:
                            return msg.strip()
                except (json.JSONDecodeError, KeyError):
                    continue
        except OSError:
            pass
        return ''

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
            data: dict[str, Any] = {}
            if self._sessions_file.exists():
                data = json.loads(self._sessions_file.read_text())
            if self._tag not in data:
                data[self._tag] = {}
            data[self._tag]['enabled'] = self._enabled
            atomic_json_write(self._sessions_file, data)
        except (json.JSONDecodeError, OSError):
            pass
