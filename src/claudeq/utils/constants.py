"""
Constants and shared configuration for ClaudeQ.

This module contains all paths, directories, and configuration values
used across the application.
"""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Final


def _find_project_root() -> Path:
    """
    Find the ClaudeQ project root directory.

    Priority order:
    1. Read from .storage/project-path (written during installation)
    2. Calculate from current file location (development fallback)

    Returns:
        Path to the project root containing .storage directory.
    """
    current = Path(__file__).resolve()

    # Strategy 1: Try to find .storage/project-path by walking up the tree
    # This works both from source and from bundled app
    for parent in [current, *current.parents]:
        storage_dir = parent / ".storage"
        project_path_file = storage_dir / "project-path"
        if project_path_file.exists():
            try:
                project_root = Path(project_path_file.read_text().strip())
                if project_root.exists():
                    return project_root
            except (OSError, ValueError):
                pass

    # Strategy 2: Calculate from source location (development fallback)
    # From src/claudeq/utils/constants.py → project root (4 levels up)
    calculated_root = current.parent.parent.parent.parent
    if calculated_root.exists():
        return calculated_root

    # Strategy 3: Last resort - use home directory (should never happen)
    return Path.home() / ".claudeq"


# Storage directory (all user data in one place, in the project root)
STORAGE_DIR: Final[Path] = _find_project_root() / ".storage"

# Directory paths (now inside .storage)
QUEUE_DIR: Final[Path] = STORAGE_DIR / "queues"
SOCKET_DIR: Final[Path] = STORAGE_DIR / "sockets"
HISTORY_DIR: Final[Path] = STORAGE_DIR / "history"
SLACK_DIR: Final[Path] = STORAGE_DIR / "slack"
SLACK_BOT_LOCK: Final[Path] = SLACK_DIR / "slack-bot.lock"

# Settings file
SETTINGS_FILE: Final[Path] = STORAGE_DIR / "settings.json"

# Timing constants
POLL_INTERVAL: Final[float] = 0.5  # Queue check interval in seconds
TITLE_RESET_INTERVAL: Final[float] = 2.0  # Terminal title reset interval
OUTPUT_SILENCE_TIMEOUT: Final[float] = 15.0  # Fallback: assume idle after N seconds of PTY silence

# Queue limits
MAX_RECENTLY_SENT: Final[int] = 20  # Maximum messages to track in recently_sent

# Terminal escape sequences
TERM_TITLE_PREFIX: Final[str] = "\033]0;"
TERM_TITLE_SUFFIX: Final[str] = "\007"

# Colors for terminal output
COLORS: Final[dict[str, str]] = {
    "yellow": "\033[33m",
    "green": "\033[32m",
    "red": "\033[31m",
    "reset": "\033[0m",
}

# Supported image extensions
IMAGE_EXTENSIONS: Final[tuple[str, ...]] = (
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'
)

# SCM polling
SCM_POLL_INTERVAL: Final[int] = 30  # seconds between PR status polls
SCM_MAX_CONCURRENT_POLLS: Final[int] = 10  # max parallel API requests per poll cycle

# JetBrains IDE process names
JETBRAINS_IDES: Final[dict[str, str]] = {
    'pycharm': 'PyCharm',
    'goland': 'GoLand',
    'webstorm': 'WebStorm',
    'phpstorm': 'PhpStorm',
    'rubymine': 'RubyMine',
    'clion': 'CLion',
    'datagrip': 'DataGrip',
    'idea': 'IntelliJ IDEA',
    'studio': 'Android Studio',
}


def ensure_storage_dirs() -> None:
    """Ensure all storage directories exist."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    SLACK_DIR.mkdir(parents=True, exist_ok=True)


def atomic_json_write(path: Path, data: Any, **json_kwargs: Any) -> None:
    """Write JSON data atomically using temp file + os.replace.

    Writes to a temporary file in the same directory, then atomically
    renames it to the target path. This prevents corruption from
    crashes or kills mid-write.

    Args:
        path: Target file path.
        data: Data to serialize as JSON.
        **json_kwargs: Extra keyword arguments passed to json.dump.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    json_kwargs.setdefault('indent', 2)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, **json_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_default_settings() -> dict[str, Any]:
    """
    Get default settings for ClaudeQ.

    Returns:
        Dictionary with default settings.
    """
    return {
        "show_auto_sent_notifications": True,
        "history_ttl_days": 3,
        "auto_send_mode": "pause"
    }


def load_settings() -> dict[str, Any]:
    """
    Load settings from JSON file with defaults.

    Returns:
        Dictionary of settings with defaults for missing keys.
    """
    defaults = get_default_settings()
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, 'r') as f:
                user_settings = json.load(f)
                # Merge with defaults (user settings override defaults)
                return {**defaults, **user_settings}
    except (json.JSONDecodeError, OSError):
        pass
    return defaults


def save_settings(settings: dict[str, Any]) -> None:
    """
    Save settings to JSON file.

    Args:
        settings: Dictionary of settings to save.
    """
    try:
        atomic_json_write(SETTINGS_FILE, settings)
    except OSError:
        pass


_TAG_PATTERN: Final = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$')


def is_valid_tag(tag: str) -> bool:
    """Check if a tag contains only letters, numbers, hyphens, and underscores."""
    return bool(_TAG_PATTERN.match(tag))
