"""Free-form notes dialog for Leap Monitor.

Supports multiple notes organized in folders under .storage/notes/.
Each note can be either plain text or a Google Keep-style checklist.
Left panel shows a searchable folder tree; right panel is the editor.
Notes auto-save on switch, close, and Cmd+S.
"""

import re
import shutil
from typing import Optional

from PyQt5 import sip
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMenu, QMessageBox, QPushButton, QSplitter,
    QStackedWidget, QStyle, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout,
    QWidget,
)
from PyQt5.QtCore import (
    QByteArray, QEvent, QPoint, QSize, QTimer, Qt,
)
from PyQt5.QtGui import (
    QColor, QCursor, QFont, QTextCharFormat, QTextCursor, QTextDocument,
    QWheelEvent,
)

from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.dialogs.notes.checklist_io import (
    _parse_checklist, _serialize_checklist,
)
from leap.monitor.dialogs.notes.checklist_widgets import (
    _ChecklistItemWidget, _ChecklistWidget, _DragGrip, _ItemLineEdit,
)
from leap.monitor.dialogs.notes.image_helpers import (
    _CHECKLIST_PLACEHOLDER_RE, _IMAGE_MARKER_RE, _ImagePreviewPopup,
    _NOTE_IMAGE_MAX_WIDTH, _all_note_image_refs, _cleanup_orphaned_images,
    _collect_image_refs, _save_note_image,
)
from leap.monitor.dialogs.notes.note_text_edit import (
    _NoteTextEdit, _setup_textedit_image_hover, _setup_textedit_url_click,
)
from leap.monitor.dialogs.notes.ordering import (
    _delete_folder_meta, _delete_order_keys, _list_folders, _load_order,
    _remove_from_order, _rename_folder_meta, _rename_in_order,
    _rename_order_keys, _save_order,
)
from leap.monitor.dialogs.notes.persistence import (
    _NOTES_META_FILE, _folder_mtime, _format_mtime, _get_note_created_at,
    _get_note_mode, _list_notes, _load_notes_meta, _migrate_old_notes_file,
    _note_path, _remove_note_meta, _rename_note_meta, _save_notes_meta,
    _set_note_mode,
)
from leap.monitor.dialogs.notes.rtl import _apply_rtl_direction, _text_is_rtl
from leap.monitor.dialogs.notes.session_picker import _SessionPickerDialog
from leap.monitor.dialogs.notes.text_helpers import (
    _ANY_URL_RE, _BOLD_END, _BOLD_START, _INLINE_FORMAT_RE, _LINK_RE,
    _URL_RE, _UrlHighlighter, _display_to_raw_pos, _find_markdown_link_at,
    _link_at_stripped_pos, _link_char_format, _strip_inline_formats,
    _strip_markdown_links, _try_open_url, _url_at_line_edit_pos,
    _url_at_pos, _url_in_text_at_col,
)
from leap.monitor.dialogs.notes.tree_widget import _NotesTreeWidget
from leap.monitor.dialogs.notes_undo import (
    BatchDeleteCmd, CreateFolderCmd, CreateNoteCmd, DeleteFolderCmd,
    DeleteNoteCmd, DuplicateFolderCmd, DuplicateNoteCmd,
    ModeSwitchCmd, MoveFolderCmd, MoveNoteCmd,
    NoteContentChangeCmd, NotesCmdContext, NotesUndoStack,
    RenameFolderCmd, RenameNoteCmd, ReorderCmd,
)
from leap.monitor.leap_sender import (
    prepend_to_leap_queue, send_to_leap_session_raw,
)
from leap.monitor.pr_tracking.config import (
    load_dialog_geometry, load_dialog_geometry_state,
    load_dialog_splitter_sizes, load_monitor_prefs, load_saved_presets,
    save_dialog_geometry, save_dialog_geometry_state,
    save_dialog_splitter_sizes, save_monitor_prefs, save_named_preset,
)
from leap.monitor.themes import current_theme
from leap.utils.constants import NOTE_IMAGES_DIR, NOTES_DIR, QUEUE_IMAGES_DIR


MAX_NOTE_NAME_LEN = 80


# ══════════════════════════════════════════════════════════════════════
#  Main dialog
# ══════════════════════════════════════════════════════════════════════

