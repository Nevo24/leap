"""Server launcher for ClaudeQ Monitor.

Handles the PR server startup flow: find/clone project directories,
check git state, checkout branches, and open CQ in a terminal.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication, QMessageBox

from claudeq.monitor.pr_tracking.config import save_pinned_sessions
from claudeq.monitor.pr_tracking.git_utils import detect_default_branch
from claudeq.monitor.navigation import open_terminal_with_command
from claudeq.monitor.scm_polling import BackgroundCallWorker
from claudeq.monitor.dialogs.settings_dialog import DEFAULT_REPOS_DIR

if TYPE_CHECKING:
    from claudeq.monitor.app import MonitorWindow

logger = logging.getLogger(__name__)


def _is_git_repo(path: Path) -> bool:
    """Check if a directory is a valid git repository."""
    try:
        r = subprocess.run(
            ['git', 'rev-parse', '--git-dir'],
            capture_output=True, cwd=str(path), timeout=5,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


class ServerLauncher:
    """Manages server start logic for pinned (dead) rows.

    For PR-pinned rows: find/clone project, check branch, checkout if needed,
    then open CQ. For auto-pinned rows: open CQ directly.
    """

    def __init__(self, window: MonitorWindow) -> None:
        self._w = window

    def _get_scm_token(self, scm_type: str) -> Optional[str]:
        """Get the authentication token for the given SCM type from the provider."""
        provider = self._w._scm_providers.get(scm_type)
        if provider is None:
            return None
        if scm_type == 'gitlab':
            return getattr(provider, '_gl', None) and provider._gl.private_token
        if scm_type == 'github':
            return getattr(provider, '_token', None)
        return None

    def _build_clone_url(self, host_url: str, remote_project: str, scm_type: str) -> str:
        """Build clone URL, injecting SCM token for authentication if available."""
        # Strip any existing credentials from host_url (may be leftover from
        # a previous run that contaminated pinned session data).
        scheme_end = host_url.index('://') + 3 if '://' in host_url else 0
        scheme = host_url[:scheme_end]
        rest = host_url[scheme_end:]
        # Remove user:pass@ prefix if present
        if '@' in rest:
            rest = rest.rsplit('@', 1)[-1]
        clean_host_url = f"{scheme}{rest}"

        base_url = f"{clean_host_url}/{remote_project}.git"
        token = self._get_scm_token(scm_type)
        if not token or not clean_host_url.startswith('http'):
            return base_url
        encoded_token = quote(token, safe='')
        if scm_type == 'github':
            return f"{scheme}x-access-token:{encoded_token}@{rest}/{remote_project}.git"
        # GitLab uses oauth2 as the username
        return f"{scheme}oauth2:{encoded_token}@{rest}/{remote_project}.git"

    def start_server(self, tag: str) -> None:
        """Start a new server for a pinned (dead) row.

        For PR-pinned rows (with remote_project_path): find/clone project,
        check branch, checkout if needed, then open CQ.
        For auto-pinned rows (with local project_path): open CQ directly.
        """
        pinned = self._w._pinned_sessions.get(tag, {})

        if pinned.get('remote_project_path'):
            project_path = pinned.get('project_path')
            if not project_path:
                # PR-pinned row, first time — needs clone + git setup
                self._start_server_from_pr(tag, pinned)
            else:
                # Check if another CQ server is already using this directory
                resolved = str(Path(project_path).resolve())
                active_paths = self._w._get_active_project_paths()
                project_dir = Path(project_path)
                if resolved in active_paths:
                    # Path in use — clear it so _start_server_from_pr finds a free dir
                    pinned['project_path'] = ''
                    self._start_server_from_pr(tag, pinned)
                elif not project_dir.is_dir() or not _is_git_repo(project_dir):
                    # Dir was deleted or isn't a valid git repo — clear and re-clone
                    pinned['project_path'] = ''
                    self._start_server_from_pr(tag, pinned)
                else:
                    # Local path free — force-align to remote
                    branch = pinned.get('branch', '') or detect_default_branch(str(project_dir))
                    self._w._show_status(f"Syncing '{project_dir.name}' to origin/{branch}...")
                    self._server_force_align(tag, pinned, project_dir, branch)
            return

        # Auto-pinned row — open directly in the default terminal from settings
        preferred_ide = self._w._prefs.get('default_terminal')
        session = next((s for s in self._w.sessions if s['tag'] == tag), None)
        project_path: Optional[str] = (
            (session.get('project_path') if session else None)
            or pinned.get('project_path')
            or None
        )

        self._open_cq_in_terminal(tag, preferred_ide, project_path)

    def _open_cq_in_terminal(
        self, tag: str, preferred_ide: Optional[str], project_path: Optional[str],
    ) -> None:
        """Open a CQ server in a terminal at the given project path."""
        # Guard: if the project directory was deleted, ask the user instead of
        # crashing the IDE (e.g. JetBrains "Could not determine current working directory").
        if project_path and not Path(project_path).is_dir():
            logger.warning("Project path does not exist: %s", project_path)
            from PyQt5.QtWidgets import QMessageBox
            reply = QMessageBox.warning(
                self._w,
                'Project Directory Missing',
                f'The project directory no longer exists:\n\n{project_path}\n\n'
                'Start the server without a project directory?',
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if reply != QMessageBox.Yes:
                return
            project_path = None
        cmd = f"cd {shlex.quote(project_path)} && claudeq {shlex.quote(tag)}" if project_path else f"claudeq {shlex.quote(tag)}"
        worker = BackgroundCallWorker(
            lambda: open_terminal_with_command(
                cmd, preferred_ide=preferred_ide, project_path=project_path,
            ),
            self._w,
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()
        # Safety net: if the server hasn't appeared after 15s (e.g. validation
        # error in the terminal), clear the "Starting..." guard.
        QTimer.singleShot(15_000, lambda: self._cancel_start(tag))

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
            if not candidate.is_dir() or not _is_git_repo(candidate):
                return candidate, True, in_use  # Doesn't exist or not a valid git repo — needs clone
            if resolved not in active_paths:
                return candidate, False, in_use  # Exists and no CQ server using it
            in_use.append(name)
        # Fallback (shouldn't happen with 100 candidates)
        fallback = repos_dir / f'{project_name}_{100}'
        return fallback, True, in_use

    def _start_server_from_pr(self, tag: str, pinned: dict[str, Any]) -> None:
        """Start server for a PR-pinned row: find/clone project, checkout branch."""
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
            clone_url = self._build_clone_url(host_url, remote_project, pinned.get('scm_type', ''))
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
                    # Remove broken/non-git directory if it exists
                    if project_dir.exists():
                        shutil.rmtree(project_dir)
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

        # Project exists and no CQ server using it — force-align to branch
        if not branch:
            branch = detect_default_branch(str(project_dir))
        self._w._show_status(f"Syncing '{project_dir.name}' to origin/{branch}...")
        self._server_force_align(tag, pinned, project_dir, branch)

    def _cancel_start(self, tag: str) -> None:
        """Clear the starting guard so the button resets."""
        self._w._starting_tags.discard(tag)
        self._w._update_table()

    def _on_server_cloned(
        self, tag: str, pinned: dict[str, Any], project_dir: Path,
        branch: str, clone_ok: list, clone_err: list,
    ) -> None:
        """Handle clone completion for server start."""
        if not clone_ok[0]:
            QMessageBox.warning(self._w, 'Clone Failed', clone_err[0] or 'Unknown error.')
            self._cancel_start(tag)
            return
        commit = pinned.get('commit', '')
        if not branch and commit:
            # Commit URL — checkout specific commit after clone
            self._w._show_status(f"Cloned. Checking out commit {commit[:8]}...")
            self._server_checkout_commit(tag, pinned, project_dir, commit)
            return
        if not branch:
            branch = detect_default_branch(str(project_dir))
        self._w._show_status(f"Cloned. Checking out branch '{branch}'...")
        self._server_force_align(tag, pinned, project_dir, branch)

    def _server_checkout_commit(
        self, tag: str, pinned: dict[str, Any], project_dir: Path, commit: str,
    ) -> None:
        """Checkout a specific commit SHA after cloning."""
        checkout_err: list[str] = ['']

        def _checkout() -> None:
            try:
                subprocess.run(
                    ['git', 'checkout', commit],
                    check=True, capture_output=True, text=True,
                    cwd=str(project_dir), timeout=30,
                )
            except subprocess.CalledProcessError as e:
                checkout_err[0] = e.stderr or str(e)
            except Exception as e:
                checkout_err[0] = str(e)

        w = BackgroundCallWorker(_checkout, self._w)
        w.finished.connect(lambda: self._on_server_commit_checked_out(
            tag, pinned, project_dir, commit, checkout_err,
        ))
        w.finished.connect(w.deleteLater)
        w.start()

    def _on_server_commit_checked_out(
        self, tag: str, pinned: dict[str, Any], project_dir: Path,
        commit: str, checkout_err: list,
    ) -> None:
        """Handle commit checkout completion."""
        if checkout_err[0]:
            QMessageBox.warning(
                self._w, 'Checkout Failed',
                f"Could not checkout commit {commit[:8]}:\n{checkout_err[0]}",
            )
            self._cancel_start(tag)
            return
        self._server_finish(tag, pinned, project_dir)

    def _server_force_align(
        self, tag: str, pinned: dict[str, Any], project_dir: Path, branch: str,
    ) -> None:
        """Fetch remote branch, force-checkout and hard-reset to origin.

        These are managed clones in repos_dir, not user workspaces — local
        changes are always discarded in favour of the remote state.
        """
        if not branch:
            branch = detect_default_branch(str(project_dir))
        self._w._show_status(f"Syncing '{project_dir.name}' to origin/{branch}...")
        fetch_err: list[str] = ['']
        align_err: list[str] = ['']
        # Pre-compute authenticated URL on main thread (accesses providers)
        auth_url = self._build_clone_url(
            pinned.get('host_url', ''), pinned.get('remote_project_path', ''),
            pinned.get('scm_type', ''),
        )

        def _align() -> None:
            cwd = str(project_dir)

            # 0. Ensure remote URL has auth token (for repos cloned before token injection)
            if auth_url:
                subprocess.run(
                    ['git', 'remote', 'set-url', 'origin', auth_url],
                    capture_output=True, text=True, cwd=cwd, timeout=5,
                )

            # 1. Fetch the branch
            refspec = f'+refs/heads/{branch}:refs/remotes/origin/{branch}'
            r = subprocess.run(
                ['git', 'fetch', 'origin', refspec],
                capture_output=True, text=True, cwd=cwd, timeout=30,
            )
            if r.returncode != 0:
                fetch_err[0] = r.stderr.strip() or 'fetch failed'
                return

            try:
                # 2. Checkout branch (create tracking branch if needed)
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
                # 3. Hard-reset to remote (unconditional)
                subprocess.run(
                    ['git', 'reset', '--hard', f'origin/{branch}'],
                    check=True, capture_output=True, text=True,
                    cwd=cwd, timeout=10,
                )
                # 4. Remove untracked files
                subprocess.run(
                    ['git', 'clean', '-fd'],
                    check=True, capture_output=True, text=True,
                    cwd=cwd, timeout=10,
                )
            except subprocess.CalledProcessError as e:
                align_err[0] = e.stderr or str(e)
            except Exception as e:
                align_err[0] = str(e)

        w = BackgroundCallWorker(_align, self._w)
        w.finished.connect(lambda: self._on_server_force_aligned(
            tag, pinned, project_dir, branch, fetch_err, align_err,
        ))
        w.finished.connect(w.deleteLater)
        w.start()

    def _on_server_force_aligned(
        self, tag: str, pinned: dict[str, Any], project_dir: Path,
        branch: str, fetch_err: list, align_err: list,
    ) -> None:
        """Handle force-align completion for server start."""
        if fetch_err[0]:
            err = fetch_err[0].lower()
            branch_gone = (
                "couldn't find remote ref" in err
                or 'not found' in err
                or 'no such remote ref' in err
            )
            if branch_gone:
                reply = QMessageBox.question(
                    self._w, 'Branch Not Available',
                    f"Branch '{branch}' was deleted on remote (PR merged?).\n\n"
                    f"CQ will start on the last local state of '{branch}' "
                    f"in {project_dir}.\n\n"
                    f"Open anyway?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply == QMessageBox.Yes:
                    self._server_finish(tag, pinned, project_dir)
                else:
                    self._cancel_start(tag)
            else:
                reply = QMessageBox.question(
                    self._w, 'Fetch Failed',
                    f"Could not fetch branch '{branch}' from remote:\n"
                    f"{fetch_err[0]}\n\n"
                    f"Start CQ without syncing?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply == QMessageBox.Yes:
                    self._server_finish(tag, pinned, project_dir)
                else:
                    self._cancel_start(tag)
            return

        if align_err[0]:
            QMessageBox.warning(
                self._w, 'Sync Failed',
                f"Could not sync '{project_dir.name}' to origin/{branch}:\n"
                f"{align_err[0]}",
            )
            self._cancel_start(tag)
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
