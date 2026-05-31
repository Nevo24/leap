"""Configuration management for SCM integrations and monitor preferences."""

import base64
import json
import os
import tempfile
from typing import Any, Optional

from leap.utils.atomic_write import atomic_write_json
from leap.utils.constants import STORAGE_DIR

GITLAB_CONFIG_FILE = STORAGE_DIR / "gitlab_config.json"
GITHUB_CONFIG_FILE = STORAGE_DIR / "github_config.json"
MONITOR_PREFS_FILE = STORAGE_DIR / "monitor_prefs.json"
PINNED_SESSIONS_FILE = STORAGE_DIR / "pinned_sessions.json"
NOTIFICATION_SEEN_FILE = STORAGE_DIR / "notification_seen.json"
LEAP_PRESET_FILE = STORAGE_DIR / "leap_selected_preset"
LEAP_DIRECT_PRESET_FILE = STORAGE_DIR / "leap_selected_direct_preset"
LEAP_AUTO_FETCH_PRESET_FILE = STORAGE_DIR / "leap_auto_fetch_preset"
LEAP_PRESETS_FILE = STORAGE_DIR / "leap_presets.json"

# Backward-compat: old file names for migration
_OLD_LEAP_CONTEXT_FILE = STORAGE_DIR / "leap_selected_ctx"
_OLD_LEAP_CONTEXTS_FILE = STORAGE_DIR / "leap_contexts.json"
_OLD_LEAP_TEMPLATE_FILE = STORAGE_DIR / "leap_selected_template"
_OLD_LEAP_DIRECT_TEMPLATE_FILE = STORAGE_DIR / "leap_selected_direct_template"
_OLD_LEAP_TEMPLATES_FILE = STORAGE_DIR / "leap_templates.json"

# Default monitor preferences
_DEFAULT_PREFS = {
    'include_bots': False,
    'auto_fetch_leap': False,
}

# Default notification preferences per notification type.
# Each type has independent 'dock' (badge count) and 'banner' (macOS banner) toggles.
_DEFAULT_NOTIFICATIONS: dict[str, dict[str, Any]] = {
    'pr_unresponded': {'dock': True, 'banner': True, 'sound': 'None'},
    'pr_all_responded': {'dock': True, 'banner': True, 'sound': 'None'},
    'pr_approved': {'dock': True, 'banner': True, 'sound': 'None'},
    'session_completed': {'dock': True, 'banner': True, 'sound': 'None'},
    'session_needs_permission': {'dock': True, 'banner': True, 'sound': 'None'},
    'session_needs_input': {'dock': True, 'banner': True, 'sound': 'None'},
    'session_interrupted': {'dock': True, 'banner': True, 'sound': 'None'},
    'review_requested': {'dock': True, 'banner': True, 'sound': 'None'},
    'assigned': {'dock': True, 'banner': True, 'sound': 'None'},
    'mentioned': {'dock': True, 'banner': True, 'sound': 'None'},
}

# macOS system sounds available in /System/Library/Sounds/
# 'Browse...' is a sentinel — the dialog replaces it with a file picker.
MACOS_SYSTEM_SOUNDS: list[str] = [
    'None', 'Default', 'Basso', 'Blow', 'Bottle', 'Frog', 'Funk', 'Glass',
    'Hero', 'Morse', 'Ping', 'Pop', 'Purr', 'Sosumi', 'Submarine', 'Tink',
    'Browse...',
]


