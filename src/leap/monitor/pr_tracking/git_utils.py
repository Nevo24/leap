"""Git remote parsing utilities."""

import logging
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


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
class ParsedPRUrl:
    """Parsed PR URL information."""
    scm_type: SCMType
    host_url: str
    project_path: str
    pr_iid: int


@dataclass
class ParsedProjectUrl:
    """Parsed project URL information (no PR number)."""
    scm_type: SCMType
    host_url: str
    project_path: str
    commit: Optional[str] = None  # Commit SHA if parsed from a commit URL


def parse_pr_url(
    url: str,
    gitlab_config: Optional[dict[str, Any]] = None,
    github_config: Optional[dict[str, Any]] = None,
) -> Optional[ParsedPRUrl]:
    """Parse a GitLab PR or GitHub PR URL.

    Supported formats:
        GitLab: https://gitlab.com/group/project/-/merge_requests/42
        GitHub: https://github.com/owner/repo/pull/42

    Args:
        url: The PR URL.
        gitlab_config: Optional GitLab config dict for custom host detection.
        github_config: Optional GitHub config dict for custom host detection.

    Returns:
        ParsedPRUrl or None if the URL cannot be parsed.
    """
    # GitLab: https://<host>/<project_path>/-/merge_requests/<iid>
    m = re.match(r'https?://([^/]+)/(.+?)/-/merge_requests/(\d+)', url)
    if m:
        host_url = f"https://{m.group(1)}"
        scm_type = detect_scm_type(host_url, gitlab_config, github_config)
        # URL structure is exclusively GitLab
        if scm_type == SCMType.UNKNOWN:
            scm_type = SCMType.GITLAB
        return ParsedPRUrl(
            scm_type=scm_type,
            host_url=host_url,
            project_path=m.group(2),
            pr_iid=int(m.group(3)),
        )

    # GitHub: https://<host>/<owner>/<repo>/pull/<number>
    m = re.match(r'https?://([^/]+)/([^/]+/[^/]+)/pull/(\d+)', url)
    if m:
        host_url = f"https://{m.group(1)}"
        scm_type = detect_scm_type(host_url, gitlab_config, github_config)
        # URL structure is exclusively GitHub
        if scm_type == SCMType.UNKNOWN:
            scm_type = SCMType.GITHUB
        return ParsedPRUrl(
            scm_type=scm_type,
            host_url=host_url,
            project_path=m.group(2),
            pr_iid=int(m.group(3)),
        )

    return None


def detect_scm_type(
    host_url: str,
    gitlab_config: Optional[dict[str, Any]] = None,
    github_config: Optional[dict[str, Any]] = None,
) -> SCMType:
    """Detect SCM platform type from a git remote host URL.

    Args:
        host_url: The host URL (e.g., 'https://github.com').
        gitlab_config: Optional GitLab config dict with 'gitlab_url' key.
        github_config: Optional GitHub config dict with 'github_url' key.

    Returns:
        SCMType indicating the platform.
    """
    if not host_url:
        return SCMType.UNKNOWN

    host_lower = host_url.lower().rstrip('/')
    if 'github.com' in host_lower:
        return SCMType.GITHUB

    if github_config:
        github_url = github_config.get('github_url', '').lower().rstrip('/')
        if github_url and github_url in host_lower:
            return SCMType.GITHUB

    if gitlab_config:
        gitlab_url = gitlab_config.get('gitlab_url', '').lower().rstrip('/')
        if gitlab_url and gitlab_url in host_lower:
            return SCMType.GITLAB

    # Default heuristic: if host contains 'gitlab', assume GitLab
    if 'gitlab' in host_lower:
        return SCMType.GITLAB

    return SCMType.UNKNOWN


def refine_scm_type(host_url: str, scm_type: SCMType) -> SCMType:
    """Refine an UNKNOWN SCM type by checking against saved provider configs.

    Loads GitLab and GitHub configs from disk and re-runs detection. This is
    useful after ``get_git_remote_info()`` which only uses hostname heuristics.

    Args:
        host_url: The host URL to check.
        scm_type: The current (possibly UNKNOWN) SCM type.

    Returns:
        Refined SCMType, or the original if still unresolvable.
    """
    if scm_type != SCMType.UNKNOWN:
        return scm_type

    # Import here to avoid circular import (config imports are lightweight)
    from leap.monitor.pr_tracking.config import (
        load_github_config, load_gitlab_config,
    )

    return detect_scm_type(
        host_url,
        gitlab_config=load_gitlab_config(),
        github_config=load_github_config(),
    )


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
            # HTTPS format: https://[user:pass@]host/project.git
            https_match = re.match(r'https://([^/]+)/(.+?)(?:\.git)?$', remote_url)
            if https_match:
                host = https_match.group(1)
                # Strip credentials (user:pass@) if present
                if '@' in host:
                    host = host.rsplit('@', 1)[-1]
                host_url = f"https://{host}"
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


