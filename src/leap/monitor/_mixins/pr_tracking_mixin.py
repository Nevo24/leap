"""PR tracking, SCM polling, thread sending, and add-row methods."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtGui import QCursor, QDesktopServices
from PyQt5.QtWidgets import QApplication, QInputDialog, QMenu, QMessageBox

from leap.cli_providers.registry import get_display_name
from leap.utils.constants import STORAGE_DIR, is_valid_tag
from leap.utils.resume_store import load_raw_tag_rows
from leap.monitor.dialogs.add_local_dialog import AddLocalDialog
from leap.monitor.dialogs.resume_session_dialog import ResumeSessionDialog
from leap.monitor.pr_tracking.base import PRState, PRStatus
from leap.monitor.pr_tracking.config import (
    load_bitbucket_config, load_github_config, load_gitlab_config,
    remove_pinned_session_tag, update_pinned_session_field,
    write_pinned_session_entry,
)
from leap.monitor.pr_tracking.git_utils import (
    ParsedProjectUrl, SCMType, detect_default_branch, get_git_remote_info,
    parse_pr_url, parse_project_url, refine_scm_type,
)
from leap.monitor.scm_polling import (
    BackgroundCallWorker, CollectThreadsWorker, SCMOneShotWorker,
    SCMPollerWorker, SendThreadsCombinedWorker, SendThreadsWorker,
)

if TYPE_CHECKING:
    from leap.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object

logger = logging.getLogger(__name__)


class PRTrackingMixin(_Base):
    """Methods for PR tracking, SCM polling, thread sending, and add-row."""

    def _auto_track_pr_pinned(self) -> None:
        """Auto-reconnect PR tracking for sessions that were tracked last time."""
        if not self._scm_providers:
            return
        # Skip tags already being checked — protects against duplicate
        # workers when this fires twice in quick succession (startup
        # auto-reconnect + SCM-dialog Save).
        for tag, pin in self._pinned_sessions.items():
            if (pin.get('pr_tracked')
                    and tag not in self._tracked_tags
                    and tag not in self._checking_tags):
                self._start_tracking(tag, _silent=True)
        # Merged/closed PR-pinned rows aren't in _tracked_tags, so the
        # _start_tracking calls above won't start the poll timer for a
        # session list that's *only* revisit rows.  Sync it explicitly so
        # those rows still get re-checked for a re-opened PR.
        self._sync_scm_poll_timer()

    def _start_tracking(self, tag: str, _silent: bool = False) -> None:
        """Start PR tracking for a session via a background one-shot check."""
        # Find the session data for this tag
        session = next((s for s in self.sessions if s['tag'] == tag), None)
        if not session:
            return

        provider = self._get_provider_for_session(session)
        if not provider:
            if _silent:
                self._show_status(f"Auto-reconnect skipped for '{tag}': no matching SCM provider")
                return
            if not self._scm_providers:
                QMessageBox.information(
                    self, 'No SCM Connected',
                    'Connect to GitLab, GitHub or Bitbucket first using the '
                    'buttons at the bottom.'
                )
            else:
                QMessageBox.information(
                    self, 'No Provider Match',
                    'No configured SCM provider matches this project\'s git remote.\n'
                    'Connect the appropriate provider (GitLab/GitHub/Bitbucket) first.'
                )
            return

        # Resolve project path and branch for the SCM query.
        # PR-pinned rows have remote_project_path/branch stored directly;
        # active sessions resolve from the local git remote.
        # Prefer pr_branch (pinned PR branch) over the live branch.
        remote_project = session.get('remote_project_path')
        branch = session.get('pr_branch') or session.get('branch')

        if remote_project and branch and branch != 'N/A':
            # Use pinned PR data directly (no local repo needed)
            scm_project_path = remote_project
            scm_branch = branch
            # Store context for enriching pinned session on result
            self._pending_tracking_context[tag] = {
                'remote_project_path': remote_project,
                'host_url': session.get('host_url', ''),
                'scm_type': session.get('scm_type', ''),
                'branch': scm_branch,
            }
        elif remote_project and (not branch or branch == 'N/A'):
            # Project-URL row with no specific branch — detect from local repo
            project_path = session.get('project_path')
            if project_path:
                scm_branch = detect_default_branch(project_path)
            else:
                if not _silent:
                    QMessageBox.information(
                        self, 'No PR Found',
                        'No branch info and no local project path to detect it from.',
                    )
                return
            scm_project_path = remote_project
            self._pending_tracking_context[tag] = {
                'remote_project_path': remote_project,
                'host_url': session.get('host_url', ''),
                'scm_type': session.get('scm_type', ''),
                'branch': scm_branch,
            }
        else:
            # Resolve from local git repo
            project_path = session.get('project_path')
            if not project_path:
                if not _silent:
                    QMessageBox.information(
                        self, 'No PR Found', 'No project path for this session.'
                    )
                return

            remote_info = get_git_remote_info(project_path)
            if not remote_info:
                if not _silent:
                    QMessageBox.information(
                        self, 'No PR Found', 'Could not determine Git remote info.'
                    )
                return
            scm_project_path = remote_info.project_path
            scm_branch = remote_info.branch
            scm_type = refine_scm_type(remote_info.host_url, remote_info.scm_type)
            # Store context for enriching pinned session on result
            self._pending_tracking_context[tag] = {
                'remote_project_path': scm_project_path,
                'host_url': remote_info.host_url,
                'scm_type': scm_type.value,
                'branch': scm_branch,
            }

        # Show "Checking..." while the API call runs in the background
        if _silent:
            self._silent_tracking_tags.add(tag)
        self._show_status(f"Checking PR for '{tag}'...")
        self._checking_tags.add(tag)
        self._set_busy(True)
        self._update_table()

        # Run the API call in a background thread
        worker = SCMOneShotWorker(self)
        worker.configure(provider, tag, scm_project_path, scm_branch)
        worker.result_ready.connect(self._on_tracking_result)
        worker.error.connect(self._on_tracking_error)
        worker.finished.connect(self._on_oneshot_cleanup)
        worker.finished.connect(worker.deleteLater)
        self._scm_oneshot_worker = worker
        worker.start()

    def _on_oneshot_cleanup(self) -> None:
        """Clear the oneshot worker reference after it finishes."""
        worker = self.sender()
        if self._scm_oneshot_worker is worker:
            self._scm_oneshot_worker = None

    def _stop_tracking(self, tag: str, _skip_prompt: bool = False,
                       _skip_ui: bool = False) -> None:
        """Stop PR tracking for a session.

        Args:
            tag: Session tag.
            _skip_prompt: If True, skip the confirmation prompt for dead rows
                (used when called from _remove_pinned_session which has its own).
            _skip_ui: If True, skip the trailing ``_update_table`` /
                ``_update_dock_badge`` calls — caller will refresh itself.
                Avoids a double-render when the caller (``_remove_pinned_session``)
                still has more state changes to make before the final render.
        """
        # If server is dead AND the row has no remaining PR data to
        # display, stopping tracking will remove the row.  Warn so the
        # user can confirm.  Rows that still have pinned PR Branch
        # data (``remote_project_path`` + ``branch``) survive via the
        # PR Branch keeper — no warning needed, no row removed.
        if not _skip_prompt:
            session = next((s for s in self.sessions if s['tag'] == tag), None)
            pin = self._pinned_sessions.get(tag, {})
            has_pr_branch_data = bool(
                pin.get('remote_project_path') and pin.get('branch'))
            if (session and session.get('server_pid') is None
                    and not has_pr_branch_data):
                reply = QMessageBox.question(
                    self, 'Stop PR Tracking',
                    f"The server for '{tag}' is not running.\n"
                    f"Stopping PR tracking will remove this row.\n\nContinue?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return

        if tag in self._tracked_tags:
            self._show_status(f"Stopped PR tracking for '{tag}'")
        self._tracked_tags.discard(tag)
        self._checking_tags.discard(tag)
        self._silent_tracking_tags.discard(tag)
        self._pr_statuses.pop(tag, None)
        self._pr_widgets.pop(tag, None)
        self._pr_approval_widgets.pop(tag, None)
        self._pending_tracking_context.pop(tag, None)
        self._pr_changed_at.pop(tag, None)
        self._dismissed_pr_new_status.discard(tag)
        self._dock_badge.discard_tag(tag)

        # Persist tracking-off so auto-reconnect won't re-track on next startup
        pin = self._pinned_sessions.get(tag)
        if pin and pin.get('pr_tracked'):
            pin['pr_tracked'] = False
            update_pinned_session_field(tag, 'pr_tracked', False)

        # If server is dead AND the row has no remaining PR data
        # (no PR Branch keeper), remove the row entirely.  Otherwise
        # let the merge keep it alive — the user kept the PR Branch
        # cell on purpose and can clear it via its X button.
        session = next((s for s in self.sessions if s['tag'] == tag), None)
        is_dead = session and session.get('server_pid') is None
        pin = self._pinned_sessions.get(tag, {})
        has_pr_branch_data = bool(
            pin.get('remote_project_path') and pin.get('branch'))
        if is_dead and not _skip_prompt and not has_pr_branch_data:
            # Offer to close the client too
            if session and session.get('has_client', False):
                client_reply = QMessageBox.question(
                    self, 'Close Client',
                    f"A client is connected to '{tag}'.\n"
                    f"Do you also want to close the client?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if client_reply == QMessageBox.Yes:
                    self._close_client(tag, session.get('client_pid'))

            self._pinned_sessions.pop(tag, None)
            remove_pinned_session_tag(tag)
            self._deleted_tags.add(tag)
            self.sessions = [s for s in self.sessions if s['tag'] != tag]
            self._cleanup_row_state(tag)

        # Start/stop the poll timer based on whether anything still needs
        # polling (tracked PRs, notifications, or Cursor GUI rows).
        self._sync_scm_poll_timer()

        if not _skip_ui:
            self._update_table()
            self._update_dock_badge()

    def _stop_tracking_closed_pr(self, tag: str) -> None:
        """X on a Merged/Closed PR badge — drop the merged/closed state.

        Keeps ``remote_project_path`` + ``branch`` so the row survives and
        its PR column flips back to a Track PR button.  The user can then
        re-track a fresh PR on the same branch, or clear the pinned data
        entirely with the X on the PR Branch column.
        """
        pin = self._pinned_sessions.get(tag)
        if not pin:
            return
        pin = pin.copy()
        for key in ('pr_merged', 'pr_closed', 'pr_title', 'pr_url',
                    'pr_iid', 'pr_tracked'):
            pin.pop(key, None)
        self._pinned_sessions[tag] = pin
        write_pinned_session_entry(tag, pin)
        self._cell_cache.pop((tag, 'pr'), None)
        # The row stops being polled, so its last (NO_PR) status would
        # otherwise linger forever — clear it like _stop_tracking does.
        self._pr_statuses.pop(tag, None)
        self._dock_badge.discard_tag(tag)
        # Dropping the badge may end the last reason to poll.
        self._sync_scm_poll_timer()
        self._show_status(f"Stopped tracking PR for '{tag}'")
        self._update_table()

    def _clear_pinned_pr_data(self, tag: str) -> None:
        """Clear pinned PR data so Track PR falls back to the server's live git info."""
        # If clearing will remove the row, warn so the user can confirm.
        # The clear removes ``remote_project_path``/``pr_tracked`` and
        # zeroes ``branch`` — so the row vanishes unless something
        # else keeps it alive (live server or a transient flag).
        session = next((s for s in self.sessions if s['tag'] == tag), None)
        will_survive = (
            (session and session.get('server_pid') is not None)
            or tag in self._starting_tags
            or tag in self._moving_tags
        )
        if not will_survive:
            reply = QMessageBox.question(
                self, 'Clear Pinned PR Data',
                f"The server for '{tag}' is not running.\n"
                f"Clearing pinned PR data will remove this row.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        pin = self._pinned_sessions.get(tag)
        if pin:
            for key in ('remote_project_path', 'host_url', 'scm_type',
                        'pr_title', 'pr_url', 'pr_iid', 'pr_tracked',
                        'pr_merged', 'pr_closed'):
                pin.pop(key, None)
            pin['branch'] = ''
            write_pinned_session_entry(tag, pin)

        # Invalidate the pr + pr_branch cell caches (a Merged/Closed badge
        # must rebuild as Track PR / vanish once its pinned data is gone).
        self._cell_cache.pop((tag, 'pr_branch'), None)
        self._cell_cache.pop((tag, 'pr'), None)
        # Dropping the pinned PR data may end the last reason to poll.
        self._sync_scm_poll_timer()

        self._show_status(f"Cleared pinned PR data for '{tag}'")
        self._update_table()

    def _on_tracking_result(self, tag: str, status: PRStatus) -> None:
        """Handle the result of a one-shot PR check."""
        self._checking_tags.discard(tag)
        self._set_busy(False)
        silent = tag in self._silent_tracking_tags
        self._silent_tracking_tags.discard(tag)

        # Row was deleted while the check was in-flight — discard result
        if tag in self._deleted_tags:
            self._pending_tracking_context.pop(tag, None)
            return

        if status.state == PRState.NO_PR:
            ctx = self._pending_tracking_context.pop(tag, None)
            if silent:
                self._show_status(f"Auto-reconnect: no open PR found for '{tag}'")
            # Resolve the provider while the session still exists.
            # _remove_dead_untracked_row drops the session from
            # self.sessions, after which _get_provider_for_session would
            # come up empty and the closed/merged-PR fallback would
            # degrade to the plain alert.  Dead-row removal is deferred
            # into _finalize_no_open_pr: if the lookup finds a merged or
            # closed PR for this branch, we keep the row alive so its PR
            # column can show a "Merged"/"Closed" badge instead.
            session = next(
                (s for s in self.sessions if s['tag'] == tag), None)
            provider = (
                self._get_provider_for_session(session) if session else None)
            self._show_no_open_pr_alert(tag, ctx, provider, silent=silent)
            return

        # PR found — promote to tracked and enrich pinned session.
        # A row coming back to OPEN supersedes any stale merged/closed
        # state, so drop those flags (and the now-stale 'pr' cell cache)
        # or the Merged/Closed badge would outlive its trigger.
        ctx = self._pending_tracking_context.pop(tag, None)
        if ctx:
            pin = self._pinned_sessions.get(tag, {})
            pin.update({
                'remote_project_path': ctx['remote_project_path'],
                'host_url': ctx['host_url'],
                'scm_type': ctx['scm_type'],
                'branch': ctx['branch'],
                'pr_title': status.pr_title or '',
                'pr_url': status.pr_url or '',
                'pr_tracked': True,
            })
            pin.pop('pr_merged', None)
            pin.pop('pr_closed', None)
            self._pinned_sessions[tag] = pin
            write_pinned_session_entry(tag, pin)
            self._cell_cache.pop((tag, 'pr'), None)
        else:
            # No context but PR found (e.g. auto-reconnect) — persist flag
            pin = self._pinned_sessions.get(tag)
            if pin and not pin.get('pr_tracked'):
                pin['pr_tracked'] = True
                update_pinned_session_field(tag, 'pr_tracked', True)

        self._show_status(f"PR found for '{tag}' - tracking started")
        self._tracked_tags.add(tag)
        self._pr_statuses[tag] = status
        self._update_table()
        self._update_dock_badge()

        if not self._scm_poll_timer.isActive():
            self._scm_poll_timer.start(self._get_poll_interval() * 1000)

    def _show_no_open_pr_alert(self, tag: str,
                               ctx: Optional[dict[str, Any]],
                               provider: Optional[Any],
                               silent: bool = False) -> None:
        """Resolve the closed/merged-PR fallback for a NO_PR tracking result.

        When the provider finds a recent closed/merged PR for the same
        branch, the matching state is persisted on the pinned row so its PR
        column flips to a "Merged"/"Closed" badge, and the interactive path
        also pops an 'Open in Browser' dialog.  The lookup is a network call,
        so it runs in a ``BackgroundCallWorker`` (never on the UI thread) —
        mirroring ``_add_row_from_pr_url``.  Falls straight through to the
        plain alert (or silent row removal) when there's nothing to look up.

        ``silent`` controls user-facing UI:
          - True  : no dialog, no busy spinner (auto-reconnect path).
          - False : modal dialog + busy spinner (interactive Track PR).
        """
        can_lookup = bool(
            provider is not None and ctx
            and ctx.get('remote_project_path') and ctx.get('branch'))
        if not can_lookup:
            self._finalize_no_open_pr(tag, ctx, closed=None, silent=silent)
            return

        project_path = ctx['remote_project_path']
        branch = ctx['branch']
        result_holder: list[Optional[Any]] = [None]

        def _fetch() -> None:
            result_holder[0] = provider.find_latest_closed_pr(
                project_path, branch)

        if not silent:
            self._set_busy(True)
            QApplication.setOverrideCursor(Qt.WaitCursor)
        worker = BackgroundCallWorker(_fetch, self)
        worker.finished.connect(
            lambda: self._on_closed_pr_lookup(tag, ctx, result_holder, silent))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_closed_pr_lookup(self, tag: str, ctx: Optional[dict[str, Any]],
                             result_holder: list, silent: bool) -> None:
        """Closed-PR lookup finished — restore busy state, then finalize."""
        if not silent:
            self._set_busy(False)
            QApplication.restoreOverrideCursor()
        self._finalize_no_open_pr(tag, ctx, result_holder[0], silent)

    def _finalize_no_open_pr(self, tag: str, ctx: Optional[dict[str, Any]],
                             closed: Optional[Any], silent: bool) -> None:
        """Persist merged/closed state when there's a PR to surface, else
        remove the (possibly dead) row; then show the dialog if interactive."""
        has_closed = bool(closed is not None
                          and getattr(closed, 'pr_url', None))
        if has_closed and tag in self._pinned_sessions:
            self._persist_closed_pr(tag, ctx, closed)
        else:
            # Nothing to surface — clean up dead rows as before.
            self._remove_dead_untracked_row(tag)
            self._update_table()
        if not silent:
            self._no_open_pr_dialog(closed, ctx)

    def _persist_closed_pr(self, tag: str, ctx: Optional[dict[str, Any]],
                           closed: Any) -> None:
        """Mark a row as displaying a merged or closed-without-merge PR.

        Keeps the row alive (via ``remote_project_path`` + ``branch``) and
        switches its PR column from a Track PR button to a Merged/Closed
        badge that opens the PR URL.  Also (re)starts the poll timer so the
        row gets re-checked for a re-opened PR.
        """
        pin = self._pinned_sessions.get(tag, {}).copy()
        if ctx:
            pin['remote_project_path'] = ctx.get(
                'remote_project_path', pin.get('remote_project_path'))
            pin['host_url'] = ctx.get('host_url', pin.get('host_url'))
            pin['scm_type'] = ctx.get('scm_type', pin.get('scm_type'))
            pin['branch'] = ctx.get('branch', pin.get('branch'))
        pin['pr_title'] = closed.pr_title or ''
        pin['pr_url'] = closed.pr_url or ''
        pin['pr_iid'] = closed.pr_iid
        pin['pr_merged'] = bool(closed.merged)
        pin['pr_closed'] = not bool(closed.merged)
        pin['pr_tracked'] = False
        self._pinned_sessions[tag] = pin
        write_pinned_session_entry(tag, pin)
        self._cell_cache.pop((tag, 'pr'), None)
        self._sync_scm_poll_timer()
        self._update_table()

    def _no_open_pr_dialog(self, closed: Optional[Any],
                           ctx: Optional[dict[str, Any]]) -> None:
        """Render the 'No open PR found' message box (+ optional Open button)."""
        if closed is None or not closed.pr_url:
            QMessageBox.information(
                self, 'No PR Found',
                'No open PR found for this branch.'
            )
            return

        branch = (ctx or {}).get('branch', '')
        intro = (f'No open PR found for "{branch}".' if branch
                 else 'No open PR found for this branch.')
        state_word = 'merged' if closed.merged else 'closed'
        title = closed.pr_title or '(no title)'
        text = (
            f'{intro}\n\n'
            f'Found a {state_word} PR for this branch:\n'
            f'PR #{closed.pr_iid}: {title}'
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle('No Open PR Found')
        box.setText(text)
        open_btn = box.addButton('Open in Browser', QMessageBox.AcceptRole)
        box.addButton('OK', QMessageBox.RejectRole)
        box.setDefaultButton(open_btn)
        box.exec_()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl(closed.pr_url))

    def _on_tracking_error(self, tag: str, message: str) -> None:
        """Handle an error from a one-shot PR check."""
        self._checking_tags.discard(tag)
        self._set_busy(False)
        silent = tag in self._silent_tracking_tags
        self._silent_tracking_tags.discard(tag)
        self._pending_tracking_context.pop(tag, None)

        # Row was deleted while the check was in-flight — discard error
        if tag in self._deleted_tags:
            return
        # Silent errors (auto-reconnect blips, transient 5xx, slow VPN)
        # used to wipe the row here.  That bypassed the merge keepers
        # and caused user-visible data loss on every flaky startup.
        # Now we leave the row alone — it stays via ``pr_tracked`` /
        # PR Branch keepers, and the user can manually click Track PR
        # once the SCM provider is healthy again.
        self._update_table()
        self._show_status(f"PR tracking error for '{tag}': {message}")
        if not silent:
            QMessageBox.warning(self, 'Error', message)

    def _revisit_tags(self) -> set[str]:
        """Tags pinned with a merged/closed PR that we keep polling.

        A merged/closed PR-pinned row shows a "Merged"/"Closed" badge but is
        still polled each cycle so that if the branch gets a *new* open PR
        (re-opened, or a fresh PR on the same branch) we can flip the badge
        back to live tracking.  Requires the pinned PR-branch data the
        poller needs to resolve the query (``remote_project_path`` +
        ``branch``).
        """
        return {
            tag for tag, pin in self._pinned_sessions.items()
            if (pin.get('pr_merged') or pin.get('pr_closed'))
            and pin.get('remote_project_path') and pin.get('branch')
        }

    def _revisit_poll_sessions(self) -> list[dict[str, Any]]:
        """Build status-only poll sessions for merged/closed PR-pinned rows.

        Each is a *fresh* dict (never the live ``self.sessions`` object, which
        other code reads) carrying just what ``_poll_session`` needs to
        resolve the query, marked ``_pr_only`` so the poller fetches PR state
        - to catch a re-opened PR - but never scans or delivers ``/leap`` for
        them: the PR is closed, and the row may be a dead row with no session
        to receive a message.  Tags already in ``_tracked_tags`` are excluded
        so a tag is never polled twice.
        """
        revisit_tags = self._revisit_tags() - self._tracked_tags
        return [
            {
                'tag': s['tag'],
                'remote_project_path': s.get('remote_project_path'),
                'scm_type': s.get('scm_type'),
                'branch': s.get('pr_branch') or s.get('branch'),
                'pr_branch': s.get('pr_branch'),
                'project_path': s.get('project_path'),
                '_pr_only': True,
            }
            for s in self.sessions if s['tag'] in revisit_tags
        ]

    def _sync_scm_poll_timer(self) -> None:
        """Start or stop the SCM poll timer based on whether anything needs
        polling: tracked PRs (which now includes opt-in-tracked Cursor
        editor Agent-tab rows, whose tag joins ``_tracked_tags``), user
        notifications, or merged/closed PR-pinned rows we keep watching for
        a re-open.

        Without this, a user who only tracks a Cursor GUI row (no real
        tracked PR, no notifications) would never poll, since every other
        timer-start site keys off tracking/notifications too.
        """
        want = bool(self._tracked_tags or self._get_notif_scm_types()
                    or self._revisit_tags())
        active = self._scm_poll_timer.isActive()
        if want and not active:
            self._scm_poll_timer.start(self._get_poll_interval() * 1000)
        elif not want and active:
            self._scm_poll_timer.stop()

    def _start_scm_poll(self) -> None:
        """Start a background SCM poll for tracked sessions and/or notifications."""
        if self._shutting_down:
            return
        if not self._scm_providers:
            return
        if self._scm_polling:
            # Force-reset if polling has been stuck for over 60 seconds
            elapsed = time.monotonic() - self._scm_poll_started_at
            if elapsed > 60:
                logger.debug("SCM poll stuck for %.0fs, force-resetting", elapsed)
                self._show_status(f"SCM poll stuck for {elapsed:.0f}s - force-reset")
                self._scm_polling = False
                if self._scm_worker:
                    old_worker = self._scm_worker
                    try:
                        old_worker.results_ready.disconnect()
                        old_worker.notifications_ready.disconnect()
                        old_worker.finished.disconnect()
                    except (TypeError, RuntimeError):
                        pass  # Already disconnected or deleted
                    # Schedule cleanup once the stuck thread eventually finishes.
                    # deleteLater() is safe here: it won't fire until the event
                    # loop processes it, and by then _on_scm_worker_finished
                    # (now disconnected) won't interfere.
                    old_worker.finished.connect(old_worker.deleteLater)
                    self._scm_worker = None
            else:
                return

        has_tracked = bool(self._tracked_tags)
        notif_types = self._get_notif_scm_types()
        # Editor-GUI rows (Cursor Agent tabs, VS Code Copilot chats) the
        # user opted to track (their tag is in _tracked_tags).  Marked
        # ``_pr_only`` so the worker fetches PR state but skips /leap
        # handling (they're not Leap sessions).  Polled on the same ~30s
        # SCM cadence, not the 1s table refresh.
        # Only ``project_path`` is required: _poll_session resolves the
        # branch from the folder's git remote itself (the passed ``branch``
        # is unused on that path), so requiring a non-empty ``branch`` here
        # only served to permanently exclude a tracked row whose folder
        # reported no branch (non-git / detached HEAD) - leaving its
        # PR cell stuck on "Checking…" forever.  Without the filter such a
        # row is polled and resolves to a normal "No PR" instead.
        cursor_poll_sessions = [
            {'tag': r['tag'], 'project_path': r.get('project_path'),
             'branch': r.get('branch'), '_pr_only': True}
            for r in getattr(self, '_cursor_gui_rows', [])
            if r.get('project_path') and r['tag'] in self._tracked_tags
        ]
        has_cursor = bool(cursor_poll_sessions)
        # Merged/closed PR-pinned rows: polled (by branch, no pr_iid) so a
        # fresh open PR on the same branch flips the badge back to tracking.
        revisit_sessions = self._revisit_poll_sessions()
        has_revisit = bool(revisit_sessions)
        if not has_tracked and not notif_types and not has_cursor \
                and not has_revisit:
            return

        tracked_sessions = [s for s in self.sessions if s['tag'] in self._tracked_tags]
        if has_tracked and not tracked_sessions:
            if not notif_types and not has_cursor and not has_revisit:
                logger.debug("SCM poll skipped: no tracked sessions found in active sessions")
                return

        poll_sessions = tracked_sessions + cursor_poll_sessions + revisit_sessions
        logger.debug("Starting SCM poll for tags: %s (cursor=%d revisit=%d notif=%s)",
                      [s['tag'] for s in tracked_sessions],
                      len(cursor_poll_sessions), len(revisit_sessions),
                      notif_types)
        self._scm_polling = True
        self._scm_poll_started_at = time.monotonic()
        worker = SCMPollerWorker(self)
        worker.configure(
            self._scm_providers, poll_sessions,
            auto_fetch_leap=self._prefs.get('auto_fetch_leap', False),
            notif_scm_types=notif_types,
        )
        worker.results_ready.connect(self._on_scm_results)
        worker.notifications_ready.connect(self._on_notifications_received)
        worker.notification_auth_error.connect(self._on_notification_auth_error)
        worker.leap_ack_failed.connect(self._on_leap_ack_failed)
        worker.leap_send_failed.connect(self._on_leap_send_failed)
        worker.leap_send_recovered.connect(self._on_leap_send_recovered)
        worker.finished.connect(self._on_scm_worker_finished)
        self._scm_worker = worker
        worker.start()

    def _on_scm_worker_finished(self) -> None:
        """Clean up after poller worker completes.

        Uses sender() to identify the actual worker that emitted ``finished``,
        avoiding a race where the stuck-poll safeguard has already replaced
        ``self._scm_worker`` with a new instance.
        """
        worker = self.sender()
        logger.debug("SCM poll worker finished")
        if worker is not None:
            worker.deleteLater()
        if self._scm_worker is worker:
            self._scm_polling = False
            self._scm_worker = None

    @staticmethod
    def _pr_fire_snapshot_changed(old: tuple, new: tuple) -> bool:
        """Whether a PR snapshot changed enough to re-arm the 🔥 nudge.

        Snapshot layout: (state, unresponded_count, approved, approved_by,
        approval_known, changes_requested, checks_failed).  The approval fields
        are compared only when *both* snapshots had a known approval state, so a
        transiently-failed-then-recovered approvals fetch doesn't masquerade as
        a real approval change (the same reason the dock/banner diff guards on
        ``approval_known``).
        """
        # Non-approval fields: state, count, changes_requested, checks_failed.
        if (old[0] != new[0] or old[1] != new[1]
                or old[5] != new[5] or old[6] != new[6]):
            return True
        # Approval fields (approved, approved_by) — only when both are known.
        if old[4] and new[4] and (old[2] != new[2] or old[3] != new[3]):
            return True
        return False

    def _on_scm_results(self, results: dict[str, PRStatus]) -> None:
        """Handle SCM poll results (runs in main thread via signal)."""
        if self._shutting_down:
            return
        try:
            if not self.isVisible():
                return
            now = time.time()
            transitions: list[str] = []
            reopenings: list[tuple[str, PRStatus]] = []
            for tag, status in results.items():
                logger.debug("SCM result: tag=%s state=%s unresponded=%s approved=%s",
                             tag, status.state.value, status.unresponded_count, status.approved)
                new_snap = (
                    status.state,
                    status.unresponded_count,
                    status.approved,
                    tuple(sorted(status.approved_by or [])),
                    # Carried so the 🔥 comparison can ignore approval when the
                    # fetch failed - a failed-then-recovered approval read must
                    # not be mistaken for a real approval change (mirrors the
                    # dock/banner approval_known guard).
                    status.approval_known,
                    # Flip the 🔥 "recently changed" nudge when a reviewer
                    # requests changes or CI starts failing (these are
                    # action-needed events worth surfacing); deliberately NOT
                    # wired into dock/banner notifications.
                    status.changes_requested,
                    status.checks_failed,
                )
                prev = self._pr_changed_at.get(tag)
                if prev is None:
                    # First time — seed with epoch 0 (no fire on startup)
                    self._pr_changed_at[tag] = (new_snap, 0)
                elif self._pr_fire_snapshot_changed(prev[0], new_snap):
                    self._pr_changed_at[tag] = (new_snap, now)
                    self._dismissed_pr_new_status.discard(tag)

                # Tracked PR just disappeared (open -> NO_PR): confirm on the
                # edge whether it was merged or closed.  ``self._pr_statuses``
                # is updated AFTER this loop, so .get(tag) is still the
                # previous status here.
                if (status.state == PRState.NO_PR
                        and tag in self._tracked_tags):
                    prev_status = self._pr_statuses.get(tag)
                    if (prev_status is not None
                            and prev_status.state != PRState.NO_PR):
                        transitions.append(tag)

                # Merged/closed PR-pinned row whose branch now has an open PR
                # again -> promote back to live tracking.
                pin = self._pinned_sessions.get(tag, {})
                if (status.state != PRState.NO_PR
                        and (pin.get('pr_merged') or pin.get('pr_closed'))):
                    reopenings.append((tag, status))

            self._pr_statuses.update(results)
            for tag in transitions:
                self._check_pr_closed_after_no_pr(tag)
            for tag, status in reopenings:
                self._reopen_tracked_pr(tag, status)
            # A re-open swaps a badge cell for a live tracked cell, which
            # only a full rebuild produces; otherwise the fast path suffices.
            if reopenings:
                self._update_table()
            else:
                self._update_pr_column()
            self._update_dock_badge()
        except Exception:
            logger.exception("Error handling SCM results")

    def _reopen_tracked_pr(self, tag: str, status: PRStatus) -> None:
        """A merged/closed PR-pinned row's branch has an open PR again —
        promote it back to live tracking and clear the stale flags."""
        pin = self._pinned_sessions.get(tag)
        if not pin:
            return
        pin = pin.copy()
        pin['pr_title'] = status.pr_title or pin.get('pr_title', '')
        pin['pr_url'] = status.pr_url or pin.get('pr_url', '')
        if status.pr_iid is not None:
            pin['pr_iid'] = status.pr_iid
        pin['pr_tracked'] = True
        pin.pop('pr_merged', None)
        pin.pop('pr_closed', None)
        self._pinned_sessions[tag] = pin
        write_pinned_session_entry(tag, pin)
        self._tracked_tags.add(tag)
        self._cell_cache.pop((tag, 'pr'), None)
        self._show_status(f"PR for '{tag}' is open again - tracking resumed")

    def _check_pr_closed_after_no_pr(self, tag: str) -> None:
        """A tracked PR just went NO_PR — look up (in the background) whether
        it was merged or closed-without-merge.  On success, drop the tag from
        ``_tracked_tags`` and persist the Merged/Closed badge state."""
        pin = self._pinned_sessions.get(tag)
        if not pin:
            return
        ctx = {
            'remote_project_path': pin.get('remote_project_path'),
            'host_url': pin.get('host_url'),
            'scm_type': pin.get('scm_type'),
            'branch': pin.get('branch') or pin.get('pr_branch'),
        }
        if not ctx['remote_project_path'] or not ctx['branch']:
            return
        session = next((s for s in self.sessions if s['tag'] == tag), None)
        provider = (
            self._get_provider_for_session(session) if session else None)
        if provider is None:
            return

        project_path = ctx['remote_project_path']
        branch = ctx['branch']
        result_holder: list[Optional[Any]] = [None]

        def _fetch() -> None:
            result_holder[0] = provider.find_latest_closed_pr(
                project_path, branch)

        worker = BackgroundCallWorker(_fetch, self)
        worker.finished.connect(
            lambda: self._on_polled_pr_closed_lookup(tag, ctx, result_holder))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_polled_pr_closed_lookup(self, tag: str, ctx: dict[str, Any],
                                    result_holder: list) -> None:
        """Result of the polled tracked->NO_PR background lookup."""
        # This fires from a background worker that may finish after the
        # window started closing (the lookup can take up to the SCM timeout).
        # _persist_closed_pr -> _update_table would then touch a torn-down
        # table, so bail out like _on_scm_results does.
        if self._shutting_down:
            return
        closed = result_holder[0]
        if closed is None or not getattr(closed, 'pr_url', None):
            # NO_PR with no surfacable closed PR: leave the tracked row alone
            # (keeps showing "No PR"; the user can stop tracking manually).
            return
        if tag not in self._pinned_sessions:
            return
        self._tracked_tags.discard(tag)
        self._pr_statuses.pop(tag, None)
        self._pr_widgets.pop(tag, None)
        self._pr_approval_widgets.pop(tag, None)
        self._persist_closed_pr(tag, ctx, closed)
        state_word = 'merged' if closed.merged else 'closed'
        self._show_status(f"PR for '{tag}' was {state_word}")

    # ------------------------------------------------------------------
    #  Thread sending
    # ------------------------------------------------------------------

    def _is_send_in_progress(self) -> bool:
        """Check if any thread-send worker is currently running."""
        return (
            (self._collect_threads_worker is not None and self._collect_threads_worker.isRunning())
            or (self._send_threads_worker is not None and self._send_threads_worker.isRunning())
            or (self._send_combined_worker is not None and self._send_combined_worker.isRunning())
        )

    def _send_all_threads_to_leap(self, tag: str) -> None:
        """Send all unresponded PR threads to the Leap session (non-blocking).

        Phase 1 (CollectThreadsWorker): resolve provider, collect threads, match sessions.
        Phase 2 (SendThreadsWorker): send each thread to Leap and acknowledge on SCM.
        """
        if not self._scm_providers:
            return

        if self._is_send_in_progress():
            QMessageBox.information(
                self, 'In Progress',
                'Already sending comments - please wait.'
            )
            return

        session = next((s for s in self.sessions if s['tag'] == tag), None)
        if not session:
            return

        project_path = session.get('project_path')
        if not project_path:
            return

        # Launch Phase 1 — everything runs in background
        self._leap_only_collect = False
        self._set_busy(True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        pr_iid = self._pinned_sessions.get(tag, {}).get('pr_iid')
        self._collect_threads_worker = CollectThreadsWorker(self)
        self._collect_threads_worker.configure(
            project_path, self._scm_providers, self.sessions, target_tag=tag,
            pr_iid=pr_iid,
        )
        self._combined_send = False
        self._collect_threads_worker.collected.connect(self._on_threads_collected)
        self._collect_threads_worker.error.connect(self._on_send_threads_error)
        self._collect_threads_worker.start()

    def _on_threads_collected(self, commands: list, matching_tags: list) -> None:
        """Handle Phase 1 completion: show dialog if needed, then launch Phase 2.

        Uses ``_combined_send`` flag to decide which Phase 2 worker to launch.
        """
        provider = self._collect_threads_worker.provider if self._collect_threads_worker else None
        # Clean up the collect worker now that Phase 1 is done
        if self._collect_threads_worker:
            self._collect_threads_worker.deleteLater()
            self._collect_threads_worker = None

        if not commands or not provider:
            self._set_busy(False)
            QApplication.restoreOverrideCursor()
            QMessageBox.information(
                self, 'No comments',
                "No comments with a '/leap' tag found." if self._leap_only_collect
                else 'No unresponded comments found.'
            )
            return

        if not matching_tags:
            self._set_busy(False)
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(
                self, 'No Session',
                'No matching Leap session found for this project.'
            )
            return

        if len(matching_tags) == 1:
            matched_tag = matching_tags[0]
        else:
            self._set_busy(False)
            QApplication.restoreOverrideCursor()
            matched_tag, ok = QInputDialog.getItem(
                self, 'Select Session',
                'Multiple sessions found.\nPick one:',
                matching_tags, 0, False
            )
            if not ok:
                return
            self._set_busy(True)
            QApplication.setOverrideCursor(Qt.WaitCursor)

        # Launch Phase 2 — send + acknowledge in background
        if self._combined_send:
            self._send_combined_worker = SendThreadsCombinedWorker(self)
            self._send_combined_worker.configure(provider, commands, matched_tag)
            self._send_combined_worker.finished.connect(self._on_send_combined_finished)
            self._send_combined_worker.error.connect(self._on_send_threads_error)
            self._send_combined_worker.ack_failed.connect(self._on_leap_ack_failed)
            self._send_combined_worker.start()
        else:
            self._send_threads_worker = SendThreadsWorker(self)
            self._send_threads_worker.configure(provider, commands, matched_tag)
            self._send_threads_worker.finished.connect(self._on_send_threads_finished)
            self._send_threads_worker.error.connect(self._on_send_threads_error)
            self._send_threads_worker.ack_failed.connect(self._on_leap_ack_failed)
            self._send_threads_worker.send_partial_failed.connect(
                self._on_send_threads_partial_failure)
            self._send_threads_worker.start()

    def _on_send_threads_finished(self, sent_count: int, matched_tag: str) -> None:
        """Handle Phase 2 completion."""
        if self._send_threads_worker:
            self._send_threads_worker.deleteLater()
            self._send_threads_worker = None
        self._set_busy(False)
        QApplication.restoreOverrideCursor()
        if sent_count > 0:
            noun = 'comment' if sent_count == 1 else 'comments'
            self._show_status(f"Sent {sent_count} {noun} to '{matched_tag}'")
            QMessageBox.information(
                self, 'Comments sent',
                f"Sent {sent_count} {noun} to session '{matched_tag}'."
            )
            self._start_scm_poll()
        else:
            # finished(0, tag) reaches us only when commands were collected
            # but every socket send returned False (typically: server died
            # mid-loop).  "No unresponded comments" would be misleading.
            QMessageBox.warning(
                self, 'Send failed',
                f"Couldn't queue any comments to session '{matched_tag}'.\n\n"
                'The session server may have stopped - check it and try again.'
            )

    def _on_send_threads_partial_failure(
        self, sent_count: int, failed_count: int, matched_tag: str,
    ) -> None:
        """Handle partial-send completion (replaces the success popup).

        Fires INSTEAD of ``finished`` when any per-cmd send returned
        False.  Worker cleanup needs to happen here too because
        ``_on_send_threads_finished`` won't run on this path.  Failed
        comments are NOT acked, so the next click re-detects them.
        """
        if self._send_threads_worker:
            self._send_threads_worker.deleteLater()
            self._send_threads_worker = None
        self._set_busy(False)
        QApplication.restoreOverrideCursor()

        if sent_count > 0:
            sent_noun = 'comment' if sent_count == 1 else 'comments'
            failed_noun = 'comment' if failed_count == 1 else 'comments'
            QMessageBox.warning(
                self, 'Partial delivery',
                f"Sent {sent_count} {sent_noun} to '{matched_tag}', but "
                f"{failed_count} {failed_noun} failed to deliver.\n\n"
                "The failed comments were NOT acknowledged on the SCM side, "
                "so they'll be re-detected next time you click "
                "'Send comments to session' (or on the next auto-fetch).\n\n"
                'Common causes: session was killed mid-loop, queue is full, '
                'or socket dropped.'
            )
            self._start_scm_poll()
        else:
            # All sends failed — single explicit popup.
            QMessageBox.warning(
                self, 'Send failed',
                f"Couldn't deliver any of the {failed_count} comment(s) to "
                f"session '{matched_tag}'.\n\n"
                'The session server may have stopped - check it and try again.'
            )

    def _on_send_threads_error(self, message: str) -> None:
        """Handle error from either background worker."""
        # Clean up whichever worker(s) are still alive
        for attr in ('_collect_threads_worker', '_send_threads_worker', '_send_combined_worker'):
            worker = getattr(self, attr, None)
            if worker is not None:
                worker.deleteLater()
                setattr(self, attr, None)
        self._set_busy(False)
        QApplication.restoreOverrideCursor()
        self._show_status(f"Comment send error: {message}")
        QMessageBox.warning(self, 'Error', message)

    def _on_leap_ack_failed(self) -> None:
        """Handle failure to post '[Leap bot] on it!' acknowledgment.

        Without the ack, the same /leap command will be re-detected every poll
        cycle, causing duplicate sends.  Disable auto-fetch and warn the user.
        """
        # Stop polling to prevent duplicate popups
        self._scm_poll_timer.stop()

        # Disable auto-fetch
        self._prefs['auto_fetch_leap'] = False
        self._save_prefs()
        self.auto_leap_check.setChecked(False)

        QMessageBox.warning(
            self, '/leap Acknowledgment Failed',
            'Failed to post "[Leap bot] on it!" reply to the PR comment.\n\n'
            "Without this reply, the same '/leap' tag will be re-detected "
            'each poll cycle, causing duplicate sends.\n\n'
            "Auto '/leap' fetch has been disabled to prevent this.\n\n"
            'Common cause: the SCM token lacks the "api" scope '
            '(GitLab) or sufficient permissions (GitHub/Bitbucket).\n'
            "Update your token, then re-enable \"Auto '/leap' fetch\"."
        )

    def _on_leap_send_recovered(self, tag: str) -> None:
        """Successful auto-fetch send for *tag* — clear any stale
        "we already warned about this tag" entry so the NEXT failure
        (if the session crashes again) gets a fresh popup instead of
        a status-bar-only message.
        """
        warned = getattr(self, '_leap_send_failed_warned', None)
        if warned is not None:
            warned.discard(tag)

    def _on_leap_send_failed(self, tag: str) -> None:
        """Handle failure to deliver an auto-fetched /leap to a Leap session.

        We deliberately don't ack the comment in this case (so the next
        poll retries the delivery) — without surfacing it the user would
        have no idea the message never landed.  De-duplicates per-tag so
        a single broken session doesn't spam popups every poll.
        """
        already_warned = getattr(self, '_leap_send_failed_warned', set())
        if tag in already_warned:
            self._show_status(
                f"/leap delivery to '{tag}' still failing - will keep retrying"
            )
            return
        already_warned.add(tag)
        self._leap_send_failed_warned = already_warned

        QMessageBox.warning(
            self, '/leap Delivery Failed',
            f"Failed to deliver an auto-fetched /leap message to session "
            f"'{tag}'.\n\n"
            "The PR comment has NOT been acknowledged, so the next poll "
            "cycle will retry delivery.\n\n"
            "Common causes:\n"
            "  • Session is not running (no socket)\n"
            "  • Session's queue is full or unhealthy\n\n"
            "If the session has been closed permanently, dismiss this row "
            "from the monitor or remove the /leap comment to stop retries."
        )

        self._show_status("/leap auto-fetch disabled (acknowledgment failed)")

        # Restart polling (now without auto-fetch)
        if self._tracked_tags or self._get_notif_scm_types():
            self._scm_poll_timer.start(self._get_poll_interval() * 1000)

    def _send_all_threads_combined_to_leap(self, tag: str) -> None:
        """Send all unresponded PR threads as one concatenated message (non-blocking).

        Reuses Phase 1 (CollectThreadsWorker) then sends a single combined message.
        """
        if not self._scm_providers:
            return

        if self._is_send_in_progress():
            QMessageBox.information(
                self, 'In Progress',
                'Already sending comments - please wait.'
            )
            return

        session = next((s for s in self.sessions if s['tag'] == tag), None)
        if not session:
            return

        project_path = session.get('project_path')
        if not project_path:
            return

        # Launch Phase 1 — collection runs in background
        self._leap_only_collect = False
        self._combined_send = True
        self._set_busy(True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        pr_iid = self._pinned_sessions.get(tag, {}).get('pr_iid')
        self._collect_threads_worker = CollectThreadsWorker(self)
        self._collect_threads_worker.configure(
            project_path, self._scm_providers, self.sessions, target_tag=tag,
            pr_iid=pr_iid,
        )
        self._collect_threads_worker.collected.connect(self._on_threads_collected)
        self._collect_threads_worker.error.connect(self._on_send_threads_error)
        self._collect_threads_worker.start()

    def _on_send_combined_finished(self, thread_count: int, matched_tag: str) -> None:
        """Handle combined send completion."""
        if self._send_combined_worker:
            self._send_combined_worker.deleteLater()
            self._send_combined_worker = None
        self._set_busy(False)
        QApplication.restoreOverrideCursor()
        if thread_count > 0:
            noun = 'comment' if thread_count == 1 else 'comments'
            self._show_status(
                f"Sent {thread_count} {noun} combined to '{matched_tag}'")
            QMessageBox.information(
                self, 'Comments sent',
                f"Sent {thread_count} {noun} as one message to session "
                f"'{matched_tag}'."
            )
            self._start_scm_poll()
        else:
            QMessageBox.information(
                self, 'No comments',
                "No comments with a '/leap' tag found." if self._leap_only_collect
                else 'No unresponded comments found.'
            )

    def _send_leap_threads_to_leap(self, tag: str) -> None:
        """Send only /leap-marked threads to Leap (one per queue message)."""
        self._send_leap_threads_common(tag, combined=False)

    def _send_leap_threads_combined_to_leap(self, tag: str) -> None:
        """Send only /leap-marked threads to Leap (combined into one message)."""
        self._send_leap_threads_common(tag, combined=True)

    def _send_leap_threads_common(self, tag: str, combined: bool) -> None:
        """Shared launcher for /leap-only thread sending."""
        if not self._scm_providers:
            return

        if self._is_send_in_progress():
            QMessageBox.information(
                self, 'In Progress',
                'Already sending comments - please wait.'
            )
            return

        session = next((s for s in self.sessions if s['tag'] == tag), None)
        if not session:
            return

        project_path = session.get('project_path')
        if not project_path:
            return

        self._leap_only_collect = True
        self._combined_send = combined
        self._set_busy(True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        pr_iid = self._pinned_sessions.get(tag, {}).get('pr_iid')
        self._collect_threads_worker = CollectThreadsWorker(self)
        self._collect_threads_worker.configure(
            project_path, self._scm_providers, self.sessions, leap_only=True,
            target_tag=tag, pr_iid=pr_iid,
        )
        self._collect_threads_worker.collected.connect(self._on_threads_collected)
        self._collect_threads_worker.error.connect(self._on_send_threads_error)
        self._collect_threads_worker.start()

    # ------------------------------------------------------------------
    #  Add row from Git URL, PR URL, or local path
    # ------------------------------------------------------------------

    def _add_row_menu(self) -> None:
        """Show a menu to choose how to add a new row."""
        menu = QMenu(self)
        if self._prefs.get('show_tooltips', True):
            menu.setToolTipsVisible(True)

        git_action = menu.addAction('From Git URL')
        git_action.setToolTip(
            'Add a row from a PR URL, commit URL,\n'
            'or plain Git project URL')
        git_action.triggered.connect(self._add_row_from_git)

        local_action = menu.addAction('From Local Path')
        local_action.setToolTip(
            'Add a row from a local Git repository -\n'
            'clone to repos dir or open directly')
        local_action.triggered.connect(self._add_row_from_local)

        resume_action = menu.addAction('From Resume')
        resume_action.setToolTip(
            'Resume a recorded CLI session - same picker as\n'
            '`leap --resume`, opened in the default terminal.')
        resume_action.triggered.connect(self._add_row_from_resume)

        menu.exec_(QCursor.pos())

    def _add_row_from_git(self) -> None:
        """Add a row from a Git URL (PR URL or plain project URL)."""
        gitlab_config = load_gitlab_config()
        github_config = load_github_config()
        bitbucket_config = load_bitbucket_config()
        prev_url = ''
        while True:
            dlg = QInputDialog(self)
            dlg.setWindowTitle('Add from Git URL')
            dlg.setLabelText('Git URL (PR URL, commit URL, or project URL):')
            dlg.setTextValue(prev_url)
            dlg.resize(800, dlg.sizeHint().height())
            ok = dlg.exec_() == QInputDialog.Accepted
            url = dlg.textValue()
            if not ok or not url.strip():
                return
            prev_url = url.strip()

            # Try PR URL first
            parsed_pr = parse_pr_url(prev_url, gitlab_config, github_config,
                                     bitbucket_config)
            if parsed_pr:
                provider = self._scm_providers.get(parsed_pr.scm_type.value)
                if not provider:
                    if not self._scm_providers:
                        QMessageBox.information(
                            self, 'No SCM Connected',
                            'Connect to GitLab, GitHub or Bitbucket first '
                            'using the buttons at the bottom.',
                        )
                    else:
                        QMessageBox.warning(
                            self, 'No Provider',
                            f'No connected provider for {parsed_pr.scm_type.value}.',
                        )
                    continue
                self._add_row_from_pr_url(parsed_pr, provider)
                return

            # Try plain project URL
            parsed_proj = parse_project_url(prev_url, gitlab_config,
                                            github_config, bitbucket_config)
            if parsed_proj:
                self._add_row_from_project_url(parsed_proj)
                return

            QMessageBox.warning(
                self, 'Invalid URL',
                'Could not parse the URL.\n\n'
                'Supported formats:\n'
                '  PR:     https://gitlab.com/group/project/-/merge_requests/42\n'
                '  PR:     https://github.com/owner/repo/pull/42\n'
                '  PR:     https://bitbucket.org/workspace/repo/pull-requests/42\n'
                '  Commit: https://gitlab.com/group/project/-/commit/abc123\n'
                '  Git:    https://host/group/project\n'
                '  SSH:    git@host:group/project.git',
            )
            continue

    def _add_row_from_pr_url(self, parsed: Any, provider: Any) -> None:
        """Fetch PR details in background, then ask for tag (PR flow)."""
        self._set_busy(True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        result_holder: list[Optional[Any]] = [None]

        def _fetch() -> None:
            result_holder[0] = provider.get_pr_details(parsed.project_path, parsed.pr_iid)

        worker = BackgroundCallWorker(_fetch, self)
        worker.finished.connect(lambda: self._on_add_row_pr_details(
            parsed, result_holder,
        ))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_add_row_pr_details(self, parsed: Any, result_holder: list) -> None:
        """Handle PR details fetched — ask for tag and pin the row."""
        self._set_busy(False)
        QApplication.restoreOverrideCursor()
        details = result_holder[0]
        if not details:
            QMessageBox.warning(self, 'No PR Found', 'Could not fetch PR details.')
            return

        if details.source_branch_deleted:
            QMessageBox.warning(
                self, 'Branch Deleted',
                f"The source branch '{details.source_branch}' no longer exists "
                f"on the remote.\n\n"
                f"This usually means the PR has been merged and the branch "
                f"was deleted.\n\n"
                f"The row cannot be added to the monitor.",
            )
            return

        tag = self._ask_tag([
            f"PR: {details.pr_title}",
            f"Branch: {details.source_branch}",
        ])
        if not tag:
            return

        # Pin the session with remote info and auto-start PR tracking.
        # ``pr_tracked: True`` records the user's intent to track this
        # PR — keeps the row alive across the initial refresh and any
        # transient tracking errors (so the user can retry Track PR
        # without re-adding from scratch).
        self._pinned_sessions[tag] = {
            'tag': tag,
            'remote_project_path': parsed.project_path,
            'host_url': parsed.host_url,
            'branch': details.source_branch,
            'pr_title': details.pr_title,
            'pr_url': details.pr_url,
            'pr_iid': parsed.pr_iid,
            'scm_type': parsed.scm_type.value,
            'pr_tracked': True,
            'project_path': '',
            'ide': '',
        }
        write_pinned_session_entry(tag, self._pinned_sessions[tag])
        self._show_status(f"Added row '{tag}' from PR: {details.source_branch}")
        self._refresh_and_show_row(tag)
        self._start_tracking(tag)

    def _add_row_from_project_url(self, parsed: ParsedProjectUrl) -> None:
        """Add a row from a plain project URL (clone + open server)."""
        # Refine UNKNOWN type using saved provider configs
        scm_type = refine_scm_type(parsed.host_url, parsed.scm_type)

        # Warn if no matching provider (clone will be unauthenticated)
        if scm_type == SCMType.UNKNOWN and self._scm_providers:
            reply = QMessageBox.question(
                self, 'Unknown Host',
                f"Could not match '{parsed.host_url}' to any connected "
                f"provider (GitLab/GitHub/Bitbucket).\n\n"
                f"The clone will be unauthenticated and may fail on "
                f"private repos.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        project_name = parsed.project_path.rsplit('/', 1)[-1]
        context_lines = [
            f"Project: {parsed.project_path}",
            f"Host: {parsed.host_url}",
        ]
        if parsed.commit:
            context_lines.append(f"Commit: {parsed.commit}")
        tag = self._ask_tag(context_lines)
        if not tag:
            return

        self._pinned_sessions[tag] = {
            'tag': tag,
            'remote_project_path': parsed.project_path,
            'host_url': parsed.host_url,
            'scm_type': scm_type.value,
            'branch': '',
            'commit': parsed.commit or '',
            'project_path': '',
            'ide': '',
        }
        write_pinned_session_entry(tag, self._pinned_sessions[tag])
        commit_suffix = f" @ {parsed.commit[:8]}" if parsed.commit else ""
        self._show_status(f"Added row '{tag}' from project: {project_name}{commit_suffix}")

        # Start before refresh — _start_server populates _starting_tags,
        # which keeps the row alive across the merge in _refresh_and_show_row.
        self._start_server(tag)
        self._refresh_and_show_row(tag)

    def _add_row_from_local(self) -> None:
        """Add a row from a local directory path."""
        dlg = AddLocalDialog(self)
        if dlg.exec_() != AddLocalDialog.Accepted:
            return

        local_path = dlg.selected_path()
        if not local_path:
            QMessageBox.warning(self, 'No Path', 'No path was entered.')
            return

        path = Path(local_path)
        if not path.is_dir():
            QMessageBox.warning(self, 'Not a Directory', f"'{local_path}' is not a directory.")
            return

        if dlg.is_clone_mode():
            # Clone mode: need git remote info to clone from
            remote_info = get_git_remote_info(str(path))
            if not remote_info:
                QMessageBox.warning(
                    self, 'No Git Remote',
                    'Could not determine Git remote info from this directory.\n'
                    'Make sure it is a Git repository with a remote.',
                )
                return

            # Refine UNKNOWN type using saved provider configs
            scm_type = refine_scm_type(remote_info.host_url, remote_info.scm_type)

            tag = self._ask_tag([
                f"Project: {remote_info.project_path}",
                f"From: {local_path}",
                "Mode: Clone to repos dir",
            ])
            if not tag:
                return

            self._pinned_sessions[tag] = {
                'tag': tag,
                'remote_project_path': remote_info.project_path,
                'host_url': remote_info.host_url,
                'scm_type': scm_type.value,
                'branch': '',
                'project_path': '',
                'ide': '',
            }
            write_pinned_session_entry(tag, self._pinned_sessions[tag])
            self._show_status(f"Added row '{tag}' (clone from {remote_info.project_path})")

            # Start before refresh — see _add_row_from_project_url for why.
            self._start_server(tag)
            self._refresh_and_show_row(tag)
        else:
            # Open directly mode
            tag = self._ask_tag([
                f"Path: {local_path}",
                "Mode: Open directly",
            ])
            if not tag:
                return

            self._pinned_sessions[tag] = {
                'tag': tag,
                'project_path': str(path),
                'ide': '',
            }
            write_pinned_session_entry(tag, self._pinned_sessions[tag])
            self._show_status(f"Added row '{tag}' from local path: {path.name}")

            # Start before refresh — see _add_row_from_resume for why.
            self._start_server(tag)
            self._refresh_and_show_row(tag)

    def _add_row_from_resume(self) -> None:
        """Pick a recorded session and hand off to ``leap --resume`` in a terminal.

        Two GUI responsibilities only — selection and the up-front
        already-running check.  The rest (cwd choice for cwd-bound
        CLIs, tag rename, provider hand-off) happens in the terminal
        we spawn so the user can answer interactive prompts.  The
        monitor row appears via auto-discovery once the server starts.
        """
        if not ResumeSessionDialog.has_resumable_sessions(STORAGE_DIR):
            QMessageBox.information(
                self, 'No Resumable Sessions',
                'No resumable sessions found.\n\n'
                'Run a CLI through Leap at least once - new sessions '
                'are recorded automatically and will appear here next '
                'time.',
            )
            return

        dlg = ResumeSessionDialog(STORAGE_DIR, self)
        if dlg.exec_() != ResumeSessionDialog.Accepted:
            return
        picked = dlg.selected_session()
        if not picked:
            return
        cli, original_tag, sess = picked

        # A session can't be loaded twice, so resuming one that's already
        # live is off the table — but rather than dead-ending, offer to
        # jump to the running session's terminal (mirrors leap-resume.py).
        # Ownership rule: a session counts as owned by a live tag when
        # (a) the tag has a live server (server_pid set), (b) that
        # server's cli_provider matches the recorded cli, and (c) the
        # session is the newest one recorded for (cli, tag).
        live_clis: dict[str, Any] = {
            s['tag']: s.get('cli_provider')
            for s in self.sessions
            if s.get('server_pid') is not None
        }
        owners: list[tuple[str, str]] = []
        for r in load_raw_tag_rows(STORAGE_DIR):
            if live_clis.get(r.tag) != r.cli:
                continue
            if r.sessions and r.sessions[0].session_id == sess.session_id:
                owners.append((r.cli, r.tag))
        if owners:
            # Jump target is the first owning tag — for today's CLIs a
            # session can only be live under one tag, so the list is a
            # single entry in practice.
            owner_tag = owners[0][1]
            tags_str = ', '.join(
                f"'{t}' ({get_display_name(c)})" for c, t in owners)
            reply = QMessageBox.question(
                self, 'Session Already Running',
                f"This CLI session is already running under Leap tag "
                f"{tags_str}.\n\n"
                f"Jump to it?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                # Same "Jump to server terminal" path the Terminal button
                # uses — navigates to the live session's terminal tab.
                self._focus_session(owner_tag, 'server')
            return

        self._show_status(
            f"Resuming [{get_display_name(cli)}] '{original_tag}' "
            f"(session {sess.session_id[:8]}) - see the new terminal"
        )
        # Mark the row as "starting" so the dead-pinned row sticks
        # around (escape hatch in ``_merge_sessions``) and renders the
        # disabled "Starting…" Server button while the IDE cold-starts
        # / poll runs.  The ``_starting_tags -= alive`` sweep in
        # ``_update_table`` clears this the moment the new leap server
        # creates its socket — so a successful resume falls back to the
        # normal live-row rendering without an extra timer firing.  The
        # 12-min safety timer is a backstop for the cold-IDE path:
        # ``_open_jetbrains_terminal`` polls for up to 10 min, plus a
        # buffer for the actual leap server boot.  Without this guard
        # a user who Ctrl+C's the terminal mid-cold-start would see
        # the row hang in "Starting…" forever (or until restart).
        self._starting_tags.add(original_tag)
        QTimer.singleShot(
            720_000, lambda t=original_tag: self._starting_tags.discard(t),
        )
        self._update_table()
        # The terminal opens at the user's default cwd; for cwd-bound
        # CLIs (Claude/Gemini/Cursor), leap-resume.py will then prompt
        # the user to pick "Original" (chdir into the recorded cwd) or
        # "Current" (relocate the transcript into the current cwd).
        # When the dialog's "Open in last app" toggle is on AND we have
        # a recorded terminal app for this session, route through that
        # so the resume lands in (say) iTerm2 even if the user's global
        # default is Terminal.app.  Otherwise fall back to the global
        # default; ``open_terminal_with_command`` handles the empty case
        # by stepping through its own fallback chain.
        if dlg.open_in_last_app and sess.terminal_app:
            preferred_ide = sess.terminal_app
        else:
            preferred_ide = self._prefs.get('default_terminal')
        # ``recorded_cwd`` + ``recorded_project_path`` are consumed by
        # ``open_resume_in_terminal`` when the resolved target is an
        # IDE.  Mirrors Move-to-IDE: IDE is opened at the project root
        # (so subdirs with their own .idea — e.g. ``proto/`` inside
        # ``tenant-manager/`` — don't get treated as separate projects),
        # while the terminal inside the IDE cds to the recorded subdir.
        # For plain terminals both are ignored downstream.
        self._server_launcher.open_resume_in_terminal(
            cli=cli, tag=original_tag, session_id=sess.session_id,
            preferred_ide=preferred_ide,
            recorded_cwd=sess.cwd or None,
            recorded_project_path=sess.project_path or None,
        )

    # ------------------------------------------------------------------
    #  Shared helpers for add-row flows
    # ------------------------------------------------------------------

    def _ask_tag(self, context_lines: list[str]) -> Optional[str]:
        """Ask user for a session tag with validation loop.

        Args:
            context_lines: Lines to display above the tag prompt.

        Returns:
            The validated tag, or None if cancelled.
        """
        context = '\n'.join(context_lines)
        prev_tag = ''
        while True:
            dlg = QInputDialog(self)
            dlg.setWindowTitle('Session Tag')
            dlg.setLabelText(f"{context}\n\nTag for this Leap session:")
            dlg.setTextValue(prev_tag)
            ok = dlg.exec_() == QInputDialog.Accepted
            tag = dlg.textValue()
            if not ok or not tag.strip():
                return None
            tag = tag.strip()
            prev_tag = tag

            if not is_valid_tag(tag):
                QMessageBox.warning(
                    self, 'Invalid Tag',
                    'Tag must contain only letters, numbers, hyphens, and underscores.',
                )
                continue

            if tag in self._pinned_sessions:
                QMessageBox.information(
                    self, 'Already Added',
                    f"A row with tag '{tag}' already exists.",
                )
                continue
            return tag

    def _refresh_and_show_row(self, tag: str) -> None:
        """Refresh sessions and table to show a newly added row."""
        self.sessions = self._merge_sessions(
            [s for s in self.sessions if s.get('server_pid') is not None]
        )
        self._update_table()
