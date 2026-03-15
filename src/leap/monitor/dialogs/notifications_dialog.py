"""Notifications configuration dialog for Leap Monitor."""

import os
from functools import partial
from typing import Any, Optional

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QCursor, QFont
from PyQt5.QtWidgets import (
    QAction, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QGridLayout, QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget,
)

from leap.monitor.pr_tracking.config import (
    MACOS_SYSTEM_SOUNDS, load_dialog_geometry, save_dialog_geometry,
)
from leap.monitor.ui.table_helpers import HoverIconButton, menu_btn_style

# Speaker SVG icon — uses #aaa as the recolorable placeholder (same as other icons).
_SPEAKER_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    b'<path d="M288 64L160 192H64v128h96l128 128V64z"'
    b' fill="#aaa" stroke="#aaa" stroke-width="16" stroke-linejoin="round"/>'
    b'<path d="M352 192c20 24 32 56 32 64s-12 40-32 64"'
    b' fill="none" stroke="#aaa" stroke-width="40" stroke-linecap="round"/>'
    b'<path d="M400 144c40 48 56 88 56 112s-16 64-56 112"'
    b' fill="none" stroke="#aaa" stroke-width="40" stroke-linecap="round"/>'
    b'</svg>'
)

_BROWSE_SENTINEL = 'Browse...'

# Display labels for each notification type
_TYPE_LABELS = {
    'pr_unresponded': 'New unresponded threads',
    'pr_all_responded': 'All threads responded',
    'pr_approved': 'PR approved',
    'session_completed': 'Session finished processing',
    'session_needs_permission': 'Session needs permission',
    'session_needs_input': 'Session needs input',
    'session_interrupted': 'Session was interrupted',
    'review_requested': 'Review requested',
    'assigned': 'Assigned to you',
    'mentioned': 'Mentioned',
}

# Grouped type keys with section titles.
_SECTIONS: list[tuple[str, list[str]]] = [
    ('PR / Session Tracking', [
        'pr_unresponded', 'pr_all_responded', 'pr_approved', 'session_completed',
        'session_needs_permission', 'session_needs_input', 'session_interrupted',
    ]),
    ('GitLab / GitHub Notifications', [
        'review_requested', 'assigned', 'mentioned',
    ]),
]

# Flat ordered list (derived from sections) for external consumers.
_TYPE_ORDER = [key for _, keys in _SECTIONS for key in keys]


