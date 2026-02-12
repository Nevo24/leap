"""Configuration management for SCM integrations and monitor preferences."""

import json
import os
import tempfile
from typing import Any, Optional

from claudeq.utils.constants import STORAGE_DIR, atomic_json_write

GITLAB_CONFIG_FILE = STORAGE_DIR / "gitlab_config.json"
GITHUB_CONFIG_FILE = STORAGE_DIR / "github_config.json"
MONITOR_PREFS_FILE = STORAGE_DIR / "monitor_prefs.json"
PINNED_SESSIONS_FILE = STORAGE_DIR / "pinned_sessions.json"
CQ_CONTEXT_FILE = STORAGE_DIR / "cq_selected_ctx"
CQ_CONTEXTS_FILE = STORAGE_DIR / "cq_contexts.json"

# Default monitor preferences
_DEFAULT_PREFS = {
    'include_bots': False,
    'auto_fetch_cq': True,
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
    atomic_json_write(GITLAB_CONFIG_FILE, config)


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
    atomic_json_write(GITHUB_CONFIG_FILE, config)


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
    atomic_json_write(MONITOR_PREFS_FILE, prefs)


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
    fd, tmp_path = tempfile.mkstemp(dir=str(STORAGE_DIR), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(name)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(CQ_CONTEXT_FILE))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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
    atomic_json_write(CQ_CONTEXTS_FILE, contexts, ensure_ascii=False)


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


def load_pinned_sessions() -> dict[str, dict[str, Any]]:
    """Load pinned sessions from storage.

    Returns:
        Dict mapping tag to session info dict with keys:
        tag, project_path, ide, branch.
    """
    if not PINNED_SESSIONS_FILE.exists():
        return {}
    try:
        with open(PINNED_SESSIONS_FILE, 'r') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_pinned_sessions(sessions: dict[str, dict[str, Any]]) -> None:
    """Save pinned sessions to storage."""
    atomic_json_write(PINNED_SESSIONS_FILE, sessions)
