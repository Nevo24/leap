"""Notifications configuration dialog for ClaudeQ Monitor."""

from typing import Optional

from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QLabel, QVBoxLayout,
)

from claudeq.monitor.mr_tracking.config import load_dialog_geometry, save_dialog_geometry


# Display labels for each notification type
_TYPE_LABELS = {
    'mr_unresponded': 'New unresponded threads',
    'mr_all_responded': 'All threads responded',
    'mr_approved': 'MR approved',
    'session_completed': 'Claude finished processing',
    'session_needs_permission': 'Claude needs permission',
    'session_has_question': 'Claude has a question',
    'session_interrupted': 'Claude was interrupted',
    'review_requested': 'Review requested',
    'assigned': 'Assigned to you',
    'mentioned': 'Mentioned',
}

# Grouped type keys with section titles.
_SECTIONS: list[tuple[str, list[str]]] = [
    ('MR / Session Tracking', [
        'mr_unresponded', 'mr_all_responded', 'mr_approved', 'session_completed',
        'session_needs_permission', 'session_has_question', 'session_interrupted',
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
        current_prefs: dict[str, dict[str, bool]],
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle('Notifications')
        self.resize(400, 260)
        saved = load_dialog_geometry('notifications')
        if saved:
            self.resize(saved[0], saved[1])

        layout = QVBoxLayout(self)

        grid = QGridLayout()
        grid.addWidget(QLabel(''), 0, 0)
        grid.addWidget(QLabel('Dock Badge'), 0, 1)
        grid.addWidget(QLabel('Banner'), 0, 2)

        self._checks: dict[str, dict[str, QCheckBox]] = {}

        row = 1
        for section_idx, (title, keys) in enumerate(_SECTIONS):
            # Section header spanning all columns
            if section_idx > 0:
                grid.addWidget(QLabel(''), row, 0)  # spacer row
                row += 1
            header = QLabel(title)
            header.setFont(QFont(header.font().family(), -1, QFont.Bold))
            header.setStyleSheet('color: #aaaaaa;')
            grid.addWidget(header, row, 0, 1, 3)
            row += 1

            for key in keys:
                label_text = _TYPE_LABELS.get(key, key)
                grid.addWidget(QLabel(label_text), row, 0)

                prefs = current_prefs.get(key, {})

                dock_cb = QCheckBox()
                dock_cb.setChecked(prefs.get('dock', True))
                grid.addWidget(dock_cb, row, 1)

                banner_cb = QCheckBox()
                banner_cb.setChecked(prefs.get('banner', False))
                grid.addWidget(banner_cb, row, 2)

                self._checks[key] = {'dock': dock_cb, 'banner': banner_cb}
                row += 1

        layout.addLayout(grid)
        layout.addSpacing(8)

        hint = QLabel(
            'Banners require macOS notification permissions.\n'
            'Enable in: System Settings > Notifications > ClaudeQ Monitor\n'
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

    def selected_prefs(self) -> dict[str, dict[str, bool]]:
        """Return the updated notification preferences."""
        result: dict[str, dict[str, bool]] = {}
        for key, checks in self._checks.items():
            result[key] = {
                'dock': checks['dock'].isChecked(),
                'banner': checks['banner'].isChecked(),
            }
        return result
