"""Free-form notes dialog for Leap Monitor.

Supports multiple notes stored as individual .txt files under .storage/notes/.
Each note can be either plain text or a Google Keep-style checklist.
Left panel shows a note list; right panel is the editor. Notes auto-save on
switch, close, and Cmd+S.
"""

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)
from PyQt5.QtCore import QMimeData, QPoint, QSize, QUrl, Qt, pyqtSignal
from PyQt5.QtGui import QCursor, QDrag, QImage, QImageReader, QPixmap, QTextCursor, QTextImageFormat

from leap.monitor.pr_tracking.config import load_dialog_geometry, save_dialog_geometry
from leap.monitor.themes import current_theme
from leap.utils.constants import NOTE_IMAGES_DIR, NOTES_DIR


MAX_NOTE_NAME_LEN = 80
_NOTES_META_FILE: Path = NOTES_DIR / '.notes_meta.json'
_IMAGE_MARKER_RE = re.compile(r'!\[image\]\(([a-f0-9]+\.png)\)')
_NOTE_IMAGE_MAX_WIDTH = 400


# ── Note image helpers ──────────────────────────────────────────────

def _save_note_image(image: QImage) -> Optional[str]:
    """Save a QImage to .storage/note_images/ with MD5 dedup.

    Returns:
        Filename (e.g. 'abc123.png') on success, None on failure.
    """
    try:
        NOTE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        buf = image.bits().asstring(image.sizeInBytes())
        content_hash = hashlib.md5(buf).hexdigest()[:12]
        filename = f'{content_hash}.png'
        path = NOTE_IMAGES_DIR / filename
        if path.is_file():
            return filename
        if image.save(str(path), 'PNG'):
            return filename
        try:
            path.unlink()
        except OSError:
            pass
        return None
    except (OSError, Exception):
        return None


def _collect_image_refs(text: str) -> set[str]:
    """Return set of image filenames referenced in note text."""
    return set(_IMAGE_MARKER_RE.findall(text))


def _cleanup_orphaned_images(current_text: str, previous_text: str) -> None:
    """Delete images from note_images/ that were in previous but not in current."""
    old_refs = _collect_image_refs(previous_text)
    new_refs = _collect_image_refs(current_text)
    for filename in old_refs - new_refs:
        try:
            (NOTE_IMAGES_DIR / filename).unlink(missing_ok=True)
        except OSError:
            pass


def _delete_note_images(text: str) -> None:
    """Delete all images referenced by a note's text."""
    for filename in _collect_image_refs(text):
        try:
            (NOTE_IMAGES_DIR / filename).unlink(missing_ok=True)
        except OSError:
            pass


class _NoteTextEdit(QTextEdit):
    """QTextEdit with image paste support for notes.

    Pastes clipboard images into .storage/note_images/, inserts them
    inline in the document, and serializes to/from a text format using
    ``![image](filename.png)`` markers.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

    def insertFromMimeData(self, source: QMimeData) -> None:
        """Override paste to handle clipboard images."""
        if source.hasImage():
            image = source.imageData()
            if isinstance(image, QImage) and not image.isNull():
                filename = _save_note_image(image)
                if filename:
                    self._insert_image(filename)
                    return
        super().insertFromMimeData(source)

    def _insert_image(self, filename: str) -> None:
        """Insert an image into the document at the cursor."""
        path = str(NOTE_IMAGES_DIR / filename)
        # Register the image resource with the document
        img = QImage(path)
        if img.isNull():
            return
        if img.width() > _NOTE_IMAGE_MAX_WIDTH:
            img = img.scaledToWidth(_NOTE_IMAGE_MAX_WIDTH, Qt.SmoothTransformation)
        self.document().addResource(
            self.document().ImageResource, QUrl(filename), img,
        )
        cursor = self.textCursor()
        fmt = QTextImageFormat()
        fmt.setName(filename)
        fmt.setWidth(img.width())
        fmt.setHeight(img.height())
        cursor.insertImage(fmt)
        cursor.insertText('\n')
        self.setTextCursor(cursor)

    def set_note_content(self, text: str) -> None:
        """Load note text, rendering ![image](file.png) markers as inline images."""
        self.clear()
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.Start)

        parts = _IMAGE_MARKER_RE.split(text)
        # parts alternates: [text, filename, text, filename, ...]
        for i, part in enumerate(parts):
            if i % 2 == 0:
                # Text segment
                if part:
                    cursor.insertText(part)
            else:
                # Image filename
                path = str(NOTE_IMAGES_DIR / part)
                img = QImage(path)
                if not img.isNull():
                    if img.width() > _NOTE_IMAGE_MAX_WIDTH:
                        img = img.scaledToWidth(_NOTE_IMAGE_MAX_WIDTH, Qt.SmoothTransformation)
                    self.document().addResource(
                        self.document().ImageResource, QUrl(part), img,
                    )
                    fmt = QTextImageFormat()
                    fmt.setName(part)
                    fmt.setWidth(img.width())
                    fmt.setHeight(img.height())
                    cursor.insertImage(fmt)
                else:
                    # Image file missing — keep marker as text
                    cursor.insertText(f'![image]({part})')
        self.setTextCursor(cursor)

    def get_note_content(self) -> str:
        """Serialize the document back to text with ![image](file.png) markers."""
        doc = self.document()
        result: list[str] = []
        block = doc.begin()
        while block.isValid():
            if block != doc.begin():
                result.append('\n')
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid():
                    fmt = fragment.charFormat()
                    if fmt.isImageFormat():
                        img_fmt = fmt.toImageFormat()
                        name = img_fmt.name()
                        if name:
                            result.append(f'![image]({name})')
                    else:
                        result.append(fragment.text())
                it += 1
            block = block.next()
        return ''.join(result)


# ── Storage helpers ──────────────────────────────────────────────────

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
    """Return note names sorted by most recently edited first."""
    _migrate_old_notes_file()
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    files = [p for p in NOTES_DIR.glob('*.txt') if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.stem for p in files]


def _format_mtime(path: Path) -> str:
    """Return the file's mtime as a human-readable string (second precision)."""
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M:%S')
    except OSError:
        return ''