class NotificationsDialog(QDialog):
    """Dialog for configuring per-type notification preferences."""

    def __init__(
        self,
        current_prefs: dict[str, dict[str, Any]],
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle('Notifications')
        self.resize(520, 340)
        saved = load_dialog_geometry('notifications')
        if saved:
            self.resize(saved[0], saved[1])

        layout = QVBoxLayout(self)

        grid = QGridLayout()
        grid.addWidget(QLabel(''), 0, 0)
        dock_header = QLabel('Dock Badge')
        dock_header.setAlignment(Qt.AlignCenter)
        grid.addWidget(dock_header, 0, 1)
        banner_header = QLabel('Banner')
        banner_header.setAlignment(Qt.AlignCenter)
        grid.addWidget(banner_header, 0, 2)
        sound_header = QLabel('Sound')
        sound_header.setAlignment(Qt.AlignCenter)
        grid.addWidget(sound_header, 0, 3)

        self._checks: dict[str, dict[str, QCheckBox]] = {}
        self._sound_combos: dict[str, QComboBox] = {}
        self._browsing: bool = False  # guard against re-entrant Browse...

        row = 1
        for section_idx, (title, keys) in enumerate(_SECTIONS):
            # Section header spanning all columns
            if section_idx > 0:
                grid.addWidget(QLabel(''), row, 0)  # spacer row
                row += 1
            header = QLabel(title)
            header.setFont(QFont(header.font().family(), -1, QFont.Bold))
            header.setStyleSheet('color: #aaaaaa;')
            grid.addWidget(header, row, 0, 1, 4)
            row += 1

            for key in keys:
                label_text = _TYPE_LABELS.get(key, key)
                grid.addWidget(QLabel(label_text), row, 0)

                prefs = current_prefs.get(key, {})

                dock_cb = QCheckBox()
                dock_cb.setChecked(prefs.get('dock', True))
                dock_container = QWidget()
                dock_lay = QHBoxLayout(dock_container)
                dock_lay.setContentsMargins(0, 0, 0, 0)
                dock_lay.addWidget(dock_cb)
                dock_lay.setAlignment(Qt.AlignCenter)
                grid.addWidget(dock_container, row, 1)

                banner_cb = QCheckBox()
                banner_cb.setChecked(prefs.get('banner', False))
                banner_container = QWidget()
                banner_lay = QHBoxLayout(banner_container)
                banner_lay.setContentsMargins(0, 0, 0, 0)
                banner_lay.addWidget(banner_cb)
                banner_lay.setAlignment(Qt.AlignCenter)
                grid.addWidget(banner_container, row, 2)

                # Sound combo + preview button in a horizontal layout
                sound_combo = QComboBox()
                sound_combo.addItems(MACOS_SYSTEM_SOUNDS)
                current_sound = prefs.get('sound', 'None')
                self._set_combo_sound(sound_combo, current_sound)
                sound_combo.setMinimumWidth(110)
                sound_combo.setMaxVisibleItems(20)
                sound_combo.setProperty('always_tooltip', True)
                # Install event filter on dropdown view for right-click removal
                sound_combo.view().viewport().installEventFilter(self)
                sound_combo.view().viewport().setProperty('_sound_key', key)
                sound_combo.currentTextChanged.connect(
                    partial(self._on_sound_changed, key))

                preview_btn = HoverIconButton(_SPEAKER_SVG, 14)
                preview_btn.setFixedSize(22, preview_btn.sizeHint().height())
                preview_btn.setStyleSheet(menu_btn_style())
                preview_btn.setToolTip('Preview sound')
                preview_btn.clicked.connect(partial(self._preview_row_sound, key))

                sound_widget = QWidget()
                sound_layout = QHBoxLayout(sound_widget)
                sound_layout.setContentsMargins(0, 0, 0, 0)
                sound_layout.setSpacing(4)
                sound_layout.setAlignment(Qt.AlignCenter)
                sound_layout.addWidget(sound_combo)
                sound_layout.addWidget(preview_btn)
                grid.addWidget(sound_widget, row, 3)

                self._checks[key] = {'dock': dock_cb, 'banner': banner_cb}
                self._sound_combos[key] = sound_combo
                row += 1

        layout.addLayout(grid)
        layout.addSpacing(4)

        hint = QLabel(
            'Banners require macOS notification permissions.\n'
            'Enable in: System Settings > Notifications > Leap Monitor\n'
            '(or "Python" if running from source)'
        )
        hint.setStyleSheet('color: grey; font-size: 11px;')
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def done(self, result: int) -> None:
        """Save dialog size on close."""
        save_dialog_geometry('notifications', self.width(), self.height())
        super().done(result)

    # ------------------------------------------------------------------
    # Sound helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _set_combo_sound(combo: QComboBox, sound_name: str) -> None:
        """Set the combo to *sound_name*, inserting a custom item if needed."""
        # Check built-in names first
        idx = combo.findText(sound_name)
        if idx >= 0:
            combo.setCurrentIndex(idx)
            combo.setToolTip('')
            return
        if not sound_name or sound_name == _BROWSE_SENTINEL:
            return
        # Check if this path is already in the combo (stored in itemData)
        for i in range(combo.count()):
            if combo.itemData(i) == sound_name:
                combo.setCurrentIndex(i)
                combo.setToolTip(sound_name)
                return
        # Custom file path — insert before "Browse..." with extension stripped
        browse_idx = combo.findText(_BROWSE_SENTINEL)
        display = os.path.splitext(os.path.basename(sound_name))[0]
        combo.insertItem(browse_idx, display, sound_name)
        combo.setCurrentIndex(browse_idx)
        combo.setToolTip(sound_name)

    def eventFilter(self, obj: Any, event: QEvent) -> bool:
        """Intercept right-clicks on combobox dropdown items."""
        if event.type() == QEvent.MouseButtonPress:
            if event.button() == Qt.RightButton:
                key = obj.property('_sound_key')
                if key:
                    combo = self._sound_combos.get(key)
                    if combo:
                        view = combo.view()
                        index = view.indexAt(event.pos())
                        if index.isValid():
                            row_idx = index.row()
                            item_data = combo.itemData(row_idx)
                            if item_data:
                                # Custom sound — close popup and show remove menu
                                item_text = combo.itemText(row_idx)
                                self._browsing = True
                                combo.hidePopup()
                                self._browsing = False
                                menu = QMenu(self)
                                remove_action = QAction(
                                    f'Remove "{item_text}"', self)
                                remove_action.triggered.connect(
                                    partial(self._remove_custom_sound,
                                            key, item_data))
                                menu.addAction(remove_action)
                                menu.exec_(QCursor.pos())
                                return True
        return super().eventFilter(obj, event)

    def _remove_custom_sound(self, key: str, file_path: str) -> None:
        """Remove a custom sound entry by file path; revert to None if selected."""
        combo = self._sound_combos.get(key)
        if not combo:
            return
        # Guard: removing the item before "Browse..." can cause Qt to auto-select
        # "Browse...", which would trigger the file picker. Block that.
        self._browsing = True
        try:
            # Find the item by its stored data (file path) — indices may have shifted
            for i in range(combo.count()):
                if combo.itemData(i) == file_path:
                    was_selected = combo.currentIndex() == i
                    if was_selected:
                        # Switch away first so removeItem doesn't auto-select Browse...
                        combo.setCurrentIndex(combo.findText('None'))
                    combo.removeItem(i)
                    break
        finally:
            self._browsing = False

    def _on_sound_changed(self, key: str, text: str) -> None:
        """Handle combo text change — open file picker when 'Browse...' selected."""
        combo = self._sound_combos.get(key)
        if not combo:
            return
        # Update tooltip: show full path for custom items, clear for built-ins
        data = combo.currentData()
        combo.setToolTip(data if data else '')
        if text != _BROWSE_SENTINEL or self._browsing:
            return
        self._browsing = True
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, 'Select Sound File', '/System/Library/Sounds',
                'Audio Files (*.aiff *.aif *.wav *.mp3 *.m4a *.caf);;All Files (*)',
            )
            if path:
                self._set_combo_sound(combo, path)
            else:
                # User cancelled — revert to None
                combo.setCurrentIndex(combo.findText('None'))
        finally:
            self._browsing = False

    def _get_combo_sound(self, combo: QComboBox) -> str:
        """Return the sound value for the combo (file path for custom items)."""
        data = combo.currentData()
        if data:
            return data  # custom file path stored in item data
        return combo.currentText()

    def _preview_row_sound(self, key: str) -> None:
        """Play the sound currently selected in the combo for *key*."""
        combo = self._sound_combos.get(key)
        if not combo:
            return
        sound_name = self._get_combo_sound(combo)
        if sound_name == 'None' or sound_name == _BROWSE_SENTINEL:
            return
        _play_sound(sound_name)

    def selected_prefs(self) -> dict[str, dict[str, Any]]:
        """Return the updated notification preferences."""
        result: dict[str, dict[str, Any]] = {}
        for key, checks in self._checks.items():
            result[key] = {
                'dock': checks['dock'].isChecked(),
                'banner': checks['banner'].isChecked(),
                'sound': self._get_combo_sound(self._sound_combos[key]),
            }
        return result


def _play_sound(sound_name: str) -> None:
    """Play a macOS system sound by name or file path.

    Args:
        sound_name: 'Default' for system alert, 'None' for silence,
                    a built-in name (e.g. 'Glass'), or an absolute file path.
    """
    if sound_name == 'None':
        return
    try:
        from AppKit import NSSound
        if sound_name == 'Default':
            from AppKit import NSBeep
            NSBeep()
        elif os.path.isabs(sound_name):
            # Custom file path
            from Foundation import NSURL
            url = NSURL.fileURLWithPath_(sound_name)
            sound = NSSound.alloc().initWithContentsOfURL_byReference_(url, True)
            if sound:
                sound.play()
        else:
            sound = NSSound.soundNamed_(sound_name)
            if sound:
                sound.play()
    except Exception:
        pass  # PyObjC not available
