"""Session lifecycle methods — merge, navigate, close, delete."""

from __future__ import annotations

import logging
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from PyQt5.QtWidgets import QMessageBox

from claudeq.utils.constants import SOCKET_DIR
from claudeq.utils.socket_utils import send_socket_request
from claudeq.monitor.session_manager import (
    is_client_lock_held, load_session_metadata, read_client_pid, session_exists,
)
from claudeq.monitor.mr_tracking.config import save_pinned_sessions
from claudeq.monitor.scm_polling import BackgroundCallWorker
from claudeq.monitor.monitor_utils import _remove_client_lock
from claudeq.monitor.navigation import (
    close_terminal_with_title, find_terminal_with_title, open_terminal_with_command,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from claudeq.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object


class SessionMixin(_Base):
    """Methods for session merging, navigation, close, delete, and server start."""

    def _merge_sessions(self, active_sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge active sessions with pinned sessions.

        Auto-pins every discovered active session. For pinned tags without a
        running server, creates a "dead" row dict with server_pid=None.

        Args:
            active_sessions: Sessions returned by get_active_sessions().

        Returns:
            Merged session list sorted by tag.
        """
        active_by_tag = {s['tag']: s for s in active_sessions}
        changed = False

        # Auto-pin all active sessions (skip explicitly deleted ones).
        # Merge with existing pin data to preserve MR-pinned fields
        # (remote_project_path, host_url, scm_type, etc.).
        for s in active_sessions:
            tag = s['tag']
            if tag in self._deleted_tags:
                continue
            existing = self._pinned_sessions.get(tag, {})
            # For MR-pinned rows, preserve the MR branch as source of truth
            # so we can detect when the local branch drifts.
            is_mr_pinned = bool(existing.get('remote_project_path'))
            pin_data = {**existing,
                'tag': tag,
                'project_path': s.get('project_path') or '',
                'ide': s.get('ide') or '',
                'branch': (
                    existing.get('branch', '')
                    if is_mr_pinned
                    else s.get('branch') or ''
                ),
            }
            if self._pinned_sessions.get(tag) != pin_data:
                self._pinned_sessions[tag] = pin_data
                changed = True

        if changed:
            save_pinned_sessions(self._pinned_sessions)

        # Prune deleted tags that are no longer active (server fully gone)
        self._deleted_tags -= self._deleted_tags - set(active_by_tag.keys())

        # Build merged list — auto-remove dead rows without MR tracking
        tags_to_remove: list[str] = []
        merged: list[dict[str, Any]] = []
        for tag, pin in self._pinned_sessions.items():
            if tag in active_by_tag:
                session = active_by_tag[tag]
                # Enrich active sessions with pinned SCM data (MR-pinned rows)
                for key in ('remote_project_path', 'host_url', 'scm_type'):
                    if pin.get(key) and not session.get(key):
                        session[key] = pin[key]
                # For MR-pinned rows, store the MR branch so the poller uses
                # it instead of the live branch (which may have drifted).
                if pin.get('remote_project_path') and pin.get('branch'):
                    session['mr_branch'] = pin['branch']
                merged.append(session)
            elif not (pin.get('remote_project_path')
                      or tag in self._tracked_tags
                      or tag in self._checking_tags):
                # Dead row with no MR tracking — schedule for removal
                tags_to_remove.append(tag)
                continue
            else:
                # Dead row — check for orphaned client
                has_client = is_client_lock_held(tag)
                client_pid = read_client_pid(tag) if has_client else None
                # For MR-pinned rows, derive project name from remote path
                if pin.get('remote_project_path'):
                    project_name = pin['remote_project_path'].rsplit('/', 1)[-1]
                else:
                    project_name = pin.get('project_path', '').rsplit('/', 1)[-1] or 'N/A'
                pinned_branch = pin.get('branch') or 'N/A'
                merged.append({
                    'tag': tag,
                    'queue_size': 0,
                    'project': project_name,
                    'branch': pinned_branch,
                    'mr_branch': pinned_branch if pin.get('remote_project_path') else None,
                    'project_path': pin.get('project_path') or None,
                    'ide': pin.get('ide') or None,
                    'server_pid': None,
                    'client_pid': client_pid,
                    'has_client': has_client,
                    # Pass through pinned SCM data for MR-pinned rows
                    'remote_project_path': pin.get('remote_project_path'),
                    'host_url': pin.get('host_url'),
                    'scm_type': pin.get('scm_type'),
                })

        # Remove dead rows without MR tracking
        if tags_to_remove:
            for tag in tags_to_remove:
                self._pinned_sessions.pop(tag, None)
            save_pinned_sessions(self._pinned_sessions)

        # Include any active sessions not yet pinned (shouldn't happen, but safe)
        pinned_tags = set(self._pinned_sessions.keys())
        for s in active_sessions:
            if s['tag'] not in pinned_tags:
                merged.append(s)

        return sorted(merged, key=lambda x: x['tag'])

    def _focus_session(self, tag: str, session_type: str = 'server') -> None:
        """Focus or open the terminal for a session (non-blocking).

        Runs navigation subprocess calls in a background thread to avoid
        blocking the UI.

        For navigation (finding existing terminal): uses the server's IDE
        from metadata so we look in the right app.
        For opening a NEW server: uses default_terminal from settings.
        For opening a NEW client: uses the server's IDE so it opens beside
        the server in the same app.
        """
        metadata = load_session_metadata(tag)
        # IDE the server is currently running in (for navigation & client open)
        server_ide = metadata.get('ide') if metadata else None
        preferred_ide = server_ide
        project_path = metadata.get('project_path') if metadata else None
        title_pattern = f"cq-{session_type} {tag}"

        if not session_exists(tag, session_type):
            ext = 'client.lock' if session_type == 'client' else 'sock'
            check_path = SOCKET_DIR / f"{tag}.{ext}"
            try:
                dir_contents = sorted(os.listdir(str(SOCKET_DIR)))
            except OSError:
                dir_contents = ['<dir missing>']
            logger.debug(
                "Session not found: tag=%s type=%s path=%s exists=%s "
                "socket_dir=%s dir_contents=%s",
                tag, session_type, check_path, check_path.exists(),
                SOCKET_DIR, dir_contents,
            )
            self._show_status(
                f"Session not found: {session_type} '{tag}' "
                f"(checked {check_path}, dir has {len(dir_contents)} files)"
            )
            reply = QMessageBox.question(
                self,
                f'{session_type.capitalize()} Not Found',
                f'{session_type.capitalize()} not found for: {tag}\n\n'
                f'Open a new {session_type}?',
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                if session_type == 'server':
                    self._start_server(tag)
                else:
                    self._show_status(f"Opening new client for '{tag}'")
                    worker = BackgroundCallWorker(
                        lambda: open_terminal_with_command(
                            f"claudeq '{tag}'",
                            preferred_ide=preferred_ide,
                            project_path=project_path,
                        ),
                        self,
                    )
                    worker.finished.connect(worker.deleteLater)
                    worker.start()
            return

        # Use a result-capturing wrapper to get find_terminal_with_title's return value
        _tag = tag
        _session_type = session_type
        _preferred_ide = preferred_ide
        _project_path = project_path
        _title_pattern = title_pattern
        result_holder: list[Optional[bool]] = [None]

        def _do_find() -> None:
            result_holder[0] = find_terminal_with_title(
                _title_pattern, _preferred_ide, _project_path, _title_pattern
            )

        def _on_done() -> None:
            if result_holder[0]:
                return  # Successfully focused

            # Terminal not found — log details for diagnosing navigation issues
            logger.debug(
                "Terminal navigation failed: tag=%s type=%s preferred_ide=%s "
                "title_pattern=%s project_path=%s",
                _tag, _session_type, _preferred_ide, _title_pattern,
                _project_path,
            )
            self._show_status(
                f"Navigation failed for {_session_type} '{_tag}' "
                f"(ide={_preferred_ide}, pattern='{_title_pattern}')"
            )

            # Terminal not found — show dialog on main thread
            if _session_type == 'client' and is_client_lock_held(_tag):
                reply = QMessageBox.question(
                    self,
                    'Client Not Found',
                    f'A client is connected to \'{_tag}\' but its terminal '
                    f'could not be found.\n\n'
                    f'Replace it with a new client?',
                    QMessageBox.Yes | QMessageBox.No
                )
                if reply == QMessageBox.Yes:
                    _remove_client_lock(_tag)
                    w = BackgroundCallWorker(
                        lambda: open_terminal_with_command(
                            f"claudeq '{_tag}'",
                            preferred_ide=_preferred_ide,
                            project_path=_project_path,
                        ),
                        self,
                    )
                    w.finished.connect(w.deleteLater)
                    w.start()
            else:
                reply = QMessageBox.question(
                    self,
                    'Navigation Failed',
                    f'Could not find terminal tab for {_session_type}: {_tag}\n\n'
                    f'Open a new {_session_type}?',
                    QMessageBox.Yes | QMessageBox.No
                )
                if reply == QMessageBox.Yes:
                    w = BackgroundCallWorker(
                        lambda: open_terminal_with_command(
                            f"claudeq '{_tag}'",
                            preferred_ide=_preferred_ide,
                            project_path=_project_path,
                        ),
                        self,
                    )
                    w.finished.connect(w.deleteLater)
                    w.start()

        find_worker = BackgroundCallWorker(_do_find, self)
        find_worker.finished.connect(_on_done)
        find_worker.finished.connect(find_worker.deleteLater)
        find_worker.start()

    def _close_server(self, tag: str, server_pid: Optional[int]) -> None:
        """Close a server session (non-blocking).

        If the session has no MR tracking, warns that closing the server
        will remove the row.
        """
        # Check if this row will survive without a server
        pin = self._pinned_sessions.get(tag, {})
        has_mr = (
            pin.get('remote_project_path')
            or tag in self._tracked_tags
            or tag in self._checking_tags
        )
        if not has_mr:
            reply = QMessageBox.question(
                self, 'Close Server',
                f"'{tag}' has no MR tracking.\n"
                f"Closing the server will remove this row.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            # Pre-schedule removal — row will disappear on next refresh
            self._pinned_sessions.pop(tag, None)
            save_pinned_sessions(self._pinned_sessions)
            self._deleted_tags.add(tag)

            # Offer to close the client too
            session = next((s for s in self.sessions if s['tag'] == tag), None)
            if session and session.get('has_client', False):
                client_reply = QMessageBox.question(
                    self, 'Close Client',
                    f"A client is connected to '{tag}'.\n"
                    f"Do you also want to close the client?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if client_reply == QMessageBox.Yes:
                    self._close_client(tag, session.get('client_pid'))

        metadata = load_session_metadata(tag)
        preferred_ide = metadata.get('ide') if metadata else None
        project_path = metadata.get('project_path') if metadata else None

        def _do_close() -> None:
            socket_path = SOCKET_DIR / f"{tag}.sock"
            response = send_socket_request(socket_path, {'type': 'shutdown'}, timeout=3.0)
            if not (response and response.get('status') == 'ok'):
                if server_pid:
                    try:
                        os.kill(server_pid, signal.SIGTERM)
                    except OSError:
                        pass
            close_terminal_with_title(
                f"cq-server {tag}", preferred_ide, project_path, f"cq-server {tag}"
            )

        self._show_status(f"Closing server '{tag}'...")
        self._set_busy(True)
        worker = BackgroundCallWorker(_do_close, self)
        worker.finished.connect(lambda: self._set_busy(False))
        worker.finished.connect(lambda: self._show_status(f"Server '{tag}' closed"))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _close_client(self, tag: str, client_pid: Optional[int]) -> None:
        """Close a client session (non-blocking, no confirmation)."""
        if client_pid:
            try:
                os.kill(client_pid, signal.SIGTERM)
            except OSError:
                pass

        metadata = load_session_metadata(tag)
        preferred_ide = metadata.get('ide') if metadata else None
        project_path = metadata.get('project_path') if metadata else None

        self._show_status(f"Closing client '{tag}'...")
        self._set_busy(True)
        worker = BackgroundCallWorker(
            lambda: close_terminal_with_title(
                f"cq-client {tag}", preferred_ide, project_path, f"cq-client {tag}"
            ),
            self,
        )
        worker.finished.connect(lambda: self._set_busy(False))
        worker.finished.connect(lambda: self._show_status(f"Client '{tag}' closed"))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _start_server(self, tag: str) -> None:
        """Start a new server for a pinned (dead) row."""
        if tag in self._starting_tags:
            return  # Already launching — ignore duplicate click
        self._starting_tags.add(tag)
        self._update_table()  # Immediately show disabled "Starting..." button
        self._server_launcher.start_server(tag)

    def _delete_row(self, tag: str) -> None:
        """Delete a row, always prompting for confirmation."""
        session = next((s for s in self.sessions if s['tag'] == tag), None)
        if not session:
            return

        server_pid = session.get('server_pid')
        has_client = session.get('has_client', False)
        client_pid = session.get('client_pid')
        has_server = server_pid is not None

        parts = []
        if has_server:
            parts.append('server')
        if has_client:
            parts.append('client')

        if parts:
            what = ' and '.join(parts)
            msg = f"This will also close the running {what}.\n\nAre you sure?"
        else:
            msg = "Are you sure you want to delete this row?"

        reply = QMessageBox.question(
            self, 'Delete Row', msg, QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._show_status(f"Deleted row '{tag}'")
        if has_server:
            self._close_server(tag, server_pid)
        if has_client:
            self._close_client(tag, client_pid)

        self._deleted_tags.add(tag)
        self._remove_pinned_session(tag)

    def _remove_dead_untracked_row(self, tag: str) -> None:
        """Silently remove a dead row that has no active MR tracking.

        Called during silent auto-reconnect when MR tracking fails and
        no server is running — the row serves no purpose.
        """
        session = next((s for s in self.sessions if s['tag'] == tag), None)
        if not session or session.get('server_pid') is not None:
            return  # Server is running — keep the row
        self._pinned_sessions.pop(tag, None)
        save_pinned_sessions(self._pinned_sessions)
        self._deleted_tags.add(tag)
        self.sessions = [s for s in self.sessions if s['tag'] != tag]
        self._show_status(f"Removed dead row '{tag}' (MR tracking failed, no server)")

    def _remove_pinned_session(self, tag: str) -> None:
        """Remove a pinned session and clean up all tracking state."""
        self._pinned_sessions.pop(tag, None)
        save_pinned_sessions(self._pinned_sessions)

        # Clean up MR tracking (skip prompt — _delete_row already prompted)
        self._stop_tracking(tag, _skip_prompt=True)

        # Remove from sessions list and refresh table
        self.sessions = [s for s in self.sessions if s['tag'] != tag]
        self._update_table()

    def _get_active_project_paths(self) -> set[str]:
        """Return the set of project_path values for all running CQ servers."""
        paths: set[str] = set()
        for s in self.sessions:
            if s.get('server_pid') is not None and s.get('project_path'):
                paths.add(str(Path(s['project_path']).resolve()))
        return paths
