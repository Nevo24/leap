"""Settings dialog for ClaudeQ Monitor."""

import os
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QGridLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout,
)

from claudeq.monitor.dialogs.notifications_dialog import NotificationsDialog

DEFAULT_REPOS_DIR = '/tmp/claudeq-repos'


def _detect_installed_terminals() -> list[str]:
    """Return list of terminal apps installed on this machine."""
    home = Path.home()
    candidates = [
        ('Terminal.app', [Path('/System/Applications/Utilities/Terminal.app')]),
        ('iTerm2', [Path('/Applications/iTerm.app'), home / 'Applications' / 'iTerm.app']),
        ('Warp', [Path('/Applications/Warp.app'), home / 'Applications' / 'Warp.app']),
    ]
    return [name for name, paths in candidates if any(p.is_dir() for p in paths)]


class SettingsDialog(QDialog):
    """Dialog for configuring monitor preferences."""

    def __init__(
        self,
        current_terminal: Optional[str] = None,
        current_repos_dir: Optional[str] = None,
        active_paths_fn: Optional[Callable[[], set[str]]] = None,
        log_fn: Optional[Callable[[str], None]] = None,
        show_tooltips: bool = True,
        notification_prefs: Optional[dict[str, dict[str, bool]]] = None,
        current_auto_send_mode: str = 'pause',
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self._active_paths_fn = active_paths_fn
        self._log_fn = log_fn
        self._notification_prefs: dict[str, dict[str, bool]] = notification_prefs or {}
        self.setWindowTitle('Settings')
        self.resize(800, 220)

        layout = QVBoxLayout(self)

        grid = QGridLayout()

        # Default terminal
        grid.addWidget(QLabel('Default terminal:'), 0, 0)
        self._terminal_combo = QComboBox()
        self._installed_terminals = _detect_installed_terminals()
        self._terminal_combo.addItems(self._installed_terminals)
        if current_terminal and current_terminal in self._installed_terminals:
            self._terminal_combo.setCurrentText(current_terminal)
        grid.addWidget(self._terminal_combo, 0, 1)

        # Warp accessibility hint (shown only when Warp is selected)
        self._warp_hint = QLabel(
            'Warp "jump to" requires Accessibility permission.\n'
            'Grant in: System Settings > Privacy & Security > Accessibility\n'
            '> enable "ClaudeQ Monitor" (or your IDE/terminal if running from source)'
        )
        self._warp_hint.setStyleSheet('color: grey; font-size: 11px;')
        self._warp_hint.setWordWrap(True)
        self._warp_hint.setVisible(self._terminal_combo.currentText() == 'Warp')
        grid.addWidget(self._warp_hint, 1, 0, 1, 4)
        self._terminal_combo.currentTextChanged.connect(
            lambda text: self._warp_hint.setVisible(text == 'Warp'))

        # Repositories directory
        grid.addWidget(QLabel('Clone to dir:'), 2, 0)
        self._repos_dir_edit = QLineEdit()
        self._repos_dir_edit.setPlaceholderText(DEFAULT_REPOS_DIR)
        if current_repos_dir:
            self._repos_dir_edit.setText(current_repos_dir)
        grid.addWidget(self._repos_dir_edit, 2, 1)
        browse_btn = QPushButton('Browse...')
        browse_btn.clicked.connect(self._browse_repos_dir)
        grid.addWidget(browse_btn, 2, 2)
        cleanup_btn = QPushButton('Clean')
        cleanup_btn.setToolTip('Delete cloned repos that have no running CQ server')
        cleanup_btn.clicked.connect(self._cleanup_repos)
        grid.addWidget(cleanup_btn, 2, 3)

        # Default auto-send mode
        grid.addWidget(QLabel('Default auto-send:'), 3, 0)
        self._auto_send_combo = QComboBox()
        self._auto_send_combo.addItems(['Pause on input', 'Always send'])
        if current_auto_send_mode == 'always':
            self._auto_send_combo.setCurrentIndex(1)
        grid.addWidget(self._auto_send_combo, 3, 1)

        # Show tooltips
        self._tooltips_check = QCheckBox('Show hover explanations')
        self._tooltips_check.setChecked(show_tooltips)
        grid.addWidget(self._tooltips_check, 4, 0, 1, 2)

        # Notifications
        notif_btn = QPushButton('Notifications...')
        notif_btn.setToolTip('Configure dock badge and banner notifications per event type')
        notif_btn.clicked.connect(self._open_notifications)
        grid.addWidget(notif_btn, 5, 0)

        layout.addLayout(grid)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_repos_dir(self) -> None:
        """Open a directory picker for repositories dir."""
        path = QFileDialog.getExistingDirectory(self, 'Select Repositories Directory')
        if path:
            self._repos_dir_edit.setText(path)

    def _cleanup_repos(self) -> None:
        """Delete all repos in the repos dir that are not used by a running CQ server."""
        repos_dir_str = self._repos_dir_edit.text().strip() or DEFAULT_REPOS_DIR
        repos_dir = Path(repos_dir_str).expanduser()
        if not repos_dir.is_dir():
            QMessageBox.information(self, 'Nothing to Clean', f"'{repos_dir}' does not exist.")
            return

        active_paths: set[str] = set()
        if self._active_paths_fn:
            active_paths = self._active_paths_fn()

        # Find subdirectories that are git repos
        unused: list[Path] = []
        for child in sorted(repos_dir.iterdir()):
            if not child.is_dir():
                continue
            if not (child / '.git').exists():
                continue
            resolved = str(child.resolve())
            if resolved not in active_paths:
                unused.append(child)

        if not unused:
            QMessageBox.information(self, 'Nothing to Clean', 'No unused repos found.')
            return

        names = '\n'.join(f'  - {d.name}' for d in unused)
        reply = QMessageBox.question(
            self, 'Clean Unused Repos',
            f"Delete {len(unused)} unused repo(s)?\n\n{names}",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        deleted: list[str] = []
        errors: list[str] = []
        for d in unused:
            try:
                shutil.rmtree(d)
                deleted.append(d.name)
            except Exception as e:
                errors.append(f"{d.name}: {e}")

        if errors:
            QMessageBox.warning(
                self, 'Cleanup Errors',
                f"Some repos could not be deleted:\n\n" + '\n'.join(errors),
            )
            if self._log_fn:
                self._log_fn(f"Repo cleanup: {len(deleted)} deleted, {len(errors)} failed")
        else:
            QMessageBox.information(
                self, 'Cleanup Complete',
                f"Deleted {len(deleted)} unused repo(s).",
            )
            if self._log_fn:
                self._log_fn(f"Repo cleanup: deleted {len(deleted)} unused repo(s): {', '.join(deleted)}")

    def _open_notifications(self) -> None:
        """Open the notifications configuration dialog."""
        dialog = NotificationsDialog(self._notification_prefs, parent=self)
        if dialog.exec_():
            self._notification_prefs = dialog.selected_prefs()

    def notification_prefs(self) -> dict[str, dict[str, bool]]:
        """Return the current notification preferences."""
        return self._notification_prefs

    def selected_terminal(self) -> str:
        """Return the selected default terminal."""
        return self._terminal_combo.currentText()

    def selected_repos_dir(self) -> str:
        """Return the repositories directory path."""
        return self._repos_dir_edit.text().strip()

    def show_tooltips(self) -> bool:
        """Return whether hover explanations are enabled."""
        return self._tooltips_check.isChecked()

    def selected_auto_send_mode(self) -> str:
        """Return the selected default auto-send mode."""
        return 'always' if self._auto_send_combo.currentIndex() == 1 else 'pause'
