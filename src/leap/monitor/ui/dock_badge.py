"""Dock icon badge overlay for macOS.

Paints a red notification badge with a count onto the application's dock icon.
Badge count tracks the number of PRs and session statuses that changed since
the user last focused the monitor window.
"""

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from leap.cli_providers.states import CLIState

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import QApplication

from leap.monitor.pr_tracking.base import PRState, PRStatus


class NotificationType(Enum):
    """Types of notification events the monitor can fire."""
    PR_UNRESPONDED = 'pr_unresponded'
    PR_ALL_RESPONDED = 'pr_all_responded'
    PR_APPROVED = 'pr_approved'
    SESSION_COMPLETED = 'session_completed'
    SESSION_NEEDS_PERMISSION = 'session_needs_permission'
    SESSION_NEEDS_INPUT = 'session_needs_input'
    SESSION_INTERRUPTED = 'session_interrupted'
    REVIEW_REQUESTED = 'review_requested'
    ASSIGNED = 'assigned'
    MENTIONED = 'mentioned'


@dataclass
class NotificationEvent:
    """A notification event emitted by DockBadge detection logic."""
    type: NotificationType
    tag: str
    pr_iid: Optional[int] = None
    pr_title: Optional[str] = None
    unresponded_count: int = 0
    approved_by: Optional[list[str]] = None
    url: Optional[str] = None
    notification_title: Optional[str] = None
    project_name: Optional[str] = None


