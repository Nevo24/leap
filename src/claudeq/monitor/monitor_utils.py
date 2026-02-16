"""Standalone utility functions for ClaudeQ Monitor."""

import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Optional

from PyQt5.QtWidgets import QMessageBox

logger = logging.getLogger(__name__)

from claudeq.monitor.session_manager import (
    load_session_metadata, read_client_pid, session_exists, is_client_lock_held,
)
from claudeq.monitor.navigation import find_terminal_with_title, open_terminal_with_command
from claudeq.utils.constants import SOCKET_DIR


def load_shell_env() -> None:
    """Import environment variables from the user's login shell.

    macOS apps launched from Finder/Dock do not inherit shell env vars
    (e.g. those exported in ~/.zshrc).  This spawns a login shell to
    capture its environment and merges missing vars into os.environ so
    that features like env-var token mode work in the bundled .app.
    """
    shell = os.environ.get('SHELL', '/bin/zsh')
    try:
        result = subprocess.run(
            [shell, '-l', '-c', 'env -0'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return
        for entry in result.stdout.split('\0'):
            if not entry:
                continue
            key, sep, value = entry.partition('=')
            if sep and key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        logger.debug("Failed to load shell environment", exc_info=True)


def find_icon() -> Optional[Path]:
    """Find the app icon, works both from source and .app bundle."""
    # From source: src/claudeq/monitor/monitor_utils.py → project_root/assets/
    candidate = Path(__file__).parent.parent.parent.parent / "assets" / "claudeq-icon.png"
    if candidate.exists():
        return candidate

    # From .app bundle: walk up to Contents/Resources/
    for parent in Path(__file__).parents:
        if parent.name == 'Resources' and parent.parent.name == 'Contents':
            candidate = parent / "claudeq-icon.png"
            if candidate.exists():
                return candidate
            break

    return None


def _remove_client_lock(tag: str) -> None:
    """Kill the old client process (if alive) and remove the lock file.

    Sending SIGTERM lets the client clean up its own lock via atexit,
    but we also unlink as a safety net in case the signal doesn't land.
    """
    pid = read_client_pid(tag)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    lock_file = SOCKET_DIR / f"{tag}.client.lock"
    try:
        if lock_file.exists():
            lock_file.unlink()
    except OSError:
        pass


def focus_session(tag: str, session_type: str = 'server') -> None:
    """
    Focus the terminal with the given session.

    Args:
        tag: Session tag name.
        session_type: 'server' or 'client'.
    """
    metadata = load_session_metadata(tag)

    preferred_ide = metadata.get('ide') if metadata else None
    project_path = metadata.get('project_path') if metadata else None
    title_pattern = f"cq-{session_type} {tag}"

    # Check if session exists
    if not session_exists(tag, session_type):
        reply = QMessageBox.question(
            None,
            f'{session_type.capitalize()} Not Found',
            f'{session_type.capitalize()} not found for: {tag}\n\n'
            f'Open a new {session_type}?',
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            open_terminal_with_command(
                f'cq {tag}',
                preferred_ide=preferred_ide,
                project_path=project_path,
            )
        return

    # Try to find and focus the terminal
    result = find_terminal_with_title(
        title_pattern,
        preferred_ide,
        project_path,
        title_pattern
    )

    if not result:
        # For clients: check if lock is held by a live process we can't find
        if session_type == 'client' and is_client_lock_held(tag):
            reply = QMessageBox.question(
                None,
                'Client Not Found',
                f'A client is connected to \'{tag}\' but its terminal '
                f'could not be found.\n\n'
                f'Replace it with a new client?',
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                _remove_client_lock(tag)
                open_terminal_with_command(
                    f'cq {tag}',
                    preferred_ide=preferred_ide,
                    project_path=project_path,
                )
        else:
            reply = QMessageBox.question(
                None,
                'Navigation Failed',
                f'Could not find terminal tab for {session_type}: {tag}\n\n'
                f'Open a new {session_type}?',
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                open_terminal_with_command(
                    f'cq {tag}',
                    preferred_ide=preferred_ide,
                    project_path=project_path,
                )
