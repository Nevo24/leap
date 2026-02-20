"""
Slack configuration and session persistence for ClaudeQ.

Manages Slack app tokens and per-session thread mappings stored
in ``.storage/slack/``.
"""

import json
from pathlib import Path
from typing import Any, Optional

from claudeq.utils.constants import SLACK_DIR, atomic_json_write

_CONFIG_FILE: Path = SLACK_DIR / "config.json"
_SESSIONS_FILE: Path = SLACK_DIR / "sessions.json"


def is_slack_installed() -> bool:
    """Check if the Slack app has been configured with tokens."""
    return _CONFIG_FILE.exists()


def load_slack_config() -> dict[str, Any]:
    """Load Slack app configuration.

    Returns:
        Dictionary with ``bot_token``, ``app_token``, ``user_id``,
        ``dm_channel_id``.  Empty dict if not configured.
    """
    try:
        if _CONFIG_FILE.exists():
            return json.loads(_CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_slack_config(config: dict[str, Any]) -> None:
    """Save Slack app configuration.

    Args:
        config: Configuration dictionary to persist.
    """
    atomic_json_write(_CONFIG_FILE, config)


def load_slack_sessions() -> dict[str, dict[str, Any]]:
    """Load per-session Slack data (thread_ts, enabled state, etc.).

    Returns:
        Mapping of tag → session data.
    """
    try:
        if _SESSIONS_FILE.exists():
            return json.loads(_SESSIONS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_slack_sessions(sessions: dict[str, dict[str, Any]]) -> None:
    """Save per-session Slack data.

    Args:
        sessions: Mapping of tag → session data.
    """
    atomic_json_write(_SESSIONS_FILE, sessions)
