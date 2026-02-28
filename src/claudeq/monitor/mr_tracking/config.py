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
NOTIFICATION_SEEN_FILE = STORAGE_DIR / "notification_seen.json"
CQ_TEMPLATE_FILE = STORAGE_DIR / "cq_selected_template"
CQ_DIRECT_TEMPLATE_FILE = STORAGE_DIR / "cq_selected_direct_template"
CQ_TEMPLATES_FILE = STORAGE_DIR / "cq_templates.json"

# Backward-compat: old file names for migration
_OLD_CQ_CONTEXT_FILE = STORAGE_DIR / "cq_selected_ctx"
_OLD_CQ_CONTEXTS_FILE = STORAGE_DIR / "cq_contexts.json"

# Default monitor preferences
_DEFAULT_PREFS = {
    'include_bots': False,
    'auto_fetch_cq': True,
}

# Default notification preferences per notification type.
# Each type has independent 'dock' (badge count) and 'banner' (macOS banner) toggles.
_DEFAULT_NOTIFICATIONS: dict[str, dict[str, bool]] = {
    'mr_unresponded': {'dock': True, 'banner': False},
    'mr_all_responded': {'dock': True, 'banner': False},
    'mr_approved': {'dock': True, 'banner': False},
    'session_completed': {'dock': True, 'banner': False},
    'session_needs_permission': {'dock': True, 'banner': False},
    'session_has_question': {'dock': True, 'banner': False},
    'session_interrupted': {'dock': True, 'banner': False},
    'review_requested': {'dock': True, 'banner': False},
    'assigned': {'dock': True, 'banner': False},
    'mentioned': {'dock': True, 'banner': False},
}


def get_notification_prefs(prefs: dict[str, Any]) -> dict[str, dict[str, bool]]:
    """Get notification preferences merged with defaults.

    Args:
        prefs: The full monitor prefs dict (may contain a 'notifications' key).

    Returns:
        Dict mapping notification type key to {'dock': bool, 'banner': bool}.
    """
    saved = prefs.get('notifications', {})
    merged: dict[str, dict[str, bool]] = {}
    for key, defaults in _DEFAULT_NOTIFICATIONS.items():
        entry = saved.get(key, {})
        merged[key] = {
            'dock': entry.get('dock', defaults['dock']),
            'banner': entry.get('banner', defaults['banner']),
        }
    return merged


def get_dock_enabled(prefs: dict[str, Any]) -> dict[str, bool]:
    """Return a flat dict mapping notification type to dock-enabled flag.

    Convenience wrapper around ``get_notification_prefs`` used by the
    dock badge and banner notification callers.

    Args:
        prefs: The full monitor prefs dict.

    Returns:
        Dict mapping notification type key to bool (dock enabled).
    """
    return {k: v['dock'] for k, v in get_notification_prefs(prefs).items()}


def load_notification_seen() -> dict[str, list[str]]:
    """Load the set of seen notification IDs per SCM type.

    Returns:
        Dict mapping scm_type ("gitlab", "github") to list of seen ID strings.
    """
    if not NOTIFICATION_SEEN_FILE.exists():
        return {}
    try:
        with open(NOTIFICATION_SEEN_FILE, 'r') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_notification_seen(seen: dict[str, list[str]]) -> None:
    """Save the set of seen notification IDs per SCM type."""
    atomic_json_write(NOTIFICATION_SEEN_FILE, seen)


def resolve_scm_token(config: dict[str, Any], token_key: str) -> Optional[str]:
    """Resolve the SCM token from config, supporting env var mode.

    If ``config['token_mode']`` is ``'env_var'``, the value stored under
    *token_key* is treated as an environment variable name and looked up
    via ``os.environ``.  Otherwise (``'direct'`` or missing key — backward
    compat) the raw value is returned.

    Args:
        config: The provider config dict (gitlab_config or github_config).
        token_key: The dict key that holds the token or env var name
                   (e.g. ``'private_token'`` or ``'token'``).

    Returns:
        The resolved token string, or None if unavailable.
    """
    if config.get('token_mode') == 'env_var':
        var_name = config.get(token_key, '')
        return os.environ.get(var_name) if var_name else None
    return config.get(token_key)


