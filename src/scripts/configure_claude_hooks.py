#!/usr/bin/env python3
"""Configure Claude Code hooks for ClaudeQ state detection.

Merges ClaudeQ hook entries into ~/.claude/settings.json so that
Claude Code calls claudeq-hook.sh on Stop and Notification events.

Hook entries use the nested format required by Claude Code:
    {matcher: "...", hooks: [{type: "command", command: "..."}]}

Three entries are created:
    Stop           -> claudeq-hook.sh idle
    Notification   -> claudeq-hook.sh needs_permission  (matcher: permission_prompt)
    Notification   -> claudeq-hook.sh has_question       (matcher: elicitation_dialog)
"""

import json
import os
import sys
from pathlib import Path


CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_MARKER = "claudeq-hook.sh"


def _load_settings() -> dict:
    """Load existing Claude settings or return empty dict."""
    if not CLAUDE_SETTINGS.exists():
        return {}
    try:
        with open(CLAUDE_SETTINGS, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_settings(settings: dict) -> None:
    """Write settings back to disk."""
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    with open(CLAUDE_SETTINGS, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def _make_entry(hook_path: str, state: str, matcher: str = "") -> dict:
    """Build a single hook entry in Claude Code's nested format."""
    entry = {
        "hooks": [
            {
                "type": "command",
                "command": f"{hook_path} {state}",
            }
        ]
    }
    if matcher:
        entry["matcher"] = matcher
    return entry


def _is_claudeq_entry(entry: dict) -> bool:
    """Check if a hook entry belongs to ClaudeQ."""
    for h in entry.get("hooks", []):
        if HOOK_MARKER in h.get("command", ""):
            return True
    return False


def _upsert_entries(hooks_list: list, new_entries: list) -> list:
    """Remove all existing ClaudeQ entries and append new ones.

    Preserves all non-ClaudeQ entries (e.g. user's sound hooks).
    """
    cleaned = [e for e in hooks_list if not _is_claudeq_entry(e)]
    cleaned.extend(new_entries)
    return cleaned


def configure_hooks(hook_path: str) -> None:
    """Merge ClaudeQ hook entries into Claude settings."""
    settings = _load_settings()

    if "hooks" not in settings:
        settings["hooks"] = {}

    hooks = settings["hooks"]

    # Stop hook -> writes "idle" state (no matcher for Stop hooks)
    if "Stop" not in hooks:
        hooks["Stop"] = []
    hooks["Stop"] = _upsert_entries(hooks["Stop"], [
        _make_entry(hook_path, "idle"),
    ])

    # Notification hooks -> separate entries per matcher
    if "Notification" not in hooks:
        hooks["Notification"] = []
    hooks["Notification"] = _upsert_entries(hooks["Notification"], [
        _make_entry(hook_path, "needs_permission", matcher="permission_prompt"),
        _make_entry(hook_path, "has_question", matcher="elicitation_dialog"),
    ])

    _save_settings(settings)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: configure_claude_hooks.py <path-to-claudeq-hook.sh>")
        sys.exit(1)

    hook_path = sys.argv[1]
    if not os.path.isfile(hook_path):
        print(f"Error: Hook script not found: {hook_path}")
        sys.exit(1)

    configure_hooks(hook_path)
    print(f"  Configured Claude hooks -> {CLAUDE_SETTINGS}")


if __name__ == "__main__":
    main()
