"""Log history for Leap Monitor.

Stores transient status messages in-memory (session-only, not persisted)
and provides a dialog to view them.
"""

import html
import time
from dataclasses import dataclass, field
from typing import List, Optional

from PyQt5.QtWidgets import QDialog, QVBoxLayout, QTextBrowser, QDialogButtonBox, QWidget

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


class LogHistoryDialog(QDialog):
    """Dialog showing all past status messages with timestamps."""

    def __init__(self, log_history: LogHistory, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Log History')
        self.resize(800, 400)
        saved = load_dialog_geometry('log_history')
        if saved:
            self.resize(saved[0], saved[1])

        layout = QVBoxLayout(self)

        text_edit = QTextBrowser()
        text_edit.setOpenExternalLinks(True)

        entries = log_history.entries()
        if entries:
            html_lines = []
            for entry in entries:
                ts = time.strftime('%H:%M:%S', time.localtime(entry.timestamp))
                msg = html.escape(entry.message)
                # Color [Notification] messages in cyan, errors in red
                t = current_theme()
                if msg.startswith('[Notification]'):
                    rest = msg[len('[Notification]'):]
                    msg = f'<span style="color: {t.accent_blue};">[Notification]</span>{rest}'
                elif _is_error_message(msg):
                    msg = f'<span style="color: {t.accent_red};">{msg}</span>'
                line = f'[{ts}] {msg}'
                if entry.url:
                    escaped_url = html.escape(entry.url)
                    line += (
                        f' <a href="{escaped_url}" '
                        f'style="color: {t.accent_blue};">(link)</a>'
                    )
                html_lines.append(line)
            text_edit.setHtml(
                '<pre style="white-space: pre-wrap;">'
                + '<br>'.join(html_lines)
                + '</pre>'
            )
        else:
            text_edit.setPlainText('No status messages yet.')

        layout.addWidget(text_edit)

        btn_box = QDialogButtonBox(QDialogButtonBox.Close)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def done(self, result: int) -> None:
        """Save dialog size on close."""
        save_dialog_geometry('log_history', self.width(), self.height())
        super().done(result)
