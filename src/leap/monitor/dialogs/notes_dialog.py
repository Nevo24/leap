"""Free-form notes dialog for Leap Monitor."""

from PyQt5.QtWidgets import (
    QDialog, QPlainTextEdit, QVBoxLayout, QWidget,
)
from PyQt5.QtCore import Qt

from leap.monitor.pr_tracking.config import load_dialog_geometry, save_dialog_geometry
from leap.utils.constants import NOTES_FILE


class NotesDialog(QDialog):
    """A simple notepad dialog for free-form user notes.

    Notes are auto-saved to disk on every close (X, Escape, etc.).
    """

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Notes')
        self.resize(500, 400)
        saved = load_dialog_geometry('notes_dialog')
        if saved:
            self.resize(saved[0], saved[1])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText('Write your notes here...')
        self._editor.setTabChangesFocus(False)
        layout.addWidget(self._editor)

        # Load existing notes
        if NOTES_FILE.exists():
            try:
                self._editor.setPlainText(NOTES_FILE.read_text(encoding='utf-8'))
            except OSError:
                pass

        # Track the saved snapshot to detect changes
        self._saved_text: str = self._editor.toPlainText()

    def _save(self) -> None:
        """Write notes to disk if changed."""
        text = self._editor.toPlainText()
        if text != self._saved_text:
            try:
                NOTES_FILE.write_text(text, encoding='utf-8')
                self._saved_text = text
            except OSError:
                pass

    def done(self, result: int) -> None:
        """Auto-save and persist geometry on Escape / reject."""
        self._save()
        save_dialog_geometry('notes_dialog', self.width(), self.height())
        super().done(result)

    def closeEvent(self, event: 'QCloseEvent') -> None:  # type: ignore[override]
        """Auto-save and persist geometry on X-button close."""
        self._save()
        save_dialog_geometry('notes_dialog', self.width(), self.height())
        super().closeEvent(event)

    def keyPressEvent(self, event: 'QKeyEvent') -> None:  # type: ignore[override]
        """Save on Cmd+S / Ctrl+S."""
        if event.key() == Qt.Key_S and event.modifiers() & Qt.ControlModifier:
            self._save()
            return
        super().keyPressEvent(event)