# ── Note mode metadata ───────────────────────────────────────────────

def _load_notes_meta() -> dict:
    try:
        if _NOTES_META_FILE.exists():
            return json.loads(_NOTES_META_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_notes_meta(meta: dict) -> None:
    try:
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        _NOTES_META_FILE.write_text(json.dumps(meta, indent=2), encoding='utf-8')
    except OSError:
        pass


def _get_note_mode(name: str) -> str:
    """Return 'text' or 'checklist' for a note."""
    return _load_notes_meta().get(name, {}).get('mode', 'text')


def _set_note_mode(name: str, mode: str) -> None:
    meta = _load_notes_meta()
    meta.setdefault(name, {})['mode'] = mode
    _save_notes_meta(meta)


def _remove_note_meta(name: str) -> None:
    meta = _load_notes_meta()
    if meta.pop(name, None) is not None:
        _save_notes_meta(meta)


def _rename_note_meta(old_name: str, new_name: str) -> None:
    meta = _load_notes_meta()
    if old_name in meta:
        meta[new_name] = meta.pop(old_name)
        _save_notes_meta(meta)


# ── Checklist serialization ──────────────────────────────────────────

def _parse_checklist(text: str) -> list[dict]:
    """Parse markdown-style checklist text into item dicts."""
    items: list[dict] = []
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in ('- [x]', '- [X]') or stripped.startswith('- [x] ') or stripped.startswith('- [X] '):
            items.append({'text': stripped[6:] if len(stripped) > 6 else '', 'checked': True})
        elif stripped == '- [ ]' or stripped.startswith('- [ ] '):
            items.append({'text': stripped[6:] if len(stripped) > 6 else '', 'checked': False})
        else:
            items.append({'text': stripped, 'checked': False})
    return items


def _serialize_checklist(items: list[dict]) -> str:
    """Serialize item dicts to markdown-style checklist text."""
    lines: list[str] = []
    for item in items:
        if not item['text'] and not item['checked']:
            continue  # skip empty unchecked items
        mark = 'x' if item['checked'] else ' '
        lines.append(f'- [{mark}] {item["text"]}')
    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════════════════
#  Checklist widgets (Google Keep style)
# ══════════════════════════════════════════════════════════════════════

class _ItemLineEdit(QLineEdit):
    """QLineEdit that signals Enter and Backspace-when-empty.

    Shows full text as tooltip when truncated.  Emits ``expand_requested``
    on any click so the parent can swap in a wrapping editor.
    """

    enter_pressed: pyqtSignal = pyqtSignal()
    empty_backspace: pyqtSignal = pyqtSignal()
    expand_requested: pyqtSignal = pyqtSignal()

    def _is_truncated(self) -> bool:
        if self.width() <= 0:
            return False
        return self.fontMetrics().horizontalAdvance(self.text()) > self.width() - 8

    def resizeEvent(self, event: 'QResizeEvent') -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._refresh_tooltip()

    def _refresh_tooltip(self) -> None:
        if self._is_truncated():
            self.setToolTip(self.text())
            self.setProperty('always_tooltip', True)
        else:
            self.setToolTip('')
            self.setProperty('always_tooltip', False)

    def mousePressEvent(self, event: 'QMouseEvent') -> None:  # type: ignore[override]
        from PyQt5.QtWidgets import QApplication
        win = self.window()
        if win and not win.isActiveWindow():
            QApplication.setActiveWindow(win)
        super().mousePressEvent(event)
        # Always request expand on click — parent decides whether to swap
        self.expand_requested.emit()

    def keyPressEvent(self, event: 'QKeyEvent') -> None:  # type: ignore[override]
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.enter_pressed.emit()
            return
        if event.key() == Qt.Key_Backspace and not self.text():
            self.empty_backspace.emit()
            return
        super().keyPressEvent(event)


class _DragGrip(QLabel):
    """Drag handle for checklist items — initiates a QDrag on mouse move.

    The drag is deferred to a zero-timer so that the QDrag event loop
    runs *after* the mouse handler returns.  This prevents a segfault
    when the rebuild (triggered inside the drag) destroys this widget
    while its mouseMoveEvent is still on the stack.
    """

    drag_started: pyqtSignal = pyqtSignal(int)  # emits the item index

    def __init__(self, index: int, parent: Optional[QWidget] = None) -> None:
        super().__init__('\u2261', parent)  # ≡ hamburger grip
        self._index = index
        self._drag_start: Optional[QPoint] = None
        t = current_theme()
        self.setFixedWidth(16)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(QCursor(Qt.OpenHandCursor))
        self.setStyleSheet(
            f'QLabel {{ color: {t.text_muted}; font-size: {t.font_size_large}px;'
            f' font-weight: bold; }}'
        )
        self.setToolTip('Drag to reorder')

    def mousePressEvent(self, event: 'QMouseEvent') -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start = event.pos()

    def mouseMoveEvent(self, event: 'QMouseEvent') -> None:
        if (self._drag_start is not None
                and (event.pos() - self._drag_start).manhattanLength()
                >= QApplication.startDragDistance()):
            self._drag_start = None
            # Defer the drag so this handler fully returns first
            idx = self._index
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, lambda: self.drag_started.emit(idx))

    def mouseReleaseEvent(self, event: 'QMouseEvent') -> None:
        self._drag_start = None