class NotesDialog(QDialog):
    """Multi-note dialog with folder hierarchy, search, and text/checklist editor."""

    _MODE_TEXT = 0
    _MODE_CHECKLIST = 1
    _ROLE_PATH = Qt.UserRole         # relative path (note name or folder path)
    _ROLE_TYPE = Qt.UserRole + 1     # 'note' or 'folder'
    _DEFAULT_SIZE = (990, 660)       # used by MonitorWindow._reset_window_size

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Notes')
        # Object name enables ID-based QSS selectors like
        # ``#leapNotesDlg QPushButton`` — specificity (ID + type = 2)
        # beats the app-level ``* { font-size: ... }`` (specificity 0)
        # and ``QPushButton { font-size: ... }`` (specificity 1).
        self.setObjectName('leapNotesDlg')
        self.resize(*self._DEFAULT_SIZE)
        saved = load_dialog_geometry('notes_dialog')
        if saved:
            self.resize(saved[0], saved[1])
        # Restore the full Qt window-state blob if we have one — this
        # carries the maximised/fullscreen flag that ``[w, h]`` alone
        # can't represent, so a window closed maximised reopens
        # maximised instead of "almost fullscreen".
        geom_state = load_dialog_geometry_state('notes_dialog')
        if geom_state:
            self.restoreGeometry(QByteArray(geom_state))
        # Position left to Qt — it auto-centers modal dialogs on the
        # parent window, matching every other dialog in the project.

        self._current_name: Optional[str] = None
        self._saved_text: str = ''
        self._switching_mode: bool = False
        self._clipboard_path: Optional[str] = None
        self._clipboard_type: Optional[str] = None
        # Font sizes — persisted separately in monitor prefs
        prefs = load_monitor_prefs()
        default_pt = current_theme().font_size_base
        self._font_size: int = prefs.get('notes_font_size', default_pt)
        self._sidebar_font_size: int = prefs.get('notes_sidebar_font_size', default_pt)
        self._buttons_font_size: int = prefs.get('notes_buttons_font_size', default_pt)
        self._zoom_target: str = 'content'  # 'content' | 'sidebar' | 'buttons'
        self._undo_stack = NotesUndoStack(limit=50)
        self._cmd_ctx = NotesCmdContext(self)
        self._pending_image_deletes: set[str] = set()
        self._undoing: bool = False

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(8, 8, 8, 8)

        self._splitter = splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet('QSplitter::handle { background: transparent; }')

        # ── Left panel: search + tree + buttons ──
        self._left_panel = left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 6, 0)
        left_layout.setSpacing(4)

        left_layout.addWidget(QLabel('Notes'))

        # Search bar
        self._search = QLineEdit()
        self._search.setPlaceholderText('Search notes...')
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        self._search.installEventFilter(self)
        left_layout.addWidget(self._search)

        # Tree widget (custom subclass handles drag-and-drop indicator + moves)
        self._tree = _NotesTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)
        t = current_theme()
        sel_color = t.accent_blue
        self._tree.setStyleSheet(
            f'QTreeWidget {{'
            f'  selection-background-color: transparent;'
            f'  selection-color: {t.text_primary};'
            f'  outline: 0;'
            f'}}'
            f'QTreeWidget::item:selected,'
            f'QTreeWidget::item:selected:active,'
            f'QTreeWidget::item:selected:!active {{'
            f'  background: transparent;'
            f'  color: {t.text_primary};'
            f'  border: 2px solid {sel_color};'
            f'  border-radius: {t.border_radius}px;'
            f'}}'
            f'QTreeWidget::branch:selected {{'
            f'  background: transparent;'
            f'}}'
        )
        self._tree.currentItemChanged.connect(self._on_item_changed)
        self._tree.item_dropped.connect(self._on_tree_drop)
        self._tree.rename_requested.connect(self._on_rename)
        self._tree.copy_requested.connect(self._on_copy)
        self._tree.paste_requested.connect(self._on_paste)
        left_layout.addWidget(self._tree, 1)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)

        new_btn = QPushButton('+ Note')
        new_btn.setToolTip('New note (Cmd+N)')
        new_btn.clicked.connect(self._on_new)

        folder_btn = QPushButton('+ Folder')
        folder_btn.setToolTip('New folder (Cmd+Shift+N)')
        folder_btn.clicked.connect(self._on_new_folder)

        rename_btn = QPushButton('Rename')
        rename_btn.setToolTip('Rename selected')
        rename_btn.clicked.connect(self._on_rename)

        delete_btn = QPushButton('Delete')
        delete_btn.setToolTip('Delete selected')
        delete_btn.clicked.connect(self._on_delete)

        btn_row.addWidget(new_btn)
        btn_row.addWidget(folder_btn)
        btn_row.addWidget(rename_btn)
        btn_row.addWidget(delete_btn)
        left_layout.addLayout(btn_row)

        left.setMinimumWidth(340)
        splitter.addWidget(left)
        splitter.setCollapsible(0, False)

        # ── Right panel: header + stacked editor ──
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 0, 0, 0)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        self._title_label = QLabel('')
        # Omit font-size so the dialog's buttons stylesheet cascades in.
        self._title_label.setStyleSheet('font-weight: bold;')
        header_row.addWidget(self._title_label)
        header_row.addStretch()
        right_layout.addLayout(header_row)

        self._dates_label = QLabel('')
        self._dates_label.setStyleSheet(f'color: {current_theme().text_secondary};')
        self._dates_label.setVisible(False)
        right_layout.addWidget(self._dates_label)

        # ── Action toolbar row ──
        toolbar_row = QHBoxLayout()
        toolbar_row.setContentsMargins(0, 0, 0, 0)
        toolbar_row.setSpacing(6)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(['Text', 'Checklist'])
        self._mode_combo.setFixedWidth(100)
        self._mode_combo.setVisible(False)
        self._mode_combo.setToolTip('Switch between plain text and checklist mode')
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        toolbar_row.addWidget(self._mode_combo)

        toolbar_row.addStretch()

        self._save_preset_btn = QPushButton('Save as Preset')
        self._save_preset_btn.setToolTip('Save note content as a reusable preset')
        self._save_preset_btn.setVisible(False)
        self._save_preset_btn.clicked.connect(self._on_save_as_preset)
        toolbar_row.addWidget(self._save_preset_btn)

        self._run_session_btn = QPushButton('Run in Session')
        self._run_session_btn.setToolTip('Send note content to a running session')
        self._run_session_btn.setVisible(False)
        self._run_session_btn.clicked.connect(self._on_run_in_session)
        toolbar_row.addWidget(self._run_session_btn)

        right_layout.addLayout(toolbar_row)

        self._stack = QStackedWidget()

        self._editor = _NoteTextEdit()
        self._editor.setPlaceholderText(
            'Select or create a note... (paste images with Cmd+V)')
        self._editor.setEnabled(False)
        self._editor.setTabChangesFocus(False)
        self._stack.addWidget(self._editor)

        self._checklist = _ChecklistWidget()
        self._checklist.content_changed.connect(self._on_checklist_changed)
        self._checklist.set_undo_stack(self._undo_stack, self._cmd_ctx)
        self._stack.addWidget(self._checklist)

        right_layout.addWidget(self._stack, 1)

        # Transparent spacer shown in place of _stack when a folder is
        # selected — gives the layout a stretch-1 anchor so the title/dates
        # stay at the top, without rendering any visible editor box.
        self._folder_spacer = QWidget()
        self._folder_spacer.setVisible(False)
        right_layout.addWidget(self._folder_spacer, 1)

        # ── In-note find bar (Cmd+F) ──
        self._find_bar = self._build_find_bar()
        right_layout.addWidget(self._find_bar)

        # Apply saved font sizes + intercept Cmd+scroll on all viewports.
        # Note: buttons size is re-applied AFTER the bottom hint is built
        # below — _apply_buttons_font_size explicitly reaches the hint,
        # and that widget doesn't exist yet here.
        self._apply_font_size()
        self._apply_sidebar_font_size()
        self._apply_buttons_font_size()
        self._editor.viewport().installEventFilter(self)
        self._checklist._scroll.viewport().installEventFilter(self)
        self._tree.viewport().installEventFilter(self)
        # Filter on the tree itself (not just viewport) so we can catch
        # Delete/Backspace keys — QAbstractItemView sometimes consumes
        # them before they reach the dialog's keyPressEvent.
        self._tree.installEventFilter(self)

        right.setMinimumWidth(375)
        splitter.addWidget(right)
        splitter.setCollapsible(1, False)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        # Restore the user's last drag position for the sidebar/editor
        # split.  When no value is stored, the stretch factors above
        # supply the default 1:3 ratio.
        saved_sizes = load_dialog_splitter_sizes('notes_dialog_main')
        if saved_sizes and len(saved_sizes) == 2:
            splitter.setSizes(saved_sizes)

        root_layout.addWidget(splitter, 1)

        # Bottom bar
        bottom_row = QHBoxLayout()
        hint = QLabel(
            'Cmd+N: New note  |  Cmd+Shift+N: New folder'
            '  |  Cmd+F: Find in note  |  Cmd+Shift+F: Search notes'
            '  |  Cmd+K: Insert link  |  Cmd+B: Bold'
            '  |  Cmd+/\u2212/0/Scroll: Zoom'
            '  |  Cmd+Z/Shift+Z: Undo/Redo'
            '  |  Delete/\u232b: Delete  |  Right-click: More')
        hint.setStyleSheet(
            f'color: {current_theme().text_muted};')
        self._bottom_hint = hint
        bottom_row.addWidget(hint)
        bottom_row.addStretch()
        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.close)
        bottom_row.addWidget(close_btn)
        root_layout.addLayout(bottom_row)

        # Re-apply buttons font size now that the bottom hint exists —
        # _apply_buttons_font_size needs the hint widget to force its font.
        self._apply_buttons_font_size()

        # Populate and select the last-open note (or first note as fallback)
        self._refresh_tree()
        last_note = _load_notes_meta().get('_last_note', '')
        target = None
        if last_note:
            target = self._find_tree_item(last_note, 'note')
        if target is None:
            target = self._find_first_note(self._tree.invisibleRootItem())
        if target:
            self._tree.setCurrentItem(target)

    # ── Tree helpers ────────────────────────────────────────────────

    def _find_first_note(
        self, parent: QTreeWidgetItem,
    ) -> Optional[QTreeWidgetItem]:
        """Return the first note item in depth-first order."""
        for i in range(parent.childCount()):
            child = parent.child(i)
            if child.data(0, self._ROLE_TYPE) == 'note':
                return child
            found = self._find_first_note(child)
            if found:
                return found
        return None

    def _find_tree_item(
        self, path: str, item_type: str,
        parent: Optional[QTreeWidgetItem] = None,
    ) -> Optional[QTreeWidgetItem]:
        """Find a tree item by its path and type."""
        if parent is None:
            parent = self._tree.invisibleRootItem()
        for i in range(parent.childCount()):
            child = parent.child(i)
            if (child.data(0, self._ROLE_PATH) == path
                    and child.data(0, self._ROLE_TYPE) == item_type):
                return child
            found = self._find_tree_item(path, item_type, child)
            if found:
                return found
        return None

    def _current_folder(self) -> str:
        """Return the folder path for the currently selected item ('' for root)."""
        item = self._tree.currentItem()
        if item is None:
            return ''
        if item.data(0, self._ROLE_TYPE) == 'folder':
            return item.data(0, self._ROLE_PATH)
        name = item.data(0, self._ROLE_PATH) or ''
        if '/' in name:
            return name.rsplit('/', 1)[0]
        return ''

    def _current_mode(self) -> int:
        return self._stack.currentIndex()

    # ── Tree management ─────────────────────────────────────────────

    def _refresh_tree(self, select_name: Optional[str] = None,
                      select_type: str = 'note') -> None:
        """Rebuild the tree from disk, respecting stored child ordering."""
        self._tree.blockSignals(True)
        self._tree.clear()

        folder_icon = self.style().standardIcon(QStyle.SP_DirIcon)
        all_order = _load_order()

        # Collect children per parent folder:
        #   parent_path -> [(type, full_path, leaf_name), ...]
        # Folders appear in alphabetical order, notes in mtime order.
        children: dict[str, list[tuple[str, str, str]]] = {}

        for folder_path in _list_folders():
            parent = folder_path.rsplit('/', 1)[0] if '/' in folder_path else ''
            leaf = folder_path.rsplit('/', 1)[-1] if '/' in folder_path else folder_path
            children.setdefault(parent, []).append(('folder', folder_path, leaf))

        for name in _list_notes():
            parent = name.rsplit('/', 1)[0] if '/' in name else ''
            leaf = name.rsplit('/', 1)[-1] if '/' in name else name
            children.setdefault(parent, []).append(('note', name, leaf))

        # Sort each parent's children by stored order (stable sort:
        # items in stored order come first in that order; unstored items
        # keep their default position — folders alpha, then notes mtime).
        for parent_path, items in children.items():
            stored = all_order.get(parent_path, [])
            if stored:
                order_map = {n: i for i, n in enumerate(stored)}
                max_idx = len(stored)
                items.sort(key=lambda x: order_map.get(x[2], max_idx))

        # Build tree recursively
        def _build(parent_item: QTreeWidgetItem, parent_path: str) -> None:
            for typ, full_path, leaf in children.get(parent_path, []):
                ti = QTreeWidgetItem(parent_item)
                ti.setText(0, leaf)
                ti.setData(0, self._ROLE_PATH, full_path)
                ti.setData(0, self._ROLE_TYPE, typ)
                if typ == 'folder':
                    ti.setIcon(0, folder_icon)
                    ti.setExpanded(True)
                    _build(ti, full_path)
                else:
                    ti.setFlags(
                        (ti.flags() | Qt.ItemIsDragEnabled)
                        & ~Qt.ItemIsDropEnabled)
                    ts = _format_mtime(_note_path(full_path))
                    if ts:
                        ti.setToolTip(0, f'{full_path}\n{ts}')

        _build(self._tree.invisibleRootItem(), '')

        # Restore search filter if active
        search_text = self._search.text().strip().lower()
        if search_text:
            self._filter_tree(self._tree.invisibleRootItem(), search_text)

        # Select requested item
        if select_name:
            target = self._find_tree_item(select_name, select_type)
            if target:
                self._tree.setCurrentItem(target)

        self._tree.blockSignals(False)

    def _refresh_dates_label(self) -> None:
        """Populate or hide the created/modified label for the current note."""
        if not self._current_name:
            self._dates_label.setVisible(False)
            return
        path = _note_path(self._current_name)
        created = _get_note_created_at(self._current_name)
        modified = _format_mtime(path)
        parts = []
        if created:
            parts.append(f'Created: {created}')
        if modified:
            parts.append(f'Modified: {modified}')
        if parts:
            self._dates_label.setText('  ·  '.join(parts))
            self._dates_label.setVisible(True)
        else:
            self._dates_label.setVisible(False)

    def _refresh_folder_dates_label(self, folder_path: str) -> None:
        """Populate or hide the created/modified label for a folder."""
        created = _get_note_created_at(folder_path)
        modified = _folder_mtime(folder_path)
        parts = []
        if created:
            parts.append(f'Created: {created}')
        if modified:
            parts.append(f'Modified: {modified}')
        if parts:
            self._dates_label.setText('  ·  '.join(parts))
            self._dates_label.setVisible(True)
        else:
            self._dates_label.setVisible(False)

    def _update_timestamp(self) -> None:
        """Update the tooltip and dates label for the current note."""
        if not self._current_name:
            return
        item = self._find_tree_item(self._current_name, 'note')
        if item:
            ts = _format_mtime(_note_path(self._current_name))
            if ts:
                item.setToolTip(0, f'{self._current_name}\n{ts}')
        self._refresh_dates_label()

    # ── Search ──────────────────────────────────────────────────────

    def _on_search(self, text: str) -> None:
        """Filter tree items based on search text (matches name and content)."""
        query = text.strip().lower()
        if not query:
            self._show_all_items(self._tree.invisibleRootItem())
        else:
            self._filter_tree(self._tree.invisibleRootItem(), query)

    def _filter_tree(self, parent: QTreeWidgetItem, query: str) -> bool:
        """Hide non-matching items. Returns True if any child is visible."""
        any_visible = False
        for i in range(parent.childCount()):
            child = parent.child(i)
            if child.data(0, self._ROLE_TYPE) == 'folder':
                children_visible = self._filter_tree(child, query)
                child.setHidden(not children_visible)
                if children_visible:
                    child.setExpanded(True)
                    any_visible = True
            else:
                # Match against note name and file content
                name = (child.data(0, self._ROLE_PATH) or '').lower()
                match = query in name
                if not match:
                    path = _note_path(child.data(0, self._ROLE_PATH) or '')
                    try:
                        if path.exists():
                            match = query in path.read_text(
                                encoding='utf-8').lower()
                    except OSError:
                        pass
                child.setHidden(not match)
                if match:
                    any_visible = True
        return any_visible

    def _show_all_items(self, parent: QTreeWidgetItem) -> None:
        """Unhide all items in the tree."""
        for i in range(parent.childCount()):
            child = parent.child(i)
            child.setHidden(False)
            if child.data(0, self._ROLE_TYPE) == 'folder':
                child.setExpanded(True)
                self._show_all_items(child)

    # ── Note selection ──────────────────────────────────────────────

    def _on_item_changed(
        self, current: Optional[QTreeWidgetItem],
        previous: Optional[QTreeWidgetItem],
    ) -> None:
        """Save the previous note, then load the newly selected one."""
        # Snapshot content change before switching (skip during undo/redo
        # to avoid polluting the stack with spurious content commands).
        if self._current_name and not self._undoing:
            try:
                if self._current_mode() == self._MODE_CHECKLIST:
                    live_text = _serialize_checklist(self._checklist.get_items())
                else:
                    live_text = self._editor.get_note_content()
            except RuntimeError:
                live_text = self._saved_text
            if live_text != self._saved_text:
                # Drop any trailing checklist commands for this note —
                # the content change captures their net effect.
                self._undo_stack.drop_trailing_checklist_cmds(
                    self._current_name)
                mode = _get_note_mode(self._current_name)
                cmd = NoteContentChangeCmd(
                    note_name=self._current_name,
                    old_text=self._saved_text, new_text=live_text, mode=mode,
                )
                self._undo_stack.record(cmd)
        self._save_current()
        if current is None:
            self._current_name = None
            self._saved_text = ''
            self._editor.clear()
            self._editor.setEnabled(False)
            self._editor.setPlaceholderText(
                'Select or create a note... (paste images with Cmd+V)')
            self._mode_combo.setVisible(False)
            self._stack.setCurrentIndex(self._MODE_TEXT)
            self._stack.setVisible(True)
            self._folder_spacer.setVisible(False)
            self._title_label.setText('')
            self._dates_label.setVisible(False)
            self._update_action_visibility(False)
            self._find_bar.setVisible(False)
            return

        if current.data(0, self._ROLE_TYPE) == 'folder':
            # Folder selected — hide the editor stack and show a transparent
            # spacer in its place so the title/dates stay pinned to the top.
            self._current_name = None
            self._saved_text = ''
            self._editor.clear()
            self._editor.setEnabled(False)
            self._mode_combo.setVisible(False)
            self._stack.setVisible(False)
            self._folder_spacer.setVisible(True)
            self._title_label.setText(current.text(0))
            self._refresh_folder_dates_label(current.data(0, self._ROLE_PATH))
            self._update_action_visibility(False)
            self._find_bar.setVisible(False)
            return

        name = current.data(0, self._ROLE_PATH)
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
            self._find_bar.setVisible(False)
        else:
            self._mode_combo.setCurrentIndex(self._MODE_TEXT)
            self._editor.set_note_content(text)
            self._editor.setEnabled(True)
            self._stack.setCurrentIndex(self._MODE_TEXT)
        self._switching_mode = False

        self._mode_combo.setVisible(True)
        self._stack.setVisible(True)
        self._folder_spacer.setVisible(False)
        display = name.rsplit('/', 1)[-1] if '/' in name else name
        self._title_label.setText(display)
        self._update_timestamp()
        self._update_action_visibility(True)

    # ── Mode switching ──────────────────────────────────────────────

    def _on_mode_changed(self, index: int) -> None:
        if self._switching_mode or not self._current_name:
            return

        # Flush any open checklist popup so its unsaved text is
        # captured before we convert to the other mode.
        self._checklist._flush_popups()

        old_mode = 'text' if index == self._MODE_CHECKLIST else 'checklist'
        new_mode = 'checklist' if index == self._MODE_CHECKLIST else 'text'

        if index == self._MODE_CHECKLIST:
            old_content = self._editor.get_note_content()
            self._checklist._pasted_images |= self._editor.take_pasted_images()
            items = _parse_checklist(old_content) if old_content.strip() else []
            new_content = _serialize_checklist(items)
        else:
            items = self._checklist.get_items()
            self._editor._pasted_images |= self._checklist.take_pasted_images()
            old_content = _serialize_checklist(self._checklist.get_items())
            # Preserve each item's bold flag (item['text'] already
            # contains any markdown links) when flattening to text.
            lines = []
            for item in items:
                text = item['text']
                if not text:
                    continue
                if item.get('bold'):
                    text = f'{_BOLD_START}{text}{_BOLD_END}'
                lines.append(text)
            new_content = '\n'.join(lines)

        cmd = ModeSwitchCmd(
            note_name=self._current_name, old_mode=old_mode, new_mode=new_mode,
            old_content=old_content, new_content=new_content,
        )
        self._undo_stack.record(cmd)

        # Apply the mode switch
        if index == self._MODE_CHECKLIST:
            self._checklist.set_items(items)
            self._stack.setCurrentIndex(self._MODE_CHECKLIST)
            _set_note_mode(self._current_name, 'checklist')
            self._save_current()
            self._find_bar.setVisible(False)
        else:
            self._editor.set_note_content(new_content)
            self._editor.setEnabled(True)
            self._stack.setCurrentIndex(self._MODE_TEXT)
            _set_note_mode(self._current_name, 'text')
            self._save_current()
        self._update_action_visibility(self._current_name is not None)

    def _on_checklist_changed(self) -> None:
        """No-op signal receiver; _save_current reads live widget state."""
        pass

    # ── Context menu ────────────────────────────────────────────────

    def _show_context_menu(self, pos: QPoint) -> None:
        """Show right-click context menu on the tree."""
        item = self._tree.itemAt(pos)
        menu = QMenu(self)

        menu.addAction('New Note', self._on_new)
        menu.addAction('New Folder', self._on_new_folder)

        if item:
            menu.addSeparator()
            item_type = item.data(0, self._ROLE_TYPE)

            if item_type == 'note':
                menu.addAction('Rename', self._on_rename)
                menu.addAction('Copy', lambda checked=False, i=item: self._on_copy(i))
                # "Move to" submenu
                note_name = item.data(0, self._ROLE_PATH) or ''
                current_note_folder = (
                    note_name.rsplit('/', 1)[0] if '/' in note_name else '')
                move_menu = menu.addMenu('Move to...')
                if current_note_folder:
                    move_menu.addAction(
                        'Root',
                        lambda: self._move_note(note_name, ''))
                for folder in _list_folders():
                    if folder != current_note_folder:
                        move_menu.addAction(
                            folder,
                            lambda f=folder: self._move_note(note_name, f))
                if move_menu.isEmpty():
                    move_menu.setEnabled(False)
            else:
                menu.addAction('Rename Folder', self._on_rename)
                menu.addAction('Copy Folder', lambda checked=False, i=item: self._on_copy(i))

            menu.addAction('Delete', self._on_delete)

        if self._clipboard_path is not None:
            menu.addSeparator()
            menu.addAction('Paste', self._on_paste)

        menu.exec_(self._tree.viewport().mapToGlobal(pos))

    def _move_note(self, note_name: str, target_folder: str,
                   target_position: Optional[int] = None) -> bool:
        """Move a note to a different folder. Returns True on success."""
        leaf = note_name.rsplit('/', 1)[-1] if '/' in note_name else note_name
        new_name = f'{target_folder}/{leaf}' if target_folder else leaf

        if new_name == note_name:
            return False
        if _note_path(new_name).exists():
            QMessageBox.warning(
                self, 'Already Exists',
                f"A note named '{leaf}' already exists in that location.")
            return False

        self._save_current()
        src_folder = note_name.rsplit('/', 1)[0] if '/' in note_name else ''
        order = _load_order().get(src_folder, [])
        pos = order.index(leaf) if leaf in order else len(order)
        cmd = MoveNoteCmd(old_name=note_name, new_name=new_name, old_folder=src_folder,
                          new_folder=target_folder, old_order_position=(src_folder, pos),
                          new_order_position=target_position)
        self._undo_stack.push(cmd, self._cmd_ctx)
        return True

    def _move_folder(self, folder_path: str, target_folder: str,
                     target_position: Optional[int] = None) -> bool:
        """Move a folder into another folder (or root). Returns True on success."""
        leaf = folder_path.rsplit('/', 1)[-1] if '/' in folder_path else folder_path
        new_path = f'{target_folder}/{leaf}' if target_folder else leaf

        if new_path == folder_path:
            return False
        # Prevent moving a folder into itself or its own descendant
        if new_path.startswith(folder_path + '/'):
            return False
        dest = NOTES_DIR / new_path
        if dest.exists():
            QMessageBox.warning(
                self, 'Already Exists',
                f"A folder named '{leaf}' already exists in that location.")
            return False

        self._save_current()
        src_parent = folder_path.rsplit('/', 1)[0] if '/' in folder_path else ''
        order = _load_order().get(src_parent, [])
        pos = order.index(leaf) if leaf in order else len(order)
        cmd = MoveFolderCmd(old_path=folder_path, new_path=new_path, old_parent=src_parent,
                            new_parent=target_folder, old_order_position=(src_parent, pos),
                            new_order_position=target_position)
        self._undo_stack.push(cmd, self._cmd_ctx)
        return True

    def _on_tree_drop(self, src_path: str, src_type: str,
                      target_folder: str, before_path: str) -> None:
        """Handle a drag-and-drop in the tree."""
        src_folder = src_path.rsplit('/', 1)[0] if '/' in src_path else ''

        if src_folder == target_folder:
            # Reorder within the same folder
            self._reorder_in_folder(
                src_path, src_type, target_folder, before_path)
        else:
            # Move to a different folder — compute target position from
            # drop location so the move command places it correctly.
            before_leaf = (before_path.rsplit('/', 1)[-1]
                           if before_path else '')
            target_order = self._effective_order(target_folder)
            if before_leaf and before_leaf in target_order:
                new_pos: Optional[int] = target_order.index(before_leaf)
            else:
                new_pos = None  # append
            if src_type == 'note':
                self._move_note(src_path, target_folder, new_pos)
            elif src_type == 'folder':
                self._move_folder(src_path, target_folder, new_pos)
        # macOS deactivates the window during native drag — reactivate so
        # focus and cursors work immediately after the drop.
        QApplication.setActiveWindow(self)

    def _effective_order(self, folder: str) -> list[str]:
        """Return the effective leaf-name order for *folder*'s children."""
        stored = _load_order().get(folder, [])
        # Collect actual children on disk
        items: list[tuple[str, str, str]] = []
        for f in _list_folders():
            p = f.rsplit('/', 1)[0] if '/' in f else ''
            if p == folder:
                items.append(('folder', f, f.rsplit('/', 1)[-1] if '/' in f else f))
        for n in _list_notes():
            p = n.rsplit('/', 1)[0] if '/' in n else ''
            if p == folder:
                items.append(('note', n, n.rsplit('/', 1)[-1] if '/' in n else n))
        if stored:
            order_map = {n: i for i, n in enumerate(stored)}
            max_idx = len(stored)
            items.sort(key=lambda x: order_map.get(x[2], max_idx))
        return [x[2] for x in items]

    def _reorder_in_folder(self, src_path: str, src_type: str,
                           folder: str, before_path: str) -> None:
        """Reorder an item within its current folder."""
        src_leaf = (src_path.rsplit('/', 1)[-1]
                    if '/' in src_path else src_path)
        before_leaf = (before_path.rsplit('/', 1)[-1]
                       if before_path else '')

        old_order = list(self._effective_order(folder))
        order = list(old_order)
        if src_leaf not in order:
            return
        order.remove(src_leaf)
        if before_leaf and before_leaf in order:
            order.insert(order.index(before_leaf), src_leaf)
        else:
            order.append(src_leaf)
        if order == old_order:
            return

        cmd = ReorderCmd(folder=folder, old_order=old_order, new_order=order)
        self._undo_stack.push(cmd, self._cmd_ctx)

    def _insert_at_position(self, folder: str, leaf: str,
                            before_path: str) -> None:
        """Insert *leaf* into *folder*'s stored order at the drop position."""
        before_leaf = (before_path.rsplit('/', 1)[-1]
                       if before_path else '')
        order = self._effective_order(folder)
        if leaf in order:
            order.remove(leaf)
        if before_leaf and before_leaf in order:
            order.insert(order.index(before_leaf), leaf)
        else:
            order.append(leaf)
        all_order = _load_order()
        all_order[folder] = order
        _save_order(all_order)

    # ── CRUD ────────────────────────────────────────────────────────

    def _on_new(self) -> None:
        """Create a new note in the selected folder."""
        folder = self._current_folder()
        prev = ''
        while True:
            name, ok = QInputDialog.getText(
                self, 'New Note', 'Note name:', text=prev)
            if not ok or not name.strip():
                return
            name = name.strip()
            prev = name
            if len(name) > MAX_NOTE_NAME_LEN:
                QMessageBox.warning(
                    self, 'Name Too Long',
                    f'Note name must be {MAX_NOTE_NAME_LEN} characters or fewer.')
                continue
            if '/' in name or '\\' in name:
                QMessageBox.warning(
                    self, 'Invalid Name',
                    'Note name cannot contain slashes.')
                continue
            full_name = f'{folder}/{name}' if folder else name
            if _note_path(full_name).exists():
                QMessageBox.warning(
                    self, 'Already Exists',
                    f"A note named '{name}' already exists in this location.")
                continue
            break

        self._save_current()
        cmd = CreateNoteCmd(name=full_name, folder=folder)
        self._undo_stack.push(cmd, self._cmd_ctx)
        if self._current_mode() == self._MODE_TEXT:
            self._editor.setFocus()

    def _on_new_folder(self) -> None:
        """Create a new folder inside the selected folder."""
        parent_folder = self._current_folder()
        prev = ''
        while True:
            name, ok = QInputDialog.getText(
                self, 'New Folder', 'Folder name:', text=prev)
            if not ok or not name.strip():
                return
            name = name.strip()
            prev = name
            if len(name) > MAX_NOTE_NAME_LEN:
                QMessageBox.warning(
                    self, 'Name Too Long',
                    f'Folder name must be {MAX_NOTE_NAME_LEN} characters or fewer.')
                continue
            if '/' in name or '\\' in name:
                QMessageBox.warning(
                    self, 'Invalid Name',
                    'Folder name cannot contain slashes.')
                continue
            full_path = f'{parent_folder}/{name}' if parent_folder else name
            if (NOTES_DIR / full_path).exists():
                QMessageBox.warning(
                    self, 'Already Exists',
                    f"A folder named '{name}' already exists here.")
                continue
            break

        cmd = CreateFolderCmd(folder_path=full_path)
        self._undo_stack.push(cmd, self._cmd_ctx)

    def _on_rename(self) -> None:
        """Rename the selected note or folder."""
        item = self._tree.currentItem()
        if not item:
            return
        item_type = item.data(0, self._ROLE_TYPE)
        old_path = item.data(0, self._ROLE_PATH)
        old_display = item.text(0)

        prev = old_display
        while True:
            label = 'New name:' if item_type == 'note' else 'New folder name:'
            title = 'Rename Note' if item_type == 'note' else 'Rename Folder'
            new_name, ok = QInputDialog.getText(
                self, title, label, text=prev)
            if not ok or not new_name.strip():
                return
            new_name = new_name.strip()
            prev = new_name
            if new_name == old_display:
                return
            if len(new_name) > MAX_NOTE_NAME_LEN:
                QMessageBox.warning(
                    self, 'Name Too Long',
                    f'Name must be {MAX_NOTE_NAME_LEN} characters or fewer.')
                continue
            if '/' in new_name or '\\' in new_name:
                QMessageBox.warning(
                    self, 'Invalid Name', 'Name cannot contain slashes.')
                continue

            # Compute new full path
            if '/' in old_path:
                parent = old_path.rsplit('/', 1)[0]
                new_full = f'{parent}/{new_name}'
            else:
                new_full = new_name

            # On case-insensitive filesystems (macOS APFS/HFS+, Windows
            # NTFS) ``.exists()`` matches the current entry by case —
            # skip the collision check when the only difference between
            # the new and old paths is letter case, so a rename like
            # ``notes`` → ``Notes`` is allowed.  The OS-level rename
            # updates the case-preserving name in the directory.
            case_only_change = (new_full != old_path
                                and new_full.lower() == old_path.lower())
            if item_type == 'note':
                if not case_only_change and _note_path(new_full).exists():
                    QMessageBox.warning(
                        self, 'Already Exists',
                        f"A note named '{new_name}' already exists.")
                    continue
            else:
                if not case_only_change and (NOTES_DIR / new_full).exists():
                    QMessageBox.warning(
                        self, 'Already Exists',
                        f"A folder named '{new_name}' already exists.")
                    continue
            break

        # Determine parent folder for order update
        parent_folder = old_path.rsplit('/', 1)[0] if '/' in old_path else ''

        if item_type == 'note':
            self._save_current()
            cmd = RenameNoteCmd(old_name=old_path, new_name=new_full, parent_folder=parent_folder,
                                old_leaf=old_display, new_leaf=new_name)
            self._undo_stack.push(cmd, self._cmd_ctx)
        else:
            cmd = RenameFolderCmd(old_path=old_path, new_path=new_full, parent_folder=parent_folder,
                                  old_leaf=old_display, new_leaf=new_name)
            self._undo_stack.push(cmd, self._cmd_ctx)

    def _on_delete(self) -> None:
        """Delete the selected note(s) or folder(s)."""
        selected = self._tree.selectedItems()
        if not selected:
            return

        note_names: list[str] = []
        folder_paths: list[str] = []
        for sel_item in selected:
            path = sel_item.data(0, self._ROLE_PATH)
            if not path:
                continue
            if sel_item.data(0, self._ROLE_TYPE) == 'folder':
                folder_paths.append(path)
            else:
                note_names.append(path)

        if not note_names and not folder_paths:
            return

        # Build confirmation message
        parts: list[str] = []
        if note_names:
            if len(note_names) == 1:
                leaf = note_names[0].rsplit('/', 1)[-1]
                parts.append(f"note '{leaf}'")
            else:
                parts.append(f'{len(note_names)} notes')
        if folder_paths:
            if len(folder_paths) == 1:
                parts.append(
                    f"folder '{folder_paths[0]}' and all its contents")
            else:
                parts.append(
                    f'{len(folder_paths)} folders and all their contents')

        reply = QMessageBox.question(
            self, 'Delete', f'Delete {" and ".join(parts)}?',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes)
        if reply != QMessageBox.Yes:
            return

        self._save_current()

        # Build undo commands for each item
        commands: list = []
        meta = _load_notes_meta()
        order = _load_order()

        # Folder commands
        for fp in folder_paths:
            # Snapshot all notes inside this folder
            folder_notes: dict[str, str] = {}
            folder_meta: dict[str, dict] = {}
            folder_image_refs: set[str] = set()
            prefix = fp + '/'
            for n in _list_notes():
                if n.startswith(prefix):
                    # Get content: use live editor if this is the current note
                    if self._current_name and self._current_name == n:
                        if self._current_mode() == self._MODE_CHECKLIST:
                            content = _serialize_checklist(self._checklist.get_items())
                        else:
                            content = self._editor.get_note_content()
                        folder_image_refs |= (self._editor.take_pasted_images()
                                              | self._checklist.take_pasted_images())
                    else:
                        try:
                            content = _note_path(n).read_text(encoding='utf-8')
                        except OSError:
                            content = ''
                    folder_notes[n] = content
                    folder_image_refs |= _collect_image_refs(content)
                    if n in meta:
                        folder_meta[n] = dict(meta[n])
            # Snapshot order entries for this folder and subfolders
            folder_order: dict[str, list[str]] = {}
            for k, v in order.items():
                if k == fp or k.startswith(prefix):
                    folder_order[k] = list(v)
            # Snapshot subfolder paths
            subfolder_paths = [f for f in _list_folders()
                               if f.startswith(prefix)]
            # Include folder and subfolder own metadata (e.g. created_at)
            for fpath in [fp] + subfolder_paths:
                if fpath in meta:
                    folder_meta[fpath] = dict(meta[fpath])
            # Parent order position
            parent = fp.rsplit('/', 1)[0] if '/' in fp else ''
            leaf = fp.rsplit('/', 1)[-1] if '/' in fp else fp
            parent_lst = order.get(parent, [])
            pos = parent_lst.index(leaf) if leaf in parent_lst else len(parent_lst)
            commands.append(DeleteFolderCmd(
                folder_path=fp, notes=folder_notes,
                metadata_entries=folder_meta, order_entries=folder_order,
                subfolder_paths=subfolder_paths,
                parent_order_position=(parent, pos),
                image_refs=folder_image_refs))

        # Note commands (standalone, not inside any deleted folder)
        deleted_folder_prefixes = [fp + '/' for fp in folder_paths]
        for name in note_names:
            if any(name.startswith(pfx) for pfx in deleted_folder_prefixes):
                continue  # already handled by a folder command
            # Get content
            if self._current_name and self._current_name == name:
                if self._current_mode() == self._MODE_CHECKLIST:
                    content = _serialize_checklist(self._checklist.get_items())
                else:
                    content = self._editor.get_note_content()
                image_refs = (self._editor.take_pasted_images()
                              | self._checklist.take_pasted_images())
                image_refs |= _collect_image_refs(content)
            else:
                try:
                    content = _note_path(name).read_text(encoding='utf-8')
                except OSError:
                    content = ''
                image_refs = _collect_image_refs(content)
            note_meta = dict(meta[name]) if name in meta else {}
            parent = name.rsplit('/', 1)[0] if '/' in name else ''
            leaf = name.rsplit('/', 1)[-1] if '/' in name else name
            parent_lst = order.get(parent, [])
            pos = parent_lst.index(leaf) if leaf in parent_lst else len(parent_lst)
            commands.append(DeleteNoteCmd(
                name=name, content=content, metadata=note_meta,
                order_position=(parent, pos), image_refs=image_refs))

        # Push as batch or single command; suppress content snapshots
        # during batch delete to avoid recording spurious changes from
        # intermediate _on_item_changed calls.
        self._undoing = True
        try:
            if len(commands) == 1:
                self._undo_stack.push(commands[0], self._cmd_ctx)
            elif commands:
                batch = BatchDeleteCmd(commands, f'Delete {" and ".join(parts)}')
                self._undo_stack.push(batch, self._cmd_ctx)
        finally:
            self._undoing = False

    def _on_copy(self, item: Optional[QTreeWidgetItem] = None) -> None:
        """Copy *item* (or the current tree item) to the internal clipboard."""
        if item is None:
            item = self._tree.currentItem()
        if not item:
            return
        self._clipboard_path = item.data(0, self._ROLE_PATH)
        self._clipboard_type = item.data(0, self._ROLE_TYPE)

    def _unique_leaf(self, folder: str, leaf: str, is_folder: bool) -> str:
        """Return a unique leaf name inside *folder*, appending ' copy' or ' copy N'."""
        def exists(name: str) -> bool:
            full = f'{folder}/{name}' if folder else name
            if is_folder:
                return (NOTES_DIR / full).exists()
            return _note_path(full).exists()

        base = leaf[:MAX_NOTE_NAME_LEN - 5]  # leave room for ' copy'
        if not exists(leaf):
            return leaf
        candidate = f'{base} copy'
        if not exists(candidate):
            return candidate
        n = 2
        while exists(f'{base} copy {n}'):
            n += 1
        return f'{base} copy {n}'

    def _on_paste(self) -> None:
        """Paste the copied note or folder into the current target folder."""
        if self._clipboard_path is None or self._clipboard_type is None:
            return

        src = self._clipboard_path
        src_leaf = src.rsplit('/', 1)[-1] if '/' in src else src
        if self._clipboard_type == 'folder':
            target_folder = src.rsplit('/', 1)[0] if '/' in src else ''
        else:
            target_folder = self._current_folder()
        new_leaf = self._unique_leaf(target_folder, src_leaf,
                                     is_folder=self._clipboard_type == 'folder')
        new_full = f'{target_folder}/{new_leaf}' if target_folder else new_leaf

        self._save_current()

        if self._clipboard_type == 'note':
            try:
                content = _note_path(src).read_text(encoding='utf-8')
            except OSError:
                content = ''
            meta = _load_notes_meta()
            note_meta = dict(meta[src]) if src in meta else {}
            cmd = DuplicateNoteCmd(src_name=src, new_name=new_full, content=content,
                                   metadata=note_meta, folder=target_folder)
            self._undo_stack.push(cmd, self._cmd_ctx)
        else:
            # Collect all notes under the folder
            prefix = src + '/'
            folder_notes: dict[str, str] = {}
            folder_meta_entries: dict[str, dict] = {}
            meta = _load_notes_meta()
            for n in _list_notes():
                if n.startswith(prefix):
                    suffix = n[len(src):]  # e.g. '/sub/note'
                    new_note_name = new_full + suffix
                    try:
                        content = _note_path(n).read_text(encoding='utf-8')
                    except OSError:
                        content = ''
                    folder_notes[new_note_name] = content
                    if n in meta:
                        folder_meta_entries[new_note_name] = dict(meta[n])
            # Collect subfolders
            subfolder_paths = []
            for sf in _list_folders():
                if sf.startswith(prefix):
                    subfolder_paths.append(new_full + sf[len(src):])
            # Remap order entries
            order = _load_order()
            new_order_entries: dict[str, list[str]] = {}
            for k, v in order.items():
                if k == src or k.startswith(prefix):
                    new_key = new_full + k[len(src):]
                    new_order_entries[new_key] = list(v)
            cmd = DuplicateFolderCmd(
                src_path=src, new_path=new_full,
                notes=folder_notes, metadata_entries=folder_meta_entries,
                order_entries=new_order_entries, subfolder_paths=subfolder_paths,
                parent_folder=target_folder)
            self._undo_stack.push(cmd, self._cmd_ctx)

    # ── Action toolbar helpers ─────────────────────────────────────

    def _update_action_visibility(self, note_selected: bool) -> None:
        """Show or hide the action buttons and include-completed checkbox."""
        self._save_preset_btn.setVisible(note_selected)
        self._run_session_btn.setVisible(note_selected)
    @staticmethod
    def _resolve_note_images(text: str) -> str:
        """Convert ``![image](hash.png)`` markers to ``@/abs/path`` refs.

        Images are **copied** from ``note_images/`` to ``queue_images/`` so
        that presets and queue messages own their own copy.  This ensures
        deleting a note never breaks image references in presets or queues.
        """
        def _replace(m: re.Match) -> str:
            filename = m.group(1)
            src = NOTE_IMAGES_DIR / filename
            dst = QUEUE_IMAGES_DIR / filename
            if src.is_file() and not dst.exists():
                QUEUE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))
            # Always point to queue_images — the copy is the authoritative
            # reference for presets/queue messages.
            return '@' + str(QUEUE_IMAGES_DIR / filename)
        return _IMAGE_MARKER_RE.sub(_replace, text)

    def _get_note_messages(self, include_completed: bool = False) -> list[str]:
        """Extract sendable messages from the current note.

        For text notes: returns a single-element list with the full text.
        For checklists: returns one message per qualifying item in original
        order. When *include_completed* is False, checked items are skipped.
        Image markers are converted to ``@/path`` references (same format
        used by the preset system).
        """
        if not self._current_name:
            return []
        if self._current_mode() == self._MODE_CHECKLIST:
            items = self._checklist.get_items()
            messages: list[str] = []
            for item in items:
                text = item['text'].strip()
                if not text:
                    continue
                if not include_completed and item['checked']:
                    continue
                # get_items() converts placeholders back to ![image](…) markers
                text = self._resolve_note_images(text).strip()
                if text:
                    messages.append(text)
            return messages
        else:
            text = self._editor.get_note_content(
                include_bold_markers=False).strip()
            text = self._resolve_note_images(text).strip()
            return [text] if text else []

    def _on_save_as_preset(self) -> None:
        """Save the current note's content as a named preset."""
        is_checklist = self._current_mode() == self._MODE_CHECKLIST

        # Default name: leaf name of the note (without folder path)
        default_name = self._current_name or ''
        if '/' in default_name:
            default_name = default_name.rsplit('/', 1)[-1]

        # Build a small custom dialog with name input + include-completed
        dlg = QDialog(self)
        dlg.setWindowTitle('Save as Preset')
        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.addWidget(QLabel('Preset name:'))
        name_edit = QLineEdit(default_name)
        name_edit.selectAll()
        dlg_layout.addWidget(name_edit)

        include_cb = QCheckBox('Include completed checkboxes')
        include_cb.setToolTip(
            'Include checked items when saving the preset')
        if is_checklist:
            prefs = load_monitor_prefs()
            include_cb.setChecked(
                prefs.get('save_preset_include_completed', False))
            dlg_layout.addWidget(include_cb)

        btn_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        dlg_layout.addWidget(btn_box)

        def _save_cb_state() -> None:
            if is_checklist:
                p = load_monitor_prefs()
                p['save_preset_include_completed'] = include_cb.isChecked()
                save_monitor_prefs(p)

        while True:
            if dlg.exec_() != QDialog.Accepted:
                _save_cb_state()
                return
            name = name_edit.text().strip()
            if not name:
                return

            if len(name) > 70:
                QMessageBox.warning(
                    self, 'Save as Preset',
                    'Preset name must be 70 characters or fewer.')
                continue

            existing = load_saved_presets()
            if name in existing:
                reply = QMessageBox.question(
                    self, 'Save as Preset',
                    f'Preset \u201c{name}\u201d already exists. Overwrite?',
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if reply != QMessageBox.Yes:
                    continue
            break

        _save_cb_state()
        messages = self._get_note_messages(
            include_completed=include_cb.isChecked())
        if not messages:
            hint = (' (or all checklist items are checked)'
                    if is_checklist else '')
            QMessageBox.information(
                self, 'Save as Preset',
                f'Nothing to save \u2014 the note is empty{hint}.')
            return

        save_named_preset(name, messages)
        count = len(messages)
        noun = 'message' if count == 1 else 'messages'
        QMessageBox.information(
            self, 'Save as Preset',
            f'Saved preset \u201c{name}\u201d with {count} {noun}.')

    def _on_run_in_session(self) -> None:
        """Send the current note's content to a running Leap session."""
        is_checklist = self._current_mode() == self._MODE_CHECKLIST
        result = _SessionPickerDialog.pick_session(
            self, is_checklist=is_checklist)
        if result is None:
            return
        tag, at_end, include_completed = result

        messages = self._get_note_messages(include_completed=include_completed)
        if not messages:
            hint = (' (or all checklist items are checked)'
                    if is_checklist else '')
            QMessageBox.information(
                self, 'Run in Session',
                f'Nothing to send \u2014 the note is empty{hint}.')
            return

        if at_end:
            results = [send_to_leap_session_raw(tag, msg) for msg in messages]
            sent = sum(results)
            total = len(results)
        else:
            ok = prepend_to_leap_queue(tag, messages)
            total = len(messages)
            sent = total if ok else 0

        noun = 'message' if total == 1 else 'messages'
        if sent == total:
            QMessageBox.information(
                self, 'Run in Session',
                f'Sent {total} {noun} to \u201c{tag}\u201d.')
        elif sent > 0:
            QMessageBox.warning(
                self, 'Run in Session',
                f'Sent {sent} of {total} {noun} to \u201c{tag}\u201d. '
                f'Some failed \u2014 the session may have stopped.')
        else:
            QMessageBox.warning(
                self, 'Run in Session',
                f'Failed to send to \u201c{tag}\u201d. '
                f'Is the session still running?')

    # ── Persistence ─────────────────────────────────────────────────

    def _save_current(self) -> None:
        """Write the current note to disk if changed."""
        if not self._current_name or self._undoing:
            return
        # Flush any open checklist popup so its unsaved text is
        # serialized before we read/save (popups feed the items list
        # on dismiss; without this, popup edits made since the last
        # keystroke that didn't emit text_edited could be lost).
        try:
            if (self._current_mode() == self._MODE_CHECKLIST
                    and not sip.isdeleted(self._checklist)):
                self._checklist._flush_popups()
        except RuntimeError:
            pass
        # Guard against reading from destroyed widgets during dialog teardown.
        # If a C++ widget was already deleted, bail out — do NOT write.
        try:
            if self._current_mode() == self._MODE_CHECKLIST:
                text = _serialize_checklist(self._checklist.get_items())
            else:
                text = self._editor.get_note_content()
        except RuntimeError:
            return
        if text != self._saved_text:
            try:
                path = _note_path(self._current_name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding='utf-8')
                pasted = (self._editor.take_pasted_images()
                          | self._checklist.take_pasted_images())
                _cleanup_orphaned_images(
                    text, self._saved_text, self._current_name, pasted,
                    deferred=self._pending_image_deletes)
                self._saved_text = text
                self._update_timestamp()
            except (OSError, RuntimeError):
                pass

    def _finalize_image_cleanup(self) -> None:
        """Delete deferred orphaned images. Called on dialog close."""
        if not self._pending_image_deletes:
            return
        all_refs = _all_note_image_refs()
        for filename in self._pending_image_deletes - all_refs:
            try:
                (NOTE_IMAGES_DIR / filename).unlink(missing_ok=True)
            except OSError:
                pass
        self._pending_image_deletes.clear()

    def changeEvent(self, event) -> None:  # type: ignore[override]
        """Re-apply tooltip font when Notes becomes the active window."""
        super().changeEvent(event)
        if (event.type() == QEvent.ActivationChange
                and self.isActiveWindow()
                and hasattr(self, '_buttons_font_size')):
            self._apply_tooltip_font_size(self._buttons_font_size)

    def showEvent(self, event) -> None:  # type: ignore[override]
        """Re-apply all three saved font sizes when the dialog becomes visible.

        Belt-and-suspenders: ``__init__`` already applies the sizes, but
        Qt may reconcile widget styles again between __init__ and show —
        re-applying here guarantees the saved values win regardless of
        whatever internal ordering Qt does.  Guarded by ``_shown_once``
        so user-initiated zoom during the session isn't clobbered if
        the dialog is hidden and shown again.
        """
        super().showEvent(event)
        if getattr(self, '_shown_once', False):
            return
        self._shown_once = True
        if hasattr(self, '_font_size'):
            self._apply_font_size()
        if hasattr(self, '_sidebar_font_size'):
            self._apply_sidebar_font_size()
        if hasattr(self, '_buttons_font_size'):
            self._apply_buttons_font_size()

    def done(self, result: int) -> None:
        """Auto-save and persist geometry on Escape / reject."""
        try:
            self._tree.currentItemChanged.disconnect(self._on_item_changed)
        except (TypeError, RuntimeError):
            pass
        try:
            self._mode_combo.currentIndexChanged.disconnect(self._on_mode_changed)
        except (TypeError, RuntimeError):
            pass
        self._save_current()
        self._finalize_image_cleanup()
        self._undo_stack.clear()
        # Flush any pending font-size save
        if hasattr(self, '_zoom_save_timer') and self._zoom_save_timer.isActive():
            self._zoom_save_timer.stop()
            self._save_font_sizes()
        if self._current_name:
            meta = _load_notes_meta()
            meta['_last_note'] = self._current_name
            _save_notes_meta(meta)
        # Save both the normal-state size (for first-run / fallback)
        # and the full Qt window-state blob (which carries
        # maximised/fullscreen).  ``normalGeometry`` is the same as
        # ``geometry`` when the window isn't maximised, so this also
        # works for the common case.
        normal_geom = self.normalGeometry()
        save_dialog_geometry(
            'notes_dialog', normal_geom.width(), normal_geom.height())
        save_dialog_geometry_state(
            'notes_dialog', bytes(self.saveGeometry()))
        if hasattr(self, '_splitter') and not sip.isdeleted(self._splitter):
            save_dialog_splitter_sizes(
                'notes_dialog_main', self._splitter.sizes())
        super().done(result)

    def closeEvent(self, event: 'QCloseEvent') -> None:  # type: ignore[override]
        """Auto-save, persist geometry, and emit finished for cleanup."""
        try:
            self._tree.currentItemChanged.disconnect(self._on_item_changed)
        except (TypeError, RuntimeError):
            pass
        try:
            self._mode_combo.currentIndexChanged.disconnect(self._on_mode_changed)
        except (TypeError, RuntimeError):
            pass
        self._save_current()
        self._finalize_image_cleanup()
        self._undo_stack.clear()
        # Flush any pending font-size save
        if hasattr(self, '_zoom_save_timer') and self._zoom_save_timer.isActive():
            self._zoom_save_timer.stop()
            self._save_font_sizes()
        if self._current_name:
            meta = _load_notes_meta()
            meta['_last_note'] = self._current_name
            _save_notes_meta(meta)
        # Save both the normal-state size (for first-run / fallback)
        # and the full Qt window-state blob (which carries
        # maximised/fullscreen).  ``normalGeometry`` is the same as
        # ``geometry`` when the window isn't maximised, so this also
        # works for the common case.
        normal_geom = self.normalGeometry()
        save_dialog_geometry(
            'notes_dialog', normal_geom.width(), normal_geom.height())
        save_dialog_geometry_state(
            'notes_dialog', bytes(self.saveGeometry()))
        if hasattr(self, '_splitter') and not sip.isdeleted(self._splitter):
            save_dialog_splitter_sizes(
                'notes_dialog_main', self._splitter.sizes())
        super().closeEvent(event)
        # Emit finished so _on_notes_closed cleans up (closeEvent does
        # not call done(), so finished is not emitted by default).
        self.finished.emit(self.result())

    # ── Font size / zoom ─────────────────────────────────────────────

    _MIN_FONT_SIZE = 9
    _MAX_FONT_SIZE = 28

    def _apply_font_size(self) -> None:
        """Apply the content font size to the text editor and checklist."""
        font = self._editor.font()
        font.setPointSize(self._font_size)
        font.setFamily('Menlo')
        self._editor.setFont(font)
        self._editor.document().setDefaultFont(font)
        # Stylesheet is the final authority — setFont/defaultFont can be
        # overridden by inserted char-formats.  Stylesheet wins.
        self._editor.setStyleSheet(
            f'QTextEdit {{ font-size: {self._font_size}pt;'
            f' font-family: Menlo; }}'
        )
        self._checklist.set_font_size(self._font_size)

    def _apply_sidebar_font_size(self) -> None:
        """Apply the sidebar font size to the tree and search bar.

        The tree has its own stylesheet (selection colors, outline) which
        blocks ancestor font-size from reliably cascading in.  Bake the
        font-size into the tree's own QSS directly.  Also setFont as
        belt-and-suspenders.
        """
        pt = self._sidebar_font_size
        self._left_panel.setStyleSheet(
            f'QTreeWidget, QLineEdit'
            f' {{ font-size: {pt}pt; }}'
        )
        # Also put font-size directly in the tree's own stylesheet.
        tree_qss = self._tree.styleSheet() or ''
        marker = '/* leap-zoom-tree */'
        base = tree_qss.split(marker)[0].rstrip()
        self._tree.setStyleSheet(
            f'{base}\n{marker}\n'
            f'QTreeWidget {{ font-size: {pt}pt; }}'
        )
        # Search bar has no own stylesheet, but setFont explicitly too.
        for w in (self._tree, self._search):
            font = w.font()
            font.setPointSize(pt)
            w.setFont(font)
        # Scale folder icons to match the font size
        icon_px = int(pt * 1.3)
        self._tree.setIconSize(QSize(icon_px, icon_px))

    def _apply_buttons_font_size(self) -> None:
        """Apply the buttons font size to chrome widgets.

        Follows the same pattern ``ZoomMixin`` uses for other dialogs
        in the project (which IS known to work):

        1. ``setFont`` on the dialog — descendants inherit unless they
           set their own font or have a stylesheet with font-size.
        2. Dialog-level QSS with many type selectors — wins cascade
           ties against ancestor QSS (descendant wins at same spec).

        Content (editor/checklist items) and sidebar widgets have
        their own stylesheets with ``font-size`` baked in (see
        ``_apply_font_size`` and ``_apply_sidebar_font_size``) so they
        are NOT affected by this dialog-level rule — widget stylesheet
        beats ancestor stylesheet.
        """
        pt = self._buttons_font_size
        # 1. setFont on the dialog — propagates via Qt font inheritance.
        dialog_font = self.font()
        dialog_font.setPointSize(pt)
        self.setFont(dialog_font)
        # 2. Dialog-level stylesheet with the same pattern ZoomMixin
        # uses — split on our marker so any external stylesheet is
        # preserved across zoom deltas.
        marker = '/* leap-notes-buttons-zoom */'
        existing = self.styleSheet() or ''
        base = existing.split(marker)[0].rstrip()
        self.setStyleSheet(
            f'{base}\n{marker}\n'
            f'QLabel, QPushButton, QComboBox, QCheckBox,'
            f' QRadioButton, QToolButton'
            f' {{ font-size: {pt}pt; }}'
        )
        # Also explicitly size the two widgets that carry their own
        # instance stylesheet (``_title_label`` has font-weight,
        # ``_bottom_hint`` has color) — Qt's cascade for font-size on
        # such widgets is unreliable, so bake it into their own QSS.
        size_rule = f'font-size: {pt}pt;'
        for attr in ('_bottom_hint', '_title_label', '_find_counter', '_dates_label'):
            w = getattr(self, attr, None)
            if w is None:
                continue
            existing_w = w.styleSheet() or ''
            cleaned = re.sub(r'\s*font-size:\s*[^;]+;\s*', '', existing_w)
            w.setStyleSheet(f'{cleaned} {size_rule}'.strip())
        # Also call the monitor-level hook so the app QSS gets an
        # ID-qualified rule too (spec 2 — extra insurance).
        app = QApplication.instance()
        if app is not None:
            for w in app.topLevelWidgets():
                cb = getattr(w, 'set_notes_chrome_font_size', None)
                if callable(cb):
                    cb(pt)
                    break
        # Update the global tooltip font (Notes is the active window).
        if self.isActiveWindow():
            self._apply_tooltip_font_size(pt)

    def _apply_tooltip_font_size(self, pt: int) -> None:
        """Ask MonitorWindow to rebuild the app QSS with this tooltip size."""
        app = QApplication.instance()
        if app is None:
            return
        for w in app.topLevelWidgets():
            cb = getattr(w, 'set_tooltip_font_size', None)
            if callable(cb):
                cb(pt)
                return

    def _zoom_target_for_widget(self, widget: Optional['QWidget']) -> str:
        """Determine zoom target based on which widget is under interaction.

        sidebar  → tree + search (only)
        content  → editor / checklist stack
        buttons  → everything else (toolbar rows, action buttons, bottom
                   Close, sidebar action buttons like "New Note")
        """
        if widget is None:
            return self._zoom_target
        # Walk up the widget tree to classify where the event originated.
        w = widget
        while w is not None:
            if w is self._tree or w is self._search:
                return 'sidebar'
            if w is self._stack:
                return 'content'
            if w is self:
                return 'buttons'
            w = w.parentWidget()
        return self._zoom_target

    def _resolve_zoom_target(self) -> str:
        """Return the zoom target based on mouse position (fall back to focus).

        Using mouse position matches the wheel behaviour and lets the
        user zoom the "buttons" target from the keyboard — there's no
        natural way to give keyboard focus to the toolbar button strip,
        so a focus-only resolver could never reach that target.
        """
        widget_under = QApplication.widgetAt(QCursor.pos())
        if widget_under is not None:
            # Only honour the cursor if it's actually over this dialog.
            w = widget_under
            while w is not None:
                if w is self:
                    return self._zoom_target_for_widget(widget_under)
                w = w.parentWidget()
        return self._zoom_target_for_widget(QApplication.focusWidget())

    def _zoom(self, delta: int, target: Optional[str] = None) -> None:
        """Change font size by delta for the given target and persist."""
        if target is None:
            target = self._resolve_zoom_target()
        self._zoom_target = target

        if target == 'sidebar':
            new_size = max(self._MIN_FONT_SIZE,
                           min(self._MAX_FONT_SIZE, self._sidebar_font_size + delta))
            if new_size == self._sidebar_font_size:
                return
            self._sidebar_font_size = new_size
            self._apply_sidebar_font_size()
        elif target == 'buttons':
            new_size = max(self._MIN_FONT_SIZE,
                           min(self._MAX_FONT_SIZE, self._buttons_font_size + delta))
            if new_size == self._buttons_font_size:
                return
            self._buttons_font_size = new_size
            self._apply_buttons_font_size()
        else:
            new_size = max(self._MIN_FONT_SIZE,
                           min(self._MAX_FONT_SIZE, self._font_size + delta))
            if new_size == self._font_size:
                return
            self._font_size = new_size
            self._apply_font_size()

        # Debounce disk write — rapid Cmd+scroll fires many events
        if not hasattr(self, '_zoom_save_timer'):
            self._zoom_save_timer = QTimer(self)
            self._zoom_save_timer.setSingleShot(True)
            self._zoom_save_timer.timeout.connect(self._save_font_sizes)
        self._zoom_save_timer.start(300)

    def _save_font_sizes(self) -> None:
        """Persist all font sizes to prefs."""
        prefs = load_monitor_prefs()
        prefs['notes_font_size'] = self._font_size
        prefs['notes_sidebar_font_size'] = self._sidebar_font_size
        prefs['notes_buttons_font_size'] = self._buttons_font_size
        save_monitor_prefs(prefs)

    def _reset_zoom(self) -> None:
        """Reset font size to theme default for the focused area."""
        target = self._resolve_zoom_target()
        default = current_theme().font_size_base
        # Cancel any pending debounced save — we'll write both values below
        if hasattr(self, '_zoom_save_timer') and self._zoom_save_timer.isActive():
            self._zoom_save_timer.stop()

        if target == 'sidebar':
            if self._sidebar_font_size == default:
                return
            self._sidebar_font_size = default
            self._apply_sidebar_font_size()
        elif target == 'buttons':
            if self._buttons_font_size == default:
                return
            self._buttons_font_size = default
            self._apply_buttons_font_size()
        else:
            if self._font_size == default:
                return
            self._font_size = default
            self._apply_font_size()

        # Save all values — the timer may have been pending for another target
        self._save_font_sizes()

    def wheelEvent(self, event: 'QWheelEvent') -> None:  # type: ignore[override]
        if event.modifiers() & Qt.ControlModifier:
            delta = 1 if event.angleDelta().y() > 0 else -1
            # Determine target from the widget under the mouse cursor
            widget_under = QApplication.widgetAt(QCursor.pos())
            target = self._zoom_target_for_widget(widget_under)
            self._zoom(delta, target=target)
            event.accept()
            return
        super().wheelEvent(event)

    def eventFilter(self, obj: 'QObject', event: 'QEvent') -> bool:  # type: ignore[override]
        if obj is self._search and event.type() == QEvent.FocusIn:
            if not self.isActiveWindow():
                QApplication.setActiveWindow(self)
        # Find-bar: Enter / Shift+Enter / Escape
        if (hasattr(self, '_find_input') and obj is self._find_input
                and event.type() == QEvent.KeyPress):
            if event.key() == Qt.Key_Escape:
                self._hide_find_bar()
                return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ShiftModifier:
                    self._find_prev()
                else:
                    self._find_next()
                return True
        # Tree: Delete / Backspace remove the selected note or folder.
        # Guarded at the filter level because QAbstractItemView can swallow
        # these keys before propagation reaches the dialog's keyPressEvent.
        if (hasattr(self, '_tree') and obj is self._tree
                and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Delete, Qt.Key_Backspace)
                and not (event.modifiers() & (
                    Qt.ControlModifier | Qt.ShiftModifier
                    | Qt.AltModifier | Qt.MetaModifier))
                and self._tree.selectedItems()):
            self._on_delete()
            return True
        # Tree: arrow keys — Up/Down move between notes (skip folders),
        # Left/Right move between folders (skip notes).  Always consume
        # the event (even if no target exists), otherwise Qt's default
        # arrow navigation fires and silently breaks the "notes only"
        # expectation at list boundaries.
        if (hasattr(self, '_tree') and obj is self._tree
                and event.type() == QEvent.KeyPress
                and event.key() in (
                    Qt.Key_Up, Qt.Key_Down,
                    Qt.Key_Left, Qt.Key_Right)
                and not (event.modifiers() & (
                    Qt.ControlModifier | Qt.ShiftModifier
                    | Qt.AltModifier | Qt.MetaModifier))):
            key = event.key()
            if key in (Qt.Key_Up, Qt.Key_Down):
                self._navigate_tree_typed('note', forward=(key == Qt.Key_Down))
            else:  # Left / Right
                self._navigate_tree_typed('folder', forward=(key == Qt.Key_Right))
            return True
        # Intercept Cmd+scroll on viewports — route to correct zoom target
        if event.type() == QEvent.Wheel:
            we = sip.cast(event, QWheelEvent)
            if we.modifiers() & Qt.ControlModifier:
                delta = 1 if we.angleDelta().y() > 0 else -1
                # Determine target from which viewport received the scroll
                target = self._zoom_target_for_widget(obj)
                self._zoom(delta, target=target)
                return True
        return super().eventFilter(obj, event)

    # ── Tree navigation ─────────────────────────────────────────────

    def _navigate_tree_typed(self, type_: str, forward: bool) -> bool:
        """Move the tree's current item to the next/prev item of *type_*.

        *type_* is ``'note'`` or ``'folder'``.  ``forward=True`` moves
        toward the end of the tree; False moves toward the start.
        Returns True if navigation happened (event should be consumed).
        """
        all_items: list[QTreeWidgetItem] = []
        it = QTreeWidgetItemIterator(self._tree)
        while it.value():
            all_items.append(it.value())
            it += 1
        current = self._tree.currentItem()
        try:
            current_pos = all_items.index(current) if current else -1
        except ValueError:
            current_pos = -1

        if forward:
            start = current_pos + 1 if current_pos >= 0 else 0
            seq = all_items[start:]
        else:
            end = current_pos if current_pos >= 0 else len(all_items)
            seq = list(reversed(all_items[:end]))
        for item in seq:
            if item.data(0, self._ROLE_TYPE) == type_:
                self._tree.setCurrentItem(item)
                # Loading a checklist note rebuilds its layout and its
                # _clear_layout() steals focus to the scroll area — that
                # made subsequent arrow-key events bypass the tree filter.
                # Restore focus so the next arrow continues navigation.
                self._tree.setFocus()
                return True
        return False

    # ── In-note find bar ────────────────────────────────────────────

    def _build_find_bar(self) -> QWidget:
        """Build the inline find bar (Cmd+F).  Hidden by default."""
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(4)

        self._find_input = QLineEdit()
        self._find_input.setPlaceholderText('Find in note…')
        self._find_input.textChanged.connect(self._on_find_query_changed)
        self._find_input.installEventFilter(self)
        row.addWidget(self._find_input, 1)

        self._find_counter = QLabel('')
        self._find_counter.setStyleSheet(
            f'color: {current_theme().text_muted}; padding: 0 6px;')
        self._find_counter.setMinimumWidth(70)
        self._find_counter.setAlignment(Qt.AlignCenter)
        row.addWidget(self._find_counter)

        prev_btn = QPushButton('◀')
        prev_btn.setFixedWidth(30)
        prev_btn.setToolTip('Previous match (Shift+Enter)')
        prev_btn.clicked.connect(self._find_prev)
        row.addWidget(prev_btn)

        next_btn = QPushButton('▶')
        next_btn.setFixedWidth(30)
        next_btn.setToolTip('Next match (Enter)')
        next_btn.clicked.connect(self._find_next)
        row.addWidget(next_btn)

        close_btn = QPushButton('✕')
        close_btn.setFixedWidth(30)
        close_btn.setToolTip('Close (Esc)')
        close_btn.clicked.connect(self._hide_find_bar)
        row.addWidget(close_btn)

        bar.setVisible(False)
        return bar

    def _show_find_bar(self) -> None:
        """Reveal the find bar, pre-filled with the editor's selection if any."""
        if not self._current_name or self._current_mode() != self._MODE_TEXT:
            return
        cursor = self._editor.textCursor()
        if cursor.hasSelection():
            # Block signals so the prefill doesn't yank the cursor back
            # to doc start via the incremental-search handler.
            self._find_input.blockSignals(True)
            self._find_input.setText(cursor.selectedText())
            self._find_input.blockSignals(False)
        self._find_bar.setVisible(True)
        self._find_input.setFocus()
        self._find_input.selectAll()
        self._update_find_counter()

    def _hide_find_bar(self) -> None:
        self._find_bar.setVisible(False)
        self._editor.setFocus()

    def _set_find_input_style(self, extra_css: str = '') -> None:
        """Apply a stylesheet to the find input, preserving the zoom size.

        The buttons-zoom layers bake a font-size into find_input's own
        stylesheet — when we later set a red-tint background, we must
        carry the font-size forward or the widget shrinks back to the
        app default.
        """
        pt = self._buttons_font_size
        css = f'font-size: {pt}pt;'
        if extra_css:
            css += f' {extra_css}'
        self._find_input.setStyleSheet(css)

    def _on_find_query_changed(self, text: str) -> None:
        """Incremental find — jumps to first match on every keystroke."""
        self._set_find_input_style()
        if not text:
            self._find_counter.setText('')
            return
        # Reset cursor to doc start so the search always lands on the
        # earliest match (Chrome-style).  Otherwise typing that extends
        # the query could skip earlier matches.
        cursor = self._editor.textCursor()
        cursor.movePosition(QTextCursor.Start)
        self._editor.setTextCursor(cursor)
        found = self._editor.find(text, QTextDocument.FindFlag(0))
        self._update_find_counter()
        if not found:
            self._set_find_input_style(
                'background: rgba(248, 113, 113, 0.25);')

    def _find_next(self) -> None:
        self._find_in_editor(backward=False)

    def _find_prev(self) -> None:
        self._find_in_editor(backward=True)

    def _find_in_editor(self, backward: bool) -> None:
        """Search for the find-bar text, wrapping around on no-match."""
        query = self._find_input.text()
        if not query:
            return
        flags = QTextDocument.FindFlag(0)
        if backward:
            flags |= QTextDocument.FindBackward
        if self._editor.find(query, flags):
            # Clear any red no-match tint from an earlier failed search —
            # otherwise the input stays red even when Next/Prev finds a
            # match because the tint is only written, never reset, here.
            self._set_find_input_style()
            self._update_find_counter()
            return
        # First attempt failed — try wrapping around.  Remember the
        # original cursor so we can restore it if the wrap also fails,
        # otherwise the user loses their place on a no-match.
        orig_cursor = self._editor.textCursor()
        wrap_cursor = QTextCursor(orig_cursor)
        wrap_cursor.movePosition(
            QTextCursor.End if backward else QTextCursor.Start)
        self._editor.setTextCursor(wrap_cursor)
        if self._editor.find(query, flags):
            # Wrap succeeded — clear any stale red tint.
            self._set_find_input_style()
        else:
            self._editor.setTextCursor(orig_cursor)
            self._set_find_input_style(
                'background: rgba(248, 113, 113, 0.25);')
        self._update_find_counter()

    def _update_find_counter(self) -> None:
        """Refresh the 'K of N' label based on cursor position + query."""
        query = self._find_input.text()
        if not query:
            self._find_counter.setText('')
            return
        doc_text = self._editor.toPlainText()
        q_lower = query.lower()
        total = doc_text.lower().count(q_lower)
        if total == 0:
            self._find_counter.setText('No results')
            return
        cursor = self._editor.textCursor()
        # If the cursor is sitting on a live match, report 1-based index;
        # otherwise report "· of N" as a neutral pre-navigation state.
        if (cursor.hasSelection()
                and cursor.selectedText().lower() == q_lower):
            before = doc_text[:cursor.selectionStart()].lower().count(q_lower)
            self._find_counter.setText(f'{before + 1} of {total}')
        else:
            self._find_counter.setText(f'· of {total}')

    def _unlink_selection(self, focus: QWidget) -> None:
        """Strip any link styling from the selection.

        For QTextEdit targets (main editor + checklist popup): clear
        anchor / underline / link-colour char-format properties while
        preserving the bold state — a bold link becomes a bold plain
        word (orange), a plain link becomes plain text (primary).
        For QLineEdit (checklist read-only display): walk the raw
        markdown text and remove the ``[text](url)`` span under the
        cursor.
        """
        if isinstance(focus, (_NoteTextEdit, QTextEdit)):
            cursor = focus.textCursor()
            if not cursor.hasSelection():
                return
            probe = cursor.charFormat()
            is_bold = probe.fontWeight() >= QFont.Bold
            t = current_theme()
            color = t.accent_orange if is_bold else t.text_primary
            clear_fmt = QTextCharFormat()
            clear_fmt.setAnchor(False)
            clear_fmt.setAnchorHref('')
            clear_fmt.setFontUnderline(False)
            clear_fmt.setForeground(QColor(color))
            cursor.mergeCharFormat(clear_fmt)
            focus.setTextCursor(cursor)
            return
        if isinstance(focus, QLineEdit):
            parent_item = focus.parent()
            if isinstance(parent_item, _ChecklistItemWidget):
                # Line edit shows stripped display — operate on
                # ``_raw_text`` so the markdown link syntax is removed.
                raw = parent_item._raw_text
                col = focus.cursorPosition()
                # Map display position to raw position, then find the
                # link span in raw that covers it.
                raw_col = _display_to_raw_pos(raw, col)
                span = _find_markdown_link_at(raw, raw_col)
                if span is None:
                    return
                a, b, display = span
                new_raw = raw[:a] + display + raw[b:]
                parent_item.set_raw_text(new_raw)
                return
            text = focus.text()
            col = focus.cursorPosition()
            span = _find_markdown_link_at(text, col)
            if span is None:
                return
            a, b, display = span
            new_text = text[:a] + display + text[b:]
            focus.setText(new_text)
            focus.setCursorPosition(a + len(display))
            return

    def _insert_link(self) -> None:
        """Prompt for a URL and insert it at the cursor (Cmd+K)."""
        focus = QApplication.focusWidget()
        # Refuse to insert if focus isn't on a note editor — sidebar
        # search, find input, or no focus at all should not receive
        # a link.  This prevents URLs landing in the search box when
        # a checklist popup dismisses during the modal.
        if not isinstance(focus, (_NoteTextEdit, QTextEdit, QLineEdit)):
            return
        if (focus is getattr(self, '_search', None)
                or focus is getattr(self, '_find_input', None)):
            return
        # Block if cursor/selection is on an image
        if isinstance(focus, _NoteTextEdit):
            cursor = focus.textCursor()
            if cursor.charFormat().isImageFormat():
                return
            if cursor.hasSelection():
                # Walk the selection for image fragments
                start, end = cursor.selectionStart(), cursor.selectionEnd()
                check = QTextCursor(focus.document())
                check.setPosition(start)
                while check.position() < end:
                    if check.charFormat().isImageFormat():
                        return
                    if not check.movePosition(QTextCursor.NextCharacter):
                        break
        # Remember selected text + its position before the dialog.
        # Checklist expand popups dismiss on focus loss when the URL
        # dialog opens, destroying the live selection — so we snapshot
        # enough state to replay the edit against the fallback line edit.
        selected = ''
        sel_start = -1
        sel_end = -1
        if isinstance(focus, (_NoteTextEdit, QTextEdit)):
            c = focus.textCursor()
            if c.hasSelection():
                selected = c.selectedText()
                sel_start = c.selectionStart()
                sel_end = c.selectionEnd()
        elif isinstance(focus, QLineEdit):
            if focus.hasSelectedText():
                selected = focus.selectedText()
                sel_start = focus.selectionStart()
                sel_end = sel_start + len(selected)

        # If focus is a checklist popup, remember the underlying line
        # edit as a fallback — the popup will dismiss when the URL
        # dialog steals focus.  Covers BOTH item popups and the
        # "Add item" field popup at the bottom of the checklist.
        fallback_line_edit: Optional[QLineEdit] = None
        if (isinstance(focus, QTextEdit)
                and not isinstance(focus, _NoteTextEdit)):
            parent_item = focus.parent()
            if (isinstance(parent_item, _ChecklistItemWidget)
                    and parent_item._popup is focus):
                fallback_line_edit = parent_item._edit
            elif (hasattr(self, '_checklist')
                    and self._checklist._add_popup is focus):
                fallback_line_edit = self._checklist._add_field

        # Pre-fill with clipboard text if it looks like a URL
        clipboard = QApplication.clipboard()
        clip = clipboard.text().strip() if clipboard else ''
        prefill = clip if _ANY_URL_RE.fullmatch(clip) else ''

        # Suppress the checklist popup's focus-out dismissal while the
        # URL dialog is open.  Otherwise the popup serializes to
        # markdown and updates ``_raw_text``, and the captured
        # ``sel_start/sel_end`` (in popup plain-text coordinates) no
        # longer match the raw text's positions — slicing raw markdown
        # with those positions corrupts content (e.g. ``[a](u1) b``
        # becomes ``[a]b`` after trying to link ``b``).
        popup_had_suppression = False
        if (isinstance(focus, QTextEdit)
                and not isinstance(focus, _NoteTextEdit)):
            popup_had_suppression = True
            focus._suppress_dismiss = True

        try:
            while True:
                url, ok = QInputDialog.getText(
                    self, 'Insert Link', 'URL:', QLineEdit.Normal, prefill)
                if not ok:
                    return
                url = url.strip()
                # Empty URL is allowed — it means "unset any link on the
                # selection".  Any other non-matching string is still an
                # error and re-prompts.
                if not url or _ANY_URL_RE.fullmatch(url):
                    break
                prefill = url
                QMessageBox.warning(self, 'Invalid URL',
                                    'Please enter a valid URL (e.g. https://…)')
        finally:
            # Clear the flag so the popup dismisses normally on its
            # next focus loss (the one that was suppressed has
            # already been consumed by the modal).
            if (popup_had_suppression
                    and focus is not None
                    and not sip.isdeleted(focus)):
                focus._suppress_dismiss = False
                # Restore focus so typing after Cmd+K stays in the popup.
                focus.setFocus()

        # The dialog may have caused the popup to dismiss.  If so, route
        # the link into its parent's line edit rather than falling back
        # to whatever Qt decided to focus next (which may be the sidebar
        # search — that is how URLs used to leak into the search box).
        if focus is None or sip.isdeleted(focus):
            if (fallback_line_edit is not None
                    and not sip.isdeleted(fallback_line_edit)):
                # Safety net for the case where something destroyed the
                # popup while the modal was up despite ``_suppress_dismiss``.
                # For item line edits the ``_raw_text``/``_display_to_raw_pos``
                # path below handles the markdown-vs-display offset.  For
                # the add field the dismissed popup serializes markdown
                # into the line edit, so display-coordinate
                # ``sel_start``/``sel_end`` may no longer point at the
                # intended range — we accept a potentially corrupted edit
                # here as the lesser evil to losing the link entirely.
                focus = fallback_line_edit
            else:
                return

        # Empty URL → strip any link off the selection, keep the text.
        if not url:
            self._unlink_selection(focus)
            return

        # Insert into whichever editor widget had focus.
        # Use the captured ``sel_start`` / ``sel_end`` / ``selected``
        # rather than the live cursor — the modal dialog can clear the
        # visual selection on some platforms (macOS focus quirks).
        fmt = _link_char_format(url)
        if isinstance(focus, _NoteTextEdit):
            cursor = focus.textCursor()
            if selected and sel_start >= 0:
                # Re-anchor to the saved range and merge the link
                # format — ``mergeCharFormat`` preserves existing
                # attributes (e.g. bold) while adding anchor + colour
                # + underline on top.  This is what lets a bold word
                # become a bold *link* instead of a plain link.
                cursor.setPosition(sel_start)
                cursor.setPosition(sel_end, QTextCursor.KeepAnchor)
                cursor.mergeCharFormat(fmt)
                # Collapse the selection BEFORE clearing the
                # insertion format — ``setCharFormat`` on a selection
                # replaces the selection's format (which would undo
                # the merge we just did).
                cursor.setPosition(sel_end)
            else:
                cursor.insertText(url, fmt)
            # Reset format so text typed after the link is normal
            cursor.setCharFormat(QTextCharFormat())
            focus.setTextCursor(cursor)
        elif isinstance(focus, QTextEdit):
            # Checklist expand popup (still alive).  Merge the link
            # format onto the selection so existing bold/other char
            # attributes survive.  If there's no saved selection, we
            # insert fresh link text with just the link format.
            cursor = focus.textCursor()
            link_fmt = _link_char_format(url)
            if selected and sel_start >= 0:
                cursor.setPosition(sel_start)
                cursor.setPosition(sel_end, QTextCursor.KeepAnchor)
                cursor.mergeCharFormat(link_fmt)
                # Collapse selection before setCharFormat (see
                # _NoteTextEdit branch above for rationale).
                cursor.setPosition(sel_end)
            elif selected:
                cursor.insertText(selected, link_fmt)
            else:
                cursor.insertText(url, link_fmt)
            # Clear anchor format so subsequent typing isn't part of the
            # link; Qt otherwise extends the anchor into neighbouring
            # characters (same symptom as the text-note link bleed).
            cursor.setCharFormat(QTextCharFormat())
            focus.setTextCursor(cursor)
            # insertText bypasses the popup's on_key wiring that normally
            # fires text_edited — do it manually so the items list is
            # updated before the popup dismisses (otherwise a quick
            # navigate-away could save stale text).
            parent_item = focus.parent()
            if isinstance(parent_item, _ChecklistItemWidget):
                parent_item.text_edited.emit(
                    parent_item._index,
                    _ChecklistItemWidget._serialize_popup_markdown(focus))
        elif isinstance(focus, QLineEdit):
            # Line edit (direct focus, or fallback after a popup dismissed).
            # For checklist items the line edit shows STRIPPED display
            # (markdown link syntax + STX/ETX bold markers hidden),
            # so the saved ``sel_start/sel_end`` are in display
            # coordinates.  Map them to raw-text positions before
            # slicing — otherwise a raw like ``[a](u) asdas`` with a
            # display-position of 2 would slice at raw position 2
            # (inside the link brackets), corrupting the markdown.
            replacement = f'[{selected}]({url})' if selected else url
            parent_item = focus.parent()
            if isinstance(parent_item, _ChecklistItemWidget):
                source = parent_item._raw_text
                if selected and sel_start >= 0:
                    raw_start = _display_to_raw_pos(source, sel_start)
                    raw_end = _display_to_raw_pos(source, sel_end)
                    new_raw = source[:raw_start] + replacement + source[raw_end:]
                else:
                    new_raw = source + replacement
                parent_item.set_raw_text(new_raw)
            else:
                # Plain line edit (add-field / else) — plain text path.
                if selected and sel_start >= 0 and sel_end <= len(focus.text()):
                    text = focus.text()
                    focus.setText(text[:sel_start] + replacement + text[sel_end:])
                    focus.setCursorPosition(sel_start + len(replacement))
                elif focus.hasSelectedText():
                    focus.del_()
                    focus.insert(replacement)
                else:
                    focus.insert(replacement)
        # If the link was inserted into the checklist's "Add item" field
        # (or its expand popup), commit it as a new item immediately so
        # the user never sees raw ``[text](url)`` syntax lingering in
        # the add-field awaiting Enter.
        checklist = getattr(self, '_checklist', None)
        if checklist is not None:
            add_field = getattr(checklist, '_add_field', None)
            add_popup = getattr(checklist, '_add_popup', None)
            if focus is add_field or (add_popup is not None and focus is add_popup):
                checklist._on_add_item()
                return

        if focus and not sip.isdeleted(focus):
            focus.setFocus()

    def keyPressEvent(self, event: 'QKeyEvent') -> None:  # type: ignore[override]
        """Handle keyboard shortcuts."""
        # Prevent Enter/Return from closing the dialog (QDialog default)
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            event.accept()
            return
        mods = event.modifiers()
        # Undo/redo — delegate to Qt's built-in text undo if available,
        # otherwise use our structural undo stack.
        if (mods & Qt.ControlModifier) and event.key() == Qt.Key_Z:
            is_redo = bool(mods & Qt.ShiftModifier)
            focus = QApplication.focusWidget()
            # Check if the focused text widget has its own undo/redo
            use_qt_undo = False
            if isinstance(focus, (_NoteTextEdit, QTextEdit)):
                avail = (focus.document().isRedoAvailable() if is_redo
                         else focus.document().isUndoAvailable())
                use_qt_undo = avail
            elif isinstance(focus, QLineEdit):
                avail = focus.isRedoAvailable() if is_redo else focus.isUndoAvailable()
                use_qt_undo = avail
            if not use_qt_undo:
                # Clear search filter so undo/redo target is visible
                if self._search.text():
                    self._search.clear()
                self._undoing = True
                try:
                    if is_redo:
                        self._undo_stack.redo(self._cmd_ctx)
                    else:
                        self._undo_stack.undo(self._cmd_ctx)
                finally:
                    self._undoing = False
                return
        if mods & Qt.ControlModifier:
            if event.key() == Qt.Key_S:
                self._save_current()
                return
            if event.key() == Qt.Key_N:
                if mods & Qt.ShiftModifier:
                    self._on_new_folder()
                else:
                    self._on_new()
                return
            if event.key() == Qt.Key_F:
                if mods & Qt.ShiftModifier:
                    # Cmd+Shift+F → focus sidebar (search all notes)
                    QApplication.setActiveWindow(self)
                    self._search.setFocus()
                    self._search.selectAll()
                else:
                    # Cmd+F → find within the current note
                    self._show_find_bar()
                return
            if event.key() == Qt.Key_K:
                self._insert_link()
                return
            if event.key() in (Qt.Key_Equal, Qt.Key_Plus):
                self._zoom(1)
                return
            if event.key() == Qt.Key_Minus:
                self._zoom(-1)
                return
            if event.key() == Qt.Key_0:
                self._reset_zoom()
                return
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and not mods:
            if self._tree.hasFocus() and self._tree.currentItem():
                self._on_delete()
                return
        super().keyPressEvent(event)
