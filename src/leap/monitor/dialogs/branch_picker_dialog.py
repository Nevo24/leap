"""Dialog for picking a git branch to compare against."""

import logging
import subprocess

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox, QCompleter, QDialog, QDialogButtonBox, QHBoxLayout,
    QLabel, QRadioButton, QVBoxLayout,
)

from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.pr_tracking.config import load_dialog_geometry, save_dialog_geometry
from leap.monitor.pr_tracking.git_utils import detect_default_branch

logger = logging.getLogger(__name__)


class BranchPickerDialog(ZoomMixin, QDialog):
    """Branch picker with Remote/Local toggle and type-to-filter combobox."""

    _DEFAULT_SIZE = (400, 130)

    def __init__(self, project_path: str, parent: object = None) -> None:
        super().__init__(parent)
        self._project_path = project_path
        self.setWindowTitle('Compare to branch')
        self.resize(*self._DEFAULT_SIZE)
        saved = load_dialog_geometry('branch_picker')
        if saved:
            self.resize(saved[0], saved[1])

        layout = QVBoxLayout()
        self.setLayout(layout)

        # Remote / Local toggle
        toggle_layout = QHBoxLayout()
        self._remote_radio = QRadioButton('Remote')
        self._local_radio = QRadioButton('Local')
        self._remote_radio.setChecked(True)
        self._remote_radio.toggled.connect(self._on_toggle)
        toggle_layout.addWidget(self._remote_radio)
        toggle_layout.addWidget(self._local_radio)
        toggle_layout.addStretch()
        layout.addLayout(toggle_layout)

        # Branch combobox
        branch_layout = QHBoxLayout()
        branch_layout.addWidget(QLabel('Branch:'))
        self._combo = QComboBox()
        self._combo.setEditable(True)
        branch_layout.addWidget(self._combo, 1)
        layout.addLayout(branch_layout)

        layout.addStretch()

        # OK / Cancel
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        # Populate initial branches
        self._default_branch = detect_default_branch(project_path)
        self._populate(remote=True)
        self._init_zoom('branch_picker_font_size')

    def _on_toggle(self, checked: bool) -> None:
        """Handle Remote/Local radio toggle, preserving current selection."""
        previous = self._combo.currentText().strip()
        if self._remote_radio.isChecked():
            self._populate(remote=True, preferred=previous)
        else:
            self._populate(remote=False, preferred=previous)

    def _populate(self, remote: bool, preferred: str = '') -> None:
        """Populate combobox and try to keep *preferred* selected."""
        branches = self._fetch_branches(remote=remote)
        self._set_branches(branches)
        if preferred:
            idx = self._combo.findText(preferred)
            if idx >= 0:
                self._combo.setCurrentIndex(idx)
                return
        # Fall back to default branch for remote, or first item
        if remote:
            idx = self._combo.findText(self._default_branch)
            if idx >= 0:
                self._combo.setCurrentIndex(idx)

    def _set_branches(self, branches: list[str]) -> None:
        """Set combobox items and attach a case-insensitive completer."""
        self._combo.clear()
        self._combo.addItems(branches)
        completer = QCompleter(branches, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self._combo.setCompleter(completer)

    def _fetch_branches(self, remote: bool) -> list[str]:
        """Fetch branch names via git."""
        cmd = ['git', 'branch', '-r', '--no-color'] if remote else ['git', 'branch', '--no-color']
        try:
            result = subprocess.run(
                cmd,
                cwd=self._project_path,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=5,
            )
            if result.returncode != 0:
                return []
            branches: list[str] = []
            for line in result.stdout.splitlines():
                name = line.strip()
                if not name or ' -> ' in name:
                    continue
                # Strip leading '* ' for current local branch
                if name.startswith('* '):
                    name = name[2:]
                if remote and name.startswith('origin/'):
                    name = name[len('origin/'):]
                branches.append(name)
            return sorted(set(branches))
        except Exception:
            logger.debug("Failed to fetch branches for %s", self._project_path, exc_info=True)
            return []

    def selected_branch(self) -> str:
        """Return the full ref for the selected branch."""
        name = self._combo.currentText().strip()
        if self._remote_radio.isChecked():
            return f'origin/{name}'
        return name

    def done(self, result: int) -> None:
        """Save dialog width on close."""
        save_dialog_geometry('branch_picker', self.width(), self.height())
        super().done(result)
