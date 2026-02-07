"""Shared utilities for ClaudeQ."""

from claudeq.utils.constants import (
    SOCKET_DIR,
    QUEUE_DIR,
    MIN_BUSY_DURATION,
    POLL_INTERVAL,
    TITLE_RESET_INTERVAL,
    MAX_RECENTLY_SENT,
    IMAGE_EXTENSIONS,
    JETBRAINS_IDES,
    COLORS,
)
from claudeq.utils.terminal import (
    set_terminal_title,
    print_banner,
)
from claudeq.utils.ide_detection import detect_ide, get_git_branch
