"""Per-row actions menus — Git Changes (server), Path actions (Open Terminal / IDE)."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from typing import TYPE_CHECKING, Optional

from PyQt5.QtWidgets import QFileDialog, QMenu, QMessageBox
from PyQt5.QtGui import QCursor

from leap.monitor.dialogs.branch_picker_dialog import BranchPickerDialog
from leap.monitor.dialogs.git_changes_dialog import CommitListDialog
from leap.monitor.navigation import open_terminal_with_command
from leap.monitor.scm_polling import BackgroundCallWorker

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from leap.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object


class ActionsMenuMixin(_Base):
    """Actions menu handlers for session rows (git menu + path menu)."""

    # ── Git menu (server 3-dot button / server right-click) ──────────

    def _show_git_menu(self, tag: str) -> None:
        """Show the git changes menu with all three options directly."""
        project_path = self._resolve_project_path(tag)
        path_missing = self._last_path_missing
        has_path = bool(project_path)
        has_git = has_path and self._has_git_project(tag)

        menu = QMenu(self)
        if self._prefs.get('show_tooltips', True):
            menu.setToolTipsVisible(True)

        no_git_tip = ('Project directory no longer exists' if path_missing
                      else 'No git project detected')

        local_action = menu.addAction('See local uncommitted changes')
        local_action.setEnabled(has_git)
        local_action.setToolTip(
            'Show uncommitted changes using difftool' if has_git
            else no_git_tip
        )

        main_action = menu.addAction('Compare to branch')
        main_action.setEnabled(has_git)
        main_action.setToolTip(
            'Compare HEAD to a selected branch' if has_git
            else no_git_tip
        )

        commit_action = menu.addAction('Compare to previous commit')
        commit_action.setEnabled(has_git)
        commit_action.setToolTip(
            'Pick a commit and show its diff using difftool' if has_git
            else no_git_tip
        )

        chosen = menu.exec_(QCursor.pos())
        if not chosen or not has_git or not project_path:
            return

        if chosen == local_action:
            self._run_git_difftool([], project_path)
        elif chosen == main_action:
            self._show_branch_picker(project_path)
        elif chosen == commit_action:
            self._show_commit_picker(project_path)

    # ── Path menu (path 3-dot button / path right-click) ─────────────

    def _show_path_menu(self, tag: str) -> None:
        """Show the path actions menu (Open in Terminal, Open with IDE)."""
        project_path = self._resolve_project_path(tag)
        path_missing = self._last_path_missing
        has_path = bool(project_path)

        no_path_tip = ('Project directory no longer exists' if path_missing
                       else 'No project path available')

        menu = QMenu(self)
        if self._prefs.get('show_tooltips', True):
            menu.setToolTipsVisible(True)

        terminal_action = menu.addAction('Open in Terminal')
        terminal_action.setEnabled(has_path)
        terminal_action.setToolTip(
            'Open default terminal and cd to project path' if has_path
            else no_path_tip
        )

        ide_action = menu.addAction('Open with IDE')
        ide_action.setEnabled(has_path)
        ide_action.setToolTip(
            'Open project in a selected .app' if has_path
            else no_path_tip
        )

        chosen = menu.exec_(QCursor.pos())
        if not chosen or not has_path or not project_path:
            return

        if chosen == terminal_action:
            self._open_in_terminal(tag, project_path)
        elif chosen == ide_action:
            self._open_with_ide(tag, project_path)

    # ── Helpers ───────────────────────────────────────────────────────

    def _resolve_project_path(self, tag: str) -> Optional[str]:
        """Resolve the project path for a session tag.

        Returns None if no path is configured or the directory no longer exists.
        Sets ``_last_path_missing`` flag so callers can distinguish the two cases.
        """
        self._last_path_missing = False
        path: Optional[str] = None
        # Try active sessions first
        for s in self.sessions:
            if s['tag'] == tag and s.get('project_path'):
                path = s['project_path']
                break
        if not path:
            # Fall back to pinned sessions
            pin = self._pinned_sessions.get(tag, {})
            path = pin.get('project_path') or None
        # Guard: verify the directory still exists on disk
        if path and not os.path.isdir(path):
            logger.warning("Project path no longer exists for '%s': %s", tag, path)
            self._last_path_missing = True
            return None
        return path

    def _has_git_project(self, tag: str) -> bool:
        """Return True if the session has a git project (Project column is not N/A)."""
        for s in self.sessions:
            if s['tag'] == tag:
                project = s.get('project', '')
                return bool(project) and project != 'N/A'
        return False

    def _open_in_terminal(self, tag: str, project_path: str) -> None:
        """Open the default terminal and cd to the project path."""
        default_terminal = self._prefs.get('default_terminal', '')

        _path = project_path
        _term = default_terminal

        def _open() -> None:
            open_terminal_with_command(
                f'cd "{_path}"',
                preferred_ide=_term or None,
            )

        worker = BackgroundCallWorker(_open, self)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self._show_status(f"Opening terminal for '{tag}'")

    def _open_with_ide(self, tag: str, project_path: str) -> None:
        """Open a file dialog to pick an .app, then open the project with it."""
        last_app = self._prefs.get('last_ide_app', '')
        if last_app:
            start_dir = str(last_app).rsplit('/', 1)[0]
        else:
            home_apps = os.path.expanduser('~/Applications')
            start_dir = home_apps if os.path.isdir(home_apps) else '/Applications'

        path, _ = QFileDialog.getOpenFileName(
            self,
            'Select IDE Application',
            start_dir,
            'Applications (*.app)',
        )
        if not path:
            return

        self._prefs['last_ide_app'] = path
        self._save_prefs()

        _app_path = path
        _proj_path = project_path

        worker = BackgroundCallWorker(
            lambda: subprocess.Popen(
                ['open', '-a', _app_path, _proj_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ),
            self,
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self._show_status(f"Opening {path.rsplit('/', 1)[-1]} for '{tag}'")

    def _show_branch_picker(self, project_path: str) -> None:
        """Open branch picker, then run difftool for the selected branch."""
        dialog = BranchPickerDialog(project_path, parent=self)
        if dialog.exec_():
            ref = dialog.selected_branch()
            if ref:
                self._run_git_difftool([ref], project_path)

    def _show_commit_picker(self, project_path: str) -> None:
        """Open commit list, then run difftool for the selected commit."""
        dialog = CommitListDialog(project_path, parent=self)
        if dialog.exec_():
            sha = dialog.selected_commit()
            if sha:
                self._run_git_difftool([f'{sha}~1', sha], project_path)

    def _run_git_difftool(self, diff_args: list, cwd: str) -> None:
        """Check for changes, then run git difftool (fire-and-forget).

        Args:
            diff_args: Ref arguments for the diff (e.g. [], ['origin/main'],
                       ['sha~1', 'sha']).
            cwd: Working directory for the git command.
        """
        # For local diffs (no ref args), stage intent-to-add for untracked
        # files so they appear in difftool (equivalent to `git add -N .`).
        if not diff_args:
            try:
                subprocess.run(
                    ['git', 'add', '-N', '.'],
                    cwd=cwd, capture_output=True, timeout=10,
                )
            except Exception:
                logger.debug("git add -N failed", exc_info=True)

        # Check if there are actual changes before launching the tool
        check_cmd = ['git', 'diff', '--quiet'] + list(diff_args)
        try:
            result = subprocess.run(
                check_cmd, cwd=cwd, capture_output=True, timeout=10,
            )
            if result.returncode == 0:
                # Exit code 0 = no differences
                QMessageBox.information(self, 'No Changes', 'No differences found.')
                return
        except Exception:
            # If the check fails, proceed anyway and let difftool handle it
            logger.debug("git diff --quiet check failed", exc_info=True)

        diff_tool = self._prefs.get('default_diff_tool', '')
        # Tools that don't support directory diff mode (-d).
        # VS Code opens temp left/right folders as a workspace instead of
        # showing actual diffs, so we use file-by-file mode for it.
        _NO_DIR_DIFF_TOOLS = {'vscode'}
        _no_dir = diff_tool in _NO_DIR_DIFF_TOOLS
        # Full-path Electron binaries (Cursor) also don't support dir diff
        if not _no_dir and '/' in diff_tool:
            _no_dir = diff_tool.rsplit('/', 1)[-1] in {'cursor'}
        use_dir_diff = not _no_dir

        if diff_tool and '/' in diff_tool:
            # Full path to a CLI binary (e.g. JetBrains IDE, Cursor).
            # git difftool --extcmd doesn't use shell expansion, so we
            # create a tiny wrapper script that calls the binary.
            # Electron apps (Cursor) use --diff flag; JetBrains uses diff subcommand.
            bin_basename = diff_tool.rsplit("/", 1)[-1]
            # Electron apps (Cursor) use `--wait --diff` (like VS Code);
            # JetBrains uses `diff` subcommand (no --wait needed, blocks by default).
            _ELECTRON_DIFF_BINARIES = {'cursor'}
            if bin_basename in _ELECTRON_DIFF_BINARIES:
                diff_flag = '--wait --diff'
            else:
                diff_flag = 'diff'
            wrapper = tempfile.NamedTemporaryFile(
                mode='w', suffix='.sh', prefix='leap-diff-', delete=False,
            )
            wrapper.write(f'#!/bin/sh\nexec "{diff_tool}" {diff_flag} "$@"\n')
            wrapper.close()
            os.chmod(wrapper.name, 0o755)
            cmd = ['git', 'difftool', '-y', f'--extcmd={wrapper.name}']
            if use_dir_diff:
                cmd.insert(2, '-d')
            cmd.extend(diff_args)
            bin_name = diff_tool.rsplit("/", 1)[-1]
            display = f'git difftool{" -d" if use_dir_diff else ""} (via {bin_name})'
        else:
            cmd = ['git', 'difftool', '-y']
            if use_dir_diff:
                cmd.insert(2, '-d')
            if diff_tool:
                cmd.append(f'--tool={diff_tool}')
            cmd.extend(diff_args)
            display = ' '.join(cmd)

        _cmd = cmd
        _cwd = cwd

        worker = BackgroundCallWorker(
            lambda: subprocess.Popen(
                _cmd,
                cwd=_cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ),
            self,
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self._show_status(f"Running: {display}")
