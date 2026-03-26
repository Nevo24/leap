"""Preset editor dialog for Leap Monitor.

A file-editor metaphor dialog for managing named Leap presets.
Presets are ordered lists of messages (multi-message bundles). Each
message is shown as a card in a scrollable area. Preset management
(Save/Save As/Delete) is independent from applying the active preset
(Apply & Close).
"""

from PyQt5.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)
from PyQt5.QtCore import Qt

from leap.monitor.themes import current_theme
from leap.monitor.ui.image_text_edit import ImageTextEdit

from leap.monitor.pr_tracking.config import (
    delete_named_preset, load_dialog_geometry, load_saved_presets,
    load_selected_direct_preset_name, load_selected_preset_name,
    save_dialog_geometry, save_named_preset,
    save_selected_direct_preset_name, save_selected_preset_name,
)
from leap.monitor.ui.table_helpers import (
    APPLY_PR_BTN, APPLY_QUICK_MSG_BTN, PR_PRESET_HINT,
    QUICK_MSG_PRESET_HINT,
)


MAX_PRESET_NAME_LEN = 70


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

        # Header row: label + remove button
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel(f'Message {index + 1}')
        self._label.setStyleSheet(f'border: none; font-weight: bold; font-size: {current_theme().font_size_base}px;')
        header.addWidget(self._label)
        header.addStretch()
        self._remove_btn = QPushButton('\u00d7')
        self._remove_btn.setFixedSize(20, 20)
        t = current_theme()
        self._remove_btn.setStyleSheet(
            f'QPushButton {{ border: none; color: {t.text_muted}; font-size: {t.font_size_base}px; }}'
            f'QPushButton:hover {{ color: {t.accent_red}; font-weight: bold; }}'
        )
        self._remove_btn.setToolTip('Remove this message')
        self._remove_btn.clicked.connect(on_remove)
        header.addWidget(self._remove_btn)
        layout.addLayout(header)

        # Text editor (supports image paste via Cmd+V)
        self._text_edit = ImageTextEdit()
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


class PresetEditorDialog(QDialog):
    """Dialog to edit Leap presets with named entries."""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Edit Presets')
        self.resize(780, 500)
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
        hint_pr.setStyleSheet(f'color: {current_theme().text_muted}; font-size: {current_theme().font_size_small}px; margin-bottom: 0px;')
        dlg_layout.addWidget(hint_pr)

        hint_quick = QLabel(QUICK_MSG_PRESET_HINT)
        hint_quick.setWordWrap(True)
        hint_quick.setIndent(0)
        hint_quick.setStyleSheet(f'color: {current_theme().text_muted}; font-size: {current_theme().font_size_small}px; margin-bottom: 4px;')
        dlg_layout.addWidget(hint_quick)

        # Preset row: combo + New + Save + Save As... + Delete
        preset_layout = QHBoxLayout()

        self._combo = QComboBox()
        self._combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._combo.setMinimumWidth(160)
        preset_layout.addWidget(self._combo, 1)

        new_btn = QPushButton('New')
        self._save_btn = QPushButton('Save')
        save_as_btn = QPushButton('Save As...')
        self._delete_btn = QPushButton('Delete')
        preset_layout.addWidget(new_btn)
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

        # Load the currently selected preset (if any)
        selected_name = load_selected_preset_name()
        if selected_name:
            presets = load_saved_presets()
            if selected_name in presets:
                self._messages = list(presets[selected_name])
                if not self._messages:
                    self._messages = ['']
        self._current_name = selected_name

        self._rebuild_cards()

        # Bottom buttons: two Apply & Close buttons + Cancel
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        apply_pr_btn = QPushButton(APPLY_PR_BTN)
        apply_direct_btn = QPushButton(APPLY_QUICK_MSG_BTN)
        cancel_btn = QPushButton('Cancel')
        btn_layout.addWidget(apply_pr_btn)
        btn_layout.addWidget(apply_direct_btn)
        btn_layout.addWidget(cancel_btn)
        dlg_layout.addLayout(btn_layout)

        # Connect signals
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        new_btn.clicked.connect(self._on_new)
        self._save_btn.clicked.connect(self._on_save)
        save_as_btn.clicked.connect(self._on_save_as)
        self._delete_btn.clicked.connect(self._on_delete)
        apply_pr_btn.clicked.connect(self._on_apply_pr)
        apply_direct_btn.clicked.connect(self._on_apply_direct)
        cancel_btn.clicked.connect(self.reject)

        self._refresh_combo(selected_name)

        # If no preset was selected but combo has items, sync _current_name
        # with what the combo is visually showing so Apply works correctly.
        if not self._current_name and self._combo.count() > 0:
            fallback = self._combo.currentText()
            if fallback:
                self._current_name = fallback
                presets = load_saved_presets()
                self._messages = list(presets.get(fallback, ['']))
                if not self._messages:
                    self._messages = ['']
                self._rebuild_cards()
                self._update_button_states()

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

    def _on_card_text_changed(self, index: int, text: str) -> None:
        """Update the internal message list when a card's text changes."""
        if 0 <= index < len(self._messages):
            self._messages[index] = text

    # -- Preset management --------------------------------------------------

    def _update_button_states(self) -> None:
        has_preset = bool(self._current_name)
        self._save_btn.setEnabled(has_preset)
        self._delete_btn.setEnabled(has_preset)

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
        self._current_name = name
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
        self._current_name = name
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
        self._current_name = name
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
            self._current_name = ''
            self._unsaved = False
            self._refresh_combo()
            # Auto-load the first remaining preset
            fallback = self._combo.currentText()
            if fallback:
                presets = load_saved_presets()
                self._messages = list(presets.get(fallback, ['']))
                if not self._messages:
                    self._messages = ['']
                self._current_name = fallback
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

    def _on_apply_pr(self) -> None:
        """Apply the current preset to the PR thread context combo and close.

        Rejects multi-message presets — PR thread context must be single-message.
        """
        if len(self._messages) > 1 and any(m.strip() for m in self._messages[1:]):
            QMessageBox.warning(
                self, 'Multi-Message Preset',
                'PR thread context must be a single-message preset.\n\n'
                'This preset has multiple messages. Only single-message '
                'presets can be used as PR thread context.',
            )
            return
        if not self._maybe_save_unsaved():
            return
        save_selected_preset_name(self._current_name)
        self.accept()

    def reject(self) -> None:
        """Intercept Cancel / X-button to check for unsaved changes."""
        if not self._maybe_save_unsaved():
            return  # User cancelled — stay in dialog
        super().reject()

    def done(self, result: int) -> None:
        """Save dialog size on close."""
        save_dialog_geometry('preset_editor', self.width(), self.height())
        super().done(result)

    def _on_apply_direct(self) -> None:
        """Apply the current preset to the Message bundle combo and close."""
        if not self._maybe_save_unsaved():
            return
        save_selected_direct_preset_name(self._current_name)
        self.accept()