def load_dialog_geometry(key: str) -> Optional[list[int]]:
    """Return [w, h] for the given dialog key, or None if not saved."""
    prefs = load_monitor_prefs()
    geom = prefs.get('dialog_geometry', {}).get(key)
    if isinstance(geom, list) and len(geom) == 2:
        return geom
    return None


def save_dialog_geometry(key: str, width: int, height: int) -> None:
    """Persist [w, h] for the given dialog key."""
    prefs = load_monitor_prefs()
    dialog_geom = prefs.get('dialog_geometry', {})
    dialog_geom[key] = [width, height]
    prefs['dialog_geometry'] = dialog_geom
    save_monitor_prefs(prefs)


def clear_all_dialog_geometry() -> None:
    """Remove all saved dialog geometries (for reset)."""
    prefs = load_monitor_prefs()
    prefs.pop('dialog_geometry', None)
    save_monitor_prefs(prefs)


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


def _migrate_old_template_files() -> None:
    """Migrate old context files to new template names (one-time)."""
    if _OLD_CQ_CONTEXT_FILE.exists() and not CQ_TEMPLATE_FILE.exists():
        _OLD_CQ_CONTEXT_FILE.rename(CQ_TEMPLATE_FILE)
    if _OLD_CQ_CONTEXTS_FILE.exists() and not CQ_TEMPLATES_FILE.exists():
        _OLD_CQ_CONTEXTS_FILE.rename(CQ_TEMPLATES_FILE)


def load_selected_template_name() -> str:
    """Load the name of the currently selected template preset.

    Returns:
        The preset name, or empty string if none selected.
    """
    _migrate_old_template_files()
    if not CQ_TEMPLATE_FILE.exists():
        return ''
    try:
        return CQ_TEMPLATE_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def save_selected_template_name(name: str) -> None:
    """Save the name of the currently selected template preset."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(STORAGE_DIR), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(name)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(CQ_TEMPLATE_FILE))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_selected_direct_template_name() -> str:
    """Load the name of the currently selected direct message template preset.

    Returns:
        The preset name, or empty string if none selected.
    """
    if not CQ_DIRECT_TEMPLATE_FILE.exists():
        return ''
    try:
        return CQ_DIRECT_TEMPLATE_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def save_selected_direct_template_name(name: str) -> None:
    """Save the name of the currently selected direct message template preset."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(STORAGE_DIR), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(name)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(CQ_DIRECT_TEMPLATE_FILE))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_cq_direct_template() -> list[str]:
    """Load the messages of the currently selected direct message template preset.

    Returns:
        List of non-empty message strings, or empty list if no preset selected
        or not found.
    """
    name = load_selected_direct_template_name()
    if not name:
        return []
    templates = load_saved_templates()
    messages = templates.get(name, [])
    return [m for m in messages if m.strip()]


def load_cq_template() -> str:
    """Load the text of the currently selected MR template preset.

    Resolves the selected preset name to its first message from
    cq_templates.json.  MR templates are enforced to be single-message
    by the combo validation, so only the first element is returned.

    Returns:
        The template string, or empty string if no preset selected or not found.
    """
    name = load_selected_template_name()
    if not name:
        return ''
    templates = load_saved_templates()
    messages = templates.get(name, [])
    return messages[0] if messages else ''


def load_saved_templates() -> dict[str, list[str]]:
    """Load all named template presets from storage.

    Auto-migrates old ``str`` values to ``[str]`` on read for backward
    compatibility.

    Returns:
        Dict mapping template name to list of message strings.
    """
    _migrate_old_template_files()
    if not CQ_TEMPLATES_FILE.exists():
        return {}
    try:
        with open(CQ_TEMPLATES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            # Auto-migrate str values → [str]
            return {
                k: v if isinstance(v, list) else [v]
                for k, v in data.items()
            }
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _write_saved_templates(templates: dict[str, list[str]]) -> None:
    """Write all named template presets to storage."""
    atomic_json_write(CQ_TEMPLATES_FILE, templates, ensure_ascii=False)


def save_named_template(name: str, messages: list[str]) -> None:
    """Save a template preset under the given name.

    Args:
        name: Preset name.
        messages: Ordered list of message strings.
    """
    templates = load_saved_templates()
    templates[name] = messages
    _write_saved_templates(templates)


def delete_named_template(name: str) -> None:
    """Delete a saved template preset by name."""
    templates = load_saved_templates()
    templates.pop(name, None)
    _write_saved_templates(templates)


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
