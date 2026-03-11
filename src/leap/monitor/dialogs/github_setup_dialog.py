"""GitHub connection setup dialog for Leap Monitor."""

import logging
from typing import Any, Optional

import requests as _requests
from PyQt5.QtWidgets import QWidget

from leap.monitor.pr_tracking.base import ConnectionTestResult
from leap.monitor.pr_tracking.config import load_github_config, save_github_config
from leap.monitor.dialogs.scm_setup_dialog import SCMSetupDialog

logger = logging.getLogger(__name__)


class GitHubSetupDialog(SCMSetupDialog):
    """Dialog for configuring GitHub connection."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

    def _window_title(self) -> str:
        return 'Connect GitHub'

    def _url_label(self) -> str:
        return 'GitHub Enterprise API URL:'

    def _url_placeholder(self) -> str:
        return 'https://api.github.com'

    def _url_default(self) -> str:
        return ''

    def _token_label(self) -> str:
        return 'Personal Access Token (repo scope):'

    def _token_placeholder(self) -> str:
        return 'ghp_...'

    def _config_url_key(self) -> str:
        return 'github_url'

    def _config_token_key(self) -> str:
        return 'token'

    def _notif_tooltip(self) -> str:
        return (
            'Poll GitHub Notifications for review requests, assignments, and mentions.\n'
            'Requires a classic personal access token with "notifications" scope.\n'
            'Fine-grained tokens do NOT support this endpoint.'
        )

    def _do_test_connection(self, url: str, token: str) -> ConnectionTestResult:
        if token.startswith('glpat-'):
            return ConnectionTestResult(
                success=False,
                username='This appears to be a GitLab token. Use the GitLab setup dialog instead.',
                warnings=[],
            )

        # Normalize: treat github.com URLs as default (need api.github.com)
        use_url = url
        if use_url:
            stripped = use_url.lower().rstrip('/')
            if stripped in ('https://github.com', 'http://github.com', 'github.com'):
                use_url = ''
        base = (use_url or 'https://api.github.com').rstrip('/')

        # Verify we're talking to GitHub BEFORE trusting auth results.
        # Hit /meta — a GitHub-only endpoint.  GitLab returns 404 for it.
        if not _verify_github_server(base, token):
            return ConnectionTestResult(
                success=False,
                username='Server does not appear to be GitHub. '
                         'Check your URL — you may be connecting to a GitLab instance.',
                warnings=[],
            )

        try:
            from github import Github
            if use_url:
                gh = Github(login_or_token=token, base_url=use_url, timeout=15)
            else:
                gh = Github(login_or_token=token, timeout=15)
            user = gh.get_user()
            username = user.login
        except Exception as e:
            return ConnectionTestResult(success=False, username=str(e), warnings=[])

        if not username:
            return ConnectionTestResult(
                success=False,
                username='Authentication succeeded but could not determine username.',
                warnings=[],
            )

        warnings = _check_github_scopes(gh)
        return ConnectionTestResult(success=True, username=username, warnings=warnings)

    def _load_config(self) -> Optional[dict[str, Any]]:
        return load_github_config()

    def _save_config(self, config: dict[str, Any]) -> None:
        save_github_config(config)


def _verify_github_server(base_url: str, token: str) -> bool:
    """Verify the server is actually GitHub using a direct HTTP request.

    Calls /meta which is a GitHub-specific endpoint.  GitLab and other
    servers will return 404 or a non-matching response.
    """
    try:
        resp = _requests.get(
            f'{base_url}/meta',
            headers={'Authorization': f'token {token}'},
            timeout=10,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        # GitHub's /meta always includes 'verifiable_password_authentication'
        return 'verifiable_password_authentication' in data
    except Exception:
        logger.debug("GitHub server verification failed", exc_info=True)
        return False


def _check_github_scopes(gh: Any) -> list[str]:
    """Check GitHub token scopes and return permission warnings.

    Uses the X-OAuth-Scopes header (populated after the first API call).
    Fine-grained tokens don't return this header (oauth_scopes is None).
    """
    warnings: list[str] = []

    try:
        scopes = gh.oauth_scopes
    except Exception:
        logger.debug("Could not read oauth_scopes from GitHub", exc_info=True)
        return []

    if scopes is None:
        # Fine-grained token — no X-OAuth-Scopes header
        warnings.append(
            'Fine-grained token detected — notification tracking requires '
            'a classic personal access token with notifications scope'
        )
        return warnings

    # Classic PAT — check for required scopes
    scope_set = set(scopes)
    if 'repo' not in scope_set:
        warnings.append(
            'Missing repo scope — PR tracking, code snippets, '
            'and /leap replies will not work'
        )
    if 'notifications' not in scope_set:
        warnings.append(
            'Missing notifications scope — notification tracking will not work'
        )

    return warnings
