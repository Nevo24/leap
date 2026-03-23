"""
Cursor Agent CLI provider.

Implements the CLIProvider interface for Cursor's terminal-based AI agent
(cursor-agent).  Ink TUI (React), same framework as Claude Code.

Key differences from Claude Code:
- Hooks: .cursor/hooks.json with JSON stdin/stdout protocol
- Only Stop hook available (no Notification hook for permission detection)
- Permission detection relies on PTY output patterns
- Menu-style approval prompts ("Allow once", "Allow always", etc.)
- Binary: cursor-agent (installed via curl https://cursor.com/install)
- Double Ctrl+C to exit
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.base import CLIProvider
from leap.cli_providers.states import SIGNAL_STATES


# Cursor hooks.json schema:
# {
#   "version": 1,
#   "hooks": {
#     "stop": [
#       { "command": "/path/to/leap-cursor-hook.sh idle" }
#     ]
#   }
# }

CURSOR_CONFIG_DIR: Path = Path.home() / ".cursor"
CURSOR_HOOKS_FILE: Path = CURSOR_CONFIG_DIR / "hooks.json"
HOOK_MARKER: str = "leap-hook.sh"


class CursorAgentProvider(CLIProvider):
    """Provider for Cursor Agent CLI (Ink TUI, Node.js)."""

    # -- Identity --------------------------------------------------------

    @property
    def name(self) -> str:
        return 'cursor-agent'

    @property
    def command(self) -> str:
        return 'cursor-agent'

    @property
    def display_name(self) -> str:
        return 'Cursor Agent'

    # -- State detection patterns ----------------------------------------

    @property
    def trust_dialog_patterns(self) -> list[bytes]:
        # Cursor Agent has a workspace trust dialog similar to Claude Code.
        # It asks if you trust the workspace before proceeding.
        return [
            b'Doyoutrustthisfolder',
            b'Trustthisworkspace',
        ]

    @property
    def interrupted_pattern(self) -> bytes:
        # Cursor Agent outputs "Interrupted" or "Conversation stopped"
        # when the user presses Ctrl+C.
        return b'Interrupted'

    @property
    def confirmed_interrupt_pattern(self) -> Optional[bytes]:
        # "Conversation stopped" is specific enough to avoid false positives
        # from conversational text containing "interrupted".
        return b'Conversationstopped'

    @property
    def dialog_patterns(self) -> list[bytes]:
        # Cursor Agent shows menu-style approval prompts.
        # "Allow once" and "Allow always" appear in permission dialogs.
        # After ANSI stripping + space removal:
        return [b'Allowonce']

    @property
    def valid_signal_states(self) -> frozenset[str]:
        # Only Stop hook available — idle comes from signal file.
        # Permission detection relies on PTY output patterns.
        return SIGNAL_STATES

    @property
    def output_triggers_running(self) -> bool:
        # Ink TUI (like Claude Code) — output after user input reliably
        # indicates processing.
        return True

    @property
    def enter_triggers_running(self) -> bool:
        return False

    @property
    def silence_timeout(self) -> Optional[float]:
        # Use default (15s).  Ink TUI has variable output cadence.
        return None

    # -- Menu / option parsing -------------------------------------------

    @property
    def has_numbered_menus(self) -> bool:
        # Cursor Agent uses menu-style prompts with arrow-key navigation,
        # not numbered menus.
        return False

    @property
    def menu_option_regex(self) -> Optional[re.Pattern[str]]:
        return None

    @property
    def free_text_option_prefix(self) -> Optional[str]:
        return None

    @property
    def below_separator_option_prefix(self) -> Optional[str]:
        return None

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
        return False

    # -- Hook configuration ----------------------------------------------

    @property
    def hook_config_dir(self) -> Path:
        return CURSOR_CONFIG_DIR

    @property
    def requires_binary_for_hooks(self) -> bool:
        return True

    def configure_hooks(self, hook_script_path: str) -> None:
        """Install hooks into ~/.cursor/hooks.json.

        Cursor Agent uses a different hooks format than Claude/Codex:
        - Top-level "version" and "hooks" keys
        - Hook events are lowercase (e.g. "stop", not "Stop")
        - Each hook entry has a "command" string (not nested "hooks" array)

        We configure the stop hook to call leap-hook.sh with "idle" state.
        """
        hooks_data: dict[str, Any] = {"version": 1, "hooks": {}}
        if CURSOR_HOOKS_FILE.exists():
            try:
                with open(CURSOR_HOOKS_FILE, "r") as f:
                    hooks_data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        if "hooks" not in hooks_data:
            hooks_data["hooks"] = {}
        if "version" not in hooks_data:
            hooks_data["version"] = 1

        hooks = hooks_data["hooks"]

        # Stop hook → writes "idle" state to signal file
        if "stop" not in hooks:
            hooks["stop"] = []

        legacy_marker = "claudeq-hook.sh"
        hooks["stop"] = [
            e for e in hooks["stop"]
            if not (HOOK_MARKER in e.get("command", "")
                    or legacy_marker in e.get("command", ""))
        ]
        hooks["stop"].append({
            "command": f"{hook_script_path} idle",
        })

        CURSOR_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CURSOR_HOOKS_FILE, "w") as f:
            json.dump(hooks_data, f, indent=2)
            f.write("\n")

    # -- CLI binary lookup -----------------------------------------------

    def find_cli(self) -> Optional[str]:
        """Find cursor-agent in PATH or common install location."""
        import os
        import shutil

        # Check PATH first
        result = shutil.which(self.command)
        if result:
            return result

        # Check common install location
        local_bin = Path.home() / ".local" / "bin" / "cursor-agent"
        if local_bin.is_file() and os.access(str(local_bin), os.X_OK):
            return str(local_bin)

        # Also check 'agent' alias
        result = shutil.which('agent')
        if result:
            return result

        return None

    def is_installed(self) -> bool:
        """Check whether cursor-agent is available."""
        return self.find_cli() is not None

    # -- CLI-specific input behaviors ------------------------------------

    def select_option(
        self,
        option_num: int,
        options: dict[int, str],
        pty_send: Any,
        pty_sendline: Any,
    ) -> dict[str, Any]:
        """Handle approval in Cursor Agent's Ink TUI.

        Cursor Agent uses arrow-key navigation for approval prompts.
        option_num=1 → Accept/Allow (Enter on first item)
        option_num=2 → Reject (arrow down + Enter)
        """
        if option_num == 1:
            # Select first option (usually "Allow once")
            pty_send('\r')
            return {'status': 'sent'}
        elif option_num >= 2:
            # Navigate down to the Nth option
            for _ in range(option_num - 1):
                pty_send('\x1b[B')
                time.sleep(0.1)
            time.sleep(0.2)
            pty_send('\r')
            return {'status': 'sent'}
        return {
            'status': 'error',
            'error': f'invalid option number: {option_num}',
        }

    def send_custom_answer(
        self,
        text: str,
        options: dict[int, str],
        pty_send: Any,
    ) -> dict[str, Any]:
        """Send text input in Cursor Agent's TUI."""
        for ch in text:
            pty_send(ch)
            time.sleep(0.02)
        time.sleep(0.1)
        pty_send('\r')
        return {'status': 'sent'}
