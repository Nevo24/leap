"""Preset editor dialog for Leap Monitor.

A file-editor metaphor dialog for managing named Leap presets.
Presets are ordered lists of messages (multi-message bundles). Each
message is shown as a card in a scrollable area. Preset management
(Save/Save As/Delete) is independent from applying the active preset
(Apply & Close).
"""

import sip

from PyQt5.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)
from PyQt5.QtCore import QEvent, QMimeData, QPoint, QTimer, Qt
from PyQt5.QtGui import QDrag, QPainter, QPixmap

from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.themes import current_theme
from leap.monitor.ui.image_text_edit import ImageTextEdit

from leap.monitor.pr_tracking.config import (
    delete_named_preset, load_dialog_geometry, load_preset_editor_last_name,
    load_saved_presets, save_dialog_geometry, save_named_preset,
    save_preset_editor_last_name,
)
from leap.monitor.ui.table_helpers import (
    PR_PRESET_HINT, QUICK_MSG_PRESET_HINT,
)


MAX_PRESET_NAME_LEN = 70


class _DragHandle(QLabel):
    """A grip label that initiates a drag on mouse-press-and-move."""

    def __init__(self, parent: '_MessageCard') -> None:
        super().__init__('\u2261', parent)  # ≡ hamburger icon
        t = current_theme()
        self.setFixedWidth(20)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.OpenHandCursor)
        self.setStyleSheet(
            f'border: none; color: {t.text_muted};'
        )
        self.setToolTip('Drag to reorder')
        self._drag_start: QPoint = QPoint()
        self._card = parent

    def mousePressEvent(self, event: object) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start = event.pos()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event: object) -> None:
        if not (event.buttons() & Qt.LeftButton):
            return
        if (event.pos() - self._drag_start).manhattanLength() < 10:
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData('application/x-leap-preset-card', str(self._card.card_index).encode())
        drag.setMimeData(mime)
        # Create a small translucent pixmap of the card
        pixmap = self._card.grab()
        scaled = pixmap.scaledToWidth(min(pixmap.width(), 320), Qt.SmoothTransformation)
        scaled_pixmap = QPixmap(scaled.size())
        scaled_pixmap.fill(Qt.transparent)
        painter = QPainter(scaled_pixmap)
        painter.setOpacity(0.7)
        painter.drawPixmap(0, 0, scaled)
        painter.end()
        drag.setPixmap(scaled_pixmap)
        drag.exec_(Qt.MoveAction)
        # The card (and this handle) may have been destroyed by _rebuild_cards
        # during the blocking drag.exec_() call.
        if not sip.isdeleted(self):
            self.setCursor(Qt.OpenHandCursor)

    def mouseReleaseEvent(self, event: object) -> None:
        if not sip.isdeleted(self):
            self.setCursor(Qt.OpenHandCursor)


class _MessageCard(QFrame):
    """A single message card with a header, text editor, and remove button."""

    def __init__(
        self,
        index: int,
        text: str,
        on_text_changed: 'callable',
        on_remove: 'callable',
        parent: QWidget = None,
    ) -> None:
        super().__init__(parent)
        self.card_index: int = index
        self.setFrameShape(QFrame.StyledPanel)
        self.setObjectName('messageCard')
        t = current_theme()
        self.setStyleSheet(
            f'QFrame#messageCard {{ border: 1px solid {t.popup_border};'
            f' border-radius: {t.border_radius}px;'
            f' padding: 4px; margin: 2px 0px; }}'
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)

        # Header row: drag handle + label + remove button
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._drag_handle = _DragHandle(self)
        header.addWidget(self._drag_handle)
        self._label = QLabel(f'Message {index + 1}')
        # Omit font-size so the dialog-level ZoomMixin stylesheet cascades in.
        self._label.setStyleSheet('border: none; font-weight: bold;')
        header.addWidget(self._label)
        header.addStretch()
        self._remove_btn = QPushButton('\u00d7')
        self._remove_btn.setFixedSize(20, 20)
        t = current_theme()
        self._remove_btn.setStyleSheet(
            f'QPushButton {{ border: none; color: {t.text_muted}; }}'
            f'QPushButton:hover {{ color: {t.accent_red}; font-weight: bold; }}'
        )
        self._remove_btn.setToolTip('Remove this message')
        self._remove_btn.clicked.connect(on_remove)
        header.addWidget(self._remove_btn)
        layout.addLayout(header)

        # Text editor (supports image paste via Cmd+V)
        self._text_edit = ImageTextEdit()
        self._text_edit.setAcceptDrops(False)  # let drag events reach the scroll viewport
        self._text_edit.setPlaceholderText(f'Message {index + 1} content... (paste images with Cmd+V)')
        self._text_edit.setPlainText(text)
        self._text_edit.setFixedHeight(80)
        self._text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self._text_edit.textChanged.connect(
            lambda: on_text_changed(self._text_edit.resolved_text())
        )
        layout.addWidget(self._text_edit)

    def set_removable(self, removable: bool) -> None:
        """Enable or disable the remove button (disabled when only 1 card)."""
        self._remove_btn.setEnabled(removable)
        self._remove_btn.setVisible(removable)

    def get_text(self) -> str:
        """Return the current text of this card (with image placeholders resolved)."""
        return self._text_edit.resolved_text()

    def focus_editor(self) -> None:
        """Set focus to this card's text editor."""
        self._text_edit.setFocus()


