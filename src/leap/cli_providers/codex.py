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
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.base import CLIProvider
from leap.cli_providers.states import SIGNAL_STATES
from leap.utils.atomic_write import atomic_write_json, atomic_write_text


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
        return 'OpenAI Codex'

    # -- State detection patterns ----------------------------------------

    @property
    def interrupted_pattern(self) -> bytes:
        # Codex outputs: "■ Conversation interrupted - tell the model
        # what to do differently."
        # After ANSI stripping (no space removal), "interrupted" appears.
        return b'interrupted'

    @property
    def confirmed_interrupt_pattern(self) -> Optional[bytes]:
        # Codex's interrupt prompt: "■ Conversation interrupted - tell
        # the model what to do differently."  In compact form (spaces
        # removed), "Conversationinterrupted" is specific enough to
        # distinguish from conversational use of "interrupted".
        return b'Conversationinterrupted'

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
    def transcript_sessions_dir(self) -> Optional[Path]:
        return CODEX_CONFIG_DIR / 'sessions'

    @property
    def cursor_hidden_while_idle(self) -> bool:
        # Ratatui hides the terminal cursor permanently and renders
        # its own cursor.  Cursor-hidden detection for auto-resume
        # would false-trigger on every idle redraw.
        return True

    @property
    def silence_timeout(self) -> Optional[float]:
        # Ratatui outputs every ~100ms during processing (spinner,
        # thinking counter, response text).  An 8-second silence gap
        # reliably indicates idle — much faster than the 15s default
        # which is tuned for Claude Code's longer pauses between tools.
        # 8s allows for brief LLM thinking pauses without false idle.
        return 8.0

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

    # -- Input history (CLI ↑/↓ recall) ----------------------------------

    def input_history(self, cwd: str) -> Optional[list[str]]:
        """Read ``~/.codex/history.jsonl`` and return all entries in
        chronological order (oldest → newest).

        Codex's history is global (not cwd-filtered), so ``cwd`` is
        ignored — matching what the user sees in the CLI's own ↑.
        Each line is ``{"session_id", "ts", "text"}`` and the file is
        append-ordered by submit time.
        """
        del cwd  # Codex history is global
        path = CODEX_CONFIG_DIR / 'history.jsonl'
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        out: list[str] = []
        for line in raw.split(b'\n'):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            text = entry.get('text')
            if isinstance(text, str) and text:
                out.append(text)
        return out

    # -- Resume support --------------------------------------------------

    @property
    def supports_resume(self) -> bool:
        return True

    @property
    def requires_cwd_bound_resume(self) -> bool:
        # Codex sessions are UUID-keyed (``codex resume <uuid>`` works
        # from any cwd, no file move required).  We still surface the
        # "Original / Current" cwd-choice prompt to keep the UX
        # symmetric with the cwd-bound CLIs and so :meth:`relocate_session`
        # below can update Leap's recorded cwd in
        # ``cli_sessions/codex/<tag>.json`` *immediately* on "Stay in
        # current" — without waiting for the next hook fire to
        # self-heal the record.
        return True

    def extract_session_id(self, hook_data: dict) -> Optional[str]:
        """Codex passes ``session_id`` directly in the hook payload.

        Fallback: peek at the first JSONL line of ``transcript_path`` and
        read ``payload.id`` from the ``session_meta`` record, for robustness
        in case older Codex versions omit ``session_id``.
        """
        sid = hook_data.get('session_id', '') or ''
        if sid:
            return sid
        path = hook_data.get('transcript_path', '') or ''
        if not path or '.codex/sessions/' not in path:
            return None
        try:
            with open(path, 'r') as f:
                first = f.readline()
            if not first.strip():
                return None
            entry = json.loads(first)
            if entry.get('type') != 'session_meta':
                return None
            return entry.get('payload', {}).get('id') or None
        except (OSError, json.JSONDecodeError):
            return None

    def resume_args(self, session_id: str) -> list[str]:
        # Codex resume is a subcommand, not a flag: ``codex resume <uuid>``.
        #
        # We also prepend ``-C <cwd>`` (codex's "use this dir as working
        # root") so codex doesn't ask its own
        # *"Choose working directory to resume this session"* picker on
        # startup — that prompt fires whenever codex's recorded
        # ``session_cwd`` differs from its launch cwd, which would
        # double-prompt the user after they already answered leap's
        # *"CD into the original / Stay in the current"* prompt.
        #
        # ``os.getcwd()`` is captured here at server-startup time, after
        # ``leap-resume.py`` has chdir'd to whichever cwd the user
        # picked.  Passing the same dir to codex via ``-C`` makes
        # codex's "current" match its "session" → no prompt.  If the
        # user passes their own ``-C`` in flags, codex uses last-wins
        # so theirs takes precedence.
        return ['-C', os.getcwd(), 'resume', session_id]

    def relocate_session(
        self,
        session_id: str,
        src_cwd: str,
        dst_cwd: str,
        *,
        transcript_path: str = '',
        on_committed: Optional[Any] = None,
    ) -> Optional[str]:
        """Logical-only relocation: no files move, just bump the recorded cwd.

        Codex stores sessions at
        ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`` —
        date+UUID-keyed, *not* cwd-derived — so ``codex resume <uuid>``
        finds the session from any cwd.  No transcript move is needed.

        We still implement this hook (rather than inheriting the base
        ``return None``) so the picker treats Codex symmetrically with
        the cwd-bound CLIs: when the user picks "Stay in current",
        ``on_committed`` is fired with the unchanged transcript path —
        the caller's bookkeeping callback then rewrites
        ``cli_sessions/codex/<tag>.json`` to point at ``dst_cwd``.
        Without this, the recorded cwd would only catch up on the next
        hook fire after a resumed turn.

        Returns the unchanged transcript path on success; ``None`` if
        the caller didn't supply one or the file is gone.
        """
        if not transcript_path or not os.path.isfile(transcript_path):
            return None
        if on_committed is not None:
            try:
                on_committed(transcript_path)
            except Exception:
                # No actual move happened, so bookkeeping failure isn't
                # catastrophic — caller gets the path back, the
                # records will self-heal on next hook fire.  We
                # deliberately swallow rather than raise so the
                # "logical move" surface still looks succeeded.
                pass
        return transcript_path

    # -- Hook configuration ----------------------------------------------

    @property
    def hook_config_dir(self) -> Path:
        return CODEX_CONFIG_DIR

    @property
    def requires_binary_for_hooks(self) -> bool:
        return True

    def configure_hooks(self, hook_script_path: str) -> None:
        """Install hooks into ~/.codex/hooks.json.

        **Schema note (Codex 0.121+):** events must be nested under a
        top-level ``"hooks"`` key — ``{"hooks": {"Stop": [...]}}``.  The
        flat form ``{"Stop": [...]}`` is silently ignored (no error, no
        log, hooks simply never fire).  We also tolerate legacy flat
        configs written by older Leap versions by lifting them into the
        nested shape.

        Also ensures the hooks feature flag is enabled in config.toml —
        without it, Codex ignores hooks.json entirely.

        The hook receives a JSON payload on stdin with:
        - session_id, transcript_path, cwd, hook_event_name, model,
          permission_mode, stop_hook_active, last_assistant_message
        """
        # Ensure hooks feature flag is enabled
        self._ensure_hooks_feature_flag()

        raw: dict[str, Any] = {}
        if CODEX_HOOKS_FILE.exists():
            try:
                with open(CODEX_HOOKS_FILE, "r") as f:
                    raw = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        # Normalise legacy flat configs (`{"Stop": [...]}`) into the
        # modern nested shape so we never re-write a file in the broken
        # form.  Any top-level key that's a known event name moves in.
        _EVENT_KEYS = {"Stop", "SessionStart", "PreToolUse", "PostToolUse",
                       "UserPromptSubmit", "Notification"}
        events: dict[str, Any] = raw.get("hooks") if isinstance(raw.get("hooks"), dict) else {}
        for k in list(raw.keys()):
            if k in _EVENT_KEYS:
                events.setdefault(k, raw.pop(k))

        def make_entry(state: str) -> dict[str, Any]:
            return {
                "hooks": [{
                    "type": "command",
                    "command": f"{hook_script_path} {state}",
                    "timeout": 60,
                }]
            }

        def upsert(hook_list: list[dict[str, Any]], new_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
            legacy_marker = "claudeq-hook.sh"
            cleaned = [
                e for e in hook_list
                if not any(
                    HOOK_MARKER in h.get("command", "") or legacy_marker in h.get("command", "")
                    for h in e.get("hooks", [])
                )
            ]
            cleaned.extend(new_entries)
            return cleaned

        # Stop hook → writes "idle" state
        events.setdefault("Stop", [])
        events["Stop"] = upsert(events["Stop"], [make_entry("idle")])

        raw["hooks"] = events

        # Write hooks file atomically (temp + fsync + rename) so a
        # concurrent reader (the session-start gate) never sees a
        # half-written file mid-rewrite.
        atomic_write_json(CODEX_HOOKS_FILE, raw)

    def hooks_installed(self) -> bool:
        """True iff all three pieces of Codex's hook setup are present:
        the hook script in ``~/.codex/``, a ``leap-hook.sh`` reference
        in ``hooks.json``, AND the ``codex_hooks`` feature flag in
        ``config.toml`` (without the flag, Codex silently ignores
        hooks.json).

        Wrapped in a broad try/except so any unexpected shape in the
        settings files returns False instead of crashing the gate.
        """
        try:
            hook_script = self.hook_config_dir / "leap-hook.sh"
            if not hook_script.is_file():
                return False
            # Feature flag in config.toml — without it, hooks never fire.
            config_file = CODEX_CONFIG_DIR / "config.toml"
            config_text = config_file.read_text() if config_file.exists() else ""
            if "codex_hooks" not in config_text:
                return False
            # Reference in hooks.json (under the nested "hooks" key — flat
            # form is silently ignored by Codex 0.121+, so we treat it as
            # not-installed too).
            with open(CODEX_HOOKS_FILE, "r") as f:
                raw = json.load(f)
            events = raw.get("hooks") if isinstance(raw, dict) else None
            if not isinstance(events, dict):
                return False
            for entries in events.values():
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

    @staticmethod
    def _ensure_hooks_feature_flag() -> None:
        """Ensure features.codex_hooks = true in ~/.codex/config.toml.

        Read-modify-write done atomically (read existing, concat new
        block, atomic-write the union) so a concurrent reader (the
        session-start gate calling :meth:`hooks_installed`) never
        sees a half-appended config file mid-rewrite.
        """
        config_file = CODEX_CONFIG_DIR / "config.toml"
        config_text = ''
        if config_file.exists():
            config_text = config_file.read_text()
        if 'codex_hooks' in config_text:
            return
        new_text = (
            config_text
            + '\n[features]\ncodex_hooks = true\n'
            + 'suppress_unstable_features_warning = true\n'
        )
        atomic_write_text(config_file, new_text)

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
