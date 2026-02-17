"""
Pinned session validation for ClaudeQ server startup.

Validates that the current working directory matches the expected repo
and branch for MR-pinned sessions before starting the server.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


def build_auth_fetch_url(pinned: dict[str, Any], storage_dir: Path) -> Optional[str]:
    """Build an authenticated fetch URL from pinned session + SCM config.

    Reads the SCM token from the appropriate config file (gitlab_config.json
    or github_config.json) and injects it into the host URL.  Returns None
    if no token is available or the URL is non-HTTP (e.g. SSH).

    Note: Token resolution logic is intentionally duplicated from
    monitor's resolve_scm_token to avoid cross-package imports.

    Args:
        pinned: Pinned session entry dict.
        storage_dir: Path to the .storage directory.

    Returns:
        Authenticated git fetch URL, or None.
    """
    host_url = pinned.get('host_url', '')
    project = pinned.get('remote_project_path', '')
    scm_type = pinned.get('scm_type', '')
    if not host_url or not project or not host_url.startswith('http'):
        return None

    # Read token from the SCM config file (supports env var mode)
    token: Optional[str] = None
    if scm_type == 'gitlab':
        cfg_path = storage_dir / "gitlab_config.json"
        token_key = 'private_token'
    elif scm_type == 'github':
        cfg_path = storage_dir / "github_config.json"
        token_key = 'token'
    else:
        return None

    if cfg_path.exists():
        try:
            with open(cfg_path, 'r') as f:
                cfg = json.load(f)
            if cfg.get('token_mode') == 'env_var':
                var_name = cfg.get(token_key, '')
                token = os.environ.get(var_name) if var_name else None
            else:
                token = cfg.get(token_key)
        except (json.JSONDecodeError, OSError):
            pass

    if not token:
        return None

    scheme_end = host_url.index('://') + 3
    scheme = host_url[:scheme_end]
    host = host_url[scheme_end:]
    if scm_type == 'github':
        return f"{scheme}x-access-token:{token}@{host}/{project}.git"
    return f"{scheme}oauth2:{token}@{host}/{project}.git"


def validate_pinned_session(tag: str, storage_dir: Path) -> None:
    """Validate current repo/branch against monitor pinned session data.

    If this tag corresponds to an MR-pinned row (has remote_project_path),
    verify that we're in the right repo, on the right branch, and not
    behind the remote.  Calls ``sys.exit(1)`` if validation fails.

    Args:
        tag: Session tag name.
        storage_dir: Path to the .storage directory.
    """
    pinned_file = storage_dir / "pinned_sessions.json"
    if not pinned_file.exists():
        return

    try:
        with open(pinned_file, 'r') as f:
            pinned_sessions = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    entry = pinned_sessions.get(tag)
    if not entry:
        return

    pinned_project = entry.get('remote_project_path')
    if not pinned_project:
        return  # Auto-pinned row, no validation needed

    pinned_branch = entry.get('branch', '')

    # --- Repo match ---
    try:
        result = subprocess.run(
            ['git', 'config', '--get', 'remote.origin.url'],
            capture_output=True, text=True, timeout=5
        )
        remote_url = result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        remote_url = ''

    local_project = None
    if remote_url:
        # SSH: git@host:user/project.git
        m = re.match(r'git@[^:]+:(.+?)(?:\.git)?$', remote_url)
        if m:
            local_project = m.group(1)
        else:
            # HTTPS: https://host/user/project.git
            m = re.match(r'https?://[^/]+/(.+?)(?:\.git)?$', remote_url)
            if m:
                local_project = m.group(1)

    if not local_project or local_project != pinned_project:
        local_desc = f"'{local_project}'" if local_project else 'not a matching git repo'
        print(
            f"\033[91mError: Tag '{tag}' is monitored for repo "
            f"'{pinned_project}', but current directory is {local_desc}.\033[0m"
        )
        sys.exit(1)

    # --- Branch match ---
    if pinned_branch and pinned_branch != 'N/A':
        try:
            result = subprocess.run(
                ['git', 'branch', '--show-current'],
                capture_output=True, text=True, timeout=5
            )
            local_branch = result.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            local_branch = ''

        if local_branch != pinned_branch:
            print(
                f"\033[91mError: Tag '{tag}' is monitored for branch "
                f"'{pinned_branch}', but current branch is "
                f"'{local_branch or '(unknown)'}'.\033[0m"
            )
            sys.exit(1)

        # --- Commits synced ---
        fetch_url = build_auth_fetch_url(entry, storage_dir)
        try:
            if fetch_url:
                # Fetch using authenticated URL directly (no remote URL change)
                refspec = (
                    f'+refs/heads/{pinned_branch}'
                    f':refs/remotes/origin/{pinned_branch}'
                )
                subprocess.run(
                    ['git', 'fetch', fetch_url, refspec],
                    capture_output=True, timeout=15
                )
            else:
                subprocess.run(
                    ['git', 'fetch', 'origin', pinned_branch],
                    capture_output=True, timeout=15
                )
        except (subprocess.TimeoutExpired, OSError):
            pass  # Network issues shouldn't block startup

        try:
            result = subprocess.run(
                ['git', 'merge-base', '--is-ancestor',
                 f'origin/{pinned_branch}', 'HEAD'],
                capture_output=True, timeout=5
            )
            if result.returncode != 0:
                print(
                    f"\033[91m✖ Tag '{tag}' is tracked by ClaudeQ Monitor "
                    f"for branch '{pinned_branch}', but the local repo is "
                    f"behind remote. Pull or rebase before starting.\033[0m"
                )
                sys.exit(1)
        except (subprocess.TimeoutExpired, OSError):
            pass  # Can't verify — allow startup

        # --- Yellow warnings for ahead / dirty state (non-fatal) ---
        ahead_count = 0
        has_uncommitted = False
        try:
            result = subprocess.run(
                ['git', 'rev-list', f'origin/{pinned_branch}..HEAD', '--count'],
                capture_output=True, text=True, timeout=5
            )
            ahead_count = int(result.stdout.strip()) if result.returncode == 0 else 0
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass

        try:
            result = subprocess.run(
                ['git', 'status', '--porcelain'],
                capture_output=True, text=True, timeout=5
            )
            has_uncommitted = result.returncode == 0 and bool(result.stdout.strip())
        except (subprocess.TimeoutExpired, OSError):
            pass

        if ahead_count > 0 and has_uncommitted:
            suffix = (
                f"is {ahead_count} commit{'s' if ahead_count != 1 else ''} "
                f"ahead of remote with uncommitted changes"
            )
        elif ahead_count > 0:
            suffix = (
                f"is {ahead_count} commit{'s' if ahead_count != 1 else ''} "
                f"ahead of remote"
            )
        elif has_uncommitted:
            suffix = "has uncommitted changes"
        else:
            suffix = ''

        if suffix:
            print(
                f"\033[93m⚠ Tag '{tag}' is tracked by ClaudeQ Monitor "
                f"for branch '{pinned_branch}', but the local repo {suffix}. "
                f"Proceeding anyway.\033[0m"
            )
