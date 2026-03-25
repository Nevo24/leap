"""Free-form notes dialog for Leap Monitor.

Supports multiple notes stored as individual .txt files under .storage/notes/.
Each note can be either plain text or a Google Keep-style checklist.
Left panel shows a note list; right panel is the editor. Notes auto-save on
switch, close, and Cmd+S.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget,
)
from PyQt5.QtCore import Qt, pyqtSignal

from leap.monitor.pr_tracking.config import load_dialog_geometry, save_dialog_geometry
from leap.utils.constants import NOTES_DIR


MAX_NOTE_NAME_LEN = 80
_NOTES_META_FILE: Path = NOTES_DIR / '.notes_meta.json'


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
        if stripped.startswith('- [x] ') or stripped.startswith('- [X] '):
            items.append({'text': stripped[6:], 'checked': True})
        elif stripped.startswith('- [ ] '):
            items.append({'text': stripped[6:], 'checked': False})
        else:
            items.append({'text': stripped, 'checked': False})
    return items


def _serialize_checklist(items: list[dict]) -> str:
    """Serialize item dicts to markdown-style checklist text."""
    lines: list[str] = []
    for item in items:
        mark = 'x' if item['checked'] else ' '
        lines.append(f'- [{mark}] {item["text"]}')
    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════════════════
#  Checklist widgets (Google Keep style)
# ══════════════════════════════════════════════════════════════════════

class _ItemLineEdit(QLineEdit):
    """QLineEdit that signals Enter and Backspace-when-empty."""

    enter_pressed: pyqtSignal = pyqtSignal()
    empty_backspace: pyqtSignal = pyqtSignal()

    def keyPressEvent(self, event: 'QKeyEvent') -> None:  # type: ignore[override]
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.enter_pressed.emit()
            return
        if event.key() == Qt.Key_Backspace and not self.text():
            self.empty_backspace.emit()
            return
        super().keyPressEvent(event)


class _ChecklistItemWidget(QFrame):
    """Single checklist row: [checkbox] [editable text] [x]."""

    toggled: pyqtSignal = pyqtSignal(int, bool)
    text_edited: pyqtSignal = pyqtSignal(int, str)
    delete_requested: pyqtSignal = pyqtSignal(int)
    new_item_after: pyqtSignal = pyqtSignal(int)
    merge_up: pyqtSignal = pyqtSignal(int)

    def __init__(
        self, index: int, text: str, checked: bool, parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._index = index

        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(6)

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
        self._edit.empty_backspace.connect(
            lambda: self.merge_up.emit(self._index),
        )
        row.addWidget(self._edit, 1)

        self._del_btn = QPushButton('\u00d7')
        self._del_btn.setFixedSize(20, 20)
        self._del_btn.setStyleSheet(
            'QPushButton { border: none; color: #999; font-size: 14px; }'
            'QPushButton:hover { color: #ff4444; }'
        )
        self._del_btn.setVisible(False)
        self._del_btn.clicked.connect(
            lambda: self.delete_requested.emit(self._index),
        )
        row.addWidget(self._del_btn)

        self._apply_checked_style(checked)
        self.setStyleSheet(
            '_ChecklistItemWidget { border-bottom: 1px solid rgba(128,128,128,0.15); }'
        )

    def _apply_checked_style(self, checked: bool) -> None:
        font = self._edit.font()
        font.setStrikeOut(checked)
        self._edit.setFont(font)
        self._edit.setStyleSheet(
            'QLineEdit { color: #888; background: transparent; }'
            if checked else 'QLineEdit { background: transparent; }'
        )

    def focus_edit(self, cursor_at_end: bool = True) -> None:
        """Focus this item's text field."""
        self._edit.setFocus()
        if cursor_at_end:
            self._edit.end(False)

    def enterEvent(self, event: 'QEvent') -> None:  # type: ignore[override]
        self._del_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event: 'QEvent') -> None:  # type: ignore[override]
        self._del_btn.setVisible(False)
        super().leaveEvent(event)


class _ChecklistWidget(QWidget):
    """Google Keep-style checklist with active and completed sections."""

    content_changed: pyqtSignal = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._completed_visible: bool = True
        self._focus_after_rebuild: Optional[tuple[int, bool]] = None
        self._focus_add_after_rebuild: bool = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.NoFrame)

        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll)

        self._add_field: Optional[_ItemLineEdit] = None

    def set_items(self, items: list[dict]) -> None:
        """Load items and rebuild the UI."""
        self._items = [dict(i) for i in items]
        self._rebuild()

    def get_items(self) -> list[dict]:
        """Return the current item list."""
        return [dict(i) for i in self._items]

    # ── Layout rebuild ───────────────────────────────────────────────

    def _clear_layout(self) -> None:
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
        self._layout.addWidget(self._add_field)

        # Completed section
        if completed:
            arrow = '\u25be' if self._completed_visible else '\u25b8'
            sep = QPushButton(f'{arrow}  Completed ({len(completed)})')
            sep.setFlat(True)
            sep.setStyleSheet(
                'QPushButton { text-align: left; color: #999; font-size: 12px; '
                'padding: 8px 4px 4px 4px; border: none; }'
                'QPushButton:hover { color: #ccc; }'
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

        # Restore focus
        if focus_widget is not None:
            focus_widget.focus_edit(cursor_at_end=focus_at_end)
        elif self._focus_add_after_rebuild and self._add_field is not None:
            self._add_field.setFocus()
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
        return w

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
        if self._add_field is None:
            return
        text = self._add_field.text().strip()
        if not text:
            return
        self._items.append({'text': text, 'checked': False})
        self._focus_add_after_rebuild = True
        self._rebuild()
        self.content_changed.emit()

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

        # ── Right panel: header + stacked editor ──
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Header row: title | mode combo | timestamp
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        self._title_label = QLabel('')
        self._title_label.setStyleSheet('font-weight: bold; font-size: 13px;')
        header_row.addWidget(self._title_label)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(['Text', 'Checklist'])
        self._mode_combo.setFixedWidth(100)
        self._mode_combo.setEnabled(False)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        header_row.addWidget(self._mode_combo)

        header_row.addStretch()
        self._timestamp_label = QLabel('')
        self._timestamp_label.setStyleSheet('color: #999; font-size: 11px;')
        header_row.addWidget(self._timestamp_label)
        right_layout.addLayout(header_row)

        # Stacked widget: page 0 = text, page 1 = checklist
        self._stack = QStackedWidget()

        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText('Select or create a note...')
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

    def _update_timestamp(self) -> None:
        """Update the timestamp label for the current note."""
        if not self._current_name:
            self._timestamp_label.setText('')
            return
        ts = _format_mtime(_note_path(self._current_name))
        self._timestamp_label.setText(f'Last edited: {ts}' if ts else '')

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
            self._editor.setPlainText('')
            self._editor.setEnabled(False)
            self._mode_combo.setEnabled(False)
            self._stack.setCurrentIndex(self._MODE_TEXT)
            self._title_label.setText('')
            self._timestamp_label.setText('')
            return

        name = current.text()
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
            self._editor.setPlainText(text)
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
            text = self._editor.toPlainText()
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
            self._editor.setPlainText(text)
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
            _note_path(self._current_name).unlink(missing_ok=True)
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
            text = self._editor.toPlainText()
        if text != self._saved_text:
            try:
                NOTES_DIR.mkdir(parents=True, exist_ok=True)
                _note_path(self._current_name).write_text(text, encoding='utf-8')
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
