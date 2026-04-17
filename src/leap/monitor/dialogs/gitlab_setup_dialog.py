"""GitLab connection setup dialog for Leap Monitor."""

import logging
from typing import Any, Optional

import gitlab
from PyQt5.QtWidgets import QWidget

from leap.monitor.pr_tracking.base import ConnectionTestResult
from leap.monitor.pr_tracking.config import load_gitlab_config, save_gitlab_config
from leap.monitor.dialogs.scm_setup_dialog import SCMSetupDialog

logger = logging.getLogger(__name__)


class GitLabSetupDialog(SCMSetupDialog):
    """Dialog for configuring GitLab connection."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

    def _window_title(self) -> str:
        return 'Connect GitLab'

    def _url_label(self) -> str:
        return 'GitLab URL:'

    def _url_placeholder(self) -> str:
        return 'https://gitlab.com'

    def _url_default(self) -> str:
        return 'https://gitlab.com'

    def _token_label(self) -> str:
        return 'Personal Access Token (api scope):'

    def _token_placeholder(self) -> str:
        return 'glpat-...'

    def _config_url_key(self) -> str:
        return 'gitlab_url'

    def _config_token_key(self) -> str:
        return 'private_token'

    def _notif_tooltip(self) -> str:
        return (
            'Poll GitLab Todos for review requests, assignments, and mentions.\n'
            'Requires a personal access token with "read_api" or "api" scope.\n'
            'Project access tokens cannot access the /todos endpoint.'
        )

    def _do_test_connection(self, url: str, token: str) -> ConnectionTestResult:
        if token.startswith(('ghp_', 'github_pat_')):
            return ConnectionTestResult(
                success=False,
                username='This appears to be a GitHub token. Use the GitHub setup dialog instead.',
                warnings=[],
            )
        try:
            gl = gitlab.Gitlab(url, private_token=token, timeout=15)
            gl.auth()
            username = gl.user.username
        except Exception as e:
            return ConnectionTestResult(success=False, username=str(e), warnings=[])

        # Verify the response is actually from GitLab's API.
        # GitLab returns "username" and "state"; GitHub returns "login"
        # and "type" instead.  If the URL points at a GitHub server,
        # gl.auth() would fail (different API path), but verify anyway.
        if not username or not hasattr(gl.user, 'state'):
            return ConnectionTestResult(
                success=False,
                username='Server authenticated but response does not match '
                         'GitLab API. Check your URL.',
                warnings=[],
            )

        warnings = _check_gitlab_scopes(gl)
        return ConnectionTestResult(success=True, username=username, warnings=warnings)

    def _load_config(self) -> Optional[dict[str, Any]]:
        return load_gitlab_config()

    def _save_config(self, config: dict[str, Any]) -> None:
        save_gitlab_config(config)


def _check_gitlab_scopes(gl: Any) -> list[str]:
    """Check GitLab token scopes and return permission warnings.

    Tries the /personal_access_tokens/self endpoint (GitLab 16.0+, PATs only).
    Falls back to probing the Todos API for project/group tokens or older GitLab.
    """
    warnings: list[str] = []

    # Try direct scope query (works for Personal Access Tokens on GitLab 16.0+)
    try:
        pat = gl.personal_access_tokens.get('self')
        scopes = set(pat.scopes)
        if 'api' in scopes:
            return []  # Full access — no warnings
        if 'read_api' not in scopes:
            warnings.append(
                'Missing read_api scope — PR tracking and code snippets will not work'
            )
        else:
            # Has read_api but not api — can read but not write
            warnings.append(
                'Missing api scope — /leap acknowledgment replies will not be posted'
            )
        return warnings
    except Exception:
        # 404 = endpoint not available (old GitLab or project/group token)
        logger.debug("Cannot query /personal_access_tokens/self, falling back to probe",
                      exc_info=True)

    # Fallback: probe Todos API to check notification access
    try:
        gl.todos.list(per_page=1, get_all=False)
    except Exception as e:
        status_code = getattr(e, 'response_code', None)
        if status_code == 403:
            warnings.append(
                'Cannot access Todos API — notification tracking will not work '
                '(project/group tokens cannot access this endpoint)'
            )
        else:
            logger.debug("Todos probe returned unexpected error", exc_info=True)

    return warnings
