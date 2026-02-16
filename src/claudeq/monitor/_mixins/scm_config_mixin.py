"""SCM provider initialization and setup dialog methods."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QMessageBox

from claudeq.monitor.mr_tracking.base import SCMProvider
from claudeq.monitor.mr_tracking.config import (
    load_github_config, load_gitlab_config, resolve_scm_token,
    save_github_config, save_gitlab_config, save_monitor_prefs,
)
from claudeq.monitor.mr_tracking.git_utils import SCMType, detect_scm_type, get_git_remote_info
from claudeq.utils.constants import SCM_POLL_INTERVAL

if TYPE_CHECKING:
    from claudeq.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object

logger = logging.getLogger(__name__)


class SCMConfigMixin(_Base):
    """Methods for SCM provider initialization, setup dialogs, and toggles."""

    def _init_scm_providers(self) -> None:
        """Load SCM configs and create providers for each configured platform.

        For env var token mode, validates the resolved token on startup.
        If validation fails (env var unset or token invalid), the provider is
        disabled, the saved username is cleared (so the popup won't repeat on
        next startup), and a warning is shown once.
        """
        filter_bots = not self._prefs.get('include_bots', False)

        # GitLab
        gitlab_config = load_gitlab_config()
        gitlab_token = self._resolve_and_validate_env_token(
            gitlab_config, 'private_token', 'GitLab', save_gitlab_config)
        if gitlab_config and gitlab_token and 'username' in gitlab_config:
            try:
                from claudeq.monitor.mr_tracking.gitlab_provider import GitLabProvider
                self._scm_providers[SCMType.GITLAB.value] = GitLabProvider(
                    gitlab_url=gitlab_config.get('gitlab_url', 'https://gitlab.com'),
                    private_token=gitlab_token,
                    username=gitlab_config['username'],
                    filter_bots=filter_bots,
                )
            except Exception:
                logger.debug("Failed to init GitLab provider", exc_info=True)
                self._scm_providers.pop(SCMType.GITLAB.value, None)
        else:
            self._scm_providers.pop(SCMType.GITLAB.value, None)

        # GitHub
        github_config = load_github_config()
        github_token = self._resolve_and_validate_env_token(
            github_config, 'token', 'GitHub', save_github_config)
        if github_config and github_token and 'username' in github_config:
            try:
                from claudeq.monitor.mr_tracking.github_provider import GitHubProvider
                self._scm_providers[SCMType.GITHUB.value] = GitHubProvider(
                    token=github_token,
                    username=github_config['username'],
                    github_url=github_config.get('github_url') or None,
                    filter_bots=filter_bots,
                )
            except Exception:
                logger.debug("Failed to init GitHub provider", exc_info=True)
                self._scm_providers.pop(SCMType.GITHUB.value, None)
        else:
            self._scm_providers.pop(SCMType.GITHUB.value, None)

        self._update_scm_buttons()

    def _resolve_and_validate_env_token(
        self,
        config: Optional[dict[str, Any]],
        token_key: str,
        provider_name: str,
        save_fn: Any,
    ) -> Optional[str]:
        """Resolve the token and validate it if using env var mode.

        For direct mode: returns the stored token as-is (already validated
        via Test Connection when saved).

        For env var mode: resolves the env var. If unset or the token is
        invalid, shows a one-time warning and clears the saved username so
        the warning won't repeat on subsequent startups.

        Returns:
            The resolved token, or None if unavailable/invalid.
        """
        if not config or 'username' not in config:
            return None  # Not configured — nothing to validate
        token = resolve_scm_token(config, token_key)
        if config.get('token_mode') != 'env_var':
            return token  # Direct mode — trust the saved value

        var_name = config.get(token_key, '')

        # Env var not set
        if not token:
            self._disable_env_var_provider(config, save_fn, provider_name,
                                           f'Environment variable ${var_name} is not set.')
            return None

        # Env var set — validate the token actually works
        success, error = self._test_env_var_token(provider_name, config, token)
        if not success:
            self._disable_env_var_provider(
                config, save_fn, provider_name,
                f'Token from ${var_name} is invalid.\n\n{error}')
            return None

        return token

    @staticmethod
    def _test_env_var_token(provider_name: str, config: dict[str, Any],
                            token: str) -> tuple[bool, str]:
        """Quick auth check for a resolved env var token."""
        try:
            if provider_name == 'GitLab':
                import gitlab
                gl = gitlab.Gitlab(
                    config.get('gitlab_url', 'https://gitlab.com'),
                    private_token=token, timeout=10)
                gl.auth()
                return True, gl.user.username
            elif provider_name == 'GitHub':
                from github import Github
                base_url = config.get('github_url', '')
                if base_url:
                    stripped = base_url.lower().rstrip('/')
                    if stripped in ('https://github.com', 'http://github.com'):
                        base_url = ''
                if base_url:
                    gh = Github(login_or_token=token, base_url=base_url, timeout=10)
                else:
                    gh = Github(login_or_token=token, timeout=10)
                return True, gh.get_user().login
        except Exception as e:
            return False, str(e)
        return False, 'Unknown provider'

    @staticmethod
    def _disable_env_var_provider(config: dict[str, Any], save_fn: Any,
                                  provider_name: str, reason: str) -> None:
        """Clear the saved username so this provider won't re-init on next startup."""
        config.pop('username', None)
        save_fn(config)
        QMessageBox.warning(
            None,
            f'{provider_name} disconnected',
            f'{reason}\n\n'
            f'{provider_name} connection is disabled. Re-open the setup '
            f'dialog and test the connection to re-enable.',
        )

    def _update_scm_buttons(self) -> None:
        """Update SCM button text/style based on connection state."""
        if SCMType.GITLAB.value in self._scm_providers:
            self.gitlab_btn.setText('GitLab Connected')
            self.gitlab_btn.setStyleSheet('QPushButton { color: #00ff00; } QToolTip { color: #e0e0e0; }')
        else:
            self.gitlab_btn.setText('Connect GitLab')
            self.gitlab_btn.setStyleSheet('')

        if SCMType.GITHUB.value in self._scm_providers:
            self.github_btn.setText('GitHub Connected')
            self.github_btn.setStyleSheet('QPushButton { color: #00ff00; } QToolTip { color: #e0e0e0; }')
        else:
            self.github_btn.setText('Connect GitHub')
            self.github_btn.setStyleSheet('')

    def _get_provider_for_session(self, session: dict[str, Any]) -> Optional[SCMProvider]:
        """Get the appropriate SCM provider for a session based on its git remote.

        For MR-pinned rows (added via '+'), uses the stored scm_type directly.
        For active sessions, resolves from the local git remote.

        Returns:
            The matching SCMProvider, or None if no provider matches.
        """
        # First try: use stored SCM type (MR-pinned rows)
        scm_type_str = session.get('scm_type')
        if scm_type_str:
            provider = self._scm_providers.get(scm_type_str)
            if provider:
                return provider

        # Second try: resolve from local git remote
        project_path = session.get('project_path')
        if not project_path:
            return None

        remote_info = get_git_remote_info(project_path)
        if not remote_info:
            return None

        # Use the SCM type detected from the remote URL
        scm_type = remote_info.scm_type
        if scm_type == SCMType.UNKNOWN:
            # Try to refine using GitLab config
            gitlab_config = load_gitlab_config()
            scm_type = detect_scm_type(remote_info.host_url, gitlab_config)

        return self._scm_providers.get(scm_type.value)

    def _get_poll_interval(self) -> int:
        """Get the minimum poll interval across all configured providers."""
        intervals = []
        gitlab_config = load_gitlab_config()
        if gitlab_config:
            intervals.append(gitlab_config.get('poll_interval', SCM_POLL_INTERVAL))
        github_config = load_github_config()
        if github_config:
            intervals.append(github_config.get('poll_interval', SCM_POLL_INTERVAL))
        return min(intervals) if intervals else SCM_POLL_INTERVAL

    def _open_gitlab_setup(self) -> None:
        """Open the GitLab setup dialog."""
        from claudeq.monitor.dialogs.gitlab_setup_dialog import GitLabSetupDialog

        dialog = GitLabSetupDialog(self)
        if dialog.exec_():
            # Re-initialize providers after successful save — reset tracking
            self._scm_poll_timer.stop()
            self._scm_providers.pop(SCMType.GITLAB.value, None)
            self._mr_statuses.clear()
            self._tracked_tags.clear()
            self._pending_tracking_context.clear()
            self._silent_tracking_tags.clear()
            self._init_scm_providers()
            self._auto_track_mr_pinned()
            self._maybe_start_notification_poll()
            self._show_status('GitLab connection updated')

    def _open_github_setup(self) -> None:
        """Open the GitHub setup dialog."""
        from claudeq.monitor.dialogs.github_setup_dialog import GitHubSetupDialog

        dialog = GitHubSetupDialog(self)
        if dialog.exec_():
            # Re-initialize providers after successful save — reset tracking
            self._scm_poll_timer.stop()
            self._scm_providers.pop(SCMType.GITHUB.value, None)
            self._mr_statuses.clear()
            self._tracked_tags.clear()
            self._pending_tracking_context.clear()
            self._silent_tracking_tags.clear()
            self._init_scm_providers()
            self._auto_track_mr_pinned()
            self._maybe_start_notification_poll()
            self._show_status('GitHub connection updated')

    def _toggle_include_bots(self, state: int) -> None:
        """Toggle bot comment inclusion and persist."""
        include = state == Qt.Checked
        self._prefs['include_bots'] = include
        save_monitor_prefs(self._prefs)
        # Update filter and re-poll tracked sessions
        for provider in self._scm_providers.values():
            provider._filter_bots = not include
        if self._scm_providers and self._tracked_tags:
            self._start_scm_poll()

    def _toggle_auto_fetch_cq(self, state: int) -> None:
        """Toggle auto /cq command fetching and persist."""
        self._prefs['auto_fetch_cq'] = state == Qt.Checked
        save_monitor_prefs(self._prefs)