def get_notification_prefs(prefs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Get notification preferences merged with defaults.

    Args:
        prefs: The full monitor prefs dict (may contain a 'notifications' key).

    Returns:
        Dict mapping notification type key to {'dock': bool, 'banner': bool, 'sound': str}.
    """
    saved = prefs.get('notifications', {})
    merged: dict[str, dict[str, Any]] = {}
    for key, defaults in _DEFAULT_NOTIFICATIONS.items():
        entry = saved.get(key, {})
        merged[key] = {
            'dock': entry.get('dock', defaults['dock']),
            'banner': entry.get('banner', defaults['banner']),
            'sound': entry.get('sound', defaults['sound']),
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
    atomic_write_json(NOTIFICATION_SEEN_FILE, seen)


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
    """Remove all saved dialog geometries (for reset).

    Clears both the legacy ``[w, h]`` map and the full Qt window-state
    blob map — leaving the latter would silently override the reset on
    next reopen because ``restoreGeometry`` is layered on top of the
    plain resize.
    """
    prefs = load_monitor_prefs()
    prefs.pop('dialog_geometry', None)
    prefs.pop('dialog_geometry_state', None)
    save_monitor_prefs(prefs)


def load_dialog_splitter_sizes(key: str) -> Optional[list[int]]:
    """Return the saved splitter sizes for the given dialog, or None."""
    prefs = load_monitor_prefs()
    sizes = prefs.get('dialog_splitter_sizes', {}).get(key)
    if (isinstance(sizes, list) and len(sizes) >= 2
            and all(isinstance(s, int) and s >= 0 for s in sizes)):
        return sizes
    return None


def save_dialog_splitter_sizes(key: str, sizes: list[int]) -> None:
    """Persist splitter sizes for the given dialog key."""
    prefs = load_monitor_prefs()
    saved = prefs.get('dialog_splitter_sizes', {})
    saved[key] = list(sizes)
    prefs['dialog_splitter_sizes'] = saved
    save_monitor_prefs(prefs)


def load_dialog_geometry_state(key: str) -> Optional[bytes]:
    """Return the saved Qt window-state blob for *key*, or None.

    Wraps Qt's ``QWidget.saveGeometry()`` byte array so we can persist
    not just width/height but also the maximised/fullscreen flag,
    position, and screen — things ``[w, h]`` alone can't represent.
    Stored base64 so it round-trips through JSON cleanly.
    """
    prefs = load_monitor_prefs()
    encoded = prefs.get('dialog_geometry_state', {}).get(key)
    if isinstance(encoded, str) and encoded:
        try:
            return base64.b64decode(encoded.encode('ascii'))
        except (ValueError, UnicodeEncodeError):
            return None
    return None


def save_dialog_geometry_state(key: str, data: bytes) -> None:
    """Persist a Qt window-state blob for *key*."""
    prefs = load_monitor_prefs()
    saved = prefs.get('dialog_geometry_state', {})
    saved[key] = base64.b64encode(data).decode('ascii')
    prefs['dialog_geometry_state'] = saved
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
    atomic_write_json(GITLAB_CONFIG_FILE, config)


def normalize_github_api_url(url: str) -> str:
    """Ensure a GitHub Enterprise base URL ends with /api/v3.

    Canonical github.com / api.github.com stays untouched (PyGithub treats an
    empty base_url as api.github.com, and api.github.com already routes the
    v3 REST namespace without the suffix). Empty / falsy input returns ''.

    GitHub Enterprise Server exposes its REST API under ``/api/v3`` (and its
    GraphQL endpoint under ``/api/graphql``).  ``GitHubProvider`` already
    derives the GraphQL URL by stripping a ``/api/v3`` suffix, so a base URL
    that lacks it breaks both the REST client and resolved-thread queries.
    Normalizing on save/load keeps the stored config in that canonical form.
    """
    if not url:
        return ''
    stripped = url.lower().rstrip('/')
    if stripped in ('https://github.com', 'http://github.com', 'github.com'):
        return ''
    # Exact-match the canonical API host (not endswith — that would also
    # swallow a real GHE host like ``myapi.github.com``).
    if stripped in ('https://api.github.com', 'http://api.github.com',
                    'api.github.com'):
        return url.rstrip('/')
    if stripped.endswith('/api/v3'):
        return url.rstrip('/')
    return url.rstrip('/') + '/api/v3'


def load_github_config() -> Optional[dict[str, Any]]:
    """Load GitHub configuration from storage.

    Normalizes a saved GitHub Enterprise base URL to its ``/api/v3`` REST
    form in the returned dict, so a config written before the URL-normalization
    fix still works.  The fix is applied **in-memory only** and is NOT
    persisted here: ``load_github_config`` runs on the SCM poll worker's
    ``ThreadPoolExecutor`` threads (via ``refine_scm_type``), and a write-back
    from there could race a concurrent ``save_github_config`` on the main
    thread and clobber a just-saved config.  ``save_github_config`` rewrites
    the file in canonical form the next time the user saves.

    Returns:
        Config dict with github_url, token, username, poll_interval,
        or None if not configured (or the file is corrupt / not a dict).
    """
    if not GITHUB_CONFIG_FILE.exists():
        return None
    try:
        with open(GITHUB_CONFIG_FILE, 'r') as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(config, dict):
        return None

    raw_url = config.get('github_url', '') or ''
    fixed_url = normalize_github_api_url(raw_url)
    if fixed_url != raw_url:
        config['github_url'] = fixed_url
    return config


def save_github_config(config: dict[str, Any]) -> None:
    """Save GitHub configuration to storage."""
    if 'github_url' in config:
        config['github_url'] = normalize_github_api_url(
            config.get('github_url', '') or '')
    atomic_write_json(GITHUB_CONFIG_FILE, config)


def _migrate_notification_keys(prefs: dict[str, Any]) -> bool:
    """Migrate old mr_* notification keys to pr_* in prefs dict. Returns True if migrated."""
    notifications = prefs.get('notifications')
    if not isinstance(notifications, dict):
        return False
    renames = {
        'mr_unresponded': 'pr_unresponded',
        'mr_all_responded': 'pr_all_responded',
        'mr_approved': 'pr_approved',
    }
    migrated = False
    for old_key, new_key in renames.items():
        if old_key in notifications and new_key not in notifications:
            notifications[new_key] = notifications.pop(old_key)
            migrated = True
        elif old_key in notifications:
            del notifications[old_key]
            migrated = True
    return migrated


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
    if _migrate_notification_keys(prefs):
        atomic_write_json(MONITOR_PREFS_FILE, prefs)
    return prefs


def save_monitor_prefs(prefs: dict[str, Any]) -> None:
    """Save monitor UI preferences to storage."""
    atomic_write_json(MONITOR_PREFS_FILE, prefs)


def load_send_position() -> str:
    """Load the shared "send position" toggle (``'next'`` or ``'end'``).

    Used by the Send message and Send preset dialogs so the user's last
    choice is remembered across dialogs and sessions.  Defaults to
    ``'end'`` (append to queue).
    """
    prefs = load_monitor_prefs()
    value = prefs.get('send_position', 'end')
    return 'next' if value == 'next' else 'end'


def save_send_position(position: str) -> None:
    """Persist the shared "send position" toggle."""
    if position not in ('next', 'end'):
        return
    prefs = load_monitor_prefs()
    prefs['send_position'] = position
    save_monitor_prefs(prefs)


def load_resume_open_in_last_app() -> bool:
    """Load the Resume dialog's "open in the app the session was last run in"
    toggle.  Defaults to ``True`` — the feature only adds value when on,
    and the user can switch it off per session.
    """
    prefs = load_monitor_prefs()
    return bool(prefs.get('resume_open_in_last_app', True))


def save_resume_open_in_last_app(value: bool) -> None:
    """Persist the Resume dialog's "open in last app" toggle."""
    prefs = load_monitor_prefs()
    prefs['resume_open_in_last_app'] = bool(value)
    save_monitor_prefs(prefs)


def load_resume_hidden_columns() -> list[str]:
    """Load the Resume dialog's per-column hidden list.

    Independent from the main monitor table's ``hidden_columns`` — the
    Resume picker is a different view and the user toggles each set
    independently via the header's right-click menu.  Returns ``[]``
    on missing / corrupt data.
    """
    prefs = load_monitor_prefs()
    val = prefs.get('resume_session_hidden_columns')
    if not isinstance(val, list):
        return []
    return [x for x in val if isinstance(x, str)]


def save_resume_hidden_columns(hidden: list[str]) -> None:
    """Persist the Resume dialog's per-column hidden list."""
    prefs = load_monitor_prefs()
    prefs['resume_session_hidden_columns'] = [
        x for x in hidden if isinstance(x, str)
    ]
    save_monitor_prefs(prefs)


def load_send_comments_prefs() -> dict[str, str]:
    """Return saved picks for the "Send comments to session" dialog.

    Keys::

        filter : 'all'      — every unresponded comment
                 'leap'     — only comments with an unacked '/leap' tag
        mode   : 'each'     — one queue message per comment
                 'combined' — all comments concatenated into a single message

    Defaults are ``'all'`` and ``'each'``.
    """
    prefs = load_monitor_prefs()
    filt = prefs.get('send_comments_filter', 'all')
    mode = prefs.get('send_comments_mode', 'each')
    return {
        'filter': 'leap' if filt == 'leap' else 'all',
        'mode': 'combined' if mode == 'combined' else 'each',
    }


def save_send_comments_prefs(filter_: str, mode: str) -> None:
    """Persist both picks for the "Send comments to session" dialog."""
    if filter_ not in ('all', 'leap') or mode not in ('each', 'combined'):
        return
    prefs = load_monitor_prefs()
    prefs['send_comments_filter'] = filter_
    prefs['send_comments_mode'] = mode
    save_monitor_prefs(prefs)


def load_preset_editor_last_name() -> str:
    """Return the preset name the editor was last focused on.

    Independent from the "active" PR-context / bundle selections used by
    the send dialogs, so editing one preset doesn't reassign which
    preset is live for sends.
    """
    prefs = load_monitor_prefs()
    value = prefs.get('preset_editor_last_name', '')
    return value if isinstance(value, str) else ''


def save_preset_editor_last_name(name: str) -> None:
    """Persist the preset name the editor is focused on."""
    prefs = load_monitor_prefs()
    prefs['preset_editor_last_name'] = name or ''
    save_monitor_prefs(prefs)


def _migrate_old_preset_files() -> None:
    """Migrate old context/template files to new preset names (one-time)."""
    # Stage 1: context → template (legacy)
    if _OLD_LEAP_CONTEXT_FILE.exists() and not _OLD_LEAP_TEMPLATE_FILE.exists():
        _OLD_LEAP_CONTEXT_FILE.rename(_OLD_LEAP_TEMPLATE_FILE)
    if _OLD_LEAP_CONTEXTS_FILE.exists() and not _OLD_LEAP_TEMPLATES_FILE.exists():
        _OLD_LEAP_CONTEXTS_FILE.rename(_OLD_LEAP_TEMPLATES_FILE)
    # Stage 2: template → preset
    if _OLD_LEAP_TEMPLATE_FILE.exists() and not LEAP_PRESET_FILE.exists():
        _OLD_LEAP_TEMPLATE_FILE.rename(LEAP_PRESET_FILE)
    if _OLD_LEAP_DIRECT_TEMPLATE_FILE.exists() and not LEAP_DIRECT_PRESET_FILE.exists():
        _OLD_LEAP_DIRECT_TEMPLATE_FILE.rename(LEAP_DIRECT_PRESET_FILE)
    if _OLD_LEAP_TEMPLATES_FILE.exists() and not LEAP_PRESETS_FILE.exists():
        _OLD_LEAP_TEMPLATES_FILE.rename(LEAP_PRESETS_FILE)


def load_selected_preset_name() -> str:
    """Load the name of the currently selected preset.

    Returns:
        The preset name, or empty string if none selected.
    """
    _migrate_old_preset_files()
    if not LEAP_PRESET_FILE.exists():
        return ''
    try:
        return LEAP_PRESET_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def save_selected_preset_name(name: str) -> None:
    """Save the name of the currently selected preset."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(STORAGE_DIR), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(name)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(LEAP_PRESET_FILE))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_selected_direct_preset_name() -> str:
    """Load the name of the currently selected direct message preset.

    Returns:
        The preset name, or empty string if none selected.
    """
    _migrate_old_preset_files()
    if not LEAP_DIRECT_PRESET_FILE.exists():
        return ''
    try:
        return LEAP_DIRECT_PRESET_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def save_selected_direct_preset_name(name: str) -> None:
    """Save the name of the currently selected direct message preset."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(STORAGE_DIR), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(name)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(LEAP_DIRECT_PRESET_FILE))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_auto_fetch_preset_name() -> str:
    """Load the name of the preset used for auto-fetched /leap comments.

    This is independent of ``load_selected_preset_name`` (which is used for
    manual comment sends via SendCommentsDialog). When "Auto '/leap' fetch"
    is on, the main window exposes a separate combobox that writes here.

    Returns:
        The preset name, or empty string if none selected.
    """
    if not LEAP_AUTO_FETCH_PRESET_FILE.exists():
        return ''
    try:
        return LEAP_AUTO_FETCH_PRESET_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def save_auto_fetch_preset_name(name: str) -> None:
    """Save the preset name used for auto-fetched /leap comments."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(STORAGE_DIR), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(name)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(LEAP_AUTO_FETCH_PRESET_FILE))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_auto_fetch_leap_preset() -> str:
    """Resolve the auto-fetch preset name to its message text.

    Returns:
        The preset string, or empty string if no preset selected or not found.
    """
    name = load_auto_fetch_preset_name()
    if not name:
        return ''
    presets = load_saved_presets()
    messages = presets.get(name, [])
    return messages[0] if messages else ''


def load_leap_preset() -> str:
    """Load the text of the currently selected PR preset.

    Resolves the selected preset name to its first message from
    leap_presets.json.  PR context presets are filtered to single-message
    entries by ``SendCommentsDialog``, which also self-heals the saved
    slot if the target grew to multi-message, so only the first element
    is returned here.

    Returns:
        The preset string, or empty string if no preset selected or not found.
    """
    name = load_selected_preset_name()
    if not name:
        return ''
    presets = load_saved_presets()
    messages = presets.get(name, [])
    return messages[0] if messages else ''


def load_saved_presets() -> dict[str, list[str]]:
    """Load all named presets from storage.

    Auto-migrates old ``str`` values to ``[str]`` on read for backward
    compatibility.

    Returns:
        Dict mapping preset name to list of message strings.
    """
    _migrate_old_preset_files()
    if not LEAP_PRESETS_FILE.exists():
        return {}
    try:
        with open(LEAP_PRESETS_FILE, 'r', encoding='utf-8') as f:
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


def _write_saved_presets(presets: dict[str, list[str]]) -> None:
    """Write all named presets to storage."""
    atomic_write_json(LEAP_PRESETS_FILE, presets, ensure_ascii=False)


def save_named_preset(name: str, messages: list[str]) -> None:
    """Save a preset under the given name.

    Args:
        name: Preset name.
        messages: Ordered list of message strings.
    """
    presets = load_saved_presets()
    presets[name] = messages
    _write_saved_presets(presets)


def delete_named_preset(name: str) -> None:
    """Delete a saved preset by name."""
    presets = load_saved_presets()
    presets.pop(name, None)
    _write_saved_presets(presets)


def _migrate_mr_to_pr_keys(session: dict[str, Any]) -> dict[str, Any]:
    """Migrate old mr_* keys to pr_* keys in a pinned session dict."""
    renames = {
        'mr_title': 'pr_title',
        'mr_url': 'pr_url',
        'mr_tracked': 'pr_tracked',
        'mr_branch': 'pr_branch',
    }
    for old_key, new_key in renames.items():
        if old_key in session and new_key not in session:
            session[new_key] = session.pop(old_key)
        elif old_key in session:
            del session[old_key]
    return session


def load_pinned_sessions() -> dict[str, dict[str, Any]]:
    """Load pinned sessions from storage.

    Returns:
        Dict mapping tag to session info dict with keys:
        tag, project_path, ide, branch.

    Tolerates a corrupt or mis-encoded file (``ValueError`` covers
    both ``json.JSONDecodeError`` and ``UnicodeDecodeError``) and skips
    non-dict tag entries during the mr→pr migration so a hand-edited
    file with a stray string/int value can't crash the monitor at
    startup.
    """
    if not PINNED_SESSIONS_FILE.exists():
        return {}
    try:
        with open(PINNED_SESSIONS_FILE, 'r') as f:
            data = json.load(f)
        if isinstance(data, dict):
            migrated = False
            for tag, session in data.items():
                if not isinstance(session, dict):
                    continue
                if any(k in session for k in ('mr_title', 'mr_url', 'mr_tracked', 'mr_branch')):
                    _migrate_mr_to_pr_keys(session)
                    migrated = True
            if migrated:
                atomic_write_json(PINNED_SESSIONS_FILE, data)
            return data
    except (OSError, ValueError):
        pass
    return {}


def _safe_load_pinned() -> dict[str, Any]:
    """Read ``pinned_sessions.json`` and return its content as a dict.

    Returns ``{}`` if the file is missing, corrupt, mis-encoded, or has
    a non-dict root.  Used by the targeted writers below — by treating
    a corrupt file as empty rather than raising, the NEXT write
    overwrites the corruption with a valid file (recovering the user's
    disk state).  Pre-fix, ``save_pinned_sessions`` overwrote
    unconditionally, so corrupt files self-healed after one write;
    without this helper, post-fix targeted writers would skip the write
    on corrupt disk and leave the file broken forever.
    """
    if not PINNED_SESSIONS_FILE.exists():
        return {}
    try:
        with open(PINNED_SESSIONS_FILE, 'r') as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
    except (OSError, ValueError):
        pass
    return {}


def update_pinned_session_field(tag: str, field: str, value: Any) -> None:
    """Targeted read-modify-write of a single field on a single pin entry.

    Used in narrow contexts (currently: monitor's ``_set_auto_send_mode``)
    where a full ``save_pinned_sessions(self._pinned_sessions)`` would risk
    overwriting OTHER tags' disk state with the monitor's stale in-memory
    cache — e.g., when a different session's server has just written its
    own ``auto_send_mode`` between the monitor's last ``_merge_sessions``
    refresh and this toggle.

    Re-reads the file (via ``_safe_load_pinned`` so a corrupt file is
    treated as empty), mutates only the requested ``field`` on the
    requested ``tag`` entry, writes back atomically.  Creates the file /
    entry if missing; coerces a non-dict entry to a fresh dict.  Silent
    on write failures.
    """
    try:
        pinned = _safe_load_pinned()
        entry = pinned.get(tag, {})
        if not isinstance(entry, dict):
            entry = {}
        if entry.get(field) == value:
            return
        entry[field] = value
        pinned[tag] = entry
        atomic_write_json(PINNED_SESSIONS_FILE, pinned)
    except (OSError, ValueError):
        # ``_safe_load_pinned`` already absorbs read-side ValueError;
        # the only remaining ValueError path is ``json.dump`` (circular
        # refs, etc.) inside atomic_write_json, which would indicate a
        # programming error.  Swallowing it here matches the rest of
        # the pin-file helpers' "best-effort write, don't crash callers"
        # contract — the toggle still has its in-memory effect.
        pass


def write_pinned_session_entry(tag: str, entry: dict[str, Any]) -> None:
    """Atomic per-tag upsert that preserves disk-only fields.

    The cross-session leak this guards against: ``auto_send_mode`` is
    the only field the server writes to ``pinned_sessions.json`` (via
    ``LeapServer._save_pinned_auto_send_mode``).  Any monitor-side write
    that goes through ``save_pinned_sessions(self._pinned_sessions)``
    sends the monitor's WHOLE in-memory map to disk — which can stomp a
    fresh server-side ``auto_send_mode`` write for some OTHER tag that
    the monitor's ``_merge_sessions`` refresh hasn't picked up yet.

    To eliminate that race, monitor-side writes must be per-tag.  This
    helper does the targeted upsert:

    * Re-reads the pin file (gets disk's current state including any
      server-side writes since the monitor's last refresh).
    * Replaces ONLY the requested tag's entry with ``entry``.
    * Preserves disk's ``auto_send_mode`` for the tag unless ``entry``
      itself includes one — the server is the source of truth for
      ``auto_send_mode``, so a monitor-side write that ships a stale
      in-memory ``auto_send_mode`` would re-open the leak.  Per-session
      toggle of the mode goes through ``update_pinned_session_field``
      (which DOES set it explicitly), not through this helper.
    * Writes back atomically; other tags' disk state is preserved
      byte-for-byte.

    Silent on corrupt / mis-encoded files — falls through and the next
    ``_merge_sessions`` refresh reconciles.
    """
    try:
        pinned = _safe_load_pinned()
        disk_entry = pinned.get(tag, {})
        new_entry: dict[str, Any] = {
            k: v for k, v in entry.items() if k != 'auto_send_mode'
        }
        # Server is the source of truth for ``auto_send_mode`` — always
        # carry disk's value over the caller's (possibly stale)
        # in-memory copy.  ``update_pinned_session_field`` is the only
        # legitimate writer of this field from the monitor side.
        if isinstance(disk_entry, dict) and 'auto_send_mode' in disk_entry:
            new_entry['auto_send_mode'] = disk_entry['auto_send_mode']
        elif 'auto_send_mode' in entry:
            new_entry['auto_send_mode'] = entry['auto_send_mode']
        # Short-circuit when disk already matches — avoids an
        # unnecessary fsync on no-op refreshes (e.g., monitor's
        # ``_merge_sessions`` rebuilding the same ``pin_data`` that
        # differs from in-memory only in fields the helper strips, so
        # the on-disk result is identical to what's already there).
        if pinned.get(tag) == new_entry:
            return
        pinned[tag] = new_entry
        atomic_write_json(PINNED_SESSIONS_FILE, pinned)
    except (OSError, ValueError):
        pass


def remove_pinned_session_tag(tag: str) -> None:
    """Atomic per-tag removal.

    Re-reads the pin file (via ``_safe_load_pinned`` so a corrupt file
    is treated as empty), drops the requested tag, writes back
    atomically.  Other tags' disk state is preserved byte-for-byte.
    Silent on write failure.  Skips the write when the tag isn't on
    disk — no point in rewriting the file just to remove nothing.
    """
    try:
        pinned = _safe_load_pinned()
        if tag not in pinned:
            return
        pinned.pop(tag, None)
        atomic_write_json(PINNED_SESSIONS_FILE, pinned)
    except (OSError, ValueError):
        pass
