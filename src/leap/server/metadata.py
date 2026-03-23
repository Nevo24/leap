"""
Session metadata management for Leap server.

Handles saving and loading session metadata including IDE, project path, and branch.
"""

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.registry import DEFAULT_PROVIDER
from leap.utils.ide_detection import detect_ide, get_git_branch

logger = logging.getLogger(__name__)


class SessionMetadata:
    """Manages session metadata for a Leap server instance."""

    def __init__(self, tag: str, socket_dir: Path) -> None:
        """
        Initialize session metadata manager.

        Args:
            tag: Session tag name.
            socket_dir: Directory for socket and metadata files.
        """
        self.tag = tag
        self.metadata_file = socket_dir / f"{tag}.meta"
        self._data: dict[str, Any] = {}

    @staticmethod
    def _get_git_root(cwd: str) -> Optional[str]:
        """Get the git repository root directory for a given path."""
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--show-toplevel'],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=2
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            pass
        return None

    def save(self, cli_provider: Optional[str] = None) -> None:
        """Save metadata about the session to disk.

        Args:
            cli_provider: CLI provider name (e.g. 'claude', 'codex', 'cursor-agent').
        """
        ide = detect_ide()
        cwd = os.getcwd()
        project_path = self._get_git_root(cwd) or cwd
        branch_name = get_git_branch(project_path)

        self._data = {
            'ide': ide,
            'terminal_title': f"lps {self.tag}",
            'tag': self.tag,
            'pid': os.getpid(),
            'project_path': project_path,
            'branch': branch_name,
            'cli_provider': cli_provider or DEFAULT_PROVIDER,
        }

        try:
            dir_path = self.metadata_file.parent
            fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix='.tmp')
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(self._data, f, indent=2)
                os.replace(tmp_path, str(self.metadata_file))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError:
            logger.warning("Failed to persist metadata to %s", self.metadata_file, exc_info=True)

    def load(self) -> dict[str, Any]:
        """
        Load metadata from disk.

        Returns:
            Dictionary with session metadata.
        """
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, 'r') as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupted metadata file %s, using defaults", self.metadata_file)
                self._data = {}
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
