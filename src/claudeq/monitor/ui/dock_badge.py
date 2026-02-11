"""Dock icon badge overlay for macOS.

Paints a red notification badge with a count onto the application's dock icon.
Badge count tracks the number of MRs and session statuses that changed since
the user last focused the monitor window.
"""

from typing import Any, Optional

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import QApplication

from claudeq.monitor.mr_tracking.base import MRState, MRStatus


class DockBadge:
    """Manages the dock icon badge overlay."""

    def __init__(self) -> None:
        self._base_icon: Optional[QPixmap] = None
        self._seen_mr_statuses: dict[str, MRStatus] = {}
        self._seen_session_busy: dict[str, bool] = {}
        self._mr_changed: int = 0
        self._session_changed: int = 0

    def update(self, mr_statuses: dict[str, MRStatus], window_active: bool) -> None:
        """Recompute MR change count and render the badge.

        Args:
            mr_statuses: Current MR statuses by tag.
            window_active: Whether the monitor window is currently focused.
        """
        if window_active:
            self._seen_mr_statuses = dict(mr_statuses)
            self._mr_changed = 0
            self._render_total()
            return

        changed = 0
        for tag, status in mr_statuses.items():
            seen = self._seen_mr_statuses.get(tag)
            if seen is None:
                if status.state not in (MRState.NOT_CONFIGURED, MRState.NO_MR):
                    changed += 1
            elif (status.state != seen.state
                  or status.unresponded_count != seen.unresponded_count
                  or status.approved != seen.approved):
                changed += 1
        self._mr_changed = changed
        self._render_total()

    def update_sessions(self, sessions: list[dict[str, Any]], window_active: bool) -> None:
        """Track session status changes (Running → Idle).

        Args:
            sessions: List of session dicts with 'tag' and 'claude_busy' keys.
            window_active: Whether the monitor window is currently focused.
        """
        current = {s['tag']: s.get('claude_busy', False) for s in sessions}
        if window_active:
            self._seen_session_busy = dict(current)
            self._session_changed = 0
            self._render_total()
            return

        # Detect Running → Idle transitions and accumulate
        for tag, busy in current.items():
            prev = self._seen_session_busy.get(tag)
            if prev is True and not busy:
                self._session_changed += 1
        # Always update so we detect the next transition
        self._seen_session_busy = dict(current)
        self._render_total()

    def clear(self, mr_statuses: dict[str, MRStatus]) -> None:
        """Clear the badge and snapshot current statuses as seen."""
        self._mr_changed = 0
        self._session_changed = 0
        self._seen_mr_statuses = dict(mr_statuses)
        self._render_total()

    def discard_tag(self, tag: str) -> None:
        """Remove a tag from the seen snapshots."""
        self._seen_mr_statuses.pop(tag, None)
        self._seen_session_busy.pop(tag, None)

    def _render_total(self) -> None:
        """Render the combined badge count."""
        total = self._mr_changed + self._session_changed
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

        # Red badge circle — top-right area
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
