"""MR tracking, SCM polling, thread sending, and add-row methods."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from PyQt5.QtWidgets import QApplication, QInputDialog, QMessageBox
from PyQt5.QtCore import Qt

from claudeq.utils.constants import is_valid_tag
from claudeq.monitor.mr_tracking.base import MRState, MRStatus
from claudeq.monitor.mr_tracking.config import (
    load_gitlab_config, load_pinned_sessions, save_pinned_sessions,
)
from claudeq.monitor.mr_tracking.git_utils import SCMType, get_git_remote_info, parse_mr_url
from claudeq.monitor.scm_polling import (
    BackgroundCallWorker, CollectThreadsWorker, SCMOneShotWorker,
    SCMPollerWorker, SendThreadsCombinedWorker, SendThreadsWorker,
)

if TYPE_CHECKING:
    from claudeq.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object

logger = logging.getLogger(__name__)


class MRTrackingMixin(_Base):
    """Methods for MR tracking, SCM polling, thread sending, and add-row."""

    def _auto_track_mr_pinned(self) -> None:
        """Auto-reconnect MR tracking for sessions that were tracked last time."""
        if not self._scm_providers:
            return
        for tag, pin in self._pinned_sessions.items():
            if pin.get('mr_tracked') and tag not in self._tracked_tags:
                self._start_tracking(tag, _silent=True)

    def _start_tracking(self, tag: str, _silent: bool = False) -> None:
        """Start MR tracking for a session via a background one-shot check."""
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
                    'Connect to GitLab or GitHub first using the buttons at the bottom.'
                )
            else:
                QMessageBox.information(
                    self, 'No Provider Match',
                    'No configured SCM provider matches this project\'s git remote.\n'
                    'Connect the appropriate provider (GitLab/GitHub) first.'
                )
            return

        # Resolve project path and branch for the SCM query.
        # MR-pinned rows have remote_project_path/branch stored directly;
        # active sessions resolve from the local git remote.
        # Prefer mr_branch (pinned MR branch) over the live branch.
        remote_project = session.get('remote_project_path')
        branch = session.get('mr_branch') or session.get('branch')

        if remote_project and branch and branch != 'N/A':
            # Use pinned MR data directly (no local repo needed)
            scm_project_path = remote_project
            scm_branch = branch
            # Store context for enriching pinned session on result
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
                        self, 'No MR Found', 'No project path for this session.'
                    )
                return

            remote_info = get_git_remote_info(project_path)
            if not remote_info:
                if not _silent:
                    QMessageBox.information(
                        self, 'No MR Found', 'Could not determine Git remote info.'
                    )
                return
            scm_project_path = remote_info.project_path
            scm_branch = remote_info.branch
            # Store context for enriching pinned session on result
            self._pending_tracking_context[tag] = {
                'remote_project_path': scm_project_path,
                'host_url': remote_info.host_url,
                'scm_type': remote_info.scm_type.value if hasattr(remote_info.scm_type, 'value') else str(remote_info.scm_type),
                'branch': scm_branch,
            }

        # Show "Checking..." while the API call runs in the background
        if _silent:
            self._silent_tracking_tags.add(tag)
        self._show_status(f"Checking MR for '{tag}'...")
        self._checking_tags.add(tag)
        self._set_busy(True)
        self._update_table()

        # Run the API call in a background thread
        worker = SCMOneShotWorker(self)
        worker.configure(provider, tag, scm_project_path, scm_branch)
        worker.result_ready.connect(self._on_tracking_result)
        worker.error.connect(self._on_tracking_error)
        worker.finished.connect(worker.deleteLater)
        self._scm_oneshot_worker = worker
        worker.start()

    def _stop_tracking(self, tag: str, _skip_prompt: bool = False) -> None:
        """Stop MR tracking for a session.

        Args:
            tag: Session tag.
            _skip_prompt: If True, skip the confirmation prompt for dead rows
                (used when called from _remove_pinned_session which has its own).
        """
        # If server is dead, warn that stopping tracking will remove the row
        if not _skip_prompt:
            session = next((s for s in self.sessions if s['tag'] == tag), None)
            if session and session.get('server_pid') is None:
                reply = QMessageBox.question(
                    self, 'Stop MR Tracking',
                    f"The server for '{tag}' is not running.\n"
                    f"Stopping MR tracking will remove this row.\n\nContinue?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return

        if tag in self._tracked_tags:
            self._show_status(f"Stopped MR tracking for '{tag}'")
        self._tracked_tags.discard(tag)
        self._checking_tags.discard(tag)
        self._silent_tracking_tags.discard(tag)
        self._mr_statuses.pop(tag, None)
        self._mr_widgets.pop(tag, None)
        self._mr_approval_widgets.pop(tag, None)
        self._pending_tracking_context.pop(tag, None)
        self._dock_badge.discard_tag(tag)

        # Persist tracking-off so auto-reconnect won't re-track on next startup
        pin = self._pinned_sessions.get(tag)
        if pin and pin.get('mr_tracked'):
            pin['mr_tracked'] = False
            save_pinned_sessions(self._pinned_sessions)

        # If server is dead, remove the row entirely
        session = next((s for s in self.sessions if s['tag'] == tag), None)
        is_dead = session and session.get('server_pid') is None
        if is_dead and not _skip_prompt:
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
            save_pinned_sessions(self._pinned_sessions)
            self._deleted_tags.add(tag)
            self.sessions = [s for s in self.sessions if s['tag'] != tag]

        # Stop poll timer if no tags are being tracked and no notifications enabled
        if (not self._tracked_tags
                and not self._get_notif_scm_types()
                and self._scm_poll_timer.isActive()):
            self._scm_poll_timer.stop()

        self._update_table()
        self._update_dock_badge()

    def _on_tracking_result(self, tag: str, status: MRStatus) -> None:
        """Handle the result of a one-shot MR check."""
        self._set_busy(False)
        self._checking_tags.discard(tag)
        silent = tag in self._silent_tracking_tags
        self._silent_tracking_tags.discard(tag)

        # Row was deleted while the check was in-flight — discard result
        if tag in self._deleted_tags:
            self._pending_tracking_context.pop(tag, None)
            return

        if status.state == MRState.NO_MR:
            self._pending_tracking_context.pop(tag, None)
            if silent:
                self._show_status(f"Auto-reconnect: no open MR found for '{tag}'")
            self._remove_dead_untracked_row(tag)
            self._update_table()
            if not silent:
                QMessageBox.information(
                    self, 'No MR Found',
                    'No open merge request found for this branch.'
                )
            return

        # MR found — promote to tracked and enrich pinned session
        ctx = self._pending_tracking_context.pop(tag, None)
        if ctx:
            pin = self._pinned_sessions.get(tag, {})
            pin.update({
                'remote_project_path': ctx['remote_project_path'],
                'host_url': ctx['host_url'],
                'scm_type': ctx['scm_type'],
                'branch': ctx['branch'],
                'mr_title': status.mr_title or '',
                'mr_url': status.mr_url or '',
                'mr_tracked': True,
            })
            self._pinned_sessions[tag] = pin
            save_pinned_sessions(self._pinned_sessions)
        else:
            # No context but MR found (e.g. auto-reconnect) — persist flag
            pin = self._pinned_sessions.get(tag)
            if pin and not pin.get('mr_tracked'):
                pin['mr_tracked'] = True
                save_pinned_sessions(self._pinned_sessions)

        self._show_status(f"MR found for '{tag}' — tracking started")
        self._tracked_tags.add(tag)
        self._mr_statuses[tag] = status
        self._update_table()
        self._update_dock_badge()

        if not self._scm_poll_timer.isActive():
            self._scm_poll_timer.start(self._get_poll_interval() * 1000)

    def _on_tracking_error(self, tag: str, message: str) -> None:
        """Handle an error from a one-shot MR check."""
        self._set_busy(False)
        self._checking_tags.discard(tag)
        silent = tag in self._silent_tracking_tags
        self._silent_tracking_tags.discard(tag)
        self._pending_tracking_context.pop(tag, None)

        # Row was deleted while the check was in-flight — discard error
        if tag in self._deleted_tags:
            return
        if silent:
            self._remove_dead_untracked_row(tag)
        self._update_table()
        self._show_status(f"MR tracking error for '{tag}': {message}")
        if not silent:
            QMessageBox.warning(self, 'Error', message)

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
                logger.warning("SCM poll stuck for %.0fs, force-resetting", elapsed)
                self._show_status(f"SCM poll stuck for {elapsed:.0f}s — force-reset")
                self._scm_polling = False
                if self._scm_worker:
                    try:
                        self._scm_worker.results_ready.disconnect()
                        self._scm_worker.notifications_ready.disconnect()
                        self._scm_worker.finished.disconnect()
                    except (TypeError, RuntimeError):
                        pass  # Already disconnected or deleted
                    self._scm_worker = None
            else:
                return

        has_tracked = bool(self._tracked_tags)
        notif_types = self._get_notif_scm_types()
        if not has_tracked and not notif_types:
            return

        tracked_sessions = [s for s in self.sessions if s['tag'] in self._tracked_tags]
        if has_tracked and not tracked_sessions:
            if not notif_types:
                logger.debug("SCM poll skipped: no tracked sessions found in active sessions")
                return

        logger.debug("Starting SCM poll for tags: %s (notif=%s)",
                      [s['tag'] for s in tracked_sessions], notif_types)
        self._scm_polling = True
        self._scm_poll_started_at = time.monotonic()
        worker = SCMPollerWorker(self)
        worker.configure(
            self._scm_providers, tracked_sessions,
            auto_fetch_cq=self._prefs.get('auto_fetch_cq', True),
            notif_scm_types=notif_types,
        )
        worker.results_ready.connect(self._on_scm_results)
        worker.notifications_ready.connect(self._on_notifications_received)
        worker.notification_auth_error.connect(self._on_notification_auth_error)
        worker.finished.connect(self._on_scm_worker_finished)
        self._scm_worker = worker
        worker.start()

    def _on_scm_worker_finished(self) -> None:
        """Clean up after poller worker completes."""
        logger.debug("SCM poll worker finished")
        self._scm_polling = False
        if self._scm_worker:
            self._scm_worker.deleteLater()
            self._scm_worker = None

    def _on_scm_results(self, results: dict[str, MRStatus]) -> None:
        """Handle SCM poll results (runs in main thread via signal)."""
        if self._shutting_down:
            return
        try:
            if not self.isVisible():
                return
            for tag, status in results.items():
                logger.debug("SCM result: tag=%s state=%s unresponded=%s approved=%s",
                             tag, status.state.value, status.unresponded_count, status.approved)
            self._mr_statuses.update(results)
            self._update_mr_column()
            self._update_dock_badge()
        except Exception:
            logger.exception("Error handling SCM results")

    # ------------------------------------------------------------------
    #  Thread sending
    # ------------------------------------------------------------------

    def _send_all_threads_to_cq(self, tag: str) -> None:
        """Send all unresponded MR threads to the CQ session (non-blocking).

        Phase 1 (CollectThreadsWorker): resolve provider, collect threads, match sessions.
        Phase 2 (SendThreadsWorker): send each thread to CQ and acknowledge on SCM.
        """
        if not self._scm_providers:
            return

        # Guard against concurrent runs
        if (self._collect_threads_worker and self._collect_threads_worker.isRunning()) or \
           (self._send_threads_worker and self._send_threads_worker.isRunning()) or \
           (self._send_combined_worker and self._send_combined_worker.isRunning()):
            QMessageBox.information(
                self, 'In Progress',
                'Already sending threads — please wait.'
            )
            return

        session = next((s for s in self.sessions if s['tag'] == tag), None)
        if not session:
            return

        project_path = session.get('project_path')
        if not project_path:
            return

        # Launch Phase 1 — everything runs in background
        self._cq_only_collect = False
        self._set_busy(True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._collect_threads_worker = CollectThreadsWorker(self)
        self._collect_threads_worker.configure(
            project_path, self._scm_providers, self.sessions
        )
        self._collect_threads_worker.collected.connect(self._on_threads_collected)
        self._collect_threads_worker.error.connect(self._on_send_threads_error)
        self._collect_threads_worker.start()

    def _on_threads_collected(self, commands: list, matching_tags: list) -> None:
        """Handle Phase 1 completion: show dialog if needed, then launch Phase 2."""
        provider = self._collect_threads_worker.provider if self._collect_threads_worker else None

        if not commands or not provider:
            self._set_busy(False)
            QApplication.restoreOverrideCursor()
            QMessageBox.information(
                self, 'No Threads',
                "No threads with '/cq' comment found." if self._cq_only_collect
                else 'No unresponded threads found.'
            )
            return

        if not matching_tags:
            self._set_busy(False)
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(
                self, 'No Session',
                'No matching CQ session found for this project.'
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
        self._send_threads_worker = SendThreadsWorker(self)
        self._send_threads_worker.configure(provider, commands, matched_tag)
        self._send_threads_worker.finished.connect(self._on_send_threads_finished)
        self._send_threads_worker.error.connect(self._on_send_threads_error)
        self._send_threads_worker.start()

    def _on_send_threads_finished(self, sent_count: int, matched_tag: str) -> None:
        """Handle Phase 2 completion."""
        self._set_busy(False)
        QApplication.restoreOverrideCursor()
        if sent_count > 0:
            self._show_status(f"Sent {sent_count} thread(s) to '{matched_tag}'")
            QMessageBox.information(
                self, 'Threads Sent',
                f"Sent {sent_count} thread(s) to session '{matched_tag}'."
            )
            self._start_scm_poll()
        else:
            QMessageBox.information(
                self, 'No Threads',
                "No threads with '/cq' comment found." if self._cq_only_collect
                else 'No unresponded threads found.'
            )

    def _on_send_threads_error(self, message: str) -> None:
        """Handle error from either background worker."""
        self._set_busy(False)
        QApplication.restoreOverrideCursor()
        self._show_status(f"Thread send error: {message}")
        QMessageBox.warning(self, 'Error', message)

    def _send_all_threads_combined_to_cq(self, tag: str) -> None:
        """Send all unresponded MR threads as one concatenated message (non-blocking).

        Reuses Phase 1 (CollectThreadsWorker) then sends a single combined message.
        """
        if not self._scm_providers:
            return

        # Guard against concurrent runs
        if (self._collect_threads_worker and self._collect_threads_worker.isRunning()) or \
           (self._send_threads_worker and self._send_threads_worker.isRunning()) or \
           (self._send_combined_worker and self._send_combined_worker.isRunning()):
            QMessageBox.information(
                self, 'In Progress',
                'Already sending threads — please wait.'
            )
            return

        session = next((s for s in self.sessions if s['tag'] == tag), None)
        if not session:
            return

        project_path = session.get('project_path')
        if not project_path:
            return

        # Launch Phase 1 — collection runs in background
        self._cq_only_collect = False
        self._set_busy(True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._collect_threads_worker = CollectThreadsWorker(self)
        self._collect_threads_worker.configure(
            project_path, self._scm_providers, self.sessions
        )
        self._collect_threads_worker.collected.connect(self._on_threads_collected_combined)
        self._collect_threads_worker.error.connect(self._on_send_threads_error)
        self._collect_threads_worker.start()

    def _on_threads_collected_combined(self, commands: list, matching_tags: list) -> None:
        """Handle Phase 1 completion for combined send."""
        provider = self._collect_threads_worker.provider if self._collect_threads_worker else None

        if not commands or not provider:
            self._set_busy(False)
            QApplication.restoreOverrideCursor()
            QMessageBox.information(
                self, 'No Threads',
                "No threads with '/cq' comment found." if self._cq_only_collect
                else 'No unresponded threads found.'
            )
            return

        if not matching_tags:
            self._set_busy(False)
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(
                self, 'No Session',
                'No matching CQ session found for this project.'
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

        # Launch Phase 2 — send combined message
        self._send_combined_worker = SendThreadsCombinedWorker(self)
        self._send_combined_worker.configure(provider, commands, matched_tag)
        self._send_combined_worker.finished.connect(self._on_send_combined_finished)
        self._send_combined_worker.error.connect(self._on_send_threads_error)
        self._send_combined_worker.start()

    def _on_send_combined_finished(self, thread_count: int, matched_tag: str) -> None:
        """Handle combined send completion."""
        self._set_busy(False)
        QApplication.restoreOverrideCursor()
        if thread_count > 0:
            self._show_status(f"Sent {thread_count} thread(s) combined to '{matched_tag}'")
            QMessageBox.information(
                self, 'Threads Sent',
                f"Sent {thread_count} thread(s) as one message to session '{matched_tag}'."
            )
            self._start_scm_poll()
        else:
            QMessageBox.information(
                self, 'No Threads',
                "No threads with '/cq' comment found." if self._cq_only_collect
                else 'No unresponded threads found.'
            )

    def _send_cq_threads_to_cq(self, tag: str) -> None:
        """Send only /cq-marked threads to CQ (one per queue message)."""
        self._send_cq_threads_common(tag, combined=False)

    def _send_cq_threads_combined_to_cq(self, tag: str) -> None:
        """Send only /cq-marked threads to CQ (combined into one message)."""
        self._send_cq_threads_common(tag, combined=True)

    def _send_cq_threads_common(self, tag: str, combined: bool) -> None:
        """Shared launcher for /cq-only thread sending."""
        if not self._scm_providers:
            return

        # Guard against concurrent runs
        if (self._collect_threads_worker and self._collect_threads_worker.isRunning()) or \
           (self._send_threads_worker and self._send_threads_worker.isRunning()) or \
           (self._send_combined_worker and self._send_combined_worker.isRunning()):
            QMessageBox.information(
                self, 'In Progress',
                'Already sending threads — please wait.'
            )
            return

        session = next((s for s in self.sessions if s['tag'] == tag), None)
        if not session:
            return

        project_path = session.get('project_path')
        if not project_path:
            return

        self._cq_only_collect = True
        self._set_busy(True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._collect_threads_worker = CollectThreadsWorker(self)
        self._collect_threads_worker.configure(
            project_path, self._scm_providers, self.sessions, cq_only=True
        )
        if combined:
            self._collect_threads_worker.collected.connect(self._on_threads_collected_combined)
        else:
            self._collect_threads_worker.collected.connect(self._on_threads_collected)
        self._collect_threads_worker.error.connect(self._on_send_threads_error)
        self._collect_threads_worker.start()

    # ------------------------------------------------------------------
    #  Add row from MR/PR URL
    # ------------------------------------------------------------------

    def _add_row(self) -> None:
        """Add a monitored row from an MR/PR URL (no git operations)."""
        if not self._scm_providers:
            QMessageBox.information(
                self, 'No SCM Connected',
                'Connect to GitLab or GitHub first using the buttons at the bottom.',
            )
            return

        prev_url = ''
        while True:
            dlg = QInputDialog(self)
            dlg.setWindowTitle('Add from MR')
            dlg.setLabelText('MR / PR URL:')
            dlg.setTextValue(prev_url)
            dlg.resize(800, dlg.sizeHint().height())
            ok = dlg.exec_() == QInputDialog.Accepted
            url = dlg.textValue()
            if not ok or not url.strip():
                return
            prev_url = url.strip()

            gitlab_config = load_gitlab_config()
            parsed = parse_mr_url(prev_url, gitlab_config)
            if not parsed:
                QMessageBox.warning(self, 'Invalid URL', 'Could not parse the MR/PR URL.')
                continue

            provider = self._scm_providers.get(parsed.scm_type.value)
            if not provider:
                QMessageBox.warning(
                    self, 'No Provider',
                    f'No connected provider for {parsed.scm_type.value}.',
                )
                continue
            break

        # Fetch MR details in the background
        self._set_busy(True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        result_holder: list[Optional[Any]] = [None]

        def _fetch() -> None:
            result_holder[0] = provider.get_mr_details(parsed.project_path, parsed.mr_iid)

        worker = BackgroundCallWorker(_fetch, self)
        worker.finished.connect(lambda: self._on_add_row_details(
            parsed, result_holder,
        ))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_add_row_details(self, parsed: Any, result_holder: list) -> None:
        """Handle MR details fetched — ask for tag and pin the row."""
        self._set_busy(False)
        QApplication.restoreOverrideCursor()
        details = result_holder[0]
        if not details:
            QMessageBox.warning(self, 'MR Not Found', 'Could not fetch MR/PR details.')
            return

        if details.source_branch_deleted:
            QMessageBox.warning(
                self, 'Branch Deleted',
                f"The source branch '{details.source_branch}' no longer exists "
                f"on the remote.\n\n"
                f"This usually means the MR/PR has been merged and the branch "
                f"was deleted.\n\n"
                f"The row cannot be added to the monitor.",
            )
            return

        prev_tag = ''
        while True:
            dlg = QInputDialog(self)
            dlg.setWindowTitle('Session Tag')
            dlg.setLabelText(
                f"MR: {details.mr_title}\nBranch: {details.source_branch}\n\n"
                f"Tag for this CQ session:"
            )
            dlg.setTextValue(prev_tag)
            ok = dlg.exec_() == QInputDialog.Accepted
            tag = dlg.textValue()
            if not ok or not tag.strip():
                return
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
            break

        # Pin the session with remote info and auto-start MR tracking
        self._pinned_sessions[tag] = {
            'tag': tag,
            'remote_project_path': parsed.project_path,
            'host_url': parsed.host_url,
            'branch': details.source_branch,
            'mr_title': details.mr_title,
            'mr_url': details.mr_url,
            'scm_type': parsed.scm_type.value,
            'project_path': '',
            'ide': '',
        }
        save_pinned_sessions(self._pinned_sessions)
        self._show_status(f"Added row '{tag}' from MR: {details.source_branch}")

        # Refresh table to show the new row
        self.sessions = self._merge_sessions(
            [s for s in self.sessions if s.get('server_pid') is not None]
        )
        self._update_table()

        # Auto-start MR tracking for rows added via "+"
        self._start_tracking(tag)
