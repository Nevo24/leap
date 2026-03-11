"""Dialog for viewing and editing queued messages."""

from pathlib import Path
from typing import Optional

from PyQt5.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSplitter, QTextEdit, QVBoxLayout,
)
from PyQt5.QtCore import Qt

from leap.monitor.pr_tracking.config import load_dialog_geometry, save_dialog_geometry
from leap.utils.socket_utils import send_socket_request


class QueueEditDialog(QDialog):
    """Dialog to view and edit queued messages for a Leap session."""

    def __init__(
        self,
        tag: str,
        socket_path: Path,
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self._tag = tag
        self._socket_path = socket_path
        # Each entry: {'id': str, 'msg': str}
        self._messages: list[dict[str, str]] = []
        self._current_index: int = -1
        self._modified: bool = False

        self.setWindowTitle(f'Edit Queue — {tag}')
        self.resize(600, 450)
        saved = load_dialog_geometry('queue_edit')
        if saved:
            self.resize(saved[0], saved[1])

        self._build_ui()
        self._load_queue()

    def _build_ui(self) -> None:
        """Build the dialog layout."""
        layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Vertical)

        # Top: message list
        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_row_changed)
        splitter.addWidget(self._list)

        # Bottom: editor
        self._editor = QTextEdit()
        self._editor.setPlaceholderText('Select a message to edit')
        self._editor.setEnabled(False)
        self._editor.textChanged.connect(self._on_text_changed)
        splitter.addWidget(self._editor)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

        # Button row
        btn_layout = QHBoxLayout()
        self._save_btn = QPushButton('Save')
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._save_current)
        btn_layout.addWidget(self._save_btn)

        btn_layout.addStretch()

        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.close)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

    def _load_queue(self) -> None:
        """Fetch queue contents from the server."""
        response = send_socket_request(
            self._socket_path, {'type': 'get_queue_details'},
        )
        if response is None:
            QMessageBox.warning(
                self, 'Queue',
                'Could not fetch queue (server offline).',
            )
            self._editor.setPlaceholderText('Server offline')
            return
        if response.get('status') != 'ok':
            error = response.get('message', 'unknown error')
            QMessageBox.warning(
                self, 'Queue',
                f'Server error: {error}',
            )
            self._editor.setPlaceholderText('Error fetching queue')
            return

        self._messages = response.get('messages', [])
        if not self._messages:
            self._editor.setPlaceholderText('Queue is empty')
            return

        self._populate_list()

    def _populate_list(self) -> None:
        """Fill the list widget from self._messages."""
        self._list.blockSignals(True)
        self._list.clear()
        for i, entry in enumerate(self._messages):
            preview = entry['msg'].split('\n', 1)[0]
            if len(preview) > 80:
                preview = preview[:77] + '...'
            item = QListWidgetItem(f'[{i + 1}] {preview}')
            item.setData(Qt.UserRole, i)
            self._list.addItem(item)
        self._list.blockSignals(False)

        if self._messages:
            self._list.setCurrentRow(0)

    def _on_row_changed(self, row: int) -> None:
        """Handle list selection change."""
        if row < 0:
            return

        # Prompt for unsaved changes on the previous item
        if self._modified and self._current_index >= 0:
            result = self._prompt_unsaved()
            if result == QMessageBox.Cancel:
                # Revert selection
                self._list.blockSignals(True)
                self._list.setCurrentRow(self._current_index)
                self._list.blockSignals(False)
                return
            if result == QMessageBox.Yes:
                if not self._do_save(self._current_index):
                    # Save failed — stay on current item
                    self._list.blockSignals(True)
                    self._list.setCurrentRow(self._current_index)
                    self._list.blockSignals(False)
                    return

        self._current_index = row
        self._editor.setEnabled(True)
        self._editor.blockSignals(True)
        self._editor.setPlainText(self._messages[row]['msg'])
        self._editor.blockSignals(False)
        self._modified = False
        self._save_btn.setEnabled(False)

    def _on_text_changed(self) -> None:
        """Mark the current message as modified."""
        if self._current_index < 0:
            return
        self._modified = True
        self._save_btn.setEnabled(True)

    def _save_current(self) -> None:
        """Save the currently edited message."""
        if self._current_index < 0:
            return
        self._do_save(self._current_index)

    def _do_save(self, index: int) -> bool:
        """Send edit_message to the server. Returns True on success."""
        entry = self._messages[index]
        new_text = self._editor.toPlainText()

        response = send_socket_request(
            self._socket_path,
            {'type': 'edit_message', 'id': entry['id'], 'new_message': new_text},
        )

        if not response:
            QMessageBox.warning(
                self, 'Save Failed',
                'Could not reach the server (offline).',
            )
            return False

        if response.get('status') != 'ok':
            # Message was already sent or removed
            QMessageBox.warning(
                self, 'Save Failed',
                f'Message was already sent or removed from queue.',
            )
            # Remove the stale item from the dialog
            self._messages.pop(index)
            self._current_index = -1
            self._modified = False
            self._save_btn.setEnabled(False)
            self._editor.setEnabled(False)
            self._editor.clear()
            self._populate_list()
            return False

        # Success — update local state
        self._messages[index]['msg'] = new_text
        self._modified = False
        self._save_btn.setEnabled(False)

        # Update preview in the list
        preview = new_text.split('\n', 1)[0]
        if len(preview) > 80:
            preview = preview[:77] + '...'
        item = self._list.item(index)
        if item:
            item.setText(f'[{index + 1}] {preview}')
        return True

    def _prompt_unsaved(self) -> int:
        """Ask whether to save unsaved changes. Returns QMessageBox button."""
        return QMessageBox.question(
            self, 'Unsaved Changes',
            'Save changes to the current message?',
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
        )

    def closeEvent(self, event: object) -> None:
        """Prompt for unsaved changes before closing."""
        if self._modified and self._current_index >= 0:
            result = self._prompt_unsaved()
            if result == QMessageBox.Cancel:
                event.ignore()
                return
            if result == QMessageBox.Yes:
                self._do_save(self._current_index)
        save_dialog_geometry('queue_edit', self.width(), self.height())
        event.accept()
