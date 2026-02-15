"""Context editor dialog for ClaudeQ Monitor.

A file-editor metaphor dialog for managing named CQ context presets.
Preset management (Save/Save As/Delete) is independent from applying
the active context (Apply & Close).
"""

from PyQt5.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog,
    QLabel, QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from claudeq.monitor.mr_tracking.config import (
    delete_named_context, load_saved_contexts, load_selected_context_name,
    save_named_context, save_selected_context_name,
)


MAX_CONTEXT_NAME_LEN = 70


class ContextEditorDialog(QDialog):
    """Dialog to edit CQ context text with named presets."""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Edit CQ Context')
        self.resize(500, 400)

        self._current_name: str = ''
        self._refreshing: bool = False
        self._unsaved: bool = False  # True after New, before first Save

        dlg_layout = QVBoxLayout(self)

        hint = QLabel('This text will be attached to every message sent from the monitor to CQ.')
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #999; font-size: 12px; margin-bottom: 4px;')
        dlg_layout.addWidget(hint)

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

        self._text_edit = QTextEdit()
        self._text_edit.setPlaceholderText(
            'Enter context here (e.g. project conventions, review instructions)...'
        )
        dlg_layout.addWidget(self._text_edit)

        # Load the currently selected preset (if any)
        selected_name = load_selected_context_name()
        if selected_name:
            contexts = load_saved_contexts()
            if selected_name in contexts:
                self._text_edit.setPlainText(contexts[selected_name])
        self._current_name = selected_name

        # Bottom buttons: Apply & Close + Cancel
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        apply_btn = QPushButton('Apply && Close')
        cancel_btn = QPushButton('Cancel')
        btn_layout.addWidget(apply_btn)
        btn_layout.addWidget(cancel_btn)
        dlg_layout.addLayout(btn_layout)

        # Connect signals
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        new_btn.clicked.connect(self._on_new)
        self._save_btn.clicked.connect(self._on_save)
        save_as_btn.clicked.connect(self._on_save_as)
        self._delete_btn.clicked.connect(self._on_delete)
        apply_btn.clicked.connect(self._on_apply)
        cancel_btn.clicked.connect(self.reject)

        self._refresh_combo(selected_name)

    def _update_button_states(self) -> None:
        has_preset = bool(self._current_name)
        self._save_btn.setEnabled(has_preset)
        self._delete_btn.setEnabled(has_preset)

    def _refresh_combo(self, select_name: str = '') -> None:
        self._refreshing = True
        self._combo.clear()
        for name in sorted(load_saved_contexts()):
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
        contexts = load_saved_contexts()
        text = contexts.get(name, '')
        self._text_edit.setPlainText(text)
        self._current_name = name
        self._unsaved = False
        self._update_button_states()

    def _prompt_and_save(self) -> None:
        """Prompt for a preset name and save. Used by Save As."""
        prev_name = ''
        while True:
            dlg = QInputDialog(self)
            dlg.setWindowTitle('Save Context As')
            dlg.setLabelText('Name for this context:')
            dlg.setTextValue(prev_name)
            ok = dlg.exec_() == QInputDialog.Accepted
            name = dlg.textValue()
            if not ok or not name.strip():
                return
            name = name.strip()
            prev_name = name
            if len(name) > MAX_CONTEXT_NAME_LEN:
                QMessageBox.warning(
                    self, 'Name Too Long',
                    f'Context name must be {MAX_CONTEXT_NAME_LEN} characters or fewer '
                    f'(currently {len(name)}).',
                )
                continue
            existing = load_saved_contexts()
            if name in existing:
                reply = QMessageBox.question(
                    self, 'Overwrite Context',
                    f"A context named '{name}' already exists. Overwrite?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    continue
            break
        save_named_context(name, self._text_edit.toPlainText())
        self._current_name = name
        self._unsaved = False
        self._refresh_combo(name)

    def _on_new(self) -> None:
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
            if len(name) > MAX_CONTEXT_NAME_LEN:
                QMessageBox.warning(
                    self, 'Name Too Long',
                    f'Context name must be {MAX_CONTEXT_NAME_LEN} characters or fewer '
                    f'(currently {len(name)}).',
                )
                continue
            existing = load_saved_contexts()
            if name in existing:
                reply = QMessageBox.question(
                    self, 'Name Exists',
                    f"A preset named '{name}' already exists. Overwrite with empty?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    continue
            break
        save_named_context(name, '')
        self._current_name = name
        self._unsaved = True
        self._text_edit.clear()
        self._refresh_combo(name)

    def _on_save(self) -> None:
        if not self._current_name:
            return
        current_text = self._text_edit.toPlainText()
        saved_text = load_saved_contexts().get(self._current_name, '')
        if current_text == saved_text:
            return  # No changes — nothing to save
        if not self._unsaved:
            reply = QMessageBox.question(
                self, 'Overwrite Preset',
                f"Overwrite preset '{self._current_name}'?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        save_named_context(self._current_name, current_text)
        self._unsaved = False

    def _on_save_as(self) -> None:
        self._prompt_and_save()

    def _on_delete(self) -> None:
        name = self._current_name
        if not name:
            return
        reply = QMessageBox.question(
            self, 'Delete Context',
            f"Delete saved context '{name}'?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            delete_named_context(name)
            self._current_name = ''
            self._unsaved = False
            self._refresh_combo()
            # Auto-load the first remaining preset
            fallback = self._combo.currentText()
            if fallback:
                contexts = load_saved_contexts()
                self._text_edit.setPlainText(contexts.get(fallback, ''))
                self._current_name = fallback
            else:
                self._text_edit.clear()
            self._update_button_states()

    def _on_apply(self) -> None:
        # Check if text differs from the saved version
        name = self._current_name
        if name:
            saved_text = load_saved_contexts().get(name, '')
            if self._text_edit.toPlainText() != saved_text:
                reply = QMessageBox.question(
                    self, 'Unsaved Changes',
                    f"Save changes to '{name}' before closing?",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                )
                if reply == QMessageBox.Cancel:
                    return
                if reply == QMessageBox.Yes:
                    save_named_context(name, self._text_edit.toPlainText())
        save_selected_context_name(name)
        self.accept()
