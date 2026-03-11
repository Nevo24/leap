"""
Claude Code CLI provider.

Implements the CLIProvider interface for Anthropic's Claude Code CLI.
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

import pexpect

from leap.cli_providers.base import CLIProvider


_MENU_OPTION_RE: re.Pattern[str] = re.compile(r'\s*(?:[❯›]\s*)?(\d+)\.\s+(.+)')


class ClaudeProvider(CLIProvider):
    """Provider for Claude Code CLI (Ink TUI, TypeScript)."""

    # -- Identity --------------------------------------------------------

    @property
    def name(self) -> str:
        return 'claude'

    @property
    def command(self) -> str:
        return 'claude'

    @property
    def display_name(self) -> str:
        return 'Claude'

    # -- State detection patterns ----------------------------------------

    @property
    def interrupted_pattern(self) -> bytes:
        return b'Interrupted'

    @property
    def dialog_patterns(self) -> list[bytes]:
        return [b'Entertoselect', b'Esctocancel']

    # -- Menu / option parsing -------------------------------------------

    @property
    def has_numbered_menus(self) -> bool:
        return True

    @property
    def menu_option_regex(self) -> Optional[re.Pattern[str]]:
        return _MENU_OPTION_RE

    @property
    def free_text_option_prefix(self) -> Optional[str]:
        return 'Type something'

    @property
    def below_separator_option_prefix(self) -> Optional[str]:
        return 'Chat about this'

    # -- Input protocol --------------------------------------------------

    @property
    def paste_settle_time(self) -> float:
        return 0.15

    @property
    def single_settle_time(self) -> float:
        return 0.05

    @property
    def image_prefix(self) -> str:
        return '@'

    @property
    def supports_image_attachments(self) -> bool:
        return True

    # -- Hook configuration ----------------------------------------------

    def configure_hooks(self, hook_script_path: str) -> None:
        """Install hooks into ~/.claude/settings.json."""
        settings_path = Path.home() / ".claude" / "settings.json"
        marker = "leap-hook.sh"

        # Load existing settings
        settings: dict[str, Any] = {}
        if settings_path.exists():
            try:
                with open(settings_path, "r") as f:
                    settings = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        if "hooks" not in settings:
            settings["hooks"] = {}

        hooks = settings["hooks"]

        def make_entry(state: str, matcher: str = "") -> dict[str, Any]:
            entry: dict[str, Any] = {
                "hooks": [{"type": "command", "command": f"{hook_script_path} {state}"}]
            }
            if matcher:
                entry["matcher"] = matcher
            return entry

        def upsert(hook_list: list[dict[str, Any]], new_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
            cleaned = [
                e for e in hook_list
                if not any(marker in h.get("command", "") for h in e.get("hooks", []))
            ]
            cleaned.extend(new_entries)
            return cleaned

        # Stop hook
        if "Stop" not in hooks:
            hooks["Stop"] = []
        hooks["Stop"] = upsert(hooks["Stop"], [make_entry("idle")])

        # Notification hooks
        if "Notification" not in hooks:
            hooks["Notification"] = []
        hooks["Notification"] = upsert(hooks["Notification"], [
            make_entry("needs_permission", matcher="permission_prompt"),
            make_entry("has_question", matcher="elicitation_dialog"),
        ])

        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")

    # -- CLI-specific input behaviors ------------------------------------

    def send_image_message(
        self,
        process: pexpect.spawn,
        message: str,
        send_lock: Any,
        write_fn: Any,
        wait_fn: Any,
    ) -> None:
        """Send @-prefixed image message with Claude's file confirmation protocol.

        Claude CLI requires: text → wait → CR (confirm file) → wait → CR (submit).
        """
        write_fn(message)
        wait_fn(settle_time=0.5)
        write_fn('\r')  # Confirm file selection
        wait_fn(settle_time=0.3)
        write_fn('\r')  # Submit message

    def select_option(
        self,
        option_num: int,
        options: dict[int, str],
        pty_send: Any,
        pty_sendline: Any,
    ) -> dict[str, Any]:
        """Select a numbered option in Claude's Ink TUI dialog.

        Handles special cases:
        - 'Type something' options: return error asking for text input
        - 'Chat about this' options: use arrow-key navigation
        - Regular options: send the number digit + CR
        """
        label = options.get(option_num)
        if label is not None:
            if self.free_text_option_prefix and label.startswith(self.free_text_option_prefix):
                return {
                    'status': 'error',
                    'error': 'type your answer as text instead',
                }
            if self.below_separator_option_prefix and label.startswith(self.below_separator_option_prefix):
                # Navigate with individual arrow-down keys
                for _ in range(option_num - 1):
                    pty_send('\x1b[B')
                    time.sleep(0.1)
                time.sleep(0.2)
                pty_send('\r')
                return {'status': 'sent'}

        if option_num not in options:
            return {
                'status': 'error',
                'error': f'option {option_num} not found in prompt',
            }
        pty_sendline(str(option_num))
        return {'status': 'sent'}

    def send_custom_answer(
        self,
        text: str,
        options: dict[int, str],
        pty_send: Any,
    ) -> dict[str, Any]:
        """Select 'Type something' and enter free-form text in Claude's Ink TUI."""
        type_option = None
        for num, label in options.items():
            if self.free_text_option_prefix and label.startswith(self.free_text_option_prefix):
                type_option = str(num)
                break
        if not type_option:
            return {'status': 'error', 'error': 'no "Type something" option found'}

        # Step 1: Send digit to navigate to "Type something."
        pty_send(type_option)
        time.sleep(0.5)
        # Step 2: Type char-by-char for Ink raw-mode compatibility
        for ch in text:
            pty_send(ch)
            time.sleep(0.02)
        time.sleep(0.1)
        # Step 3: Submit
        pty_send('\r')
        return {'status': 'sent'}
