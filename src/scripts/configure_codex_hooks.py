#!/usr/bin/env python3
"""Configure Codex CLI hooks for ClaudeQ state detection.

Merges ClaudeQ hook entries into ~/.codex/hooks.json so that
Codex calls claudeq-hook.sh on Stop events.

Codex hook format:
    { "Stop": [{ "hooks": [{ "type": "command", "command": "...", "timeout": 60 }] }] }

Unlike Claude Code, Codex doesn't have a Notification hook, so
permission/question state detection relies on PTY output parsing.
"""

import json
import os
import sys
from pathlib import Path


CODEX_HOOKS_FILE = Path.home() / ".codex" / "hooks.json"
HOOK_MARKER = "claudeq-hook.sh"


def _load_hooks() -> dict:
    """Load existing Codex hooks or return empty dict."""
    if not CODEX_HOOKS_FILE.exists():
        return {}
    try:
        with open(CODEX_HOOKS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_hooks(hooks: dict) -> None:
    """Write hooks back to disk."""
    CODEX_HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CODEX_HOOKS_FILE, "w") as f:
        json.dump(hooks, f, indent=2)
        f.write("\n")


def _make_entry(hook_path: str, state: str) -> dict:
    """Build a single hook entry in Codex's format."""
    return {
        "hooks": [
            {
                "type": "command",
                "command": f"{hook_path} {state}",
                "timeout": 60,
            }
        ]
    }


def _is_claudeq_entry(entry: dict) -> bool:
    """Check if a hook entry belongs to ClaudeQ."""
    for h in entry.get("hooks", []):
        if HOOK_MARKER in h.get("command", ""):
            return True
    return False


def _upsert_entries(hooks_list: list, new_entries: list) -> list:
    """Remove all existing ClaudeQ entries and append new ones."""
    cleaned = [e for e in hooks_list if not _is_claudeq_entry(e)]
    cleaned.extend(new_entries)
    return cleaned


def configure_hooks(hook_path: str) -> None:
    """Merge ClaudeQ hook entries into Codex hooks."""
    hooks = _load_hooks()

    # Stop hook → writes "idle" state
    if "Stop" not in hooks:
        hooks["Stop"] = []
    hooks["Stop"] = _upsert_entries(hooks["Stop"], [
        _make_entry(hook_path, "idle"),
    ])

    _save_hooks(hooks)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: configure_codex_hooks.py <path-to-claudeq-hook.sh>")
        sys.exit(1)

    hook_path = sys.argv[1]
    if not os.path.isfile(hook_path):
        print(f"Error: Hook script not found: {hook_path}")
        sys.exit(1)

    configure_hooks(hook_path)
    print(f"  Configured Codex hooks -> {CODEX_HOOKS_FILE}")


if __name__ == "__main__":
    main()
