"""Git remote parsing utilities."""

import re
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class GitRemoteInfo:
    """Parsed git remote information."""
    branch: str
    remote_url: str
    project_path: str
    host_url: str


def get_git_remote_info(cwd: str) -> Optional[GitRemoteInfo]:
    """Parse git remote info from a working directory.

    Args:
        cwd: Working directory to run git commands in.

    Returns:
        GitRemoteInfo or None if not a git repo or no remote.
    """
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, check=True,
            cwd=cwd, timeout=2
        )
        branch = result.stdout.strip()
        if not branch:
            return None

        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=True,
            cwd=cwd, timeout=2
        )
        remote_url = result.stdout.strip()

        host_url = None
        project_path = None

        # SSH format: git@gitlab.com:user/project.git
        ssh_match = re.match(r'git@([^:]+):(.+?)(?:\.git)?$', remote_url)
        if ssh_match:
            host_url = f"https://{ssh_match.group(1)}"
            project_path = ssh_match.group(2)
        else:
            # HTTPS format: https://gitlab.com/user/project.git
            https_match = re.match(r'https://([^/]+)/(.+?)(?:\.git)?$', remote_url)
            if https_match:
                host_url = f"https://{https_match.group(1)}"
                project_path = https_match.group(2)

        if not project_path or not host_url:
            return None

        return GitRemoteInfo(
            branch=branch,
            remote_url=remote_url,
            project_path=project_path,
            host_url=host_url,
        )

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
