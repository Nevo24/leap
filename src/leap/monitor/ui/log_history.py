"""Log history for Leap Monitor.

Stores transient status messages in-memory (session-only, not persisted)
and provides a dialog to view them.
"""

import html
import time
from dataclasses import dataclass
from typing import List, Optional

from PyQt5.QtCore import QObject, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QPainter
from PyQt5.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.pr_tracking.config import load_dialog_geometry, save_dialog_geometry
from leap.monitor.themes import current_theme
from leap.monitor.ui.table_helpers import border_subtle_pen


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


class _LogContainer(QWidget):
    """Scroll-area container whose sizeHint respects heightForWidth.

    Word-wrapped QLabels report a sizeHint that assumes a very narrow
    width, which inflates the layout's sizeHint().height() by 3–4×.
    QScrollArea trusts that value verbatim under setWidgetResizable=True,
    which leaves a huge empty region below the last entry. Returning
    layout.heightForWidth(current_width) instead pins the container to
    the actual rendered content height.
    """

    def _hfw_height(self, fallback: int) -> int:
        layout = self.layout()
        if layout is not None and layout.hasHeightForWidth():
            w = self.width() or fallback
            return layout.heightForWidth(w)
        return fallback

    def sizeHint(self) -> QSize:  # type: ignore[override]
        hint = super().sizeHint()
        return QSize(hint.width(), self._hfw_height(hint.height()))

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        # QScrollArea sizes its child widget to at least minimumSizeHint.
        # QLabel(wordWrap=True) reports a minimumSizeHint that assumes the
        # narrowest reasonable width, which inflates the aggregated height
        # via the layout. Anchor it to heightForWidth at the real width.
        hint = super().minimumSizeHint()
        return QSize(hint.width(), self._hfw_height(hint.height()))

    def hasHeightForWidth(self) -> bool:  # type: ignore[override]
        layout = self.layout()
        if layout is not None:
            return layout.hasHeightForWidth()
        return super().hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:  # type: ignore[override]
        layout = self.layout()
        if layout is not None and layout.hasHeightForWidth():
            return layout.heightForWidth(width)
        return super().heightForWidth(width)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        # heightForWidth depends on the current width; tell parents to
        # re-query our hints whenever the width changes (e.g. user
        # resizes the dialog), so the scroll area shrinks the container
        # back down to the new actual content height.
        super().resizeEvent(event)
        self.updateGeometry()


class _LogEntryRow(QFrame):
    """One log entry — subtle bottom separator, hover highlight, word-wrapped rich text."""

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

        hover = current_theme().hover_bg
        # objectName-scoped so the hover doesn't leak to the inner QLabel.
        # Bottom separator is drawn in paintEvent via border_subtle_pen()
        # so it's pixel-identical to the column separators used in the
        # Resume dialog and the main table's intra-group dividers (QSS
        # 1px borders render very faintly on macOS HiDPI).
        self.setStyleSheet(
            f'QFrame#logEntryRow:hover {{ background-color: {hover}; }}\n'
        )

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(border_subtle_pen())
        y = self.height() - 1
        painter.drawLine(0, y, self.width(), y)
        painter.end()


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

        # _LogContainer overrides sizeHint to use heightForWidth — see
        # the class docstring for why the default would otherwise leave a
        # huge empty region scrollable below the last entry.
        self._container = _LogContainer()
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
            # The new row triggers a chain of layout invalidations
            # (insertWidget → stylesheet write from
            # _apply_zoom_content_font_size → resizeEvent → updateGeometry)
            # that don't all flush in a single event-loop tick.  A single
            # singleShot(0) would read a stale sb.maximum() and leave us
            # ~1 row short of the bottom.  rangeChanged fires whenever
            # the scroll range actually updates — one-shot subscription
            # snaps us to the freshly-extended bottom and unhooks.
            def _snap_to_bottom(_min=0, _max=0, _sb=sb):
                _sb.setValue(_sb.maximum())
                try:
                    _sb.rangeChanged.disconnect(_snap_to_bottom)
                except (TypeError, RuntimeError):
                    pass
            sb.rangeChanged.connect(_snap_to_bottom)
            # Belt-and-suspenders: if no rangeChanged fires (e.g. the
            # new row didn't actually push past viewport), the timer
            # below still does the no-op setValue.
            QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))

    def done(self, result: int) -> None:
        """Save dialog size and disconnect the live-update signal on close."""
        try:
            self._log_history.entry_added.disconnect(self._on_entry_added)
        except (TypeError, RuntimeError):
            pass
        save_dialog_geometry('log_history', self.width(), self.height())
        super().done(result)
