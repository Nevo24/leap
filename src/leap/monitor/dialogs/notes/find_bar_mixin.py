"""Find bar (Cmd+F) mixin for ``NotesDialog``.

Provides incremental, Chrome-style find within a text-mode note:
typing in the bar jumps to the first match and updates the "K of N"
counter on every keystroke; Enter/Shift+Enter cycle next/prev with
wrap-around; a no-match tints the input red until the next successful
match.

The host class is expected to:
* call :meth:`_build_find_bar` from ``__init__`` and assign the
  returned ``QWidget`` to ``self._find_bar`` (then add it to the
  right-hand layout — placement is the host's choice).
* expose ``self._editor`` (the searchable ``QTextEdit``),
  ``self._current_name`` (note state guard), ``self._current_mode``
  + ``self._MODE_TEXT`` (don't show find on checklist notes), and
  ``self._buttons_font_size`` (zoom target for the input's QSS).
* install ``self`` as an event filter on ``self._find_input`` and
  forward Enter / Shift+Enter / Escape into :meth:`_find_next`,
  :meth:`_find_prev`, :meth:`_hide_find_bar` (the host owns the
  ``QEvent.KeyPress`` filter — the mixin only needs the slots).
"""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QTextCursor, QTextDocument
from PyQt5.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget,
)

from leap.monitor.themes import current_theme


class _NotesFindBarMixin:
    """Find bar (Cmd+F) implementation for ``NotesDialog``."""

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
