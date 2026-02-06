"""
Session metadata management for ClaudeQ server.

Handles saving and loading session metadata including IDE, project path, and branch.
"""

import json
import os
from pathlib import Path
from typing import Any, Optional

from claudeq.utils.ide_detection import detect_ide, get_git_branch


class SessionMetadata:
    """Manages session metadata for a ClaudeQ server instance."""

    def __init__(self, tag: str, socket_dir: Path):
        """
        Initialize session metadata manager.

        Args:
            tag: Session tag name.
            socket_dir: Directory for socket and metadata files.
        """
        self.tag = tag
        self.metadata_file = socket_dir / f"{tag}.meta"
        self._data: dict[str, Any] = {}

    def save(self) -> None:
        """Save metadata about the session to disk."""
        ide = detect_ide()
        project_path = os.getcwd()
        branch_name = get_git_branch(project_path)

        self._data = {
            'ide': ide,
            'terminal_title': f"cq-server {self.tag}",
            'tag': self.tag,
            'pid': os.getpid(),
            'project_path': project_path,
            'branch': branch_name
        }

        with open(self.metadata_file, 'w') as f:
            json.dump(self._data, f, indent=2)

    def load(self) -> dict[str, Any]:
        """
        Load metadata from disk.

        Returns:
            Dictionary with session metadata.
        """
        if self.metadata_file.exists():
            with open(self.metadata_file, 'r') as f:
                self._data = json.load(f)
        return self._data

    def cleanup(self) -> None:
        """Remove metadata file."""
        try:
            if self.metadata_file.exists():
                self.metadata_file.unlink()
        except OSError:
            pass

    @property
    def ide(self) -> Optional[str]:
        """Get detected IDE name."""
        return self._data.get('ide')

    @property
    def project_path(self) -> Optional[str]:
        """Get project path."""
        return self._data.get('project_path')

    @property
    def branch(self) -> Optional[str]:
        """Get git branch name."""
        return self._data.get('branch')