class PresetEditorDialog(ZoomMixin, QDialog):
    """Dialog to edit Leap presets with named entries."""

    _DEFAULT_SIZE = (780, 500)

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Edit Presets')
        self.resize(*self._DEFAULT_SIZE)
        saved = load_dialog_geometry('preset_editor')
        if saved:
            self.resize(saved[0], saved[1])

        self._current_name: str = ''
        self._refreshing: bool = False
        self._unsaved: bool = False  # True after New, before first Save
        self._messages: list[str] = ['']
        self._cards: list[_MessageCard] = []

        dlg_layout = QVBoxLayout(self)

        hint_pr = QLabel(PR_PRESET_HINT)
        hint_pr.setWordWrap(True)
        hint_pr.setIndent(0)
        hint_pr.setStyleSheet(f'color: {current_theme().text_muted}; margin-bottom: 0px;')
        dlg_layout.addWidget(hint_pr)

        hint_quick = QLabel(QUICK_MSG_PRESET_HINT)
        hint_quick.setWordWrap(True)
        hint_quick.setIndent(0)
        hint_quick.setStyleSheet(f'color: {current_theme().text_muted}; margin-bottom: 4px;')
        dlg_layout.addWidget(hint_quick)

        # Preset row: combo + Save + Save As... + Delete
        preset_layout = QHBoxLayout()

        self._combo = QComboBox()
        self._combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._combo.setMinimumWidth(160)
        preset_layout.addWidget(self._combo, 1)

        self._save_btn = QPushButton('Save')
        save_as_btn = QPushButton('Save As...')
        self._delete_btn = QPushButton('Delete')
        preset_layout.addWidget(self._save_btn)
        preset_layout.addWidget(save_as_btn)
        preset_layout.addWidget(self._delete_btn)
        dlg_layout.addLayout(preset_layout)

        # Scrollable area for message cards
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_content = QWidget()
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.setContentsMargins(4, 4, 4, 4)
        self._scroll_layout.setSpacing(4)
        self._scroll.setWidget(self._scroll_content)
        dlg_layout.addWidget(self._scroll, 1)

        # Drop indicator line (hidden by default, shown between cards during drag)
        self._drop_indicator = QWidget(self._scroll.viewport())
        self._drop_indicator.setFixedHeight(2)
        self._drop_indicator.setStyleSheet(
            f'background-color: {current_theme().accent_blue};')
        self._drop_indicator.setVisible(False)
        self._drop_indicator.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._scroll.viewport().setAcceptDrops(True)
        self._scroll.viewport().installEventFilter(self)

        # Load the preset the editor was last focused on (if it still exists)
        last_name = load_preset_editor_last_name()
        presets = load_saved_presets()
        if last_name and last_name in presets:
            self._messages = list(presets[last_name])
            if not self._messages:
                self._messages = ['']
            selected_name = last_name
        else:
            selected_name = ''
            if last_name:
                # Saved selection no longer exists — clear it so we don't
                # keep trying to restore a phantom preset.
                save_preset_editor_last_name('')
        self._current_name = selected_name

        self._rebuild_cards()

        # Bottom buttons: New + Cancel at bottom-left
        btn_layout = QHBoxLayout()
        new_btn = QPushButton('New')
        cancel_btn = QPushButton('Cancel')
        btn_layout.addWidget(new_btn)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addStretch()
        dlg_layout.addLayout(btn_layout)

        # Connect signals
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        new_btn.clicked.connect(self._on_new)
        self._save_btn.clicked.connect(self._on_save)
        save_as_btn.clicked.connect(self._on_save_as)
        self._delete_btn.clicked.connect(self._on_delete)
        cancel_btn.clicked.connect(self.reject)

        self._refresh_combo(selected_name)

        # If no preset was selected but combo has items, sync _current_name
        # with what the combo is visually showing so Save/Delete target
        # the right preset when the user hits them immediately.
        if not self._current_name and self._combo.count() > 0:
            fallback = self._combo.currentText()
            if fallback:
                self._set_current_name(fallback)
                presets = load_saved_presets()
                self._messages = list(presets.get(fallback, ['']))
                if not self._messages:
                    self._messages = ['']
                self._rebuild_cards()
                self._update_button_states()

        # Split zoom: text (message cards' QTextEdit) vs. buttons/chrome.
        # ZoomMixin queries _card_text_widgets() on each Ctrl+wheel/key to
        # handle cards being added/removed/reordered.
        self._init_zoom(
            pref_key='preset_editor_font_size',
            content_pref_key='preset_editor_text_font_size',
            content_widgets=self._card_text_widgets,
        )

    def _card_text_widgets(self) -> list:
        """Current list of content widgets for split-zoom routing.

        Includes the scroll area (so mouse-wheel over *any* part of the
        scrollable cards region — gaps between cards, card headers, the
        viewport itself — routes to content zoom), plus each card's text
        editor for explicit lookup.
        """
        widgets = []
        if getattr(self, '_scroll', None) is not None:
            widgets.append(self._scroll)
        widgets.extend(
            c._text_edit for c in self._cards if c is not None)
        return widgets

    def _apply_zoom_content_font_size(self) -> None:  # type: ignore[override]
        """Apply content zoom and scale each card's text-edit height so
        more (zoomed) lines stay visible without clipping."""
        super()._apply_zoom_content_font_size()
        pt = getattr(self, '_zoom_content_font_size', None)
        if pt is None:
            return
        base = current_theme().font_size_base
        scale = max(0.5, pt / base)
        new_h = max(50, int(80 * scale))
        for card in self._cards:
            if card is not None and card._text_edit is not None:
                card._text_edit.setFixedHeight(new_h)

    # -- Card management ----------------------------------------------------

    def _rebuild_cards(self) -> None:
        """Rebuild the scrollable card list from self._messages."""
        # Remove old cards
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()

        # Remove old "+ Add Message" button if present
        if hasattr(self, '_add_btn') and self._add_btn is not None:
            self._add_btn.setParent(None)
            self._add_btn.deleteLater()

        # Remove spacer if present
        while self._scroll_layout.count():
            item = self._scroll_layout.takeAt(0)
            # Widgets were already handled above; just clear layout items
            if item.widget():
                item.widget().setParent(None)

        # Create a card for each message
        for i, text in enumerate(self._messages):
            card = _MessageCard(
                index=i,
                text=text,
                on_text_changed=lambda t, idx=i: self._on_card_text_changed(idx, t),
                on_remove=lambda checked=False, idx=i: self._remove_message(idx),
            )
            card.set_removable(len(self._messages) > 1)
            self._cards.append(card)
            self._scroll_layout.addWidget(card)

        # "+ Add Message" button
        self._add_btn = QPushButton('+ Add Message')
        t = current_theme()
        self._add_btn.setStyleSheet(
            f'QPushButton {{ color: {t.text_muted}; border: 1px dashed {t.border_solid};'
            f' border-radius: {t.border_radius}px; padding: 6px; }}'
            f'QPushButton:hover {{ color: {t.text_primary}; border-color: {t.text_secondary}; }}'
        )
        self._add_btn.clicked.connect(self._add_message)
        self._scroll_layout.addWidget(self._add_btn)

        # Push cards to the top
        self._scroll_layout.addStretch()

        # Apply text-zoom size to the freshly-built cards (via ZoomMixin)
        if hasattr(self, '_zoom_content_pref_key'):
            self._zoom_reapply_content()

    def _add_message(self) -> None:
        """Append a new empty message and rebuild cards."""
        self._messages.append('')
        self._rebuild_cards()
        # Focus the new card
        if self._cards:
            self._cards[-1].focus_editor()

    def _remove_message(self, index: int) -> None:
        """Remove a message by index and rebuild cards. Min 1 message."""
        if len(self._messages) <= 1:
            return
        del self._messages[index]
        self._rebuild_cards()

    # -- Drag-and-drop (event filter on scroll viewport) ---------------------

    def _drop_target_index(self, viewport_y: int) -> int:
        """Return the card index where a drop at viewport_y should insert BEFORE."""
        for card in self._cards:
            mapped = self._scroll.viewport().mapFromGlobal(
                card.mapToGlobal(QPoint(0, 0)))
            mid = mapped.y() + card.height() // 2
            if viewport_y < mid:
                return card.card_index
        # Past the last card → append at the end
        return len(self._cards)

    def _show_drop_indicator(self, target_index: int) -> None:
        """Position the 2px indicator line above the target card."""
        target_y = 0
        if target_index < len(self._cards):
            card = self._cards[target_index]
            mapped = self._scroll.viewport().mapFromGlobal(
                card.mapToGlobal(QPoint(0, 0)))
            target_y = mapped.y()
        elif self._cards:
            # After the last card
            card = self._cards[-1]
            mapped = self._scroll.viewport().mapFromGlobal(
                card.mapToGlobal(QPoint(0, 0)))
            target_y = mapped.y() + card.height()
        self._drop_indicator.setGeometry(
            0, target_y - 1, self._scroll.viewport().width(), 2)
        self._drop_indicator.setVisible(True)
        self._drop_indicator.raise_()

    _MIME_TYPE = 'application/x-leap-preset-card'

    def eventFilter(self, obj: object, event: object) -> bool:
        """Handle drag events on the scroll viewport (zoom is handled by ZoomMixin)."""
        if obj is not self._scroll.viewport():
            return super().eventFilter(obj, event)

        if event.type() == QEvent.DragEnter:
            if event.mimeData().hasFormat(self._MIME_TYPE):
                event.acceptProposedAction()
                return True

        elif event.type() == QEvent.DragMove:
            if event.mimeData().hasFormat(self._MIME_TYPE):
                event.acceptProposedAction()
                target = self._drop_target_index(event.pos().y())
                self._show_drop_indicator(target)
                return True

        elif event.type() == QEvent.DragLeave:
            self._drop_indicator.setVisible(False)
            return True

        elif event.type() == QEvent.Drop:
            self._drop_indicator.setVisible(False)
            if event.mimeData().hasFormat(self._MIME_TYPE):
                event.acceptProposedAction()
                src = int(bytes(event.mimeData().data(self._MIME_TYPE)).decode())
                dst = self._drop_target_index(event.pos().y())
                # Adjust destination when moving down (item removed before insert)
                if dst > src:
                    dst -= 1
                if src != dst and 0 <= src < len(self._messages):
                    msg = self._messages.pop(src)
                    self._messages.insert(dst, msg)
                    QTimer.singleShot(0, self._rebuild_cards)
                return True

        return super().eventFilter(obj, event)

    def _on_card_text_changed(self, index: int, text: str) -> None:
        """Update the internal message list when a card's text changes."""
        if 0 <= index < len(self._messages):
            self._messages[index] = text

    # -- Preset management --------------------------------------------------

    def _update_button_states(self) -> None:
        has_preset = bool(self._current_name)
        self._save_btn.setEnabled(has_preset)
        self._delete_btn.setEnabled(has_preset)

    def _set_current_name(self, name: str) -> None:
        """Update ``_current_name`` and persist it so the editor reopens here."""
        self._current_name = name
        save_preset_editor_last_name(name)

    def _refresh_combo(self, select_name: str = '') -> None:
        self._refreshing = True
        self._combo.clear()
        for name in sorted(load_saved_presets()):
            self._combo.addItem(name)
        if select_name:
            idx = self._combo.findText(select_name)
            if idx >= 0:
                self._combo.setCurrentIndex(idx)
        self._refreshing = False
        self._update_button_states()

    def _on_combo_changed(self, index: int) -> None:
        if self._refreshing:
            return
        name = self._combo.currentText()
        if not name:
            return
        # Check for unsaved changes before switching away
        if not self._maybe_save_unsaved():
            # User cancelled — revert combo to previous preset
            self._refreshing = True
            idx = self._combo.findText(self._current_name)
            if idx >= 0:
                self._combo.setCurrentIndex(idx)
            self._refreshing = False
            return
        presets = load_saved_presets()
        self._messages = list(presets.get(name, ['']))
        if not self._messages:
            self._messages = ['']
        self._rebuild_cards()
        self._set_current_name(name)
        self._unsaved = False
        self._update_button_states()

    def _prompt_and_save(self) -> None:
        """Prompt for a preset name and save. Used by Save As."""
        prev_name = ''
        while True:
            dlg = QInputDialog(self)
            dlg.setWindowTitle('Save Preset As')
            dlg.setLabelText('Name for this preset:')
            dlg.setTextValue(prev_name)
            ok = dlg.exec_() == QInputDialog.Accepted
            name = dlg.textValue()
            if not ok or not name.strip():
                return
            name = name.strip()
            prev_name = name
            if len(name) > MAX_PRESET_NAME_LEN:
                QMessageBox.warning(
                    self, 'Name Too Long',
                    f'Preset name must be {MAX_PRESET_NAME_LEN} characters or fewer '
                    f'(currently {len(name)}).',
                )
                continue
            existing = load_saved_presets()
            if name in existing:
                reply = QMessageBox.question(
                    self, 'Overwrite Preset',
                    f"A preset named '{name}' already exists. Overwrite?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    continue
            break
        save_named_preset(name, list(self._messages))
        self._set_current_name(name)
        self._unsaved = False
        self._refresh_combo(name)

    def _on_new(self) -> None:
        if not self._maybe_save_unsaved():
            return
        prev_name = ''
        while True:
            dlg = QInputDialog(self)
            dlg.setWindowTitle('New Preset')
            dlg.setLabelText('Name for the new preset:')
            dlg.setTextValue(prev_name)
            ok = dlg.exec_() == QInputDialog.Accepted
            name = dlg.textValue()
            if not ok or not name.strip():
                return
            name = name.strip()
            prev_name = name
            if len(name) > MAX_PRESET_NAME_LEN:
                QMessageBox.warning(
                    self, 'Name Too Long',
                    f'Preset name must be {MAX_PRESET_NAME_LEN} characters or fewer '
                    f'(currently {len(name)}).',
                )
                continue
            existing = load_saved_presets()
            if name in existing:
                reply = QMessageBox.question(
                    self, 'Name Exists',
                    f"A preset named '{name}' already exists. Overwrite with empty?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    continue
            break
        save_named_preset(name, [''])
        self._set_current_name(name)
        self._unsaved = True
        self._messages = ['']
        self._rebuild_cards()
        self._refresh_combo(name)

    def _on_save(self) -> None:
        if not self._current_name:
            return
        current_messages = list(self._messages)
        saved_messages = load_saved_presets().get(self._current_name, [''])
        if current_messages == saved_messages:
            return  # No changes — nothing to save
        if not self._unsaved:
            reply = QMessageBox.question(
                self, 'Overwrite Preset',
                f"Overwrite preset '{self._current_name}'?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        save_named_preset(self._current_name, current_messages)
        self._unsaved = False

    def _on_save_as(self) -> None:
        self._prompt_and_save()

    def _on_delete(self) -> None:
        name = self._current_name
        if not name:
            return
        reply = QMessageBox.question(
            self, 'Delete Preset',
            f"Delete saved preset '{name}'?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            delete_named_preset(name)
            self._set_current_name('')
            self._unsaved = False
            self._refresh_combo()
            # Auto-load the first remaining preset
            fallback = self._combo.currentText()
            if fallback:
                presets = load_saved_presets()
                self._messages = list(presets.get(fallback, ['']))
                if not self._messages:
                    self._messages = ['']
                self._set_current_name(fallback)
            else:
                self._messages = ['']
            self._rebuild_cards()
            self._update_button_states()

    def _maybe_save_unsaved(self) -> bool:
        """Check for unsaved changes and offer to save. Returns False if cancelled."""
        name = self._current_name
        if name:
            saved_messages = load_saved_presets().get(name, [''])
            if list(self._messages) != saved_messages:
                reply = QMessageBox.question(
                    self, 'Unsaved Changes',
                    f"Save changes to '{name}' before closing?",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                )
                if reply == QMessageBox.Cancel:
                    return False
                if reply == QMessageBox.Yes:
                    save_named_preset(name, list(self._messages))
        return True

    def reject(self) -> None:
        """Intercept Cancel / X-button to check for unsaved changes."""
        if not self._maybe_save_unsaved():
            return  # User cancelled — stay in dialog
        super().reject()

    def done(self, result: int) -> None:
        """Save dialog size on close."""
        save_dialog_geometry('preset_editor', self.width(), self.height())
        super().done(result)
