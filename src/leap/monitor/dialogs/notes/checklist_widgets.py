"""Google Keep-style checklist editor widgets.

Four tightly-coupled classes form the checklist editor:

* :class:`_ItemLineEdit` — display/edit single-line item, paste images
  and propagate Cmd+B/V/arrows to the parent ``_ChecklistItemWidget``.
* :class:`_DragGrip` — hamburger icon initiating a deferred ``QDrag``.
* :class:`_ChecklistItemWidget` — one row (grip + checkbox + line edit
  + delete button), owns its own expand-popup ``QTextEdit`` and a
  rich-text overlay for per-link styling.
* :class:`_ChecklistWidget` — orchestrates the items list, scroll area,
  active/completed sections, drag-drop reorder, and the "Add item" row.

Inter-class references inside this module are deliberate: ``_ItemLineEdit``
sniffs ``isinstance(parent, _ChecklistItemWidget)`` to map clicks back to
the raw markdown, and ``_ChecklistWidget`` uses ``_ChecklistItemWidget``'s
static ``_serialize_popup_markdown`` helper for the add-popup serialization.
Keeping all four in one module avoids the circular-import dance.
"""

import html
import re
from typing import Optional

from PyQt5 import sip
from PyQt5.QtCore import (
    QEvent, QMimeData, QPoint, QTimer, Qt, pyqtSignal,
)
from PyQt5.QtGui import (
    QColor, QCursor, QDrag, QFont, QFontMetrics, QImage, QTextCharFormat,
    QTextCursor,
)
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from leap.monitor.dialogs.notes.image_helpers import (
    _CHECKLIST_PLACEHOLDER_RE, _IMAGE_MARKER_RE, _ImagePreviewPopup,
    _save_note_image,
)
from leap.monitor.dialogs.notes.note_text_edit import (
    _NoteTextEdit, _setup_textedit_image_hover, _setup_textedit_url_click,
)
from leap.monitor.dialogs.notes.rtl import _apply_rtl_direction
from leap.monitor.dialogs.notes.text_helpers import (
    _BOLD_END, _BOLD_START, _INLINE_FORMAT_RE, _LINK_RE, _UrlHighlighter,
    _link_at_stripped_pos, _strip_inline_formats, _try_open_url,
    _url_at_line_edit_pos,
)
from leap.monitor.dialogs.notes_undo import (
    ChecklistAddItemCmd, ChecklistDeleteItemCmd, ChecklistReorderCmd,
    ChecklistToggleCmd, NotesCmdContext, NotesUndoStack,
)
from leap.monitor.themes import current_theme
from leap.utils.constants import NOTE_IMAGES_DIR


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
        self._edit = _ItemLineEdit(_strip_inline_formats(text))
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
        # contentsMargins(11, 0, 11, 0) matches the line edit's text X
        # position: theme ``QLineEdit { padding: 6px 10px; border: 1px
        # solid ... }`` puts line-edit text at x=11.  Without this,
        # the overlay renders text at x=2 and adding a link (which
        # makes the overlay visible) visibly jerks the text ~9px
        # leftward.  Vertical alignment is AlignVCenter so the overlay
        # text sits at the same Y as the line edit's (Qt-centered)
        # text — matches the popup edit position too.
        self._rich_overlay.setContentsMargins(11, 0, 11, 0)
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
        # Fixed vertical size policy: item always matches its sizeHint
        # height and never absorbs excess space from the parent
        # ``QVBoxLayout``.  Without this, the parent layout would
        # distribute excess space across items when the trailing
        # ``addStretch()`` wasn't enough — observed as a ~200px gap
        # below the edited item even though sizeHint was 40.  The
        # item's sizeHint still grows dynamically when the popup
        # expands (because children drive sizeHint), so multi-line
        # edit still expands correctly.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

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
        bold_color = t.accent_orange if not self._checked else t.text_muted
        # Walk the raw text: emit link / bold spans with their own
        # styling, plain runs in base_color.  Escape HTML-specials so
        # user text isn't interpreted as tags.
        # CSS ``text-decoration`` isn't inherited — child spans replace
        # the parent's value — so include ``line-through`` explicitly
        # on link spans when the item is checked.
        link_decoration = ('underline line-through'
                           if self._checked else 'underline')
        bold_decoration = ('line-through' if self._checked else 'none')
        parts: list[str] = []
        pos = 0
        for m in _INLINE_FORMAT_RE.finditer(self._raw_text):
            if m.start() > pos:
                parts.append(html.escape(self._raw_text[pos:m.start()]))
            if m.group(1) is not None:
                # Link span: [display](url)
                display = html.escape(m.group(1))
                parts.append(
                    f'<span style="color:{link_color};'
                    f' text-decoration:{link_decoration}">{display}</span>'
                )
            else:
                # Bold span: \x02text\x03 — may wrap a link
                # (\x02[display](url)\x03) which renders as bold+link.
                bold_content = m.group(3)
                nested_link = _LINK_RE.fullmatch(bold_content)
                if nested_link:
                    display = html.escape(nested_link.group(1))
                    parts.append(
                        f'<span style="color:{link_color};'
                        f' font-weight:bold;'
                        f' text-decoration:{link_decoration}">{display}</span>'
                    )
                else:
                    btext = html.escape(bold_content)
                    parts.append(
                        f'<span style="color:{bold_color};'
                        f' font-weight:bold;'
                        f' text-decoration:{bold_decoration}">{btext}</span>'
                    )
            pos = m.end()
        if pos < len(self._raw_text):
            parts.append(html.escape(self._raw_text[pos:]))
        weight = 'bold' if self._bold else 'normal'
        strike = ' text-decoration:line-through;' if self._checked else ''
        # Wrap the whole string in a span that sets the base color +
        # strike (link / bold spans override color+decoration).
        # ``white-space: pre-wrap`` preserves consecutive spaces that
        # HTML would otherwise collapse — matches what the line edit
        # and popup show for raw text like ``hello  hi``.
        size_rule = (f' font-size:{self._font_size}pt;'
                     if self._font_size else '')
        self._rich_overlay.setText(
            f'<span style="color:{base_color}; font-weight:{weight};'
            f' font-family:Menlo;{size_rule}{strike}'
            f' white-space:pre-wrap;">'
            f'{"".join(parts)}</span>'
        )
        self._rich_overlay.setGeometry(self._edit.rect())
        self._rich_overlay.show()

    def _has_link_display(self) -> bool:
        """True if the raw text contains any inline-format span.

        Historically this checked only for markdown links.  Now we
        also trigger the overlay for inline bold (STX/ETX markers)
        and for the deprecated ``_bold`` flag so legacy items still
        render their bolding via the rich-text path.  Name kept for
        call-site compatibility.
        """
        if _LINK_RE.search(self._raw_text):
            return True
        if _BOLD_START in self._raw_text and _BOLD_END in self._raw_text:
            return True
        return False

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
        self._edit.setText(_strip_inline_formats(raw))
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
        """Toggle bold formatting.

        If the expand popup is open and the user has a selection,
        apply bold as a per-character char format (the selected run
        flips between QFont.Black + accent_orange and QFont.Normal +
        text_primary).  Otherwise fall back to the legacy whole-item
        toggle that flips ``self._bold`` — used when the line edit has
        focus in non-edit mode (no selection available).
        """
        t = current_theme()
        if self._popup is not None:
            wrap = self._popup
            cursor = wrap.textCursor()
            # Detect current bold/anchor state at cursor.  We only
            # override the foreground colour when the span is NOT a
            # link — for a bold link we want to keep the link's blue
            # colour after un-bolding (it's still a link) and we don't
            # want to paint the link word orange when bolding it
            # (link identity > bold colour).
            probe = cursor.charFormat()
            is_bold = probe.fontWeight() >= QFont.Bold
            is_anchor = probe.isAnchor() and probe.anchorHref()
            fmt = QTextCharFormat()
            if is_bold:
                fmt.setFontWeight(QFont.Normal)
                if not is_anchor:
                    fmt.setForeground(QColor(
                        t.text_muted if self._checked else t.text_primary))
            else:
                fmt.setFontWeight(QFont.Black)
                if not is_anchor:
                    fmt.setForeground(QColor(t.accent_orange))
            if cursor.hasSelection():
                cursor.mergeCharFormat(fmt)
            else:
                wrap.mergeCurrentCharFormat(fmt)
            return
        # Legacy whole-item toggle (no popup — line edit Cmd+B).
        self._bold = not self._bold
        self._apply_bold_style()
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
        fragments (blue + underlined) and bold runs as
        ``QFont.Bold``-weight char formats.  Serialize back to the
        note format: anchors → ``[text](url)``, bold runs →
        ``\\x02text\\x03`` (STX/ETX markers), plain fragments emit
        their own text.
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
                    text = frag.text()
                    is_anchor = fmt.isAnchor() and fmt.anchorHref()
                    is_bold = fmt.fontWeight() >= QFont.Bold
                    if is_anchor and is_bold and text:
                        parts.append(
                            f'{_BOLD_START}[{text}]'
                            f'({fmt.anchorHref()}){_BOLD_END}')
                    elif is_anchor:
                        parts.append(f'[{text}]({fmt.anchorHref()})')
                    elif is_bold and text:
                        parts.append(f'{_BOLD_START}{text}{_BOLD_END}')
                    else:
                        parts.append(text)
                it += 1
            block = block.next()
        return ''.join(parts)

    @staticmethod
    def _serialize_cursor_selection_to_markdown(cursor: QTextCursor) -> str:
        """Convert a QTextCursor selection into markdown text.

        Walks the fragments covered by the selection; anchor-format
        runs become ``[text](url)``, plain runs emit their own text.
        Used by the popup's Cmd+C override so pasting back into
        another popup (or the text editor) re-renders the link via
        ``_insert_text_with_links``.
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
                parts.append(' ')
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
                        text = frag.text()[
                            clip_start - frag_start:clip_end - frag_start]
                        fmt = frag.charFormat()
                        is_anchor = fmt.isAnchor() and fmt.anchorHref()
                        is_bold = fmt.fontWeight() >= QFont.Bold
                        if is_anchor and is_bold and text:
                            parts.append(
                                f'{_BOLD_START}[{text}]'
                                f'({fmt.anchorHref()}){_BOLD_END}')
                        elif is_anchor:
                            parts.append(f'[{text}]({fmt.anchorHref()})')
                        elif is_bold and text:
                            parts.append(f'{_BOLD_START}{text}{_BOLD_END}')
                        else:
                            parts.append(text)
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
        wrap.setFont(self._edit.font())
        # documentMargin(7) + padding(0 3) + border(1) positions the
        # popup's text at the same x/y as the line edit's (theme gives
        # the line edit border:1 + padding:6 10 and Qt centres the
        # text vertically inside).  The math:
        #   line edit text x = 1(border) + 10(padding-l) = 11;
        #                  y = 1 + 6 + (content_h-text_h)/2 ≈ 8 for line_h=33.
        #   popup    text x = 1(border) + 3(padding-l) + 7(doc-margin) = 11 ✓
        #                  y = 1 + 0 + 7 = 8 ✓.
        wrap.document().setDocumentMargin(7)
        t = current_theme()
        color = (t.text_muted if self._checked
                 else t.accent_orange if self._bold
                 else t.text_primary)
        # Bake font-size into the widget's own stylesheet — a widget QSS
        # that declares any property blocks the ancestor cascade for
        # font-size in Qt, so we must specify it explicitly here.
        size_css = (f' font-size: {self._font_size}pt; font-family: Menlo;'
                    if self._font_size else '')
        # 1px border gives the user a visible "square" signalling edit
        # mode; padding: 0 3px offsets text horizontally to line up
        # with the line edit's 10px left padding (the rest comes from
        # the documentMargin above).
        wrap.setStyleSheet(
            f'QTextEdit {{ color: {color}; background: transparent;'
            f' border: 1px solid {t.text_secondary}; padding: 0 3px;'
            f'{size_css} }}'
        )
        # Paste: if the clipboard text contains markdown links, render
        # them as anchor spans (so ``[hello](url)`` appears as a styled
        # link, not literal markdown).  Bare text still pastes as
        # plain text.  HTML is intentionally not interpreted — only
        # markdown text is — to avoid browser-style rich formatting
        # leaking in.
        def _paste_with_links(src: QMimeData) -> None:
            if not src.hasText():
                return
            text = src.text()
            cursor = wrap.textCursor()
            # Reset char format — pasting plain text inside an anchor
            # span would otherwise inherit its underline/colour.
            cursor.setCharFormat(QTextCharFormat())
            _NoteTextEdit._insert_text_with_links(cursor, text)
            wrap.setTextCursor(cursor)
            # Emit text_edited so the items list is kept current.
            # Cmd+V via on_key emits this anyway (tail of the handler),
            # but right-click paste (context menu) calls this directly
            # and would otherwise leak pasted content into a stale
            # items dict until the popup dismisses.
            self.text_edited.emit(
                self._index, self._serialize_popup_markdown(wrap))
        wrap.insertFromMimeData = _paste_with_links
        # Render the raw markdown as rich text (anchor-styled links),
        # not visible ``[word](url)`` syntax.  On dismiss we serialize
        # the document back to markdown (_serialize_popup_markdown).
        # Migrate legacy whole-item bold: if the ``_bold`` flag is set
        # but there are no inline STX/ETX markers in the raw text, wrap
        # the text so the popup renders it with bold char format and a
        # subsequent dismiss saves the inline form (Cmd+B per-char then
        # works cleanly).
        render_text = self._raw_text
        if (self._bold and _BOLD_START not in render_text
                and render_text):
            render_text = f'{_BOLD_START}{render_text}{_BOLD_END}'
        cursor = wrap.textCursor()
        cursor.movePosition(QTextCursor.Start)
        _NoteTextEdit._insert_text_with_links(cursor, render_text)

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
            # +2 accounts for the 1px border top + 1px border bottom.
            content_h = int(fm.lineSpacing() * line_count + margin * 2 + 2)
            # Grow ONLY when the content is actually taller than the
            # line edit it replaced (the text wrapped or has multiple
            # visual lines).  Single-line content keeps exactly the
            # line edit's height — the row doesn't move on edit.
            wrap.setFixedHeight(max(line_h, content_h))

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
            # Cmd+C — copy selection as markdown so pasting back into
            # another popup (or the text editor) re-renders any link
            # spans as anchor-styled text instead of dropping them.
            if (event.key() == Qt.Key_C
                    and mods_masked == Qt.ControlModifier):
                cursor = wrap.textCursor()
                if cursor.hasSelection():
                    md = _ChecklistItemWidget \
                        ._serialize_cursor_selection_to_markdown(cursor)
                    QApplication.clipboard().setText(md)
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
            # Suppress dismissal when a modal URL dialog (Cmd+K) is
            # stealing focus.  Without this the popup serializes to
            # markdown and calls set_raw_text before the URL dialog
            # returns — the fallback line-edit path then slices raw
            # markdown using positions captured from popup plain text,
            # corrupting content (e.g. producing ``[word]asdas``).
            if getattr(wrap, '_suppress_dismiss', False):
                return
            QTimer.singleShot(0, lambda: dismiss(True))

        _setup_textedit_image_hover(wrap, self._edit._resolve_placeholder_fn)
        _setup_textedit_url_click(wrap)
        wrap._url_hl = _UrlHighlighter(wrap.document())
        wrap._suppress_dismiss = False
        wrap.keyPressEvent = on_key
        wrap.focusOutEvent = on_focus_out

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
        self._add_popup: Optional[QTextEdit] = None

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
        # Flush any active popup BEFORE replacing self._items — the
        # popup's text_edited signal is keyed to the OLD index, so
        # letting it fire after the items list changed would write
        # into the new note's data (wrong row) or get silently
        # dropped.
        self._flush_popups()
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

    def _flush_popups(self) -> None:
        """Dismiss any active item or add-field popup, saving content.

        Call this BEFORE mutating ``self._items`` (delete / insert /
        toggle / reorder / replace) so the popup's ``text_edited``
        signal flushes into the *old* items list at the correct index.
        If called AFTER mutation, the popup's index may point to a
        shifted or removed item and the text update goes to the wrong
        row (or gets silently dropped by the index-range check).
        """
        prev = _ChecklistItemWidget._active_expand
        if prev is not None:
            prev._dismiss_popup_if_active()
        if self._add_popup is not None:
            self._dismiss_add_popup(save=True)

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
        # Match the add-popup's effective geometry so entering edit mode
        # doesn't shift the text.  Popup uses padding(6, 4) + border(1),
        # so the add field needs padding(7, 5) — same internal space +
        # 1px on each side that the popup's border occupies.
        self._add_field.setStyleSheet(
            f'QLineEdit {{ padding: 7px 5px; background: transparent;'
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
        # Both calls are gated on ``isActiveWindow()`` so they fire only
        # when there's an actual deactivation to recover from; calling
        # ``activateWindow`` on an already-active window on macOS triggers
        # a redundant deactivate-reactivate cycle, visible to the user as
        # a rapid flash to the parent monitor window on every checkbox
        # toggle.  Sync call handles any immediate steal from setParent(None);
        # deferred call catches the steal from deleteLater on the next loop.
        win = self.window()
        if win:
            if not win.isActiveWindow():
                win.activateWindow()
                win.raise_()
            def _deferred_activate() -> None:
                try:
                    if not win.isActiveWindow():
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
        # Flush popup before reorder — see _flush_popups docstring.
        self._flush_popups()
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
        # Flush a popup on a DIFFERENT item before toggling — otherwise
        # the popup's pending text_edited would land on a shifted row
        # after the rebuild moves items between active/completed
        # sections.
        self._flush_popups()
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
        # Flush popup BEFORE the deletion — otherwise the popup's
        # pending text_edited would write to the wrong row (indices
        # above the deleted one shift down) or get dropped.
        self._flush_popups()
        item = self._items[index]
        del self._items[index]
        self._rebuild()
        self.content_changed.emit()
        if self._undo_stack is not None:
            self._undo_stack.record(ChecklistDeleteItemCmd(
                note_name=self._cmd_ctx.current_name, item_index=index, item_text=item['text'],
                item_checked=item['checked'], item_bold=item.get('bold', False)))

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
        # Flush popups before the del — see _flush_popups docstring.
        self._flush_popups()
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
                item_checked=item['checked'], item_bold=item.get('bold', False)))

    def _on_add_item(self) -> None:
        # If the add popup is active, serialize its document so any
        # inline link / bold spans survive into the new item.  The
        # line-edit fallback can't have inline formatting, so plain
        # text is fine there.
        if self._add_popup is not None:
            text = _ChecklistItemWidget \
                ._serialize_popup_markdown(self._add_popup) \
                .replace('\n', ' ').strip()
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
        # Bake current zoom size into the font so ``QFontMetrics``
        # inside resize_wrap reflects the actual rendered line height
        # (see checkbox popup for rationale — otherwise wrapped text
        # is clipped at higher zoom levels).
        wrap.setFont(self._add_field.font())
        # Popup text sits at (x=5, y=8) via padding:7 4 + border:1.
        # Add field uses padding:7 5 (no border) which also places
        # text at (5, 8) — so entering edit mode shows NO visible
        # text movement, only the border appearing around the text.
        wrap.document().setDocumentMargin(0)
        t = current_theme()
        wrap.setStyleSheet(
            f'QTextEdit {{ color: {t.text_primary}; background: transparent;'
            f' border: 1px solid {t.text_secondary}; padding: 7px 4px;'
            f' font-size: {self._current_font_size}pt;'
            f' font-family: Menlo; }}'
        )
        # Paste: render markdown links as anchor spans instead of
        # literal ``[text](url)`` syntax.  Matches the checkbox popup.
        def _paste_with_links(src: QMimeData) -> None:
            if not src.hasText():
                return
            cursor = wrap.textCursor()
            cursor.setCharFormat(QTextCharFormat())
            _NoteTextEdit._insert_text_with_links(cursor, src.text())
            wrap.setTextCursor(cursor)
        wrap.insertFromMimeData = _paste_with_links
        # Render any markdown in the line edit as anchor / bold spans —
        # ``setPlainText`` would show the literal ``[text](url)`` syntax
        # and lose the link URL on a subsequent serialize.  This matches
        # the item popup which renders its raw text the same way.
        cursor = wrap.textCursor()
        cursor.movePosition(QTextCursor.Start)
        _NoteTextEdit._insert_text_with_links(cursor, self._add_field.text())
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
            # +2 for the 1px border top/bottom, +12 for ``padding: 6px``
            # top/bottom (must match the popup's stylesheet above).
            # +16 = 2 (border top+bottom) + 14 (padding 7px top+bottom)
            content_h = int(fm.lineSpacing() * line_count + margin * 2 + 16)
            # Grow only when the content genuinely needs more room;
            # single-line input keeps the add-field's height so the
            # row doesn't visibly jump on edit.
            wrap.setFixedHeight(max(line_h, content_h))

        def on_key(event: 'QKeyEvent') -> None:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                # Treat Enter as "add this item"
                self._on_add_item()
                return
            if event.key() == Qt.Key_Escape:
                # Escape cancels — do NOT save popup content into the
                # line edit (matches the item popup's Escape behaviour
                # and what users expect from a "cancel" key).
                self._dismiss_add_popup(save=False)
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
            mods_masked = event.modifiers() & (
                Qt.ControlModifier | Qt.ShiftModifier
                | Qt.AltModifier | Qt.MetaModifier)
            # Cmd+B — per-character bold (same pattern as the checkbox
            # item popup).  Toggles bold on the current selection or
            # subsequent typing when there's no selection.  Preserves
            # link colour when toggling bold on a link span.
            if (event.key() == Qt.Key_B
                    and mods_masked == Qt.ControlModifier):
                cursor = wrap.textCursor()
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
                else:
                    wrap.mergeCurrentCharFormat(fmt)
                return
            # Cmd+C — copy selection as markdown so pasting back into
            # another popup or the text editor re-renders any link
            # spans as anchor-styled text.
            if (event.key() == Qt.Key_C
                    and mods_masked == Qt.ControlModifier):
                cursor = wrap.textCursor()
                if cursor.hasSelection():
                    md = _ChecklistItemWidget \
                        ._serialize_cursor_selection_to_markdown(cursor)
                    QApplication.clipboard().setText(md)
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
            # Suppress dismissal when a modal URL dialog (Cmd+K) is
            # stealing focus — see checkbox popup for rationale.
            if getattr(wrap, '_suppress_dismiss', False):
                return
            QTimer.singleShot(0, lambda: self._dismiss_add_popup(save=True))

        _setup_textedit_image_hover(wrap, self._resolve_placeholder)
        _setup_textedit_url_click(wrap)
        wrap._url_hl = _UrlHighlighter(wrap.document())
        wrap._suppress_dismiss = False
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
        """Collapse the add-field wrapping editor back to QLineEdit.

        On save=True we serialize to markdown (``[text](url)`` and
        STX/ETX bold markers) rather than plain text — ``toPlainText``
        would strip the URL from a link span, silently destroying the
        user's work on a simple click-away.  The line edit shows the
        raw markdown until the user expands again, at which point
        ``_expand_add_field`` parses it back into anchor/bold spans.
        """
        wrap = self._add_popup
        if wrap is None:
            return
        self._add_popup = None
        if sip.isdeleted(wrap):
            return
        if save:
            new_text = _ChecklistItemWidget \
                ._serialize_popup_markdown(wrap).replace('\n', ' ')
        else:
            new_text = ''
        wrap.setVisible(False)
        self._layout.removeWidget(wrap)
        wrap.deleteLater()
        if self._add_field and not sip.isdeleted(self._add_field):
            self._add_field.setVisible(True)
            if save and new_text:
                self._add_field.setText(new_text)

    def _toggle_completed(self) -> None:
        # Flush popups before rebuild — see _flush_popups docstring.
        self._flush_popups()
        self._completed_visible = not self._completed_visible
        self._rebuild()

