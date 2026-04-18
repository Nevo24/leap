"""Settings dialog for Leap Monitor."""

import glob
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)
from PyQt5.QtGui import QKeySequence

from leap.monitor.dialogs.notifications_dialog import NotificationsDialog
from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.cli_providers.states import AutoSendMode
from leap.monitor.pr_tracking.config import load_dialog_geometry, save_dialog_geometry
from leap.monitor.themes import THEMES, current_theme

DEFAULT_REPOS_DIR = os.path.expanduser('~/leap-repos')

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


# Apps with Electron-style --diff flag (resolved to full CLI binary path).
# Maps .app display name → (relative binary path inside .app, diff flag).
_ELECTRON_DIFF_APPS: dict[str, tuple[str, str]] = {
    'Cursor': ('Contents/Resources/app/bin/cursor', '--diff'),
}


def _detect_installed_terminals() -> list[str]:
    """Return list of terminal apps installed on this machine."""
    home = Path.home()
    candidates = [
        ('Terminal.app', [Path('/System/Applications/Utilities/Terminal.app')]),
        ('iTerm2', [Path('/Applications/iTerm.app'), home / 'Applications' / 'iTerm.app']),
        ('Warp', [Path('/Applications/Warp.app'), home / 'Applications' / 'Warp.app']),
        ('WezTerm', [Path('/Applications/WezTerm.app'), home / 'Applications' / 'WezTerm.app']),
    ]
    found = [name for name, paths in candidates if any(p.is_dir() for p in paths)]
    # WezTerm may be installed outside standard locations — use Spotlight
    if 'WezTerm' not in found:
        try:
            result = subprocess.run(
                ['mdfind', 'kMDItemCFBundleIdentifier == "com.github.wez.wezterm"'],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                found.append('WezTerm')
        except (subprocess.SubprocessError, OSError):
            pass
    return found


# JetBrains IDEs to search, in priority order (PyCharm first).
# Each entry: (glob pattern, CLI binary name inside Contents/MacOS/).
_DIFFTOOL_SEARCH_ORDER: list[tuple[str, str]] = [
    ('PyCharm*.app', 'pycharm'),
    ('IntelliJ*.app', 'idea'),
    ('GoLand*.app', 'goland'),
    ('WebStorm*.app', 'webstorm'),
    ('PhpStorm*.app', 'phpstorm'),
    ('CLion*.app', 'clion'),
    ('RubyMine*.app', 'rubymine'),
    ('DataGrip*.app', 'datagrip'),
    ('Rider*.app', 'rider'),
]

_DIFFTOOL_APP_DIRS: list[str] = [
    '/Applications',
    os.path.expanduser('~/Applications'),
]


def detect_default_difftool() -> str:
    """Auto-detect a JetBrains diff tool on first launch.

    Returns the full CLI binary path (e.g. /Applications/PyCharm.app/Contents/MacOS/pycharm)
    or '' if the user already has a git difftool configured or no JetBrains IDE is found.
    """
    # If the user already configured a difftool in git, respect their choice.
    try:
        result = subprocess.run(
            ['git', 'config', 'diff.tool'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return ''
    except Exception:
        pass

    # Search for JetBrains IDEs in priority order.
    for app_pattern, binary_name in _DIFFTOOL_SEARCH_ORDER:
        for app_dir in _DIFFTOOL_APP_DIRS:
            for app_path in sorted(glob.glob(f'{app_dir}/{app_pattern}')):
                binary = Path(app_path) / 'Contents' / 'MacOS' / binary_name
                if binary.is_file():
                    return str(binary)

    return ''


class SettingsDialog(ZoomMixin, QDialog):
    """Dialog for configuring monitor preferences."""

    _DEFAULT_SIZE = (800, 440)

    def __init__(
        self,
        current_terminal: Optional[str] = None,
        current_repos_dir: Optional[str] = None,
        active_paths_fn: Optional[Callable[[], set[str]]] = None,
        log_fn: Optional[Callable[[str], None]] = None,
        show_tooltips: bool = True,
        notification_prefs: Optional[dict[str, dict[str, Any]]] = None,
        current_auto_send_mode: str = AutoSendMode.PAUSE,
        current_diff_tool: str = '',
        new_status_seconds: int = 60,
        current_global_shortcut: str = '',
        current_notes_shortcut_focused: str = '',
        current_notes_shortcut_global: str = '',
        current_theme_name: str = 'Nord',
        on_theme_change: Optional[Callable[[str], None]] = None,
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)
        self._active_paths_fn = active_paths_fn
        self._log_fn = log_fn
        self._notification_prefs: dict[str, dict[str, Any]] = notification_prefs or {}
        self._on_theme_change = on_theme_change
        self._original_theme = current_theme_name
        self.setWindowTitle('Settings')
        self.resize(*self._DEFAULT_SIZE)
        saved = load_dialog_geometry('settings')
        if saved:
            self.resize(saved[0], saved[1])

        layout = QVBoxLayout(self)

        grid = QGridLayout()

        # Theme selector
        theme_label = QLabel('Theme:')
        theme_label.setToolTip('Visual color scheme for the monitor window')
        grid.addWidget(theme_label, 0, 0)
        self._theme_combo = QComboBox()
        self._theme_combo.setToolTip(theme_label.toolTip())
        self._theme_combo.addItems(list(THEMES.keys()))
        if current_theme_name in THEMES:
            self._theme_combo.setCurrentText(current_theme_name)
        self._theme_combo.currentTextChanged.connect(self._on_theme_combo_changed)
        grid.addWidget(self._theme_combo, 0, 1)

        # Default terminal
        terminal_label = QLabel('Default terminal:')
        terminal_label.setToolTip(
            'Terminal app used when opening a new session from the monitor')
        grid.addWidget(terminal_label, 1, 0)
        self._terminal_combo = QComboBox()
        self._terminal_combo.setToolTip(terminal_label.toolTip())
        self._installed_terminals = _detect_installed_terminals()
        self._terminal_combo.addItems(self._installed_terminals)
        if current_terminal and current_terminal in self._installed_terminals:
            self._terminal_combo.setCurrentText(current_terminal)
        grid.addWidget(self._terminal_combo, 1, 1)

        # Warp accessibility hint (shown only when Warp is selected)
        self._warp_hint = QLabel(
            'Warp "jump to" requires Accessibility permission.\n'
            'Grant in: System Settings > Privacy & Security > Accessibility\n'
            '> enable "Leap Monitor" (or your IDE/terminal if running from source)'
        )
        self._warp_hint.setStyleSheet(f'color: {current_theme().text_muted};')
        self._warp_hint.setWordWrap(True)
        self._warp_hint.setVisible(self._terminal_combo.currentText() == 'Warp')
        grid.addWidget(self._warp_hint, 2, 0, 1, 4)
        self._terminal_combo.currentTextChanged.connect(
            lambda text: self._warp_hint.setVisible(text == 'Warp'))

        # Repositories directory
        repos_label = QLabel('Clone to dir:')
        repos_label.setToolTip(
            'Directory where PR repos are cloned when adding sessions from Git URLs')
        grid.addWidget(repos_label, 3, 0)
        self._repos_dir_edit = QLineEdit()
        self._repos_dir_edit.setToolTip(repos_label.toolTip())
        self._repos_dir_edit.setPlaceholderText(DEFAULT_REPOS_DIR)
        if current_repos_dir:
            self._repos_dir_edit.setText(current_repos_dir)
        grid.addWidget(self._repos_dir_edit, 3, 1)
        browse_btn = QPushButton('Browse...')
        browse_btn.clicked.connect(self._browse_repos_dir)
        grid.addWidget(browse_btn, 3, 2)
        cleanup_btn = QPushButton('Clean')
        cleanup_btn.setToolTip('Delete cloned repos that have no running Leap server')
        cleanup_btn.clicked.connect(self._cleanup_repos)
        grid.addWidget(cleanup_btn, 3, 3)

        # Default auto-send mode
        auto_send_label = QLabel('Default auto-send:')
        auto_send_label.setToolTip(
            'Default auto-send mode for new sessions.\n'
            '\n'
            'Pause on input:\n'
            '  Sends queued messages only when the CLI is idle.\n'
            '  Waits during running, permission, and question states.\n'
            '\n'
            'Always send:\n'
            '  Auto-approves permission prompts ("Yes") and sends\n'
            '  queued messages when idle. Waits during questions.')
        grid.addWidget(auto_send_label, 4, 0)
        self._auto_send_combo = QComboBox()
        self._auto_send_combo.setToolTip(auto_send_label.toolTip())
        self._auto_send_combo.addItems(['Pause on input', 'Always send'])
        if current_auto_send_mode == AutoSendMode.ALWAYS:
            self._auto_send_combo.setCurrentIndex(1)
        grid.addWidget(self._auto_send_combo, 4, 1)

        # Git diff tool
        diff_label = QLabel('Git diff tool:')
        diff_label.setToolTip(
            'Tool name for git difftool --tool=<name>. Leave blank to use '
            'gitconfig default. Examples: pycharm, vscode, meld, opendiff'
        )
        grid.addWidget(diff_label, 5, 0)
        self._diff_tool_edit = QLineEdit()
        self._diff_tool_edit.setPlaceholderText('(use git default)')
        self._diff_tool_edit.setToolTip(diff_label.toolTip())
        if current_diff_tool:
            self._diff_tool_edit.setText(current_diff_tool)
        grid.addWidget(self._diff_tool_edit, 5, 1)
        diff_browse_btn = QPushButton('Browse...')
        diff_browse_btn.setToolTip('Select a diff application')
        diff_browse_btn.clicked.connect(self._browse_diff_tool)
        grid.addWidget(diff_browse_btn, 5, 2)

        # Show tooltips
        self._tooltips_check = QCheckBox('Show hover explanations')
        # This tooltip is deliberately NOT toggled off by _apply_tooltips —
        # otherwise, after disabling, the user would have no hover hint
        # telling them how to re-enable them.
        self._tooltips_check.setToolTip(
            'Show hover tooltips on buttons and cells throughout the '
            'monitor. Uncheck for a quieter UI.')
        self._original_tooltips = show_tooltips
        self._tooltips_check.setChecked(show_tooltips)
        self._tooltips_check.toggled.connect(self._apply_tooltips)
        grid.addWidget(self._tooltips_check, 6, 0, 1, 2)

        # New change indicator duration
        new_status_label = QLabel('New change indicator (\U0001f525):')
        new_status_label.setToolTip(
            'Show a fire icon in the Status and PR columns\n'
            'when the value recently changed.\n'
            'Set to 0 to disable.'
        )
        grid.addWidget(new_status_label, 7, 0)
        new_status_layout = QHBoxLayout()
        self._new_status_spin = QSpinBox()
        self._new_status_spin.setRange(0, 999)
        self._new_status_spin.setSpecialValueText('Disabled')
        self._new_status_spin.setValue(new_status_seconds)
        self._new_status_spin.setToolTip(new_status_label.toolTip())
        self._new_status_spin.setFixedWidth(80)
        new_status_layout.addWidget(self._new_status_spin)
        new_status_layout.addWidget(QLabel('seconds'))
        new_status_layout.addStretch()
        grid.addLayout(new_status_layout, 7, 1)

        # Notifications
        notif_btn = QPushButton('Notifications')
        notif_btn.setToolTip('Configure dock badge and banner notifications per event type')
        notif_btn.clicked.connect(self._open_notifications)
        grid.addWidget(notif_btn, 8, 0)

        # Global focus shortcut
        shortcut_label = QLabel('Global focus shortcut:')
        shortcut_label.setToolTip(
            'System-wide keyboard shortcut to bring the monitor to the foreground'
        )
        grid.addWidget(shortcut_label, 9, 0)
        self._shortcut_edit = _ShortcutEdit()
        self._shortcut_edit.setToolTip(shortcut_label.toolTip())
        if current_global_shortcut:
            self._shortcut_edit.setKeySequence(QKeySequence(current_global_shortcut))
        grid.addWidget(self._shortcut_edit, 9, 1)
        clear_shortcut_btn = QPushButton('Clear')
        clear_shortcut_btn.clicked.connect(self._shortcut_edit.clear)
        grid.addWidget(clear_shortcut_btn, 9, 2)

        # Notes shortcut (when Leap is focused)
        notes_focused_label = QLabel('Notes shortcut (focused):')
        notes_focused_label.setToolTip(
            'Keyboard shortcut to open/close Notes when the Leap window is active')
        grid.addWidget(notes_focused_label, 10, 0)
        self._notes_focused_edit = _ShortcutEdit()
        self._notes_focused_edit.setToolTip(notes_focused_label.toolTip())
        if current_notes_shortcut_focused:
            self._notes_focused_edit.setKeySequence(
                QKeySequence(current_notes_shortcut_focused))
        grid.addWidget(self._notes_focused_edit, 10, 1)
        clear_notes_focused = QPushButton('Clear')
        clear_notes_focused.clicked.connect(self._notes_focused_edit.clear)
        grid.addWidget(clear_notes_focused, 10, 2)

        # Notes shortcut (global — any app)
        notes_global_label = QLabel('Notes shortcut (global):')
        notes_global_label.setToolTip(
            'System-wide shortcut to open/close Notes from any app '
            '(brings Leap to the foreground)')
        grid.addWidget(notes_global_label, 11, 0)
        self._notes_global_edit = _ShortcutEdit()
        self._notes_global_edit.setToolTip(notes_global_label.toolTip())
        if current_notes_shortcut_global:
            self._notes_global_edit.setKeySequence(
                QKeySequence(current_notes_shortcut_global))
        grid.addWidget(self._notes_global_edit, 11, 1)
        clear_notes_global = QPushButton('Clear')
        clear_notes_global.clicked.connect(self._notes_global_edit.clear)
        grid.addWidget(clear_notes_global, 11, 2)

        # Accessibility permission hint (always visible)
        shortcut_hint = QLabel(
            'Global shortcuts require Accessibility permission.\n'
            'Grant in: System Settings > Privacy & Security > Accessibility\n'
            '> enable "Leap Monitor" (or "Python" if running from source)')
        shortcut_hint.setStyleSheet(
            f'color: {current_theme().text_muted};')
        shortcut_hint.setWordWrap(True)
        grid.addWidget(shortcut_hint, 12, 0, 1, 4)

        layout.addLayout(grid)
        layout.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Collect widgets that have tooltips for the toggle
        self._tooltip_widgets: list[tuple[QWidget, str]] = []
        for w in self.findChildren(QWidget):
            tip = w.toolTip()
            if tip and w is not self._tooltips_check:
                self._tooltip_widgets.append((w, tip))
        self._apply_tooltips(show_tooltips)
        self._init_zoom('settings_font_size')

    def _apply_tooltips(self, enabled: bool) -> None:
        """Show or hide tooltips on all settings widgets.

        Also updates the app-level tooltip flag so the TooltipApp event
        filter doesn't suppress ToolTip events while the dialog is open.
        """
        for widget, tip in self._tooltip_widgets:
            widget.setToolTip(tip if enabled else '')
        # Update the app-level flag for immediate effect
        app = QApplication.instance()
        if hasattr(app, 'tooltips_enabled'):
            app.tooltips_enabled = enabled

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

        # 2. Electron-style app (Cursor) — resolve CLI binary with --diff flag
        electron_info = _ELECTRON_DIFF_APPS.get(app_name)
        if electron_info:
            rel_binary, _ = electron_info
            binary_path = Path(path) / rel_binary
            if binary_path.is_file():
                self._diff_tool_edit.setText(str(binary_path))
                return
            QMessageBox.warning(
                self, 'Diff Tool',
                f'Could not find CLI binary at:\n{binary_path}\n\n'
                f'Is {app_name} installed correctly?',
            )
            return

        # 3. JetBrains IDE — resolve CLI binary inside the .app bundle
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
        """Delete all repos in the repos dir that are not used by a running Leap server."""
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

    def notification_prefs(self) -> dict[str, dict[str, Any]]:
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
        return AutoSendMode.ALWAYS if self._auto_send_combo.currentIndex() == 1 else AutoSendMode.PAUSE

    def new_status_seconds(self) -> int:
        """Return the new-status fire indicator duration in seconds."""
        return self._new_status_spin.value()

    def selected_theme(self) -> str:
        """Return the selected theme name."""
        return self._theme_combo.currentText()

    def _on_theme_combo_changed(self, theme_name: str) -> None:
        """Live-preview theme change."""
        if self._on_theme_change:
            self._on_theme_change(theme_name)

    def done(self, result: int) -> None:
        """Save dialog size on close. Revert theme and tooltips on cancel."""
        if result != QDialog.Accepted:
            if self._on_theme_change:
                self._on_theme_change(self._original_theme)
            # Revert app-level tooltip flag to saved state
            app = QApplication.instance()
            if hasattr(app, 'tooltips_enabled'):
                app.tooltips_enabled = self._original_tooltips
        save_dialog_geometry('settings', self.width(), self.height())
        super().done(result)

    def selected_diff_tool(self) -> str:
        """Return the configured git diff tool name (empty = use git default)."""
        return self._diff_tool_edit.text().strip()

    def selected_global_shortcut(self) -> str:
        """Return the global focus shortcut as a portable string (e.g. 'Ctrl+Shift+M')."""
        seq = self._shortcut_edit.keySequence()
        return seq.toString() if not seq.isEmpty() else ''

    def selected_notes_shortcut_focused(self) -> str:
        """Return the notes shortcut (when focused) as a portable string."""
        seq = self._notes_focused_edit.keySequence()
        return seq.toString() if not seq.isEmpty() else ''

    def selected_notes_shortcut_global(self) -> str:
        """Return the notes shortcut (global) as a portable string."""
        seq = self._notes_global_edit.keySequence()
        return seq.toString() if not seq.isEmpty() else ''


class _ShortcutEdit(QLineEdit):
    """Single-shortcut capture field.

    QKeySequenceEdit allows multi-chord sequences and has a
    timeout-based "finish" that feels sluggish. This simple
    QLineEdit captures one key combo on press and is done.
    """

    @staticmethod
    def _style_normal() -> str:
        t = current_theme()
        return (
            f'QLineEdit {{ border: 1px solid {t.input_border};'
            f' border-radius: {t.border_radius}px; padding: 5px 8px;'
            f' background-color: {t.input_bg};'
            f' color: {t.text_primary}; }}'
        )

    @staticmethod
    def _style_focused() -> str:
        t = current_theme()
        return (
            f'QLineEdit {{ border: 2px solid {t.input_focus_border};'
            f' border-radius: {t.border_radius}px; padding: 4px 7px;'
            f' background-color: {t.input_bg};'
            f' color: {t.text_primary}; }}'
        )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._seq = QKeySequence()
        self.setReadOnly(True)
        self.setPlaceholderText('Click to set shortcut')
        self.setStyleSheet(self._style_normal())

    def keySequence(self) -> QKeySequence:
        return self._seq

    def setKeySequence(self, seq: QKeySequence) -> None:
        self._seq = seq
        self.setText(seq.toString(QKeySequence.NativeText))

    def clear(self) -> None:
        self._seq = QKeySequence()
        self.setText('')

    # -- Qt overrides --------------------------------------------------

    def focusInEvent(self, event: Any) -> None:
        super().focusInEvent(event)
        self.setStyleSheet(self._style_focused())
        if not self._seq.isEmpty():
            self.setPlaceholderText('Press new shortcut…')
        else:
            self.setPlaceholderText('Press shortcut…')

    def focusOutEvent(self, event: Any) -> None:
        super().focusOutEvent(event)
        self.setStyleSheet(self._style_normal())
        self.setPlaceholderText('Click to set shortcut')

    def keyPressEvent(self, event: Any) -> None:
        key = event.key()
        if key in (Qt.Key_unknown, Qt.Key_Control, Qt.Key_Shift,
                   Qt.Key_Alt, Qt.Key_Meta):
            return  # modifier-only, wait for the real key
        modifiers = int(event.modifiers()) & ~Qt.KeypadModifier
        self.setKeySequence(QKeySequence(key | modifiers))
