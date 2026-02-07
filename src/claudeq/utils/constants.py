"""
Constants and shared configuration for ClaudeQ.

This module contains all paths, directories, and configuration values
used across the application.
"""

from pathlib import Path
from typing import Final

# Storage directory (all user data in one place, in the project root)
STORAGE_DIR: Final[Path] = Path(__file__).parent.parent.parent.parent / ".storage"

# Directory paths (now inside .storage)
QUEUE_DIR: Final[Path] = STORAGE_DIR / "queues"
SOCKET_DIR: Final[Path] = STORAGE_DIR / "sockets"

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
COLORS: Final[dict] = {
    "yellow": "\033[33m",
    "green": "\033[32m",
    "red": "\033[31m",
    "reset": "\033[0m",
}

# Supported image extensions
IMAGE_EXTENSIONS: Final[tuple] = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')

# JetBrains IDE process names
JETBRAINS_IDES: Final[dict] = {
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
