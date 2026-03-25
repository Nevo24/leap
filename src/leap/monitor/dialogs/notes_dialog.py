"""Free-form notes dialog for Leap Monitor.

Supports multiple notes stored as individual .txt files under .storage/notes/.
Left panel shows a note list; right panel is the editor. Notes auto-save on
switch, close, and Cmd+S.
"""

from pathlib import Path
from typing import Optional

from PyQt5.QtWidgets import (
    QDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)
from PyQt5.QtCore import Qt

from leap.monitor.pr_tracking.config import load_dialog_geometry, save_dialog_geometry
from leap.utils.constants import NOTES_DIR


MAX_NOTE_NAME_LEN = 80


def _note_path(name: str) -> Path:
    """Return the .txt path for a note name."""
    return NOTES_DIR / f'{name}.txt'


def _migrate_old_notes_file() -> None:
    """One-time migration: move .storage/notes.txt → .storage/notes/Notes.txt."""
    old_file = NOTES_DIR.parent / 'notes.txt'
    if old_file.exists() and old_file.is_file():
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        dest = NOTES_DIR / 'Notes.txt'
        if not dest.exists():
            try:
                old_file.rename(dest)
            except OSError:
                pass


def _list_notes() -> list[str]:
    """Return sorted list of note names (without extension)."""
    _migrate_old_notes_file()
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(
        (p.stem for p in NOTES_DIR.glob('*.txt') if p.is_file()),
        key=str.lower,
    )


