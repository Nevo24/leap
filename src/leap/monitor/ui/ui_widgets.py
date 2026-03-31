"""Reusable PyQt5 widgets for Leap Monitor."""

import math
import webbrowser
from typing import Optional

from PyQt5.QtWidgets import QAction, QApplication, QLabel, QMenu, QWidget
from PyQt5.QtCore import QPoint, QTimer, Qt
from PyQt5.QtGui import (
    QColor, QCursor, QLinearGradient, QMouseEvent, QPainter,
)

from leap.monitor.themes import current_theme


class ShimmerBar(QWidget):
    """A thin gradient bar with a slowly moving highlight shimmer."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._phase: float = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        self._phase += 0.004
        if self._phase > 2.0:
            self._phase -= 2.0
        self.update()

    def paintEvent(self, event: object) -> None:
        t = current_theme()
        w = self.width()
        h = self.height()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        grad = QLinearGradient(0, 0, w, 0)
        c1 = QColor(t.accent_blue)
        c2 = QColor(t.input_focus_border)

        # Base gradient
        grad.setColorAt(0.0, c1)
        grad.setColorAt(0.5, c2)
        grad.setColorAt(1.0, c1)

        # Add a bright shimmer spot that moves across
        shimmer_pos = self._phase if self._phase <= 1.0 else 2.0 - self._phase
        shimmer = QColor(c2)
        shimmer.setAlpha(255)
        bright = QColor('#ffffff') if t.is_dark else QColor('#000000')
        bright.setAlpha(60)

        lo = max(0.0, shimmer_pos - 0.08)
        hi = min(1.0, shimmer_pos + 0.08)
        grad.setColorAt(max(0.001, lo), c2 if lo > 0.4 else c1)
        if 0.01 < shimmer_pos < 0.99:
            grad.setColorAt(shimmer_pos, bright)
        grad.setColorAt(min(0.999, hi), c1 if hi < 0.6 else c2)

        painter.fillRect(0, 0, w, h, grad)
        painter.end()


class ElidedLabel(QLabel):
    """QLabel that elides text with '...' when it doesn't fit."""

    def __init__(self, text: str = '', parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self._full_text: str = text

    def setText(self, text: str) -> None:
        self._full_text = text
        super().setText(text)
        self.update()

    def is_truncated(self) -> bool:
        """Return True if the text is currently elided (doesn't fit)."""
        return self.fontMetrics().horizontalAdvance(self._full_text) > self.width()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        metrics = self.fontMetrics()
        elided = metrics.elidedText(
            self._full_text, Qt.ElideRight, self.width())
        painter.setPen(self.palette().windowText().color())
        painter.drawText(self.rect(), self.alignment(), elided)


class IndicatorPopup(QLabel):
    """Floating popup that explains PR indicator icons."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.ToolTip)
        self.setWordWrap(True)
        t = current_theme()
        self.setStyleSheet(
            'QLabel {'
            f'  background-color: {t.popup_bg};'
            f'  color: {t.text_primary};'
            f'  border: 1px solid {t.popup_border};'
            f'  padding: 8px 12px;'
            f'  font-size: {t.font_size_base}px;'
            '}'
        )
        self.setMaximumWidth(280)


class IndicatorLabel(QLabel):
    """A small label with its own hover popup for individual PR indicators."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._indicator_help: Optional[str] = None
        self._popup: Optional[IndicatorPopup] = None
        self._click_url: Optional[str] = None
        self._preserve_popup: bool = False

    def set_indicator_help(self, text: Optional[str]) -> None:
        """Set the help text shown in the hover popup."""
        self._indicator_help = text
        # Live-update visible popup
        if self._popup and self._popup.isVisible():
            if text:
                self._popup.setText(text)
                self._popup.adjustSize()
            else:
                self._popup.close()
                self._popup = None

    def update_popup_position(self) -> None:
        """Reposition visible popup after widget was reparented."""
        if self._popup and self._popup.isVisible():
            global_pos = self.mapToGlobal(QPoint(0, 0))
            self._popup.move(global_pos.x(),
                             global_pos.y() - self._popup.height() - 4)

    def set_click_url(self, url: Optional[str]) -> None:
        """Set the URL to open when this indicator is clicked."""
        self._click_url = url
        if url:
            self.setCursor(QCursor(Qt.PointingHandCursor))
        else:
            self.setCursor(QCursor(Qt.ArrowCursor))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._click_url and event.button() == Qt.LeftButton:
            webbrowser.open(self._click_url)
        else:
            super().mousePressEvent(event)

    def set_preserve_popup(self, preserve: bool) -> None:
        """Suppress enter/leave popup changes during widget reparenting."""
        self._preserve_popup = preserve

    def enterEvent(self, event) -> None:
        if self._indicator_help and not self._preserve_popup:
            if self._popup:
                self._popup.close()
            self._popup = IndicatorPopup()
            self._popup.setText(self._indicator_help)
            self._popup.adjustSize()
            global_pos = self.mapToGlobal(QPoint(0, 0))
            self._popup.move(global_pos.x(), global_pos.y() - self._popup.height() - 4)
            self._popup.show()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self._popup and not self._preserve_popup:
            self._popup.close()
            self._popup = None
        super().leaveEvent(event)


class PulsingLabel(QLabel):
    """A label that can pulse its text color for attention."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pulsing: bool = False
        self._pr_url: Optional[str] = None
        self._phase: float = 0.0
        self._on_send_to_leap: Optional[callable] = None
        self._on_send_combined_to_leap: Optional[callable] = None
        self._on_send_leap_threads: Optional[callable] = None
        self._on_send_leap_threads_combined: Optional[callable] = None
        self._has_unresponded: bool = False
        self._server_running: bool = False
        self._auto_fetch_leap: bool = True
        self._indicator_help: Optional[str] = None
        self._popup: Optional[IndicatorPopup] = None
        self._preserve_popup: bool = False

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(32)  # ~30fps for smoother animation
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

    def set_pr_url(self, url: Optional[str]) -> None:
        self._pr_url = url
        if url:
            self.setCursor(QCursor(Qt.PointingHandCursor))
        else:
            self.setCursor(QCursor(Qt.ArrowCursor))

    def set_send_to_leap_callback(self, callback: Optional[callable]) -> None:
        """Set the callback for 'Send all threads to Leap' context menu action."""
        self._on_send_to_leap = callback

    def set_send_combined_to_leap_callback(self, callback: Optional[callable]) -> None:
        """Set the callback for 'Send all threads as one message' context menu action."""
        self._on_send_combined_to_leap = callback

    def set_send_leap_threads_callback(self, callback: Optional[callable]) -> None:
        """Set the callback for 'Send /leap threads (each)' context menu action."""
        self._on_send_leap_threads = callback

    def set_send_leap_threads_combined_callback(self, callback: Optional[callable]) -> None:
        """Set the callback for 'Send /leap threads (combined)' context menu action."""
        self._on_send_leap_threads_combined = callback

    def set_has_unresponded(self, has_unresponded: bool) -> None:
        """Set whether there are unresponded threads (controls menu item enabled state)."""
        self._has_unresponded = has_unresponded

    def set_server_running(self, running: bool) -> None:
        """Set whether the Leap server is running (controls menu item enabled state)."""
        self._server_running = running

    def set_auto_fetch_leap(self, auto_fetch_leap: bool) -> None:
        """Set whether auto /leap fetch is enabled (disables manual /leap menu items)."""
        self._auto_fetch_leap = auto_fetch_leap

    def set_indicator_help(self, text: Optional[str]) -> None:
        """Set the help text shown in the hover popup."""
        self._indicator_help = text
        # Live-update visible popup
        if self._popup and self._popup.isVisible():
            if text:
                self._popup.setText(text)
                self._popup.adjustSize()
            else:
                self._popup.close()
                self._popup = None

    def update_popup_position(self) -> None:
        """Reposition visible popup after widget was reparented."""
        if self._popup and self._popup.isVisible():
            global_pos = self.mapToGlobal(QPoint(0, 0))
            self._popup.move(global_pos.x(),
                             global_pos.y() - self._popup.height() - 4)

    def set_preserve_popup(self, preserve: bool) -> None:
        """Suppress enter/leave popup changes during widget reparenting."""
        self._preserve_popup = preserve

    def enterEvent(self, event) -> None:
        if self._indicator_help and not self._preserve_popup:
            if self._popup:
                self._popup.close()
            self._popup = IndicatorPopup()
            self._popup.setText(self._indicator_help)
            self._popup.adjustSize()
            # Position above the widget
            global_pos = self.mapToGlobal(QPoint(0, 0))
            popup_x = global_pos.x()
            popup_y = global_pos.y() - self._popup.height() - 4
            self._popup.move(popup_x, popup_y)
            self._popup.show()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self._popup and not self._preserve_popup:
            self._popup.close()
            self._popup = None
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._pr_url and event.button() == Qt.LeftButton:
            if self._has_unresponded:
                self._show_context_menu(event.pos())
            else:
                webbrowser.open(self._pr_url)

    def _show_context_menu(self, pos) -> None:
        """Show context menu on the PR status label (left click)."""
        url = self._pr_url
        if not url:
            return

        # Capture callback refs before auto-refresh may destroy this widget
        send_to_leap = self._on_send_to_leap
        send_combined = self._on_send_combined_to_leap
        send_leap_threads = self._on_send_leap_threads
        send_leap_combined = self._on_send_leap_threads_combined
        has_unresponded = self._has_unresponded
        server_running = self._server_running
        auto_fetch_leap = self._auto_fetch_leap

        # Parent menu to the top-level window so it survives table refresh
        top_level = self.window()
        menu = QMenu(top_level)
        app = QApplication.instance()
        if getattr(app, 'tooltips_enabled', False):
            menu.setToolTipsVisible(True)

        go_action = QAction('Go to first thread', menu)
        go_action.setToolTip('Open the first unresponded PR thread in your browser')
        go_action.triggered.connect(lambda: webbrowser.open(url))
        menu.addAction(go_action)

        send_action = QAction('Send each thread to Leap (one per queue message)', menu)
        send_action.setToolTip(
            'Queue each unresponded thread as a separate message\n'
            'so the CLI handles them one at a time')
        send_action.triggered.connect(lambda: send_to_leap() if send_to_leap else None)
        send_action.setEnabled(bool(server_running and has_unresponded and send_to_leap))
        menu.addAction(send_action)

        combined_action = QAction('Send all threads to Leap (combined into one message)', menu)
        combined_action.setToolTip(
            'Concatenate all unresponded threads into a single\n'
            'message so the CLI sees them all at once')
        combined_action.triggered.connect(lambda: send_combined() if send_combined else None)
        combined_action.setEnabled(bool(server_running and has_unresponded and send_combined))
        menu.addAction(combined_action)

        menu.addSeparator()

        leap_each_action = QAction("Send each '/leap' thread to Leap (one per queue message)", menu)
        leap_each_action.setToolTip(
            "Only threads with an unacknowledged '/leap' comment —\n"
            'queue each as a separate message')
        leap_each_action.triggered.connect(lambda: send_leap_threads() if send_leap_threads else None)
        leap_each_action.setEnabled(bool(server_running and not auto_fetch_leap and has_unresponded and send_leap_threads))
        menu.addAction(leap_each_action)

        leap_combined_action = QAction("Send all '/leap' threads to Leap (combined into one message)", menu)
        leap_combined_action.setToolTip(
            "Only threads with an unacknowledged '/leap' comment —\n"
            'concatenate into a single message')
        leap_combined_action.triggered.connect(lambda: send_leap_combined() if send_leap_combined else None)
        leap_combined_action.setEnabled(bool(server_running and not auto_fetch_leap and has_unresponded and send_leap_combined))
        menu.addAction(leap_combined_action)

        menu.exec_(self.mapToGlobal(pos))

    def _animate(self) -> None:
        try:
            self._phase += 0.035  # slower, more gentle breathing
            # Smooth ease-in-out oscillation (0.4 → 1.0)
            raw = math.sin(self._phase)
            eased = raw * raw if raw >= 0 else -(raw * raw)  # ease-in-out
            opacity = 0.70 + 0.30 * eased
            t = current_theme()
            c = QColor(t.accent_orange)
            r, g, b = c.red(), c.green(), c.blue()
            self.setStyleSheet(
                f'color: rgba({r}, {g}, {b}, {opacity:.2f});'
                f' font-weight: bold;'
                f' font-size: {t.font_size_base}px;'
            )
        except Exception:
            self._pulse_timer.stop()
