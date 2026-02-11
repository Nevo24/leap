"""GitLab connection setup dialog for ClaudeQ Monitor."""

from typing import Any, Optional

from PyQt5.QtWidgets import QWidget

from claudeq.monitor.mr_tracking.config import load_gitlab_config, save_gitlab_config
from claudeq.monitor.dialogs.scm_setup_dialog import SCMSetupDialog


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

    def _do_test_connection(self, url: str, token: str) -> tuple[bool, str]:
        try:
            import gitlab
            gl = gitlab.Gitlab(url, private_token=token)
            gl.auth()
            return True, gl.user.username
        except Exception as e:
            return False, str(e)

    def _load_config(self) -> Optional[dict[str, Any]]:
        return load_gitlab_config()

    def _save_config(self, config: dict[str, Any]) -> None:
        save_gitlab_config(config)
