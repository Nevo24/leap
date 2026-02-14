"""Table construction, refresh, settings, and context editor methods."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from PyQt5 import sip
from PyQt5.QtWidgets import (
    QApplication, QHBoxLayout, QPushButton, QTableWidgetItem, QWidget,
)
from PyQt5.QtCore import Qt

from claudeq.monitor.mr_tracking.base import MRState
from claudeq.monitor.mr_tracking.config import (
    get_notification_prefs, save_monitor_prefs,
)
from claudeq.monitor.session_manager import get_active_sessions
from claudeq.monitor.scm_polling import SessionRefreshWorker
from claudeq.monitor.ui.ui_widgets import IndicatorLabel, PulsingLabel
from claudeq.monitor.ui.table_helpers import GROUP_BOUNDARY_COLS, INTRA_GROUP_COLS

if TYPE_CHECKING:
    from claudeq.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object

logger = logging.getLogger(__name__)


class TableBuilderMixin(_Base):
    """Methods for table construction, cell helpers, refresh, and settings."""

    _CENTER_COLS = frozenset({5, 6})  # COL_STATUS, COL_QUEUE

    def _set_cell_widget(self, row: int, col: int, widget: QWidget) -> None:
        """Set a cell widget with column group separator styling.

        Wraps the widget in a container with a right border when the column
        sits at a group boundary or within a group.
        """
        if col in GROUP_BOUNDARY_COLS or col in INTRA_GROUP_COLS:
            border = ('1px solid white' if col in GROUP_BOUNDARY_COLS
                      else '1px solid rgba(255, 255, 255, 50)')
            wrapper = QWidget()
            wrapper.setObjectName('_cqSep')
            wrapper.setStyleSheet(f'#_cqSep {{ border-right: {border}; }}')
            lay = QHBoxLayout(wrapper)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            lay.addWidget(widget)
            self.table.setCellWidget(row, col, wrapper)
        else:
            self.table.setCellWidget(row, col, widget)

    def _set_cell_text(self, row: int, col: int, text: str) -> None:
        """Set cell text only if it changed, to avoid flicker."""
        item = self.table.item(row, col)
        center = col in self._CENTER_COLS or text == 'N/A'
        if item is None:
            item = QTableWidgetItem(text)
            if center:
                item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col, item)
        else:
            if item.text() != text:
                item.setText(text)
            alignment = Qt.AlignCenter if center else int(Qt.AlignLeft | Qt.AlignVCenter)
            if item.textAlignment() != alignment:
                item.setTextAlignment(alignment)

    def _update_table(self) -> None:
        """Update table with current sessions.

        Most cell widgets are recreated each refresh to avoid dangling C++
        pointers (Qt auto-deletes cell widgets on setRowCount shrink).
        MR status widgets (PulsingLabel, IndicatorLabel) are reused across
        refreshes to preserve hover popups — they survive reparenting into
        new containers via addWidget().  Stale MR widgets (for removed tags)
        are cleaned up after the loop.
        """
        new_count = len(self.sessions)

        self.table.setUpdatesEnabled(False)
        try:
            # Track which cached MR widgets are stale (tag no longer in table).
            # Widgets for still-present tracked tags are reused to preserve
            # hover popups across table rebuilds.
            stale_mr_tags = set(self._mr_widgets.keys())

            if not self.sessions:
                # All MR widgets are stale — stop pulsing and clear
                for w in self._mr_widgets.values():
                    try:
                        w.set_pulsing(False)
                    except RuntimeError:
                        pass
                self._mr_widgets.clear()
                self._mr_approval_widgets.clear()
                self.table.setRowCount(1)
                for col in range(self.table.columnCount()):
                    self.table.removeCellWidget(0, col)
                    text = 'No active sessions' if col == self.COL_TAG else ''
                    item = self.table.item(0, col)
                    if not item:
                        self.table.setItem(0, col, QTableWidgetItem(text))
                    elif item.text() != text:
                        item.setText(text)
                return

            self.table.setRowCount(new_count)

            for row, session in enumerate(self.sessions):
                tag = session['tag']
                server_pid = session.get('server_pid')
                is_dead = server_pid is None

                # Delete button (leftmost column, replaces row index)
                del_container = QWidget()
                del_layout = QHBoxLayout(del_container)
                del_layout.setContentsMargins(0, 0, 0, 0)
                del_layout.setSpacing(0)
                del_btn = QPushButton('X')
                del_btn.setFixedSize(24, del_btn.sizeHint().height())
                del_btn.setStyleSheet(
                    'QPushButton { color: #999; font-size: 11px; padding: 0; }'
                    'QPushButton:hover { color: #ff4444; font-weight: bold; }'
                )
                del_btn.setToolTip(f'Remove row for {tag}')
                del_btn.clicked.connect(
                    lambda checked, t=tag: self._delete_row(t)
                )
                del_layout.addWidget(del_btn, 0, Qt.AlignCenter)
                self._set_cell_widget(row, self.COL_DELETE, del_container)

                # Text cells — only update if value changed
                self._set_cell_text(row, self.COL_TAG, tag)

                pinned_data = self._pinned_sessions.get(tag, {})

                # Server Branch always shows the live branch
                server_branch = session['branch']

                # MR Branch shows the MR's source branch if tracked
                if pinned_data.get('remote_project_path'):
                    mr_branch = pinned_data.get('branch') or 'N/A'
                else:
                    mr_branch = 'N/A'

                if is_dead:
                    self._set_cell_text(row, self.COL_PROJECT, session['project'])
                    self._set_cell_text(row, self.COL_SERVER_BRANCH, 'N/A')
                    self._set_cell_text(row, self.COL_STATUS, 'N/A')
                    self._set_cell_text(row, self.COL_QUEUE, 'N/A')
                else:
                    self._set_cell_text(row, self.COL_PROJECT, session['project'])
                    self._set_cell_text(row, self.COL_SERVER_BRANCH, server_branch)
                    status = '\u2705 Running' if session['claude_busy'] else '\u26aa Idle'
                    self._set_cell_text(row, self.COL_STATUS, status)
                    self._set_cell_text(row, self.COL_QUEUE, str(session['queue_size']))

                # MR column: "Track MR" (spans MR+MR Branch) → "Checking..." → tracked
                if tag in self._checking_tags:
                    if self.table.columnSpan(row, self.COL_MR) > 1:
                        self.table.setSpan(row, self.COL_MR, 1, 1)
                    checking_label = PulsingLabel()
                    checking_label.setText('Checking...')
                    checking_label.setStyleSheet('color: grey; font-style: italic;')
                    self._set_cell_widget(row, self.COL_MR, checking_label)
                    self._set_cell_text(row, self.COL_MR_BRANCH, '')
                elif tag in self._tracked_tags:
                    # Remove span — MR and MR Branch are separate columns
                    if self.table.columnSpan(row, self.COL_MR) > 1:
                        self.table.setSpan(row, self.COL_MR, 1, 1)

                    stale_mr_tags.discard(tag)

                    # Reuse existing MR widgets to preserve hover popups
                    mr_widget = self._mr_widgets.get(tag)
                    if mr_widget and not sip.isdeleted(mr_widget):
                        reused_mr = True
                        mr_widget.set_preserve_popup(True)
                    else:
                        mr_widget = PulsingLabel()
                        self._mr_widgets[tag] = mr_widget
                        reused_mr = False

                    approval_label = self._mr_approval_widgets.get(tag)
                    if approval_label and not sip.isdeleted(approval_label):
                        reused_approval = True
                        approval_label.set_preserve_popup(True)
                    else:
                        approval_label = IndicatorLabel()
                        self._mr_approval_widgets[tag] = approval_label
                        reused_approval = False

                    mr_container = QWidget()
                    mr_layout = QHBoxLayout(mr_container)
                    mr_layout.setContentsMargins(0, 0, 0, 0)
                    mr_layout.setSpacing(2)

                    mr_x = QPushButton('X')
                    mr_x.setFixedSize(24, mr_x.sizeHint().height())
                    mr_x.setStyleSheet(
                        'QPushButton { color: #999; font-size: 11px; padding: 0; }'
                        'QPushButton:hover { color: #ff4444; font-weight: bold; }'
                    )
                    mr_x.setToolTip(f'Stop tracking MR for {tag}')
                    mr_x.clicked.connect(
                        lambda checked, t=tag: self._stop_tracking(t)
                    )
                    mr_layout.addWidget(mr_x, 0, Qt.AlignVCenter)

                    mr_layout.addStretch()
                    mr_layout.addWidget(approval_label)

                    mr_status = self._mr_statuses.get(tag)
                    self._apply_mr_status(mr_widget, approval_label, mr_status)
                    mr_widget.set_has_unresponded(
                        mr_status is not None
                        and mr_status.state == MRState.UNRESPONDED
                    )
                    mr_widget.set_server_running(not is_dead)
                    if not reused_mr:
                        mr_widget.set_send_to_cq_callback(
                            lambda t=tag: self._send_all_threads_to_cq(t)
                        )
                        mr_widget.set_send_combined_to_cq_callback(
                            lambda t=tag: self._send_all_threads_combined_to_cq(t)
                        )
                        mr_widget.set_send_cq_threads_callback(
                            lambda t=tag: self._send_cq_threads_to_cq(t)
                        )
                        mr_widget.set_send_cq_threads_combined_callback(
                            lambda t=tag: self._send_cq_threads_combined_to_cq(t)
                        )
                    mr_widget.set_auto_fetch_cq(
                        self._prefs.get('auto_fetch_cq', True)
                    )
                    mr_layout.addWidget(mr_widget)

                    mr_layout.addStretch()

                    self._set_cell_widget(row, self.COL_MR, mr_container)
                    # MR Branch shows the MR source branch
                    self._set_cell_text(row, self.COL_MR_BRANCH, mr_branch)

                    # Restore popup behavior after reparenting.
                    # Popup stays at its original position (same table cell).
                    if reused_mr:
                        mr_widget.set_preserve_popup(False)
                    if reused_approval:
                        approval_label.set_preserve_popup(False)
                else:
                    # Not tracked — span "Track MR" across both MR columns
                    self.table.setSpan(row, self.COL_MR, 1, 2)
                    existing = self.table.cellWidget(row, self.COL_MR)
                    if isinstance(existing, QPushButton) \
                            and getattr(existing, '_cq_tag', None) == tag:
                        pass  # Reuse — avoids destroying the button mid-click
                    else:
                        track_btn = QPushButton('Track MR')
                        track_btn._cq_tag = tag
                        track_btn.setToolTip(f'Start tracking MR/PR for {tag}')
                        track_btn.setStyleSheet('font-size: 11px;')
                        track_btn.clicked.connect(
                            lambda checked, t=tag: self._start_tracking(t)
                        )
                        if is_dead:
                            track_btn.setEnabled(False)
                            track_btn.setToolTip('Start a server first to discover MR from branch')
                        # Direct setCellWidget — button spans MR+MR Branch, no border
                        self.table.setCellWidget(row, self.COL_MR, track_btn)

                client_pid = session.get('client_pid')
                has_client = session.get('has_client', False)

                # Server button + close button
                server_container = QWidget()
                server_layout = QHBoxLayout(server_container)
                server_layout.setContentsMargins(0, 0, 0, 0)
                server_layout.setSpacing(2)

                if not is_dead:
                    server_x = QPushButton('X')
                    server_x.setFixedSize(24, server_x.sizeHint().height())
                    server_x.setStyleSheet(
                        'QPushButton { color: #999; font-size: 11px; padding: 0; }'
                        'QPushButton:hover { color: #ff4444; font-weight: bold; }'
                    )
                    server_x.setToolTip(f'Close server {tag}')
                    server_x.clicked.connect(
                        lambda checked, t=tag, spid=server_pid:
                            self._close_server(t, spid)
                    )
                    server_layout.addWidget(server_x, 0, Qt.AlignVCenter)

                if is_dead:
                    server_btn = QPushButton('Server')
                    server_btn.setToolTip(f'Start server for {tag}')
                    server_btn.clicked.connect(
                        lambda checked, t=tag: self._start_server(t)
                    )
                else:
                    # Check branch mismatch for MR-pinned rows
                    pinned_branch = pinned_data.get('branch', '')
                    branch_mismatch = (
                        pinned_data.get('remote_project_path')
                        and pinned_branch
                        and pinned_branch != 'N/A'
                        and session.get('branch')
                        and session['branch'] != pinned_branch
                    )
                    if branch_mismatch:
                        server_btn = QPushButton('\u26a0 Server')
                        server_btn.setStyleSheet('QPushButton { color: #ffa500; } QToolTip { color: #e0e0e0; }')
                        server_btn.setToolTip(
                            f"Branch mismatch: expected '{pinned_branch}', "
                            f"got '{session['branch']}'"
                        )
                    else:
                        server_btn = QPushButton('Server')
                        server_btn.setStyleSheet('QPushButton { color: #00ff00; } QToolTip { color: #e0e0e0; }')
                        server_btn.setToolTip(f'Jump to server terminal for {tag}')
                    server_btn.clicked.connect(
                        lambda checked, t=tag: self._focus_session(t, 'server')
                    )
                server_layout.addWidget(server_btn)
                self._set_cell_widget(row, self.COL_SERVER, server_container)

                # Client button + close button
                client_container = QWidget()
                client_layout = QHBoxLayout(client_container)
                client_layout.setContentsMargins(0, 0, 0, 0)
                client_layout.setSpacing(2)

                if has_client:
                    client_x = QPushButton('X')
                    client_x.setFixedSize(24, client_x.sizeHint().height())
                    client_x.setStyleSheet(
                        'QPushButton { color: #999; font-size: 11px; padding: 0; }'
                        'QPushButton:hover { color: #ff4444; font-weight: bold; }'
                    )
                    client_x.setToolTip(f'Close client {tag}')
                    client_x.clicked.connect(
                        lambda checked, t=tag, pid=client_pid: self._close_client(t, pid)
                    )
                    client_layout.addWidget(client_x, 0, Qt.AlignVCenter)

                client_btn = QPushButton('Client')
                if is_dead and not has_client:
                    client_btn.setEnabled(False)
                    client_btn.setToolTip('No client connected')
                else:
                    if has_client:
                        client_btn.setStyleSheet('QPushButton { color: #00ff00; } QToolTip { color: #e0e0e0; }')
                        client_btn.setToolTip(f'Jump to client terminal for {tag}')
                    else:
                        client_btn.setToolTip(f'Open new client for {tag}')
                    client_btn.clicked.connect(
                        lambda checked, t=tag: self._focus_session(t, 'client')
                    )
                client_layout.addWidget(client_btn)

                self._set_cell_widget(row, self.COL_CLIENT, client_container)

            # Clean up stale MR widgets for tags no longer shown
            for stale_tag in stale_mr_tags:
                w = self._mr_widgets.pop(stale_tag, None)
                if w:
                    try:
                        w.set_pulsing(False)
                    except RuntimeError:
                        pass
                self._mr_approval_widgets.pop(stale_tag, None)
        finally:
            self.table.setUpdatesEnabled(True)

    def _refresh_data(self) -> None:
        """Refresh session data and update table (non-blocking).

        Launches a SessionRefreshWorker to query sockets in the background.
        Falls back to synchronous refresh on first call (before timer starts).
        """
        if self._refresh_worker and self._refresh_worker.isRunning():
            return  # skip this cycle

        self._refresh_worker = SessionRefreshWorker(self)
        self._refresh_worker.sessions_ready.connect(self._on_sessions_refreshed)
        self._refresh_worker.finished.connect(self._on_refresh_worker_finished)
        self._refresh_worker.start()

    def _on_refresh_worker_finished(self) -> None:
        """Clean up the refresh worker reference after it completes."""
        if self._refresh_worker:
            self._refresh_worker.deleteLater()
            self._refresh_worker = None

    def _on_sessions_refreshed(self, sessions: list) -> None:
        """Handle background session refresh result."""
        self.sessions = self._merge_sessions(sessions)
        self._update_table()
        notif_prefs = get_notification_prefs(self._prefs)
        dock_enabled = {k: v['dock'] for k, v in notif_prefs.items()}
        events = self._dock_badge.update_sessions(
            sessions, self.isActiveWindow(), dock_enabled,
        )
        self._send_banner_notifications(events)

    def _open_settings(self) -> None:
        """Open the settings dialog."""
        from claudeq.monitor.dialogs.settings_dialog import SettingsDialog, DEFAULT_REPOS_DIR

        dialog = SettingsDialog(
            current_terminal=self._prefs.get('default_terminal', 'Terminal.app'),
            current_repos_dir=self._prefs.get('repos_dir', DEFAULT_REPOS_DIR),
            active_paths_fn=self._get_active_project_paths,
            log_fn=self._show_status,
            show_tooltips=self._prefs.get('show_tooltips', True),
            notification_prefs=get_notification_prefs(self._prefs),
            parent=self,
        )
        if dialog.exec_():
            self._prefs['default_terminal'] = dialog.selected_terminal()
            self._prefs['repos_dir'] = dialog.selected_repos_dir()
            self._prefs['show_tooltips'] = dialog.show_tooltips()
            self._prefs['notifications'] = dialog.notification_prefs()
            save_monitor_prefs(self._prefs)
            self._apply_tooltips_setting()
            self._show_status('Settings saved')

    def _open_context_editor(self) -> None:
        """Open a dialog to edit the CQ context text with named presets."""
        from claudeq.monitor.dialogs.scm_context_dialog import ContextEditorDialog

        dialog = ContextEditorDialog(self)
        dialog.exec_()

    def _apply_tooltips_setting(self) -> None:
        """Sync the tooltip app with the current preference."""
        if hasattr(self, '_tooltip_app'):
            self._tooltip_app.tooltips_enabled = self._prefs.get('show_tooltips', True)
