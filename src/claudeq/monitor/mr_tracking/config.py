"""Configuration management for SCM integrations and monitor preferences."""

import json
from typing import Any, Optional

from claudeq.utils.constants import STORAGE_DIR

GITLAB_CONFIG_FILE = STORAGE_DIR / "gitlab_config.json"
MONITOR_PREFS_FILE = STORAGE_DIR / "monitor_prefs.json"

# Default monitor preferences
_DEFAULT_PREFS = {
    'include_bots': False,
}


def load_gitlab_config() -> Optional[dict[str, Any]]:
    """Load GitLab configuration from storage.

    Returns:
        Config dict with gitlab_url, private_token, username, poll_interval,
        or None if not configured.
    """
    if not GITLAB_CONFIG_FILE.exists():
        return None
    try:
        with open(GITLAB_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_gitlab_config(config: dict[str, Any]) -> None:
    """Save GitLab configuration to storage."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with open(GITLAB_CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def load_monitor_prefs() -> dict[str, Any]:
    """Load monitor UI preferences from storage.

    Returns:
        Prefs dict. Missing keys are filled with defaults.
    """
    prefs = dict(_DEFAULT_PREFS)
    if MONITOR_PREFS_FILE.exists():
        try:
            with open(MONITOR_PREFS_FILE, 'r') as f:
                prefs.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return prefs


def save_monitor_prefs(prefs: dict[str, Any]) -> None:
    """Save monitor UI preferences to storage."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with open(MONITOR_PREFS_FILE, 'w') as f:
        json.dump(prefs, f, indent=2)
