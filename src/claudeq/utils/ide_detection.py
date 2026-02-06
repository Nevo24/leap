"""
IDE detection utilities for ClaudeQ.

Detects which IDE/terminal the application is running in.
"""

import os
import subprocess
from typing import Optional

from claudeq.utils.constants import JETBRAINS_IDES


def detect_ide() -> str:
    """
    Detect which IDE/terminal the application is running in.

    Returns:
        Name of the detected IDE ('PyCharm', 'VS Code', 'iTerm2', etc.)
        or 'Unknown' if not detected.
    """
    terminal_emulator = os.environ.get('TERMINAL_EMULATOR', '')

    # Check for JetBrains IDE
    if 'JetBrains' in terminal_emulator or 'jetbrains' in terminal_emulator.lower():
        ide = _detect_jetbrains_ide()
        if ide:
            return ide
        return 'JetBrains IDE'

    # Check for VS Code
    if 'vscode' in terminal_emulator.lower() or os.environ.get('TERM_PROGRAM') == 'vscode':
        return 'VS Code'

    # Check for iTerm2
    if os.environ.get('TERM_PROGRAM') == 'iTerm.app':
        return 'iTerm2'

    # Check for Terminal.app
    if os.environ.get('TERM_PROGRAM') == 'Apple_Terminal':
        return 'Terminal.app'

    return 'Unknown'


def _detect_jetbrains_ide() -> Optional[str]:
    """
    Walk up the process tree to find the specific JetBrains IDE.

    Returns:
        Name of the specific JetBrains IDE or None if not found.
    """
    try:
        current_pid = os.getpid()
        for _ in range(10):  # Check up to 10 levels up
            result = subprocess.run(
                ['ps', '-p', str(current_pid), '-o', 'ppid=,comm='],
                capture_output=True,
                text=True,
                timeout=1
            )
            if result.returncode != 0:
                break

            output = result.stdout.strip().split(None, 1)
            if len(output) < 2:
                break

            ppid = output[0]
            process_name = output[1].lower()

            # Check against known JetBrains IDEs
            for key, name in JETBRAINS_IDES.items():
                if key in process_name:
                    # Special case: 'idea' should not match 'pycharm'
                    if key == 'idea' and 'pycharm' in process_name:
                        continue
                    return name

            # Move to parent
            current_pid = int(ppid)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass

    return None


def get_git_branch(cwd: Optional[str] = None) -> Optional[str]:
    """
    Get the current git branch name.

    Args:
        cwd: Working directory to check. Defaults to current directory.

    Returns:
        Branch name or None if not in a git repository.
    """
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True,
            text=True,
            timeout=1,
            cwd=cwd
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None
