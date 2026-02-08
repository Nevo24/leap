"""Reusable PyQt5 widgets for ClaudeQ Monitor."""

import math
import webbrowser
from typing import Optional

from PyQt5.QtWidgets import QLabel, QWidget
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QCursor, QMouseEvent


class PulsingLabel(QLabel):
    """A label that can pulse its text color for attention."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pulsing: bool = False
        self._mr_url: Optional[str] = None
        self._phase: float = 0.0

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(50)
        self._pulse_timer.timeout.connect(self._animate)

        self.setAlignment(Qt.AlignCenter)

    def set_pulsing(self, pulsing: bool) -> None:
        self._pulsing = pulsing
        if pulsing:
            self._phase = 0.0
            self._pulse_timer.start()
        else:
            self._pulse_timer.stop()
            self.setStyleSheet('')

    def set_mr_url(self, url: Optional[str]) -> None:
        self._mr_url = url
        if url:
            self.setCursor(QCursor(Qt.PointingHandCursor))
        else:
            self.setCursor(QCursor(Qt.ArrowCursor))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._mr_url and event.button() == Qt.LeftButton:
            webbrowser.open(self._mr_url)
        else:
            super().mousePressEvent(event)

    def _animate(self) -> None:
        try:
            self._phase += 0.05
            # Oscillate opacity between 0.3 and 1.0
            opacity = 0.65 + 0.35 * math.sin(self._phase)
            r, g, b = 230, 150, 0  # orange
            self.setStyleSheet(f'color: rgba({r}, {g}, {b}, {opacity:.2f}); font-weight: bold;')
        except Exception:
            # Silently stop pulsing if animation fails
            self._pulse_timer.stop()