class _ChecklistItemWidget(QFrame):
    """Single checklist row: [grip] [checkbox] [editable text] [x]."""

    # Class-level: only one expand popup may be open at a time.
    _active_expand: Optional['_ChecklistItemWidget'] = None

    toggled: pyqtSignal = pyqtSignal(int, bool)
    text_edited: pyqtSignal = pyqtSignal(int, str)
    delete_requested: pyqtSignal = pyqtSignal(int)
    new_item_after: pyqtSignal = pyqtSignal(int)
    merge_up: pyqtSignal = pyqtSignal(int)
    drag_started: pyqtSignal = pyqtSignal(int)

    def __init__(
        self, index: int, text: str, checked: bool, parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._index = index
        self._checked = checked
        self._popup: Optional[QPlainTextEdit] = None

        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(8)

        # Drag handle (only for unchecked items — checked ones live
        # in the Completed section which has its own order)
        self._grip = _DragGrip(index)
        self._grip.drag_started.connect(lambda idx: self.drag_started.emit(idx))
        self._grip.setVisible(not checked)
        row.addWidget(self._grip)

        self._cb = QCheckBox()
        self._cb.setChecked(checked)
        self._cb.toggled.connect(lambda ch: self.toggled.emit(self._index, ch))
        row.addWidget(self._cb)

        self._edit = _ItemLineEdit(text)
        self._edit.setFrame(False)
        self._edit.textChanged.connect(
            lambda t: self.text_edited.emit(self._index, t),
        )
        self._edit.enter_pressed.connect(
            lambda: self.new_item_after.emit(self._index),
        )
        self._edit.expand_requested.connect(self._show_expand_popup)
        self._edit.empty_backspace.connect(
            lambda: self.merge_up.emit(self._index),
        )
        row.addWidget(self._edit, 1)

        self._del_btn = QPushButton('\u00d7')
        self._del_btn.setFixedSize(20, 20)
        t = current_theme()
        self._del_btn.setStyleSheet(
            f'QPushButton {{ border: none; color: {t.text_muted}; font-size: {t.font_size_base}px; }}'
            f'QPushButton:hover {{ color: {t.accent_red}; }}'
        )
        self._del_btn.setVisible(False)
        self._del_btn.clicked.connect(
            lambda: self.delete_requested.emit(self._index),
        )
        row.addWidget(self._del_btn, 0, Qt.AlignVCenter)

        self._apply_checked_style(checked)
        self.setStyleSheet(
            f'_ChecklistItemWidget {{ border-bottom: 1px solid {current_theme().border_subtle}; }}'
        )

    def _apply_checked_style(self, checked: bool) -> None:
        font = self._edit.font()
        font.setStrikeOut(checked)
        self._edit.setFont(font)
        t = current_theme()
        self._edit.setStyleSheet(
            f'QLineEdit {{ color: {t.text_muted}; background: transparent; }}'
            if checked else 'QLineEdit { background: transparent; }'
        )
        self._checked = checked

    def focus_edit(self, cursor_at_end: bool = True) -> None:
        """Focus this item's text field and expand into wrapping editor."""
        from PyQt5.QtWidgets import QApplication
        win = self.window()
        if win:
            QApplication.setActiveWindow(win)
        self._edit.setFocus()
        if cursor_at_end:
            self._edit.end(False)
        # Auto-expand so the user is always in the wrapping editor
        self._show_expand_popup()

    def enterEvent(self, event: 'QEvent') -> None:  # type: ignore[override]
        self._del_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event: 'QEvent') -> None:  # type: ignore[override]
        self._del_btn.setVisible(False)
        super().leaveEvent(event)

    def _dismiss_popup_if_active(self) -> None:
        """Dismiss this item's popup if it's open."""
        if self._popup is None:
            return
        import sip
        wrap = self._popup
        self._popup = None
        if _ChecklistItemWidget._active_expand is self:
            _ChecklistItemWidget._active_expand = None
        new_text = ''
        if not sip.isdeleted(wrap):
            new_text = wrap.toPlainText().replace('\n', ' ')
            wrap.setVisible(False)
            self.layout().removeWidget(wrap)
            wrap.deleteLater()
        if not sip.isdeleted(self._edit):
            self._edit.setVisible(True)
            if new_text and new_text != self._edit.text():
                self._edit.setText(new_text)

    def _show_expand_popup(self) -> None:
        """Replace QLineEdit with inline wrapping editor."""
        if self._popup is not None:
            return
        # Dismiss any other item's active popup first
        prev = _ChecklistItemWidget._active_expand
        if prev is not None and prev is not self:
            prev._dismiss_popup_if_active()
        _ChecklistItemWidget._active_expand = self
        row_layout = self.layout()
        edit_idx = row_layout.indexOf(self._edit)

        wrap = QPlainTextEdit()
        wrap.setFrameShape(QFrame.NoFrame)
        wrap.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrap.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrap.setTabChangesFocus(True)
        wrap.document().setDocumentMargin(2)
        wrap.setFont(self._edit.font())
        t = current_theme()
        color = t.text_muted if self._checked else t.text_primary
        wrap.setStyleSheet(
            f'QPlainTextEdit {{ color: {color}; background: transparent;'
            f' border: 1px solid {t.text_secondary}; }}'
        )
        wrap.setPlainText(self._edit.text())

        self._edit.setVisible(False)
        row_layout.insertWidget(edit_idx + 1, wrap, 1)
        self._popup = wrap

        def resize_wrap() -> None:
            if self._popup is not wrap:
                return
            line_h = wrap.fontMetrics().lineSpacing()
            line_count = max(1, int(wrap.document().size().height()))
            # Extra line of padding so the last line isn't clipped
            wrap.setFixedHeight(max(self._edit.height(), (line_count + 1) * line_h))

        def dismiss(save: bool) -> None:
            import sip
            if self._popup is not wrap:
                return
            self._popup = None
            if _ChecklistItemWidget._active_expand is self:
                _ChecklistItemWidget._active_expand = None
            if sip.isdeleted(wrap):
                return
            new_text = wrap.toPlainText().replace('\n', ' ') if save else None
            wrap.setVisible(False)
            row_layout.removeWidget(wrap)
            wrap.deleteLater()
            if sip.isdeleted(self._edit):
                return
            self._edit.setVisible(True)
            if new_text is not None and new_text != self._edit.text():
                self._edit.setText(new_text)

        def on_key(event: 'QKeyEvent') -> None:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                dismiss(True)
                self.new_item_after.emit(self._index)
                return
            if event.key() == Qt.Key_Escape:
                dismiss(False)
                self._edit.setFocus()
                return
            if event.key() == Qt.Key_Backspace and not wrap.toPlainText():
                dismiss(True)
                self.merge_up.emit(self._index)
                return
            QPlainTextEdit.keyPressEvent(wrap, event)
            self.text_edited.emit(self._index, wrap.toPlainText().replace('\n', ' '))
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, resize_wrap)

        def on_focus_out(event: 'QFocusEvent') -> None:
            try:
                QPlainTextEdit.focusOutEvent(wrap, event)
            except RuntimeError:
                return
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, lambda: dismiss(True))

        wrap.keyPressEvent = on_key
        wrap.focusOutEvent = on_focus_out

        from PyQt5.QtCore import QTimer
        QTimer.singleShot(0, resize_wrap)
        wrap.setFocus()
        cursor = wrap.textCursor()
        cursor.movePosition(cursor.End)
        wrap.setTextCursor(cursor)


