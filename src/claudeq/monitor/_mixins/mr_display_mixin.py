"""MR display, dock badge, and banner notification methods."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PyQt5 import sip

from claudeq.monitor.mr_tracking.base import MRState, MRStatus
from claudeq.monitor.mr_tracking.config import get_notification_prefs
from claudeq.monitor.ui.dock_badge import NotificationEvent, NotificationType
from claudeq.monitor.ui.ui_widgets import IndicatorLabel, PulsingLabel
from claudeq.monitor.monitor_utils import find_icon

if TYPE_CHECKING:
    from claudeq.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object


class MRDisplayMixin(_Base):
    """Methods for MR column styling, dock badge updates, and banner notifications."""

    def _update_mr_column(self) -> None:
        """Update just the MR column widgets without rebuilding the whole table."""
        for row in range(self.table.rowCount()):
            tag_item = self.table.item(row, self.COL_TAG)
            if not tag_item:
                continue
            tag = tag_item.text()
            mr_widget = self._mr_widgets.get(tag)
            if not mr_widget or sip.isdeleted(mr_widget):
                self._mr_widgets.pop(tag, None)
                self._mr_approval_widgets.pop(tag, None)
                continue
            approval_label = self._mr_approval_widgets.get(tag)
            if approval_label and sip.isdeleted(approval_label):
                self._mr_approval_widgets.pop(tag, None)
                approval_label = None
            try:
                status = self._mr_statuses.get(tag)
                self._apply_mr_status(mr_widget, approval_label, status)
                mr_widget.set_has_unresponded(
                    status is not None and status.state == MRState.UNRESPONDED
                )
            except RuntimeError:
                # Widget was deleted, remove from cache
                self._mr_widgets.pop(tag, None)
                self._mr_approval_widgets.pop(tag, None)

    def _apply_mr_status(
        self, widget: PulsingLabel, approval_widget: Optional[IndicatorLabel],
        status: Optional[MRStatus]
    ) -> None:
        """Apply MR status to the status and approval indicator widgets."""
        # Hide approval label by default
        if approval_widget:
            approval_widget.setVisible(False)

        if not status or not self._scm_providers:
            widget.setText('N/A')
            widget.setStyleSheet('color: grey;')
            widget.setToolTip('No SCM provider configured')
            widget.set_pulsing(False)
            widget.set_mr_url(None)
            widget.set_indicator_help(None)
            return

        # Show/hide approval indicator
        if approval_widget and status.approved:
            approval_widget.setText('\U0001f44d')
            approval_widget.setVisible(True)
            approval_widget.set_click_url(status.mr_url)
            if status.approved_by:
                names = ', '.join(status.approved_by)
                approval_widget.set_indicator_help(f'Approved by {names}')
            else:
                approval_widget.set_indicator_help('MR approved')

        if status.state == MRState.NOT_CONFIGURED:
            widget.setText('N/A')
            widget.setStyleSheet('color: grey;')
            widget.setToolTip('')
            widget.set_pulsing(False)
            widget.set_mr_url(None)
            widget.set_indicator_help('No SCM provider configured')

        elif status.state == MRState.NO_MR:
            widget.setText('No MR')
            widget.setStyleSheet('color: grey;')
            widget.setToolTip('')
            widget.set_pulsing(False)
            widget.set_mr_url(None)
            widget.set_indicator_help('No open MR for this branch')

        elif status.state == MRState.ALL_RESPONDED:
            widget.setText('\u2713')
            widget.setStyleSheet('color: green; font-weight: bold;')
            approval_line = self._format_approval_line(status)
            widget.setToolTip('')
            widget.set_pulsing(False)
            widget.set_mr_url(status.mr_url)
            widget.set_indicator_help(
                f'MR !{status.mr_iid}: {status.mr_title}\n'
                f'All threads responded.{approval_line}'
            )

        elif status.state == MRState.UNRESPONDED:
            widget.setText(f'\U0001f4ac {status.unresponded_count}')
            approval_line = self._format_approval_line(status)
            widget.setToolTip('')
            widget.set_pulsing(True)
            # Jump directly to first unresolved comment thread
            url = status.mr_url
            if url and status.first_unresponded_note_id:
                url = f'{url}#note_{status.first_unresponded_note_id}'
            widget.set_mr_url(url)
            widget.set_indicator_help(
                f'MR !{status.mr_iid}: {status.mr_title}\n'
                f'{status.unresponded_count} unresponded thread(s).{approval_line}'
            )

    @staticmethod
    def _format_approval_line(status: MRStatus) -> str:
        """Format an approval line for tooltips, including approver names."""
        if not status.approved:
            return ''
        if status.approved_by:
            names = ', '.join(status.approved_by)
            return f'\nApproved by {names}'
        return '\nApproved'

    def _update_dock_badge(self) -> None:
        """Update the dock badge with number of MRs changed since last window focus."""
        notif_prefs = get_notification_prefs(self._prefs)
        dock_enabled = {k: v['dock'] for k, v in notif_prefs.items()}
        events = self._dock_badge.update(
            self._mr_statuses, self.isActiveWindow(), dock_enabled,
        )
        self._send_banner_notifications(events)

    def _clear_dock_badge(self) -> None:
        """Clear the dock badge and snapshot current MR statuses as seen."""
        self._dock_badge.clear(self._mr_statuses)

    def _send_banner_notifications(self, events: list[NotificationEvent]) -> None:
        """Send macOS banner notifications for events where banner is enabled."""
        if not events or self.isActiveWindow():
            return
        notif_prefs = get_notification_prefs(self._prefs)
        for ev in events:
            if not notif_prefs.get(ev.type.value, {}).get('banner', False):
                continue
            subtitle, body = self._format_banner_text(ev)
            self._send_macos_notification(subtitle, body)

    @staticmethod
    def _format_banner_text(event: NotificationEvent) -> tuple[str, str]:
        """Format subtitle and body for a macOS banner notification."""
        tag = event.tag
        mr_ref = ''
        if event.mr_iid:
            title = event.mr_title or ''
            mr_ref = f"MR !{event.mr_iid}"
            if title:
                mr_ref += f" '{title}'"

        if event.type == NotificationType.MR_UNRESPONDED:
            return (tag, f"{mr_ref} has {event.unresponded_count} unresponded thread(s)")
        elif event.type == NotificationType.MR_ALL_RESPONDED:
            return (tag, f"{mr_ref} — all threads responded")
        elif event.type == NotificationType.MR_APPROVED:
            if event.approved_by:
                names = ', '.join(event.approved_by)
                return (tag, f"{mr_ref} approved by {names}")
            return (tag, f"{mr_ref} approved")
        elif event.type == NotificationType.SESSION_COMPLETED:
            return (tag, 'Claude finished processing')
        elif event.type == NotificationType.SESSION_NEEDS_PERMISSION:
            return (tag, 'Claude needs permission to use a tool')
        elif event.type == NotificationType.SESSION_HAS_QUESTION:
            return (tag, 'Claude is asking you a question')
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
    def _send_macos_notification(subtitle: str, body: str) -> None:
        """Send a macOS banner notification via native NSUserNotification."""
        try:
            from AppKit import NSImage
            from Foundation import NSUserNotification, NSUserNotificationCenter
            notif = NSUserNotification.alloc().init()
            notif.setTitle_('ClaudeQ')
            if subtitle:
                notif.setSubtitle_(subtitle)
            if body:
                notif.setInformativeText_(body)
            # Override the Python app icon with the ClaudeQ icon
            icon_path = find_icon()
            if icon_path:
                image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
                if image:
                    notif.setValue_forKey_(image, '_identityImage')
                    notif.setValue_forKey_(False, '_identityImageHasBorder')
            NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(notif)
        except Exception:
            pass  # PyObjC not available or notification failed
