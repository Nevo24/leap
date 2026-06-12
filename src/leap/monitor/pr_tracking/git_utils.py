"""Git remote parsing utilities."""

import logging
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from leap.monitor.pr_tracking.config import (
    load_bitbucket_config, load_github_config, load_gitlab_config,
)

logger = logging.getLogger(__name__)


# Per-process cache for `ssh -G <host>` results.  Cleared on monitor restart;
# avoids the subprocess call for every remote-info lookup but doesn't survive
# changes to ~/.ssh/config across sessions.
_SSH_HOST_CACHE: dict[str, str] = {}


def resolve_ssh_alias(host_fragment: str) -> str:
    """Resolve an SSH host alias to its real hostname via ``ssh -G``.

    Many users set up aliases in ``~/.ssh/config`` to clone with a specific
    identity file or jump host (e.g. ``Host planck_gitlab\\n HostName
    gitlab.com``).  These aliases work over SSH but a literal HTTPS URL
    built by prefixing ``https://`` to the alias is not DNS-resolvable.

    This helper runs ``ssh -G <alias>`` to read the ``hostname`` line from
    the resolved config.  Returns the original *host_fragment* if SSH
    isn't available or the resolution fails — preserving the caller's
    behaviour for hosts that are already proper DNS names.
    """
    if not host_fragment:
        return host_fragment
    # If it already looks like a real DNS name (has a dot or is localhost),
    # don't bother shelling out.  ssh -G would just echo it back.
    if '.' in host_fragment or host_fragment in ('localhost',):
        return host_fragment
    if host_fragment in _SSH_HOST_CACHE:
        return _SSH_HOST_CACHE[host_fragment]
    try:
        result = subprocess.run(
            ['ssh', '-G', host_fragment],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        _SSH_HOST_CACHE[host_fragment] = host_fragment
        return host_fragment

    resolved = host_fragment
    for line in result.stdout.splitlines():
        # Lines look like 'hostname gitlab.com' — case-insensitive, leading
        # whitespace possible (different ssh implementations vary slightly).
        stripped = line.strip()
        if stripped.lower().startswith('hostname '):
            candidate = stripped.split(None, 1)[1].strip()
            # If ssh resolved to the same string we asked for, treat as
            # "no alias defined" — keep the original.
            if candidate and candidate.lower() != host_fragment.lower():
                resolved = candidate
            break
    _SSH_HOST_CACHE[host_fragment] = resolved
    return resolved


class SCMType(Enum):
    """Type of source code management platform."""
    GITLAB = "gitlab"
    GITHUB = "github"
    BITBUCKET = "bitbucket"
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


def _bare_host(url: str) -> str:
    """Extract just the hostname from a URL (no scheme/creds/port/path)."""
    host = url.split('://', 1)[-1]
    return host.split('/', 1)[0].rsplit('@', 1)[-1].split(':', 1)[0]


def _normalize_host(host_with_creds: str) -> str:
    """Strip ``user:pass@`` and resolve any SSH alias in *host_with_creds*.

    Used by ``parse_pr_url`` / ``parse_project_url`` so a user who pastes
    a URL using their SSH alias as the hostname (e.g.
    ``https://planck_gitlab/...``) gets the same alias-resolution that
    the local-clone path already does.
    """
    host = (
        host_with_creds.rsplit('@', 1)[-1]
        if '@' in host_with_creds else host_with_creds
    )
    return resolve_ssh_alias(host)


def parse_pr_url(
    url: str,
    gitlab_config: Optional[dict[str, Any]] = None,
    github_config: Optional[dict[str, Any]] = None,
    bitbucket_config: Optional[dict[str, Any]] = None,
) -> Optional[ParsedPRUrl]:
    """Parse a GitLab, GitHub or Bitbucket PR URL.

    Supported formats:
        GitLab: https://gitlab.com/group/project/-/merge_requests/42
        GitHub: https://github.com/owner/repo/pull/42
        Bitbucket Cloud: https://bitbucket.org/workspace/repo/pull-requests/42
        Bitbucket Server: https://host/projects/KEY/repos/repo/pull-requests/42

    Args:
        url: The PR URL.
        gitlab_config: Optional GitLab config dict for custom host detection.
        github_config: Optional GitHub config dict for custom host detection.
        bitbucket_config: Optional Bitbucket config dict for custom host detection.

    Returns:
        ParsedPRUrl or None if the URL cannot be parsed.
    """
    # GitLab: https://<host>/<project_path>/-/merge_requests/<iid>
    m = re.match(r'https?://([^/]+)/(.+?)/-/merge_requests/(\d+)', url)
    if m:
        host_url = f"https://{_normalize_host(m.group(1))}"
        scm_type = detect_scm_type(host_url, gitlab_config, github_config,
                                   bitbucket_config)
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
        host_url = f"https://{_normalize_host(m.group(1))}"
        scm_type = detect_scm_type(host_url, gitlab_config, github_config,
                                   bitbucket_config)
        # URL structure is exclusively GitHub
        if scm_type == SCMType.UNKNOWN:
            scm_type = SCMType.GITHUB
        return ParsedPRUrl(
            scm_type=scm_type,
            host_url=host_url,
            project_path=m.group(2),
            pr_iid=int(m.group(3)),
        )

    # Bitbucket Server/DC: https://<host>[/context]/projects/<KEY>/repos/<slug>/pull-requests/<id>
    # Must be tried before the Cloud pattern - a Server URL would otherwise
    # match the generic <a>/<b>/pull-requests/<id> shape with the wrong path.
    # The optional context segments (installs under e.g. /stash) become part
    # of host_url so clone/fetch URL construction keeps working.
    m = re.match(
        r'https?://([^/]+)((?:/[^/]+)*?)/projects/([^/]+)/repos/([^/]+)'
        r'/pull-requests/(\d+)',
        url)
    if m:
        host_url = f"https://{_normalize_host(m.group(1))}{m.group(2)}"
        # URL structure is exclusively Bitbucket Server.  Project path is
        # normalized to KEY/slug (the canonical form the provider expects).
        return ParsedPRUrl(
            scm_type=SCMType.BITBUCKET,
            host_url=host_url,
            project_path=f'{m.group(3)}/{m.group(4)}',
            pr_iid=int(m.group(5)),
        )

    # Bitbucket Cloud: https://<host>/<workspace>/<repo>/pull-requests/<id>
    m = re.match(r'https?://([^/]+)/([^/]+/[^/]+)/pull-requests/(\d+)', url)
    if m:
        host_url = f"https://{_normalize_host(m.group(1))}"
        scm_type = detect_scm_type(host_url, gitlab_config, github_config,
                                   bitbucket_config)
        # URL structure is exclusively Bitbucket
        if scm_type == SCMType.UNKNOWN:
            scm_type = SCMType.BITBUCKET
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
    bitbucket_config: Optional[dict[str, Any]] = None,
) -> SCMType:
    """Detect SCM platform type from a git remote host URL.

    Args:
        host_url: The host URL (e.g., 'https://github.com').
        gitlab_config: Optional GitLab config dict with 'gitlab_url' key.
        github_config: Optional GitHub config dict with 'github_url' key.
        bitbucket_config: Optional Bitbucket config dict with 'bitbucket_url' key.

    Returns:
        SCMType indicating the platform.
    """
    if not host_url:
        return SCMType.UNKNOWN

    host_lower = host_url.lower().rstrip('/')
    if 'github.com' in host_lower:
        return SCMType.GITHUB

    if 'bitbucket.org' in host_lower:
        return SCMType.BITBUCKET

    if github_config:
        github_url = (github_config.get('github_url') or '').lower().rstrip('/')
        # Saved URL may include the /api/v3 API suffix; strip it before
        # comparing against the bare host URL from the git remote.
        if github_url.endswith('/api/v3'):
            github_url = github_url[: -len('/api/v3')]
        if github_url and github_url in host_lower:
            return SCMType.GITHUB

    if gitlab_config:
        gitlab_url = (gitlab_config.get('gitlab_url') or '').lower().rstrip('/')
        if gitlab_url and gitlab_url in host_lower:
            return SCMType.GITLAB

    if bitbucket_config:
        bitbucket_url = (bitbucket_config.get('bitbucket_url') or '').lower().rstrip('/')
        if bitbucket_url and bitbucket_url in host_lower:
            return SCMType.BITBUCKET
        # Context-path installs: the saved URL 'https://host/stash' is never
        # a substring of the bare 'https://host' a git remote yields, so
        # fall back to comparing hostnames.
        if bitbucket_url and _bare_host(bitbucket_url) == _bare_host(host_lower):
            return SCMType.BITBUCKET

    # Default heuristics on the hostname.  'gitlab' is checked first so
    # every host this function classified as GitLab before Bitbucket
    # support existed still resolves identically (strictly additive).
    # 'stash' is Bitbucket Server's pre-rename product name and is still
    # a very common self-hosted subdomain (stash.company.com).
    if 'gitlab' in host_lower:
        return SCMType.GITLAB
    if 'bitbucket' in host_lower or 'stash' in host_lower:
        return SCMType.BITBUCKET

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

    return detect_scm_type(
        host_url,
        gitlab_config=load_gitlab_config(),
        github_config=load_github_config(),
        bitbucket_config=load_bitbucket_config(),
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

        # ssh:// URI format: ssh://git@gitlab.com[:22]/group/project.git
        # (Port is standardised in ssh:// URIs — explicit and unambiguous.)
        ssh_uri_match = re.match(
            r'ssh://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$',
            remote_url,
        )
        if ssh_uri_match:
            host_fragment = ssh_uri_match.group(1)
            project_path = ssh_uri_match.group(2)
            host_url = f"https://{resolve_ssh_alias(host_fragment)}"
        else:
            # scp-style SSH: git@host:path[.git]
            # NOTE: there is no port-disambiguation in scp-style syntax.
            # ``git@host:42/repo`` means *path = "42/repo"* — git itself
            # treats it as such (numeric GitLab groups are valid).  Anyone
            # who needs a non-default SSH port must use the ssh:// URI form
            # above, which is unambiguous.  An earlier version of this
            # regex tried to detect ``:port/path`` here and broke numeric
            # GitLab groups — don't reintroduce that.
            ssh_match = re.match(
                r'git@([^:/]+):(.+?)(?:\.git)?/?$',
                remote_url,
            )
            if ssh_match:
                host_fragment = ssh_match.group(1)
                project_path = ssh_match.group(2)
                host_url = f"https://{resolve_ssh_alias(host_fragment)}"
            else:
                # HTTPS format: https://[user:pass@]host/project.git
                https_match = re.match(
                    r'https://([^/]+)/(.+?)(?:\.git)?$', remote_url)
                if https_match:
                    host_url = f"https://{_normalize_host(https_match.group(1))}"
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

# Known path suffixes on Bitbucket Cloud that follow workspace/repo
_BITBUCKET_URL_SUFFIXES = re.compile(
    r'/(?:src|commits|branches|pull-requests|downloads|pipelines'
    r'|deployments|wiki|issues|admin)(?:/.*)?$'
)


def parse_project_url(
    url: str,
    gitlab_config: Optional[dict[str, Any]] = None,
    github_config: Optional[dict[str, Any]] = None,
    bitbucket_config: Optional[dict[str, Any]] = None,
) -> Optional[ParsedProjectUrl]:
    """Parse a plain Git project URL (HTTPS or SSH).

    Supported formats:
        HTTPS: https://host/group/project[.git]
        HTTPS with path suffixes: https://host/group/project/-/tree/main
        SSH: git@host:group/project[.git]
        Bitbucket Server web: https://host/projects/KEY/repos/repo[/browse]
        Bitbucket Server clone: https://host/scm/KEY/repo[.git]

    Args:
        url: The project URL.
        gitlab_config: Optional GitLab config dict for custom host detection.
        github_config: Optional GitHub config dict for custom host detection.
        bitbucket_config: Optional Bitbucket config dict for custom host detection.

    Returns:
        ParsedProjectUrl or None if the URL cannot be parsed.
    """
    url = url.strip()

    # SSH: git@host:group/project[.git]
    m = re.match(r'git@([^:]+):(.+?)(?:\.git)?$', url)
    if m:
        host_url = f"https://{_normalize_host(m.group(1))}"
        project_path = m.group(2).rstrip('/')
        if '/' not in project_path:
            return None
        return ParsedProjectUrl(
            scm_type=detect_scm_type(host_url, gitlab_config, github_config,
                                     bitbucket_config),
            host_url=host_url,
            project_path=project_path,
        )

    # Bitbucket Server web URL: https://host[/context]/projects/KEY/repos/repo[/...]
    # Gated on the host NOT detecting as GitLab/GitHub, so a GitLab group
    # that happens to be named 'projects' keeps the generic handling.
    m = re.match(
        r'https?://([^/]+)((?:/[^/]+)*?)/projects/([^/]+)/repos/([^/]+?)'
        r'(?:\.git)?(/.*)?$',
        url)
    if m:
        host_url = f"https://{_normalize_host(m.group(1))}{m.group(2)}"
        detected = detect_scm_type(host_url, gitlab_config, github_config,
                                   bitbucket_config)
        if detected not in (SCMType.GITLAB, SCMType.GITHUB):
            tail = m.group(5) or ''
            commit_match = re.match(
                r'/commits/([0-9a-fA-F]{7,40})(?:/.*)?$', tail)
            return ParsedProjectUrl(
                scm_type=SCMType.BITBUCKET,
                host_url=host_url,
                project_path=f'{m.group(3)}/{m.group(4)}',
                commit=commit_match.group(1) if commit_match else None,
            )

    # Bitbucket Server clone URL: https://host[/context]/scm/KEY/repo[.git]
    m = re.match(
        r'https?://([^/]+)((?:/[^/]+)*?)/scm/([^/]+)/([^/]+?)(?:\.git)?/?$',
        url)
    if m:
        host_url = f"https://{_normalize_host(m.group(1))}{m.group(2)}"
        detected = detect_scm_type(host_url, gitlab_config, github_config,
                                   bitbucket_config)
        if detected not in (SCMType.GITLAB, SCMType.GITHUB):
            return ParsedProjectUrl(
                scm_type=SCMType.BITBUCKET,
                host_url=host_url,
                project_path=f'{m.group(3)}/{m.group(4)}',
            )

    # HTTPS: https://host/group/project[.git][/-/tree/...]
    m = re.match(r'https?://([^/]+)/(.+?)(?:\.git)?/?$', url)
    if not m:
        return None

    host_url = f"https://{_normalize_host(m.group(1))}"
    raw_path = m.group(2).rstrip('/')

    scm_type = detect_scm_type(host_url, gitlab_config, github_config,
                               bitbucket_config)

    if scm_type == SCMType.BITBUCKET:
        # Bitbucket Cloud: single-commit URLs use /commits/<sha> and the
        # browse suffixes differ from GitLab/GitHub - handled separately so
        # the GitLab/GitHub path below stays byte-identical.
        commit_match = re.search(r'/commits/([0-9a-fA-F]{7,40})(?:/.*)?$',
                                 raw_path)
        commit_sha = commit_match.group(1) if commit_match else None
        if commit_match:
            raw_path = raw_path[:commit_match.start()]
        project_path = _BITBUCKET_URL_SUFFIXES.sub('', raw_path).rstrip('/')
        if '/' not in project_path:
            return None
        return ParsedProjectUrl(
            scm_type=scm_type,
            host_url=host_url,
            project_path=project_path,
            commit=commit_sha,
        )

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
        scm_type=scm_type,
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