class _ChecklistWidget(QWidget):
    """Google Keep-style checklist with active and completed sections."""

    content_changed: pyqtSignal = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._completed_visible: bool = True
        self._focus_after_rebuild: Optional[tuple[int, bool]] = None
        self._focus_add_after_rebuild: bool = False
        self._dragging_index: int = -1

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setAcceptDrops(True)
        self._scroll.viewport().setAcceptDrops(True)
        self._scroll.viewport().installEventFilter(self)

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll)

        self._add_field: Optional[_ItemLineEdit] = None

        # Drop indicator line (hidden by default)
        self._drop_indicator = QWidget(self._scroll.viewport())
        self._drop_indicator.setFixedHeight(2)
        self._drop_indicator.setStyleSheet(
            f'background-color: {current_theme().accent_blue};')
        self._drop_indicator.setVisible(False)
        self._drop_indicator.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_items(self, items: list[dict]) -> None:
        """Load items and rebuild the UI."""
        self._items = [dict(i) for i in items]
        self._rebuild()

    def get_items(self) -> list[dict]:
        """Return the current item list."""
        return [dict(i) for i in self._items]

    # ── Layout rebuild ───────────────────────────────────────────────

    def _clear_layout(self) -> None:
        # Move focus to the scroll area BEFORE destroying children —
        # if the focused widget is destroyed, macOS deactivates the
        # window and subsequent setFocus() calls silently fail.
        self._scroll.setFocus()
        # Reset class-level active expand — widgets are about to be destroyed
        _ChecklistItemWidget._active_expand = None
        self._add_popup = None
        while self._layout.count():
            child = self._layout.takeAt(0)
            w = child.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

    def _rebuild(self) -> None:
        self._clear_layout()

        active = [(i, d) for i, d in enumerate(self._items) if not d['checked']]
        completed = [(i, d) for i, d in enumerate(self._items) if d['checked']]

        focus_widget: Optional[_ChecklistItemWidget] = None
        focus_at_end = True

        # Active (unchecked) items
        for list_idx, data in active:
            w = self._make_item_widget(list_idx, data['text'], False)
            self._layout.addWidget(w)
            if (self._focus_after_rebuild is not None
                    and self._focus_after_rebuild[0] == list_idx):
                focus_widget = w
                focus_at_end = self._focus_after_rebuild[1]

        # "Add item" field
        self._add_field = _ItemLineEdit()
        self._add_field.setPlaceholderText('Add item')
        self._add_field.setFrame(False)
        self._add_field.setStyleSheet(
            'QLineEdit { padding: 6px 4px; background: transparent; }'
        )
        self._add_field.enter_pressed.connect(self._on_add_item)
        self._add_field.expand_requested.connect(self._expand_add_field)
        self._layout.addWidget(self._add_field)
        self._add_popup: Optional[QPlainTextEdit] = None

        # Completed section
        if completed:
            arrow = '\u25be' if self._completed_visible else '\u25b8'
            sep = QPushButton(f'{arrow}  Completed ({len(completed)})')
            sep.setFlat(True)
            t = current_theme()
            sep.setStyleSheet(
                f'QPushButton {{ text-align: left; color: {t.text_muted}; font-size: {t.font_size_base}px; '
                f'padding: 8px 4px 4px 4px; border: none; }}'
                f'QPushButton:hover {{ color: {t.text_secondary}; }}'
            )
            sep.setCursor(Qt.PointingHandCursor)
            sep.clicked.connect(self._toggle_completed)
            self._layout.addWidget(sep)

            if self._completed_visible:
                for list_idx, data in completed:
                    w = self._make_item_widget(list_idx, data['text'], True)
                    self._layout.addWidget(w)
                    if (self._focus_after_rebuild is not None
                            and self._focus_after_rebuild[0] == list_idx):
                        focus_widget = w
                        focus_at_end = self._focus_after_rebuild[1]

        self._layout.addStretch()

        # Restore focus (deferred so widgets are fully laid out).
        from PyQt5.QtCore import QTimer
        if focus_widget is not None:
            w_ref = focus_widget
            at_end = focus_at_end
            QTimer.singleShot(0, lambda: w_ref.focus_edit(cursor_at_end=at_end))
        elif self._focus_add_after_rebuild and self._add_field is not None:
            field = self._add_field
            def _focus_add() -> None:
                from PyQt5.QtWidgets import QApplication
                win = field.window()
                if win:
                    QApplication.setActiveWindow(win)
                field.setFocus()
                self._expand_add_field()
            QTimer.singleShot(0, _focus_add)
        self._focus_after_rebuild = None
        self._focus_add_after_rebuild = False

    def _make_item_widget(
        self, index: int, text: str, checked: bool,
    ) -> _ChecklistItemWidget:
        w = _ChecklistItemWidget(index, text, checked)
        w.toggled.connect(self._on_toggle)
        w.text_edited.connect(self._on_text_edited)
        w.delete_requested.connect(self._on_delete)
        w.new_item_after.connect(self._on_new_after)
        w.merge_up.connect(self._on_merge_up)
        w.drag_started.connect(self._start_item_drag)
        return w

    # ── Drag-and-drop reordering ──────────────────────────────────────

    def _start_item_drag(self, index: int) -> None:
        """Initiate a QDrag for the given item index."""
        if index < 0 or index >= len(self._items):
            return
        self._dragging_index = index
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData('application/x-leap-checklist-item', str(index).encode())
        drag.setMimeData(mime)

        # Grab a pixmap snapshot of the item widget for visual feedback
        for i in range(self._layout.count()):
            item_at = self._layout.itemAt(i)
            if item_at is None:
                continue
            w = item_at.widget()
            if isinstance(w, _ChecklistItemWidget) and w._index == index:
                pixmap = w.grab()
                drag.setPixmap(pixmap)
                drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
                break

        drag.exec_(Qt.MoveAction)
        self._drop_indicator.setVisible(False)
        self._dragging_index = -1

    def _active_indices(self) -> list[int]:
        """Return the indices of unchecked items in their current order."""
        return [i for i, d in enumerate(self._items) if not d['checked']]

    def _drop_target_index(self, viewport_y: int) -> int:
        """Determine which active-item position a drop at *viewport_y* maps to.

        Returns the index in self._items where the dragged item should be
        inserted BEFORE.
        """
        active = self._active_indices()
        for layout_pos in range(self._layout.count()):
            w = self._layout.itemAt(layout_pos).widget()
            if not isinstance(w, _ChecklistItemWidget):
                continue
            if w._index not in active:
                continue
            mapped = self._scroll.viewport().mapFromGlobal(
                w.mapToGlobal(QPoint(0, 0)))
            mid = mapped.y() + w.height() // 2
            if viewport_y < mid:
                return w._index
        # Past the last active item → append after the last one
        if active:
            return active[-1] + 1
        return 0

    def eventFilter(self, obj: 'QObject', event: 'QEvent') -> bool:
        """Handle drag-over and drop events on the scroll viewport."""
        from PyQt5.QtCore import QEvent as _QE
        if obj is not self._scroll.viewport():
            return super().eventFilter(obj, event)

        if event.type() == _QE.DragEnter:
            if event.mimeData().hasFormat('application/x-leap-checklist-item'):
                event.acceptProposedAction()
                return True

        elif event.type() == _QE.DragMove:
            if event.mimeData().hasFormat('application/x-leap-checklist-item'):
                event.acceptProposedAction()
                target = self._drop_target_index(event.pos().y())
                self._show_drop_indicator(target)
                return True

        elif event.type() == _QE.DragLeave:
            self._drop_indicator.setVisible(False)
            return True

        elif event.type() == _QE.Drop:
            self._drop_indicator.setVisible(False)
            if event.mimeData().hasFormat('application/x-leap-checklist-item'):
                event.acceptProposedAction()
                src = self._dragging_index
                dst = self._drop_target_index(event.pos().y())
                if src >= 0 and src != dst:
                    self._move_item(src, dst)
                return True

        return super().eventFilter(obj, event)

    def _show_drop_indicator(self, target_index: int) -> None:
        """Position the 2px indicator line above the target item."""
        active = self._active_indices()
        # Find the widget at target_index (or after the last active)
        target_y = 0
        for layout_pos in range(self._layout.count()):
            w = self._layout.itemAt(layout_pos).widget()
            if isinstance(w, _ChecklistItemWidget) and w._index == target_index:
                mapped = self._scroll.viewport().mapFromGlobal(
                    w.mapToGlobal(QPoint(0, 0)))
                target_y = mapped.y()
                break
        else:
            # Past the last active item — position after the last active widget
            for layout_pos in range(self._layout.count() - 1, -1, -1):
                w = self._layout.itemAt(layout_pos).widget()
                if isinstance(w, _ChecklistItemWidget) and w._index in active:
                    mapped = self._scroll.viewport().mapFromGlobal(
                        w.mapToGlobal(QPoint(0, 0)))
                    target_y = mapped.y() + w.height()
                    break
        self._drop_indicator.setGeometry(
            0, target_y, self._scroll.viewport().width(), 2)
        self._drop_indicator.setVisible(True)
        self._drop_indicator.raise_()

    def _move_item(self, src: int, dst: int) -> None:
        """Move an item from src index to before dst index in self._items."""
        item = self._items.pop(src)
        # Adjust dst if it was after the removed item
        if dst > src:
            dst -= 1
        self._items.insert(dst, item)
        self._rebuild()
        self.content_changed.emit()

    # ── Item actions ─────────────────────────────────────────────────

    def _on_toggle(self, index: int, checked: bool) -> None:
        self._items[index]['checked'] = checked
        self._rebuild()
        self.content_changed.emit()

    def _on_text_edited(self, index: int, text: str) -> None:
        self._items[index]['text'] = text
        self.content_changed.emit()

    def _on_delete(self, index: int) -> None:
        del self._items[index]
        self._rebuild()
        self.content_changed.emit()

    def _on_new_after(self, index: int) -> None:
        """Insert a new empty item after the given index."""
        new_idx = index + 1
        self._items.insert(new_idx, {'text': '', 'checked': False})
        self._focus_after_rebuild = (new_idx, True)
        self._rebuild()
        self.content_changed.emit()

    def _on_merge_up(self, index: int) -> None:
        """Backspace on empty item → delete it and focus the previous one."""
        if self._items[index]['text']:
            return
        unchecked = [i for i, d in enumerate(self._items) if not d['checked']]
        try:
            pos = unchecked.index(index)
        except ValueError:
            return
        if pos <= 0:
            return
        prev_idx = unchecked[pos - 1]
        del self._items[index]
        if prev_idx > index:
            prev_idx -= 1
        self._focus_after_rebuild = (prev_idx, True)
        self._rebuild()
        self.content_changed.emit()

    def _on_add_item(self) -> None:
        # If the add popup is active, read from it
        if self._add_popup is not None:
            text = self._add_popup.toPlainText().replace('\n', ' ').strip()
            self._dismiss_add_popup(save=False)  # don't save back, we're consuming it
        elif self._add_field is not None:
            text = self._add_field.text().strip()
        else:
            return
        if not text:
            return
        self._items.append({'text': text, 'checked': False})
        self._focus_add_after_rebuild = True
        self._rebuild()
        self.content_changed.emit()

    def _expand_add_field(self) -> None:
        """Swap the Add item QLineEdit for a wrapping editor."""
        if self._add_field is None:
            return
        # Dismiss any active item popup
        prev = _ChecklistItemWidget._active_expand
        if prev is not None:
            prev._dismiss_popup_if_active()
            _ChecklistItemWidget._active_expand = None
        # Dismiss any stale add popup
        if self._add_popup is not None:
            self._dismiss_add_popup(save=True)
        import time
        row_layout = self._layout
        edit_idx = row_layout.indexOf(self._add_field)

        wrap = QPlainTextEdit()
        wrap.setFrameShape(QFrame.NoFrame)
        wrap.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrap.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrap.setTabChangesFocus(True)
        wrap.document().setDocumentMargin(2)
        wrap.setFont(self._add_field.font())
        t = current_theme()
        wrap.setStyleSheet(
            f'QPlainTextEdit {{ color: {t.text_primary}; background: transparent;'
            f' border: 1px solid {t.text_secondary}; padding: 4px; }}'
        )
        wrap.setPlainText(self._add_field.text())
        wrap.setPlaceholderText('Add item')

        self._add_field.setVisible(False)
        row_layout.insertWidget(edit_idx + 1, wrap, 1)
        self._add_popup = wrap

        def resize_wrap() -> None:
            if self._add_popup is not wrap:
                return
            line_h = wrap.fontMetrics().lineSpacing()
            line_count = max(1, int(wrap.document().size().height()))
            wrap.setFixedHeight(max(self._add_field.height(), (line_count + 1) * line_h))

        def on_key(event: 'QKeyEvent') -> None:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                # Treat Enter as "add this item"
                self._on_add_item()
                return
            if event.key() == Qt.Key_Escape:
                self._dismiss_add_popup(save=True)
                self._add_field.setFocus()
                return
            QPlainTextEdit.keyPressEvent(wrap, event)
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, resize_wrap)

        def on_focus_out(event: 'QFocusEvent') -> None:
            try:
                QPlainTextEdit.focusOutEvent(wrap, event)
            except RuntimeError:
                return
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._dismiss_add_popup(save=True))

        wrap.keyPressEvent = on_key
        wrap.focusOutEvent = on_focus_out

        from PyQt5.QtCore import QTimer
        from PyQt5.QtWidgets import QApplication
        QTimer.singleShot(0, resize_wrap)
        win = wrap.window()
        if win:
            QApplication.setActiveWindow(win)
        wrap.setFocus()
        cursor = wrap.textCursor()
        cursor.movePosition(cursor.End)
        wrap.setTextCursor(cursor)

    def _dismiss_add_popup(self, save: bool) -> None:
        """Collapse the add-field wrapping editor back to QLineEdit."""
        import sip
        wrap = self._add_popup
        if wrap is None:
            return
        self._add_popup = None
        if sip.isdeleted(wrap):
            return
        new_text = wrap.toPlainText().replace('\n', ' ') if save else ''
        wrap.setVisible(False)
        self._layout.removeWidget(wrap)
        wrap.deleteLater()
        if self._add_field and not sip.isdeleted(self._add_field):
            self._add_field.setVisible(True)
            if save and new_text:
                self._add_field.setText(new_text)

    def _toggle_completed(self) -> None:
        self._completed_visible = not self._completed_visible
        self._rebuild()


