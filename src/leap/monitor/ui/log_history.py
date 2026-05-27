"""Log history for Leap Monitor.

Stores transient status messages in-memory (session-only, not persisted)
and provides a dialog to view them.
"""

import html
import time
from dataclasses import dataclass
from typing import List, Optional

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
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


class LogHistory(QObject):
    """In-memory log of status messages with an ``entry_added`` signal."""

    entry_added = pyqtSignal(object)  # LogEntry

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._entries: List[LogEntry] = []

    def append(self, message: str, url: Optional[str] = None) -> None:
        """Append a new status message and notify listeners."""
        entry = LogEntry(
            timestamp=time.time(), message=message, url=url,
        )
        self._entries.append(entry)
        self.entry_added.emit(entry)

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


_MONO_OPEN = '<span style="font-family: \'Menlo\', \'Monaco\', monospace;">'


def _entry_html(entry: LogEntry) -> str:
    """Render a LogEntry to the HTML string used by ``_LogEntryRow``."""
    t = current_theme()
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
    return _MONO_OPEN + line + '</span>'


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
    _EMPTY_TEXT = 'No status messages yet.'

    def __init__(self, log_history: LogHistory, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Log History')
        self.resize(*self._DEFAULT_SIZE)
        saved = load_dialog_geometry('log_history')
        if saved:
            self.resize(saved[0], saved[1])

        self._log_history = log_history
        self._entry_labels: List[QLabel] = []
        self._empty_label: Optional[QLabel] = None

        layout = QVBoxLayout(self)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)

        self._container = QWidget()
        self._vlayout = QVBoxLayout(self._container)
        self._vlayout.setContentsMargins(0, 0, 0, 0)
        self._vlayout.setSpacing(0)

        entries = log_history.entries()
        if entries:
            for entry in entries:
                self._append_row(entry)
        else:
            self._show_empty_label()
        self._vlayout.addStretch(1)

        self._scroll.setWidget(self._container)
        layout.addWidget(self._scroll, 1)

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

        log_history.entry_added.connect(self._on_entry_added)

    def _show_empty_label(self) -> None:
        """Insert the 'no messages yet' placeholder at the top of the list."""
        self._empty_label = QLabel(self._EMPTY_TEXT, self._container)
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setContentsMargins(20, 20, 20, 20)
        self._entry_labels.append(self._empty_label)
        # Insert at position 0 so it ends up above the trailing stretch
        self._vlayout.insertWidget(0, self._empty_label)

    def _append_row(self, entry: LogEntry) -> None:
        """Build a row for *entry* and insert it before the trailing stretch."""
        row = _LogEntryRow(_entry_html(entry), self._container)
        self._entry_labels.append(row.label)
        # Insert before the trailing stretch (which is always the last item
        # once it has been added; before that, we're in initial build so
        # appending also works — count() with no stretch == row count).
        insert_at = self._vlayout.count()
        last_item = self._vlayout.itemAt(insert_at - 1) if insert_at else None
        if last_item is not None and last_item.spacerItem() is not None:
            insert_at -= 1
        self._vlayout.insertWidget(insert_at, row)

    def _on_entry_added(self, entry: LogEntry) -> None:
        """Slot: a new entry was appended to the underlying ``LogHistory``."""
        # Drop the empty placeholder on the first real entry
        if self._empty_label is not None:
            try:
                self._entry_labels.remove(self._empty_label)
            except ValueError:
                pass
            self._empty_label.setParent(None)
            self._empty_label.deleteLater()
            self._empty_label = None

        sb = self._scroll.verticalScrollBar()
        was_at_bottom = sb.value() >= sb.maximum() - 4

        self._append_row(entry)
        # Re-apply the current zoom font size to the freshly-added label
        # so it matches the rest (the mixin only writes on zoom events).
        if hasattr(self, '_apply_zoom_content_font_size'):
            self._apply_zoom_content_font_size()

        if was_at_bottom:
            # Defer until after layout so maximum() reflects the new row
            QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))

    def done(self, result: int) -> None:
        """Save dialog size and disconnect the live-update signal on close."""
        try:
            self._log_history.entry_added.disconnect(self._on_entry_added)
        except (TypeError, RuntimeError):
            pass
        save_dialog_geometry('log_history', self.width(), self.height())
        super().done(result)