class DockBadge:
    """Manages the dock icon badge overlay."""

    # Only count Running->Idle if the session was busy for at least this long.
    MIN_BUSY_SECONDS: float = 1.5

    def __init__(self) -> None:
        self._base_icon: Optional[QPixmap] = None
        self._seen_pr_statuses: dict[str, PRStatus] = {}
        self._seen_session_states: dict[str, str] = {}  # tag -> cli_state
        self._busy_since: dict[str, float] = {}  # tag -> monotonic timestamp
        self._pr_changed: int = 0
        self._session_changed: int = 0
        self._notification_changed: int = 0
        # Coalescing: track (tag, NotificationType) already counted while inactive
        self._session_notified: set[tuple[str, NotificationType]] = set()
        self._pr_notified: set[tuple[str, NotificationType]] = set()

    def update(
        self,
        pr_statuses: dict[str, PRStatus],
        window_active: bool,
        dock_enabled: Optional[dict[str, bool]] = None,
    ) -> list[NotificationEvent]:
        """Recompute PR change count and render the badge.

        Args:
            pr_statuses: Current PR statuses by tag.
            window_active: Whether the monitor window is currently focused.
            dock_enabled: Map of NotificationType.value -> bool for dock counting.
                          If None, all types count toward the badge (legacy behavior).

        Returns:
            List of NotificationEvent for changes detected this cycle.
        """
        if window_active:
            self._seen_pr_statuses = dict(pr_statuses)
            self._pr_changed = 0
            self._pr_notified.clear()
            self._render_total()
            return []

        events: list[NotificationEvent] = []
        dock_count = 0

        for tag, status in pr_statuses.items():
            seen = self._seen_pr_statuses.get(tag)
            tag_events = self._detect_pr_events(tag, seen, status)
            events.extend(tag_events)

            # Count toward dock badge only for types where dock is enabled
            # and not already counted for this tag while window inactive
            for ev in tag_events:
                key = (ev.tag, ev.type)
                if key not in self._pr_notified:
                    if dock_enabled is None or dock_enabled.get(ev.type.value, True):
                        dock_count += 1
                        self._pr_notified.add(key)

        self._seen_pr_statuses = dict(pr_statuses)
        self._pr_changed += dock_count
        self._render_total()
        return events

    def _detect_pr_events(
        self, tag: str, seen: Optional[PRStatus], current: PRStatus,
    ) -> list[NotificationEvent]:
        """Detect notification events for a single PR status transition."""
        events: list[NotificationEvent] = []

        if seen is None:
            # First time seeing this tag — seed silently, no alert on startup
            return events

        # State became UNRESPONDED or unresponded_count increased
        if current.state == PRState.UNRESPONDED:
            if (seen.state != PRState.UNRESPONDED
                    or current.unresponded_count > seen.unresponded_count):
                events.append(NotificationEvent(
                    type=NotificationType.PR_UNRESPONDED,
                    tag=tag,
                    pr_iid=current.pr_iid,
                    pr_title=current.pr_title,
                    unresponded_count=current.unresponded_count,
                ))

        # State changed from UNRESPONDED to ALL_RESPONDED
        if (seen.state == PRState.UNRESPONDED
                and current.state == PRState.ALL_RESPONDED):
            events.append(NotificationEvent(
                type=NotificationType.PR_ALL_RESPONDED,
                tag=tag,
                pr_iid=current.pr_iid,
                pr_title=current.pr_title,
            ))

        # Approval: notify when new approvers appear, but not for self-only changes.
        # Detect new approvers by comparing the approved_by lists.
        seen_approvers = set(seen.approved_by or [])
        current_approvers = set(current.approved_by or [])
        new_approvers = current_approvers - seen_approvers
        if new_approvers and current.approved:
            # The only self-detectable change is self_approved flipping False->True.
            # If that happened AND exactly one new approver appeared, it's us.
            self_just_approved = current.self_approved and not seen.self_approved
            only_self = self_just_approved and len(new_approvers) == 1
            if not only_self:
                events.append(NotificationEvent(
                    type=NotificationType.PR_APPROVED,
                    tag=tag,
                    pr_iid=current.pr_iid,
                    pr_title=current.pr_title,
                    approved_by=current.approved_by,
                ))

        return events

    def update_sessions(
        self,
        sessions: list[dict[str, Any]],
        window_active: bool,
        dock_enabled: Optional[dict[str, bool]] = None,
    ) -> list[NotificationEvent]:
        """Track session state changes and emit notification events.

        Detects transitions from 'running' to other states:
        - running -> idle: SESSION_COMPLETED
        - running -> needs_permission: SESSION_NEEDS_PERMISSION
        - running -> needs_input: SESSION_NEEDS_INPUT

        Args:
            sessions: List of session dicts with 'tag' and 'cli_state' keys.
            window_active: Whether the monitor window is currently focused.
            dock_enabled: Map of NotificationType.value -> bool for dock counting.

        Returns:
            List of NotificationEvent for state transitions.
        """
        current = {s['tag']: s.get('cli_state', CLIState.IDLE) for s in sessions}
        if window_active:
            self._seen_session_states = dict(current)
            self._session_changed = 0
            self._session_notified.clear()
            self._render_total()
            return []

        events: list[NotificationEvent] = []
        now = time.monotonic()
        dock_count = 0

        _TRANSITION_MAP = {
            CLIState.IDLE: NotificationType.SESSION_COMPLETED,
            CLIState.NEEDS_PERMISSION: NotificationType.SESSION_NEEDS_PERMISSION,
            CLIState.NEEDS_INPUT: NotificationType.SESSION_NEEDS_INPUT,
            CLIState.INTERRUPTED: NotificationType.SESSION_INTERRUPTED,
        }

        for tag, state in current.items():
            prev = self._seen_session_states.get(tag)
            if state == CLIState.RUNNING and prev != CLIState.RUNNING:
                self._busy_since[tag] = now
            elif prev == CLIState.RUNNING and state != CLIState.RUNNING:
                started = self._busy_since.pop(tag, None)
                if started is not None and (now - started) >= self.MIN_BUSY_SECONDS:
                    notif_type = _TRANSITION_MAP.get(state)
                    if notif_type:
                        ev = NotificationEvent(type=notif_type, tag=tag)
                        events.append(ev)
                        # Only count toward dock badge if not already counted
                        # for this tag+type while window is inactive
                        key = (tag, notif_type)
                        if key not in self._session_notified:
                            if dock_enabled is None or dock_enabled.get(ev.type.value, True):
                                dock_count += 1
                                self._session_notified.add(key)
            elif state != CLIState.RUNNING:
                self._busy_since.pop(tag, None)

        self._seen_session_states = dict(current)
        self._session_changed += dock_count
        self._render_total()
        return events

    def count_user_notification_events(
        self,
        events: list[NotificationEvent],
        window_active: bool,
        dock_enabled: Optional[dict[str, bool]] = None,
    ) -> None:
        """Count user notification events toward the dock badge.

        Args:
            events: Notification events to count.
            window_active: Whether the monitor window is focused.
            dock_enabled: Map of NotificationType.value -> bool.
        """
        if window_active:
            self._notification_changed = 0
            self._render_total()
            return

        count = 0
        for ev in events:
            if dock_enabled is None or dock_enabled.get(ev.type.value, True):
                count += 1
        self._notification_changed += count
        self._render_total()

    def clear(self, pr_statuses: dict[str, PRStatus]) -> None:
        """Clear the badge and snapshot current statuses as seen."""
        self._pr_changed = 0
        self._session_changed = 0
        self._notification_changed = 0
        self._seen_pr_statuses = dict(pr_statuses)
        self._session_notified.clear()
        self._pr_notified.clear()
        self._render_total()

    def discard_tag(self, tag: str) -> None:
        """Remove a tag from the seen snapshots."""
        self._seen_pr_statuses.pop(tag, None)
        self._seen_session_states.pop(tag, None)

    def _render_total(self) -> None:
        """Render the combined badge count."""
        total = self._pr_changed + self._session_changed + self._notification_changed
        self._render(str(total) if total > 0 else '')

    def _render(self, label: str) -> None:
        """Paint the badge onto the dock icon."""
        app = QApplication.instance()
        if not app:
            return

        # Capture the original icon once
        if self._base_icon is None:
            icon = app.windowIcon()
            if icon.isNull():
                return
            self._base_icon = icon.pixmap(128, 128)

        if not label:
            app.setWindowIcon(QIcon(self._base_icon))
            return

        # Paint badge onto a copy of the icon
        pixmap = self._base_icon.copy()
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        # Red badge circle -- top-right area
        badge_size = 52
        x = pixmap.width() - badge_size - 2
        y = 2
        painter.setBrush(QBrush(QColor(220, 40, 40)))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(x, y, badge_size, badge_size)

        # White text centered in the circle
        font = QFont('Helvetica', 28, QFont.Bold)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(QRect(x, y, badge_size, badge_size), Qt.AlignCenter, label)

        painter.end()
        app.setWindowIcon(QIcon(pixmap))
