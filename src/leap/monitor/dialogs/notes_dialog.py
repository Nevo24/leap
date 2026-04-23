"""Free-form notes dialog for Leap Monitor.

Supports multiple notes organized in folders under .storage/notes/.
Each note can be either plain text or a Google Keep-style checklist.
Left panel shows a searchable folder tree; right panel is the editor.
Notes auto-save on switch, close, and Cmd+S.
"""

import hashlib
import html
import json
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt5 import sip
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSplitter,
    QStackedWidget, QStyle, QTableWidget, QTableWidgetItem, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout,
    QWidget,
)
from PyQt5.QtCore import QEvent, QMimeData, QPoint, QSize, QTimer, QUrl, Qt, pyqtSignal
from PyQt5.QtGui import (
    QColor, QCursor, QDesktopServices, QDrag, QFont, QFontMetrics, QImage,
    QPixmap, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument,
    QTextImageFormat, QWheelEvent,
)

from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.dialogs.notes_undo import (
    BatchDeleteCmd, ChecklistAddItemCmd, ChecklistDeleteItemCmd,
    ChecklistReorderCmd, ChecklistToggleCmd,
    CreateFolderCmd, CreateNoteCmd, DeleteFolderCmd,
    DeleteNoteCmd, ModeSwitchCmd, MoveFolderCmd, MoveNoteCmd,
    NoteContentChangeCmd, NotesCmdContext, NotesUndoStack,
    RenameFolderCmd, RenameNoteCmd, ReorderCmd,
)
from leap.monitor.leap_sender import (
    prepend_to_leap_queue, send_to_leap_session_raw,
)
from leap.monitor.pr_tracking.config import (
    load_dialog_geometry, load_monitor_prefs, load_saved_presets,
    load_send_position, save_dialog_geometry, save_monitor_prefs,
    save_named_preset,
)
from leap.monitor.session_manager import get_active_sessions
from leap.monitor.themes import current_theme
from leap.monitor.ui.image_text_edit import _build_send_position_toggle
from leap.utils.constants import NOTE_IMAGES_DIR, NOTES_DIR, QUEUE_IMAGES_DIR


MAX_NOTE_NAME_LEN = 80
_NOTES_META_FILE: Path = NOTES_DIR / '.notes_meta.json'
_IMAGE_MARKER_RE = re.compile(r'!\[image\]\(([a-f0-9]+\.png)\)')
_CHECKLIST_PLACEHOLDER_RE = re.compile(r'\[Image #\d+\]')
_NOTE_IMAGE_MAX_WIDTH = 400
_URL_RE = re.compile(r'https?://[^\s<>\"\')]+')
# Any scheme (slack://, mailto:, tel:, etc.)
_ANY_URL_RE = re.compile(r'[a-zA-Z][a-zA-Z0-9+.-]*://\S+|mailto:\S+')
# Markdown link: [display text](url) — negative lookbehind excludes ![image](…)
_LINK_RE = re.compile(r'(?<!!)\[([^\]]+)\]\((\S+://[^\s\)]+)\)')
# Bold is delimited on disk by ASCII control chars STX/ETX (U+0002/U+0003).
# These are impossible to type on a keyboard, so any text the user types —
# including literal ``**``, ``<b>``, ``__``, etc. — is preserved exactly.
# The trade-off: if you open a note's .txt externally, bolded spans show
# up as ``^B…^C`` glyphs.  Notes are edited via the Leap UI by design.
_BOLD_START = '\x02'
_BOLD_END = '\x03'
# Combined inline formats: [link](url) OR STX…ETX.  Bold does not span
# newlines (``.`` without re.DOTALL).  Groups: 1=link-text, 2=link-url,
# 3=bold-text.
_INLINE_FORMAT_RE = re.compile(
    r'(?<!!)\[([^\]]+)\]\((\S+://[^\s\)]+)\)'
    f'|{_BOLD_START}(.+?){_BOLD_END}'
)


# ── URL highlighting ───────────────────────────────────────────────


class _UrlHighlighter(QSyntaxHighlighter):
    """Highlights bare URLs and [text](url) links with the theme accent blue."""

    def __init__(self, parent: 'QTextDocument') -> None:
        super().__init__(parent)
        self._link_fmt = QTextCharFormat()
        t = current_theme()
        self._link_fmt.setForeground(QColor(t.accent_blue))
        self._link_fmt.setFontUnderline(True)
        self._muted_fmt = QTextCharFormat()
        self._muted_fmt.setForeground(QColor(t.text_muted))

    def highlightBlock(self, text: str) -> None:
        # Track which ranges are covered by markdown links
        link_ranges: list[tuple[int, int]] = []
        for m in _LINK_RE.finditer(text):
            # Highlight display text as link
            self.setFormat(m.start(1), len(m.group(1)), self._link_fmt)
            # Dim the surrounding syntax: [ ] ( url )
            self.setFormat(m.start(), 1, self._muted_fmt)        # [
            self.setFormat(m.end(1), 2, self._muted_fmt)         # ](
            self.setFormat(m.start(2), len(m.group(2)), self._muted_fmt)  # url
            self.setFormat(m.end(2), 1, self._muted_fmt)         # )
            link_ranges.append((m.start(), m.end()))
        # Highlight bare URLs not inside markdown links
        for m in _URL_RE.finditer(text):
            if not any(s <= m.start() and m.end() <= e for s, e in link_ranges):
                self.setFormat(m.start(), m.end() - m.start(), self._link_fmt)


def _url_in_text_at_col(text: str, col: int) -> Optional[str]:
    """Return the URL at column position in text (markdown link or bare URL)."""
    # Check markdown links first — click on display text opens the URL
    for m in _LINK_RE.finditer(text):
        if m.start() <= col < m.end():
            return m.group(2)
    for m in _URL_RE.finditer(text):
        if m.start() <= col < m.end():
            return m.group()
    return None


def _url_at_pos(widget: 'QTextEdit', pos: QPoint) -> Optional[str]:
    """Return the URL at viewport position in a QTextEdit, or None.

    Supports three styles of URL: anchor-format char spans (what the
    checklist popup and text-note editor render for ``[word](url)``),
    literal markdown syntax in the plain text, and bare URL matches.
    """
    # anchorAt clamps at the glyph, so reject empty-space clicks.
    href = widget.anchorAt(pos)
    if href:
        return href
    cursor = widget.cursorForPosition(pos)
    fmt = cursor.charFormat()
    if fmt.isAnchor() and fmt.anchorHref():
        return fmt.anchorHref()
    return _url_in_text_at_col(cursor.block().text(), cursor.positionInBlock())


def _url_at_line_edit_pos(widget: QLineEdit, pos: QPoint) -> Optional[str]:
    """Return the URL at position in a QLineEdit, or None."""
    col = widget.cursorPositionAt(pos)
    return _url_in_text_at_col(widget.text(), col)


def _find_markdown_link_at(text: str, col: int) -> Optional[tuple[int, int, str]]:
    """Return (start, end, display_text) of the markdown link covering col.

    Used by the "Cmd+K with empty URL = unlink" flow for plain-text
    targets (checklist line edit / expand popup) where links are stored
    as literal ``[text](url)`` rather than rich-text anchors.
    """
    for m in _LINK_RE.finditer(text):
        if m.start() <= col <= m.end():
            return m.start(), m.end(), m.group(1)
    return None


def _link_char_format(url: str) -> QTextCharFormat:
    """Return a QTextCharFormat styled as a clickable link."""
    fmt = QTextCharFormat()
    t = current_theme()
    fmt.setForeground(QColor(t.accent_blue))
    fmt.setFontUnderline(True)
    # Explicitly normal weight so linking bolded text drops the bold —
    # the user asked a bold word to become a link, not a bold link.
    fmt.setFontWeight(QFont.Normal)
    fmt.setAnchor(True)
    fmt.setAnchorHref(url)
    return fmt


def _try_open_url(url: str) -> None:
    """Open a URL in the default browser."""
    QDesktopServices.openUrl(QUrl(url))


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


def _all_note_image_refs(exclude_name: Optional[str] = None) -> set[str]:
    """Scan all notes on disk and return the union of referenced image filenames.

    Args:
        exclude_name: Note name (relative path without .txt) to skip.
    """
    refs: set[str] = set()
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    for p in NOTES_DIR.rglob('*.txt'):
        if not p.is_file():
            continue
        rel = str(p.relative_to(NOTES_DIR).with_suffix(''))
        if exclude_name and rel == exclude_name:
            continue
        try:
            refs |= _collect_image_refs(p.read_text(encoding='utf-8'))
        except OSError:
            pass
    return refs


def _cleanup_orphaned_images(
    current_text: str, previous_text: str, note_name: str,
    pasted: Optional[set[str]] = None,
    deferred: Optional[set[str]] = None,
) -> None:
    """Delete images removed from a note, unless still used by another note.

    *pasted* includes images saved to disk this session that may not appear
    in *previous_text* (e.g. pasted then deleted before save).

    When *deferred* is provided (a mutable set), orphaned filenames are
    collected into the set instead of being deleted immediately.  The caller
    is responsible for calling the actual unlink later (e.g. on dialog close).
    """
    old_refs = _collect_image_refs(previous_text)
    if pasted:
        old_refs |= pasted
    new_refs = _collect_image_refs(current_text)
    candidates = old_refs - new_refs
    if not candidates:
        return
    # Check all other notes before deleting
    other_refs = _all_note_image_refs(exclude_name=note_name)
    for filename in candidates - other_refs:
        if deferred is not None:
            deferred.add(filename)
        else:
            try:
                (NOTE_IMAGES_DIR / filename).unlink(missing_ok=True)
            except OSError:
                pass



_NOTE_IMAGE_PREVIEW_MAX = 600


