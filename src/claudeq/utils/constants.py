"""
Constants and shared configuration for ClaudeQ.

This module contains all paths, directories, and configuration values
used across the application.
"""

import json
from pathlib import Path
from typing import Final


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

# Settings file
SETTINGS_FILE: Final[Path] = STORAGE_DIR / "settings.json"

# Timing constants
MIN_BUSY_DURATION: Final[float] = 3.0  # Minimum seconds to consider busy after sending
POLL_INTERVAL: Final[float] = 0.5  # Queue check interval in seconds
TITLE_RESET_INTERVAL: Final[float] = 2.0  # Terminal title reset interval

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

# GitLab polling
GITLAB_POLL_INTERVAL: Final[int] = 30  # seconds between MR status polls
GITLAB_MAX_CONCURRENT_POLLS: Final[int] = 10  # max parallel API requests per poll cycle

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
}


def ensure_storage_dirs() -> None:
    """Ensure all storage directories exist."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def get_default_settings() -> dict:
    """
    Get default settings for ClaudeQ.

    Returns:
        Dictionary with default settings.
    """
    return {
        "show_auto_sent_notifications": True,
        "history_ttl_days": 3
    }


def load_settings() -> dict:
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


def save_settings(settings: dict) -> None:
    """
    Save settings to JSON file.

    Args:
        settings: Dictionary of settings to save.
    """
    try:
        ensure_storage_dirs()
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass
