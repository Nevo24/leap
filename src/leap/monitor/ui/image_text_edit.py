"""Image-aware QTextEdit and Send Message dialog for Leap Monitor.

Provides a QTextEdit subclass that detects clipboard images on paste,
saves them to ``.storage/queue_images/``, and inserts ``[Image #N]``
placeholders (matching the CLI client behaviour). Placeholders are
resolved to ``@/path/to/image`` when the text is retrieved for sending.
"""

import hashlib
import os
import re
from pathlib import Path
from typing import Optional

from PyQt5.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QTextEdit,
    QVBoxLayout, QWidget,
)
from PyQt5.QtCore import QMimeData, QPoint, Qt
from PyQt5.QtGui import QImage, QPixmap, QTextCursor

from leap.monitor.themes import current_theme
from leap.utils.constants import QUEUE_IMAGES_DIR

_PLACEHOLDER_RE = re.compile(r'\[Image #\d+\]')
_PREVIEW_MAX = 400


def _save_qimage(image: QImage, target_dir: Optional[Path] = None) -> Optional[str]:
    """Save a QImage to a storage directory.

    Uses an MD5 hash of the image bytes as the filename so that
    saving the same image twice produces the same file (natural dedup).

    Args:
        image: The QImage to save.
        target_dir: Directory to save into. Defaults to ``.storage/queue_images/``.

    Returns:
        Absolute path to the saved file, or None on failure.
    """
    save_dir = target_dir or QUEUE_IMAGES_DIR
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        # Serialize to PNG bytes in memory to compute hash
        buf = image.bits().asstring(image.sizeInBytes())
        content_hash = hashlib.md5(buf).hexdigest()[:12]
        path = str(save_dir / f'{content_hash}.png')
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


class _ImagePreviewPopup(QLabel):
    """Frameless popup showing an image preview on hover."""

    def __init__(self) -> None:
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet('background: transparent; padding: 0px;')
        self._current_path: Optional[str] = None

    def show_for_path(self, path: str, global_pos: QPoint) -> None:
        if path == self._current_path and self.isVisible():
            return
        px = QPixmap(path)
        if px.isNull():
            self.hide()
            return
        if px.width() > _PREVIEW_MAX or px.height() > _PREVIEW_MAX:
            px = px.scaled(
                _PREVIEW_MAX, _PREVIEW_MAX,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
        self._current_path = path
        self.setPixmap(px)
        self.adjustSize()
        screen = QApplication.screenAt(global_pos)
        if screen:
            sg = screen.availableGeometry()
            x = min(global_pos.x() + 12, sg.right() - self.width())
            y = min(global_pos.y() + 12, sg.bottom() - self.height())
            self.move(max(x, sg.left()), max(y, sg.top()))
        else:
            self.move(global_pos.x() + 12, global_pos.y() + 12)
        self.show()

    def hide_preview(self) -> None:
        self._current_path = None
        self.hide()


class ImageTextEdit(QTextEdit):
    """QTextEdit that intercepts paste to handle clipboard images.

    When the clipboard contains an image, the image is saved and an
    ``[Image #N]`` placeholder is inserted at the cursor. Call
    :meth:`resolved_text` to convert placeholders to ``@path``
    references before sending.
    """

    def __init__(self, parent: Optional[QWidget] = None, image_dir: Optional[Path] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._image_dir: Optional[Path] = image_dir
        self._image_counter: int = 0
        self._image_placeholders: dict[str, str] = {}
        self._submit_callback: Optional[object] = None
        self._preview: Optional[_ImagePreviewPopup] = None

    def set_submit_callback(self, callback: object) -> None:
        """Set a custom callback for Cmd+Enter instead of dialog accept."""
        self._submit_callback = callback

    def _placeholder_path_at(self, pos: QPoint) -> Optional[str]:
        """Return the image path if the cursor is over a [Image #N] placeholder."""
        cursor = self.cursorForPosition(pos)
        block_text = cursor.block().text()
        col = cursor.positionInBlock()
        for m in _PLACEHOLDER_RE.finditer(block_text):
            if m.start() <= col < m.end():
                return self._image_placeholders.get(m.group())
        return None

    def mouseMoveEvent(self, event: 'QMouseEvent') -> None:
        path = self._placeholder_path_at(event.pos())
        if path:
            if self._preview is None:
                self._preview = _ImagePreviewPopup()
            self._preview.show_for_path(path, event.globalPos())
        elif self._preview and self._preview.isVisible():
            self._preview.hide_preview()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: 'QEvent') -> None:
        if self._preview and self._preview.isVisible():
            self._preview.hide_preview()
        super().leaveEvent(event)

    def keyPressEvent(self, event: 'QKeyEvent') -> None:
        """Accept Cmd+Enter / Cmd+Return as submit shortcut."""
        if (event.modifiers() & Qt.ControlModifier
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)):
            if self._submit_callback:
                self._submit_callback()
                return
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
                path = _save_qimage(image, self._image_dir)
                if path:
                    # Deduplicate: if identical image already pasted, reuse placeholder
                    existing = self._find_duplicate(path)
                    if existing:
                        # Only delete if it's a different file (not the same dedup hit)
                        existing_path = self._image_placeholders.get(existing)
                        if existing_path and os.path.realpath(path) != os.path.realpath(existing_path):
                            try:
                                os.unlink(path)
                            except OSError:
                                pass
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
        hint.setStyleSheet(f'color: {current_theme().text_muted}; font-size: {current_theme().font_size_small}px;')
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