# Known path suffixes on GitLab/GitHub that follow the project path
_PROJECT_URL_SUFFIXES = re.compile(
    r'(?:/-/(?:tree|blob|merge_requests|issues|pipelines|commits|branches|tags|settings)(?:/.*)?'
    r'|/(?:tree|blob|pull|issues|actions|commits|branches|tags|settings)(?:/.*)?'
    r')$'
)


def parse_project_url(
    url: str,
    gitlab_config: Optional[dict[str, Any]] = None,
    github_config: Optional[dict[str, Any]] = None,
) -> Optional[ParsedProjectUrl]:
    """Parse a plain Git project URL (HTTPS or SSH).

    Supported formats:
        HTTPS: https://host/group/project[.git]
        HTTPS with path suffixes: https://host/group/project/-/tree/main
        SSH: git@host:group/project[.git]

    Args:
        url: The project URL.
        gitlab_config: Optional GitLab config dict for custom host detection.
        github_config: Optional GitHub config dict for custom host detection.

    Returns:
        ParsedProjectUrl or None if the URL cannot be parsed.
    """
    url = url.strip()

    # SSH: git@host:group/project[.git]
    m = re.match(r'git@([^:]+):(.+?)(?:\.git)?$', url)
    if m:
        host_url = f"https://{m.group(1)}"
        project_path = m.group(2).rstrip('/')
        if '/' not in project_path:
            return None
        return ParsedProjectUrl(
            scm_type=detect_scm_type(host_url, gitlab_config, github_config),
            host_url=host_url,
            project_path=project_path,
        )

    # HTTPS: https://host/group/project[.git][/-/tree/...]
    m = re.match(r'https?://([^/]+)/(.+?)(?:\.git)?/?$', url)
    if not m:
        return None

    host_url = f"https://{m.group(1)}"
    raw_path = m.group(2).rstrip('/')

    # Check for commit URL: /-/commit/<sha> (GitLab) or /commit/<sha> (GitHub)
    commit_match = re.search(r'(?:/-)?/commit/([0-9a-fA-F]{7,40})(?:/.*)?$', raw_path)
    commit_sha = commit_match.group(1) if commit_match else None
    if commit_match:
        raw_path = raw_path[:commit_match.start()]

    # Strip known path suffixes (e.g. /-/tree/main, /pull/42)
    project_path = _PROJECT_URL_SUFFIXES.sub('', raw_path).rstrip('/')

    if '/' not in project_path:
        return None

    return ParsedProjectUrl(
        scm_type=detect_scm_type(host_url, gitlab_config, github_config),
        host_url=host_url,
        project_path=project_path,
        commit=commit_sha,
    )


def detect_default_branch(project_path: str) -> str:
    """Detect the default remote branch for a git repository.

    Reads ``refs/remotes/origin/HEAD``.  If the ref is missing (common after
    a fresh clone), runs ``git remote set-head origin --auto`` to fetch it
    from the remote and retries.

    Args:
        project_path: Filesystem path to the git working tree.

    Returns:
        Branch name (e.g. ``'main'``).  Falls back to ``'main'`` if detection
        fails entirely.
    """
    try:
        result = subprocess.run(
            ['git', 'symbolic-ref', 'refs/remotes/origin/HEAD'],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().rsplit('/', 1)[-1]

        # origin/HEAD not set locally — fetch it from remote and retry
        subprocess.run(
            ['git', 'remote', 'set-head', 'origin', '--auto'],
            cwd=project_path,
            capture_output=True,
            timeout=10,
        )
        result = subprocess.run(
            ['git', 'symbolic-ref', 'refs/remotes/origin/HEAD'],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().rsplit('/', 1)[-1]
    except Exception:
        logger.debug("Failed to detect default branch for %s", project_path, exc_info=True)
    return 'main'
