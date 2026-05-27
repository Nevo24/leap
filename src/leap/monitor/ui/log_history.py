"""Log history for Leap Monitor.

Stores transient status messages in-memory (session-only, not persisted)
and provides a dialog to view them.
"""

import html
import time
from dataclasses import dataclass
from typing import List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.pr_tracking.config import load_dialog_geometry, save_dialog_geometry
from leap.monitor.themes import current_theme


@dataclass
class LogEntry:
    """A single log history entry."""
    timestamp: float
    message: str
    url: Optional[str] = None


class LogHistory:
    """In-memory log of status messages."""

    def __init__(self) -> None:
        self._entries: List[LogEntry] = []

    def append(self, message: str, url: Optional[str] = None) -> None:
        """Append a new status message."""
        self._entries.append(LogEntry(
            timestamp=time.time(), message=message, url=url,
        ))

    def entries(self) -> List[LogEntry]:
        """Return all log entries."""
        return list(self._entries)

    def clear(self) -> None:
        """Clear all entries."""
        self._entries.clear()


_ERROR_KEYWORDS = ('error', 'failed', 'fail', 'disabled')


def _is_error_message(msg: str) -> bool:
    """Return True if the message looks like an error/failure."""
    lower = msg.lower()
    return any(kw in lower for kw in _ERROR_KEYWORDS)


def _bumped_subtle_color() -> str:
    """Subtle border color, alpha-bumped to match the main table's intra-group separator.

    Mirrors the 2.5x alpha bump applied by ``border_subtle_pen()`` in
    ``ui/table_helpers.py`` so the dividers in the log dialog read the
    same weight as the lighter (intra-group) separators in the table.
    """
    bs = current_theme().border_subtle
    if bs.startswith('rgba('):
        parts = [p.strip() for p in bs[5:-1].split(',')]
        alpha = min(255, int(int(parts[3]) * 2.5))
        return f'rgba({parts[0]}, {parts[1]}, {parts[2]}, {alpha})'
    return bs


class _LogEntryRow(QFrame):
    """One log entry — subtle bottom border, hover highlight, word-wrapped rich text."""

    def __init__(self, html_text: str, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setObjectName('logEntryRow')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(0)

        self.label = QLabel(html_text, self)
        self.label.setTextFormat(Qt.RichText)
        self.label.setWordWrap(True)
        self.label.setOpenExternalLinks(True)
        self.label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        layout.addWidget(self.label, 1)

        subtle = _bumped_subtle_color()
        hover = current_theme().hover_bg
        # objectName-scoped so the hover/border doesn't leak to the inner QLabel
        self.setStyleSheet(
            f'QFrame#logEntryRow {{ border-bottom: 1px solid {subtle}; }}\n'
            f'QFrame#logEntryRow:hover {{ background-color: {hover}; }}\n'
        )


class LogHistoryDialog(ZoomMixin, QDialog):
    """Dialog showing all past status messages with timestamps."""

    _DEFAULT_SIZE = (800, 400)

    def __init__(self, log_history: LogHistory, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Log History')
        self.resize(*self._DEFAULT_SIZE)
        saved = load_dialog_geometry('log_history')
        if saved:
            self.resize(saved[0], saved[1])

        layout = QVBoxLayout(self)
        self._entry_labels: List[QLabel] = []

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        vlayout = QVBoxLayout(container)
        vlayout.setContentsMargins(0, 0, 0, 0)
        vlayout.setSpacing(0)

        entries = log_history.entries()
        if entries:
            t = current_theme()
            mono_open = (
                '<span style="font-family: \'Menlo\', \'Monaco\', monospace;">'
            )
            for entry in entries:
                ts = time.strftime('%H:%M:%S', time.localtime(entry.timestamp))
                msg = html.escape(entry.message)
                if msg.startswith('[Notification]'):
                    rest = msg[len('[Notification]'):]
                    msg = (
                        f'<span style="color: {t.accent_orange};">'
                        f'[Notification]</span>{rest}'
                    )
                elif _is_error_message(msg):
                    msg = f'<span style="color: {t.accent_red};">{msg}</span>'
                line = f'[{ts}] {msg}'
                if entry.url:
                    escaped_url = html.escape(entry.url)
                    line += (
                        f' <a href="{escaped_url}" '
                        f'style="color: {t.accent_blue};">(link)</a>'
                    )
                row = _LogEntryRow(mono_open + line + '</span>', container)
                self._entry_labels.append(row.label)
                vlayout.addWidget(row)
            vlayout.addStretch(1)
        else:
            empty = QLabel('No status messages yet.', container)
            empty.setAlignment(Qt.AlignCenter)
            empty.setContentsMargins(20, 20, 20, 20)
            self._entry_labels.append(empty)
            vlayout.addWidget(empty)
            vlayout.addStretch(1)

        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton('Close')
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._init_zoom(
            pref_key='log_history_font_size',
            content_pref_key='log_history_text_font_size',
            content_widgets=lambda: list(self._entry_labels),
        )

    def done(self, result: int) -> None:
        """Save dialog size on close."""
        save_dialog_geometry('log_history', self.width(), self.height())
        super().done(result)
