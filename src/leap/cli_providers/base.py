"""
Abstract base class for CLI providers.

Each provider defines the patterns, timings, and behaviors specific to
a CLI tool (Claude Code, Codex, Cursor Agent, Gemini CLI, etc.) so that the PTY handler, state
tracker, and server can work with any registered CLI.
"""

import json
import os
import re
import shutil
import sys
import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pexpect

from leap.cli_providers.states import SIGNAL_ALIASES, SIGNAL_STATES


class CLIProvider(ABC):
    """Abstract interface for a CLI backend."""

    # -- Identity --------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier used in config/metadata (e.g. 'claude', 'codex', 'cursor-agent', 'gemini')."""

    @property
    @abstractmethod
    def command(self) -> str:
        """Binary name to search for in PATH (e.g. 'claude', 'codex', 'cursor-agent', 'gemini')."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name (e.g. 'Claude Code', 'OpenAI Codex', 'Cursor Agent', 'Gemini CLI')."""

    def is_installed(self) -> bool:
        """Check whether the CLI binary is available on PATH."""
        return shutil.which(self.command) is not None

    # -- State detection patterns ----------------------------------------

    @property
    def trust_dialog_patterns(self) -> list[bytes]:
        """Compact patterns (ANSI-stripped, spaces removed) for startup trust dialog.

        Return empty list if the CLI has no trust dialog.
        Any match triggers detection.
        """
        return [
            b'Yes,Itrustthisfolder',
            b'Doyoutrustthecontentsofthisdirectory?',
        ]

    @property
    @abstractmethod
    def interrupted_pattern(self) -> bytes:
        """Text that appears in PTY output when the user interrupts."""

    @property
    def confirmed_interrupt_pattern(self) -> Optional[bytes]:
        """Specific pattern (ANSI-stripped, spaces removed) that confirms
        a real interrupt prompt — not just the word in conversation text.

        Checked against compact output (ANSI stripped + spaces removed).
        Must be specific enough to avoid false positives.  Used as a
        fallback when the Escape/Ctrl+C input bypasses on_input().

        Return None to rely solely on the escape-time-based check.
        """
        return None

    @property
    @abstractmethod
    def dialog_patterns(self) -> list[bytes]:
        """Compact patterns (ANSI-stripped, spaces removed) that indicate
        a permission/question dialog.  ALL must be present for a match."""

    def has_dialog_indicator(self, compact_text: str) -> bool:
        """Lenient dialog check: any single indicator is sufficient.

        Used by the Late Notification guard to *verify* a hook signal —
        the hook already confirmed a dialog, so a weak match suffices.
        Default: any single ``dialog_patterns`` entry is present
        (catches edit-confirmation dialogs that only show "Esc to cancel"
        without "Enter to select").

        Override for CLIs with additional dialog formats (e.g. numbered
        menus) that don't contain the standard dialog footer patterns.

        Args:
            compact_text: Screen text with spaces and newlines removed.
        """
        return any(
            p.decode('utf-8', errors='replace') in compact_text
            for p in self.dialog_patterns
        )

    def is_dialog_certain(self, compact_text: str) -> bool:
        """Strict dialog check: high confidence that a dialog is visible.

        Used for *proactive* detection (running→idle, startup) where no
        hook signal exists yet and false positives are costly (state gets
        stuck in needs_permission until the 60s safety timeout).

        Default: ALL ``dialog_patterns`` must be present.
        Override to add provider-specific high-confidence indicators
        (e.g. numbered menu cursor character).

        Args:
            compact_text: Screen text with spaces and newlines removed.
        """
        patterns = self.dialog_patterns
        return bool(patterns) and all(
            p.decode('utf-8', errors='replace') in compact_text
            for p in patterns
        )

    @property
    def valid_signal_states(self) -> frozenset[str]:
        """States that can appear in the hook signal file."""
        return SIGNAL_STATES

    @property
    def cursor_hidden_while_idle(self) -> bool:
        """Whether the CLI keeps the terminal cursor hidden during idle.

        Full-screen TUIs (Ratatui) hide the cursor permanently and
        manage their own cursor rendering.  When True, the auto-resume
        cursor visibility check is disabled (cursor hidden doesn't
        indicate processing).  Defaults to False (Ink TUIs show cursor
        when idle).
        """
        return False

    @property
    def silence_timeout(self) -> Optional[float]:
        """Override the default silence timeout (seconds) for this CLI.

        Return None to use the global SAFETY_SILENCE_TIMEOUT constant.
        Full-screen TUIs (Ratatui) that output constantly during processing
        can use a shorter timeout since any output gap indicates idle.
        """
        return None

    # -- Transcript-based idle detection ---------------------------------

    @property
    def transcript_sessions_dir(self) -> Optional[Path]:
        """Directory where the CLI stores session transcripts.

        When set, the state tracker polls the most recent transcript
        for completion events, enabling near-instant idle detection
        instead of relying on the silence timeout.

        Return None if the CLI doesn't have accessible transcripts.
        """
        return None

    def read_transcript_completion(self, since: float = 0) -> Optional[str]:
        """Check the CLI's transcript for a task-completion event.

        Reads the tail of the most recently modified transcript file
        and looks for a ``task_complete`` event whose ISO timestamp is
        newer than ``since`` (Unix epoch).  This prevents detecting
        stale completions from previous turns when the transcript is
        incrementally updated (user message written before task_complete).

        Called every poll cycle (~0.5s), so must be fast:
        - Only scans today's date directory (not full rglob)
        - Reads only the last 32KB of the file

        Args:
            since: Unix timestamp.  Only return completions with an
                ISO timestamp strictly after this.

        Returns:
            The last assistant message text, or None if not found.
        """
        sessions_dir = self.transcript_sessions_dir
        if sessions_dir is None or not sessions_dir.exists():
            return None
        try:
            transcript = self._find_active_transcript(sessions_dir)
            if transcript is None:
                return None
            if time.time() - transcript.stat().st_mtime > 30:
                return None
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
                        # Check the entry's timestamp against 'since'
                        ts_str = entry.get('timestamp', '')
                        if ts_str and since > 0:
                            entry_dt = datetime.fromisoformat(
                                ts_str.replace('Z', '+00:00'),
                            )
                            entry_ts = entry_dt.timestamp()
                            if entry_ts <= since:
                                return None  # Stale completion
                        msg = payload.get('last_agent_message', '')
                        return msg.strip() if msg else None
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        except OSError:
            pass
        return None

    def _find_active_transcript(self, sessions_dir: Path) -> Optional[Path]:
        """Find the most recently modified transcript in today's directory.

        Much faster than rglob — only lists files in today's date dir.
        Falls back to yesterday if today's dir doesn't exist yet.
        """
        today = date.today()
        for d in (today, today - timedelta(days=1)):
            day_dir = sessions_dir / d.strftime('%Y/%m/%d')
            if not day_dir.is_dir():
                continue
            best: Optional[Path] = None
            best_mtime: float = 0
            try:
                for f in day_dir.iterdir():
                    if f.suffix == '.jsonl':
                        mt = f.stat().st_mtime
                        if mt > best_mtime:
                            best = f
                            best_mtime = mt
            except OSError:
                continue
            if best is not None:
                return best
        return None

    # -- Menu / option parsing -------------------------------------------

    @property
    def has_numbered_menus(self) -> bool:
        """Whether the CLI uses numbered menu options for prompts."""
        return True

    @property
    def menu_option_regex(self) -> Optional[re.Pattern[str]]:
        """Regex to extract numbered options from prompt output.

        Must have groups: (1) option number, (2) option label.
        Return None if the CLI doesn't use numbered menus.
        """
        return None

    @property
    def free_text_option_prefix(self) -> Optional[str]:
        """Label prefix for the 'type your own answer' option."""
        return None

    @property
    def below_separator_option_prefix(self) -> Optional[str]:
        """Label prefix for options below a separator that need arrow-key nav."""
        return None

    # -- Input protocol --------------------------------------------------

    @property
    def paste_settle_time(self) -> float:
        """Settle time (seconds) after sending multi-line text."""
        return 0.15

    @property
    def single_settle_time(self) -> float:
        """Settle time (seconds) after sending single-line text."""
        return 0.05

    @property
    def image_prefix(self) -> str:
        """Prefix character for image file attachments (e.g. '@')."""
        return '@'

    @property
    def supports_image_attachments(self) -> bool:
        """Whether the CLI supports inline image file attachments."""
        return False

    # -- Hook configuration ----------------------------------------------

    @property
    @abstractmethod
    def hook_config_dir(self) -> Path:
        """Directory where the CLI stores its configuration/hooks.

        The leap-hook.sh script will be copied into this directory
        during installation.  E.g. ``~/.claude/hooks``, ``~/.codex``, ``~/.cursor``, or ``~/.gemini``.
        """

    @property
    def requires_binary_for_hooks(self) -> bool:
        """Whether hook configuration should be skipped if the CLI binary is not found.

        Return True if hooks should only be configured when the CLI
        is actually installed (e.g. Codex).  Return False to always
        configure hooks (e.g. Claude Code, which is the primary CLI).
        """
        return False

    @abstractmethod
    def configure_hooks(self, hook_script_path: str) -> None:
        """Install hooks into the CLI's configuration.

        Args:
            hook_script_path: Absolute path to the leap-hook.sh script.
        """

    # -- CLI binary lookup -----------------------------------------------

    def find_cli(self) -> Optional[str]:
        """Find the CLI executable in PATH.

        Returns:
            Absolute path to the CLI binary, or None if not found.
        """
        for path_dir in os.environ.get('PATH', '').split(':'):
            candidate = os.path.join(path_dir, self.command)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    # -- Environment variables -------------------------------------------

    def get_spawn_env(
        self, tag: Optional[str], signal_dir: Optional[Path],
    ) -> dict[str, str]:
        """Build extra environment variables for the spawned CLI process.

        Args:
            tag: Session tag name.
            signal_dir: Directory for signal files.

        Returns:
            Dict of environment variables to merge into os.environ.
        """
        env: dict[str, str] = {}
        if tag:
            env['LEAP_TAG'] = tag
        if signal_dir:
            env['LEAP_SIGNAL_DIR'] = str(signal_dir)
        # Pass the current Python interpreter path so hook scripts can use
        # the venv Python instead of relying on a bare `python3` in PATH.
        env['LEAP_PYTHON'] = sys.executable
        # Tell the hook which provider is firing so recording is routed to
        # the right `.storage/cli_sessions/<cli>/` subdir without the hook
        # needing to probe every provider's transcript-path signature.
        env['LEAP_CLI_PROVIDER'] = self.name
        return env

    # -- Resume support (leap --resume) ----------------------------------

    @property
    def supports_resume(self) -> bool:
        """Whether this CLI supports resuming via `leap --resume`.

        Default ``False``.  Override and return ``True`` after implementing
        :meth:`extract_session_id` and :meth:`resume_args`.
        """
        return False

    def extract_session_id(self, hook_data: dict) -> Optional[str]:
        """Return the CLI's session id from a hook payload, or ``None``.

        ``hook_data`` is the JSON the CLI sent to leap-hook.sh on stdin
        (Stop/Notification events).  Different CLIs surface the id
        differently — e.g. Claude encodes it in the ``transcript_path``
        filename, Codex passes it directly as ``session_id``.  Return
        ``None`` when the payload isn't one of this CLI's sessions so the
        hook knows to skip recording.
        """
        return None

    def resume_args(self, session_id: str) -> list[str]:
        """CLI argv tokens to resume the given session.

        These are **prepended** to the user's CLI flags before the server
        spawns the binary, so positional subcommand forms (Codex's
        ``resume <id>``) stay in the right spot.  Return empty list to
        opt out.  Example implementations::

            # Claude: ``claude --resume=<id>``
            return [f'--resume={session_id}']

            # Codex: ``codex resume <id>``
            return ['resume', session_id]
        """
        return []

    # -- Hook payload extraction -----------------------------------------

    def extract_last_assistant_message(self, hook_data: dict) -> str:
        """Return the last assistant-generated text from a hook payload.

        Most CLIs (Codex, Cursor, Gemini) pass the string directly as
        ``hook_data['last_assistant_message']``.  Claude Code writes its
        output to a JSONL transcript and expects consumers to tail it —
        :class:`ClaudeProvider` overrides this to do that.  Consumed by
        the Slack integration to preview the last reply.
        """
        msg = hook_data.get('last_assistant_message', '')
        return msg if isinstance(msg, str) else ''

    # -- CLI-specific input behaviors ------------------------------------

    def send_message(
        self,
        process: pexpect.spawn,
        message: str,
        send_lock: Any,
        write_fn: Any,
        wait_fn: Any,
    ) -> None:
        """Send a regular message to the CLI.

        Default implementation: write text, wait for settle, send CR.

        Args:
            process: The pexpect process.
            message: Message text to send.
            send_lock: Threading lock (already held by caller).
            write_fn: Callable to write raw data to PTY.
            wait_fn: Callable to wait for output settle.
        """
        settle = self.paste_settle_time if '\n' in message else self.single_settle_time
        write_fn(message)
        wait_fn(settle_time=settle)
        write_fn('\r')

    def send_image_message(
        self,
        process: pexpect.spawn,
        message: str,
        send_lock: Any,
        write_fn: Any,
        wait_fn: Any,
    ) -> None:
        """Send an image attachment message.

        Uses fixed sleeps instead of ``wait_fn`` to avoid its Phase 1
        timeout (up to 2 s) which can give file-picker autocomplete
        time to open and capture the CR. Two CRs are sent: the first
        confirms any autocomplete, the second submits.

        Args:
            process: The pexpect process.
            message: Message text (may include image reference).
            send_lock: Threading lock (already held by caller).
            write_fn: Callable to write raw data to PTY.
            wait_fn: Callable to wait for output settle.
        """
        write_fn(message)
        time.sleep(1.5)   # Let autocomplete fully render
        write_fn('\r')    # Confirm file selection
        time.sleep(1.0)   # Let TUI process the selection
        write_fn('\r')    # Submit the message

    def is_image_message(self, message: str) -> bool:
        """Check if a message is an image attachment.

        Args:
            message: The message to check.

        Returns:
            True if this message requires special image handling.
        """
        return self.supports_image_attachments and message.startswith(self.image_prefix)

    def select_option(
        self,
        option_num: int,
        options: dict[int, str],
        pty_send: Any,
        pty_sendline: Any,
    ) -> dict[str, Any]:
        """Select a numbered option in a permission/question dialog.

        Args:
            option_num: The option number to select.
            options: Dict of {number: label} for available options.
            pty_send: Callable to send raw data to PTY.
            pty_sendline: Callable to send data + CR to PTY.

        Returns:
            Response dict with 'status' key.
        """
        return {'status': 'error', 'error': 'option selection not supported'}

    def send_custom_answer(
        self,
        text: str,
        options: dict[int, str],
        pty_send: Any,
    ) -> dict[str, Any]:
        """Send a free-form text answer to a question dialog.

        Args:
            text: The user's text answer.
            options: Dict of {number: label} for available options.
            pty_send: Callable to send raw data to PTY.

        Returns:
            Response dict with 'status' key.
        """
        return {'status': 'error', 'error': 'custom answers not supported'}

    # -- Hook signal file parsing ----------------------------------------

    def parse_signal_file(self, raw: str) -> Optional[str]:
        """Parse the signal file content and return the state.

        Default implementation: parse JSON with 'state' key.

        Args:
            raw: Raw file content.

        Returns:
            A valid state string, or None.
        """
        try:
            data = json.loads(raw)
            state = data.get('state', '')
            # Backward compat: old hooks may write 'has_question'
            state = SIGNAL_ALIASES.get(state, state)
            if state in self.valid_signal_states:
                return state
        except (json.JSONDecodeError, AttributeError):
            pass
        return None
