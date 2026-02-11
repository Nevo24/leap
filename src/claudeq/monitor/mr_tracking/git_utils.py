"""Git remote parsing utilities."""

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class SCMType(Enum):
    """Type of source code management platform."""
    GITLAB = "gitlab"
    GITHUB = "github"
    UNKNOWN = "unknown"


@dataclass
class GitRemoteInfo:
    """Parsed git remote information."""
    branch: str
    remote_url: str
    project_path: str
    host_url: str
    scm_type: SCMType = SCMType.UNKNOWN


@dataclass
class ParsedMRUrl:
    """Parsed MR/PR URL information."""
    scm_type: SCMType
    host_url: str
    project_path: str
    mr_iid: int


def parse_mr_url(url: str, gitlab_config: Optional[dict[str, Any]] = None) -> Optional[ParsedMRUrl]:
    """Parse a GitLab MR or GitHub PR URL.

    Supported formats:
        GitLab: https://gitlab.com/group/project/-/merge_requests/42
        GitHub: https://github.com/owner/repo/pull/42

    Args:
        url: The MR/PR URL.
        gitlab_config: Optional GitLab config dict for custom host detection.

    Returns:
        ParsedMRUrl or None if the URL cannot be parsed.
    """
    # GitLab: https://<host>/<project_path>/-/merge_requests/<iid>
    m = re.match(r'https?://([^/]+)/(.+?)/-/merge_requests/(\d+)', url)
    if m:
        host_url = f"https://{m.group(1)}"
        return ParsedMRUrl(
            scm_type=detect_scm_type(host_url, gitlab_config),
            host_url=host_url,
            project_path=m.group(2),
            mr_iid=int(m.group(3)),
        )

    # GitHub: https://<host>/<owner>/<repo>/pull/<number>
    m = re.match(r'https?://([^/]+)/([^/]+/[^/]+)/pull/(\d+)', url)
    if m:
        host_url = f"https://{m.group(1)}"
        return ParsedMRUrl(
            scm_type=detect_scm_type(host_url, gitlab_config),
            host_url=host_url,
            project_path=m.group(2),
            mr_iid=int(m.group(3)),
        )

    return None


def detect_scm_type(host_url: str, gitlab_config: Optional[dict[str, Any]] = None) -> SCMType:
    """Detect SCM platform type from a git remote host URL.

    Args:
        host_url: The host URL (e.g., 'https://github.com').
        gitlab_config: Optional GitLab config dict with 'gitlab_url' key.

    Returns:
        SCMType indicating the platform.
    """
    if not host_url:
        return SCMType.UNKNOWN

    host_lower = host_url.lower().rstrip('/')
    if 'github.com' in host_lower:
        return SCMType.GITHUB

    if gitlab_config:
        gitlab_url = gitlab_config.get('gitlab_url', '').lower().rstrip('/')
        if gitlab_url and gitlab_url in host_lower:
            return SCMType.GITLAB

    # Default heuristic: if host contains 'gitlab', assume GitLab
    if 'gitlab' in host_lower:
        return SCMType.GITLAB

    return SCMType.UNKNOWN


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

        scm_type = detect_scm_type(host_url)

        return GitRemoteInfo(
            branch=branch,
            remote_url=remote_url,
            project_path=project_path,
            host_url=host_url,
            scm_type=scm_type,
        )

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
