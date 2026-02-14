"""SCM provider initialization and setup dialog methods."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from PyQt5.QtCore import Qt

from claudeq.monitor.mr_tracking.base import SCMProvider
from claudeq.monitor.mr_tracking.config import (
    load_github_config, load_gitlab_config, save_monitor_prefs,
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
        """Load SCM configs and create providers for each configured platform."""
        filter_bots = not self._prefs.get('include_bots', False)

        # GitLab
        gitlab_config = load_gitlab_config()
        if gitlab_config and 'private_token' in gitlab_config and 'username' in gitlab_config:
            try:
                from claudeq.monitor.mr_tracking.gitlab_provider import GitLabProvider
                self._scm_providers[SCMType.GITLAB.value] = GitLabProvider(
                    gitlab_url=gitlab_config.get('gitlab_url', 'https://gitlab.com'),
                    private_token=gitlab_config['private_token'],
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
        if github_config and 'token' in github_config and 'username' in github_config:
            try:
                from claudeq.monitor.mr_tracking.github_provider import GitHubProvider
                self._scm_providers[SCMType.GITHUB.value] = GitHubProvider(
                    token=github_config['token'],
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
