"""User notification handling methods (GitLab Todos / GitHub notifications)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from claudeq.monitor.mr_tracking.base import UserNotification
from claudeq.monitor.mr_tracking.config import (
    get_notification_prefs, load_github_config, load_gitlab_config,
    save_github_config, save_gitlab_config, save_notification_seen,
)
from claudeq.monitor.mr_tracking.git_utils import SCMType
from claudeq.monitor.ui.dock_badge import NotificationEvent, NotificationType

if TYPE_CHECKING:
    from claudeq.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object

logger = logging.getLogger(__name__)


class NotificationsMixin(_Base):
    """Methods for user-level SCM notification handling."""

    def _get_notif_scm_types(self) -> set[str]:
        """Return the set of SCM types that have notification tracking enabled."""
        result: set[str] = set()
        gitlab_config = load_gitlab_config()
        if (gitlab_config
                and gitlab_config.get('enable_notifications')
                and SCMType.GITLAB.value in self._scm_providers):
            result.add(SCMType.GITLAB.value)
        github_config = load_github_config()
        if (github_config
                and github_config.get('enable_notifications')
                and SCMType.GITHUB.value in self._scm_providers):
            result.add(SCMType.GITHUB.value)
        return result

    def _maybe_start_notification_poll(self) -> None:
        """Start the SCM poll timer if notification tracking is enabled."""
        if self._get_notif_scm_types() and not self._scm_poll_timer.isActive():
            interval = self._get_poll_interval()
            self._scm_poll_timer.start(interval * 1000)

    def _on_notification_auth_error(self, scm_type: str) -> None:
        """Handle a 403/auth error from the notification fetch.

        Stops polling, shows a blocking popup, then disables notifications
        for the failing provider and restarts polling.
        """
        from PyQt5.QtWidgets import QMessageBox

        provider_name = scm_type.title()  # "github" -> "Github"
        if scm_type not in (SCMType.GITHUB.value, SCMType.GITLAB.value):
            return

        # Stop polling to prevent duplicate popups while the dialog is open
        self._scm_poll_timer.stop()

        if scm_type == SCMType.GITHUB.value:
            scope_hint = (
                'GitHub notifications require a classic personal access token '
                'with the "notifications" scope.\n'
                'Fine-grained tokens do NOT support this endpoint.'
            )
        else:
            scope_hint = (
                'GitLab Todos require a personal access token with '
                '"read_api" or "api" scope.\n'
                'Project access tokens cannot access the /todos endpoint.'
            )

        QMessageBox.warning(
            self, 'Notifications Disabled',
            f'Failed to fetch notifications from {provider_name} '
            f'(403 Forbidden).\n\n'
            f'{scope_hint}\n\n'
            f'Notification tracking has been disabled for {provider_name}.\n'
            f'To re-enable, update your token and re-check the option '
            f'in the {provider_name} setup.'
        )

        # Disable notifications in the provider config after user clicks OK
        if scm_type == SCMType.GITHUB.value:
            config = load_github_config()
            if config:
                config['enable_notifications'] = False
                save_github_config(config)
        else:
            config = load_gitlab_config()
            if config:
                config['enable_notifications'] = False
                save_gitlab_config(config)

        self._show_status(f"{provider_name} notifications disabled (403 Forbidden)")

        # Restart polling (now without notifications for that provider)
        if self._tracked_tags or self._get_notif_scm_types():
            self._scm_poll_timer.start(self._get_poll_interval() * 1000)

    def _on_notifications_received(self, notifications: list) -> None:
        """Handle user notifications from the background poller."""
        if self._shutting_down:
            return
        try:
            self._process_user_notifications(notifications)
        except Exception:
            logger.exception("Error processing user notifications")

    def _process_user_notifications(
        self, notifications: list,
    ) -> None:
        """Deduplicate and fire events for new user notifications."""
        # Group notifications by scm_type
        by_type: dict[str, list] = {}
        for n in notifications:
            by_type.setdefault(n.scm_type, []).append(n)

        all_events: list[NotificationEvent] = []

        for scm_type, notifs in by_type.items():
            current_ids = {n.id for n in notifs}

            if scm_type not in self._notification_seeded:
                # First time seeing this SCM type — seed without firing
                self._notification_seen[scm_type] = current_ids
                self._notification_seeded.add(scm_type)
                continue

            seen_ids = self._notification_seen.get(scm_type, set())
            new_ids = current_ids - seen_ids
            # Map new IDs to their notification objects
            new_notifs = [n for n in notifs if n.id in new_ids]

            for n in new_notifs:
                if n.reason == 'other':
                    continue
                ev = self._notification_to_event(n)
                if ev:
                    all_events.append(ev)

            # Update seen set (prunes disappeared IDs naturally)
            self._notification_seen[scm_type] = current_ids

        # Prune seen sets for SCM types that returned zero notifications
        # (e.g., user marked all todos as done on the web)
        for seeded_type in list(self._notification_seeded):
            if seeded_type not in by_type:
                self._notification_seen[seeded_type] = set()

        # Persist seen IDs
        serializable = {k: list(v) for k, v in self._notification_seen.items()}
        save_notification_seen(serializable)

        if not all_events:
            return

        # Log to status log
        for ev in all_events:
            self._log_notification_event(ev)

        # Update dock badge
        notif_prefs = get_notification_prefs(self._prefs)
        dock_enabled = {k: v['dock'] for k, v in notif_prefs.items()}
        self._dock_badge.count_user_notification_events(
            all_events, self.isActiveWindow(), dock_enabled,
        )

        # Send banner notifications
        self._send_banner_notifications(all_events)

    @staticmethod
    def _notification_to_event(n: UserNotification) -> Optional[NotificationEvent]:
        """Convert a UserNotification to a NotificationEvent."""
        reason_map = {
            'review_requested': NotificationType.REVIEW_REQUESTED,
            'assigned': NotificationType.ASSIGNED,
            'mentioned': NotificationType.MENTIONED,
        }
        ntype = reason_map.get(n.reason)
        if not ntype:
            return None
        return NotificationEvent(
            type=ntype,
            tag='',  # Not tied to a specific CQ session
            url=n.target_url,
            notification_title=n.title,
            project_name=n.project_name,
        )

    def _log_notification_event(self, ev: NotificationEvent) -> None:
        """Log a notification event to the status log."""
        title = ev.notification_title or ''
        project = ev.project_name or ''
        if ev.type == NotificationType.REVIEW_REQUESTED:
            msg = f"[Notification] Review requested: {title}"
        elif ev.type == NotificationType.ASSIGNED:
            msg = f"[Notification] Assigned: {title}"
        elif ev.type == NotificationType.MENTIONED:
            msg = f"[Notification] Mentioned: {title}"
        else:
            msg = f"[Notification] {title}"
        if project:
            msg += f" ({project})"
        self._show_status(msg, url=ev.url)
