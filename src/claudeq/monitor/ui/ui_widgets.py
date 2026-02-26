"""Reusable PyQt5 widgets for ClaudeQ Monitor."""

import math
import webbrowser
from typing import Optional

from PyQt5.QtWidgets import QAction, QApplication, QLabel, QMenu, QWidget
from PyQt5.QtCore import QPoint, QTimer, Qt
from PyQt5.QtGui import QCursor, QMouseEvent, QPainter


class ElidedLabel(QLabel):
    """QLabel that elides text with '...' when it doesn't fit."""

    def __init__(self, text: str = '', parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self._full_text: str = text

    def setText(self, text: str) -> None:
        self._full_text = text
        super().setText(text)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        metrics = self.fontMetrics()
        elided = metrics.elidedText(
            self._full_text, Qt.ElideRight, self.width())
        painter.setPen(self.palette().windowText().color())
        painter.drawText(self.rect(), self.alignment(), elided)


class IndicatorPopup(QLabel):
    """Floating popup that explains MR indicator icons."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.ToolTip)
        self.setWordWrap(True)
        self.setStyleSheet(
            'QLabel {'
            '  background-color: #2b2b2b;'
            '  color: #e0e0e0;'
            '  border: 1px solid #555;'
            '  border-radius: 4px;'
            '  padding: 6px 8px;'
            '  font-size: 12px;'
            '}'
        )
        self.setMaximumWidth(260)


class IndicatorLabel(QLabel):
    """A small label with its own hover popup for individual MR indicators."""

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
        self._mr_url: Optional[str] = None
        self._phase: float = 0.0
        self._on_send_to_cq: Optional[callable] = None
        self._on_send_combined_to_cq: Optional[callable] = None
        self._on_send_cq_threads: Optional[callable] = None
        self._on_send_cq_threads_combined: Optional[callable] = None
        self._has_unresponded: bool = False
        self._server_running: bool = False
        self._auto_fetch_cq: bool = True
        self._indicator_help: Optional[str] = None
        self._popup: Optional[IndicatorPopup] = None
        self._preserve_popup: bool = False

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

    def set_send_to_cq_callback(self, callback: Optional[callable]) -> None:
        """Set the callback for 'Send all threads to CQ' context menu action."""
        self._on_send_to_cq = callback

    def set_send_combined_to_cq_callback(self, callback: Optional[callable]) -> None:
        """Set the callback for 'Send all threads as one message' context menu action."""
        self._on_send_combined_to_cq = callback

    def set_send_cq_threads_callback(self, callback: Optional[callable]) -> None:
        """Set the callback for 'Send /cq threads (each)' context menu action."""
        self._on_send_cq_threads = callback

    def set_send_cq_threads_combined_callback(self, callback: Optional[callable]) -> None:
        """Set the callback for 'Send /cq threads (combined)' context menu action."""
        self._on_send_cq_threads_combined = callback

    def set_has_unresponded(self, has_unresponded: bool) -> None:
        """Set whether there are unresponded threads (controls menu item enabled state)."""
        self._has_unresponded = has_unresponded

    def set_server_running(self, running: bool) -> None:
        """Set whether the CQ server is running (controls menu item enabled state)."""
        self._server_running = running

    def set_auto_fetch_cq(self, auto_fetch_cq: bool) -> None:
        """Set whether auto /cq fetch is enabled (disables manual /cq menu items)."""
        self._auto_fetch_cq = auto_fetch_cq

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
        if self._mr_url and event.button() == Qt.LeftButton:
            if self._has_unresponded:
                self._show_context_menu(event.pos())
            else:
                webbrowser.open(self._mr_url)

    def _show_context_menu(self, pos) -> None:
        """Show context menu on the MR status label (left click)."""
        url = self._mr_url
        if not url:
            return

        # Capture callback refs before auto-refresh may destroy this widget
        send_to_cq = self._on_send_to_cq
        send_combined = self._on_send_combined_to_cq
        send_cq_threads = self._on_send_cq_threads
        send_cq_combined = self._on_send_cq_threads_combined
        has_unresponded = self._has_unresponded
        server_running = self._server_running
        auto_fetch_cq = self._auto_fetch_cq

        # Parent menu to the top-level window so it survives table refresh
        top_level = self.window()
        menu = QMenu(top_level)
        app = QApplication.instance()
        if getattr(app, 'tooltips_enabled', False):
            menu.setToolTipsVisible(True)

        go_action = QAction('Go to first thread', menu)
        go_action.setToolTip('Open the first unresponded MR thread in your browser')
        go_action.triggered.connect(lambda: webbrowser.open(url))
        menu.addAction(go_action)

        send_action = QAction('Send each thread to CQ (one per queue message)', menu)
        send_action.setToolTip(
            'Queue each unresponded thread as a separate message\n'
            'so Claude handles them one at a time')
        send_action.triggered.connect(lambda: send_to_cq() if send_to_cq else None)
        send_action.setEnabled(bool(server_running and has_unresponded and send_to_cq))
        menu.addAction(send_action)

        combined_action = QAction('Send all threads to CQ (combined into one message)', menu)
        combined_action.setToolTip(
            'Concatenate all unresponded threads into a single\n'
            'message so Claude sees them all at once')
        combined_action.triggered.connect(lambda: send_combined() if send_combined else None)
        combined_action.setEnabled(bool(server_running and has_unresponded and send_combined))
        menu.addAction(combined_action)

        menu.addSeparator()

        cq_each_action = QAction("Send each '/cq' thread to CQ (one per queue message)", menu)
        cq_each_action.setToolTip(
            "Only threads with an unacknowledged '/cq' comment —\n"
            'queue each as a separate message')
        cq_each_action.triggered.connect(lambda: send_cq_threads() if send_cq_threads else None)
        cq_each_action.setEnabled(bool(server_running and not auto_fetch_cq and has_unresponded and send_cq_threads))
        menu.addAction(cq_each_action)

        cq_combined_action = QAction("Send all '/cq' threads to CQ (combined into one message)", menu)
        cq_combined_action.setToolTip(
            "Only threads with an unacknowledged '/cq' comment —\n"
            'concatenate into a single message')
        cq_combined_action.triggered.connect(lambda: send_cq_combined() if send_cq_combined else None)
        cq_combined_action.setEnabled(bool(server_running and not auto_fetch_cq and has_unresponded and send_cq_combined))
        menu.addAction(cq_combined_action)

        menu.exec_(self.mapToGlobal(pos))

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
