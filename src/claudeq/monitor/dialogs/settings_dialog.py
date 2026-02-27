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

# Map macOS .app display names → git difftool --tool= names
_APP_TO_DIFFTOOL: dict[str, str] = {
    'Visual Studio Code': 'vscode',
    'Visual Studio Code - Insiders': 'vscode',
    'Code': 'vscode',
    'Sublime Merge': 'smerge',
    'Beyond Compare': 'bc',
    'Meld': 'meld',
    'KDiff3': 'kdiff3',
    'DiffMerge': 'diffmerge',
    'FileMerge': 'opendiff',
    'Araxis Merge': 'araxis',
    'DeltaWalker': 'deltawalker',
    'P4Merge': 'p4merge',
    'Helix P4Merge': 'p4merge',
    'ExamDiff Pro': 'examdiff',
    'Code Compare': 'codecompare',
    'ECMerge': 'ecmerge',
    'Guiffy': 'guiffy',
    'TkDiff': 'tkdiff',
}

# JetBrains .app display names → CLI binary name inside Contents/MacOS/
_JETBRAINS_BINARY: dict[str, str] = {
    'IntelliJ IDEA CE': 'idea',
    'IntelliJ IDEA': 'idea',
    'IntelliJ IDEA Ultimate': 'idea',
    'IntelliJ IDEA Community Edition': 'idea',
    'PyCharm': 'pycharm',
    'PyCharm CE': 'pycharm',
    'PyCharm Community Edition': 'pycharm',
    'PyCharm Professional Edition': 'pycharm',
    'GoLand': 'goland',
    'WebStorm': 'webstorm',
    'CLion': 'clion',
    'PhpStorm': 'phpstorm',
    'Rider': 'rider',
    'RubyMine': 'rubymine',
    'DataGrip': 'datagrip',
    'Android Studio': 'studio',
    'Fleet': 'fleet',
}


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
        current_diff_tool: str = '',
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

        # Git diff tool
        diff_label = QLabel('Git diff tool:')
        diff_label.setToolTip(
            'Tool name for git difftool --tool=<name>. Leave blank to use '
            'gitconfig default. Examples: pycharm, vscode, meld, opendiff'
        )
        grid.addWidget(diff_label, 4, 0)
        self._diff_tool_edit = QLineEdit()
        self._diff_tool_edit.setPlaceholderText('(use git default)')
        self._diff_tool_edit.setToolTip(diff_label.toolTip())
        if current_diff_tool:
            self._diff_tool_edit.setText(current_diff_tool)
        grid.addWidget(self._diff_tool_edit, 4, 1)
        diff_browse_btn = QPushButton('Browse...')
        diff_browse_btn.setToolTip('Select a diff application')
        diff_browse_btn.clicked.connect(self._browse_diff_tool)
        grid.addWidget(diff_browse_btn, 4, 2)

        # Show tooltips
        self._tooltips_check = QCheckBox('Show hover explanations')
        self._tooltips_check.setChecked(show_tooltips)
        grid.addWidget(self._tooltips_check, 5, 0, 1, 2)

        # Notifications
        notif_btn = QPushButton('Notifications...')
        notif_btn.setToolTip('Configure dock badge and banner notifications per event type')
        notif_btn.clicked.connect(self._open_notifications)
        grid.addWidget(notif_btn, 6, 0)

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

    def _browse_diff_tool(self) -> None:
        """Open a file picker for a diff application.

        Resolution order:
        1. Known difftool name (VS Code → 'vscode', etc.)
        2. JetBrains IDE → store full CLI binary path (used via --extcmd)
        3. Unknown → show help with available tool names
        """
        home_apps = os.path.expanduser('~/Applications')
        start_dir = home_apps if os.path.isdir(home_apps) else '/Applications'
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select Diff Application', start_dir,
            'Applications (*.app);;All Files (*)',
        )
        if not path:
            return
        app_name = path.rsplit('/', 1)[-1]
        if app_name.endswith('.app'):
            app_name = app_name[:-4]

        # 1. Known git difftool name
        tool_name = _APP_TO_DIFFTOOL.get(app_name)
        if tool_name:
            self._diff_tool_edit.setText(tool_name)
            return

        # 2. JetBrains IDE — resolve CLI binary inside the .app bundle
        binary_name = _JETBRAINS_BINARY.get(app_name)
        if binary_name:
            binary_path = Path(path) / 'Contents' / 'MacOS' / binary_name
            if binary_path.is_file():
                self._diff_tool_edit.setText(str(binary_path))
                return
            # Binary not found at expected path — warn
            QMessageBox.warning(
                self, 'Diff Tool',
                f'Could not find CLI binary at:\n{binary_path}\n\n'
                f'Is {app_name} installed correctly?',
            )
            return

        # 3. Unknown app — show available tools
        available = self._get_available_difftools()
        hint = (
            f'"{app_name}" is not a recognised git difftool name.\n\n'
            'git difftool uses short identifiers. '
        )
        if available:
            hint += 'Available tools on this system:\n\n' + '\n'.join(
                f'  \u2022 {t}' for t in available
            )
        else:
            hint += 'Examples: vscode, opendiff, meld, bc, kdiff3'
        hint += (
            '\n\nIf you have a tool configured in ~/.gitconfig '
            '(e.g. [difftool "custom"]), leave this field blank '
            'to use your gitconfig default.'
        )
        QMessageBox.information(self, 'Diff Tool', hint)

    @staticmethod
    def _get_available_difftools() -> list[str]:
        """Return list of difftool names available on this system.

        Parses ``git difftool --tool-help`` output.  Built-in tools appear
        as ``<name>  Use ...`` while user-defined tools appear as
        ``<name>.cmd <command>`` — we extract the name before ``.cmd``.
        """
        import subprocess
        try:
            result = subprocess.run(
                ['git', 'difftool', '--tool-help'],
                capture_output=True, text=True, timeout=5,
            )
            seen: set[str] = set()
            tools: list[str] = []
            for line in result.stdout.splitlines():
                if not line.startswith('\t\t'):
                    continue
                token = line.strip().split()[0]
                if not token:
                    continue
                # User-defined: "custom.cmd ..." → extract "custom"
                if '.cmd' in token:
                    name = token.split('.cmd')[0]
                elif token.islower():
                    name = token
                else:
                    continue
                if name and name not in seen:
                    seen.add(name)
                    tools.append(name)
            return tools
        except Exception:
            return []

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

    def selected_diff_tool(self) -> str:
        """Return the configured git diff tool name (empty = use git default)."""
        return self._diff_tool_edit.text().strip()