class NotesDialog(QDialog):
    """Multi-note dialog with a list panel and an editor."""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Notes')
        self.resize(680, 450)
        saved = load_dialog_geometry('notes_dialog')
        if saved:
            self.resize(saved[0], saved[1])

        self._current_name: Optional[str] = None
        self._saved_text: str = ''

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)

        splitter = QSplitter(Qt.Horizontal)

        # ── Left panel: note list + buttons ──
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        left_layout.addWidget(QLabel('Notes'))

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_item_changed)
        left_layout.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        new_btn = QPushButton('+')
        new_btn.setFixedWidth(32)
        new_btn.setToolTip('New note')
        new_btn.clicked.connect(self._on_new)

        rename_btn = QPushButton('Rename')
        rename_btn.setToolTip('Rename selected note')
        rename_btn.clicked.connect(self._on_rename)

        delete_btn = QPushButton('Delete')
        delete_btn.setToolTip('Delete selected note')
        delete_btn.clicked.connect(self._on_delete)

        btn_row.addWidget(new_btn)
        btn_row.addWidget(rename_btn)
        btn_row.addWidget(delete_btn)
        left_layout.addLayout(btn_row)

        splitter.addWidget(left)

        # ── Right panel: editor ──
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self._title_label = QLabel('')
        self._title_label.setStyleSheet('font-weight: bold; font-size: 13px;')
        right_layout.addWidget(self._title_label)

        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText('Select or create a note...')
        self._editor.setEnabled(False)
        self._editor.setTabChangesFocus(False)
        right_layout.addWidget(self._editor, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)   # list: 1
        splitter.setStretchFactor(1, 3)   # editor: 3

        root_layout.addWidget(splitter, 1)

        # Populate list and auto-select first note
        self._refresh_list()
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    # ── List management ──────────────────────────────────────────────

    def _refresh_list(self, select_name: Optional[str] = None) -> None:
        """Reload the list widget from disk."""
        self._list.blockSignals(True)
        self._list.clear()
        for name in _list_notes():
            self._list.addItem(QListWidgetItem(name))
        if select_name:
            items = self._list.findItems(select_name, Qt.MatchExactly)
            if items:
                self._list.setCurrentItem(items[0])
        self._list.blockSignals(False)

    def _on_item_changed(
        self, current: Optional[QListWidgetItem], previous: Optional[QListWidgetItem],
    ) -> None:
        """Save the previous note, then load the newly selected one."""
        self._save_current()
        if current is None:
            self._current_name = None
            self._saved_text = ''
            self._editor.setPlainText('')
            self._editor.setEnabled(False)
            self._title_label.setText('')
            return
        name = current.text()
        self._current_name = name
        path = _note_path(name)
        try:
            text = path.read_text(encoding='utf-8') if path.exists() else ''
        except OSError:
            text = ''
        self._editor.setPlainText(text)
        self._saved_text = text
        self._editor.setEnabled(True)
        self._title_label.setText(name)

    # ── CRUD ─────────────────────────────────────────────────────────

    def _on_new(self) -> None:
        """Create a new note."""
        prev = ''
        while True:
            name, ok = QInputDialog.getText(
                self, 'New Note', 'Note name:', text=prev,
            )
            if not ok or not name.strip():
                return
            name = name.strip()
            prev = name
            if len(name) > MAX_NOTE_NAME_LEN:
                QMessageBox.warning(
                    self, 'Name Too Long',
                    f'Note name must be {MAX_NOTE_NAME_LEN} characters or fewer.',
                )
                continue
            if '/' in name or '\\' in name:
                QMessageBox.warning(self, 'Invalid Name', 'Note name cannot contain slashes.')
                continue
            if _note_path(name).exists():
                QMessageBox.warning(self, 'Already Exists', f"A note named '{name}' already exists.")
                continue
            break

        self._save_current()
        _note_path(name).write_text('', encoding='utf-8')
        self._refresh_list(select_name=name)
        self._on_item_changed(self._list.currentItem(), None)
        self._editor.setFocus()

    def _on_rename(self) -> None:
        """Rename the selected note."""
        if not self._current_name:
            return
        old_name = self._current_name
        prev = old_name
        while True:
            new_name, ok = QInputDialog.getText(
                self, 'Rename Note', 'New name:', text=prev,
            )
            if not ok or not new_name.strip():
                return
            new_name = new_name.strip()
            prev = new_name
            if new_name == old_name:
                return
            if len(new_name) > MAX_NOTE_NAME_LEN:
                QMessageBox.warning(
                    self, 'Name Too Long',
                    f'Note name must be {MAX_NOTE_NAME_LEN} characters or fewer.',
                )
                continue
            if '/' in new_name or '\\' in new_name:
                QMessageBox.warning(self, 'Invalid Name', 'Note name cannot contain slashes.')
                continue
            if _note_path(new_name).exists():
                QMessageBox.warning(self, 'Already Exists', f"A note named '{new_name}' already exists.")
                continue
            break

        self._save_current()
        old_path = _note_path(old_name)
        new_path = _note_path(new_name)
        try:
            old_path.rename(new_path)
        except OSError:
            QMessageBox.warning(self, 'Error', 'Could not rename the note file.')
            return
        self._current_name = new_name
        self._refresh_list(select_name=new_name)
        self._on_item_changed(self._list.currentItem(), None)

    def _on_delete(self) -> None:
        """Delete the selected note."""
        if not self._current_name:
            return
        reply = QMessageBox.question(
            self, 'Delete Note',
            f"Delete note '{self._current_name}'?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        path = _note_path(self._current_name)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        self._current_name = None
        self._saved_text = ''
        self._refresh_list()
        if self._list.count() > 0:
            self._list.setCurrentRow(0)
        else:
            self._on_item_changed(None, None)

    # ── Persistence ──────────────────────────────────────────────────

    def _save_current(self) -> None:
        """Write the current note to disk if changed."""
        if not self._current_name:
            return
        text = self._editor.toPlainText()
        if text != self._saved_text:
            try:
                NOTES_DIR.mkdir(parents=True, exist_ok=True)
                _note_path(self._current_name).write_text(text, encoding='utf-8')
                self._saved_text = text
            except OSError:
                pass

    def done(self, result: int) -> None:
        """Auto-save and persist geometry on Escape / reject."""
        self._save_current()
        save_dialog_geometry('notes_dialog', self.width(), self.height())
        super().done(result)

    def closeEvent(self, event: 'QCloseEvent') -> None:  # type: ignore[override]
        """Auto-save and persist geometry on X-button close."""
        self._save_current()
        save_dialog_geometry('notes_dialog', self.width(), self.height())
        super().closeEvent(event)

    def keyPressEvent(self, event: 'QKeyEvent') -> None:  # type: ignore[override]
        """Save on Cmd+S / Ctrl+S."""
        if event.key() == Qt.Key_S and event.modifiers() & Qt.ControlModifier:
            self._save_current()
            return
        super().keyPressEvent(event)
