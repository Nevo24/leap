"""
Claude Code CLI provider.

Implements the CLIProvider interface for Anthropic's Claude Code CLI.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

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
        return 'Claude Code'

    # -- State detection patterns ----------------------------------------

    @property
    def interrupted_pattern(self) -> bytes:
        return b'Interrupted'

    @property
    def confirmed_interrupt_pattern(self) -> Optional[bytes]:
        # Disabled: pattern matching on raw PTY buffers is unreliable for
        # Ink TUI — full-screen redraws include scrollback content, and
        # after ANSI stripping + space removal, unrelated text (commit
        # messages, code, conversation) containing "Interrupted" near a
        # middle dot (common TUI decoration) falsely matches.
        #
        # Interrupt detection for Claude relies on the _interrupt_pending
        # flag (requires Escape/Ctrl+C before the Stop hook fires).
        # Self-interrupts (tool timeouts) are covered by the Notification
        # hook writing needs_input for the interrupt dialog.
        return None

    @property
    def dialog_patterns(self) -> list[bytes]:
        return [b'Entertoselect', b'Esctocancel']

    def _has_numbered_menu(self, compact_text: str) -> bool:
        """Check for numbered menu cursor indicator (❯ or ›) before option 1."""
        # ❯ = U+276F, › = U+203A — both used by Ink TUI
        return '\u276f1.' in compact_text or '\u203a1.' in compact_text

    def has_dialog_indicator(self, compact_text: str) -> bool:
        """Lenient: standard footer patterns OR numbered menu cursor."""
        if super().has_dialog_indicator(compact_text):
            return True
        return self._has_numbered_menu(compact_text)

    def is_dialog_certain(self, compact_text: str) -> bool:
        """Strict: all standard footer patterns OR numbered menu cursor."""
        if super().is_dialog_certain(compact_text):
            return True
        return self._has_numbered_menu(compact_text)

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

    # -- Resume support --------------------------------------------------

    @property
    def supports_resume(self) -> bool:
        return True

    def extract_session_id(self, hook_data: dict) -> Optional[str]:
        """Claude Code's session id is the basename of ``transcript_path``
        (``~/.claude/projects/<slug>/<uuid>.jsonl``).  The ``.claude/projects/``
        substring check guards against cross-contamination if a different
        CLI's hook runs with Claude set as the ``LEAP_CLI_PROVIDER``.
        """
        path = hook_data.get('transcript_path', '') or ''
        if not path or '.claude/projects/' not in path:
            return None
        name = os.path.basename(path)
        if name.endswith('.jsonl'):
            name = name[:-6]
        return name or None

    def resume_args(self, session_id: str) -> list[str]:
        # Must be the single-token `=` form — leap-server.py's flag filter
        # drops any argv element that doesn't start with `--`, so the
        # space-separated form would lose the UUID and make claude open
        # its own picker instead of resuming directly.
        return [f'--resume={session_id}']

    # -- Last assistant message (Slack) ----------------------------------

    def extract_last_assistant_message(self, hook_data: dict) -> str:
        """Claude doesn't pass the assistant text in the hook payload —
        tail the transcript JSONL and pull the most recent
        ``type=="assistant"`` entry's concatenated text parts.
        Bounded to the last 32 KiB so very long transcripts stay cheap.
        """
        path = hook_data.get('transcript_path', '') or ''
        if not path or '.claude/projects/' not in path:
            return ''
        try:
            size = os.path.getsize(path)
        except OSError:
            return ''
        try:
            chunk = 32768
            with open(path, 'rb') as f:
                f.seek(max(0, size - chunk))
                tail = f.read()
        except OSError:
            return ''
        for raw in reversed(tail.split(b'\n')):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if entry.get('type') != 'assistant':
                continue
            parts = [
                c.get('text', '')
                for c in entry.get('message', {}).get('content', [])
                if c.get('type') == 'text'
            ]
            joined = '\n'.join(p for p in parts if p)
            if joined:
                return joined
        return ''

    # -- Hook configuration ----------------------------------------------

    @property
    def hook_config_dir(self) -> Path:
        return Path.home() / ".claude" / "hooks"

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
            legacy_marker = "claudeq-hook.sh"
            cleaned = [
                e for e in hook_list
                if not any(
                    marker in h.get("command", "") or legacy_marker in h.get("command", "")
                    for h in e.get("hooks", [])
                )
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
            make_entry("needs_input", matcher="elicitation_dialog"),
        ])

        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")

    # -- CLI-specific input behaviors ------------------------------------

    # send_image_message: uses base class fixed-sleep protocol

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
