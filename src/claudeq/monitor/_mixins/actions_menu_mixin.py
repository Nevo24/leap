"""Per-row actions menu — Open with IDE, See Git Changes."""

from __future__ import annotations

import logging
import os
import subprocess
from typing import TYPE_CHECKING, Optional

from PyQt5.QtWidgets import QFileDialog, QMenu, QMessageBox
from PyQt5.QtGui import QCursor

from claudeq.monitor.scm_polling import BackgroundCallWorker

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from claudeq.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object


class ActionsMenuMixin(_Base):
    """Three-dot actions menu handlers for session rows."""

    def _show_actions_menu(self, tag: str) -> None:
        """Show the per-row actions menu at the cursor position."""
        project_path = self._resolve_project_path(tag)
        has_path = bool(project_path)
        has_git = has_path and self._has_git_project(tag)

        menu = QMenu(self)
        if self._prefs.get('show_tooltips', True):
            menu.setToolTipsVisible(True)

        ide_action = menu.addAction('Open with IDE')
        ide_action.setEnabled(has_path)
        ide_action.setToolTip(
            'Open project in a selected .app' if has_path
            else 'No project path available'
        )

        git_action = menu.addAction('See Git Changes')
        git_action.setEnabled(has_git)
        git_action.setToolTip(
            'View diffs using git difftool' if has_git
            else 'No git project detected'
        )

        chosen = menu.exec_(QCursor.pos())
        if chosen == ide_action and has_path:
            self._open_with_ide(tag, project_path)
        elif chosen == git_action and has_git:
            self._show_git_changes(tag, project_path)

    def _resolve_project_path(self, tag: str) -> Optional[str]:
        """Resolve the project path for a session tag."""
        # Try active sessions first
        for s in self.sessions:
            if s['tag'] == tag and s.get('project_path'):
                return s['project_path']
        # Fall back to pinned sessions
        pin = self._pinned_sessions.get(tag, {})
        return pin.get('project_path') or None

    def _has_git_project(self, tag: str) -> bool:
        """Return True if the session has a git project (Project column is not N/A)."""
        for s in self.sessions:
            if s['tag'] == tag:
                project = s.get('project', '')
                return bool(project) and project != 'N/A'
        return False

    def _open_with_ide(self, tag: str, project_path: str) -> None:
        """Open a file dialog to pick an .app, then open the project with it."""
        last_app = self._prefs.get('last_ide_app', '')
        start_dir = str(last_app).rsplit('/', 1)[0] if last_app else '/Applications'

        path, _ = QFileDialog.getOpenFileName(
            self,
            'Select IDE Application',
            start_dir,
            'Applications (*.app)',
        )
        if not path:
            return

        self._prefs['last_ide_app'] = path
        from claudeq.monitor.mr_tracking.config import save_monitor_prefs
        save_monitor_prefs(self._prefs)

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

    def _show_git_changes(self, tag: str, project_path: str) -> None:
        """Open the Git Changes dialog."""
        from claudeq.monitor.dialogs.git_changes_dialog import GitChangesDialog

        dialog = GitChangesDialog(
            project_path=project_path,
            on_run_git=self._run_git_difftool,
            parent=self,
        )
        dialog.exec_()

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
        if diff_tool and '/' in diff_tool:
            # Full path to a CLI binary (e.g. JetBrains IDE).
            # git difftool --extcmd doesn't use shell expansion, so we
            # create a tiny wrapper script that calls "<binary> diff $@".
            import tempfile
            wrapper = tempfile.NamedTemporaryFile(
                mode='w', suffix='.sh', prefix='cq-diff-', delete=False,
            )
            wrapper.write(f'#!/bin/sh\nexec "{diff_tool}" diff "$@"\n')
            wrapper.close()
            os.chmod(wrapper.name, 0o755)
            cmd = ['git', 'difftool', '-d', '-y', f'--extcmd={wrapper.name}']
            cmd.extend(diff_args)
            display = f'git difftool -d (via {diff_tool.rsplit("/", 1)[-1]})'
        else:
            cmd = ['git', 'difftool', '-d', '-y']
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
