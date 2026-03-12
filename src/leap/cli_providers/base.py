"""
Abstract base class for CLI providers.

Each provider defines the patterns, timings, and behaviors specific to
a CLI tool (Claude Code, Codex, etc.) so that the PTY handler, state
tracker, and server can work with any supported CLI.
"""

import json
import os
import re
from abc import ABC, abstractmethod
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
        """Short identifier used in config/metadata (e.g. 'claude', 'codex')."""

    @property
    @abstractmethod
    def command(self) -> str:
        """Binary name to search for in PATH (e.g. 'claude', 'codex')."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name (e.g. 'Claude', 'Codex')."""

    # -- State detection patterns ----------------------------------------

    @property
    def trust_dialog_pattern(self) -> Optional[bytes]:
        """Compact pattern (ANSI-stripped, spaces removed) for startup trust dialog.

        Return None if the CLI has no trust dialog.
        """
        return b'Doyoutrustthecontentsofthisdirectory?'

    @property
    @abstractmethod
    def interrupted_pattern(self) -> bytes:
        """Text that appears in PTY output when the user interrupts."""

    @property
    @abstractmethod
    def dialog_patterns(self) -> list[bytes]:
        """Compact patterns (ANSI-stripped, spaces removed) that indicate
        a permission/question dialog.  ALL must be present for a match."""

    @property
    def valid_signal_states(self) -> frozenset[str]:
        """States that can appear in the hook signal file."""
        return SIGNAL_STATES

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
        return env

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

        Default implementation: same as regular message.
        Providers with special image protocols should override.

        Args:
            process: The pexpect process.
            message: Message text (may include image reference).
            send_lock: Threading lock (already held by caller).
            write_fn: Callable to write raw data to PTY.
            wait_fn: Callable to wait for output settle.
        """
        self.send_message(process, message, send_lock, write_fn, wait_fn)

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
