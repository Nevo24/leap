"""
Session management for Leap monitor.

Discovers and tracks active Leap sessions.
"""

import fcntl
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from leap.utils.constants import SOCKET_DIR
from leap.utils.ide_detection import get_git_branch
from leap.utils.socket_utils import send_socket_request


def query_server_status(socket_path: Path) -> Optional[dict[str, Any]]:
    """
    Query server for status via socket.

    Args:
        socket_path: Path to the server's Unix socket.

    Returns:
        Status response dictionary or None on error.
    """
    return send_socket_request(
        socket_path, {'type': 'status', 'message': ''}, timeout=1.0
    )


def load_session_metadata(tag: str) -> Optional[dict[str, Any]]:
    """
    Load metadata for a session.

    Args:
        tag: Session tag name.

    Returns:
        Metadata dictionary or None if not found.
    """
    metadata_file = SOCKET_DIR / f"{tag}.meta"
    if metadata_file.exists():
        try:
            with open(metadata_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _is_lock_held(lock_path: Path) -> bool:
    """
    Check if a lock file is held by a live process.

    Uses a non-blocking flock probe to detect if another process holds the lock,
    without needing the PID to be written in the file.

    Args:
        lock_path: Path to the lock file.

    Returns:
        True if the lock is held by another process.
    """
    fd = None
    try:
        fd = open(lock_path, 'r')
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Got the lock — no one is holding it, stale file
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                fd.close()
            except OSError:
                pass


def is_client_lock_held(tag: str) -> bool:
    """
    Check if a client lock is held by a live process.

    Args:
        tag: Session tag name.

    Returns:
        True if the client lock file exists and is held by a live process.
    """
    client_lock = SOCKET_DIR / f"{tag}.client.lock"
    if not client_lock.exists():
        return False
    return _is_lock_held(client_lock)


def read_client_pid(tag: str) -> Optional[int]:
    """
    Read the client PID from its lock file.

    Args:
        tag: Session tag name.

    Returns:
        Client PID if readable, or None.
    """
    client_lock = SOCKET_DIR / f"{tag}.client.lock"
    if not client_lock.exists():
        return None
    try:
        with open(client_lock, 'r') as f:
            pid_str = f.read().strip()
            if pid_str:
                return int(pid_str)
    except (OSError, ValueError):
        pass
    return None


def _get_git_project_name(cwd: str) -> Optional[str]:
    """Extract the git project name from the remote origin URL.

    Args:
        cwd: Working directory to run git in.

    Returns:
        Project name (e.g. 'leap') or None if not a git repo / no remote.
    """
    try:
        result = subprocess.run(
            ['git', 'config', '--get', 'remote.origin.url'],
            capture_output=True, text=True, cwd=cwd, timeout=2,
        )
        if result.returncode != 0:
            return None
        remote_url = result.stdout.strip()
        # SSH: git@host:user/project.git  or  HTTPS: https://host/user/project.git
        m = re.match(r'(?:git@[^:]+:|https?://[^/]+/)(.+?)(?:\.git)?$', remote_url)
        if m:
            return m.group(1).rsplit('/', 1)[-1]
    except Exception:
        pass
    return None


_last_good_status: dict[str, dict[str, Any]] = {}


def get_active_sessions() -> list[dict[str, Any]]:
    """
    Get list of active Leap sessions.

    Returns:
        List of session dictionaries with tag, status, queue info, etc.
    """
    sessions = []

    if not SOCKET_DIR.exists():
        return sessions

    current_tags: set[str] = set()

    for socket_file in SOCKET_DIR.glob("*.sock"):
        tag = socket_file.stem
        current_tags.add(tag)
        status_response = query_server_status(socket_file)

        if status_response:
            _last_good_status[tag] = status_response
        elif tag in _last_good_status:
            # Server busy — reuse last known good status so the row
            # doesn't flicker/disappear for one refresh cycle.
            status_response = _last_good_status[tag]
        else:
            continue

        queue_size = status_response.get('queue_size', 0)
        claude_state = status_response.get('claude_state', 'idle')
        auto_send_mode = status_response.get('auto_send_mode', 'pause')
        slack_enabled = status_response.get('slack_enabled', False)

        # Load metadata
        metadata = load_session_metadata(tag)
        project_name = None
        branch_name = None
        project_path = None
        ide = None

        if metadata:
            project_path = metadata.get('project_path', '')
            if project_path:
                project_name = _get_git_project_name(project_path)
            branch_name = (
                get_git_branch(project_path) if project_path
                else metadata.get('branch')
            )
            ide = metadata.get('ide')

        # Server PID from metadata
        server_pid: Optional[int] = None
        if metadata:
            pid_val = metadata.get('pid')
            if pid_val is not None:
                server_pid = int(pid_val)

        # Client PID from lock file
        client_pid: Optional[int] = None
        has_client = False
        client_lock = SOCKET_DIR / f"{tag}.client.lock"
        if client_lock.exists():
            try:
                with open(client_lock, 'r') as f:
                    pid_str = f.read().strip()
                    if pid_str:
                        client_pid = int(pid_str)
                        has_client = True
                    else:
                        # Empty lock file — probe if held by a live process
                        has_client = _is_lock_held(client_lock)
            except (OSError, ValueError):
                pass

        cli_provider = status_response.get('cli_provider', 'claude')

        sessions.append({
            'tag': tag,
            'claude_state': claude_state,
            'auto_send_mode': auto_send_mode,
            'queue_size': queue_size,
            'project': project_name or 'N/A',
            'branch': branch_name or 'N/A',
            'project_path': project_path,
            'ide': ide,
            'server_pid': server_pid,
            'client_pid': client_pid,
            'has_client': has_client,
            'slack_enabled': slack_enabled,
            'cli_provider': cli_provider,
        })

    # Evict stale cache entries for sockets that no longer exist
    stale = [t for t in _last_good_status if t not in current_tags]
    for t in stale:
        _last_good_status.pop(t, None)

    return sorted(sessions, key=lambda x: x['tag'])


def session_exists(tag: str, session_type: str) -> bool:
    """
    Check if a session exists.

    Args:
        tag: Session tag name.
        session_type: 'server' or 'client'.

    Returns:
        True if the session exists.
    """
    if session_type == 'client':
        return (SOCKET_DIR / f"{tag}.client.lock").exists()
    elif session_type == 'server':
        return (SOCKET_DIR / f"{tag}.sock").exists()
    return False
