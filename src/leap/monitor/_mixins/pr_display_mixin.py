"""PR display, dock badge, and banner notification methods."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PyQt5 import sip
from PyQt5.QtWidgets import QLabel

from leap.monitor.pr_tracking.base import PRState, PRStatus
from leap.monitor.pr_tracking.config import get_dock_enabled, get_notification_prefs
from leap.monitor.themes import current_theme
from leap.monitor.ui.dock_badge import NotificationEvent, NotificationType
from leap.monitor.ui.ui_widgets import IndicatorLabel, PulsingLabel
from leap.monitor.monitor_utils import find_icon

if TYPE_CHECKING:
    from leap.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object


class PRDisplayMixin(_Base):
    """Methods for PR column styling, dock badge updates, and banner notifications."""

    def _update_pr_column(self) -> None:
        """Update just the PR column widgets without rebuilding the whole table."""
        row_tags = self.table.property('_row_tags') or []
        for row in range(self.table.rowCount()):
            if row >= len(row_tags):
                break
            tag = row_tags[row]
            if not tag:
                continue
            pr_widget = self._pr_widgets.get(tag)
            if not pr_widget or sip.isdeleted(pr_widget):
                self._pr_widgets.pop(tag, None)
                self._pr_approval_widgets.pop(tag, None)
                continue
            approval_label = self._pr_approval_widgets.get(tag)
            if approval_label and sip.isdeleted(approval_label):
                self._pr_approval_widgets.pop(tag, None)
                approval_label = None
            try:
                status = self._pr_statuses.get(tag)
                self._apply_pr_status(pr_widget, approval_label, status)
                pr_widget.set_has_unresponded(
                    status is not None and status.state == PRState.UNRESPONDED
                )
                # Update fire label on the fast path
                cell_widget = self.table.cellWidget(row, self.COL_PR)
                if cell_widget and not sip.isdeleted(cell_widget):
                    fire_label = cell_widget.findChild(QLabel, '_prFireLabel')
                    if fire_label and not sip.isdeleted(fire_label):
                        show = self._should_show_pr_fire(tag)
                        fire_label.setText('\U0001f525' if show else '')
                        fire_label.setToolTip(
                            self._pr_fire_tooltip(tag) if show else '')
            except RuntimeError:
                # Widget was deleted, remove from cache
                self._pr_widgets.pop(tag, None)
                self._pr_approval_widgets.pop(tag, None)

    def _apply_pr_status(
        self, widget: PulsingLabel, approval_widget: Optional[IndicatorLabel],
        status: Optional[PRStatus]
    ) -> None:
        """Apply PR status to the status and approval indicator widgets."""
        # Hide approval label by default
        if approval_widget:
            approval_widget.setVisible(False)

        if not status or not self._scm_providers:
            widget.setText('N/A')
            widget.setStyleSheet(f'color: {current_theme().text_muted};')
            widget.setToolTip('No SCM provider configured')
            widget.set_pulsing(False)
            widget.set_pr_url(None)
            widget.set_indicator_help(None)
            return

        # Show/hide approval indicator
        if approval_widget and status.approved:
            approval_widget.setText('\U0001f44d')
            approval_widget.setVisible(True)
            approval_widget.set_click_url(status.pr_url)
            if status.approved_by:
                names = ', '.join(status.approved_by)
                approval_widget.set_indicator_help(f'Approved by {names}')
            else:
                approval_widget.set_indicator_help('PR approved')

        if status.state == PRState.NOT_CONFIGURED:
            widget.setText('N/A')
            widget.setStyleSheet(f'color: {current_theme().text_muted};')
            widget.setToolTip('')
            widget.set_pulsing(False)
            widget.set_pr_url(None)
            widget.set_indicator_help('No SCM provider configured')

        elif status.state == PRState.NO_PR:
            widget.setText('No PR')
            widget.setStyleSheet(f'color: {current_theme().text_muted};')
            widget.setToolTip('')
            widget.set_pulsing(False)
            widget.set_pr_url(None)
            widget.set_indicator_help('No open PR for this branch')

        elif status.state == PRState.ALL_RESPONDED:
            widget.setText('\u2713')
            widget.setStyleSheet(f'color: {current_theme().accent_green}; font-weight: bold;')
            approval_line = self._format_approval_line(status)
            widget.setToolTip('')
            widget.set_pulsing(False)
            widget.set_pr_url(status.pr_url)
            widget.set_indicator_help(
                f'PR !{status.pr_iid}: {status.pr_title}\n'
                f'All threads responded.{approval_line}'
            )

        elif status.state == PRState.UNRESPONDED:
            widget.setText(f'\U0001f4ac {status.unresponded_count}')
            approval_line = self._format_approval_line(status)
            widget.setToolTip('')
            widget.set_pulsing(True)
            # Jump directly to first unresolved comment thread
            url = status.pr_url
            if url and status.first_unresponded_note_id:
                url = f'{url}#note_{status.first_unresponded_note_id}'
            widget.set_pr_url(url)
            widget.set_indicator_help(
                f'PR !{status.pr_iid}: {status.pr_title}\n'
                f'{status.unresponded_count} unresponded thread(s).{approval_line}'
            )

    @staticmethod
    def _format_approval_line(status: PRStatus) -> str:
        """Format an approval line for tooltips, including approver names."""
        if not status.approved:
            return ''
        if status.approved_by:
            names = ', '.join(status.approved_by)
            return f'\nApproved by {names}'
        return '\nApproved'

    def _update_dock_badge(self) -> None:
        """Update the dock badge with number of PRs changed since last window focus."""
        dock_enabled = get_dock_enabled(self._prefs)
        events = self._dock_badge.update(
            self._pr_statuses, self.isActiveWindow(), dock_enabled,
        )
        self._send_banner_notifications(events)

    def _clear_dock_badge(self) -> None:
        """Clear the dock badge and snapshot current PR statuses as seen."""
        self._dock_badge.clear(self._pr_statuses)
        self._banner_notified = set()

    def _send_banner_notifications(self, events: list[NotificationEvent]) -> None:
        """Send macOS banner notifications and play sounds for events.

        Coalesces repeated (tag, type) combos while the window is inactive —
        only the first occurrence triggers a banner/sound.
        """
        if self.isActiveWindow():
            self._banner_notified: set[tuple[str, str]] = set()
            return
        if not events:
            return
        if not hasattr(self, '_banner_notified'):
            self._banner_notified = set()
        notif_prefs = get_notification_prefs(self._prefs)
        for ev in events:
            type_prefs = notif_prefs.get(ev.type.value, {})
            banner_enabled = type_prefs.get('banner', False)
            sound_name = type_prefs.get('sound', 'None')
            key = (ev.tag, ev.type.value)
            if key in self._banner_notified:
                continue
            # At least one of banner or sound must be enabled
            if not banner_enabled and sound_name == 'None':
                continue
            self._banner_notified.add(key)
            if banner_enabled:
                subtitle, body = self._format_banner_text(ev)
                self._send_macos_notification(subtitle, body, sound_name)
            elif sound_name != 'None':
                # Sound only (no banner)
                self._play_notification_sound(sound_name)

    @staticmethod
    def _format_banner_text(event: NotificationEvent) -> tuple[str, str]:
        """Format subtitle and body for a macOS banner notification."""
        tag = event.tag
        pr_ref = ''
        if event.pr_iid:
            title = event.pr_title or ''
            pr_ref = f"PR !{event.pr_iid}"
            if title:
                pr_ref += f" '{title}'"

        if event.type == NotificationType.PR_UNRESPONDED:
            return (tag, f"{pr_ref} has {event.unresponded_count} unresponded thread(s)")
        elif event.type == NotificationType.PR_ALL_RESPONDED:
            return (tag, f"{pr_ref} — all threads responded")
        elif event.type == NotificationType.PR_APPROVED:
            if event.approved_by:
                names = ', '.join(event.approved_by)
                return (tag, f"{pr_ref} approved by {names}")
            return (tag, f"{pr_ref} approved")
        elif event.type == NotificationType.SESSION_COMPLETED:
            return (tag, 'Session finished processing')
        elif event.type == NotificationType.SESSION_NEEDS_PERMISSION:
            return (tag, 'Session needs permission to use a tool')
        elif event.type == NotificationType.SESSION_NEEDS_INPUT:
            return (tag, 'Session needs your input')
        elif event.type == NotificationType.SESSION_INTERRUPTED:
            return (tag, 'Session was interrupted')
        elif event.type == NotificationType.REVIEW_REQUESTED:
            title = event.notification_title or ''
            project = event.project_name or ''
            return ('Notification', f"Review requested: {title} ({project})")
        elif event.type == NotificationType.ASSIGNED:
            title = event.notification_title or ''
            project = event.project_name or ''
            return ('Notification', f"Assigned: {title} ({project})")
        elif event.type == NotificationType.MENTIONED:
            title = event.notification_title or ''
            project = event.project_name or ''
            return ('Notification', f"Mentioned: {title} ({project})")
        return (tag, '')

    @staticmethod
    def _send_macos_notification(subtitle: str, body: str, sound_name: str = 'None') -> None:
        """Send a macOS banner notification via native NSUserNotification."""
        try:
            import os
            from AppKit import NSImage
            from Foundation import NSUserNotification, NSUserNotificationCenter
            notif = NSUserNotification.alloc().init()
            notif.setTitle_('Leap')
            if subtitle:
                notif.setSubtitle_(subtitle)
            if body:
                notif.setInformativeText_(body)
            # Set notification sound (built-in names only; custom files are
            # played separately since NSUserNotification only supports named sounds)
            if sound_name and sound_name != 'None':
                if os.path.isabs(sound_name):
                    # Custom file — play via _play_notification_sound after delivery
                    pass
                elif sound_name == 'Default':
                    notif.setSoundName_('NSUserNotificationDefaultSoundName')
                else:
                    notif.setSoundName_(sound_name)
            # Override the Python app icon with the Leap icon
            icon_path = find_icon()
            if icon_path:
                image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
                if image:
                    notif.setValue_forKey_(image, '_identityImage')
                    notif.setValue_forKey_(False, '_identityImageHasBorder')
            NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(notif)
            # For custom file paths, play the sound separately
            if sound_name and os.path.isabs(sound_name):
                from leap.monitor.dialogs.notifications_dialog import _play_sound
                _play_sound(sound_name)
        except Exception:
            pass  # PyObjC not available or notification failed

    @staticmethod
    def _play_notification_sound(sound_name: str) -> None:
        """Play a notification sound without sending a banner."""
        from leap.monitor.dialogs.notifications_dialog import _play_sound
        _play_sound(sound_name)
