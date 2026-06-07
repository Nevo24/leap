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
from leap.utils.atomic_write import atomic_write_json
from leap.utils.context_usage import ContextUsage, gemini_context_usage
from leap.utils.gemini_session_move import relocate_gemini_session
from leap.utils.resume_store import latest_transcript_for


GEMINI_CONFIG_DIR: Path = Path.home() / ".gemini"
GEMINI_SETTINGS_FILE: Path = GEMINI_CONFIG_DIR / "settings.json"

# Matches Gemini's top-level ``"sessionId": "<uuid>"`` field in the
# session JSON.  Used by :meth:`GeminiProvider.extract_session_id`.
_SESSION_ID_RE: re.Pattern[str] = re.compile(r'"sessionId"\s*:\s*"([^"]+)"')
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

    # -- Input history (CLI ↑/↓ recall) ----------------------------------

    def input_history(self, cwd: str) -> Optional[list[str]]:
        """Read ``~/.gemini/tmp/<slug>/logs.json`` for ``cwd`` and return
        the user-typed messages in chronological order.

        Gemini stores history per project, where the cwd→slug mapping
        lives in ``~/.gemini/projects.json``.  If the cwd isn't
        registered (user has never started a Gemini session there),
        return an empty list — matches what ↑ would surface in that
        cwd.  The logs.json shape is a JSON array of
        ``{sessionId, messageId, type, message, timestamp}``; we keep
        ``type == "user"`` entries.
        """
        registry_path = GEMINI_CONFIG_DIR / 'projects.json'
        try:
            registry = json.loads(registry_path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        projects = (registry.get('projects')
                    if isinstance(registry, dict) else None)
        if not isinstance(projects, dict):
            return None
        slug = projects.get(cwd)
        if not isinstance(slug, str) or not slug:
            return []  # cwd not registered → no history yet
        logs_path = GEMINI_CONFIG_DIR / 'tmp' / slug / 'logs.json'
        try:
            data = json.loads(logs_path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        out: list[str] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            if entry.get('type') != 'user':
                continue
            msg = entry.get('message')
            if isinstance(msg, str) and msg:
                out.append(msg)
        return out

    # -- Resume support --------------------------------------------------
    #
    # Despite what ``gemini --help`` advertises ("Use 'latest' for most
    # recent or index number"), Gemini's ``--resume`` also accepts a
    # full session UUID — per Google's docs and the community docs site.
    # Session files at ``~/.gemini/tmp/<project_hash>/chats/session-<date>-<short>.json``
    # store the full UUID in a top-level ``sessionId`` field, so we read
    # it from there when the hook fires.

    @property
    def supports_resume(self) -> bool:
        return True

    @property
    def requires_cwd_bound_resume(self) -> bool:
        # Gemini stores sessions under ~/.gemini/tmp/<slug>/chats/...
        # where <slug> derives from cwd via projects.json registry;
        # resume only locates the session when run from the matching
        # cwd (or after relocate_session moves it).
        return True

    def extract_session_id(self, hook_data: dict) -> Optional[str]:
        """Read Gemini's session UUID from the hook payload.

        Direct ``session_id`` / ``sessionId`` hook-payload fields come
        first (in case upstream later adds them).  Otherwise we peek at
        the head of the session JSON that Gemini writes at session start
        and regex-match the top-level ``sessionId`` field.

        Regex (not ``json.loads``) because Gemini session files grow
        unbounded — a busy session's JSON is >> 4 KiB, and a bounded
        read would produce a truncated-JSON ``JSONDecodeError``.  The
        field we need is always written near the top of the file in
        Gemini's serialiser output, so a 4 KiB head read + regex is
        both cheap and complete.
        """
        for key in ('session_id', 'sessionId'):
            sid = hook_data.get(key) or ''
            if sid:
                return sid
        path = hook_data.get('transcript_path', '') or ''
        if not path or '.gemini/' not in path:
            return None
        try:
            with open(path, 'r') as f:
                head = f.read(4096)
        except OSError:
            return None
        m = _SESSION_ID_RE.search(head)
        return m.group(1) if m else None

    def resume_args(self, session_id: str) -> list[str]:
        # Gemini's flag takes a value as a second token; Leap's server
        # argv forwarder keeps that value (no ``--``-only filter).
        return ['--resume', session_id]

    @property
    def supports_context_usage(self) -> bool:
        return True

    def context_usage(self, cli_name: str, tag: str,
                      storage_dir: Path) -> Optional[ContextUsage]:
        """Context-window usage from the latest ``gemini`` chat-session turn."""
        transcript = latest_transcript_for(storage_dir, cli_name, tag)
        return gemini_context_usage(transcript) if transcript else None

    def relocate_session(
        self,
        session_id: str,
        src_cwd: str,
        dst_cwd: str,
        *,
        transcript_path: str = '',  # unused — Gemini locates by sessionId
        on_committed: Optional[Any] = None,
    ) -> Optional[str]:
        """Move a Gemini session JSONL from src_cwd's slug to dst_cwd's.

        Gemini stores sessions under
        ``~/.gemini/tmp/<slug>/chats/session-<ts>-<short>.jsonl`` with
        ``~/.gemini/projects.json`` mapping ``cwd → slug``.  Resume
        running in a different cwd doesn't transparently work because
        Gemini only looks under the slug for *its* cwd — so we
        physically move the single session file into ``dst_cwd``'s
        slug dir and update the registry.  See
        :mod:`leap.utils.gemini_session_move` for the safety
        properties (signal-blocked, atomic, verified).
        """
        return relocate_gemini_session(
            session_id, src_cwd, dst_cwd, on_committed=on_committed,
        )

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

        atomic_write_json(GEMINI_SETTINGS_FILE, settings)

    def hooks_installed(self) -> bool:
        """True iff ``~/.gemini/leap-hook.sh`` exists AND
        ``~/.gemini/settings.json`` references it from any hook entry.

        Wrapped in a broad try/except so any unexpected shape in the
        settings file returns False instead of crashing the gate.
        """
        try:
            hook_script = self.hook_config_dir / "leap-hook.sh"
            if not hook_script.is_file():
                return False
            with open(GEMINI_SETTINGS_FILE, "r") as f:
                settings = json.load(f)
            hooks = settings.get("hooks") if isinstance(settings, dict) else None
            if not isinstance(hooks, dict):
                return False
            for entries in hooks.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    inner = entry.get("hooks")
                    if not isinstance(inner, list):
                        continue
                    for h in inner:
                        if not isinstance(h, dict):
                            continue
                        cmd = h.get("command")
                        if isinstance(cmd, str) and HOOK_MARKER in cmd:
                            return True
            return False
        except Exception:
            return False

    def deconfigure_hooks(self) -> None:
        """Remove Leap's hook entries from ~/.gemini/settings.json."""
        try:
            if GEMINI_SETTINGS_FILE.is_file():
                with open(GEMINI_SETTINGS_FILE) as f:
                    settings = json.load(f)
                if isinstance(settings, dict):
                    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else None
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
                                    and any(
                                        isinstance(h, dict)
                                        and any(m in h.get("command", "") for m in _MARKERS)
                                        for h in e.get("hooks", [])
                                    )
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
                                settings.pop("hooks", None)
                            atomic_write_json(GEMINI_SETTINGS_FILE, settings)
        except Exception:
            pass
        super().deconfigure_hooks()

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
