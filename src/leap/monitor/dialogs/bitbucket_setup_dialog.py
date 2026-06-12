"""Bitbucket connection setup dialog for Leap Monitor."""

import logging
from typing import Any, Optional

from PyQt5.QtWidgets import QLabel, QLineEdit

from leap.monitor.pr_tracking.base import ConnectionTestResult
from leap.monitor.pr_tracking.bitbucket_provider import (
    BitbucketProvider, is_bitbucket_cloud_url,
)
from leap.monitor.pr_tracking.config import (
    load_bitbucket_config, save_bitbucket_config,
)
from leap.monitor.dialogs.scm_setup_dialog import SCMSetupDialog

logger = logging.getLogger(__name__)

_CLOUD_NOTIF_TOOLTIP = (
    'Bitbucket Cloud does not expose a notifications API - this '
    'setting takes effect only when connected to a Bitbucket Server / '
    'Data Center (self-hosted) URL.'
)
_SERVER_NOTIF_TOOLTIP = (
    'Poll the Bitbucket Server reviewer dashboard for PRs '
    'awaiting your review.'
)


class BitbucketSetupDialog(SCMSetupDialog):
    """Setup dialog for Bitbucket Cloud / Server connections.

    The "Self-hosted" toggle doubles as the flavor switch: unchecked means
    Bitbucket Cloud (bitbucket.org, API 2.0), checked means a Bitbucket
    Server / Data Center URL (REST API 1.0).  An extra "Email / username"
    field supplies the HTTP Basic identity - required for Cloud API tokens
    and app passwords, optional everywhere else (empty = Bearer token).
    """

    def _window_title(self) -> str:
        return 'Connect Bitbucket'

    def _url_label(self) -> str:
        return 'Bitbucket Server URL:'

    def _url_placeholder(self) -> str:
        return 'https://bitbucket.mycompany.com'

    def _url_default(self) -> str:
        return 'https://bitbucket.org'

    def _token_label(self) -> str:
        return 'API Token / Access Token:'

    def _token_placeholder(self) -> str:
        return 'API token, app password, or HTTP access token'

    def _env_var_placeholder(self) -> str:
        return 'e.g. BITBUCKET_TOKEN'

    def _config_url_key(self) -> str:
        return 'bitbucket_url'

    def _config_token_key(self) -> str:
        return 'token'

    def _notif_tooltip(self) -> str:
        return _SERVER_NOTIF_TOOLTIP

    def _init_ui(self) -> None:
        super()._init_ui()
        layout = self.layout()

        # Basic-auth identity field, inserted right below the token input.
        self._auth_user_label = QLabel('Email / username (for API tokens):')
        self.auth_user_input = QLineEdit()
        self.auth_user_input.setPlaceholderText(
            'Atlassian email (API token) or username (app password); '
            'empty = Bearer token')
        tooltip = (
            'Bitbucket Cloud API tokens authenticate as your Atlassian '
            'email; app passwords as your Bitbucket username.\n'
            'Leave empty for workspace/repository access tokens and '
            'Bitbucket Server HTTP access tokens (Bearer auth).'
        )
        self._auth_user_label.setToolTip(tooltip)
        self.auth_user_input.setToolTip(tooltip)
        pos = layout.indexOf(self.token_input) + 1
        layout.insertWidget(pos, self.auth_user_input)
        layout.insertWidget(pos, self._auth_user_label)

        # The self-hosted toggle doubles as the Cloud/Server flavor switch;
        # notification tracking only exists on Server.
        self._url_check.toggled.connect(self._update_notif_for_flavor)
        self._update_notif_for_flavor(self._url_check.isChecked())

    def _update_notif_for_flavor(self, self_hosted: bool) -> None:
        """Swap the notifications tooltip for the current flavor.

        The checkbox stays enabled and checked-by-default on both flavors
        (same look as the GitLab/GitHub dialogs).  On Cloud the saved
        setting simply has no runtime effect - the provider's
        ``supports_notifications()`` returning False is what gates the
        polling - so the tooltip explains the limitation instead of the
        checkbox graying out.
        """
        self.notif_check.setToolTip(
            _SERVER_NOTIF_TOOLTIP if self_hosted else _CLOUD_NOTIF_TOOLTIP)

    def _is_default_url(self, saved_url: str) -> bool:
        # Any bitbucket.org host (api.bitbucket.org included) is Cloud -
        # don't auto-expand the self-hosted field for it.
        return super()._is_default_url(saved_url) or is_bitbucket_cloud_url(saved_url)

    def _load_existing(self) -> None:
        super()._load_existing()
        config = self._load_config() or {}
        self.auth_user_input.setText(config.get('auth_user', ''))
        # _load_existing may have auto-checked the self-hosted box; sync the
        # notifications checkbox with the resulting flavor.
        self._update_notif_for_flavor(self._url_check.isChecked())

    def _form_values(self) -> Optional[dict[str, Any]]:
        values = super()._form_values()
        if values is None:
            return None
        values['auth_user'] = self.auth_user_input.text().strip()
        return values

    def _do_test_connection(self, url: str, token: str) -> ConnectionTestResult:
        """Test connection to Bitbucket (Cloud or Server, decided by URL)."""
        auth_user = self._pending_values.get('auth_user', '')
        try:
            provider = BitbucketProvider(
                bitbucket_url=url, token=token, username='',
                auth_user=auth_user)
            success, message = provider.test_connection()
        except Exception as e:
            logger.debug("Bitbucket connection test failed", exc_info=True)
            return ConnectionTestResult(success=False, username=str(e),
                                        warnings=[])
        if not success:
            return ConnectionTestResult(success=False, username=message,
                                        warnings=[])
        return ConnectionTestResult(success=True, username=message,
                                    warnings=[])

    def _load_config(self) -> Optional[dict[str, Any]]:
        return load_bitbucket_config()

    def _save_config(self, config: dict[str, Any]) -> None:
        save_bitbucket_config(config)
