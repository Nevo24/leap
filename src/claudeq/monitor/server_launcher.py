"""Server launcher for ClaudeQ Monitor.

Handles the MR server startup flow: find/clone project directories,
check git state, checkout branches, and open CQ in a terminal.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from PyQt5.QtWidgets import QApplication, QMessageBox

from claudeq.monitor.mr_tracking.config import save_pinned_sessions
from claudeq.monitor.navigation import open_terminal_with_command
from claudeq.monitor.scm_polling import BackgroundCallWorker
from claudeq.monitor.dialogs.settings_dialog import DEFAULT_REPOS_DIR

if TYPE_CHECKING:
    from claudeq.monitor.app import MonitorWindow

logger = logging.getLogger(__name__)


class ServerLauncher:
    """Manages server start logic for pinned (dead) rows.

    For MR-pinned rows: find/clone project, check branch, checkout if needed,
    then open CQ. For auto-pinned rows: open CQ directly.
    """

    def __init__(self, window: MonitorWindow) -> None:
        self._w = window

    def start_server(self, tag: str) -> None:
        """Start a new server for a pinned (dead) row.

        For MR-pinned rows (with remote_project_path): find/clone project,
        check branch, checkout if needed, then open CQ.
        For auto-pinned rows (with local project_path): open CQ directly.
        """
        pinned = self._w._pinned_sessions.get(tag, {})

        # MR-pinned row that hasn't been set up locally yet — needs git setup
        if pinned.get('remote_project_path') and not pinned.get('project_path'):
            self._start_server_from_mr(tag, pinned)
            return

        # Auto-pinned row or MR-pinned with known local path — open directly
        preferred_ide: Optional[str] = None
        project_path: Optional[str] = None

        session = next((s for s in self._w.sessions if s['tag'] == tag), None)
        if session and session.get('has_client'):
            preferred_ide = session.get('ide') or pinned.get('ide')
            project_path = session.get('project_path') or pinned.get('project_path')
        else:
            preferred_ide = self._w._prefs.get('default_terminal')
            project_path = pinned.get('project_path') or None

        self._open_cq_in_terminal(tag, preferred_ide, project_path)

    def _open_cq_in_terminal(
        self, tag: str, preferred_ide: Optional[str], project_path: Optional[str],
    ) -> None:
        """Open a CQ server in a terminal at the given project path."""
        cmd = f"cd {project_path} && cq '{tag}'" if project_path else f"cq '{tag}'"
        worker = BackgroundCallWorker(
            lambda: open_terminal_with_command(
                cmd, preferred_ide=preferred_ide, project_path=project_path,
            ),
            self._w,
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _find_available_project_dir(
        self, repos_dir: Path, project_name: str,
    ) -> tuple[Path, bool, list[str]]:
        """Find a project directory not used by a running CQ server.

        Checks repo-name, repo-name_1, repo-name_2, ...
        Returns (project_dir, needs_clone, in_use_names).
        in_use_names lists the directory names that were skipped because
        they have an active CQ server.
        """
        active_paths = self._w._get_active_project_paths()
        in_use: list[str] = []

        # Start with base name, then _1, _2, ...
        candidates = [project_name] + [f'{project_name}_{i}' for i in range(1, 100)]
        for name in candidates:
            candidate = repos_dir / name
            resolved = str(candidate.resolve())
            if not candidate.is_dir():
                return candidate, True, in_use  # Doesn't exist — needs clone
            if resolved not in active_paths:
                return candidate, False, in_use  # Exists and no CQ server using it
            in_use.append(name)
        # Fallback (shouldn't happen with 100 candidates)
        fallback = repos_dir / f'{project_name}_{100}'
        return fallback, True, in_use

    def _start_server_from_mr(self, tag: str, pinned: dict[str, Any]) -> None:
        """Start server for an MR-pinned row: find/clone project, checkout branch."""
        repos_dir = self._w._prefs.get('repos_dir', DEFAULT_REPOS_DIR).strip() or DEFAULT_REPOS_DIR

        remote_project = pinned['remote_project_path']
        host_url = pinned.get('host_url', '')
        branch = pinned.get('branch', '')
        project_name = remote_project.rsplit('/', 1)[-1]
        rd = Path(repos_dir).expanduser()
        rd.mkdir(parents=True, exist_ok=True)

        project_dir, needs_clone, in_use_names = self._find_available_project_dir(
            rd, project_name,
        )

        if needs_clone:
            clone_url = f"{host_url}/{remote_project}.git"
            if in_use_names:
                used = ', '.join(in_use_names)
                self._w._show_status(
                    f"Cloning to {project_dir.name} "
                    f"({used} in use by other servers)",
                )
            else:
                self._w._show_status(f"Cloning {project_name} to {project_dir.name}...")
            clone_ok: list[bool] = [False]
            clone_err: list[str] = ['']

            def _clone() -> None:
                try:
                    subprocess.run(
                        ['git', 'clone', clone_url, str(project_dir)],
                        check=True, capture_output=True, text=True, timeout=120,
                    )
                    clone_ok[0] = True
                except subprocess.CalledProcessError as e:
                    clone_err[0] = e.stderr or str(e)
                except Exception as e:
                    clone_err[0] = str(e)

            w = BackgroundCallWorker(_clone, self._w)
            w.finished.connect(lambda: self._on_server_cloned(
                tag, pinned, project_dir, branch, clone_ok, clone_err,
            ))
            w.finished.connect(w.deleteLater)
            w.start()
            return

        # Project exists and no CQ server using it — check git state
        self._w._show_status(f"Checking '{project_dir.name}' is up to date...")
        self._server_check_git(tag, pinned, project_dir, branch)

    def _on_server_cloned(
        self, tag: str, pinned: dict[str, Any], project_dir: Path,
        branch: str, clone_ok: list, clone_err: list,
    ) -> None:
        """Handle clone completion for server start."""
        if not clone_ok[0]:
            QMessageBox.warning(self._w, 'Clone Failed', clone_err[0] or 'Unknown error.')
            return
        self._w._show_status(f"Cloned. Checking branch '{branch}'...")
        self._server_check_git(tag, pinned, project_dir, branch)

    def _server_check_git(
        self, tag: str, pinned: dict[str, Any], project_dir: Path, branch: str,
    ) -> None:
        """Fetch remote branch, check if local is up-to-date, checkout if needed."""
        self._w._show_status(f"Fetching branch '{branch}' in '{project_dir.name}'...")
        state: dict[str, Any] = {'fetch_err': '', 'up_to_date': False, 'dirty': False}

        def _check() -> None:
            cwd = str(project_dir)
            refspec = f'+refs/heads/{branch}:refs/remotes/origin/{branch}'
            r = subprocess.run(
                ['git', 'fetch', 'origin', refspec],
                capture_output=True, text=True, cwd=cwd, timeout=30,
            )
            if r.returncode != 0:
                state['fetch_err'] = r.stderr.strip() or 'fetch failed'
                return
            r = subprocess.run(
                ['git', 'merge-base', '--is-ancestor', f'origin/{branch}', 'HEAD'],
                capture_output=True, text=True, cwd=cwd, timeout=5,
            )
            state['up_to_date'] = r.returncode == 0
            if not state['up_to_date']:
                r = subprocess.run(
                    ['git', 'status', '--porcelain'],
                    capture_output=True, text=True, cwd=cwd, timeout=5,
                )
                state['dirty'] = bool(r.stdout.strip())

        w = BackgroundCallWorker(_check, self._w)
        w.finished.connect(lambda: self._on_server_git_checked(
            tag, pinned, project_dir, branch, state,
        ))
        w.finished.connect(w.deleteLater)
        w.start()

    def _on_server_git_checked(
        self, tag: str, pinned: dict[str, Any], project_dir: Path,
        branch: str, state: dict[str, Any],
    ) -> None:
        """Handle git check result for server start."""
        if state['fetch_err']:
            # Branch gone (e.g. MR merged) — still open CQ in the project dir
            QMessageBox.information(
                self._w, 'Branch Not Available',
                f"Branch '{branch}' does not exist on the remote.\n\n"
                f"Opening CQ in the project directory anyway.",
            )
            self._server_finish(tag, pinned, project_dir)
            return

        if state['up_to_date']:
            self._server_finish(tag, pinned, project_dir)
            return

        if state['dirty']:
            QMessageBox.warning(
                self._w, 'Cannot Update',
                f"'{project_dir.name}' is behind '{branch}' and has "
                f"uncommitted changes.\n\n"
                f"Commit or stash your changes first.",
            )
            return

        # Behind but clean — checkout + pull
        self._w._show_status(f"Updating {project_dir.name} to latest '{branch}'...")
        checkout_err: list[str] = ['']

        def _checkout() -> None:
            try:
                cwd = str(project_dir)
                r = subprocess.run(
                    ['git', 'checkout', branch],
                    capture_output=True, text=True, cwd=cwd, timeout=10,
                )
                if r.returncode != 0:
                    subprocess.run(
                        ['git', 'checkout', '--track', f'origin/{branch}'],
                        check=True, capture_output=True, text=True,
                        cwd=cwd, timeout=10,
                    )
                subprocess.run(
                    ['git', 'pull', '--ff-only'],
                    capture_output=True, text=True, cwd=cwd, timeout=30,
                )
            except subprocess.CalledProcessError as e:
                checkout_err[0] = e.stderr or str(e)
            except Exception as e:
                checkout_err[0] = str(e)

        w = BackgroundCallWorker(_checkout, self._w)
        w.finished.connect(lambda: self._on_server_checked_out(
            tag, pinned, project_dir, checkout_err,
        ))
        w.finished.connect(w.deleteLater)
        w.start()

    def _on_server_checked_out(
        self, tag: str, pinned: dict[str, Any], project_dir: Path,
        checkout_err: list,
    ) -> None:
        """Handle checkout completion for server start."""
        if checkout_err[0]:
            QMessageBox.warning(
                self._w, 'Checkout Failed',
                f"Could not switch to branch:\n{checkout_err[0]}",
            )
            return
        self._server_finish(tag, pinned, project_dir)

    def _server_finish(self, tag: str, pinned: dict[str, Any], project_dir: Path) -> None:
        """Final step: update pinned data with local path and open CQ."""
        self._w._show_status(f"Opening CQ '{tag}' in {project_dir.name}...")

        # Save local project path for future use
        pinned['project_path'] = str(project_dir)
        self._w._pinned_sessions[tag] = pinned
        save_pinned_sessions(self._w._pinned_sessions)

        preferred_ide = self._w._prefs.get('default_terminal')
        self._open_cq_in_terminal(tag, preferred_ide, str(project_dir))
