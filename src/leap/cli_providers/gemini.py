"""
Gemini CLI provider.

Implements the CLIProvider interface for Google's Gemini CLI.
Ink TUI (React), same framework as Claude Code.

Key differences from Claude Code:
- Hooks: ~/.gemini/settings.json with AfterAgent/Notification events
- Hook protocol: JSON stdin/stdout (hooks must output JSON, at minimum '{}')
- Permission prompts use radio-button menus ("Allow once", "Allow for this session", etc.)
- Binary: gemini (installed via npm: @google/gemini-cli)
- --yolo flag for auto-approve (equivalent to --dangerously-skip-permissions)
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.base import CLIProvider


GEMINI_CONFIG_DIR: Path = Path.home() / ".gemini"
GEMINI_SETTINGS_FILE: Path = GEMINI_CONFIG_DIR / "settings.json"
HOOK_MARKER: str = "leap-hook.sh"


class GeminiProvider(CLIProvider):
    """Provider for Gemini CLI (Ink TUI, TypeScript/Node.js)."""

    # -- Identity --------------------------------------------------------

    @property
    def name(self) -> str:
        return 'gemini'

    @property
    def command(self) -> str:
        return 'gemini'

    @property
    def display_name(self) -> str:
        return 'Gemini CLI'

    # -- State detection patterns ----------------------------------------

    @property
    def trust_dialog_patterns(self) -> list[bytes]:
        # Gemini CLI does not have a workspace trust dialog on startup.
        return []

    @property
    def interrupted_pattern(self) -> bytes:
        return b'Cancelled'

    @property
    def confirmed_interrupt_pattern(self) -> Optional[bytes]:
        return None

    @property
    def dialog_patterns(self) -> list[bytes]:
        # Gemini shows radio-button permission prompts with these options.
        # After ANSI stripping + space removal:
        return [b'Allowonce']

    @property
    def silence_timeout(self) -> Optional[float]:
        return None

    # -- Menu / option parsing -------------------------------------------

    @property
    def has_numbered_menus(self) -> bool:
        # Gemini uses radio-button menus for approval prompts,
        # navigated with arrow keys (not numbered input).
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
        return True

    # -- Hook configuration ----------------------------------------------

    @property
    def hook_config_dir(self) -> Path:
        return GEMINI_CONFIG_DIR

    @property
    def requires_binary_for_hooks(self) -> bool:
        return True

    def configure_hooks(self, hook_script_path: str) -> None:
        """Install hooks into ~/.gemini/settings.json.

        Gemini CLI hooks format:
        - Top-level "hooks" key with event arrays
        - Each entry: {matcher, hooks: [{type, command, name}]}
        - Hooks receive JSON on stdin, must output JSON on stdout
        - leap-hook.sh already outputs '{}' to satisfy this requirement

        We configure:
        - AfterAgent → writes "idle" state to signal file
        - Notification (ToolPermission) → writes "needs_permission"
        """
        settings: dict[str, Any] = {}
        if GEMINI_SETTINGS_FILE.exists():
            try:
                with open(GEMINI_SETTINGS_FILE, "r") as f:
                    settings = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        if "hooks" not in settings:
            settings["hooks"] = {}

        hooks = settings["hooks"]

        def make_entry(
            state: str, matcher: str = "*", name: str = "",
        ) -> dict[str, Any]:
            return {
                "matcher": matcher,
                "hooks": [{
                    "type": "command",
                    "command": f"{hook_script_path} {state}",
                    "name": name or f"leap-{state}",
                }],
            }

        def upsert(
            hook_list: list[dict[str, Any]],
            new_entries: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            """Remove old Leap entries and add new ones."""
            cleaned = [
                e for e in hook_list
                if not any(
                    HOOK_MARKER in h.get("command", "")
                    for h in e.get("hooks", [])
                )
            ]
            cleaned.extend(new_entries)
            return cleaned

        # AfterAgent hook → idle state
        if "AfterAgent" not in hooks:
            hooks["AfterAgent"] = []
        hooks["AfterAgent"] = upsert(
            hooks["AfterAgent"],
            [make_entry("idle", name="leap-idle")],
        )

        # Notification hook → needs_permission (ToolPermission only)
        if "Notification" not in hooks:
            hooks["Notification"] = []
        hooks["Notification"] = upsert(
            hooks["Notification"],
            [make_entry("needs_permission", matcher="ToolPermission",
                        name="leap-needs-permission")],
        )

        GEMINI_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(GEMINI_SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")

    # -- CLI-specific input behaviors ------------------------------------

    def select_option(
        self,
        option_num: int,
        options: dict[int, str],
        pty_send: Any,
        pty_sendline: Any,
    ) -> dict[str, Any]:
        """Handle approval in Gemini's Ink TUI.

        Gemini uses arrow-key navigation for radio-button menus.
        option_num=1 → first option (Enter on first item)
        option_num>=2 → navigate down with arrow keys
        """
        if option_num == 1:
            pty_send('\r')
            return {'status': 'sent'}
        elif option_num >= 2:
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
        """Send text input in Gemini's Ink TUI."""
        for ch in text:
            pty_send(ch)
            time.sleep(0.02)
        time.sleep(0.1)
        pty_send('\r')
        return {'status': 'sent'}