# ══════════════════════════════════════════════════════════════════════
#  Main dialog
# ══════════════════════════════════════════════════════════════════════

class NotesDialog(QDialog):
    """Multi-note dialog with a list panel and a text/checklist editor."""

    _MODE_TEXT = 0
    _MODE_CHECKLIST = 1

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Notes')
        self.resize(680, 450)
        saved = load_dialog_geometry('notes_dialog')
        if saved:
            self.resize(saved[0], saved[1])

        self._current_name: Optional[str] = None
        self._saved_text: str = ''
        self._switching_mode: bool = False

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
        t = current_theme()
        self._list.setStyleSheet(
            f'QListWidget::item:selected {{'
            f'  background: transparent;'
            f'  border: 2px solid {t.accent_blue};'
            f'  border-radius: {t.border_radius}px;'
            f'}}'
        )
        self._list.currentItemChanged.connect(self._on_item_changed)
        left_layout.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        new_btn = QPushButton('+')
        new_btn.setFixedWidth(44)
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

        # ── Right panel: header + stacked editor ──
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Header row: title | mode combo | timestamp
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        self._title_label = QLabel('')
        self._title_label.setStyleSheet(f'font-weight: bold; font-size: {current_theme().font_size_large}px;')
        header_row.addWidget(self._title_label)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(['Text', 'Checklist'])
        self._mode_combo.setFixedWidth(100)
        self._mode_combo.setEnabled(False)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        header_row.addWidget(self._mode_combo)

        header_row.addStretch()
        right_layout.addLayout(header_row)

        # Stacked widget: page 0 = text, page 1 = checklist
        self._stack = QStackedWidget()

        self._editor = _NoteTextEdit()
        self._editor.setPlaceholderText('Select or create a note... (paste images with Cmd+V)')
        self._editor.setEnabled(False)
        self._editor.setTabChangesFocus(False)
        self._stack.addWidget(self._editor)

        self._checklist = _ChecklistWidget()
        self._checklist.content_changed.connect(self._on_checklist_changed)
        self._stack.addWidget(self._checklist)

        right_layout.addWidget(self._stack, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        root_layout.addWidget(splitter, 1)

        # Bottom bar with Close button
        from PyQt5.QtWidgets import QDialogButtonBox
        bottom_btns = QDialogButtonBox(QDialogButtonBox.Close)
        bottom_btns.rejected.connect(self.close)
        root_layout.addWidget(bottom_btns)

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
            item = QListWidgetItem()
            item.setData(Qt.UserRole, name)
            ts = _format_mtime(_note_path(name))
            widget = QWidget()
            layout = QVBoxLayout(widget)
            layout.setContentsMargins(4, 4, 4, 4)
            layout.setSpacing(1)
            name_label = QLabel(name)
            name_label.setStyleSheet(f'font-weight: bold; font-size: {current_theme().font_size_base}px;')
            layout.addWidget(name_label)
            if ts:
                ts_label = QLabel(ts)
                ts_label.setStyleSheet(f'color: {current_theme().text_secondary}; font-size: {current_theme().font_size_small}px;')
                layout.addWidget(ts_label)
            item.setSizeHint(widget.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, widget)
        if select_name:
            for i in range(self._list.count()):
                if self._list.item(i).data(Qt.UserRole) == select_name:
                    self._list.setCurrentRow(i)
                    break
        self._list.blockSignals(False)

    def _update_timestamp(self) -> None:
        """Update the timestamp in the list for the current note."""
        if not self._current_name:
            return
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.data(Qt.UserRole) == self._current_name:
                widget = self._list.itemWidget(item)
                if widget:
                    labels = widget.findChildren(QLabel)
                    ts = _format_mtime(_note_path(self._current_name))
                    if len(labels) >= 2 and ts:
                        labels[1].setText(ts)
                    elif len(labels) == 1 and ts:
                        ts_label = QLabel(ts)
                        ts_label.setStyleSheet(f'color: {current_theme().text_secondary}; font-size: {current_theme().font_size_small}px;')
                        widget.layout().addWidget(ts_label)
                    item.setSizeHint(widget.sizeHint())
                break

    def _current_mode(self) -> int:
        return self._stack.currentIndex()

    # ── Note selection ───────────────────────────────────────────────

    def _on_item_changed(
        self, current: Optional[QListWidgetItem], previous: Optional[QListWidgetItem],
    ) -> None:
        """Save the previous note, then load the newly selected one."""
        self._save_current()
        if current is None:
            self._current_name = None
            self._saved_text = ''
            self._editor.clear()
            self._editor.setEnabled(False)
            self._mode_combo.setEnabled(False)
            self._stack.setCurrentIndex(self._MODE_TEXT)
            self._title_label.setText('')
            return

        name = current.data(Qt.UserRole)
        self._current_name = name
        path = _note_path(name)
        try:
            text = path.read_text(encoding='utf-8') if path.exists() else ''
        except OSError:
            text = ''
        self._saved_text = text

        mode = _get_note_mode(name)
        self._switching_mode = True
        if mode == 'checklist':
            self._mode_combo.setCurrentIndex(self._MODE_CHECKLIST)
            self._checklist.set_items(_parse_checklist(text))
            self._stack.setCurrentIndex(self._MODE_CHECKLIST)
        else:
            self._mode_combo.setCurrentIndex(self._MODE_TEXT)
            self._editor.set_note_content(text)
            self._editor.setEnabled(True)
            self._stack.setCurrentIndex(self._MODE_TEXT)
        self._switching_mode = False

        self._mode_combo.setEnabled(True)
        self._title_label.setText(name)
        self._update_timestamp()

    # ── Mode switching ───────────────────────────────────────────────

    def _on_mode_changed(self, index: int) -> None:
        if self._switching_mode or not self._current_name:
            return

        if index == self._MODE_CHECKLIST:
            # Text → Checklist: each non-empty line becomes an unchecked item
            text = self._editor.get_note_content()
            items = _parse_checklist(text) if text.strip() else []
            self._checklist.set_items(items)
            self._stack.setCurrentIndex(self._MODE_CHECKLIST)
            _set_note_mode(self._current_name, 'checklist')
            # Save immediately so the format on disk matches
            self._save_current()
        else:
            # Checklist → Text: convert items to plain text lines
            items = self._checklist.get_items()
            lines = [item['text'] for item in items if item['text']]
            text = '\n'.join(lines)
            self._editor.set_note_content(text)
            self._editor.setEnabled(True)
            self._stack.setCurrentIndex(self._MODE_TEXT)
            _set_note_mode(self._current_name, 'text')
            self._save_current()

    def _on_checklist_changed(self) -> None:
        """Mark that checklist content changed (for auto-save comparison)."""
        # No-op signal receiver; _save_current reads live widget state.
        pass

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
                QMessageBox.warning(
                    self, 'Invalid Name', 'Note name cannot contain slashes.',
                )
                continue
            if _note_path(name).exists():
                QMessageBox.warning(
                    self, 'Already Exists',
                    f"A note named '{name}' already exists.",
                )
                continue
            break

        self._save_current()
        _note_path(name).write_text('', encoding='utf-8')
        self._refresh_list(select_name=name)
        self._on_item_changed(self._list.currentItem(), None)
        if self._current_mode() == self._MODE_TEXT:
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
                QMessageBox.warning(
                    self, 'Invalid Name', 'Note name cannot contain slashes.',
                )
                continue
            if _note_path(new_name).exists():
                QMessageBox.warning(
                    self, 'Already Exists',
                    f"A note named '{new_name}' already exists.",
                )
                continue
            break

        self._save_current()
        try:
            _note_path(old_name).rename(_note_path(new_name))
        except OSError:
            QMessageBox.warning(self, 'Error', 'Could not rename the note file.')
            return
        _rename_note_meta(old_name, new_name)
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
        try:
            path = _note_path(self._current_name)
            if path.exists():
                _delete_note_images(path.read_text(encoding='utf-8'))
            path.unlink(missing_ok=True)
        except OSError:
            pass
        _remove_note_meta(self._current_name)
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
        if self._current_mode() == self._MODE_CHECKLIST:
            text = _serialize_checklist(self._checklist.get_items())
        else:
            text = self._editor.get_note_content()
        if text != self._saved_text:
            try:
                NOTES_DIR.mkdir(parents=True, exist_ok=True)
                _note_path(self._current_name).write_text(text, encoding='utf-8')
                _cleanup_orphaned_images(text, self._saved_text)
                self._saved_text = text
                self._update_timestamp()
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
