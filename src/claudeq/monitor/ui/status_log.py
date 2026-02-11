"""Status log history for ClaudeQ Monitor.

Stores transient status messages in-memory (session-only, not persisted)
and provides a dialog to view them.
"""

import time
from dataclasses import dataclass, field
from typing import List

from PyQt5.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QDialogButtonBox, QWidget


@dataclass
class StatusEntry:
    """A single status log entry."""
    timestamp: float
    message: str


class StatusLog:
    """In-memory log of status messages."""

    def __init__(self) -> None:
        self._entries: List[StatusEntry] = []

    def append(self, message: str) -> None:
        """Append a new status message."""
        self._entries.append(StatusEntry(timestamp=time.time(), message=message))

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
        self.resize(500, 400)

        layout = QVBoxLayout(self)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)

        entries = status_log.entries()
        if entries:
            lines = []
            for entry in entries:
                ts = time.strftime('%H:%M:%S', time.localtime(entry.timestamp))
                lines.append(f'[{ts}] {entry.message}')
            text_edit.setPlainText('\n'.join(lines))
        else:
            text_edit.setPlainText('No status messages yet.')

        layout.addWidget(text_edit)

        btn_box = QDialogButtonBox(QDialogButtonBox.Close)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)
