"""
Claude Code CLI provider.

Implements the CLIProvider interface for Anthropic's Claude Code CLI.
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.base import CLIProvider
from leap.utils.atomic_write import atomic_write_json
from leap.utils.claude_session_move import relocate_claude_session, slugify
from leap.utils.menu import MENU_OPTION_RE


_TRANSCRIPT_TAIL_BYTES = 32768
_TRANSCRIPT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"


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

    @property
    def running_indicator_patterns(self) -> list[bytes]:
        # Claude's "Compacting conversation…" spinner is shown during
        # both the /compact slash command and auto-compact between turns.
        # No hook fires for compaction, and between-turns auto-compact
        # starts immediately after a Stop hook has already written
        # ``idle`` — without this indicator the session would read as
        # idle even though Claude is still working.  In compact form
        # (spaces+newlines removed), "Compactingconversation" is
        # specific enough to avoid colliding with conversational text.
        return [b'Compactingconversation']

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
        return MENU_OPTION_RE

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

    @property
    def requires_cwd_bound_resume(self) -> bool:
        # Claude stores transcripts under ~/.claude/projects/<cwd-slug>/<uuid>.jsonl;
        # `claude --resume=<uuid>` only finds the session when run from
        # the matching cwd, so leap must offer the cwd-choice picker.
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

    def relocate_session(
        self,
        session_id: str,
        src_cwd: str,
        dst_cwd: str,
        *,
        transcript_path: str = '',  # unused — Claude derives path from slug
        on_committed: Optional[Any] = None,
    ) -> Optional[str]:
        return relocate_claude_session(
            session_id, src_cwd, dst_cwd, on_committed=on_committed,
        )

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
            chunk = _TRANSCRIPT_TAIL_BYTES
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

    # -- Transcript-based "still running" check --------------------------

    @property
    def transcript_projects_root(self) -> Path:
        """Root directory for Claude session transcripts.

        Claude stores each session at
        ``<root>/<slug(cwd)>/<session_id>.jsonl``.  Tests override this
        property to redirect to a tmp_path; production reads from
        ``~/.claude/projects/``.
        """
        return _TRANSCRIPT_PROJECTS_ROOT

    def transcript_says_running(
        self,
        since: float,
        cwd: str,
        tag: str = '',
        storage_dir: Optional[Path] = None,
    ) -> bool:
        """True iff the transcript shows an in-flight ``tool_use``
        from the current turn.

        Hybrid file lookup:
          1. ``cli_sessions/claude/<tag>.json`` for the most recent
             recorded ``session_id`` (populated by the Stop hook).
          2. mtime fallback: most recently modified ``*.jsonl`` in
             the cwd's slug directory.
        """
        project_dir = self.transcript_projects_root / slugify(cwd)
        if not project_dir.is_dir():
            return False

        transcript = self._resolve_transcript_path(
            project_dir, tag, storage_dir,
        )
        if transcript is None:
            return False

        try:
            size = transcript.stat().st_size
        except OSError:
            return False
        try:
            with open(transcript, 'rb') as f:
                f.seek(max(0, size - _TRANSCRIPT_TAIL_BYTES))
                tail = f.read()
        except OSError:
            return False

        # Walk back to the most recent assistant entry; its stop_reason
        # tells us whether the agent loop is still in tool-use mode.
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
            ts_str = entry.get('timestamp', '')
            if not ts_str:
                return False
            try:
                ts = datetime.fromisoformat(
                    ts_str.replace('Z', '+00:00'),
                ).timestamp()
            except (ValueError, TypeError):
                return False
            # Stale entry from a previous turn — current turn hasn't
            # produced an assistant entry yet, so we can't tell.
            if ts <= since:
                return False
            stop_reason = entry.get('message', {}).get('stop_reason', '')
            return stop_reason == 'tool_use'
        return False

    def _resolve_transcript_path(
        self,
        project_dir: Path,
        tag: str,
        storage_dir: Optional[Path],
    ) -> Optional[Path]:
        """Pick the active transcript: recorded ``session_id`` first,
        most-recently-modified ``*.jsonl`` second.

        Returns ``None`` when neither yields a readable file.
        """
        if tag and storage_dir is not None:
            tag_file = (
                storage_dir / 'cli_sessions' / 'claude' / f'{tag}.json'
            )
            sid = self._latest_session_id(tag_file)
            if sid:
                candidate = project_dir / f'{sid}.jsonl'
                if candidate.is_file():
                    return candidate

        # Fallback: most recently modified .jsonl in the slug dir.
        try:
            best: Optional[Path] = None
            best_mtime: float = 0
            for f in project_dir.iterdir():
                if f.suffix != '.jsonl':
                    continue
                try:
                    mt = f.stat().st_mtime
                except OSError:
                    continue
                if mt > best_mtime:
                    best = f
                    best_mtime = mt
            return best
        except OSError:
            return None

    @staticmethod
    def _latest_session_id(tag_file: Path) -> str:
        """Read the most recent recorded session_id from a tag file.

        ``cli_sessions/claude/<tag>.json`` is a list of records ordered
        oldest-first by Leap's hook.  Walk from the end and return the
        first entry's ``session_id``.  Empty string on any failure.
        """
        if not tag_file.is_file():
            return ''
        try:
            data = json.loads(tag_file.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            return ''
        if not isinstance(data, list):
            return ''
        for entry in reversed(data):
            if not isinstance(entry, dict):
                continue
            sid = entry.get('session_id', '')
            if isinstance(sid, str) and sid:
                return sid
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

        # SessionStart(resume) — fires on `/resume` inside a running Claude
        # and on `claude --resume=<id>` startup.  Without it, a user who
        # loads a past session but exits before sending a message never
        # triggers Stop, so the session id is never recorded and
        # `leap --resume` can't see it.  Matcher "startup" is intentionally
        # omitted so abandoned fresh sessions don't clutter the picker.
        if "SessionStart" not in hooks:
            hooks["SessionStart"] = []
        hooks["SessionStart"] = upsert(hooks["SessionStart"], [
            make_entry("idle", matcher="resume"),
        ])

        atomic_write_json(settings_path, settings)

    def hooks_installed(self) -> bool:
        """True iff ``~/.claude/hooks/leap-hook.sh`` exists AND
        ``~/.claude/settings.json`` references it from any hook entry.

        Wrapped in a broad try/except so any unexpected shape in the
        settings file (e.g. a ``command`` that's a non-string scalar)
        returns False instead of crashing the session-start gate.
        """
        try:
            hook_script = self.hook_config_dir / "leap-hook.sh"
            if not hook_script.is_file():
                return False
            settings_path = Path.home() / ".claude" / "settings.json"
            with open(settings_path, "r") as f:
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
                        if isinstance(cmd, str) and "leap-hook.sh" in cmd:
                            return True
            return False
        except Exception:
            return False

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
