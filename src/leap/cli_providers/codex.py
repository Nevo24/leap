"""
OpenAI Codex CLI provider.

Implements the CLIProvider interface for OpenAI's Codex CLI (Rust/Ratatui TUI).

Key differences from Claude Code:
- Ratatui full-screen TUI (not Ink)
- Approval prompts are y/n style in bottom pane (not numbered menus)
- Hooks: SessionStart + Stop events via ~/.codex/hooks.json
- No Notification hook — permission/question detection relies on PTY output
- Image support via clipboard paste (Ctrl+V) or -i flag
- Config: ~/.codex/config.toml (TOML, not JSON)
"""

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.base import CLIProvider
from leap.cli_providers.states import SIGNAL_STATES


# Codex hooks.json schema:
# {
#   "Stop": [
#     { "hooks": [{ "type": "command", "command": "...", "timeout": 60 }] }
#   ]
# }

CODEX_CONFIG_DIR: Path = Path.home() / ".codex"
CODEX_HOOKS_FILE: Path = CODEX_CONFIG_DIR / "hooks.json"
HOOK_MARKER: str = "leap-hook.sh"


class CodexProvider(CLIProvider):
    """Provider for OpenAI Codex CLI (Ratatui TUI, Rust)."""

    # -- Identity --------------------------------------------------------

    @property
    def name(self) -> str:
        return 'codex'

    @property
    def command(self) -> str:
        return 'codex'

    @property
    def display_name(self) -> str:
        return 'Codex'

    # -- State detection patterns ----------------------------------------

    @property
    def interrupted_pattern(self) -> bytes:
        # Codex outputs: "■ Conversation interrupted - tell the model
        # what to do differently."
        # After ANSI stripping (no space removal), "interrupted" appears.
        return b'interrupted'

    @property
    def dialog_patterns(self) -> list[bytes]:
        # Codex uses Ratatui — no reliable PTY patterns for dialog
        # detection yet.  Return empty to disable PTY-based dialog
        # detection (rely on hooks when available).
        return []

    @property
    def valid_signal_states(self) -> frozenset[str]:
        # Codex's Stop hook writes 'idle'.  Since there's no Notification
        # hook, needs_permission/needs_input come from PTY output only
        # (not from the signal file).  We still accept them in case
        # future Codex versions add notification hooks.
        return SIGNAL_STATES

    @property
    def output_triggers_running(self) -> bool:
        # Ratatui redraws the full screen on every keystroke, producing
        # hundreds of bytes of stripped output (box-drawing, spinners,
        # status bar).  This is indistinguishable from actual CLI
        # processing output, so disable output-based idle→running.
        return False

    @property
    def enter_triggers_running(self) -> bool:
        # Since output_triggers_running is False, detect submit via
        # Enter key in the server terminal.  This is the primary way
        # to detect that Codex started processing a user message
        # typed directly in the server terminal.
        return True

    # -- Menu / option parsing -------------------------------------------

    @property
    def has_numbered_menus(self) -> bool:
        # Codex uses y/n approval prompts, not numbered menus.
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
        # Codex Rust TUI may handle paste differently than Ink.
        return 0.15

    @property
    def single_settle_time(self) -> float:
        return 0.05

    @property
    def image_prefix(self) -> str:
        return '@'

    @property
    def supports_image_attachments(self) -> bool:
        # Codex supports images via -i flag and clipboard paste,
        # but not via @path inline syntax.
        return False

    # -- Hook configuration ----------------------------------------------

    def configure_hooks(self, hook_script_path: str) -> None:
        """Install hooks into ~/.codex/hooks.json.

        Codex supports SessionStart and Stop events.  We configure a Stop
        hook that writes the idle state to the signal file.

        Also ensures the hooks feature flag is enabled in config.toml —
        without this, Codex ignores hooks.json entirely.

        The hook receives a JSON payload on stdin with:
        - session_id, transcript_path, cwd, hook_event_name, model,
          permission_mode, stop_hook_active, last_assistant_message
        """
        # Ensure hooks feature flag is enabled
        self._ensure_hooks_feature_flag()

        hooks_data: dict[str, Any] = {}
        if CODEX_HOOKS_FILE.exists():
            try:
                with open(CODEX_HOOKS_FILE, "r") as f:
                    hooks_data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        def make_entry(state: str) -> dict[str, Any]:
            return {
                "hooks": [{
                    "type": "command",
                    "command": f"{hook_script_path} {state}",
                    "timeout": 60,
                }]
            }

        def upsert(hook_list: list[dict[str, Any]], new_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
            cleaned = [
                e for e in hook_list
                if not any(HOOK_MARKER in h.get("command", "") for h in e.get("hooks", []))
            ]
            cleaned.extend(new_entries)
            return cleaned

        # Stop hook → writes "idle" state
        if "Stop" not in hooks_data:
            hooks_data["Stop"] = []
        hooks_data["Stop"] = upsert(hooks_data["Stop"], [make_entry("idle")])

        # Write hooks file
        CODEX_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CODEX_HOOKS_FILE, "w") as f:
            json.dump(hooks_data, f, indent=2)
            f.write("\n")

    @staticmethod
    def _ensure_hooks_feature_flag() -> None:
        """Ensure features.codex_hooks = true in ~/.codex/config.toml."""
        config_file = CODEX_CONFIG_DIR / "config.toml"
        CODEX_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config_text = ''
        if config_file.exists():
            config_text = config_file.read_text()
        if 'codex_hooks' in config_text:
            return
        with open(config_file, 'a') as f:
            f.write('\n[features]\ncodex_hooks = true\nsuppress_unstable_features_warning = true\n')

    # -- CLI-specific input behaviors ------------------------------------

    def select_option(
        self,
        option_num: int,
        options: dict[int, str],
        pty_send: Any,
        pty_sendline: Any,
    ) -> dict[str, Any]:
        """Handle approval in Codex's Ratatui TUI.

        Codex uses y/n style approval prompts, not numbered menus.
        option_num=1 is treated as 'approve' (y), option_num=2 as 'reject' (n).
        """
        if option_num == 1:
            pty_send('y')
            return {'status': 'sent'}
        elif option_num == 2:
            pty_send('n')
            return {'status': 'sent'}
        return {
            'status': 'error',
            'error': 'Codex uses y/n approval (option 1=yes, 2=no)',
        }

    def send_custom_answer(
        self,
        text: str,
        options: dict[int, str],
        pty_send: Any,
    ) -> dict[str, Any]:
        """Send text input in Codex's TUI.

        Codex's Ratatui composer accepts direct text input.
        """
        for ch in text:
            pty_send(ch)
            time.sleep(0.02)
        time.sleep(0.1)
        pty_send('\r')
        return {'status': 'sent'}
