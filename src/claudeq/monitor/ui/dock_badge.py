"""Dock icon badge overlay for macOS.

Paints a red notification badge with a count onto the application's dock icon.
Badge count tracks the number of MRs and session statuses that changed since
the user last focused the monitor window.
"""

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import QApplication

from claudeq.monitor.mr_tracking.base import MRState, MRStatus


class NotificationType(Enum):
    """Types of notification events the monitor can fire."""
    MR_UNRESPONDED = 'mr_unresponded'
    MR_ALL_RESPONDED = 'mr_all_responded'
    MR_APPROVED = 'mr_approved'
    SESSION_COMPLETED = 'session_completed'
    REVIEW_REQUESTED = 'review_requested'
    ASSIGNED = 'assigned'
    MENTIONED = 'mentioned'


@dataclass
class NotificationEvent:
    """A notification event emitted by DockBadge detection logic."""
    type: NotificationType
    tag: str
    mr_iid: Optional[int] = None
    mr_title: Optional[str] = None
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
        self._seen_mr_statuses: dict[str, MRStatus] = {}
        self._seen_session_busy: dict[str, bool] = {}
        self._busy_since: dict[str, float] = {}  # tag -> monotonic timestamp
        self._mr_changed: int = 0
        self._session_changed: int = 0
        self._notification_changed: int = 0

    def update(
        self,
        mr_statuses: dict[str, MRStatus],
        window_active: bool,
        dock_enabled: Optional[dict[str, bool]] = None,
    ) -> list[NotificationEvent]:
        """Recompute MR change count and render the badge.

        Args:
            mr_statuses: Current MR statuses by tag.
            window_active: Whether the monitor window is currently focused.
            dock_enabled: Map of NotificationType.value -> bool for dock counting.
                          If None, all types count toward the badge (legacy behavior).

        Returns:
            List of NotificationEvent for changes detected this cycle.
        """
        if window_active:
            self._seen_mr_statuses = dict(mr_statuses)
            self._mr_changed = 0
            self._render_total()
            return []

        events: list[NotificationEvent] = []
        dock_count = 0

        for tag, status in mr_statuses.items():
            seen = self._seen_mr_statuses.get(tag)
            tag_events = self._detect_mr_events(tag, seen, status)
            events.extend(tag_events)

            # Count toward dock badge only for types where dock is enabled
            for ev in tag_events:
                if dock_enabled is None or dock_enabled.get(ev.type.value, True):
                    dock_count += 1

        self._mr_changed = dock_count
        self._render_total()
        return events

    def _detect_mr_events(
        self, tag: str, seen: Optional[MRStatus], current: MRStatus,
    ) -> list[NotificationEvent]:
        """Detect notification events for a single MR status transition."""
        events: list[NotificationEvent] = []

        if seen is None:
            # First time seeing this tag — only fire if it's already in a notable state
            if current.state == MRState.UNRESPONDED:
                events.append(NotificationEvent(
                    type=NotificationType.MR_UNRESPONDED,
                    tag=tag,
                    mr_iid=current.mr_iid,
                    mr_title=current.mr_title,
                    unresponded_count=current.unresponded_count,
                ))
            return events

        # State became UNRESPONDED or unresponded_count increased
        if current.state == MRState.UNRESPONDED:
            if (seen.state != MRState.UNRESPONDED
                    or current.unresponded_count > seen.unresponded_count):
                events.append(NotificationEvent(
                    type=NotificationType.MR_UNRESPONDED,
                    tag=tag,
                    mr_iid=current.mr_iid,
                    mr_title=current.mr_title,
                    unresponded_count=current.unresponded_count,
                ))

        # State changed from UNRESPONDED to ALL_RESPONDED
        if (seen.state == MRState.UNRESPONDED
                and current.state == MRState.ALL_RESPONDED):
            events.append(NotificationEvent(
                type=NotificationType.MR_ALL_RESPONDED,
                tag=tag,
                mr_iid=current.mr_iid,
                mr_title=current.mr_title,
            ))

        # Approved changed False -> True
        if not seen.approved and current.approved:
            events.append(NotificationEvent(
                type=NotificationType.MR_APPROVED,
                tag=tag,
                mr_iid=current.mr_iid,
                mr_title=current.mr_title,
                approved_by=current.approved_by,
            ))

        return events

    def update_sessions(
        self,
        sessions: list[dict[str, Any]],
        window_active: bool,
        dock_enabled: Optional[dict[str, bool]] = None,
    ) -> list[NotificationEvent]:
        """Track session status changes (Running -> Idle).

        Args:
            sessions: List of session dicts with 'tag' and 'claude_busy' keys.
            window_active: Whether the monitor window is currently focused.
            dock_enabled: Map of NotificationType.value -> bool for dock counting.

        Returns:
            List of NotificationEvent for Running->Idle transitions.
        """
        current = {s['tag']: s.get('claude_busy', False) for s in sessions}
        if window_active:
            self._seen_session_busy = dict(current)
            self._session_changed = 0
            self._render_total()
            return []

        events: list[NotificationEvent] = []
        now = time.monotonic()
        dock_count = 0

        # Track when sessions become busy; detect Running -> Idle transitions
        for tag, busy in current.items():
            prev = self._seen_session_busy.get(tag)
            if busy and prev is not True:
                # Just became busy -- record the start time
                self._busy_since[tag] = now
            elif prev is True and not busy:
                # Running -> Idle -- only count if busy long enough
                started = self._busy_since.pop(tag, None)
                if started is not None and (now - started) >= self.MIN_BUSY_SECONDS:
                    ev = NotificationEvent(
                        type=NotificationType.SESSION_COMPLETED,
                        tag=tag,
                    )
                    events.append(ev)
                    if dock_enabled is None or dock_enabled.get(ev.type.value, True):
                        dock_count += 1
            elif not busy:
                self._busy_since.pop(tag, None)

        # Always update so we detect the next transition
        self._seen_session_busy = dict(current)
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

    def clear(self, mr_statuses: dict[str, MRStatus]) -> None:
        """Clear the badge and snapshot current statuses as seen."""
        self._mr_changed = 0
        self._session_changed = 0
        self._notification_changed = 0
        self._seen_mr_statuses = dict(mr_statuses)
        self._render_total()

    def discard_tag(self, tag: str) -> None:
        """Remove a tag from the seen snapshots."""
        self._seen_mr_statuses.pop(tag, None)
        self._seen_session_busy.pop(tag, None)

    def _render_total(self) -> None:
        """Render the combined badge count."""
        total = self._mr_changed + self._session_changed + self._notification_changed
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
