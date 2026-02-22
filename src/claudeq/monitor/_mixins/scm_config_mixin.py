"""SCM provider initialization and setup dialog methods."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from typing import TYPE_CHECKING, Any, Optional

from PyQt5.QtCore import QProcess, QProcessEnvironment, Qt
from PyQt5.QtWidgets import QAction, QMenu, QMessageBox

from claudeq.monitor.mr_tracking.base import SCMProvider
from claudeq.monitor.mr_tracking.config import (
    load_github_config, load_gitlab_config, resolve_scm_token,
    save_github_config, save_gitlab_config, save_monitor_prefs,
)
from claudeq.monitor.mr_tracking.git_utils import SCMType, detect_scm_type, get_git_remote_info
from claudeq.slack.config import is_slack_installed
from claudeq.utils.constants import SCM_POLL_INTERVAL, SLACK_BOT_LOCK, STORAGE_DIR

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
            display_name = f'${var_name}' if var_name else '(not configured)'
            self._disable_env_var_provider(config, save_fn, provider_name,
                                           f'Environment variable {display_name} is not set.')
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
        """Quick auth check for a resolved env var token.

        Also checks token scopes and logs any permission warnings.
        """
        if provider_name == 'GitLab' and token.startswith(('ghp_', 'github_pat_')):
            return False, 'Token appears to be a GitHub token, not a GitLab token.'
        if provider_name == 'GitHub' and token.startswith('glpat-'):
            return False, 'Token appears to be a GitLab token, not a GitHub token.'
        try:
            if provider_name == 'GitLab':
                import gitlab
                from claudeq.monitor.dialogs.gitlab_setup_dialog import _check_gitlab_scopes
                gl = gitlab.Gitlab(
                    config.get('gitlab_url', 'https://gitlab.com'),
                    private_token=token, timeout=10)
                gl.auth()
                username = gl.user.username
                if not username or not hasattr(gl.user, 'state'):
                    return False, 'Server does not appear to be GitLab.'
                warnings = _check_gitlab_scopes(gl)
                for w in warnings:
                    logger.warning("GitLab token: %s", w)
                return True, username
            elif provider_name == 'GitHub':
                from github import Github
                from claudeq.monitor.dialogs.github_setup_dialog import (
                    _check_github_scopes, _verify_github_server,
                )
                base_url = config.get('github_url', '')
                if base_url:
                    stripped = base_url.lower().rstrip('/')
                    if stripped in ('https://github.com', 'http://github.com'):
                        base_url = ''
                base = (base_url or 'https://api.github.com').rstrip('/')
                if not _verify_github_server(base, token):
                    return False, 'Server does not appear to be GitHub.'
                if base_url:
                    gh = Github(login_or_token=token, base_url=base_url, timeout=10)
                else:
                    gh = Github(login_or_token=token, timeout=10)
                username = gh.get_user().login
                if not username:
                    return False, 'Could not determine GitHub username.'
                warnings = _check_github_scopes(gh)
                for w in warnings:
                    logger.warning("GitHub token: %s", w)
                return True, username
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

    # ------------------------------------------------------------------
    #  Slack bot management
    # ------------------------------------------------------------------

    @staticmethod
    def _is_slack_bot_running() -> bool:
        """Check if the Slack bot lock directory exists (bot is running)."""
        return SLACK_BOT_LOCK.is_dir()

    def _update_slack_bot_button(self) -> None:
        """Sync the Slack Bot button appearance with actual bot state."""
        if not is_slack_installed():
            self.slack_bot_btn.setVisible(False)
            return

        self.slack_bot_btn.setVisible(True)
        self.slack_bot_btn.setEnabled(True)
        if self._is_slack_bot_running():
            self.slack_bot_btn.setText('Slack Bot Running')
            self.slack_bot_btn.setStyleSheet(
                'QPushButton { color: #00ff00; } QToolTip { color: #e0e0e0; }')
            self.slack_bot_btn.setToolTip('Slack bot is running — click to stop')
        else:
            self.slack_bot_btn.setText('Run Slack Bot')
            self.slack_bot_btn.setStyleSheet('')
            self.slack_bot_btn.setToolTip('Click to start the Slack bot daemon')

    def _toggle_slack_bot(self) -> None:
        """Start or stop the Slack bot."""
        if self._is_slack_bot_running():
            self._stop_slack_bot()
        else:
            self._start_slack_bot()

    def _start_slack_bot(self, silent: bool = False) -> None:
        """Launch the Slack bot as a QProcess.

        Args:
            silent: If True, suppress the status bar message (used for auto-start).
        """
        if self._is_slack_bot_running():
            return

        project_dir = STORAGE_DIR.parent
        script = str(project_dir / 'src' / 'scripts' / 'claudeq-main.sh')

        process = QProcess(self)
        process.setProgram('/bin/bash')
        process.setArguments([script, '--slack'])

        # Inherit current environment + tell the script we're the monitor
        env = QProcessEnvironment.systemEnvironment()
        env.insert('CQ_SLACK_SOURCE', 'monitor')
        process.setProcessEnvironment(env)

        process.finished.connect(self._on_slack_bot_finished)
        process.start()

        self._slack_bot_process = process
        self._prefs['slack_bot_enabled'] = True
        save_monitor_prefs(self._prefs)
        self._update_slack_bot_button()

        if not silent:
            self._show_status('Slack bot started')

    def _stop_slack_bot(self) -> None:
        """Stop the Slack bot — via QProcess, terminal close, or direct kill.

        When the bot runs in a terminal we must both kill the process AND
        close the terminal tab.  Strategy:
        1. Try closing the terminal tab (kills the process too).
        2. Kill any surviving processes (bash wrapper + Python child).
        3. If step 1 failed, try closing the terminal tab again (now dead).
        4. Clean up the lock directory.
        """
        from claudeq.monitor.navigation import close_terminal_with_title

        if self._slack_bot_process and self._slack_bot_process.state() != QProcess.NotRunning:
            # We started it via QProcess — no terminal tab to close.
            # terminate() sends SIGTERM which doesn't reliably kill
            # slack-bolt (blocks on Event().wait()), so follow up with kill().
            self._slack_bot_process.terminate()
            if not self._slack_bot_process.waitForFinished(500):
                self._slack_bot_process.kill()
        else:
            default_term = self._prefs.get('default_terminal')

            # Step 1: try closing the terminal tab (this also kills the process)
            tab_closed = close_terminal_with_title('cq slack-bot',
                                                   preferred_ide=default_term)

            # Step 2: kill any surviving processes (bash wrapper + Python child)
            self._kill_slack_bot_processes()

            # Step 3: if the tab wasn't closed in step 1, try again now that
            # the process is dead (tab title may still match)
            if not tab_closed:
                close_terminal_with_title('cq slack-bot',
                                          preferred_ide=default_term)

        # Always remove lock dir immediately so the button updates right away
        try:
            (SLACK_BOT_LOCK / 'source').unlink(missing_ok=True)
            SLACK_BOT_LOCK.rmdir()
        except OSError:
            pass

        self._prefs['slack_bot_enabled'] = False
        save_monitor_prefs(self._prefs)
        self._update_slack_bot_button()
        self._show_status('Slack bot stopped')

    @staticmethod
    def _kill_slack_bot_processes() -> None:
        """Kill the Slack bot bash wrapper and Python child processes.

        SIGTERM alone doesn't work reliably on the Python Slack bot because
        slack-bolt's SocketModeHandler blocks on ``threading.Event().wait()``
        which is not interrupted by signals on macOS.  We therefore collect
        all matching PIDs, send SIGTERM, wait briefly, then SIGKILL any
        survivors.
        """
        pids: list[int] = []
        for pattern in ['claudeq-main.sh --slack', 'claudeq-slack.py']:
            try:
                result = subprocess.run(
                    ['pgrep', '-f', pattern],
                    capture_output=True, text=True, timeout=5)
                for pid_str in result.stdout.strip().split('\n'):
                    if pid_str:
                        try:
                            pids.append(int(pid_str))
                        except ValueError:
                            pass
            except (subprocess.TimeoutExpired, OSError):
                pass

        if not pids:
            return

        # Try graceful shutdown first
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        # Give processes a moment to exit
        time.sleep(0.3)

        # Force-kill any survivors (SIGTERM doesn't interrupt
        # slack-bolt's blocking Event().wait() on macOS)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _on_slack_bot_finished(self) -> None:
        """Clean up after the Slack bot QProcess exits."""
        if self._slack_bot_process:
            self._slack_bot_process.deleteLater()
            self._slack_bot_process = None
        self._update_slack_bot_button()

    def _slack_bot_context_menu(self, pos: Any) -> None:
        """Show right-click context menu on the Slack Bot button."""
        if not self._is_slack_bot_running():
            return

        menu = QMenu(self)

        jump_action = QAction('Jump to terminal', self)
        # Only enable if the bot is running in a terminal (not our QProcess)
        running_in_terminal = (
            self._slack_bot_process is None
            or self._slack_bot_process.state() == QProcess.NotRunning
        )
        jump_action.setEnabled(running_in_terminal)
        jump_action.triggered.connect(self._jump_to_slack_bot_terminal)
        menu.addAction(jump_action)

        menu.exec_(self.slack_bot_btn.mapToGlobal(pos))

    def _jump_to_slack_bot_terminal(self) -> None:
        """Focus the terminal running the Slack bot."""
        from claudeq.monitor.navigation import find_terminal_with_title
        default_term = self._prefs.get('default_terminal')
        if not find_terminal_with_title('cq slack-bot',
                                        preferred_ide=default_term):
            self._show_status('Could not find Slack bot terminal')
