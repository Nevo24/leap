"""GitHub connection setup dialog for ClaudeQ Monitor."""

from typing import Any, Optional

from PyQt5.QtWidgets import QWidget

from claudeq.monitor.mr_tracking.config import load_github_config, save_github_config
from claudeq.monitor.scm_setup_dialog import SCMSetupDialog


class GitHubSetupDialog(SCMSetupDialog):
    """Dialog for configuring GitHub connection."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

    def _window_title(self) -> str:
        return 'Connect GitHub'

    def _url_label(self) -> str:
        return 'GitHub API URL (leave empty for github.com):'

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

    def _do_test_connection(self, url: str, token: str) -> tuple[bool, str]:
        try:
            from github import Github
            # Normalize: treat github.com URLs as default (need api.github.com)
            use_url = url
            if use_url:
                stripped = use_url.lower().rstrip('/')
                if stripped in ('https://github.com', 'http://github.com', 'github.com'):
                    use_url = ''
            if use_url:
                gh = Github(login_or_token=token, base_url=use_url, timeout=15)
            else:
                gh = Github(login_or_token=token, timeout=15)
            user = gh.get_user()
            return True, user.login
        except Exception as e:
            return False, str(e)

    def _load_config(self) -> Optional[dict[str, Any]]:
        return load_github_config()

    def _save_config(self, config: dict[str, Any]) -> None:
        save_github_config(config)
