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
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.base import CLIProvider
from leap.cli_providers.states import SIGNAL_STATES
from leap.utils.atomic_write import atomic_write_json
from leap.utils.cursor_session_move import find_chat_dir, relocate_cursor_session


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

    # -- Input history (CLI ↑/↓ recall) ----------------------------------

    def input_history(self, cwd: str) -> Optional[list[str]]:
        """Read ``~/.cursor/prompt_history.json`` (plain JSON array of
        strings, oldest first) and return it unchanged.

        Cursor's history is global — ``cwd`` is ignored, matching the
        CLI's own ↑ behavior.
        """
        del cwd  # Cursor history is global
        path = CURSOR_CONFIG_DIR / 'prompt_history.json'
        try:
            # Pin UTF-8 and catch decode errors: read_text() with the
            # platform default would raise UnicodeDecodeError (a ValueError,
            # NOT an OSError) on a non-UTF-8 locale, silently disabling ↑/↓
            # recall.  Sibling providers already guard this.
            raw = path.read_text(encoding='utf-8')
        except (OSError, ValueError):
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, list):
            return None
        return [s for s in data if isinstance(s, str) and s]

    # -- Hook configuration ----------------------------------------------

    # -- Resume support --------------------------------------------------
    #
    # **Heads-up: Cursor gates `~/.cursor/hooks.json` behind a server-side
    # feature flag (`claude_code_hooks_enabled`, visible in the
    # cursor-agent binary's protobuf schema).  On plans where the flag
    # isn't enabled, Cursor silently never fires any hook — not even for
    # a perfectly-valid schema.  That means the resume recording below
    # depends on the user's Cursor plan; on free plans our `stop` hook
    # won't fire and the picker won't show a `[Cursor Agent]` row.**
    #
    # The provider implements the full protocol anyway so that accounts
    # with the flag enabled get the feature end-to-end, and so any future
    # change by Cursor (e.g. universal hooks) will just start working.

    @property
    def supports_resume(self) -> bool:
        return True

    @property
    def requires_cwd_bound_resume(self) -> bool:
        # Cursor stores chats under ~/.cursor/chats/<MD5(workspace)>/<chatId>/
        # so `cursor-agent --resume <chatId>` only finds the session
        # when run from a cwd whose MD5 matches the chat's project dir
        # (or after relocate_session moves it).
        return True

    def session_exists(self, session_id: str, cwd: str) -> bool:
        """Cursor records have ``transcript_path=""`` so the picker's
        path-based stale filter never fires.  Verify here by scanning
        for the chat dir under any project hash — same fallback the
        relocator uses, so a chat that cursor's workspace-root walk
        landed under a parent's hash is still reachable.
        """
        return find_chat_dir(session_id, prefer_cwd=cwd) is not None

    def extract_session_id(self, hook_data: dict) -> Optional[str]:
        """Cursor Agent stores chats under
        ``~/.cursor/chats/<project-hash>/<chat-uuid>/`` and accepts the
        chat UUID via ``--resume <chatId>``.

        Per Cursor's official docs the ``stop`` hook stdin payload
        carries the chat UUID as ``conversation_id``; older builds may
        also send ``chatId`` / ``chat_id`` / ``session_id``.  We check
        all of them, then fall back to parsing the UUID out of any
        transcript path under ``~/.cursor/chats/<project>/<uuid>/``.
        """
        for key in ('conversation_id', 'chatId', 'chat_id', 'session_id'):
            sid = hook_data.get(key)
            # Must be a non-empty STRING.  A truthy non-string (a JSON
            # array/object from an odd hook payload) would otherwise be
            # returned, then later joined into a Path by session_exists ->
            # find_chat_dir, raising TypeError and crashing the whole
            # `leap --resume` picker (for every CLI, not just Cursor).
            if isinstance(sid, str) and sid:
                return sid
        path = hook_data.get('transcript_path', '') or ''
        if path and '.cursor/' in path:
            # expected: .../chats/<project>/<chat-uuid>/... or
            # .../chats/<project>/<chat-uuid>.jsonl
            parts = Path(path).parts
            for i, p in enumerate(parts):
                if p == 'chats' and i + 2 < len(parts):
                    candidate = parts[i + 2]
                    if candidate.endswith('.jsonl'):
                        candidate = candidate[:-6]
                    return candidate or None
        return None

    def resume_args(self, session_id: str) -> list[str]:
        # Cursor Agent: `cursor-agent --resume <chatId>` (space form).
        # The flag/value pair flows through the server since the new
        # argv forwarder no longer drops non-`--` tokens.
        return ['--resume', session_id]

    def relocate_session(
        self,
        session_id: str,
        src_cwd: str,
        dst_cwd: str,
        *,
        transcript_path: str = '',  # unused — Cursor locates by chat-dir
        on_committed: Optional[Any] = None,
    ) -> Optional[str]:
        """Move ``~/.cursor/chats/<MD5(src)>/<chatId>/`` to the dst hash.

        Delegates to :mod:`leap.utils.cursor_session_move` for the
        atomic copy-verify-rename dance.  Returns the new chat-dir
        path on success or ``None`` when the chat couldn't be located
        (caller falls back to ``chdir`` into the recorded cwd).
        """
        return relocate_cursor_session(
            session_id, src_cwd, dst_cwd, on_committed=on_committed,
        )

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
                    loaded = json.load(f)
                # Only adopt a dict-shaped file.  A malformed root (list /
                # string / number) would make the ``hooks_data["hooks"]``
                # assignment below raise TypeError and abort the whole
                # `leap --reconfigure` / `make install` run; keep the fresh
                # default instead and rewrite the file into valid shape.
                # ValueError covers UnicodeDecodeError on a non-UTF-8 file.
                if isinstance(loaded, dict):
                    hooks_data = loaded
            except (json.JSONDecodeError, OSError, ValueError):
                pass

        # Defensive shape-coercion: every nested container we index must be
        # the expected type even if a hand-edited file got them wrong.
        if not isinstance(hooks_data.get("hooks"), dict):
            hooks_data["hooks"] = {}
        if "version" not in hooks_data:
            hooks_data["version"] = 1

        hooks = hooks_data["hooks"]

        # Stop hook → writes "idle" state to signal file
        if not isinstance(hooks.get("stop"), list):
            hooks["stop"] = []

        legacy_marker = "claudeq-hook.sh"
        hooks["stop"] = [
            e for e in hooks["stop"]
            if isinstance(e, dict)
            and not (HOOK_MARKER in e.get("command", "")
                     or legacy_marker in e.get("command", ""))
        ]
        hooks["stop"].append({
            "command": f"{hook_script_path} idle",
        })

        atomic_write_json(CURSOR_HOOKS_FILE, hooks_data)

    def hooks_installed(self) -> bool:
        """True iff ``~/.cursor/leap-hook.sh`` exists AND
        ``~/.cursor/hooks.json`` references it from any hook entry.

        Cursor's schema is flatter than Claude/Gemini — entries are
        ``{"command": "..."}`` directly, with no nested ``"hooks"``
        list, and event names are lowercase.

        Wrapped in a broad try/except so any unexpected shape in the
        settings file returns False instead of crashing the gate.
        """
        try:
            hook_script = self.hook_config_dir / "leap-hook.sh"
            if not hook_script.is_file():
                return False
            with open(CURSOR_HOOKS_FILE, "r") as f:
                data = json.load(f)
            hooks = data.get("hooks") if isinstance(data, dict) else None
            if not isinstance(hooks, dict):
                return False
            for entries in hooks.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    cmd = entry.get("command")
                    if isinstance(cmd, str) and HOOK_MARKER in cmd:
                        return True
            return False
        except Exception:
            return False

    def deconfigure_hooks(self) -> None:
        """Remove Leap's hook entries from ~/.cursor/hooks.json."""
        try:
            if CURSOR_HOOKS_FILE.is_file():
                with open(CURSOR_HOOKS_FILE) as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else None
                    if isinstance(hooks, dict):
                        _MARKERS = (HOOK_MARKER, "claudeq-hook.sh")
                        changed = False
                        for event in list(hooks.keys()):
                            entries = hooks.get(event)
                            if not isinstance(entries, list):
                                continue
                            cleaned = [
                                e for e in entries
                                if not (
                                    isinstance(e, dict)
                                    and any(m in e.get("command", "") for m in _MARKERS)
                                )
                            ]
                            if len(cleaned) != len(entries):
                                if cleaned:
                                    hooks[event] = cleaned
                                else:
                                    del hooks[event]
                                changed = True
                        if changed:
                            if not hooks:
                                data.pop("hooks", None)
                            atomic_write_json(CURSOR_HOOKS_FILE, data)
        except Exception:
            pass
        super().deconfigure_hooks()

    # -- CLI binary lookup -----------------------------------------------

    def find_cli(self) -> Optional[str]:
        """Find cursor-agent in PATH or common install location."""
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
