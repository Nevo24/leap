"""Image-aware QTextEdit and Send Message dialog for Leap Monitor.

Provides a QTextEdit subclass that detects clipboard images on paste,
saves them to ``.storage/images/``, and inserts ``[Image #N]``
placeholders (matching the CLI client behaviour). Placeholders are
resolved to ``@/path/to/image`` when the text is retrieved for sending.
"""

import hashlib
import os
from typing import Optional

from PyQt5.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)
from PyQt5.QtCore import QMimeData, Qt
from PyQt5.QtGui import QImage

from leap.utils.constants import IMAGES_DIR


def _save_qimage(image: QImage) -> Optional[str]:
    """Save a QImage to ``.storage/images/``.

    Uses an MD5 hash of the image bytes as the filename so that
    saving the same image twice produces the same file (natural dedup).

    Returns:
        Absolute path to the saved file, or None on failure.
    """
    try:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        # Serialize to PNG bytes in memory to compute hash
        buf = image.bits().asstring(image.sizeInBytes())
        content_hash = hashlib.md5(buf).hexdigest()[:12]
        path = str(IMAGES_DIR / f'{content_hash}.png')
        if os.path.isfile(path):
            return path  # Already saved — dedup
        if image.save(path, 'PNG'):
            return path
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    except (OSError, Exception):
        return None


def resolve_image_placeholders(
    message: str,
    placeholders: dict[str, str],
) -> str:
    """Replace ``[Image #N]`` placeholders with ``@path`` references.

    Args:
        message: Text that may contain placeholders.
        placeholders: Mapping of placeholder string to file path.

    Returns:
        Message with placeholders replaced by ``@path`` references.
    """
    for placeholder, path in placeholders.items():
        if placeholder in message:
            message = message.replace(placeholder, f'@{path}')
    return message


class ImageTextEdit(QTextEdit):
    """QTextEdit that intercepts paste to handle clipboard images.

    When the clipboard contains an image, the image is saved and an
    ``[Image #N]`` placeholder is inserted at the cursor. Call
    :meth:`resolved_text` to convert placeholders to ``@path``
    references before sending.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._image_counter: int = 0
        self._image_placeholders: dict[str, str] = {}

    def keyPressEvent(self, event: 'QKeyEvent') -> None:
        """Accept Cmd+Enter / Cmd+Return as dialog accept (send)."""
        if (event.modifiers() & Qt.ControlModifier
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)):
            dialog = self.window()
            if isinstance(dialog, QDialog):
                dialog.accept()
                return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source: QMimeData) -> None:
        """Override paste to detect images in the clipboard."""
        if source.hasImage():
            image = source.imageData()
            if isinstance(image, QImage) and not image.isNull():
                path = _save_qimage(image)
                if path:
                    # Deduplicate: if identical image already pasted, reuse placeholder
                    existing = self._find_duplicate(path)
                    if existing:
                        os.unlink(path)
                        cursor = self.textCursor()
                        cursor.insertText(f'{existing} ')
                        self.setTextCursor(cursor)
                        return
                    self._image_counter += 1
                    placeholder = f'[Image #{self._image_counter}]'
                    self._image_placeholders[placeholder] = path
                    cursor = self.textCursor()
                    cursor.insertText(f'{placeholder} ')
                    self.setTextCursor(cursor)
                    return
        # Fall through to default text paste
        super().insertFromMimeData(source)

    def _find_duplicate(self, new_path: str) -> Optional[str]:
        """Return existing placeholder if *new_path* is identical to an already-pasted image."""
        try:
            with open(new_path, 'rb') as f:
                new_hash = hashlib.md5(f.read()).hexdigest()
        except OSError:
            return None
        for placeholder, existing_path in self._image_placeholders.items():
            try:
                with open(existing_path, 'rb') as f:
                    if hashlib.md5(f.read()).hexdigest() == new_hash:
                        return placeholder
            except OSError:
                continue
        return None

    def resolved_text(self) -> str:
        """Return text with image placeholders resolved to ``@path`` refs."""
        return resolve_image_placeholders(
            self.toPlainText(), self._image_placeholders,
        )

    def reset_images(self) -> None:
        """Clear image counter and placeholder map (e.g. after sending)."""
        self._image_counter = 0
        self._image_placeholders.clear()


class SendMessageDialog(QDialog):
    """Custom send-message dialog with image paste support.

    Replaces ``QInputDialog.getMultiLineText()`` for monitor message
    composition, adding clipboard image paste support.
    """

    def __init__(
        self,
        parent: Optional[QWidget],
        title: str,
        label: str,
        initial_text: str = '',
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(480, 250)

        layout = QVBoxLayout(self)

        lbl = QLabel(label)
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

        self._editor = ImageTextEdit()
        self._editor.setPlainText(initial_text)
        self._editor.setPlaceholderText('Type a message... (paste images with Cmd+V)')
        layout.addWidget(self._editor, 1)

        hint = QLabel('Tip: paste an image from clipboard to attach it\n'
                      'Tip: cmd+Enter to send')
        hint.setStyleSheet('color: #888; font-size: 11px;')
        layout.addWidget(hint)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        ok_btn = QPushButton('Send')
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def get_text(self) -> str:
        """Return the composed message with placeholders resolved."""
        return self._editor.resolved_text()

    @staticmethod
    def get_message(
        parent: Optional[QWidget],
        title: str,
        label: str,
        initial_text: str = '',
    ) -> tuple[str, bool]:
        """Show the dialog and return (text, accepted).

        Drop-in replacement for ``QInputDialog.getMultiLineText()``.
        """
        dlg = SendMessageDialog(parent, title, label, initial_text)
        accepted = dlg.exec_() == QDialog.Accepted
        return dlg.get_text(), accepted
