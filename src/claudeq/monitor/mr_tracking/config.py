"""Configuration management for SCM integrations and monitor preferences."""

import json
from typing import Any, Optional

from claudeq.utils.constants import STORAGE_DIR

GITLAB_CONFIG_FILE = STORAGE_DIR / "gitlab_config.json"
GITHUB_CONFIG_FILE = STORAGE_DIR / "github_config.json"
MONITOR_PREFS_FILE = STORAGE_DIR / "monitor_prefs.json"
CQ_CONTEXT_FILE = STORAGE_DIR / "cq_selected_ctx.txt"
CQ_CONTEXTS_FILE = STORAGE_DIR / "cq_contexts.json"

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


def load_github_config() -> Optional[dict[str, Any]]:
    """Load GitHub configuration from storage.

    Returns:
        Config dict with github_url, token, username, poll_interval,
        or None if not configured.
    """
    if not GITHUB_CONFIG_FILE.exists():
        return None
    try:
        with open(GITHUB_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_github_config(config: dict[str, Any]) -> None:
    """Save GitHub configuration to storage."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with open(GITHUB_CONFIG_FILE, 'w') as f:
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


def load_selected_context_name() -> str:
    """Load the name of the currently selected context preset.

    Returns:
        The preset name, or empty string if none selected.
    """
    if not CQ_CONTEXT_FILE.exists():
        return ''
    try:
        return CQ_CONTEXT_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def save_selected_context_name(name: str) -> None:
    """Save the name of the currently selected context preset."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    CQ_CONTEXT_FILE.write_text(name, encoding='utf-8')


def load_cq_context() -> str:
    """Load the text of the currently selected context preset.

    Resolves the selected preset name to its text from cq_contexts.json.

    Returns:
        The context string, or empty string if no preset selected or not found.
    """
    name = load_selected_context_name()
    if not name:
        return ''
    contexts = load_saved_contexts()
    return contexts.get(name, '')


def load_saved_contexts() -> dict[str, str]:
    """Load all named context presets from storage.

    Returns:
        Dict mapping context name to text.
    """
    if not CQ_CONTEXTS_FILE.exists():
        return {}
    try:
        with open(CQ_CONTEXTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _write_saved_contexts(contexts: dict[str, str]) -> None:
    """Write all named context presets to storage."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CQ_CONTEXTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(contexts, f, indent=2, ensure_ascii=False)


def save_named_context(name: str, text: str) -> None:
    """Save a context preset under the given name."""
    contexts = load_saved_contexts()
    contexts[name] = text
    _write_saved_contexts(contexts)


def delete_named_context(name: str) -> None:
    """Delete a saved context preset by name."""
    contexts = load_saved_contexts()
    contexts.pop(name, None)
    _write_saved_contexts(contexts)
