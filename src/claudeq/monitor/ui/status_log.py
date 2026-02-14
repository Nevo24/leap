"""Status log history for ClaudeQ Monitor.

Stores transient status messages in-memory (session-only, not persisted)
and provides a dialog to view them.
"""

import html
import time
from dataclasses import dataclass, field
from typing import List, Optional

from PyQt5.QtWidgets import QDialog, QVBoxLayout, QTextBrowser, QDialogButtonBox, QWidget


@dataclass
class StatusEntry:
    """A single status log entry."""
    timestamp: float
    message: str
    url: Optional[str] = None


class StatusLog:
    """In-memory log of status messages."""

    def __init__(self) -> None:
        self._entries: List[StatusEntry] = []

    def append(self, message: str, url: Optional[str] = None) -> None:
        """Append a new status message."""
        self._entries.append(StatusEntry(
            timestamp=time.time(), message=message, url=url,
        ))

    def entries(self) -> List[StatusEntry]:
        """Return all log entries."""
        return list(self._entries)

    def clear(self) -> None:
        """Clear all entries."""
        self._entries.clear()


class StatusLogDialog(QDialog):
    """Dialog showing all past status messages with timestamps."""

    def __init__(self, status_log: StatusLog, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Status Log')
        self.resize(800, 400)

        layout = QVBoxLayout(self)

        text_edit = QTextBrowser()
        text_edit.setOpenExternalLinks(True)

        entries = status_log.entries()
        if entries:
            html_lines = []
            for entry in entries:
                ts = time.strftime('%H:%M:%S', time.localtime(entry.timestamp))
                msg = html.escape(entry.message)
                # Color [Notification] prefix in light pink
                if msg.startswith('[Notification]'):
                    prefix = '<span style="color: #FFB6C1;">[Notification]</span>'
                    msg = prefix + msg[len('[Notification]'):]
                line = f'[{ts}] {msg}'
                if entry.url:
                    escaped_url = html.escape(entry.url)
                    line += (
                        f' <a href="{escaped_url}" '
                        f'style="color: cyan;">(link)</a>'
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
