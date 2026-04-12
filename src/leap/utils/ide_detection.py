"""
IDE detection utilities for Leap.

Detects which IDE/terminal the application is running in.
"""

import os
import subprocess
from typing import Optional

from leap.utils.constants import JETBRAINS_IDES


def detect_ide() -> str:
    """
    Detect which IDE/terminal the application is running in.

    Checks TERM_PROGRAM first (set by the actual terminal emulator)
    before TERMINAL_EMULATOR (which can leak across sessions from
    JetBrains IDEs).

    Returns:
        Name of the detected IDE ('PyCharm', 'VS Code', 'iTerm2', etc.)
        or 'Unknown' if not detected.
    """
    term_program = os.environ.get('TERM_PROGRAM', '')
    terminal_emulator = os.environ.get('TERMINAL_EMULATOR', '')

    # Check TERM_PROGRAM first — it's set by the real terminal emulator
    # and is more reliable than TERMINAL_EMULATOR which can leak.
    # Both VS Code and Cursor set TERM_PROGRAM='vscode', so use
    # __CFBundleIdentifier (set by macOS for GUI app child processes)
    # to distinguish them.
    if term_program == 'vscode':
        bundle_id = os.environ.get('__CFBundleIdentifier', '')
        if 'todesktop' in bundle_id or 'cursor' in bundle_id.lower():
            return 'Cursor'
        return 'VS Code'

    if term_program == 'iTerm.app':
        return 'iTerm2'

    if term_program == 'Apple_Terminal':
        return 'Terminal.app'

    if term_program == 'WarpTerminal':
        return 'Warp'

    if term_program == 'kitty':
        return 'Kitty'

    if term_program == 'ghostty':
        return 'Ghostty'

    if term_program == 'WezTerm':
        return 'WezTerm'

    # Check for JetBrains IDE (only if TERM_PROGRAM didn't identify
    # a known terminal — avoids false positives from leaked env vars)
    if 'JetBrains' in terminal_emulator or 'jetbrains' in terminal_emulator.lower():
        ide = _detect_jetbrains_ide()
        if ide:
            return ide
        return 'JetBrains IDE'

    # VS Code/Cursor also set TERMINAL_EMULATOR in some configurations
    if 'vscode' in terminal_emulator.lower():
        bundle_id = os.environ.get('__CFBundleIdentifier', '')
        if 'todesktop' in bundle_id or 'cursor' in bundle_id.lower():
            return 'Cursor'
        return 'VS Code'

    # Arduino IDE (Theia-based) sets neither TERM_PROGRAM nor
    # TERMINAL_EMULATOR, but macOS provides __CFBundleIdentifier.
    bundle_id = os.environ.get('__CFBundleIdentifier', '')
    if 'arduino' in bundle_id.lower():
        return 'Arduino IDE'

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
