"""
Session management for ClaudeQ monitor.

Discovers and tracks active ClaudeQ sessions.
"""

import json
import os
import socket
from pathlib import Path
from typing import Any, Optional

from claudeq.utils.constants import SOCKET_DIR


def query_server_status(socket_path: Path) -> Optional[dict[str, Any]]:
    """
    Query server for status via socket.

    Args:
        socket_path: Path to the server's Unix socket.

    Returns:
        Status response dictionary or None on error.
    """
    try:
        client_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client_socket.settimeout(1.0)
        client_socket.connect(str(socket_path))

        data = {'type': 'status', 'message': ''}
        client_socket.send(json.dumps(data).encode('utf-8'))
        chunks = []
        while True:
            chunk = client_socket.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        client_socket.close()
        response = b''.join(chunks).decode('utf-8')

        return json.loads(response)
    except (socket.error, json.JSONDecodeError, OSError):
        return None


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


def get_active_sessions() -> list[dict[str, Any]]:
    """
    Get list of active ClaudeQ sessions.

    Returns:
        List of session dictionaries with tag, status, queue info, etc.
    """
    sessions = []

    if not SOCKET_DIR.exists():
        return sessions

    for socket_file in SOCKET_DIR.glob("*.sock"):
        tag = socket_file.stem
        status_response = query_server_status(socket_file)

        if not status_response:
            continue

        queue_size = status_response.get('queue_size', 0)
        is_ready = status_response.get('ready', True)
        claude_busy = not is_ready

        # Load metadata
        metadata = load_session_metadata(tag)
        project_name = None
        branch_name = None
        project_path = None
        ide = None

        if metadata:
            project_path = metadata.get('project_path', '')
            if project_path:
                project_name = os.path.basename(project_path)
            branch_name = metadata.get('branch')
            ide = metadata.get('ide')

        sessions.append({
            'tag': tag,
            'claude_busy': claude_busy,
            'queue_size': queue_size,
            'project': project_name or 'N/A',
            'branch': branch_name or 'N/A',
            'project_path': project_path,
            'ide': ide,
        })

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
