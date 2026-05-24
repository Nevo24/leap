"""Server launcher for Leap Monitor.

Handles the PR server startup flow: find/clone project directories,
check git state, checkout branches, and open Leap in a terminal.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QStyle, QVBoxLayout,
)

from leap.monitor.pr_tracking.config import save_pinned_sessions
from leap.monitor.pr_tracking.git_utils import detect_default_branch, resolve_ssh_alias
from leap.monitor.navigation import (
    find_jetbrains_app, is_ide_app, open_terminal_with_command,
)
from leap.monitor.scm_polling import BackgroundCallWorker
from leap.monitor.dialogs.settings_dialog import DEFAULT_REPOS_DIR


def _git_root_or(cwd: str, fallback: str) -> str:
    """Return ``git rev-parse --show-toplevel`` from *cwd*, or *fallback*.

    Used by ``open_resume_in_terminal`` to derive the project root for
    legacy resume records (saved before ``project_path`` was a field
    on ``SessionRecord``).  Best-effort: any failure — directory gone,
    not a git repo, git binary missing — falls through to ``fallback``.
    Short timeout because this runs synchronously on a UI button press.
    """
    if not cwd:
        return fallback
    try:
        result = subprocess.run(
            ['git', '-C', cwd, 'rev-parse', '--show-toplevel'],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            root = result.stdout.strip()
            if root:
                return root
    except (OSError, subprocess.SubprocessError):
        pass
    return fallback

if TYPE_CHECKING:
    from leap.monitor.app import MonitorWindow

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


def _dir_index(project_name: str, dir_name: str) -> int:
    """Numeric suffix of a candidate managed-clone dir name.

    ``<project_name>`` → 0, ``<project_name>_1`` → 1, etc. Returns -1
    for anything that doesn't fit the pattern.
    """
    if dir_name == project_name:
        return 0
    prefix = f'{project_name}_'
    if not dir_name.startswith(prefix):
        return -1
    try:
        return int(dir_name[len(prefix):])
    except ValueError:
        return -1


def _detached_head_sha(project_dir: Path) -> Optional[str]:
    """Return the short SHA at HEAD if HEAD is detached, else None.

    Detached HEAD means the user (or a prior commit-URL flow) checked out a
    specific commit rather than a branch. A sync would move HEAD to the
    target branch tip — so we want the dialog to spell that out explicitly,
    not just report an "N commits ahead" count that the user might read as
    "their commits".
    """
    try:
        r = subprocess.run(
            ['git', 'symbolic-ref', '--quiet', 'HEAD'],
            capture_output=True, cwd=str(project_dir), timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode == 0:
        return None  # HEAD is symbolic (on a branch) — not detached
    try:
        r = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True,
            cwd=str(project_dir), timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    sha = r.stdout.strip()
    return sha or None


def _commits_ahead_of_origin(project_dir: Path, branch: str) -> Optional[int]:
    """Count local commits on HEAD not in ``origin/<branch>``.

    Returns 0 if local is up-to-date or behind, a positive int if ahead, or
    ``None`` if the count itself couldn't be determined (no ``origin/<branch>``
    ref, git error, etc.). ``None`` MUST be treated as "unknown — be safe".

    The count is only meaningful against a freshly-fetched ``origin/<branch>``;
    callers should ensure a fetch ran first or accept that a stale local
    tracking ref may inflate the count (commits already in remote but not yet
    in local ``origin/<branch>``).
    """
    try:
        r = subprocess.run(
            ['git', 'rev-list', '--count', f'origin/{branch}..HEAD'],
            capture_output=True, text=True,
            cwd=str(project_dir), timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        logger.warning(
            "Ahead-count subprocess failed for %s (%s)",
            project_dir, branch, exc_info=True,
        )
        return None
    if r.returncode != 0:
        logger.warning(
            "Ahead-count non-zero for %s (%s): %s",
            project_dir, branch, (r.stderr or '').strip(),
        )
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def _dirty_files(project_dir: Path) -> Optional[list[str]]:
    """List local files a force-align would discard in this managed clone.

    Combines tracked modifications (wiped by ``reset --hard``) and
    untracked files (wiped by ``clean -fd``). Ignored paths are skipped
    because ``git status --porcelain`` honours .gitignore.

    Returns ``[]`` when the tree is clean, a non-empty list when dirty,
    and ``None`` when the scan itself failed (timeout, missing git, etc.).
    Callers MUST treat ``None`` as "unknown" rather than "clean" — the
    consent gate exists precisely so we don't destroy work silently.
    """
    try:
        r = subprocess.run(
            ['git', 'status', '--porcelain'],
            capture_output=True, text=True,
            cwd=str(project_dir), timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        logger.warning("Dirty-check subprocess failed for %s", project_dir, exc_info=True)
        return None
    if r.returncode != 0:
        logger.warning(
            "Dirty-check non-zero for %s: %s",
            project_dir, (r.stderr or '').strip(),
        )
        return None
    out: list[str] = []
    for line in r.stdout.splitlines():
        # Porcelain format: "XY path" — XY is 2 chars + 1 space; path at index 3.
        if len(line) >= 4:
            out.append(line[3:])
    return out


class ServerLauncher:
    """Manages server start logic for pinned (dead) rows.

    For PR-pinned rows: find/clone project, check branch, checkout if needed,
    then open Leap. For auto-pinned rows: open Leap directly.
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
        """Build clone URL, injecting SCM token for authentication if available.

        Resolves SSH aliases at the host fragment (e.g. ``planck_gitlab`` →
        ``gitlab.com`` via ``~/.ssh/config``) — historically Leap stored
        ``host_url=https://<alias>`` for repos cloned via SSH alias, which
        is unusable for HTTPS clone.  The resolution heals such entries
        transparently; older pinned-session JSON gets corrected on the
        next ``save_pinned_sessions`` write.
        """
        # Strip any existing credentials from host_url (may be leftover from
        # a previous run that contaminated pinned session data).
        scheme_end = host_url.index('://') + 3 if '://' in host_url else 0
        scheme = host_url[:scheme_end]
        rest = host_url[scheme_end:]
        # Remove user:pass@ prefix if present
        if '@' in rest:
            rest = rest.rsplit('@', 1)[-1]
        # Resolve SSH alias (no-op for proper DNS hostnames thanks to the
        # short-circuit in resolve_ssh_alias).
        rest = resolve_ssh_alias(rest)
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

    def _heal_pinned_host_url(self, tag: str, pinned: dict[str, Any]) -> None:
        """Migrate a pinned-session entry whose host_url is an SSH alias.

        Called best-effort from the server-start path: if the stored
        ``host_url`` looks alias-shaped (no dots after the scheme) and
        ``ssh -G`` resolves it to something different, rewrite the
        pinned-sessions entry so future polls / clones / save-pinned
        rounds produce a usable URL.  No-op for proper DNS hostnames.
        """
        host_url = pinned.get('host_url', '')
        if not host_url or '://' not in host_url:
            return
        scheme, _, rest = host_url.partition('://')
        # Only consider alias-shaped hosts (no dots) — proper DNS names
        # already work, and resolve_ssh_alias would just echo them back.
        host_only = rest.split('/', 1)[0]
        if '.' in host_only or host_only == 'localhost':
            return
        resolved = resolve_ssh_alias(host_only)
        if resolved == host_only:
            return  # ssh -G didn't translate; nothing to migrate
        new_host_url = f'{scheme}://{resolved}'
        pinned['host_url'] = new_host_url
        try:
            save_pinned_sessions(self._w._pinned_sessions)
            logger.debug("Migrated host_url alias for tag %s: %s -> %s",
                         tag, host_only, resolved)
        except Exception:
            logger.debug("Failed to persist host_url migration for %s",
                         tag, exc_info=True)

    def start_server(self, tag: str) -> None:
        """Start a new server for a pinned (dead) row.

        For PR-pinned rows (with remote_project_path): find/clone project,
        check branch, checkout if needed, then open Leap.
        For auto-pinned rows (with local project_path): open Leap directly.
        """
        pinned = self._w._pinned_sessions.get(tag, {})
        # Best-effort: heal a host_url that looks like an SSH alias before
        # we try to use it.  No-op for proper DNS hostnames.
        self._heal_pinned_host_url(tag, pinned)

        if pinned.get('remote_project_path'):
            project_path = pinned.get('project_path')
            if not project_path:
                # PR-pinned row, first time — needs clone + git setup
                self._start_server_from_pr(tag, pinned)
            else:
                # Check if another Leap server is already using this directory
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
                    # Local path free — gate force-align on a dirty-tree prompt
                    # so we don't silently discard user edits in the managed clone.
                    branch = pinned.get('branch', '') or detect_default_branch(str(project_dir))
                    self._w._show_status(f"Checking '{project_dir.name}'...")
                    self._dirty_check_then_align(tag, pinned, project_dir, branch)
            return

        # Auto-pinned row — open directly in the default terminal from settings
        preferred_ide = self._w._prefs.get('default_terminal')
        session = next((s for s in self._w.sessions if s['tag'] == tag), None)
        project_path: Optional[str] = (
            (session.get('project_path') if session else None)
            or pinned.get('project_path')
            or None
        )

        self._open_leap_in_terminal(tag, preferred_ide, project_path)

    def open_resume_in_terminal(
        self, *, cli: str, tag: str, session_id: str,
        preferred_ide: Optional[str] = None,
        recorded_cwd: Optional[str] = None,
        recorded_project_path: Optional[str] = None,
    ) -> None:
        """Spawn a terminal running ``leap --resume --cli=… --tag=… --session=…``.

        Used by the GUI's "Add row from resume" flow: the dialog
        already picked + did the already-running check, and now we
        hand off to the CLI so the user can answer interactive
        prompts (cwd choice for Claude/Gemini/Cursor; nothing for
        Codex which finds sessions by UUID alone).

        When ``preferred_ide`` resolves to an IDE (VS Code / Cursor /
        JetBrains family) we thread ``recorded_cwd`` through as
        ``project_path``: the IDE opens that project (or focuses an
        existing window for it) and the new terminal lands in the
        project's basePath — so ``leap-resume.py``'s cwd check sees
        a match with the recorded cwd and skips the prompt.

        Mirrors :meth:`ActionsMenuMixin._move_session_to_ide`'s
        cold-start handling: we pre-open the IDE via
        ``subprocess.Popen(['open', '-a', <ide>, <project>])`` BEFORE
        calling ``open_terminal_with_command`` and pass
        ``project_already_open=True`` so JetBrains' own
        ``[ide_cmd, project_path]`` call is skipped — that call uses
        the Toolbox-generated CLI shim (``open -na`` internally) which
        is unreliable when the IDE is fully closed, and without our
        bootstrap the poll loop would spin for ten minutes before
        falling back to a plain terminal.  ``fallback_terminal`` is
        also threaded through so an unsupported recorded app (Kitty,
        Ghostty, generic 'JetBrains IDE') lands in the user's chosen
        default terminal instead of the absolute-last-resort
        Terminal.app.

        For plain terminals (iTerm2, Terminal.app, WezTerm, Warp,
        Kitty, Ghostty) we leave ``project_path=None`` — those openers
        ignore it anyway, and ``leap-resume.py`` will prompt the user
        with the "Original / Current" choice for cwd-bound CLIs.
        """
        leap_cmd = (
            f"leap --resume "
            f"--cli={shlex.quote(cli)} "
            f"--tag={shlex.quote(tag)} "
            f"--session={shlex.quote(session_id)}"
        )
        ide_target = is_ide_app(preferred_ide)
        # Pick the path we hand to the IDE: prefer the recorded project
        # root (git toplevel that the leap server captured into
        # ``<tag>.meta`` and the hook then snapshotted into the resume
        # record).  When missing (pre-feature record), derive via
        # ``git rev-parse`` from the recorded cwd — handles legacy
        # entries before this PR.  Fall back to the cwd itself when
        # git resolution fails too (no .git, dir gone, etc.).
        # This split (open IDE at project root, cd terminal to subdir)
        # mirrors Move-to-IDE — without it, JetBrains opens the deep
        # subdir as a separate project when that subdir happens to
        # have its own ``.idea/`` (e.g. ``tenant-manager/proto``).
        ide_open_path: Optional[str] = None
        if ide_target and recorded_project_path:
            ide_open_path = recorded_project_path
        elif ide_target and recorded_cwd:
            ide_open_path = _git_root_or(recorded_cwd, recorded_cwd)
        project_path = ide_open_path if ide_target else None
        fallback_terminal = self._w._prefs.get('default_terminal') or None

        # For JetBrains, resolve the concrete ``.app`` bundle so the
        # CLI subprocess (``[binary, project_path]`` and
        # ``ideScript``) runs from that exact install — NOT via the
        # Toolbox-generated shim (``open -na`` internally), which
        # is unreliable for cold-start.  Move-to-IDE has this for
        # free (user picked the ``.app`` via QFileDialog); Resume
        # has only the recorded IDE name to go on, so we glob for it.
        # ``find_jetbrains_app`` returns None for VS Code / Cursor —
        # that's fine: ``_open_vscode_terminal`` handles them via
        # AppleScript ``tell application … activate``.
        ide_app_path: Optional[str] = None
        if ide_target:
            ide_app_path = find_jetbrains_app(preferred_ide)

        # When targeting an IDE, prepend ``cd <recorded_cwd>`` so the
        # terminal lands at the exact subdir the session was last in
        # — JetBrains opens its terminals at ``getBasePath()`` (the
        # project root), which may be above the recorded cwd, so
        # without this ``cd`` the leap-resume.py cwd-check would see
        # a mismatch and prompt the user.  For VS Code the workspace
        # folder usually IS the recorded cwd so the ``cd`` is a no-op,
        # but adding it uniformly keeps the IDE path symmetric.  Plain
        # terminals (iTerm2 etc.) keep the existing flow — we *want*
        # leap-resume.py's Original/Current picker there.
        #
        # Separator is ``;`` (not ``&&``) so a failed ``cd`` (recorded
        # cwd deleted off disk) does NOT short-circuit ``leap`` — the
        # leap command still runs from whatever cwd the IDE landed in
        # (project root) and leap-resume.py's cwd-picker fires as the
        # safety net.  With ``&&`` the chain would abort on cd failure
        # and the user would be staring at a dead terminal.
        if ide_target and recorded_cwd:
            leap_cmd = f"cd {shlex.quote(recorded_cwd)} ; {leap_cmd}"

        def _spawn() -> bool:
            # VS Code / Cursor: pre-open the workspace via
            # LaunchServices so the editor follows the recorded
            # project — without this, an already-open VS Code window
            # on a different workspace stays focused and the new
            # leap terminal lands inside the WRONG file tree.  VS
            # Code (unlike JetBrains) correctly interprets a
            # directory arg to ``open -a`` as "open as workspace",
            # focusing an existing window if it has that folder
            # loaded or opening a new window otherwise.  Explicit
            # ``in ('VS Code', 'Cursor')`` check, NOT a negation of
            # ``ide_app_path`` — those two are the only IDEs that
            # treat the directory arg as a workspace.  JetBrains
            # mishandles it (empty duplicate window) and must go
            # through ``_open_jetbrains_terminal``'s pinned-binary
            # path even when ``find_jetbrains_app`` can't locate the
            # bundle (e.g., custom install dir).
            if preferred_ide in ('VS Code', 'Cursor') and project_path:
                vscode_bundle = (
                    'Visual Studio Code' if preferred_ide == 'VS Code'
                    else 'Cursor'
                )
                try:
                    subprocess.Popen(
                        ['open', '-a', vscode_bundle, project_path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError:
                    pass
            # JetBrains: ``_open_jetbrains_terminal`` issues the
            # project-open via ``[binary, project_path]`` (using the
            # ``ide_app_path``-pinned canonical install, NOT the
            # Toolbox shim).  The binary's CLI logic handles
            # cold/warm/different-project cases cleanly.
            return bool(open_terminal_with_command(
                leap_cmd,
                preferred_ide=preferred_ide,
                project_path=project_path,
                fallback_terminal=fallback_terminal,
                ide_app_path=ide_app_path,
            ))

        worker = BackgroundCallWorker(_spawn, self._w)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _open_leap_in_terminal(
        self, tag: str, preferred_ide: Optional[str], project_path: Optional[str],
    ) -> None:
        """Open a Leap server in a terminal at the given project path."""
        # Guard: if the project directory was deleted, ask the user instead of
        # crashing the IDE (e.g. JetBrains "Could not determine current working directory").
        if project_path and not Path(project_path).is_dir():
            logger.warning("Project path does not exist: %s", project_path)
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
        parts: list[str] = []
        if project_path:
            parts.append(f"cd {shlex.quote(project_path)}")
        parts.append(f"leap {shlex.quote(tag)}")
        cmd = " && ".join(parts)
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
        start_index: int = 0,
    ) -> tuple[Path, bool, list[str]]:
        """Find a project directory not used by a running Leap server.

        Scans ``<repos_dir>/<project_name>`` (index 0), then ``_1``, ``_2``,
        ... starting at ``start_index`` (used by the dirty-tree dialog to
        bump past a dir the user just rejected). Returns
        ``(project_dir, needs_clone, in_use_names)``. ``in_use_names`` lists
        directories skipped because they have an active Leap server.
        """
        active_paths = self._w._get_active_project_paths()
        in_use: list[str] = []

        for i in range(start_index, 100):
            name = project_name if i == 0 else f'{project_name}_{i}'
            candidate = repos_dir / name
            resolved = str(candidate.resolve())
            if not candidate.is_dir() or not _is_git_repo(candidate):
                return candidate, True, in_use  # Doesn't exist or not a valid git repo — needs clone
            if resolved not in active_paths:
                return candidate, False, in_use  # Exists and no Leap server using it
            in_use.append(name)
        # Fallback (shouldn't happen with 100 candidates)
        fallback = repos_dir / f'{project_name}_{100}'
        return fallback, True, in_use

    def _start_server_from_pr(
        self, tag: str, pinned: dict[str, Any], start_index: int = 0,
    ) -> None:
        """Start server for a PR-pinned row: find/clone project, checkout branch.

        ``start_index`` lets the dirty-tree dialog re-enter this flow at a
        higher index after the user opts to bump past a dirty managed
        clone (e.g. ``mwb-manifests`` dirty → start_index=1 picks the
        lowest free dir at or after ``mwb-manifests_1``).
        """
        # Heal an SSH-alias host_url here too: the dirty-tree dialog re-enters
        # this method directly (bypassing ``start_server``), so without this
        # the recursion's ``_build_clone_url`` would run on an un-healed alias.
        # Idempotent — no-op for proper DNS hostnames.
        self._heal_pinned_host_url(tag, pinned)

        repos_dir = self._w._prefs.get('repos_dir', DEFAULT_REPOS_DIR).strip() or DEFAULT_REPOS_DIR

        remote_project = pinned.get('remote_project_path', '')
        host_url = pinned.get('host_url', '')
        branch = pinned.get('branch', '')
        project_name = remote_project.rsplit('/', 1)[-1]
        # Safety: an empty project_name turns ``repos_dir / ''`` into
        # ``repos_dir`` itself, and the clone path's ``shutil.rmtree``
        # would wipe every managed clone the user owns.
        if not project_name:
            QMessageBox.warning(
                self._w, 'Invalid Remote',
                f"Cannot start a server for tag '{tag}': pinned "
                f"remote_project_path '{remote_project}' has no project name.",
            )
            self._cancel_start(tag)
            return
        rd = Path(repos_dir).expanduser()
        rd.mkdir(parents=True, exist_ok=True)

        project_dir, needs_clone, in_use_names = self._find_available_project_dir(
            rd, project_name, start_index=start_index,
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

        # Project exists and no Leap server using it — gate force-align on a
        # dirty-tree prompt so we don't silently discard user edits.
        if not branch:
            branch = detect_default_branch(str(project_dir))
        self._w._show_status(f"Checking '{project_dir.name}'...")
        self._dirty_check_then_align(tag, pinned, project_dir, branch)

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

    def _dirty_check_then_align(
        self, tag: str, pinned: dict[str, Any], project_dir: Path, branch: str,
    ) -> None:
        """Gate ``_server_force_align`` on a user prompt if local state would be lost.

        Managed clones in repos_dir get wiped on every sync — but if the
        user has uncommitted edits (working-tree changes), local commits
        ahead of ``origin/<branch>``, or a detached HEAD, we surface that
        before destroying them. Clean + up-to-date + on a branch → straight
        to align (no dialog). Anything to lose (or scan failure — treated
        as "unknown, be safe") → 3-way prompt (clone into next / discard
        & sync / cancel).

        The worker pre-fetches so the ahead-count is against current remote
        state. We do NOT defer to ``_server_force_align`` on pre-fetch
        failure — doing so would open a silent-destruction window if the
        pre-fetch failed transiently while ``_align``'s later retry
        succeeded (network recovered, auth re-resolved), and ``_align``
        ran ``reset --hard`` without any consent. Instead we surface the
        dialog with a synthetic "(could not fetch …)" entry so the user
        stays in control even when remote state is unknown.
        """
        auth_url = self._build_clone_url(
            pinned.get('host_url', ''), pinned.get('remote_project_path', ''),
            pinned.get('scm_type', ''),
        )
        refspec = f'+refs/heads/{branch}:refs/remotes/origin/{branch}'
        # state: (fetch_failed, dirty, ahead, detached_sha)
        state: list[tuple[bool, Optional[list[str]], Optional[int], Optional[str]]] = [
            (False, None, None, None),
        ]

        def _scan() -> None:
            cwd = str(project_dir)
            if auth_url:
                subprocess.run(
                    ['git', 'remote', 'set-url', 'origin', auth_url],
                    capture_output=True, text=True, cwd=cwd, timeout=5,
                )
            fetch_failed = False
            try:
                r = subprocess.run(
                    ['git', 'fetch', 'origin', refspec],
                    capture_output=True, text=True, cwd=cwd, timeout=30,
                )
                if r.returncode != 0:
                    fetch_failed = True
            except (subprocess.SubprocessError, OSError):
                fetch_failed = True
            dirty = _dirty_files(project_dir)
            # Skip ahead-count if fetch failed — the local origin/<branch>
            # ref is stale, so any count we report would be misleading.
            ahead = (
                _commits_ahead_of_origin(project_dir, branch)
                if not fetch_failed else None
            )
            detached = _detached_head_sha(project_dir)
            state[0] = (fetch_failed, dirty, ahead, detached)

        w = BackgroundCallWorker(_scan, self._w)
        w.finished.connect(lambda: self._on_dirty_check(
            tag, pinned, project_dir, branch, *state[0],
        ))
        w.finished.connect(w.deleteLater)
        w.start()

    def _on_dirty_check(
        self, tag: str, pinned: dict[str, Any], project_dir: Path,
        branch: str, fetch_failed: bool, dirty: Optional[list[str]],
        ahead: Optional[int], detached_sha: Optional[str],
    ) -> None:
        """Handle scan result: dialog if anything to lose, else straight to align."""
        # The scan runs in a worker, so by the time we get here the user
        # may have deleted the row. If so, the resurrection paths in
        # _server_finish would re-insert ``pinned`` into _pinned_sessions
        # for a tag the user explicitly dropped.
        if tag not in self._w._pinned_sessions:
            self._cancel_start(tag)
            return

        # Build the "what would be lost" list. Synthetic entries (detached-HEAD,
        # fetch-fail, ahead-count, scan failures) go FIRST so they're never
        # hidden behind the dialog's "…and N more" truncation when the working
        # tree has many dirty files.
        #
        # We do NOT silently defer to _server_force_align on fetch failure.
        # Doing so would open a silent-destruction window: pre-fetch could fail
        # transiently while _align's later retry succeeds (network recovered,
        # auth re-resolved, etc.), and _align would then run reset --hard
        # without any consent prompt. Surfacing the dialog keeps the user in
        # control even when we can't verify remote state.
        items: list[str] = []
        if detached_sha:
            # Most likely arrival: the user re-opens a row that was originally
            # added from a commit URL — the prior session left HEAD detached
            # at the pinned SHA. Without spelling this out, the "N commits
            # ahead" entry below reads as if the user has authored new commits.
            items.append(
                f'HEAD is detached at {detached_sha} — '
                f'sync will move it to origin/{branch}',
            )
        if fetch_failed:
            items.append(
                f'(could not fetch — local state may already diverge '
                f'from origin/{branch})',
            )
        elif ahead is None:
            # Only flag "couldn't check ahead" when fetch succeeded; otherwise
            # the fetch-fail entry already covers it.
            items.append('(could not check local commits vs origin)')
        elif ahead > 0:
            plural = '' if ahead == 1 else 's'
            items.append(
                f'{ahead} local commit{plural} ahead of origin/{branch}',
            )
        if dirty is None:
            items.append('(could not check working tree — proceeding may discard local changes)')
        else:
            items.extend(dirty)

        if not items:
            self._server_force_align(tag, pinned, project_dir, branch)
            return

        repos_dir = Path(
            self._w._prefs.get('repos_dir', DEFAULT_REPOS_DIR).strip()
            or DEFAULT_REPOS_DIR,
        ).expanduser()
        # ``.get`` rather than bracket-access: even though the entry-guard above
        # confirmed the tag is still pinned, another handler could in principle
        # drop this key in the worker→main-thread gap. Empty → cancel cleanly.
        project_name = pinned.get('remote_project_path', '').rsplit('/', 1)[-1]
        if not project_name:
            self._cancel_start(tag)
            return
        current_idx = _dir_index(project_name, project_dir.name)
        # max(_, 1) defends against an unexpected dir name (current_idx == -1):
        # we never want to re-suggest the same dir or fall back to index 0.
        next_start = max(current_idx + 1, 1)
        next_dir, _next_needs_clone, _ = self._find_available_project_dir(
            repos_dir, project_name, start_index=next_start,
        )
        action = self._ask_dirty_action(project_dir, items, next_dir)
        # Re-check: the row could have been deleted while the modal was open
        # (auto-refresh and other handlers still run during ``exec_()``).
        if tag not in self._w._pinned_sessions:
            self._cancel_start(tag)
            return
        if action == 'cancel':
            # Override the stale "Checking..." status the caller emitted,
            # so the user sees their cancel reflected immediately.
            self._w._show_status(f"Cancelled — '{project_dir.name}' left as-is")
            self._cancel_start(tag)
            return
        if action == 'discard':
            self._server_force_align(tag, pinned, project_dir, branch)
            return
        # 'next' — recurse from next_start. Don't clear pinned['project_path']:
        # if the recursive flow ends up cancelled too, today's saved path
        # is still the best fallback for the next Terminal click.
        self._start_server_from_pr(tag, pinned, start_index=next_start)

    def _ask_dirty_action(
        self, project_dir: Path, items: list[str], next_dir: Path,
    ) -> str:
        """3-way prompt for a managed clone with local state to lose.

        ``items`` mixes file paths (working-tree dirt) and synthetic strings
        like ``"3 local commits not in origin/<branch>"``. Returns
        'next'/'discard'/'cancel'. Shows the full on-disk path of both the
        affected dir and the proposed bump target so the user can locate
        them in Finder/IDE.

        Built as a plain ``QDialog`` rather than ``QMessageBox`` because the
        latter follows the platform's button layout (macOS pins
        ``RejectRole`` to the middle, between Destructive and Accept). The
        product wants Cancel pinned to the bottom-left.
        """
        shown = items[:5]
        more = len(items) - len(shown)
        items_text = '\n'.join(f'  • {f}' for f in shown)
        if more > 0:
            items_text += f'\n  …and {more} more'

        dialog = QDialog(self._w)
        dialog.setWindowTitle('Local Changes')

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(20, 16, 20, 12)
        outer.setSpacing(12)

        # Top row: warning icon + message body
        top = QHBoxLayout()
        top.setSpacing(14)
        icon_label = QLabel()
        icon_label.setPixmap(self._w.style().standardIcon(
            QStyle.SP_MessageBoxWarning,
        ).pixmap(48, 48))
        # Layout-level pin: QLabel.setAlignment governs pixmap-within-label,
        # not the QLabel's vertical position inside the row. Without this, a
        # 48px icon sits mid-message instead of at the top.
        top.addWidget(icon_label, alignment=Qt.AlignTop)

        msg = QLabel(
            f"The managed clone at\n"
            f"    {project_dir}\n"
            f"has local state that syncing would discard:\n\n"
            f"{items_text}\n\n"
            f"Syncing to the PR branch would destroy them."
        )
        # Force plain text so a path containing '<' isn't mis-parsed as HTML.
        msg.setTextFormat(Qt.PlainText)
        # Word-wrap helps long file paths in the bullet list (Qt can break at
        # '/'). Min-width keeps the dialog from collapsing; max-width prevents
        # an exotic 200-char path from blowing past the screen edge — at that
        # cap, an unbreakable token overflows the label, but the dialog stays
        # usable.
        msg.setWordWrap(True)
        msg.setMinimumWidth(480)
        msg.setMaximumWidth(720)
        top.addWidget(msg, stretch=1)
        outer.addLayout(top)

        # Button row: Cancel left, Discard + Clone right.
        btn_row = QHBoxLayout()
        btn_cancel = QPushButton('Cancel')
        btn_discard = QPushButton('Discard && sync')
        btn_next = QPushButton(f'Clone into {next_dir.name}')
        btn_next.setToolTip(str(next_dir))
        btn_next.setDefault(True)
        btn_next.setAutoDefault(True)
        # Critical: Qt's per-button autoDefault means a focused button consumes
        # Enter — so without this, tabbing onto Discard and pressing Enter would
        # destroy local edits. Force Enter from any focus to fall through to the
        # safe default (Clone into next). Cancel keeps autoDefault on — Enter
        # on a focused Cancel cancelling is both expected and safe.
        btn_discard.setAutoDefault(False)

        btn_row.addWidget(btn_cancel)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_discard)
        btn_row.addWidget(btn_next)
        outer.addLayout(btn_row)

        # ``result`` is set by whichever button is clicked; defaults to
        # 'cancel' so Esc / close-window / unexpected dismissal map to it.
        result = ['cancel']

        def _pick(value: str) -> None:
            result[0] = value
            dialog.accept()

        btn_cancel.clicked.connect(lambda: _pick('cancel'))
        btn_discard.clicked.connect(lambda: _pick('discard'))
        btn_next.clicked.connect(lambda: _pick('next'))
        # Esc / X / unexpected dismissal: QDialog.reject() runs but no
        # button-click fires, so ``result`` stays at the 'cancel' default.

        dialog.exec_()
        return result[0]

    def _server_force_align(
        self, tag: str, pinned: dict[str, Any], project_dir: Path, branch: str,
    ) -> None:
        """Fetch remote branch, force-checkout and hard-reset to origin.

        These are managed clones in repos_dir, not user workspaces — local
        changes are always discarded in favour of the remote state.
        Callers entering this method through ``_dirty_check_then_align``
        have already prompted the user for consent.
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
                # 2a. Best-effort: abort any in-progress merge/rebase/cherry-
                #     pick/revert. Each call no-ops (non-zero exit, swallowed)
                #     if not in that state. Without this, a dirty tree caused
                #     by an unfinished merge would survive the reset and the
                #     subsequent checkout would fail with "you need to resolve
                #     your current index first".
                for abort_cmd in (
                    ['git', 'merge', '--abort'],
                    ['git', 'rebase', '--abort'],
                    ['git', 'cherry-pick', '--abort'],
                    ['git', 'revert', '--abort'],
                ):
                    subprocess.run(
                        abort_cmd, capture_output=True, text=True,
                        cwd=cwd, timeout=10,
                    )
                # 2b. Pre-clean the current branch so the upcoming checkout
                #     can't be blocked by dirty tracked files or untracked-vs-
                #     target-tracked overlaps. Best-effort: the user-consent
                #     gate is the upstream _dirty_check_then_align dialog; if
                #     these fail for some unusual reason the real source of
                #     truth is the unconditional reset to origin/<branch> below.
                subprocess.run(
                    ['git', 'reset', '--hard', 'HEAD'],
                    capture_output=True, text=True, cwd=cwd, timeout=10,
                )
                subprocess.run(
                    ['git', 'clean', '-fd'],
                    capture_output=True, text=True, cwd=cwd, timeout=10,
                )
                # 3. Checkout branch (create tracking branch if needed)
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
                # 4. Hard-reset to remote (unconditional)
                subprocess.run(
                    ['git', 'reset', '--hard', f'origin/{branch}'],
                    check=True, capture_output=True, text=True,
                    cwd=cwd, timeout=10,
                )
                # 5. Remove untracked files left behind by the target branch
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
                    f"Leap will start on the last local state of '{branch}' "
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
                    f"Start Leap without syncing?",
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
        """Final step: update pinned data with local path and open Leap."""
        self._w._show_status(f"Opening Leap '{tag}' in {project_dir.name}...")

        # Save local project path for future use
        pinned['project_path'] = str(project_dir)
        self._w._pinned_sessions[tag] = pinned
        save_pinned_sessions(self._w._pinned_sessions)

        preferred_ide = self._w._prefs.get('default_terminal')
        self._open_leap_in_terminal(tag, preferred_ide, str(project_dir))
