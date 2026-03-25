"""Standalone utility functions for Leap Monitor."""

import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from leap.monitor.session_manager import read_client_pid
from leap.utils.constants import SOCKET_DIR


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
            [shell, '-l', '-i', '-c', 'env -0'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return
        for entry in result.stdout.split('\0'):
            if not entry:
                continue
            key, sep, value = entry.partition('=')
            if not sep or not key:
                continue
            if key == 'PATH':
                # Merge: prepend shell PATH entries that the app env is missing
                current = set(os.environ.get('PATH', '').split(':'))
                extra = [p for p in value.split(':') if p and p not in current]
                if extra:
                    os.environ['PATH'] = ':'.join(extra) + ':' + os.environ.get('PATH', '')
            elif key not in os.environ:
                os.environ[key] = value
    except Exception:
        logger.debug("Failed to load shell environment", exc_info=True)

    # Ensure UTF-8 locale — bundled .app may default to ASCII
    if not os.environ.get('LANG'):
        os.environ['LANG'] = 'en_US.UTF-8'
    if not os.environ.get('LC_ALL'):
        os.environ['LC_ALL'] = 'en_US.UTF-8'


def find_icon() -> Optional[Path]:
    """Find the app icon, works both from source and .app bundle."""
    # From source: src/leap/monitor/monitor_utils.py → project_root/assets/
    candidate = Path(__file__).parent.parent.parent.parent / "assets" / "leap-icon.png"
    if candidate.exists():
        return candidate

    # From .app bundle: walk up to Contents/Resources/
    for parent in Path(__file__).parents:
        if parent.name == 'Resources' and parent.parent.name == 'Contents':
            candidate = parent / "leap-icon.png"
            if candidate.exists():
                return candidate
            break

    return None


def find_notes_icon() -> Optional[Path]:
    """Find the notes icon (pen-to-square), works from source and .app bundle."""
    candidate = Path(__file__).parent.parent.parent.parent / "assets" / "notes-icon.png"
    if candidate.exists():
        return candidate

    for parent in Path(__file__).parents:
        if parent.name == 'Resources' and parent.parent.name == 'Contents':
            candidate = parent / "notes-icon.png"
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
