"""``_NoteTextEdit`` — the rich-text editor used for plain-text notes.

Adds three things on top of ``QTextEdit``:
* Inline image rendering (paste from clipboard or load from ``![image](hash)``).
* Inline link + bold rendering (``[text](url)`` and STX/ETX-wrapped spans
  parsed by :mod:`text_helpers`).
* URL hover/click + image hover preview (Cmd+B / Cmd+C key handling).

Two utility functions also live here:
* :func:`_setup_textedit_url_click` — bolt URL click + pointer cursor onto
  any ``QTextEdit`` (e.g. the checklist expand popup).
* :func:`_setup_textedit_image_hover` — bolt image hover preview onto any
  ``QTextEdit``, optionally resolving ``[Image #N]`` placeholders.
"""

from typing import Optional

from PyQt5.QtCore import QEvent, QMimeData, QPoint, QUrl, Qt
from PyQt5.QtGui import (
    QColor, QFont, QImage, QTextCharFormat, QTextCursor, QTextImageFormat,
)
from PyQt5.QtWidgets import QApplication, QTextEdit, QWidget

from leap.monitor.dialogs.notes.image_helpers import (
    _CHECKLIST_PLACEHOLDER_RE, _IMAGE_MARKER_RE, _ImagePreviewPopup,
    _NOTE_IMAGE_MAX_WIDTH, _save_note_image,
)
from leap.monitor.dialogs.notes.text_helpers import (
    _BOLD_END, _BOLD_START, _INLINE_FORMAT_RE, _LINK_RE, _UrlHighlighter,
    _link_char_format, _try_open_url, _url_at_pos,
)
from leap.monitor.themes import current_theme
from leap.utils.constants import NOTE_IMAGES_DIR


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
        # Cmd+C — convert selection to markdown so pasting elsewhere
        # (other notes / checklist popups) preserves link and bold
        # spans as ``[text](url)`` / ``**text**``.
        if event.key() == Qt.Key_C and mods == Qt.ControlModifier:
            cursor = self.textCursor()
            if cursor.hasSelection():
                md = _NoteTextEdit._serialize_selection_to_markdown(cursor)
                QApplication.clipboard().setText(md)
                event.accept()
                return
        super().keyPressEvent(event)

    @staticmethod
    def _serialize_selection_to_markdown(cursor: QTextCursor) -> str:
        """Convert a QTextCursor selection to the note's serialized form.

        Anchor spans → ``[text](url)``.  Bold spans → STX/ETX-wrapped
        (the same form ``_insert_text_with_links`` parses back into
        bold formatting).  Image fragments → ``![image](filename)``.
        Plain fragments keep their text.
        """
        if not cursor.hasSelection():
            return ''
        sel_start = cursor.selectionStart()
        sel_end = cursor.selectionEnd()
        doc = cursor.document()
        parts: list[str] = []
        block = doc.findBlock(sel_start)
        first = True
        while block.isValid() and block.position() < sel_end:
            if not first:
                parts.append('\n')
            first = False
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    frag_start = frag.position()
                    frag_end = frag_start + frag.length()
                    clip_start = max(frag_start, sel_start)
                    clip_end = min(frag_end, sel_end)
                    if clip_start < clip_end:
                        fmt = frag.charFormat()
                        if fmt.isImageFormat():
                            name = fmt.toImageFormat().name()
                            if name:
                                parts.append(f'![image]({name})')
                        else:
                            text = frag.text()[
                                clip_start - frag_start:clip_end - frag_start]
                            is_anchor = (fmt.isAnchor()
                                         and fmt.anchorHref())
                            is_bold = fmt.fontWeight() >= QFont.Bold
                            if is_anchor and is_bold and text:
                                parts.append(
                                    f'{_BOLD_START}[{text}]'
                                    f'({fmt.anchorHref()}){_BOLD_END}')
                            elif is_anchor:
                                parts.append(f'[{text}]({fmt.anchorHref()})')
                            elif is_bold and text:
                                parts.append(
                                    f'{_BOLD_START}{text}{_BOLD_END}')
                            else:
                                parts.append(text)
                it += 1
            block = block.next()
        return ''.join(parts)

    def _toggle_bold(self) -> None:
        """Flip bold on the selection, or on subsequent typing if no selection.

        If the cursor/selection starts inside a link (anchor span), the
        link colour is preserved — we only touch font weight.  This
        keeps a bold link's blue colour instead of painting it orange.
        """
        cursor = self.textCursor()
        probe = cursor.charFormat()
        is_bold = probe.fontWeight() >= QFont.Bold
        is_anchor = probe.isAnchor() and probe.anchorHref()
        t = current_theme()
        fmt = QTextCharFormat()
        if is_bold:
            fmt.setFontWeight(QFont.Normal)
            if not is_anchor:
                fmt.setForeground(QColor(t.text_primary))
        else:
            fmt.setFontWeight(QFont.Black)
            if not is_anchor:
                fmt.setForeground(QColor(t.accent_orange))
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
        # Render ``[text](url)`` + STX/ETX-wrapped bold markdown as
        # styled spans (anchor / bold) instead of literal syntax.
        # Clipboard HTML is intentionally ignored so browser-style rich
        # formatting doesn't leak in.
        if source.hasText():
            cursor = self.textCursor()
            # If the cursor is sitting in an anchor span (typical after
            # inserting a link), reset the format so the pasted plain
            # run doesn't inherit the link styling.
            cursor.setCharFormat(QTextCharFormat())
            self._insert_text_with_links(cursor, source.text())
            # Push the mutated cursor back so the caret + selection
            # reflect the end of the paste, matching Qt's default
            # insertPlainText behaviour.
            self.setTextCursor(cursor)
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
        """Insert plain text, rendering [text](url) / STX-ETX bold.

        Handles the three inline forms: ``[text](url)`` (link),
        ``\\x02text\\x03`` (bold), and ``\\x02[text](url)\\x03`` (bold
        link — the whole STX/ETX block contains a single markdown link,
        rendered with bold weight AND link colour+underline).
        """
        pos = 0
        for m in _INLINE_FORMAT_RE.finditer(text):
            if m.start() > pos:
                cursor.insertText(text[pos:m.start()])
            if m.group(1) is not None:
                # Markdown link
                cursor.insertText(m.group(1), _link_char_format(m.group(2)))
            else:
                # Bold — may wrap a link (\x02[text](url)\x03) in which
                # case we render bold + link in one span.
                bold_content = m.group(3)
                link_match = _LINK_RE.fullmatch(bold_content)
                if link_match:
                    fmt = _link_char_format(link_match.group(2))
                    fmt.setFontWeight(QFont.Black)
                    cursor.insertText(link_match.group(1), fmt)
                else:
                    bold_fmt = QTextCharFormat()
                    bold_fmt.setFontWeight(QFont.Black)
                    bold_fmt.setForeground(
                        QColor(current_theme().accent_orange))
                    cursor.insertText(bold_content, bold_fmt)
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
                        it += 1
                        continue
                    txt = fragment.text()
                    is_anchor = fmt.isAnchor() and fmt.anchorHref()
                    is_bold = fmt.fontWeight() >= QFont.Bold
                    if is_anchor and is_bold and txt and include_bold_markers:
                        result.append(
                            f'{_BOLD_START}[{txt}]'
                            f'({fmt.anchorHref()}){_BOLD_END}')
                    elif is_anchor:
                        result.append(f'[{txt}]({fmt.anchorHref()})')
                    elif is_bold and txt and include_bold_markers:
                        result.append(f'{_BOLD_START}{txt}{_BOLD_END}')
                    else:
                        result.append(txt)
                it += 1
            block = block.next()
        return ''.join(result)


# ── QTextEdit enhancers ─────────────────────────────────────────────
# These bolt URL clicking + image hover onto any QTextEdit (not just
# _NoteTextEdit) — used by the checklist popup expand editor.


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
