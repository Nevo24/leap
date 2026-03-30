"""PR display, dock badge, and banner notification methods."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, Optional

from PyQt5 import sip
from PyQt5.QtWidgets import QLabel

from leap.monitor.pr_tracking.base import PRState, PRStatus
from leap.monitor.pr_tracking.config import get_dock_enabled, get_notification_prefs
from leap.monitor.themes import current_theme
from leap.monitor.ui.dock_badge import NotificationEvent, NotificationType
from leap.monitor.ui.ui_widgets import IndicatorLabel, PulsingLabel
from leap.monitor.monitor_utils import find_icon

logger = logging.getLogger(__name__)

# Session notification types that get a "Terminal" action button.
_SESSION_NOTIFICATION_TYPES = {
    NotificationType.SESSION_COMPLETED,
    NotificationType.SESSION_NEEDS_PERMISSION,
    NotificationType.SESSION_NEEDS_INPUT,
    NotificationType.SESSION_INTERRUPTED,
}

# Category identifiers for UNUserNotificationCenter
_CAT_SESSION_WITH_CLIENT = 'leap_session_with_client'
_CAT_SESSION_SERVER_ONLY = 'leap_session_server_only'

# UNNotificationActionOptionForeground — brings app to foreground on tap
_ACTION_OPT_FOREGROUND = 1 << 2

# Module-level state for the modern notification API
_un_center: Any = None  # UNUserNotificationCenter (lazy-loaded)
_un_ready: bool = False  # True once framework + auth + categories are set up


def _setup_modern_notifications(monitor: MonitorWindow) -> None:
    """Set up UNUserNotificationCenter with categories and delegate.

    Call once at monitor startup.  Falls back gracefully — if the
    UserNotifications framework isn't available, ``_un_ready`` stays False
    and the legacy NSUserNotification path is used instead.
    """
    global _un_center, _un_ready
    if _un_ready:
        return  # Already set up
    try:
        import objc
        from Foundation import NSObject, NSSet

        objc.loadBundle(
            'UserNotifications', globals(),
            '/System/Library/Frameworks/UserNotifications.framework',
        )
        UNUserNotificationCenter = objc.lookUpClass('UNUserNotificationCenter')
        UNNotificationAction = objc.lookUpClass('UNNotificationAction')
        UNNotificationCategory = objc.lookUpClass('UNNotificationCategory')

        # Register block signatures that PyObjC can't infer
        objc.registerMetaDataForSelector(
            b'UNUserNotificationCenter',
            b'requestAuthorizationWithOptions:completionHandler:',
            {'arguments': {3: {'callable': {
                'retval': {'type': b'v'},
                'arguments': {0: {'type': b'^v'}, 1: {'type': b'Z'}, 2: {'type': b'@'}},
            }}}},
        )
        objc.registerMetaDataForSelector(
            b'UNUserNotificationCenter',
            b'addNotificationRequest:withCompletionHandler:',
            {'arguments': {3: {'callable': {
                'retval': {'type': b'v'},
                'arguments': {0: {'type': b'^v'}, 1: {'type': b'@'}},
            }}}},
        )
        objc.registerMetaDataForSelector(
            b'NSObject',
            b'userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:',
            {'arguments': {4: {'callable': {
                'retval': {'type': b'v'},
                'arguments': {0: {'type': b'^v'}},
            }}}},
        )
        objc.registerMetaDataForSelector(
            b'NSObject',
            b'userNotificationCenter:willPresentNotification:withCompletionHandler:',
            {'arguments': {4: {'callable': {
                'retval': {'type': b'v'},
                'arguments': {0: {'type': b'^v'}, 1: {'type': b'Q'}},
            }}}},
        )

        center = UNUserNotificationCenter.currentNotificationCenter()

        # ---- Delegate (handles clicks) ----
        # macOS calls delegate methods on an arbitrary thread, but Qt GUI
        # operations must run on the main thread.  We use QTimer.singleShot
        # with 0ms to safely marshal the work onto the Qt event loop.
        from PyQt5.QtCore import QTimer

        class _LeapUNDelegate(NSObject):
            monitor_ref = None

            def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
                self, center: object, response: object, completion: object,
            ) -> None:
                try:
                    mon = self.monitor_ref
                    if not mon or mon._shutting_down:
                        return
                    action_id = response.actionIdentifier()
                    user_info = response.notification().request().content().userInfo()
                    tag = user_info.get('tag', '') if user_info else ''
                    if action_id in ('server', 'client') and tag:
                        # Marshal to Qt main thread
                        def _navigate(_m: object = mon, _t: str = tag,
                                      _a: str = action_id) -> None:
                            if not _m._shutting_down:
                                _m._focus_session(_t, _a)
                        QTimer.singleShot(0, _navigate)
                    else:
                        # Default action (banner click) → bring monitor to foreground
                        def _activate(_m: object = mon) -> None:
                            if _m._shutting_down:
                                return
                            from AppKit import NSApplication
                            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                            _m.activateWindow()
                            _m.raise_()
                        QTimer.singleShot(0, _activate)
                except Exception:
                    logger.debug("Notification response handler error", exc_info=True)
                finally:
                    completion()

            def userNotificationCenter_willPresentNotification_withCompletionHandler_(
                self, center: object, notification: object, completion: object,
            ) -> None:
                # Show banner + sound even when app is in foreground
                # UNNotificationPresentationOptionBanner=16 | Sound=2
                completion(16 | 2)

        delegate = _LeapUNDelegate.alloc().init()
        delegate.monitor_ref = monitor
        center.setDelegate_(delegate)
        # Prevent GC
        monitor._un_delegate = delegate

        # ---- Authorization ----
        center.requestAuthorizationWithOptions_completionHandler_(
            (1 << 0) | (1 << 1) | (1 << 2),  # badge | sound | alert
            lambda granted, error: logger.debug(
                "UNUserNotificationCenter auth: granted=%s error=%s", granted, error),
        )

        # ---- Categories ----
        server_action = UNNotificationAction.actionWithIdentifier_title_options_(
            'server', 'Server Terminal', _ACTION_OPT_FOREGROUND)
        client_action = UNNotificationAction.actionWithIdentifier_title_options_(
            'client', 'Client Terminal', _ACTION_OPT_FOREGROUND)
        terminal_action = UNNotificationAction.actionWithIdentifier_title_options_(
            'server', 'Terminal', _ACTION_OPT_FOREGROUND)

        cat_both = UNNotificationCategory.categoryWithIdentifier_actions_intentIdentifiers_options_(
            _CAT_SESSION_WITH_CLIENT, [server_action, client_action], [], 0)
        cat_server = UNNotificationCategory.categoryWithIdentifier_actions_intentIdentifiers_options_(
            _CAT_SESSION_SERVER_ONLY, [terminal_action], [], 0)
        center.setNotificationCategories_(NSSet.setWithArray_([cat_both, cat_server]))

        _un_center = center
        _un_ready = True
        logger.debug("UNUserNotificationCenter ready (modern notifications)")

    except Exception:
        logger.debug("UNUserNotificationCenter setup failed, will use legacy API",
                      exc_info=True)


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

        # Log all events to the status log (regardless of banner/sound prefs).
        # User notifications (review_requested, assigned, mentioned) are already
        # logged in _process_user_notifications — skip them here.
        _USER_NOTIF_TYPES = {
            NotificationType.REVIEW_REQUESTED,
            NotificationType.ASSIGNED,
            NotificationType.MENTIONED,
        }
        for ev in events:
            if ev.type not in _USER_NOTIF_TYPES:
                subtitle, body = self._format_banner_text(ev)
                tag_prefix = f"[{subtitle}] " if subtitle else ''
                self._show_status(f"{tag_prefix}{body}", url=ev.url)

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
                # Check if this session has a connected client
                has_client = False
                if ev.type in _SESSION_NOTIFICATION_TYPES:
                    session = next(
                        (s for s in self.sessions if s['tag'] == ev.tag), None)
                    has_client = bool(session and session.get('has_client'))
                self._send_macos_notification(
                    subtitle, body, sound_name,
                    tag=ev.tag, notif_type=ev.type,
                    has_client=has_client,
                )
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
    def _send_macos_notification(
        subtitle: str, body: str, sound_name: str = 'None',
        tag: str = '', notif_type: Optional[NotificationType] = None,
        has_client: bool = False,
    ) -> None:
        """Send a macOS banner notification.

        Uses UNUserNotificationCenter (modern API) when available, with
        action buttons for session notifications.  Falls back to the
        legacy NSUserNotification API if the modern one isn't ready.

        Args:
            subtitle: Notification subtitle (usually the session tag).
            body: Notification body text.
            sound_name: Sound to play ('None' for silent).
            tag: Session tag — stored in userInfo for the delegate.
            notif_type: Notification type — session types get action buttons.
            has_client: Whether the session has a connected client.
                When True, both "Server Terminal" and "Client Terminal"
                buttons are shown.  When False, only "Terminal".
        """
        if _un_ready:
            _send_modern_notification(subtitle, body, sound_name,
                                     tag, notif_type, has_client)
        else:
            _send_legacy_notification(subtitle, body, sound_name)

    @staticmethod
    def _play_notification_sound(sound_name: str) -> None:
        """Play a notification sound without sending a banner."""
        from leap.monitor.dialogs.notifications_dialog import _play_sound
        _play_sound(sound_name)


# ---------------------------------------------------------------------------
# Notification delivery helpers (module-level, used by the static method)
# ---------------------------------------------------------------------------

def _send_modern_notification(
    subtitle: str, body: str, sound_name: str,
    tag: str, notif_type: Optional[NotificationType], has_client: bool,
) -> None:
    """Send a notification via UNUserNotificationCenter."""
    try:
        import objc
        from Foundation import NSDictionary

        UNMutableNotificationContent = objc.lookUpClass('UNMutableNotificationContent')
        UNNotificationRequest = objc.lookUpClass('UNNotificationRequest')
        UNNotificationSound = objc.lookUpClass('UNNotificationSound')

        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_('Leap')
        if subtitle:
            content.setSubtitle_(subtitle)
        if body:
            content.setBody_(body)

        # Attach category (action buttons) for session notifications
        if notif_type in _SESSION_NOTIFICATION_TYPES and tag:
            cat_id = _CAT_SESSION_WITH_CLIENT if has_client else _CAT_SESSION_SERVER_ONLY
            content.setCategoryIdentifier_(cat_id)
            content.setUserInfo_(NSDictionary.dictionaryWithDictionary_({'tag': tag}))

        # Sound
        if sound_name and sound_name != 'None':
            import os
            if os.path.isabs(sound_name):
                # Custom file — play separately, no system sound on the notification
                from leap.monitor.dialogs.notifications_dialog import _play_sound
                _play_sound(sound_name)
            elif sound_name == 'Default':
                content.setSound_(UNNotificationSound.defaultSound())
            else:
                content.setSound_(
                    UNNotificationSound.soundNamed_(sound_name))

        request = UNNotificationRequest.requestWithIdentifier_content_trigger_(
            str(uuid.uuid4()), content, None)
        _un_center.addNotificationRequest_withCompletionHandler_(
            request,
            lambda error: (
                logger.debug("UNNotification error: %s", error) if error else None
            ),
        )
    except Exception:
        logger.debug("Modern notification send failed", exc_info=True)
        # Fall back to legacy
        _send_legacy_notification(subtitle, body, sound_name)


def _send_legacy_notification(subtitle: str, body: str, sound_name: str) -> None:
    """Send a notification via the deprecated NSUserNotification (fallback)."""
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
        if sound_name and sound_name != 'None':
            if os.path.isabs(sound_name):
                pass  # played separately below
            elif sound_name == 'Default':
                notif.setSoundName_('NSUserNotificationDefaultSoundName')
            else:
                notif.setSoundName_(sound_name)
        icon_path = find_icon()
        if icon_path:
            image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
            if image:
                notif.setValue_forKey_(image, '_identityImage')
                notif.setValue_forKey_(False, '_identityImageHasBorder')
        NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(notif)
        if sound_name and os.path.isabs(sound_name):
            from leap.monitor.dialogs.notifications_dialog import _play_sound
            _play_sound(sound_name)
    except Exception:
        pass  # PyObjC not available or notification failed
