"""Per-row actions menus — Git Changes (server), Path actions (Open Terminal / IDE)."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tempfile
from typing import TYPE_CHECKING, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
    QMenu, QMessageBox, QPushButton, QVBoxLayout,
)
from PyQt5.QtGui import QCursor

from leap.cli_providers.registry import DEFAULT_PROVIDER
from leap.monitor.cursor_gui_scan import CURSOR_GUI_TAG_PREFIX
from leap.monitor.dialogs.branch_picker_dialog import BranchPickerDialog
from leap.monitor.dialogs.git_changes_dialog import CommitListDialog
from leap.monitor.navigation import (
    detect_supported_ide_for_move, find_terminal_with_title,
    is_jetbrains_app, is_jetbrains_project_open, open_terminal_with_command,
)
from leap.monitor.scm_polling import BackgroundCallWorker
from leap.monitor.session_manager import load_session_metadata
from leap.utils.constants import STORAGE_DIR
from leap.utils.resume_store import load_tag_rows

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
        """Show the path actions menu (Open in Terminal, Open in IDE)."""
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

        ide_action = menu.addAction('Open in IDE')
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
            # Cursor editor Agent-tab rows (read-only overlay, not in
            # self.sessions) carry their project folder directly.
            for r in getattr(self, '_cursor_gui_rows', []):
                if r['tag'] == tag and r.get('project_path'):
                    path = r['project_path']
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
        for r in getattr(self, '_cursor_gui_rows', []):
            if r['tag'] == tag:
                project = r.get('project', '')
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
        """Open a file dialog to pick an .app, then open the project with it.

        For JetBrains-family and VS Code .apps, also offer to *move* the
        currently-running leap session into the IDE's integrated
        terminal — the existing server is closed (same close path as
        the row's X button) and a new ``leap <tag>`` is launched in the
        IDE's terminal, with ``LEAP_RESUME_*`` env vars when a Claude
        transcript exists for the tag (otherwise a fresh start).
        """
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

        if tag.startswith(CURSOR_GUI_TAG_PREFIX):
            # Cursor editor Agent-tab rows aren't Leap sessions - there's
            # nothing to "move", so just open the folder in the chosen app.
            self._maybe_set_jetbrains_alias(tag, path, project_path)
            self._just_open_ide(tag, path, project_path)
            return

        preferred_ide = detect_supported_ide_for_move(path)
        if preferred_ide is None:
            # Sublime, Xcode, Arduino, Cursor, RubyMine/CLion/etc. —
            # no integrated terminal we drive from leap, so fall back
            # to plain "open the .app". RubyMine/CLion are still JetBrains,
            # so the alias prompt below self-gates and reaches them too.
            self._maybe_set_jetbrains_alias(tag, path, project_path)
            self._just_open_ide(tag, path, project_path)
            return

        # Short-circuit: if the session is already running in this
        # exact IDE at this exact path, there's nothing to "move" —
        # skip the popup entirely and just focus the existing leap
        # terminal tab (same UX as clicking the row's Terminal button).
        metadata = load_session_metadata(tag) or {}
        if (metadata.get('ide') == preferred_ide
                and metadata.get('project_path') == project_path):
            self._focus_existing_session_tab(
                tag, preferred_ide, project_path, metadata.get('pid')
            )
            return

        app_label = path.rsplit('/', 1)[-1].removesuffix('.app')
        choice = self._ask_move_to_ide_choice(tag, app_label)
        if choice == 'cancel':
            return
        # Offer a JetBrains tab alias once the user has committed to
        # opening (cancelling above skips this). Self-gates to JetBrains.
        self._maybe_set_jetbrains_alias(tag, path, project_path)
        if choice == 'only':
            self._just_open_ide(tag, path, project_path)
            return
        # 'move'
        self._move_session_to_ide(tag, path, project_path, preferred_ide)

    def _maybe_set_jetbrains_alias(
        self, tag: str, app_path: str, project_path: Optional[str],
    ) -> None:
        """Offer to set the project's JetBrains tab name before opening.

        JetBrains reads ``<project>/.idea/.name`` on project load and shows
        its contents as the display name in the IDE's project tab. When the
        chosen ``.app`` is a JetBrains IDE and the session has a real project
        directory, prompt for an alias (the field always defaults to the
        *tag*) and write it to ``.idea/.name`` before launch so the tab is
        labelled.

        No-op for non-JetBrains apps or sessions without a project directory.
        Three outcomes from the prompt:

        * **OK + a value** -> write it as the alias.
        * **OK + blank** -> keep the current name unchanged (the previous
          alias if a ``.name`` already exists, otherwise the repo name).
        * **"Use project's name"** (the relabelled Cancel button, or Escape)
          -> delete any ``.name`` so the IDE falls back to the repo
          (directory) name.

        The prompt's message spells out what blank vs. "Use project's name"
        will leave you with.

        JetBrains reads ``.idea/.name`` only when it *loads* a project, so if
        the IDE already has it open the prompt is skipped (relabelling a live
        window is futile) - see ``is_jetbrains_project_open``, which covers the
        whole JetBrains family and is best-effort (a probe that can't confirm
        falls through to prompting).
        """
        if not is_jetbrains_app(app_path):
            return
        if not project_path or not os.path.isdir(project_path):
            return

        # If the IDE already has this project open, relabelling is futile -
        # JetBrains won't re-read .idea/.name without a reload and will
        # overwrite our value on its next save. Skip the prompt entirely.
        if is_jetbrains_project_open(app_path, project_path):
            logger.info(
                "Project already open in %s; skipping IDE tab alias prompt",
                app_path,
            )
            return

        idea_dir = os.path.join(project_path, '.idea')
        name_file = os.path.join(idea_dir, '.name')

        existing = ''
        try:
            with open(name_file, 'r') as f:
                existing = f.read().strip()
        except OSError:
            existing = ''

        repo_name = os.path.basename(os.path.normpath(project_path))
        if existing:
            label = (
                'Name to show on the project tab in the IDE.\n'
                f"Leave blank to keep '{existing}' (previous alias); "
                f"use the project's name to revert to '{repo_name}'."
            )
        else:
            label = (
                'Name to show on the project tab in the IDE.\n'
                f"Leave blank or use the project's name to keep '{repo_name}'."
            )

        dlg = QInputDialog(self)
        dlg.setWindowTitle('IDE tab alias')
        dlg.setLabelText(label)
        dlg.setTextValue(tag)
        dlg.setCancelButtonText("Use project's name")
        accepted = dlg.exec_() == QDialog.Accepted
        alias = dlg.textValue().strip()

        if not accepted:
            # "Use project's name" (or Escape) -> drop any alias so the IDE
            # falls back to the repo (directory) name.
            try:
                os.remove(name_file)
                logger.info("Cleared IDE tab alias: %s", name_file)
                self._show_status(f"Using project name: {repo_name}")
            except FileNotFoundError:
                pass
            except OSError as e:
                self._show_status(f"Couldn't clear IDE tab alias: {e}")
            return

        if not alias:
            return  # Blank + OK -> keep the current name (no change).

        try:
            os.makedirs(idea_dir, exist_ok=True)
            with open(name_file, 'w') as f:
                f.write(alias)
            logger.info("Set IDE tab alias '%s' -> %s", alias, name_file)
            self._show_status(f"Set IDE tab alias: {name_file}")
        except OSError as e:
            self._show_status(f"Couldn't set IDE tab alias: {e}")

    def _focus_existing_session_tab(
        self, tag: str, preferred_ide: str, project_path: str,
        server_pid: Optional[int] = None,
    ) -> None:
        """Bring the leap terminal tab for *tag* to the front in *preferred_ide*.

        Used when the user clicks "Open in IDE" but the session is
        already running in that exact IDE at that exact path — same
        navigation as the row's Terminal button. *server_pid* enables the
        JetBrains rename-proof PID fallback (this is always a server tab).
        """
        title = f'lps {tag}'
        _ide, _proj, _title, _pid = preferred_ide, project_path, title, server_pid

        def _focus() -> None:
            find_terminal_with_title(_title, _ide, _proj, _title, _pid)

        worker = BackgroundCallWorker(_focus, self)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self._show_status(
            f"Session '{tag}' is already in {preferred_ide} - "
            f"focusing its terminal tab"
        )

    def _ask_move_to_ide_choice(self, tag: str, app_label: str) -> str:
        """Present the 3-way Move-to-IDE prompt and return the choice.

        Returns one of ``'cancel'``, ``'only'``, ``'move'``.

        Uses a custom :class:`QDialog` instead of ``QMessageBox`` so we
        can lay buttons out left-to-right in our own order (QMessageBox
        on macOS forces the platform's role-driven ordering and would
        put Cancel between the two action buttons, which we want to
        avoid).  Layout is::

            [ Cancel ]    [ Only Open IDE ]    [ Open IDE + Move session ]

        Cancel sits on the far left as a clear opt-out; the rightmost
        button is the default action (Mac convention).  Esc routes to
        Cancel; Enter triggers the default (the full Move).
        """
        dlg = QDialog(self)
        dlg.setWindowTitle('Move session to IDE')

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(20, 20, 20, 16)
        outer.setSpacing(12)

        title = QLabel(f"<b>Move session '{tag}' to {app_label}?</b>")
        title.setWordWrap(True)
        title.setTextFormat(Qt.RichText)
        outer.addWidget(title)

        info = QLabel(
            f"<b>Open IDE + Move session</b> &mdash; close the current "
            f"leap server and resume this session inside {app_label}'s "
            f"integrated terminal.<br><br>"
            f"<b>Only Open IDE</b> &mdash; open {app_label} on the "
            f"project; the running session is left alone.<br><br>"
            f"<b>Cancel</b> &mdash; do nothing."
        )
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        outer.addWidget(info)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        cancel_btn = QPushButton('Cancel')
        only_btn = QPushButton('Only Open IDE')
        move_btn = QPushButton('Open IDE + Move session')
        for btn in (cancel_btn, only_btn, move_btn):
            btn.setMinimumWidth(120)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(only_btn)
        btn_row.addWidget(move_btn)
        outer.addLayout(btn_row)

        # Mutable holder so the lambdas can return a string out of the
        # exec_() loop without a class.
        result: dict[str, str] = {'choice': 'cancel'}

        def _pick(value: str) -> None:
            result['choice'] = value
            dlg.accept()

        cancel_btn.clicked.connect(dlg.reject)
        only_btn.clicked.connect(lambda: _pick('only'))
        move_btn.clicked.connect(lambda: _pick('move'))

        # Enter → default (Move); Esc → reject (cancel).
        move_btn.setDefault(True)
        move_btn.setAutoDefault(True)
        cancel_btn.setAutoDefault(False)
        only_btn.setAutoDefault(False)

        dlg.exec_()  # Cancel (or window-X / Esc) reject() → result stays 'cancel'
        return result['choice']

    def _just_open_ide(self, tag: str, app_path: str, project_path: str) -> None:
        """Plain "open the .app on the project" — pre-existing behaviour."""
        _app = app_path
        _proj = project_path
        worker = BackgroundCallWorker(
            lambda: subprocess.Popen(
                ['open', '-a', _app, _proj],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ),
            self,
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self._show_status(f"Opening {app_path.rsplit('/', 1)[-1]} for '{tag}'")

    def _move_session_to_ide(
        self, tag: str, app_path: str, project_path: str, preferred_ide: str,
    ) -> None:
        """Close the running leap server for *tag* and resume it in the IDE.

        Steps:
          1. Look up the session's CLI provider and most recent
             ``session_id`` from ``.storage/cli_sessions/<cli>/<tag>.json``.
             A missing/empty record means no transcript on disk —
             handled by falling through to a fresh ``leap <tag>``.
          2. Close the running leap server via the same path the row's
             X button uses (``_close_server`` with ``_from_delete=True``
             so it doesn't pop another confirmation), chained with
             ``on_done`` so we run the IDE launch only after shutdown
             completes.
          3. From the on_done callback: ``open -a`` the bundle the
             user picked (reliable LaunchServices open — important
             for JetBrains Toolbox installs whose CLI shims use
             ``open -na`` and don't reliably activate), then call
             ``open_terminal_with_command`` with
             ``project_already_open=True`` so ``_open_jetbrains_terminal``
             skips its own ``[ide_cmd, project_path]`` open call.
             This gives us exactly *one* open request in flight, which
             prevents both the duplicate-window bug (two competing
             opens) and the Toolbox-CLI-doesn't-activate bug (no
             usable open at all).  The poll loop then waits for the
             IDE's scripting engine and creates the terminal tab.

        ``preferred_ide`` is the canonical key
        ``open_terminal_with_command`` routes on (e.g. ``'PyCharm'``,
        ``'IntelliJ IDEA'``, ``'VS Code'``) — supplied by
        :func:`detect_supported_ide_for_move`.

        Note: callers should consult the row's session metadata
        first.  When the session is *already* running in
        ``preferred_ide`` at ``project_path`` they should skip the
        Move popup and call :meth:`_focus_existing_session_tab`
        instead.  ``_open_with_ide`` does this short-circuit before
        reaching this method.
        """
        # Resolve cli + server_pid from the active row.
        session = next((s for s in self.sessions if s.get('tag') == tag), None)
        cli = (session or {}).get('cli_provider') or DEFAULT_PROVIDER
        server_pid = (session or {}).get('server_pid')

        # Most recent recorded session_id + cwd for this (cli, tag) — if any.
        # ``load_tag_rows`` already filters stale entries (transcript
        # file gone), so ``session_id is None`` means "nothing to
        # resume → start fresh under the same tag".  We also grab the
        # recorded cwd so we can ``cd`` to the actual subdir the
        # session was in — JetBrains opens its integrated terminal at
        # ``getBasePath()`` (the project root), which is typically a
        # parent of the recorded cwd, so without the ``cd`` the
        # leap-resume.py cwd-check would mismatch and pop a needless
        # Original/Current picker.
        session_id: Optional[str] = None
        recorded_cwd: Optional[str] = None
        try:
            for row in load_tag_rows(STORAGE_DIR):
                if row.tag == tag and row.cli == cli and row.sessions:
                    session_id = row.sessions[0].session_id
                    recorded_cwd = row.sessions[0].cwd or None
                    break
        except Exception:  # pragma: no cover - best-effort lookup
            session_id = None

        _app, _proj = app_path, project_path
        _tag, _cli, _sid = tag, cli, session_id
        # Prefer the recorded cwd (deeper, where the session was last
        # active) over the project root for the ``cd``.  Falls back to
        # the project root when no session has been recorded yet
        # (fresh start) or the resume-store lookup failed.
        _cd_target = recorded_cwd or project_path
        _preferred_ide = preferred_ide
        _fallback_terminal = self._prefs.get('default_terminal', '') or None

        def _after_close() -> None:
            # 1) Open the .app on the project via LaunchServices.  This
            # uses the *exact* ``.app`` path the user picked, which is
            # critical for JetBrains Toolbox installs: their generated
            # CLI shims (`~/.../Toolbox/scripts/<ide>`) use ``open -na``
            # internally — that forces a *new* instance and the
            # subsequent activation is unreliable; on some setups
            # nothing visible happens at all.  ``open -a <full path>``
            # on the bundle bypasses that and reliably focuses the
            # right window.
            #
            # The duplicate-window bug that originally motivated
            # removing this line is now prevented by passing
            # ``project_already_open=True`` to
            # ``open_terminal_with_command`` below, which makes
            # ``_open_jetbrains_terminal`` skip its own
            # ``[ide_cmd, project_path]`` call — so we have exactly
            # one open request in flight, not two competing ones.
            try:
                subprocess.Popen(
                    ['open', '-a', _app, _proj],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as e:
                logger.warning("Could not open %s for '%s': %s", _app, _tag, e)
                self._show_status(f"Could not open {_app}: {e}")
                return

            # 2) Build the leap command (resume vs fresh) and run it
            # inside the IDE's integrated terminal.  Mirrors
            # ``ServerLauncher._open_leap_in_terminal`` to stay
            # consistent with the From-Resume hand-off path.
            # ``_cd_target`` is the deeper recorded cwd when available
            # (so the terminal lands where the session was actually
            # running, not at the project root JetBrains defaults to)
            # — falls back to the project root for fresh starts.
            leap_cmd = f"leap {shlex.quote(_tag)}"
            if _sid:
                leap_cmd = (
                    f"LEAP_RESUME_SESSION_ID={shlex.quote(_sid)} "
                    f"LEAP_RESUME_CLI={shlex.quote(_cli)} "
                    f"LEAP_CLI={shlex.quote(_cli)} "
                    f"{leap_cmd}"
                )
            # ``;`` (not ``&&``) so a failed ``cd`` (recorded cwd
            # deleted off disk) doesn't short-circuit leap — without
            # this guard the user would land in a dead terminal with
            # ``cd: no such file or directory`` and no leap server.
            # With ``;``, leap runs from whatever cwd the IDE put us
            # in (project root) and leap-resume.py's cwd-picker fires
            # as the safety net.
            if _cd_target:
                cmd = f"cd {shlex.quote(_cd_target)} ; {leap_cmd}"
            else:
                cmd = leap_cmd

            label = _app.rsplit('/', 1)[-1].removesuffix('.app')
            verb = 'Resuming' if _sid else 'Starting'
            self._show_status(f"{verb} '{_tag}' in {label}")

            # Track which app the helper actually opened in, so we can
            # show an accurate completion message instead of leaving
            # "Resuming '<tag>' in PyCharm" stuck on screen for minutes
            # — possibly lying outright if the helper falls back.
            result_holder: dict = {'ok': False, 'used': None}

            # The poll loop in ``_open_jetbrains_terminal`` calls this
            # every iteration; once the tag leaves ``_moving_tags`` (X
            # button, drag-remove, or the 12-min safety net) we want
            # the worker to bail instead of spinning for another 10
            # minutes against an IDE the user has visibly given up on.
            def _should_cancel() -> bool:
                return _tag not in self._moving_tags

            def _do_open() -> None:
                outcome: dict = {}
                ok = bool(open_terminal_with_command(
                    cmd, preferred_ide=_preferred_ide, project_path=_proj,
                    fallback_terminal=_fallback_terminal,
                    outcome=outcome,
                    should_cancel=_should_cancel,
                    project_already_open=True,
                    # Pin the JetBrains CLI to *this* .app's binary —
                    # the friend's "opens twice" report could resurface
                    # if multiple PyCharm installs are on PATH and
                    # ``ideScript`` lands in a different instance than
                    # the one ``open -a`` activated.
                    ide_app_path=_app,
                ))
                result_holder['ok'] = ok
                result_holder['used'] = outcome.get('used')

            launch_worker = BackgroundCallWorker(_do_open, self)

            # JetBrains cold-start can run minutes — emit a periodic
            # "still waiting" tick so the status bar doesn't look frozen
            # behind the initial "Resuming…" line.  ``done_flag`` (a
            # mutable single-element list, *not* ``launch_worker``) is
            # what the tick checks: a ``timeout`` event already queued
            # when ``progress_timer.stop()`` runs in ``_on_done`` will
            # still fire, and (a) we don't want it to overwrite the
            # final status with a stale "Waiting…", and (b) probing
            # ``launch_worker`` after its ``deleteLater`` has resolved
            # would raise a "wrapped C++ object deleted" RuntimeError.
            progress_timer = QTimer(self)
            progress_timer.setInterval(30_000)
            done_flag = [False]

            def _progress_tick() -> None:
                if not done_flag[0]:
                    self._show_status(
                        f"Waiting for {label} to be ready for "
                        f"'{_tag}'…"
                    )

            progress_timer.timeout.connect(_progress_tick)
            progress_timer.start()

            def _on_done() -> None:
                done_flag[0] = True
                progress_timer.stop()
                progress_timer.deleteLater()
                used = result_holder['used']
                if not result_holder['ok']:
                    self._show_status(
                        f"Could not open '{_tag}' in {label}"
                    )
                elif used == _preferred_ide:
                    self._show_status(
                        f"'{_tag}' opened in {label}"
                    )
                else:
                    self._show_status(
                        f"'{_tag}' opened in {used} "
                        f"({label} unavailable)"
                    )

            launch_worker.finished.connect(_on_done)
            launch_worker.finished.connect(launch_worker.deleteLater)
            launch_worker.start()

        # Bridge the dead-row gap: between ``_close_server`` finishing
        # and the IDE-launched ``leap <tag>`` registering a new active
        # session, ``_merge_sessions`` would see "no live server, no PR
        # tracking" and wipe the row's pin — losing color, alias, and
        # ``row_order`` position; the new server would then re-pin as a
        # fresh row at the bottom of the table.
        #
        # We use ``_moving_tags`` (not ``_starting_tags``) because the
        # latter is auto-cleared on every refresh for any tag whose
        # server is currently alive — and at this exact moment the old
        # server *is* still alive, so a 1 s auto-refresh tick during
        # the close would clear it before close completes.  The
        # ``_moving_tags`` set has no auto-clear; we explicitly drop
        # the tag after a safety-net timeout.  When the new server
        # registers as active, ``_merge_sessions`` merges normally;
        # ``_moving_tags`` becomes a no-op for that tag (it only
        # matters in the dead-row branch).
        #
        # The timeout must comfortably exceed
        # ``_open_jetbrains_terminal``'s poll budget (10 min) plus
        # the post-poll handoff to ``leap <tag>`` and the new server
        # registering.  Earlier 60 s value was tuned for a 10 s
        # ideScript timeout; raising the poll budget without raising
        # this would let a slow cold-start tick past the safety net
        # and silently wipe the row's pin (alias, colour,
        # ``row_order``) before the new server registers — at which
        # point the new server re-pins as a fresh row at the bottom.
        self._moving_tags.add(_tag)
        # Record the pid we're moving away from so _merge_sessions can clear
        # the guard the moment the relaunched server (a different pid) is up,
        # instead of leaving it armed for the whole 12-min safety window
        # (which made a quick close-in-IDE flip the row to a stuck
        # "Starting..." dead row).
        self._moving_old_pid[_tag] = server_pid

        def _clear_move_guard(t: str = _tag) -> None:
            self._moving_tags.discard(t)
            self._moving_old_pid.pop(t, None)

        QTimer.singleShot(720_000, _clear_move_guard)  # 12 min backstop
        # Close the server (same path as the X button), then run our
        # follow-up.  ``_from_delete=True`` skips _close_server's own
        # confirmation popups (we already asked "Move?").
        self._close_server(_tag, server_pid, _from_delete=True, on_done=_after_close)

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