class _ImagePreviewPopup(QLabel):
    """Frameless popup that shows a larger version of a note image."""

    def __init__(self) -> None:
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet('background: transparent; padding: 0px;')
        self._current_name: Optional[str] = None

    def show_for_image(self, name: str, global_pos: QPoint) -> None:
        """Show the popup near *global_pos* for the image *name*."""
        if name == self._current_name and self.isVisible():
            return
        path = str(NOTE_IMAGES_DIR / name)
        px = QPixmap(path)
        if px.isNull():
            self.hide()
            return
        if px.width() > _NOTE_IMAGE_PREVIEW_MAX or px.height() > _NOTE_IMAGE_PREVIEW_MAX:
            px = px.scaled(
                _NOTE_IMAGE_PREVIEW_MAX, _NOTE_IMAGE_PREVIEW_MAX,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
        self._current_name = name
        self.setPixmap(px)
        self.adjustSize()
        # Position below and to the right of cursor, clamped to screen
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
        self._current_name = None
        self.hide()


class _NoteTextEdit(QTextEdit):
    """QTextEdit with image paste support for notes.

    Pastes clipboard images into .storage/note_images/, inserts them
    inline in the document, and serializes to/from a text format using
    ``![image](filename.png)`` markers.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self._preview: Optional[_ImagePreviewPopup] = None
        self._pasted_images: set[str] = set()  # all images pasted in this session
        self._url_hl = _UrlHighlighter(self.document())
        self._clearing_anchor = False
        self.cursorPositionChanged.connect(self._clear_anchor_format)

    def _clear_anchor_format(self) -> None:
        """Prevent anchor formatting from bleeding into newly typed text."""
        if self._clearing_anchor:
            return
        cursor = self.textCursor()
        if not cursor.hasSelection() and cursor.charFormat().isAnchor():
            self._clearing_anchor = True
            cursor.setCharFormat(QTextCharFormat())
            self.setTextCursor(cursor)
            self._clearing_anchor = False

    def _image_name_at(self, pos: QPoint) -> Optional[str]:
        """Return the image filename at viewport position, or None."""
        cursor = self.cursorForPosition(pos)
        fmt = cursor.charFormat()
        if fmt.isImageFormat():
            name = fmt.toImageFormat().name()
            if name and _IMAGE_MARKER_RE.match(f'![image]({name})'):
                return name
        return None

    def _clickable_url_at(self, pos: QPoint) -> Optional[str]:
        """Return clickable URL at viewport position (anchor href or bare URL).

        Uses ``anchorAt`` rather than ``cursorForPosition().charFormat()``
        — the latter clamps to the nearest text position and then
        inherits the anchor format of the neighbouring character, so
        hovering past or beneath a link would falsely report it as
        clickable.  ``anchorAt`` only returns an href when the pixel
        is actually over the rendered anchor glyphs.
        """
        href = self.anchorAt(pos)
        if href:
            return href
        return _url_at_pos(self, pos)

    def mousePressEvent(self, event: 'QMouseEvent') -> None:
        if event.button() == Qt.LeftButton:
            url = self._clickable_url_at(event.pos())
            if url:
                _try_open_url(url)
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: 'QKeyEvent') -> None:  # type: ignore[override]
        # Cmd+B toggles bold on the current selection (or insertion point).
        # Qt maps Cmd → ControlModifier on macOS.  Mask keypad/fn bits.
        mods = event.modifiers() & (
            Qt.ControlModifier | Qt.ShiftModifier
            | Qt.AltModifier | Qt.MetaModifier)
        if event.key() == Qt.Key_B and mods == Qt.ControlModifier:
            self._toggle_bold()
            event.accept()
            return
        super().keyPressEvent(event)

    def _toggle_bold(self) -> None:
        """Flip bold on the selection, or on subsequent typing if no selection."""
        is_bold = self.fontWeight() >= QFont.Bold
        t = current_theme()
        fmt = QTextCharFormat()
        if is_bold:
            fmt.setFontWeight(QFont.Normal)
            fmt.setForeground(QColor(t.text_primary))
        else:
            fmt.setFontWeight(QFont.Black)
            fmt.setForeground(QColor(t.accent_orange))
        cursor = self.textCursor()
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        self.mergeCurrentCharFormat(fmt)

    def mouseMoveEvent(self, event: 'QMouseEvent') -> None:
        name = self._image_name_at(event.pos())
        if name:
            if self._preview is None:
                self._preview = _ImagePreviewPopup()
            self._preview.show_for_image(name, event.globalPos())
        elif self._preview and self._preview.isVisible():
            self._preview.hide_preview()
        if self._clickable_url_at(event.pos()):
            self.viewport().setCursor(Qt.PointingHandCursor)
        else:
            self.viewport().setCursor(Qt.IBeamCursor)
        super().mouseMoveEvent(event)

    def take_pasted_images(self) -> set[str]:
        """Return and clear the set of images pasted since last call."""
        imgs = self._pasted_images
        self._pasted_images = set()
        return imgs

    def leaveEvent(self, event: 'QEvent') -> None:
        if self._preview and self._preview.isVisible():
            self._preview.hide_preview()
        super().leaveEvent(event)

    def insertFromMimeData(self, source: QMimeData) -> None:
        """Override paste to handle clipboard images and strip rich text."""
        if source.hasImage():
            image = source.imageData()
            if isinstance(image, QImage) and not image.isNull():
                filename = _save_note_image(image)
                if filename:
                    self._pasted_images.add(filename)
                    self._insert_image(filename)
                    return
        # Force plain-text paste — clipboard HTML (e.g. browser links)
        # would otherwise leak rich formatting into the editor.
        if source.hasText():
            self.insertPlainText(source.text())
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

    @staticmethod
    def _insert_text_with_links(cursor: QTextCursor, text: str) -> None:
        """Insert plain text, rendering [text](url) as links and **text** as bold."""
        pos = 0
        for m in _INLINE_FORMAT_RE.finditer(text):
            if m.start() > pos:
                cursor.insertText(text[pos:m.start()])
            if m.group(1) is not None:
                # Markdown link
                cursor.insertText(m.group(1), _link_char_format(m.group(2)))
            else:
                # Bold
                bold_fmt = QTextCharFormat()
                bold_fmt.setFontWeight(QFont.Black)
                bold_fmt.setForeground(QColor(current_theme().accent_orange))
                cursor.insertText(m.group(3), bold_fmt)
            # Reset format so subsequent text has no inherited styling
            cursor.setCharFormat(QTextCharFormat())
            pos = m.end()
        if pos < len(text):
            cursor.insertText(text[pos:])

    def set_note_content(self, text: str) -> None:
        """Load note text, rendering markers as inline images and links."""
        self.clear()
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.Start)

        parts = _IMAGE_MARKER_RE.split(text)
        # parts alternates: [text, filename, text, filename, ...]
        for i, part in enumerate(parts):
            if i % 2 == 0:
                # Text segment — also render [text](url) links
                if part:
                    self._insert_text_with_links(cursor, part)
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

    def get_note_content(self, include_bold_markers: bool = True) -> str:
        """Serialize the document back to text with markers for images and links.

        Bold spans are delimited by ASCII STX/ETX control chars — see
        the module-level comment near ``_BOLD_START``.  When
        *include_bold_markers* is False, bold fragments emit plain text
        without any wrapper — used for "Run in Session" where the
        downstream AI does not need formatting metadata.
        """
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
                    elif fmt.isAnchor() and fmt.anchorHref():
                        result.append(
                            f'[{fragment.text()}]({fmt.anchorHref()})')
                    else:
                        txt = fragment.text()
                        if (txt and include_bold_markers
                                and fmt.fontWeight() >= QFont.Bold):
                            txt = f'{_BOLD_START}{txt}{_BOLD_END}'
                        result.append(txt)
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
    """Return note names (relative paths without .txt) sorted by mtime desc."""
    _migrate_old_notes_file()
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    files = [p for p in NOTES_DIR.rglob('*.txt') if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p.relative_to(NOTES_DIR).with_suffix('')) for p in files]


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

def _unwrap_bold(text: str) -> tuple[str, bool]:
    """If *text* is wrapped in STX/ETX, return (inner, True); else (text, False)."""
    if (len(text) >= 2 and text[0] == _BOLD_START
            and text[-1] == _BOLD_END):
        return text[1:-1], True
    return text, False


def _strip_markdown_links(text: str) -> str:
    """Return *text* with markdown ``[display](url)`` spans reduced to ``display``."""
    return _LINK_RE.sub(r'\1', text)


def _link_at_stripped_pos(raw: str, stripped_pos: int) -> Optional[str]:
    """If *stripped_pos* falls within a markdown link's display text in
    *raw*, return that link's URL.  Otherwise ``None``.

    Used so clicks on the rendered "word" in a checklist item's line
    edit (which hides the ``[…](url)`` syntax) can still open the link.
    """
    stripped_count = 0
    raw_pos = 0
    for m in _LINK_RE.finditer(raw):
        before_len = m.start() - raw_pos
        if stripped_count + before_len > stripped_pos:
            return None  # plain text before link
        stripped_count += before_len
        display_len = len(m.group(1))
        if stripped_count <= stripped_pos < stripped_count + display_len:
            return m.group(2)
        stripped_count += display_len
        raw_pos = m.end()
    return None


def _parse_checklist(text: str) -> list[dict]:
    """Parse markdown-style checklist text into item dicts.

    ``item['text']`` contains the RAW body (with any markdown link syntax
    preserved) — display stripping happens at render time, not here.
    """
    items: list[dict] = []
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in ('- [x]', '- [X]') or stripped.startswith('- [x] ') or stripped.startswith('- [X] '):
            raw = stripped[6:] if len(stripped) > 6 else ''
            item_text, bold = _unwrap_bold(raw)
            items.append({'text': item_text, 'checked': True, 'bold': bold})
        elif stripped == '- [ ]' or stripped.startswith('- [ ] '):
            raw = stripped[6:] if len(stripped) > 6 else ''
            item_text, bold = _unwrap_bold(raw)
            items.append({'text': item_text, 'checked': False, 'bold': bold})
        else:
            item_text, bold = _unwrap_bold(stripped)
            items.append({'text': item_text, 'checked': False, 'bold': bold})
    return items


def _serialize_checklist(items: list[dict]) -> str:
    """Serialize item dicts to markdown-style checklist text."""
    lines: list[str] = []
    for item in items:
        if not item['text'] and not item['checked']:
            continue  # skip empty unchecked items
        mark = 'x' if item['checked'] else ' '
        text = item['text']
        if item.get('bold') and text:
            text = f'{_BOLD_START}{text}{_BOLD_END}'
        lines.append(f'- [{mark}] {text}')
    return '\n'.join(lines)


# ── Folder helpers ──────────────────────────────────────────────────

def _list_folders() -> list[str]:
    """Return all folder paths relative to NOTES_DIR, sorted alphabetically."""
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    folders: list[str] = []
    for p in sorted(NOTES_DIR.rglob('*')):
        if p.is_dir():
            folders.append(str(p.relative_to(NOTES_DIR)))
    return folders


def _rename_folder_meta(old_prefix: str, new_prefix: str) -> None:
    """Update metadata keys when a folder is renamed."""
    meta = _load_notes_meta()
    updated: dict = {}
    for key, value in meta.items():
        if key.startswith(old_prefix + '/') or key == old_prefix:
            new_key = new_prefix + key[len(old_prefix):]
            updated[new_key] = value
        else:
            updated[key] = value
    if updated != meta:
        _save_notes_meta(updated)


def _delete_folder_meta(prefix: str) -> None:
    """Remove metadata entries for all notes under a folder."""
    meta = _load_notes_meta()
    keys = [k for k in meta if k.startswith(prefix + '/') or k == prefix]
    if keys:
        for k in keys:
            del meta[k]
        _save_notes_meta(meta)


# ── Item ordering ───────────────────────────────────────────────────

def _load_order() -> dict[str, list[str]]:
    """Load per-folder child ordering from metadata.

    Returns dict mapping folder paths ('' for root) to ordered lists
    of child leaf names (notes and subfolders mixed).
    """
    return _load_notes_meta().get('_order', {})


def _save_order(order: dict[str, list[str]]) -> None:
    """Persist per-folder child ordering."""
    meta = _load_notes_meta()
    if order:
        meta['_order'] = order
    else:
        meta.pop('_order', None)
    _save_notes_meta(meta)


def _rename_in_order(folder: str, old_leaf: str, new_leaf: str) -> None:
    """Rename an item in its parent folder's stored ordering."""
    order = _load_order()
    lst = order.get(folder, [])
    if old_leaf in lst:
        lst[lst.index(old_leaf)] = new_leaf
        order[folder] = lst
        _save_order(order)


def _remove_from_order(folder: str, leaf: str) -> None:
    """Remove *leaf* from *folder*'s stored order list."""
    order = _load_order()
    lst = order.get(folder, [])
    if leaf in lst:
        lst.remove(leaf)
        if lst:
            order[folder] = lst
        else:
            order.pop(folder, None)
        _save_order(order)


def _rename_order_keys(old_prefix: str, new_prefix: str) -> None:
    """Rename a folder's and its sub-folders' keys in the _order dict."""
    order = _load_order()
    changed = False
    if old_prefix in order:
        order[new_prefix] = order.pop(old_prefix)
        changed = True
    pfx = old_prefix + '/'
    for old_k in [k for k in order if k.startswith(pfx)]:
        order[new_prefix + old_k[len(old_prefix):]] = order.pop(old_k)
        changed = True
    if changed:
        _save_order(order)


def _delete_order_keys(prefix: str) -> None:
    """Delete a folder's and its sub-folders' keys from the _order dict."""
    order = _load_order()
    keys = [k for k in order if k == prefix or k.startswith(prefix + '/')]
    if keys:
        for k in keys:
            del order[k]
        _save_order(order)


# ══════════════════════════════════════════════════════════════════════
#  Checklist widgets (Google Keep style)
# ══════════════════════════════════════════════════════════════════════


def _text_is_rtl(text: str) -> Optional[bool]:
    """Return True if the first letter in *text* is RTL, False if LTR, None if no letter."""
    for ch in text:
        bidi = unicodedata.bidirectional(ch)
        if bidi in ('R', 'AL', 'AN'):
            return True
        if bidi == 'L':
            return False
    return None


def _apply_rtl_direction(widget: 'QWidget', text: str) -> None:
    """Set layout direction on a QLineEdit based on RTL detection of text."""
    rtl = _text_is_rtl(text)
    want = Qt.RightToLeft if rtl is True else Qt.LeftToRight
    if widget.layoutDirection() != want:
        widget.setLayoutDirection(want)

class _ItemLineEdit(QLineEdit):
    """QLineEdit that signals Enter and Backspace-when-empty.

    Shows full text as tooltip when truncated.  Emits ``expand_requested``
    on any click so the parent can swap in a wrapping editor.
    Supports pasting images as ``![image](hash.png)`` markers with hover preview.
    """

    enter_pressed: pyqtSignal = pyqtSignal()
    empty_backspace: pyqtSignal = pyqtSignal()
    expand_requested: pyqtSignal = pyqtSignal()
    image_pasted: pyqtSignal = pyqtSignal(str)  # emits the filename
    arrow_up: pyqtSignal = pyqtSignal()
    arrow_down: pyqtSignal = pyqtSignal()
    bold_toggle_requested: pyqtSignal = pyqtSignal()

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.setMouseTracking(True)
        self._preview: Optional[_ImagePreviewPopup] = None
        self._pasted_images: set[str] = set()
        self._register_image_fn: Optional[object] = None  # callback: filename → placeholder
        self._resolve_placeholder_fn: Optional[object] = None  # callback: placeholder → filename
        # One-shot flag: skip the next auto-expand on focus-in.  Used
        # when dismissing the popup sets focus back to this line edit —
        # without this, the focusInEvent handler would immediately
        # reopen the popup and the user could never cancel out.
        self._suppress_focus_expand: bool = False
        # Rich-text overlay so checklist items can show per-word link
        # styling (QLineEdit can only apply font attributes to the whole
        # widget, so mixed content like ``[word](url) tail`` would
        # otherwise underline the whole row).  Set by the parent
        # ``_ChecklistItemWidget`` when it owns this line edit; remains
        # None for the "Add item" field / non-checklist uses where there
        # is no raw markdown to render.
        self._rich_overlay: Optional['QLabel'] = None
        self.textChanged.connect(self._update_text_direction)
        self._update_text_direction(self.text())

    def _update_text_direction(self, text: str) -> None:
        """Set layout direction based on RTL/LTR content detection."""
        _apply_rtl_direction(self, text)

    def resizeEvent(self, event: 'QResizeEvent') -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._rich_overlay is not None:
            self._rich_overlay.setGeometry(self.rect())

    def _reset_cursor_to_start(self) -> None:
        """Move cursor so the visual start of the text is shown when not editing."""
        self.setCursorPosition(0)

    def mousePressEvent(self, event: 'QMouseEvent') -> None:  # type: ignore[override]
        win = self.window()
        if win and not win.isActiveWindow():
            QApplication.setActiveWindow(win)
        # Click on a URL opens it instead of expanding the editor.  For
        # checklist items, the line edit shows stripped text — so we
        # also map the click position back to the raw markdown and
        # check whether it landed inside a ``[display](url)`` span.
        # Plain left-click is enough: clicking on a styled link word
        # opens it; clicks elsewhere on the row fall through to the
        # expand-popup logic.
        if event.button() == Qt.LeftButton:
            url = _url_at_line_edit_pos(self, event.pos())
            if url:
                _try_open_url(url)
                return
            parent_item = self.parent()
            if (isinstance(parent_item, _ChecklistItemWidget)
                    and self._click_is_over_text(event.pos())):
                col = self.cursorPositionAt(event.pos())
                url = _link_at_stripped_pos(parent_item._raw_text, col)
                if url:
                    _try_open_url(url)
                    return
        super().mousePressEvent(event)
        # Always request expand on click — parent decides whether to swap
        self.expand_requested.emit()

    def _click_is_over_text(self, pos: 'QPoint') -> bool:
        """Is *pos* visually over a glyph (not empty padding to the side)?

        ``cursorPositionAt`` clamps to the nearest character when the
        click is past the text — so on its own it would let empty-space
        clicks open a link.  Compute the visible text rect from the
        font metrics + current alignment and reject anything outside.
        """
        text = self.text()
        if not text:
            return False
        fm = QFontMetrics(self.font())
        text_width = fm.horizontalAdvance(text)
        rect = self.contentsRect()
        alignment = self.alignment()
        if not alignment:
            alignment = (Qt.AlignLeft
                         if self.layoutDirection() == Qt.LeftToRight
                         else Qt.AlignRight)
        if alignment & Qt.AlignRight:
            text_left = rect.right() - text_width
            text_right = rect.right()
        elif alignment & Qt.AlignHCenter:
            text_left = rect.left() + (rect.width() - text_width) // 2
            text_right = text_left + text_width
        else:
            text_left = rect.left() + 2  # small inner padding
            text_right = text_left + text_width
        return text_left <= pos.x() < text_right

    def focusInEvent(self, event: 'QFocusEvent') -> None:  # type: ignore[override]
        super().focusInEvent(event)
        # Honour a one-shot suppression — the popup's dismiss path sets
        # this when it wants to hand focus back without re-opening.
        if self._suppress_focus_expand:
            self._suppress_focus_expand = False
            return
        # Only auto-expand on Tab navigation.  Mouse clicks go through
        # mousePressEvent (which gets first crack at URL detection
        # before falling through to expand_requested).  Programmatic
        # focus from ``focus_edit`` / Escape-dismiss already handles the
        # popup explicitly — auto-expanding here would reopen the popup
        # the user just closed.
        if event.reason() != Qt.TabFocusReason:
            return
        parent_item = self.parent()
        if (isinstance(parent_item, _ChecklistItemWidget)
                and self.isReadOnly()
                and parent_item._popup is None):
            self.expand_requested.emit()

    def mouseMoveEvent(self, event: 'QMouseEvent') -> None:  # type: ignore[override]
        name = self._image_marker_name_at(event.pos())
        if name:
            if self._preview is None:
                self._preview = _ImagePreviewPopup()
            self._preview.show_for_image(name, event.globalPos())
        elif self._preview and self._preview.isVisible():
            self._preview.hide_preview()
        over_link = bool(_url_at_line_edit_pos(self, event.pos()))
        # Checklist items hide the ``[display](url)`` syntax in the line
        # edit — map the click position back to the raw markdown so the
        # cursor reports "hand" while hovering the rendered link word.
        if not over_link:
            parent_item = self.parent()
            if (isinstance(parent_item, _ChecklistItemWidget)
                    and self._click_is_over_text(event.pos())):
                col = self.cursorPositionAt(event.pos())
                if _link_at_stripped_pos(parent_item._raw_text, col):
                    over_link = True
        self.setCursor(Qt.PointingHandCursor if over_link else Qt.IBeamCursor)
        super().mouseMoveEvent(event)

    def focusOutEvent(self, event: 'QFocusEvent') -> None:  # type: ignore[override]
        super().focusOutEvent(event)
        self._reset_cursor_to_start()

    def leaveEvent(self, event: 'QEvent') -> None:  # type: ignore[override]
        if self._preview and self._preview.isVisible():
            self._preview.hide_preview()
        self.setCursor(Qt.IBeamCursor)
        super().leaveEvent(event)

    def _image_marker_name_at(self, pos: QPoint) -> Optional[str]:
        """Return image filename if cursor is over an image placeholder or marker."""
        col = self.cursorPositionAt(pos)
        text = self.text()
        # Check [Image #N] placeholders (displayed in checklist mode)
        if self._resolve_placeholder_fn:
            for m in _CHECKLIST_PLACEHOLDER_RE.finditer(text):
                if m.start() <= col < m.end():
                    filename = self._resolve_placeholder_fn(m.group())
                    if filename and (NOTE_IMAGES_DIR / filename).is_file():
                        return filename
        # Check ![image](hash.png) markers (fallback)
        for m in _IMAGE_MARKER_RE.finditer(text):
            if m.start() <= col < m.end():
                filename = m.group(1)
                if (NOTE_IMAGES_DIR / filename).is_file():
                    return filename
        return None

    def keyPressEvent(self, event: 'QKeyEvent') -> None:  # type: ignore[override]
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.enter_pressed.emit()
            return
        if event.key() == Qt.Key_Escape:
            self.clearFocus()
            return
        if event.key() == Qt.Key_Up:
            self.arrow_up.emit()
            return
        if event.key() == Qt.Key_Down:
            self.arrow_down.emit()
            return
        if event.key() == Qt.Key_Backspace and not self.text():
            self.empty_backspace.emit()
            return
        # Cmd+B — toggle bold on the parent checklist item
        mods_masked = event.modifiers() & (
            Qt.ControlModifier | Qt.ShiftModifier
            | Qt.AltModifier | Qt.MetaModifier)
        if (event.key() == Qt.Key_B
                and mods_masked == Qt.ControlModifier):
            self.bold_toggle_requested.emit()
            event.accept()
            return
        # Cmd+V / Ctrl+V — check clipboard for images
        if (event.key() == Qt.Key_V
                and event.modifiers() & Qt.ControlModifier):
            clipboard = QApplication.clipboard()
            mime = clipboard.mimeData()
            if mime and mime.hasImage():
                image = mime.imageData()
                if isinstance(image, QImage) and not image.isNull():
                    filename = _save_note_image(image)
                    if filename:
                        self._pasted_images.add(filename)
                        if self._register_image_fn:
                            placeholder = self._register_image_fn(filename)
                        else:
                            placeholder = f'![image]({filename})'
                        self.insert(placeholder)
                        self.image_pasted.emit(filename)
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
    focus_prev: pyqtSignal = pyqtSignal(int)  # arrow up
    focus_next: pyqtSignal = pyqtSignal(int)  # arrow down
    bold_changed: pyqtSignal = pyqtSignal(int, bool)

    def __init__(
        self, index: int, text: str, checked: bool,
        bold: bool = False, font_size: Optional[int] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._index = index
        self._checked = checked
        self._bold = bold
        # Content font size (from the Notes content zoom pref).  Stored
        # so the item's own stylesheet can include font-size — ancestor
        # QSS does not reliably cascade font-size into a widget that
        # already has its own stylesheet.
        self._font_size: Optional[int] = font_size
        # Raw text (may contain markdown link syntax).  The line edit
        # shows a *stripped* version; the raw is the source of truth and
        # is what gets persisted + round-tripped through the popup.
        self._raw_text: str = text
        self._popup: Optional[QTextEdit] = None

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

        # Display stripped text (markdown hidden).  The raw form lives
        # in self._raw_text; the popup is the only editor that sees it.
        self._edit = _ItemLineEdit(_strip_markdown_links(text))
        self._edit.setFrame(False)
        # Read-only: typing always routes through the rich-text popup,
        # so the raw markdown stays intact.  Focus auto-expands.
        self._edit.setReadOnly(True)
        # Rich-text overlay for per-word link styling — the label sits
        # on top of the line edit, shows HTML with only the link span
        # underlined+blue, and passes mouse events through to the line
        # edit below.  The line edit's own text is hidden by making it
        # transparent when the overlay is in use (see
        # ``_sync_rich_overlay``).  Popup edit mode hides ``self._edit``
        # which also hides the overlay (it's a child of the line edit).
        self._rich_overlay = QLabel(self._edit)
        self._rich_overlay.setTextFormat(Qt.RichText)
        self._rich_overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
        # 2px left padding matches QLineEdit's internal text offset so
        # the overlay text lines up with the (invisible) line edit text.
        self._rich_overlay.setContentsMargins(2, 0, 2, 0)
        self._rich_overlay.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self._rich_overlay.setStyleSheet('background: transparent;')
        self._rich_overlay.setGeometry(self._edit.rect())
        self._edit._rich_overlay = self._rich_overlay
        # text_edited is intentionally NOT wired from textChanged —
        # the line edit is display-only.  Changes flow from the popup's
        # dismiss path (_dismiss_popup_if_active / on_focus_out).
        self._edit.enter_pressed.connect(
            lambda: self.new_item_after.emit(self._index),
        )
        self._edit.expand_requested.connect(self._show_expand_popup)
        self._edit.empty_backspace.connect(
            lambda: self.merge_up.emit(self._index),
        )
        self._edit.arrow_up.connect(
            lambda: self.focus_prev.emit(self._index),
        )
        self._edit.arrow_down.connect(
            lambda: self.focus_next.emit(self._index),
        )
        self._edit.bold_toggle_requested.connect(self._toggle_bold)
        row.addWidget(self._edit, 1)

        self._del_btn = QPushButton('\u00d7')
        self._del_btn.setFixedSize(20, 20)
        t = current_theme()
        self._del_btn.setStyleSheet(
            f'QPushButton {{ border: none; color: {t.text_muted}; }}'
            f'QPushButton:hover {{ color: {t.accent_red}; }}'
        )
        self._del_btn.setVisible(False)
        self._del_btn.clicked.connect(
            lambda: self.delete_requested.emit(self._index),
        )
        row.addWidget(self._del_btn, 0, Qt.AlignVCenter)

        self._apply_checked_style(checked)
        self._apply_bold_style()
        self.setStyleSheet(
            f'_ChecklistItemWidget {{ border-bottom: 1px solid {current_theme().border_subtle}; }}'
        )

        # Show the start of the text (not the end) when the item is first laid out.
        QTimer.singleShot(0, self._edit._reset_cursor_to_start)

    def _apply_checked_style(self, checked: bool) -> None:
        font = self._edit.font()
        font.setStrikeOut(checked)
        self._edit.setFont(font)
        t = current_theme()
        self._edit.setStyleSheet(self._compose_edit_style(
            t, checked, self._bold, has_url=False,
            font_size=self._font_size,
            text_transparent=self._has_link_display()))
        self._checked = checked
        self._sync_rich_overlay()

    def _apply_bold_style(self) -> None:
        """Apply bold weight + keep widget-level underline off.

        Per-word link styling is rendered via ``_rich_overlay`` (QLabel
        with HTML) — QLineEdit can only apply font attributes to the
        whole widget, so mixed content like ``[word](url) tail`` can't
        be expressed on the line edit itself.  The overlay handles link
        styling; here we only apply bold weight to the line edit's
        font (the overlay mirrors bold in its HTML too).
        """
        font = self._edit.font()
        font.setWeight(QFont.Black if self._bold else QFont.Normal)
        font.setUnderline(False)
        self._edit.setFont(font)
        t = current_theme()
        self._edit.setStyleSheet(
            self._compose_edit_style(
                t, self._checked, self._bold,
                has_url=False, font_size=self._font_size,
                text_transparent=self._has_link_display()))
        self._update_link_tooltip()
        self._sync_rich_overlay()

    def _sync_rich_overlay(self) -> None:
        """Render the item text with per-link styling on the overlay label.

        Emits HTML where each ``[display](url)`` span is rendered as an
        underlined + accent-blue run, plain text stays as ``text_primary``
        (or ``text_muted`` when the item is checked / ``accent_orange``
        when bolded).  When ``_has_link_display`` is False, the overlay
        is hidden and the line edit shows its own text — a plain-text
        item doesn't need the overlay at all.
        """
        if self._rich_overlay is None:
            return
        if not self._has_link_display():
            self._rich_overlay.hide()
            return
        # Match the line edit's current font so the overlay text has
        # the same metrics (especially bold/strikeOut reflected on the
        # line edit font).  Without this the overlay defaults to the
        # Qt application font and visibly drifts from the cursor + any
        # selection the QLineEdit still shows during focus.
        self._rich_overlay.setFont(self._edit.font())
        t = current_theme()
        if self._checked:
            base_color = t.text_muted
        elif self._bold:
            base_color = t.accent_orange
        else:
            base_color = t.text_primary
        link_color = t.accent_blue
        # Walk the raw text: emit link spans as underlined+blue, other
        # text in ``base_color``.  Escape HTML-specials to avoid
        # accidental tag interpretation.
        # CSS ``text-decoration`` isn't inherited — the link span
        # replaces the parent span's value — so include ``line-through``
        # explicitly on the link when the item is checked, otherwise
        # the whole row is struck through *except* the link word.
        link_decoration = ('underline line-through'
                           if self._checked else 'underline')
        parts: list[str] = []
        pos = 0
        for m in _LINK_RE.finditer(self._raw_text):
            if m.start() > pos:
                parts.append(html.escape(self._raw_text[pos:m.start()]))
            display = html.escape(m.group(1))
            parts.append(
                f'<span style="color:{link_color};'
                f' text-decoration:{link_decoration}">{display}</span>'
            )
            pos = m.end()
        if pos < len(self._raw_text):
            parts.append(html.escape(self._raw_text[pos:]))
        weight = 'bold' if self._bold else 'normal'
        strike = ' text-decoration:line-through;' if self._checked else ''
        # Wrap the whole string in a span that sets the base color +
        # strike (link spans override color+underline).
        size_rule = (f' font-size:{self._font_size}pt;'
                     if self._font_size else '')
        self._rich_overlay.setText(
            f'<span style="color:{base_color}; font-weight:{weight};'
            f' font-family:Menlo;{size_rule}{strike}">'
            f'{"".join(parts)}</span>'
        )
        self._rich_overlay.setGeometry(self._edit.rect())
        self._rich_overlay.show()

    def _has_link_display(self) -> bool:
        """True if the raw text contains any markdown link span."""
        return _LINK_RE.search(self._raw_text) is not None

    def set_font_size(self, pt: int) -> None:
        """Update the item's content font size and re-apply QSS styles."""
        if pt == self._font_size:
            return
        self._font_size = pt
        # Re-apply styles so the new size is baked into the QSS.
        self._apply_checked_style(self._checked)
        self._apply_bold_style()

    def set_raw_text(self, raw: str) -> None:
        """Update the backing raw text + refresh the line edit display.

        The popup's dismiss path calls this with its toPlainText()
        result; the line edit then shows the stripped form.  Emits
        ``text_edited`` so the parent checklist persists the change.
        """
        if raw == self._raw_text:
            return
        self._raw_text = raw
        self._edit.setText(_strip_markdown_links(raw))
        # Refresh styling (link color/underline depends on markdown presence).
        self._apply_checked_style(self._checked)
        self._apply_bold_style()
        self._update_link_tooltip()
        self.text_edited.emit(self._index, raw)

    def _update_link_tooltip(self) -> None:
        """Show the first link's URL in the tooltip + mark Cmd+click UX."""
        urls = _LINK_RE.findall(self._raw_text)
        if urls:
            first_url = urls[0][1]
            extra = f' (+{len(urls) - 1} more)' if len(urls) > 1 else ''
            self._edit.setToolTip(f'Click a link to open: {first_url}{extra}')
        else:
            self._edit.setToolTip('')

    @staticmethod
    def _compose_edit_style(
        t: object, checked: bool, bold: bool, has_url: bool = False,
        font_size: Optional[int] = None, text_transparent: bool = False,
    ) -> str:
        """Assemble the QSS for the item's line edit based on its flags.

        ``font_size`` is baked into the rule so ancestor QSS font-size
        (from ``_ChecklistWidget.set_font_size``) is not lost to Qt's
        unreliable cascade for widgets with their own stylesheet.

        When ``text_transparent`` is True the line edit's own text is
        hidden — used while the rich-text overlay label handles display
        so the underlying QLineEdit text doesn't bleed through.
        """
        if text_transparent:
            color = 'transparent'
        elif checked:
            color = t.text_muted
        elif has_url:
            color = t.accent_blue
        elif bold:
            color = t.accent_orange
        else:
            color = t.text_primary
        size_rule = f' font-size: {font_size}pt;' if font_size else ''
        return (
            f'QLineEdit {{ color: {color}; background: transparent;'
            f'{size_rule} }}')

    def _toggle_bold(self) -> None:
        """Flip the item's bold flag and report to the parent checklist."""
        self._bold = not self._bold
        self._apply_bold_style()
        # If the expand popup is open, mirror the font + color there too —
        # otherwise the user toggling bold inside the popup would see no
        # change until dismissal.
        if self._popup is not None:
            popup_font = self._popup.font()
            popup_font.setWeight(QFont.Black if self._bold else QFont.Normal)
            self._popup.setFont(popup_font)
            t = current_theme()
            color = (t.text_muted if self._checked
                     else t.accent_orange if self._bold
                     else t.text_primary)
            # Preserve the baked-in font-size so bold toggle doesn't
            # shrink the popup back to Qt default (widget stylesheet
            # blocks ancestor font-size cascade).
            size_css = (f' font-size: {self._font_size}pt; font-family: Menlo;'
                        if self._font_size else '')
            self._popup.setStyleSheet(
                f'QTextEdit {{ color: {color}; background: transparent;'
                f' border: 1px solid {t.text_secondary};{size_css} }}'
            )
        self.bold_changed.emit(self._index, self._bold)

    def focus_edit(self, cursor_at_end: bool = True) -> None:
        """Focus this item's text field and expand into wrapping editor."""
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

    @staticmethod
    def _serialize_popup_markdown(wrap: QTextEdit) -> str:
        """Walk the popup's document, emitting raw markdown text.

        The popup renders ``[text](url)`` spans as anchor-format
        fragments (blue + underlined).  To round-trip back to disk we
        need the markdown form: anchors → ``[text](url)``, plain
        fragments → their own text.
        """
        doc = wrap.document()
        parts: list[str] = []
        block = doc.begin()
        first = True
        while block.isValid():
            if not first:
                parts.append(' ')  # multi-line collapses to space
            first = False
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    fmt = frag.charFormat()
                    if fmt.isAnchor() and fmt.anchorHref():
                        parts.append(f'[{frag.text()}]({fmt.anchorHref()})')
                    else:
                        parts.append(frag.text())
                it += 1
            block = block.next()
        return ''.join(parts)

    def _dismiss_popup_if_active(self) -> None:
        """Dismiss this item's popup if it's open."""
        if self._popup is None:
            return
        wrap = self._popup
        self._popup = None
        if _ChecklistItemWidget._active_expand is self:
            _ChecklistItemWidget._active_expand = None
        new_text = ''
        if not sip.isdeleted(wrap):
            new_text = self._serialize_popup_markdown(wrap)
            wrap.setVisible(False)
            self.layout().removeWidget(wrap)
            wrap.deleteLater()
        if not sip.isdeleted(self._edit):
            self._edit.setVisible(True)
            # The popup edits the RAW markdown text.  Sync it back and
            # let the line edit re-render the stripped display.
            if new_text and new_text != self._raw_text:
                self.set_raw_text(new_text)

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

        wrap = QTextEdit()
        wrap.setFrameShape(QFrame.NoFrame)
        wrap.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrap.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrap.setTabChangesFocus(True)
        wrap.setAcceptRichText(False)
        wrap.document().setDocumentMargin(2)
        wrap.setFont(self._edit.font())
        t = current_theme()
        color = (t.text_muted if self._checked
                 else t.accent_orange if self._bold
                 else t.text_primary)
        # Bake font-size into the widget's own stylesheet — a widget QSS
        # that declares any property blocks the ancestor cascade for
        # font-size in Qt, so we must specify it explicitly here.
        size_css = (f' font-size: {self._font_size}pt; font-family: Menlo;'
                    if self._font_size else '')
        wrap.setStyleSheet(
            f'QTextEdit {{ color: {color}; background: transparent;'
            f' border: 1px solid {t.text_secondary};{size_css} }}'
        )
        # Force plain-text paste — clipboard HTML (e.g. browser links)
        # would otherwise leak rich formatting into the plain-text editor.
        wrap.insertFromMimeData = lambda src: wrap.insertPlainText(
            src.text()) if src.hasText() else None
        # Render the raw markdown as rich text (anchor-styled links),
        # not visible ``[word](url)`` syntax.  On dismiss we serialize
        # the document back to markdown (_serialize_popup_markdown).
        cursor = wrap.textCursor()
        cursor.movePosition(QTextCursor.Start)
        _NoteTextEdit._insert_text_with_links(cursor, self._raw_text)

        # Stop the cursor's anchor format from bleeding into subsequent
        # typing — same mechanism _NoteTextEdit uses for text notes.
        def _clear_anchor_on_cursor_move() -> None:
            c = wrap.textCursor()
            if not c.hasSelection() and c.charFormat().isAnchor():
                c.setCharFormat(QTextCharFormat())
                wrap.setTextCursor(c)
        wrap.cursorPositionChanged.connect(_clear_anchor_on_cursor_move)

        # Pin the height to match the line edit BEFORE the widget is
        # inserted — otherwise Qt uses QTextEdit's default sizeHint
        # (~150-200px) for layout and the row visibly stretches until
        # resize_wrap's deferred call shrinks it.
        line_h = self._edit.sizeHint().height() or self._edit.height() or 24
        wrap.setFixedHeight(line_h)

        self._edit.setVisible(False)
        row_layout.insertWidget(edit_idx + 1, wrap, 1)
        self._popup = wrap

        def resize_wrap() -> None:
            if self._popup is not wrap:
                return
            # ``document().size().height()`` returns surprisingly large
            # values during Qt's initial layout pass (before the widget's
            # viewport has been sized) — using it here made the row
            # visibly balloon on entering edit mode.  Compute height
            # deterministically from font metrics × visual line count.
            fm = QFontMetrics(wrap.font())
            line_count = 0
            block = wrap.document().firstBlock()
            while block.isValid():
                layout = block.layout()
                if layout is not None and layout.lineCount() > 0:
                    line_count += layout.lineCount()
                else:
                    line_count += 1
                block = block.next()
            line_count = max(1, line_count)
            margin = wrap.document().documentMargin()
            # ``documentMargin()`` returns qreal (float in Python), so
            # cast to int before ``setFixedHeight`` which is int-only.
            content_h = int(fm.lineSpacing() * line_count + margin * 2)
            # +4 for the 1px border top/bottom plus descent safety.
            wrap.setFixedHeight(max(line_h, content_h + 4))

        def dismiss(save: bool) -> None:
            if self._popup is not wrap:
                return
            self._popup = None
            if _ChecklistItemWidget._active_expand is self:
                _ChecklistItemWidget._active_expand = None
            if sip.isdeleted(wrap):
                return
            new_text = (self._serialize_popup_markdown(wrap)
                        if save else None)
            wrap.setVisible(False)
            row_layout.removeWidget(wrap)
            wrap.deleteLater()
            if sip.isdeleted(self._edit):
                return
            self._edit.setVisible(True)
            if new_text is not None and new_text != self._raw_text:
                self.set_raw_text(new_text)

        def on_key(event: 'QKeyEvent') -> None:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                dismiss(True)
                self.new_item_after.emit(self._index)
                return
            if event.key() == Qt.Key_Escape:
                dismiss(False)
                # Escape means "cancel out" — don't let focusInEvent
                # auto-reopen the popup we just closed.
                self._edit._suppress_focus_expand = True
                self._edit.setFocus()
                return
            # Arrow up/down: try moving within visual lines first;
            # only navigate to adjacent item if the cursor can't move.
            if event.key() == Qt.Key_Up:
                cur = wrap.textCursor()
                pos_before = cur.position()
                cur.movePosition(QTextCursor.Up)
                if cur.position() == pos_before:
                    dismiss(True)
                    self.focus_prev.emit(self._index)
                    return
                wrap.setTextCursor(cur)
                return
            if event.key() == Qt.Key_Down:
                cur = wrap.textCursor()
                pos_before = cur.position()
                cur.movePosition(QTextCursor.Down)
                if cur.position() == pos_before:
                    dismiss(True)
                    self.focus_next.emit(self._index)
                    return
                wrap.setTextCursor(cur)
                return
            if event.key() == Qt.Key_Backspace and not wrap.toPlainText():
                dismiss(True)
                self.merge_up.emit(self._index)
                return
            # Cmd+B — toggle bold on the whole item (QLineEdit can't do
            # per-character bold, so bold is an item-level flag).
            mods_masked = event.modifiers() & (
                Qt.ControlModifier | Qt.ShiftModifier
                | Qt.AltModifier | Qt.MetaModifier)
            if (event.key() == Qt.Key_B
                    and mods_masked == Qt.ControlModifier):
                self._toggle_bold()
                return
            # Cmd+V / Ctrl+V — paste image
            if (event.key() == Qt.Key_V
                    and event.modifiers() & Qt.ControlModifier):
                clipboard = QApplication.clipboard()
                mime = clipboard.mimeData()
                if mime and mime.hasImage():
                    image = mime.imageData()
                    if isinstance(image, QImage) and not image.isNull():
                        filename = _save_note_image(image)
                        if filename:
                            self._edit._pasted_images.add(filename)
                            self._edit.image_pasted.emit(filename)
                            if self._edit._register_image_fn:
                                placeholder = self._edit._register_image_fn(filename)
                            else:
                                placeholder = f'![image]({filename})'
                            wrap.insertPlainText(placeholder)
                            self.text_edited.emit(self._index, self._serialize_popup_markdown(wrap))
                            QTimer.singleShot(0, resize_wrap)
                            return
            # Preempt anchor-format bleed: if the cursor is sitting at
            # the trailing edge of an anchor span and the next key will
            # insert text, reset the char format so the new character
            # lands with default styling (not underlined/blue).  The
            # deferred ``cursorPositionChanged`` handler runs after
            # insertion, which is too late — by then the new character
            # has already inherited the anchor's format.
            if event.text() and event.text().isprintable():
                cur = wrap.textCursor()
                if not cur.hasSelection():
                    cf = cur.charFormat()
                    if cf.isAnchor() or cf.fontUnderline():
                        cur.setCharFormat(QTextCharFormat())
                        wrap.setTextCursor(cur)
            QTextEdit.keyPressEvent(wrap, event)
            self.text_edited.emit(self._index, self._serialize_popup_markdown(wrap))
            QTimer.singleShot(0, resize_wrap)

        def on_focus_out(event: 'QFocusEvent') -> None:
            try:
                QTextEdit.focusOutEvent(wrap, event)
            except RuntimeError:
                return
            QTimer.singleShot(0, lambda: dismiss(True))

        _setup_textedit_image_hover(wrap, self._edit._resolve_placeholder_fn)
        _setup_textedit_url_click(wrap)
        wrap._url_hl = _UrlHighlighter(wrap.document())
        wrap.keyPressEvent = on_key
        wrap.focusOutEvent = on_focus_out

        QTimer.singleShot(0, resize_wrap)
        wrap.setFocus()
        cursor = wrap.textCursor()
        cursor.movePosition(cursor.End)
        wrap.setTextCursor(cursor)


def _setup_textedit_url_click(wrap: QTextEdit) -> None:
    """Add click-to-open URL and pointer cursor to a QTextEdit."""
    wrap.setMouseTracking(True)
    wrap.viewport().setMouseTracking(True)
    _orig_press = wrap.mousePressEvent
    _orig_move = wrap.mouseMoveEvent

    def on_press(event: 'QMouseEvent') -> None:
        if event.button() == Qt.LeftButton:
            url = _url_at_pos(wrap, event.pos())
            if url:
                _try_open_url(url)
                return
        _orig_press(event)

    def on_move(event: 'QMouseEvent') -> None:
        if _url_at_pos(wrap, event.pos()):
            wrap.viewport().setCursor(Qt.PointingHandCursor)
        else:
            wrap.viewport().setCursor(Qt.IBeamCursor)
        _orig_move(event)

    wrap.mousePressEvent = on_press
    wrap.mouseMoveEvent = on_move


def _setup_textedit_image_hover(
    wrap: QTextEdit,
    resolve_placeholder_fn: Optional[object] = None,
) -> None:
    """Add image hover preview to a QTextEdit via monkey-patching."""
    wrap.setMouseTracking(True)
    wrap.viewport().setMouseTracking(True)
    _preview_ref: list[Optional[_ImagePreviewPopup]] = [None]

    def on_mouse_move(event: 'QMouseEvent') -> None:
        cursor = wrap.cursorForPosition(event.pos())
        block_text = cursor.block().text()
        col = cursor.positionInBlock()
        name: Optional[str] = None
        # Check [Image #N] placeholders
        if resolve_placeholder_fn:
            for m in _CHECKLIST_PLACEHOLDER_RE.finditer(block_text):
                if m.start() <= col < m.end():
                    fname = resolve_placeholder_fn(m.group())
                    if fname and (NOTE_IMAGES_DIR / fname).is_file():
                        name = fname
                    break
        # Check ![image](hash.png) markers
        if name is None:
            for m in _IMAGE_MARKER_RE.finditer(block_text):
                if m.start() <= col < m.end():
                    fname = m.group(1)
                    if (NOTE_IMAGES_DIR / fname).is_file():
                        name = fname
                    break
        if name:
            if _preview_ref[0] is None:
                _preview_ref[0] = _ImagePreviewPopup()
            _preview_ref[0].show_for_image(name, event.globalPos())
        elif _preview_ref[0] and _preview_ref[0].isVisible():
            _preview_ref[0].hide_preview()
        QTextEdit.mouseMoveEvent(wrap, event)

    def on_leave(event: 'QEvent') -> None:
        if _preview_ref[0] and _preview_ref[0].isVisible():
            _preview_ref[0].hide_preview()
        QTextEdit.leaveEvent(wrap, event)

    wrap.mouseMoveEvent = on_mouse_move
    wrap.leaveEvent = on_leave


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
        self._pasted_images: set[str] = set()  # track images pasted in checklist
        self._undo_stack: Optional['NotesUndoStack'] = None
        self._cmd_ctx: Optional['NotesCmdContext'] = None
        self._image_counter: int = 0
        # Maps "[Image #N]" ↔ filename for display/storage conversion
        self._placeholder_to_file: dict[str, str] = {}
        self._file_to_placeholder: dict[str, str] = {}
        # Current content-zoom font size — passed to every item widget
        # on creation so the size is baked into the item's own QSS
        # (ancestor QSS font-size does not reliably cascade into a
        # widget that has its own stylesheet).  Updated via
        # set_font_size() when the user zooms.
        self._current_font_size: int = current_theme().font_size_base

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

    def set_font_size(self, pt: int) -> None:
        """Update font size on all item editors and the add field."""
        self._current_font_size = pt
        self._container.setStyleSheet(
            f'QLineEdit, QTextEdit {{ font-size: {pt}pt;'
            f' font-family: Menlo; }}'
        )
        # Each item has its own QSS (for color/strike/etc.), which blocks
        # ancestor font-size from cascading in — propagate to each item
        # directly so it bakes the size into its own stylesheet.
        for item in self.findChildren(_ChecklistItemWidget):
            item.set_font_size(pt)
        # The "Add item" row also has its own widget stylesheet (padding +
        # transparent background) that blocks the container's font-size
        # cascade — bake it into the widget's own QSS so it tracks zoom.
        add_field = getattr(self, '_add_field', None)
        if add_field is not None:
            existing = add_field.styleSheet() or ''
            cleaned = re.sub(r'\s*font-size:\s*[^;]+;\s*', '', existing)
            add_field.setStyleSheet(
                f'{cleaned} font-size: {pt}pt; font-family: Menlo;'.strip())

    def set_items(self, items: list[dict]) -> None:
        """Load items and rebuild the UI."""
        # Reset image mapping for new note
        self._image_counter = 0
        self._placeholder_to_file.clear()
        self._file_to_placeholder.clear()
        # Convert ![image](hash.png) markers to [Image #N] for display
        self._items = []
        for i in items:
            d = dict(i)
            d['text'] = self._markers_to_placeholders(d['text'])
            self._items.append(d)
        self._rebuild()

    def get_items(self) -> list[dict]:
        """Return items with [Image #N] converted back to ![image](hash.png)."""
        result = []
        for i in self._items:
            d = dict(i)
            d['text'] = self._placeholders_to_markers(d['text'])
            result.append(d)
        return result

    def take_pasted_images(self) -> set[str]:
        """Return and clear all images pasted in checklist items."""
        imgs = self._pasted_images
        self._pasted_images = set()
        return imgs

    def set_undo_stack(self, stack: 'NotesUndoStack',
                       ctx: 'NotesCmdContext') -> None:
        """Attach an undo stack so checklist mutations are recorded."""
        self._undo_stack = stack
        self._cmd_ctx = ctx

    def _register_image(self, filename: str) -> str:
        """Register a filename and return its [Image #N] placeholder."""
        if filename in self._file_to_placeholder:
            return self._file_to_placeholder[filename]
        self._image_counter += 1
        placeholder = f'[Image #{self._image_counter}]'
        self._placeholder_to_file[placeholder] = filename
        self._file_to_placeholder[filename] = placeholder
        return placeholder

    def _markers_to_placeholders(self, text: str) -> str:
        """Convert ![image](hash.png) markers to [Image #N] for display."""
        def _replace(m: re.Match) -> str:
            return self._register_image(m.group(1))
        return _IMAGE_MARKER_RE.sub(_replace, text)

    def _placeholders_to_markers(self, text: str) -> str:
        """Convert [Image #N] placeholders back to ![image](hash.png) for storage."""
        def _replace(m: re.Match) -> str:
            placeholder = m.group()
            filename = self._placeholder_to_file.get(placeholder)
            return f'![image]({filename})' if filename else placeholder
        return _CHECKLIST_PLACEHOLDER_RE.sub(_replace, text)

    def _resolve_placeholder(self, placeholder: str) -> Optional[str]:
        """Return the filename for a [Image #N] placeholder, or None."""
        return self._placeholder_to_file.get(placeholder)

    # ── Layout rebuild ───────────────────────────────────────────────

    def _clear_layout(self) -> None:
        # Move focus to the scroll area BEFORE destroying children —
        # if the focused widget is destroyed, macOS deactivates the
        # window and subsequent setFocus() calls silently fail.
        self._scroll.setFocus()
        # Collect pasted images from items being destroyed
        for i in range(self._layout.count()):
            item = self._layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if isinstance(w, _ChecklistItemWidget):
                self._pasted_images |= w._edit._pasted_images
        if self._add_field:
            self._pasted_images |= self._add_field._pasted_images
        # Reset class-level active expand — widgets are about to be destroyed
        _ChecklistItemWidget._active_expand = None
        self._add_popup = None
        while self._layout.count():
            child = self._layout.takeAt(0)
            w = child.widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _rebuild(self) -> None:
        # Preserve the vertical scroll offset across the rebuild so toggling
        # a checkbox in a long checklist doesn't snap the view back to top.
        # Skip preservation when a rebuild is expected to move focus to a
        # specific widget (new item, re-added row) — in those cases Qt
        # auto-scrolls to the focused widget and we shouldn't override it.
        preserve_scroll = (
            self._focus_after_rebuild is None
            and not self._focus_add_after_rebuild)
        scroll_bar = self._scroll.verticalScrollBar()
        scroll_pos = (scroll_bar.value()
                      if (scroll_bar is not None and preserve_scroll) else 0)
        self._clear_layout()

        active = [(i, d) for i, d in enumerate(self._items) if not d['checked']]
        completed = [(i, d) for i, d in enumerate(self._items) if d['checked']]

        focus_widget: Optional[_ChecklistItemWidget] = None
        focus_at_end = True

        # Active (unchecked) items
        for list_idx, data in active:
            w = self._make_item_widget(
                list_idx, data['text'], False, data.get('bold', False))
            self._layout.addWidget(w)
            if (self._focus_after_rebuild is not None
                    and self._focus_after_rebuild[0] == list_idx):
                focus_widget = w
                focus_at_end = self._focus_after_rebuild[1]

        # "Add item" field
        self._add_field = _ItemLineEdit()
        self._add_field.setPlaceholderText('Add item')
        self._add_field.image_pasted.connect(lambda fn: self._pasted_images.add(fn))
        self._add_field._register_image_fn = self._register_image
        self._add_field._resolve_placeholder_fn = self._resolve_placeholder
        self._add_field.setFrame(False)
        self._add_field.setStyleSheet(
            f'QLineEdit {{ padding: 6px 4px; background: transparent;'
            f' font-size: {self._current_font_size}pt;'
            f' font-family: Menlo; }}'
        )
        self._add_field.enter_pressed.connect(self._on_add_item)
        self._add_field.expand_requested.connect(self._expand_add_field)
        self._add_field.arrow_up.connect(self._on_add_field_arrow_up)
        self._add_field.arrow_down.connect(self._on_add_field_arrow_down)
        self._layout.addWidget(self._add_field)
        self._add_popup: Optional[QTextEdit] = None

        # Completed section
        if completed:
            arrow = '\u25be' if self._completed_visible else '\u25b8'
            sep = QPushButton(f'{arrow}  Completed ({len(completed)})')
            sep.setFlat(True)
            t = current_theme()
            sep.setStyleSheet(
                f'QPushButton {{ text-align: left; color: {t.text_muted};'
                f' padding: 8px 4px 4px 4px; border: none; }}'
                f'QPushButton:hover {{ color: {t.text_secondary}; }}'
            )
            sep.setCursor(Qt.PointingHandCursor)
            sep.clicked.connect(self._toggle_completed)
            self._layout.addWidget(sep)

            if self._completed_visible:
                for list_idx, data in completed:
                    w = self._make_item_widget(
                        list_idx, data['text'], True, data.get('bold', False))
                    self._layout.addWidget(w)
                    if (self._focus_after_rebuild is not None
                            and self._focus_after_rebuild[0] == list_idx):
                        focus_widget = w
                        focus_at_end = self._focus_after_rebuild[1]

        self._layout.addStretch()

        # Restore focus (deferred so widgets are fully laid out).
        if focus_widget is not None:
            w_ref = focus_widget
            at_end = focus_at_end
            QTimer.singleShot(0, lambda: w_ref.focus_edit(cursor_at_end=at_end))
        elif self._focus_add_after_rebuild and self._add_field is not None:
            field = self._add_field
            def _focus_add() -> None:
                win = field.window()
                if win:
                    QApplication.setActiveWindow(win)
                field.setFocus()
                self._expand_add_field()
            QTimer.singleShot(0, _focus_add)
        self._focus_after_rebuild = None
        self._focus_add_after_rebuild = False

        # Re-activate the dialog window — on macOS, widget destruction
        # during _clear_layout can shift focus to the parent window.
        # Synchronous call handles immediate focus steal; deferred call
        # catches the steal from deleteLater() on the next event loop.
        win = self.window()
        if win:
            win.activateWindow()
            win.raise_()
            def _deferred_activate() -> None:
                try:
                    win.activateWindow()
                    win.raise_()
                except RuntimeError:
                    pass
            QTimer.singleShot(0, _deferred_activate)

        # Restore the scroll offset after the layout has had a chance to
        # settle — toggling a checkbox otherwise snaps back to the top.
        if preserve_scroll and scroll_pos:
            def _restore_scroll() -> None:
                bar = self._scroll.verticalScrollBar()
                if bar is not None:
                    bar.setValue(scroll_pos)
            QTimer.singleShot(0, _restore_scroll)


    def _make_item_widget(
        self, index: int, text: str, checked: bool, bold: bool = False,
    ) -> _ChecklistItemWidget:
        w = _ChecklistItemWidget(
            index, text, checked, bold, font_size=self._current_font_size)
        w.toggled.connect(self._on_toggle)
        w.text_edited.connect(self._on_text_edited)
        w.delete_requested.connect(self._on_delete)
        w.new_item_after.connect(self._on_new_after)
        w.merge_up.connect(self._on_merge_up)
        w.drag_started.connect(self._start_item_drag)
        w.focus_prev.connect(self._on_focus_prev)
        w.focus_next.connect(self._on_focus_next)
        w.bold_changed.connect(self._on_bold_changed)
        w._edit.image_pasted.connect(lambda fn: self._pasted_images.add(fn))
        w._edit._register_image_fn = self._register_image
        w._edit._resolve_placeholder_fn = self._resolve_placeholder
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
        if obj is not self._scroll.viewport():
            return super().eventFilter(obj, event)

        if event.type() == QEvent.DragEnter:
            if event.mimeData().hasFormat('application/x-leap-checklist-item'):
                event.acceptProposedAction()
                return True

        elif event.type() == QEvent.DragMove:
            if event.mimeData().hasFormat('application/x-leap-checklist-item'):
                event.acceptProposedAction()
                target = self._drop_target_index(event.pos().y())
                self._show_drop_indicator(target)
                return True

        elif event.type() == QEvent.DragLeave:
            self._drop_indicator.setVisible(False)
            return True

        elif event.type() == QEvent.Drop:
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
        if self._undo_stack is not None:
            self._undo_stack.record(ChecklistReorderCmd(note_name=self._cmd_ctx.current_name, src_index=src, dst_index=dst))
        item = self._items.pop(src)
        # Adjust dst if it was after the removed item
        if dst > src:
            dst -= 1
        self._items.insert(dst, item)
        self._rebuild()
        self.content_changed.emit()

    # ── Item actions ─────────────────────────────────────────────────

    def _on_toggle(self, index: int, checked: bool) -> None:
        if index < 0 or index >= len(self._items):
            return
        old_checked = self._items[index]['checked']
        self._items[index]['checked'] = checked
        self._rebuild()
        self.content_changed.emit()
        if self._undo_stack is not None:
            self._undo_stack.record(ChecklistToggleCmd(note_name=self._cmd_ctx.current_name, item_index=index, old_checked=old_checked))

    def _on_text_edited(self, index: int, text: str) -> None:
        if index < 0 or index >= len(self._items):
            return
        self._items[index]['text'] = text
        self.content_changed.emit()

    def _on_bold_changed(self, index: int, bold: bool) -> None:
        if index < 0 or index >= len(self._items):
            return
        self._items[index]['bold'] = bold
        self.content_changed.emit()

    def _on_delete(self, index: int) -> None:
        if index < 0 or index >= len(self._items):
            return
        item = self._items[index]
        del self._items[index]
        self._rebuild()
        self.content_changed.emit()
        if self._undo_stack is not None:
            self._undo_stack.record(ChecklistDeleteItemCmd(
                note_name=self._cmd_ctx.current_name, item_index=index, item_text=item['text'], item_checked=item['checked']))

    def _on_new_after(self, index: int) -> None:
        """Insert a new empty item after the given index."""
        if index < 0 or index >= len(self._items):
            return
        new_idx = index + 1
        self._items.insert(new_idx, {'text': '', 'checked': False, 'bold': False})
        self._focus_after_rebuild = (new_idx, True)
        self._rebuild()
        self.content_changed.emit()
        if self._undo_stack is not None:
            self._undo_stack.record(ChecklistAddItemCmd(
                note_name=self._cmd_ctx.current_name, item_index=new_idx, item_text=''))

    def _on_focus_prev(self, index: int) -> None:
        """Arrow up — focus the previous checklist item."""
        # Collect focusable widgets in layout order
        widgets = self._focusable_widgets()
        for i, (w, _) in enumerate(widgets):
            if isinstance(w, _ChecklistItemWidget) and w._index == index:
                if i > 0:
                    prev_w, _ = widgets[i - 1]
                    if isinstance(prev_w, _ChecklistItemWidget):
                        prev_w.focus_edit(cursor_at_end=True)
                    elif isinstance(prev_w, _ItemLineEdit):
                        prev_w.setFocus()
                return

    def _on_focus_next(self, index: int) -> None:
        """Arrow down — focus the next checklist item or add field."""
        widgets = self._focusable_widgets()
        for i, (w, _) in enumerate(widgets):
            if isinstance(w, _ChecklistItemWidget) and w._index == index:
                if i < len(widgets) - 1:
                    next_w, _ = widgets[i + 1]
                    if isinstance(next_w, _ChecklistItemWidget):
                        next_w.focus_edit(cursor_at_end=False)
                    elif isinstance(next_w, _ItemLineEdit):
                        next_w.setFocus()
                return

    def _on_add_field_arrow_up(self) -> None:
        """Arrow up from add field — focus the last item above it."""
        widgets = self._focusable_widgets()
        for i, (w, _) in enumerate(widgets):
            if w is self._add_field and i > 0:
                prev_w, _ = widgets[i - 1]
                if isinstance(prev_w, _ChecklistItemWidget):
                    prev_w.focus_edit(cursor_at_end=True)
                return

    def _on_add_field_arrow_down(self) -> None:
        """Arrow down from add field — focus the first completed item below."""
        widgets = self._focusable_widgets()
        for i, (w, _) in enumerate(widgets):
            if w is self._add_field and i < len(widgets) - 1:
                next_w, _ = widgets[i + 1]
                if isinstance(next_w, _ChecklistItemWidget):
                    next_w.focus_edit(cursor_at_end=False)
                return

    def _focusable_widgets(self) -> list[tuple['QWidget', int]]:
        """Return (widget, layout_index) for all checklist items and add field."""
        result: list[tuple[QWidget, int]] = []
        for i in range(self._layout.count()):
            item = self._layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if isinstance(w, (_ChecklistItemWidget, _ItemLineEdit)):
                result.append((w, i))
        return result

    def _on_merge_up(self, index: int) -> None:
        """Backspace on empty item → delete it and focus the previous one."""
        if index < 0 or index >= len(self._items):
            return
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
        item = self._items[index]
        del self._items[index]
        if prev_idx > index:
            prev_idx -= 1
        self._focus_after_rebuild = (prev_idx, True)
        self._rebuild()
        self.content_changed.emit()
        if self._undo_stack is not None:
            self._undo_stack.record(ChecklistDeleteItemCmd(
                note_name=self._cmd_ctx.current_name, item_index=index, item_text=item['text'],
                item_checked=item['checked']))

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
        new_idx = len(self._items)
        self._items.append({'text': text, 'checked': False, 'bold': False})
        self._focus_add_after_rebuild = True
        self._rebuild()
        self.content_changed.emit()
        if self._undo_stack is not None:
            self._undo_stack.record(ChecklistAddItemCmd(
                note_name=self._cmd_ctx.current_name, item_index=new_idx, item_text=text))

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
        row_layout = self._layout
        edit_idx = row_layout.indexOf(self._add_field)

        wrap = QTextEdit()
        wrap.setFrameShape(QFrame.NoFrame)
        wrap.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrap.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        wrap.setTabChangesFocus(True)
        wrap.setAcceptRichText(False)
        wrap.document().setDocumentMargin(2)
        wrap.setFont(self._add_field.font())
        t = current_theme()
        # Bake font-size into the popup's own stylesheet — a widget QSS
        # with any properties blocks ancestor font-size cascade in Qt, so
        # we need to specify the zoom size here explicitly.
        wrap.setStyleSheet(
            f'QTextEdit {{ color: {t.text_primary}; background: transparent;'
            f' border: 1px solid {t.text_secondary}; padding: 4px;'
            f' font-size: {self._current_font_size}pt;'
            f' font-family: Menlo; }}'
        )
        wrap.insertFromMimeData = lambda src: wrap.insertPlainText(
            src.text()) if src.hasText() else None
        wrap.setPlainText(self._add_field.text())
        wrap.setPlaceholderText('Add item')

        # Pin to line-edit height BEFORE inserting — otherwise the row
        # briefly stretches to QTextEdit's default sizeHint.
        line_h = (self._add_field.sizeHint().height()
                  or self._add_field.height() or 24)
        wrap.setFixedHeight(line_h)

        self._add_field.setVisible(False)
        row_layout.insertWidget(edit_idx + 1, wrap, 1)
        self._add_popup = wrap

        def resize_wrap() -> None:
            if self._add_popup is not wrap:
                return
            # Deterministic font-metrics × line-count height — avoids
            # the row visibly ballooning when ``document().size()``
            # returns a large value during Qt's initial layout pass.
            fm = QFontMetrics(wrap.font())
            line_count = 0
            block = wrap.document().firstBlock()
            while block.isValid():
                layout = block.layout()
                if layout is not None and layout.lineCount() > 0:
                    line_count += layout.lineCount()
                else:
                    line_count += 1
                block = block.next()
            line_count = max(1, line_count)
            margin = wrap.document().documentMargin()
            # ``documentMargin()`` returns qreal (float in Python), so
            # cast to int before ``setFixedHeight`` which is int-only.
            content_h = int(fm.lineSpacing() * line_count + margin * 2)
            wrap.setFixedHeight(max(line_h, content_h + 4))

        def on_key(event: 'QKeyEvent') -> None:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                # Treat Enter as "add this item"
                self._on_add_item()
                return
            if event.key() == Qt.Key_Escape:
                self._dismiss_add_popup(save=True)
                self._add_field.setFocus()
                return
            # Arrow up/down: try moving within visual lines first;
            # only navigate to adjacent item if the cursor can't move.
            if event.key() == Qt.Key_Up:
                cur = wrap.textCursor()
                pos_before = cur.position()
                cur.movePosition(QTextCursor.Up)
                if cur.position() == pos_before:
                    self._dismiss_add_popup(save=True)
                    self._on_add_field_arrow_up()
                    return
                wrap.setTextCursor(cur)
                return
            if event.key() == Qt.Key_Down:
                cur = wrap.textCursor()
                pos_before = cur.position()
                cur.movePosition(QTextCursor.Down)
                if cur.position() == pos_before:
                    self._dismiss_add_popup(save=True)
                    self._on_add_field_arrow_down()
                    return
                wrap.setTextCursor(cur)
                return
            # Cmd+V / Ctrl+V — paste image
            if (event.key() == Qt.Key_V
                    and event.modifiers() & Qt.ControlModifier):
                clipboard = QApplication.clipboard()
                mime = clipboard.mimeData()
                if mime and mime.hasImage():
                    image = mime.imageData()
                    if isinstance(image, QImage) and not image.isNull():
                        filename = _save_note_image(image)
                        if filename:
                            if self._add_field:
                                self._add_field._pasted_images.add(filename)
                                self._add_field.image_pasted.emit(filename)
                            else:
                                self._pasted_images.add(filename)
                            placeholder = self._register_image(filename)
                            wrap.insertPlainText(placeholder)
                            QTimer.singleShot(0, resize_wrap)
                            return
            QTextEdit.keyPressEvent(wrap, event)
            QTimer.singleShot(0, resize_wrap)

        def on_focus_out(event: 'QFocusEvent') -> None:
            try:
                QTextEdit.focusOutEvent(wrap, event)
            except RuntimeError:
                return
            QTimer.singleShot(0, lambda: self._dismiss_add_popup(save=True))

        _setup_textedit_image_hover(wrap, self._resolve_placeholder)
        _setup_textedit_url_click(wrap)
        wrap._url_hl = _UrlHighlighter(wrap.document())
        wrap.keyPressEvent = on_key
        wrap.focusOutEvent = on_focus_out

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
#  Drag-and-drop tree widget
# ══════════════════════════════════════════════════════════════════════


class _NotesTreeWidget(QTreeWidget):
    """QTreeWidget that uses Qt's native InternalMove for the drop indicator
    line, but intercepts ``dropEvent`` so the *dialog* can do the real
    filesystem move and rebuild the tree.
    """

    # (source_path, source_type, target_folder, before_path)
    # target_folder '' = root.  before_path '' = append at end.
    item_dropped = pyqtSignal(str, str, str, str)

    _ROLE_PATH = Qt.UserRole
    _ROLE_TYPE = Qt.UserRole + 1

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)

    def dropEvent(self, event: 'QDropEvent') -> None:
        """Intercept drop — compute target folder, emit signal, skip Qt rearrange."""
        dragged = self.selectedItems()
        if not dragged:
            event.ignore()
            return
        source = dragged[0]
        src_path = source.data(0, self._ROLE_PATH) or ''
        src_type = source.data(0, self._ROLE_TYPE) or ''
        if not src_path:
            event.ignore()
            return

        target_item = self.itemAt(event.pos())
        indicator = self.dropIndicatorPosition()

        if target_item is None:
            target_folder = ''
        elif (indicator == QAbstractItemView.OnItem
              and target_item.data(0, self._ROLE_TYPE) == 'folder'):
            # Dropped directly onto a folder → move inside it
            target_folder = target_item.data(0, self._ROLE_PATH) or ''
        else:
            # Above/below an item → use the containing folder
            item_path = target_item.data(0, self._ROLE_PATH) or ''
            if (target_item.data(0, self._ROLE_TYPE) == 'folder'
                    and target_item.parent() is not None):
                # Between folders inside a parent → that parent folder
                parent_path = target_item.parent().data(
                    0, self._ROLE_PATH) or ''
                target_folder = parent_path
            elif target_item.data(0, self._ROLE_TYPE) == 'folder':
                # Between top-level folders → root
                target_folder = ''
            else:
                # Between notes → same folder as the note
                target_folder = (
                    item_path.rsplit('/', 1)[0] if '/' in item_path else '')

        # Compute insertion position for ordering
        before_path = ''
        if indicator == QAbstractItemView.AboveItem and target_item is not None:
            before_path = target_item.data(0, self._ROLE_PATH) or ''
        elif indicator == QAbstractItemView.BelowItem and target_item is not None:
            parent_ti = target_item.parent() or self.invisibleRootItem()
            idx = parent_ti.indexOfChild(target_item)
            # Find next sibling, skipping the dragged item itself
            for j in range(idx + 1, parent_ti.childCount()):
                sibling = parent_ti.child(j)
                if sibling is not source:
                    before_path = (
                        sibling.data(0, self._ROLE_PATH) or '')
                    break

        # Accept without calling super — prevents Qt from rearranging items
        event.setDropAction(Qt.IgnoreAction)
        event.accept()
        self.item_dropped.emit(src_path, src_type, target_folder, before_path)


# ══════════════════════════════════════════════════════════════════════
#  Session picker dialog (for "Run in Session")
# ══════════════════════════════════════════════════════════════════════


class _SessionPickerDialog(ZoomMixin, QDialog):
    """Modal dialog to choose a running Leap session and send mode."""

    _DEFAULT_SIZE = (480, 300)

    def __init__(self, sessions: list[dict], aliases: dict,
                 is_checklist: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Select Session')
        self.resize(*self._DEFAULT_SIZE)
        self._result: Optional[tuple[str, bool]] = None
        self._is_checklist = is_checklist

        layout = QVBoxLayout(self)

        self._table = QTableWidget(len(sessions), 3)
        self._table.setHorizontalHeaderLabels(['Tag', 'Project', 'State'])
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents)

        self._tags: list[str] = []
        for row, session in enumerate(sessions):
            tag = session['tag']
            self._tags.append(tag)
            display_tag = aliases.get(tag, tag)
            self._table.setItem(row, 0, QTableWidgetItem(display_tag))
            self._table.setItem(
                row, 1, QTableWidgetItem(session.get('project', 'N/A')))
            state = session.get('cli_state', '')
            if hasattr(state, 'value'):
                state = state.value
            self._table.setItem(row, 2, QTableWidgetItem(str(state)))
        self._table.doubleClicked.connect(self._on_send_clicked)
        layout.addWidget(self._table)

        if sessions:
            self._table.selectRow(0)

        # "Include completed" — only shown for checklist notes
        self._include_completed = QCheckBox('Include completed checkboxes')
        self._include_completed.setToolTip(
            'Include checked items when sending to session')
        if is_checklist:
            prefs = load_monitor_prefs()
            self._include_completed.setChecked(
                prefs.get('run_session_include_completed', False))
            layout.addWidget(self._include_completed)

        toggle_row, _group, _next_radio, _end_radio = (
            _build_send_position_toggle(self))
        layout.addLayout(toggle_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        send_btn = QPushButton('Send')
        send_btn.setDefault(True)
        send_btn.clicked.connect(self._on_send_clicked)
        btn_row.addWidget(send_btn)
        layout.addLayout(btn_row)

        self._init_zoom(
            pref_key='session_picker_font_size',
            content_pref_key='session_picker_text_font_size',
            content_widgets=[self._table],
        )

    @property
    def include_completed(self) -> bool:
        """Whether the 'Include completed' checkbox is checked."""
        return self._include_completed.isChecked()

    def _accept(self, at_end: bool) -> None:
        row = self._table.currentRow()
        if row < 0 or row >= len(self._tags):
            return
        self._result = (self._tags[row], at_end)
        self.accept()

    def _on_send_clicked(self) -> None:
        """Read the persisted toggle and dispatch to _accept."""
        at_end = load_send_position() != 'next'
        self._accept(at_end)

    @staticmethod
    def pick_session(
        parent: Optional[QWidget] = None,
        is_checklist: bool = False,
    ) -> Optional[tuple[str, bool, bool]]:
        """Show the picker and return (tag, at_end, include_completed) or None."""
        sessions = get_active_sessions()
        if not sessions:
            QMessageBox.information(
                parent, 'Run in Session', 'No active sessions found.')
            return None
        prefs = load_monitor_prefs()
        aliases = prefs.get('aliases', {})
        # Match the main-window order (drag-and-drop reorder is persisted
        # as ``row_order`` in monitor prefs).  Unknown tags go to the end.
        row_order = prefs.get('row_order', [])
        order_map = {tag: i for i, tag in enumerate(row_order)}
        sessions.sort(key=lambda s: order_map.get(s['tag'], float('inf')))
        dlg = _SessionPickerDialog(sessions, aliases, is_checklist, parent)
        accepted = dlg.exec_() == QDialog.Accepted
        # Persist checkbox state on any close (OK, Cancel, or X)
        if is_checklist:
            prefs = load_monitor_prefs()
            prefs['run_session_include_completed'] = dlg.include_completed
            save_monitor_prefs(prefs)
        if accepted and dlg._result is not None:
            tag, at_end = dlg._result
            return (tag, at_end, dlg.include_completed)
        return None


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
        # Position left to Qt — it auto-centers modal dialogs on the
        # parent window, matching every other dialog in the project.

        self._current_name: Optional[str] = None
        self._saved_text: str = ''
        self._switching_mode: bool = False
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

        splitter = QSplitter(Qt.Horizontal)
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

    def _update_timestamp(self) -> None:
        """Update the tooltip for the current note in the tree."""
        if not self._current_name:
            return
        item = self._find_tree_item(self._current_name, 'note')
        if item:
            ts = _format_mtime(_note_path(self._current_name))
            if ts:
                item.setToolTip(0, f'{self._current_name}\n{ts}')

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
            self._mode_combo.setVisible(False)
            self._stack.setCurrentIndex(self._MODE_TEXT)
            self._title_label.setText('')
            self._update_action_visibility(False)
            self._find_bar.setVisible(False)
            return

        if current.data(0, self._ROLE_TYPE) == 'folder':
            # Folder selected — clear editor, show folder name
            self._current_name = None
            self._saved_text = ''
            self._editor.clear()
            self._editor.setEnabled(False)
            self._mode_combo.setVisible(False)
            self._stack.setCurrentIndex(self._MODE_TEXT)
            self._title_label.setText(current.text(0))
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
        display = name.rsplit('/', 1)[-1] if '/' in name else name
        self._title_label.setText(display)
        self._update_timestamp()
        self._update_action_visibility(True)

    # ── Mode switching ──────────────────────────────────────────────

    def _on_mode_changed(self, index: int) -> None:
        if self._switching_mode or not self._current_name:
            return

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

            menu.addAction('Delete', self._on_delete)

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

            if item_type == 'note':
                if _note_path(new_full).exists():
                    QMessageBox.warning(
                        self, 'Already Exists',
                        f"A note named '{new_name}' already exists.")
                    continue
            else:
                if (NOTES_DIR / new_full).exists():
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
            QMessageBox.Yes | QMessageBox.No)
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
        save_dialog_geometry('notes_dialog', self.width(), self.height())
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
        save_dialog_geometry('notes_dialog', self.width(), self.height())
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
        for attr in ('_bottom_hint', '_title_label', '_find_counter'):
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

        For _NoteTextEdit: clears the anchor/underline/link-color from
        char formats in the selection, keeping the text intact.
        For popup QTextEdit / QLineEdit: if the selection (or the text
        around it) is a markdown ``[text](url)`` span, replace it with
        just ``text``.
        """
        if isinstance(focus, _NoteTextEdit):
            cursor = focus.textCursor()
            if not cursor.hasSelection():
                return
            clear_fmt = QTextCharFormat()
            clear_fmt.setAnchor(False)
            clear_fmt.setAnchorHref('')
            clear_fmt.setFontUnderline(False)
            clear_fmt.setForeground(QColor(current_theme().text_primary))
            cursor.mergeCharFormat(clear_fmt)
            focus.setTextCursor(cursor)
            return
        # Markdown-text targets: look for a [text](url) span overlapping
        # the selection/cursor and replace it with the display text.
        if isinstance(focus, QTextEdit):
            cursor = focus.textCursor()
            block_text = cursor.block().text()
            block_start = cursor.block().position()
            col = cursor.position() - block_start
            span = _find_markdown_link_at(block_text, col)
            if span is None:
                return
            a, b, display = span
            c = QTextCursor(focus.document())
            c.setPosition(block_start + a)
            c.setPosition(block_start + b, QTextCursor.KeepAnchor)
            c.insertText(display)
            focus.setTextCursor(c)
            return
        if isinstance(focus, QLineEdit):
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

        # The dialog may have caused the popup to dismiss.  If so, route
        # the link into its parent's line edit rather than falling back
        # to whatever Qt decided to focus next (which may be the sidebar
        # search — that is how URLs used to leak into the search box).
        if focus is None or sip.isdeleted(focus):
            if (fallback_line_edit is not None
                    and not sip.isdeleted(fallback_line_edit)):
                # Keep ``selected`` and ``sel_start/sel_end`` — the popup
                # text transfers to the line edit 1:1 on dismissal, so
                # the same positions apply.
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
                # Re-anchor to the saved range
                cursor.setPosition(sel_start)
                cursor.setPosition(sel_end, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                cursor.insertText(selected, fmt)
            else:
                cursor.insertText(url, fmt)
            # Reset format so text typed after the link is normal
            cursor.setCharFormat(QTextCharFormat())
            focus.setTextCursor(cursor)
        elif isinstance(focus, QTextEdit):
            # Checklist expand popup (still alive).  Insert the link as
            # an anchor-format span so it renders styled (blue +
            # underline) matching the popup's rich-text rendering —
            # otherwise new links would show as plain ``[x](y)`` text
            # while existing ones already render as anchors.
            cursor = focus.textCursor()
            link_fmt = _link_char_format(url)
            if selected and sel_start >= 0:
                cursor.setPosition(sel_start)
                cursor.setPosition(sel_end, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                cursor.insertText(selected, link_fmt)
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
            # For checklist items the line edit shows STRIPPED display,
            # but the authoritative text is the widget's _raw_text (which
            # matches the popup's former content 1:1).  We perform the
            # insertion against that raw string and let set_raw_text
            # propagate the change back to the display + the items list.
            replacement = f'[{selected}]({url})' if selected else url
            parent_item = focus.parent()
            if isinstance(parent_item, _ChecklistItemWidget):
                source = parent_item._raw_text
                if selected and sel_start >= 0 and sel_end <= len(source):
                    new_raw = source[:sel_start] + replacement + source[sel_end:]
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
