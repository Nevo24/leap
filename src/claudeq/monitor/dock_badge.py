"""Dock icon badge overlay for macOS.

Paints a red notification badge with a count onto the application's dock icon.
Badge count tracks the number of MRs that changed since the user last focused
the monitor window.
"""

from typing import Optional

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import QApplication

from claudeq.monitor.mr_tracking.base import MRState, MRStatus


class DockBadge:
    """Manages the dock icon badge overlay."""

    def __init__(self) -> None:
        self._count: int = 0
        self._base_icon: Optional[QPixmap] = None
        self._seen_statuses: dict[str, MRStatus] = {}

    def update(self, mr_statuses: dict[str, MRStatus], window_active: bool) -> None:
        """Recompute and render the badge based on current MR statuses.

        Args:
            mr_statuses: Current MR statuses by tag.
            window_active: Whether the monitor window is currently focused.
        """
        # If user is looking at the monitor, just update the snapshot — no badge
        if window_active:
            self._seen_statuses = dict(mr_statuses)
            if self._count > 0:
                self._count = 0
                self._render('')
            return

        changed = 0
        for tag, status in mr_statuses.items():
            seen = self._seen_statuses.get(tag)
            if seen is None:
                # New MR we haven't seen at all — counts as a change
                if status.state not in (MRState.NOT_CONFIGURED, MRState.NO_MR):
                    changed += 1
            elif (status.state != seen.state
                  or status.unresponded_count != seen.unresponded_count
                  or status.approved != seen.approved):
                changed += 1
        if changed == self._count:
            return
        self._count = changed
        self._render(str(changed) if changed > 0 else '')

    def clear(self, mr_statuses: dict[str, MRStatus]) -> None:
        """Clear the badge and snapshot current MR statuses as seen."""
        self._count = 0
        self._seen_statuses = dict(mr_statuses)
        self._render('')

    def discard_tag(self, tag: str) -> None:
        """Remove a tag from the seen snapshot."""
        self._seen_statuses.pop(tag, None)

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
