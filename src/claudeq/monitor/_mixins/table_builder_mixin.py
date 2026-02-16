"""Table construction, refresh, settings, and template editor methods."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt5 import sip
from PyQt5.QtWidgets import (
    QAction, QApplication, QHBoxLayout, QLabel, QMenu, QPushButton,
    QTableWidgetItem, QWidget,
)
from PyQt5.QtCore import Qt

from claudeq.monitor.mr_tracking.base import MRState
from claudeq.monitor.mr_tracking.config import (
    get_notification_prefs, load_cq_direct_template, load_saved_templates,
    load_selected_direct_template_name, load_selected_template_name,
    save_monitor_prefs, save_selected_direct_template_name,
    save_selected_template_name,
)
from claudeq.monitor.session_manager import get_active_sessions
from claudeq.monitor.scm_polling import SessionRefreshWorker
from claudeq.monitor.ui.ui_widgets import IndicatorLabel, PulsingLabel
from claudeq.monitor.ui.table_helpers import (
    GROUP_BOUNDARY_COLS, INTRA_GROUP_COLS,
    MR_TEMPLATE_TOOLTIP, QUICK_MSG_SEND_DIRECTLY, QUICK_MSG_SEND_TO_QUEUE,
    QUICK_MSG_TEMPLATE_TOOLTIP,
)

if TYPE_CHECKING:
    from claudeq.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object

logger = logging.getLogger(__name__)


class TableBuilderMixin(_Base):
    """Methods for table construction, cell helpers, refresh, settings, and template editor."""

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
            item.setToolTip(text)
            if center:
                item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col, item)
        else:
            if item.text() != text:
                item.setText(text)
                item.setToolTip(text)
            alignment = Qt.AlignCenter if center else int(Qt.AlignLeft | Qt.AlignVCenter)
            if item.textAlignment() != alignment:
                item.setTextAlignment(alignment)

    def _cell_cached(self, tag: str, col: str, state: tuple,
                     row: int, table_col: int) -> bool:
        """Check if a cell widget can be reused (state unchanged, same row)."""
        cached = self._cell_cache.get((tag, col))
        return (cached is not None
                and cached[0] == state
                and not sip.isdeleted(cached[1])
                and self.table.cellWidget(row, table_col) is cached[1])

    def _cache_cell(self, tag: str, col: str, state: tuple,
                    row: int, table_col: int) -> None:
        """Store the current cell widget in the cache after building it."""
        self._cell_cache[(tag, col)] = (
            state, self.table.cellWidget(row, table_col))

    def _update_table(self) -> None:
        """Update table with current sessions.

        Cell widgets for button columns (Delete, Server, Client, MR) are
        cached by ``(tag, column)`` with a state key.  When the state key
        matches and the widget is still at the correct row, the cell is
        left untouched — preserving active tooltips.  When the state
        changes, the cell is rebuilt from scratch and re-cached.

        MR status widgets (PulsingLabel, IndicatorLabel) are additionally
        cached in ``_mr_widgets`` / ``_mr_approval_widgets`` to preserve
        hover popups via ``set_preserve_popup()``.
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
                self._cell_cache.clear()
                self.table.setRowCount(1)
                for col in range(self.table.columnCount()):
                    self.table.removeCellWidget(0, col)
                total_cols = self.table.columnCount()
                # Span the entire row so no column separators are visible
                self.table.setSpan(0, 0, 1, total_cols)
                item = self.table.item(0, 0)
                if not item:
                    self.table.setItem(0, 0, QTableWidgetItem('No active sessions'))
                elif item.text() != 'No active sessions':
                    item.setText('No active sessions')
                return

            # Reset the full-row span and placeholder text from the empty state
            if self.table.columnSpan(0, 0) > 1:
                self.table.setSpan(0, 0, 1, 1)
                item = self.table.item(0, 0)
                if item and item.text() == 'No active sessions':
                    item.setText('')

            self.table.setRowCount(new_count)

            # Clear starting guard for tags whose server is now running
            if self._starting_tags:
                alive = {s['tag'] for s in self.sessions if s.get('server_pid')}
                self._starting_tags -= alive

            for row, session in enumerate(self.sessions):
                tag = session['tag']
                server_pid = session.get('server_pid')
                is_dead = server_pid is None
                client_pid = session.get('client_pid')
                has_client = session.get('has_client', False)
                pinned_data = self._pinned_sessions.get(tag, {})
                pinned_branch = pinned_data.get('branch', '')

                # ── Delete button ──────────────────────────────────
                del_state = ()  # never changes for a given tag
                if not self._cell_cached(tag, 'del', del_state,
                                         row, self.COL_DELETE):
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
                    self._cache_cell(tag, 'del', del_state,
                                     row, self.COL_DELETE)

                # ── Text cells ─────────────────────────────────────
                self._set_cell_text(row, self.COL_TAG, tag)

                # Server Branch always shows the live branch
                server_branch = session['branch']

                # MR Branch shows the MR's source branch if tracked
                if pinned_data.get('remote_project_path'):
                    mr_branch = pinned_branch or 'N/A'
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

                # ── Server button + close button ───────────────────
                starting = tag in self._starting_tags if is_dead else False
                branch_mismatch = bool(
                    not is_dead
                    and pinned_data.get('remote_project_path')
                    and pinned_branch
                    and pinned_branch != 'N/A'
                    and session.get('branch')
                    and session['branch'] != pinned_branch
                )
                srv_state = (is_dead, starting, branch_mismatch,
                             pinned_branch, session.get('branch', ''),
                             server_pid)

                if not self._cell_cached(tag, 'server', srv_state,
                                         row, self.COL_SERVER):
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
                        server_btn = QPushButton(
                            'Starting...' if starting else 'Server')
                        server_btn.setToolTip(
                            f'Server is starting for {tag}...' if starting
                            else f'Start server for {tag}'
                        )
                        if starting:
                            server_btn.setEnabled(False)
                        server_btn.clicked.connect(
                            lambda checked, t=tag: self._start_server(t)
                        )
                    else:
                        if branch_mismatch:
                            server_btn = QPushButton('\u26a0 Server')
                            server_btn.setStyleSheet(
                                'QPushButton { color: #ffa500; } '
                                'QToolTip { color: #e0e0e0; }')
                            server_btn.setToolTip(
                                f"Branch mismatch: expected '{pinned_branch}', "
                                f"got '{session['branch']}'"
                            )
                        else:
                            server_btn = QPushButton('Server')
                            server_btn.setStyleSheet(
                                'QPushButton { color: #00ff00; } '
                                'QToolTip { color: #e0e0e0; }')
                            server_btn.setToolTip(
                                f'Jump to server terminal for {tag}')
                        server_btn.clicked.connect(
                            lambda checked, t=tag:
                                self._focus_session(t, 'server')
                        )
                    server_btn.setContextMenuPolicy(Qt.CustomContextMenu)
                    server_btn.customContextMenuRequested.connect(
                        lambda pos, btn=server_btn, t=tag, dead=is_dead:
                            self._show_server_context_menu(btn, pos, t, dead)
                    )
                    server_layout.addWidget(server_btn)
                    self._set_cell_widget(row, self.COL_SERVER,
                                          server_container)
                    self._cache_cell(tag, 'server', srv_state,
                                     row, self.COL_SERVER)

                # ── Client button + close button ───────────────────
                cli_state = (is_dead, has_client, client_pid)

                if not self._cell_cached(tag, 'client', cli_state,
                                         row, self.COL_CLIENT):
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
                            lambda checked, t=tag, pid=client_pid:
                                self._close_client(t, pid)
                        )
                        client_layout.addWidget(client_x, 0, Qt.AlignVCenter)

                    client_btn = QPushButton('Client')
                    if is_dead and not has_client:
                        client_btn.setEnabled(False)
                        client_btn.setToolTip('No client connected')
                    else:
                        if has_client:
                            client_btn.setStyleSheet(
                                'QPushButton { color: #00ff00; } '
                                'QToolTip { color: #e0e0e0; }')
                            client_btn.setToolTip(
                                f'Jump to client terminal for {tag}')
                        else:
                            client_btn.setToolTip(
                                f'Open new client for {tag}')
                        client_btn.clicked.connect(
                            lambda checked, t=tag:
                                self._focus_session(t, 'client')
                        )
                    client_layout.addWidget(client_btn)
                    self._set_cell_widget(row, self.COL_CLIENT,
                                          client_container)
                    self._cache_cell(tag, 'client', cli_state,
                                     row, self.COL_CLIENT)

                # ── MR column: "Track MR" → "Checking..." → tracked
                if tag in self._checking_tags:
                    mr_state = ('checking',)
                    if not self._cell_cached(tag, 'mr', mr_state,
                                             row, self.COL_MR):
                        if self.table.columnSpan(row, self.COL_MR) > 1:
                            self.table.setSpan(row, self.COL_MR, 1, 1)
                        checking_label = PulsingLabel()
                        checking_label.setText('Checking...')
                        checking_label.setStyleSheet(
                            'color: grey; font-style: italic;')
                        self._set_cell_widget(row, self.COL_MR,
                                              checking_label)
                        self._cache_cell(tag, 'mr', mr_state,
                                         row, self.COL_MR)
                    self.table.removeCellWidget(row, self.COL_MR_BRANCH)
                    self._set_cell_text(row, self.COL_MR_BRANCH, mr_branch)

                elif tag in self._tracked_tags:
                    stale_mr_tags.discard(tag)

                    # Get or create MR widgets
                    mr_widget = self._mr_widgets.get(tag)
                    if mr_widget and not sip.isdeleted(mr_widget):
                        reused_mr = True
                    else:
                        mr_widget = PulsingLabel()
                        self._mr_widgets[tag] = mr_widget
                        reused_mr = False

                    approval_label = self._mr_approval_widgets.get(tag)
                    if approval_label and not sip.isdeleted(approval_label):
                        reused_approval = True
                    else:
                        approval_label = IndicatorLabel()
                        self._mr_approval_widgets[tag] = approval_label
                        reused_approval = False

                    # Reuse MR container if widgets survived and cell is
                    # still at the right row.
                    mr_state = ('tracked',)
                    mr_cached = (
                        reused_mr and reused_approval
                        and self._cell_cached(tag, 'mr', mr_state,
                                              row, self.COL_MR)
                    )
                    if not mr_cached:
                        if self.table.columnSpan(row, self.COL_MR) > 1:
                            self.table.setSpan(row, self.COL_MR, 1, 1)

                        if reused_mr:
                            mr_widget.set_preserve_popup(True)
                        if reused_approval:
                            approval_label.set_preserve_popup(True)

                        mr_container = QWidget()
                        mr_layout = QHBoxLayout(mr_container)
                        mr_layout.setContentsMargins(0, 0, 0, 0)
                        mr_layout.setSpacing(2)

                        mr_x = QPushButton('X')
                        mr_x.setFixedSize(24, mr_x.sizeHint().height())
                        mr_x.setStyleSheet(
                            'QPushButton { color: #999; font-size: 11px; '
                            'padding: 0; }'
                            'QPushButton:hover { color: #ff4444; '
                            'font-weight: bold; }'
                        )
                        mr_x.setToolTip(f'Stop tracking MR for {tag}')
                        mr_x.clicked.connect(
                            lambda checked, t=tag: self._stop_tracking(t)
                        )
                        mr_layout.addWidget(mr_x, 0, Qt.AlignVCenter)

                        mr_layout.addStretch()
                        mr_layout.addWidget(approval_label)
                        mr_layout.addWidget(mr_widget)
                        mr_layout.addStretch()

                        self._set_cell_widget(row, self.COL_MR,
                                              mr_container)
                        self._cache_cell(tag, 'mr', mr_state,
                                         row, self.COL_MR)

                        if reused_mr:
                            mr_widget.set_preserve_popup(False)
                        if reused_approval:
                            approval_label.set_preserve_popup(False)

                    # Always update MR widget properties (change each poll)
                    mr_status = self._mr_statuses.get(tag)
                    self._apply_mr_status(mr_widget, approval_label,
                                          mr_status)
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
                            lambda t=tag:
                                self._send_all_threads_combined_to_cq(t)
                        )
                        mr_widget.set_send_cq_threads_callback(
                            lambda t=tag: self._send_cq_threads_to_cq(t)
                        )
                        mr_widget.set_send_cq_threads_combined_callback(
                            lambda t=tag:
                                self._send_cq_threads_combined_to_cq(t)
                        )
                    mr_widget.set_auto_fetch_cq(
                        self._prefs.get('auto_fetch_cq', True)
                    )
                    self.table.removeCellWidget(row, self.COL_MR_BRANCH)
                    self._set_cell_text(row, self.COL_MR_BRANCH, mr_branch)

                else:
                    # Not tracked — "Track MR" button
                    mr_state = ('untracked', is_dead)
                    if not self._cell_cached(tag, 'mr', mr_state,
                                             row, self.COL_MR):
                        if self.table.columnSpan(row, self.COL_MR) > 1:
                            self.table.setSpan(row, self.COL_MR, 1, 1)
                        track_btn = QPushButton('Track MR')
                        track_btn.setToolTip(
                            'Start a server first to discover MR from branch'
                            if is_dead
                            else f'Start tracking MR/PR for {tag}'
                        )
                        track_btn.setStyleSheet('font-size: 11px;')
                        track_btn.clicked.connect(
                            lambda checked, t=tag: self._start_tracking(t)
                        )
                        if is_dead:
                            track_btn.setEnabled(False)
                        self._set_cell_widget(row, self.COL_MR, track_btn)
                        self._cache_cell(tag, 'mr', mr_state,
                                         row, self.COL_MR)

                    # MR Branch: show stored branch + X button if MR-pinned
                    is_mr_pinned = (
                        pinned_data.get('remote_project_path')
                        and mr_branch != 'N/A'
                    )
                    if is_mr_pinned:
                        mr_br_state = ('untracked_pinned', mr_branch)
                        if not self._cell_cached(tag, 'mr_branch', mr_br_state,
                                                 row, self.COL_MR_BRANCH):
                            mr_br_container = QWidget()
                            mr_br_layout = QHBoxLayout(mr_br_container)
                            mr_br_layout.setContentsMargins(0, 0, 0, 0)
                            mr_br_layout.setSpacing(4)

                            mr_br_x = QPushButton('X')
                            mr_br_x.setFixedSize(24, mr_br_x.sizeHint().height())
                            mr_br_x.setStyleSheet(
                                'QPushButton { color: #999; font-size: 11px; '
                                'padding: 0; }'
                                'QPushButton:hover { color: #ff4444; '
                                'font-weight: bold; }'
                            )
                            mr_br_x.setToolTip(
                                f'Clear pinned MR data for {tag}')
                            mr_br_x.clicked.connect(
                                lambda checked, t=tag:
                                    self._clear_pinned_mr_data(t)
                            )
                            mr_br_layout.addWidget(
                                mr_br_x, 0, Qt.AlignVCenter)

                            mr_br_label = QLabel(mr_branch)
                            mr_br_label.setToolTip(mr_branch)
                            mr_br_layout.addWidget(mr_br_label, 1)

                            # Clear underlying item text so it doesn't
                            # render through behind the widget.
                            item = self.table.item(row, self.COL_MR_BRANCH)
                            if item:
                                item.setText('')
                            self._set_cell_widget(
                                row, self.COL_MR_BRANCH, mr_br_container)
                            self._cache_cell(
                                tag, 'mr_branch', mr_br_state,
                                row, self.COL_MR_BRANCH)
                    else:
                        self.table.removeCellWidget(row, self.COL_MR_BRANCH)
                        self._set_cell_text(row, self.COL_MR_BRANCH, 'N/A')

            # Clean up stale MR widgets for tags no longer shown
            for stale_tag in stale_mr_tags:
                w = self._mr_widgets.pop(stale_tag, None)
                if w:
                    try:
                        w.set_pulsing(False)
                    except RuntimeError:
                        pass
                self._mr_approval_widgets.pop(stale_tag, None)

            # Clean up stale cell cache entries for tags no longer shown
            current_tags = {s['tag'] for s in self.sessions}
            stale_keys = [k for k in self._cell_cache
                          if k[0] not in current_tags]
            for k in stale_keys:
                self._cell_cache.pop(k, None)
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

    def _show_server_context_menu(
        self, btn: QPushButton, pos: 'QPoint', tag: str, is_dead: bool,
    ) -> None:
        """Show right-click context menu on the Server button."""
        menu = QMenu(self)

        direct_action = QAction(QUICK_MSG_SEND_DIRECTLY, self)
        if is_dead:
            direct_action.setEnabled(False)
        else:
            direct_action.triggered.connect(
                lambda: self._send_direct_template(tag)
            )
        menu.addAction(direct_action)

        queue_action = QAction(QUICK_MSG_SEND_TO_QUEUE, self)
        if is_dead:
            queue_action.setEnabled(False)
        else:
            queue_action.triggered.connect(
                lambda: self._send_direct_template_to_queue(tag)
            )
        menu.addAction(queue_action)

        menu.exec_(btn.mapToGlobal(pos))

    def _send_direct_template(self, tag: str) -> None:
        """Send the direct message template directly to the CQ session."""
        from claudeq.monitor.cq_sender import send_to_cq_session_direct

        template = load_cq_direct_template()
        if not template:
            self._show_status('No quick message template selected')
            return
        if send_to_cq_session_direct(tag, template):
            self._show_status(f'Quick message sent to {tag}')
        else:
            self._show_status(f'Failed to send quick message to {tag}')

    def _send_direct_template_to_queue(self, tag: str) -> None:
        """Queue the direct message template for the CQ session."""
        from claudeq.monitor.cq_sender import send_to_cq_session_raw

        template = load_cq_direct_template()
        if not template:
            self._show_status('No quick message template selected')
            return
        if send_to_cq_session_raw(tag, template):
            self._show_status(f'Quick message queued for {tag}')
        else:
            self._show_status(f'Failed to queue quick message for {tag}')

    def _open_template_editor(self) -> None:
        """Open a dialog to edit the CQ template text with named presets."""
        from claudeq.monitor.dialogs.scm_template_dialog import TemplateEditorDialog

        dialog = TemplateEditorDialog(self)
        dialog.exec_()
        self._populate_template_combo()
        self._populate_direct_template_combo()

    def _populate_template_combo(self) -> None:
        """Reload template combo items from saved presets and selection."""
        max_display = 40
        combo = self.template_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem('(None)')
        for name in sorted(load_saved_templates().keys()):
            if len(name) > max_display:
                combo.addItem(name[:max_display] + '\u2026')
                combo.setItemData(combo.count() - 1, name, Qt.UserRole)
            else:
                combo.addItem(name)
        selected = load_selected_template_name()
        if selected and len(selected) > max_display:
            display = selected[:max_display] + '\u2026'
            idx = combo.findText(display)
        else:
            idx = combo.findText(selected) if selected else 0
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)
        self._update_template_combo_tooltip()

    def _on_template_combo_changed(self) -> None:
        """Handle template combo selection change."""
        text = self.template_combo.currentText()
        if text == '(None)':
            save_selected_template_name('')
            self._update_template_combo_tooltip()
            return
        # Resolve truncated display name back to full name via UserRole
        idx = self.template_combo.currentIndex()
        full_name = self.template_combo.itemData(idx, Qt.UserRole)
        save_selected_template_name(full_name if full_name else text)
        self._update_template_combo_tooltip()

    def _update_template_combo_tooltip(self) -> None:
        """Set combo tooltip to the full template name when truncated."""
        combo = self.template_combo
        idx = combo.currentIndex()
        full_name = combo.itemData(idx, Qt.UserRole) if idx >= 0 else None
        if full_name:
            combo.setToolTip(full_name)
        else:
            combo.setToolTip(MR_TEMPLATE_TOOLTIP)

    def _populate_direct_template_combo(self) -> None:
        """Reload direct template combo items from saved presets and selection."""
        max_display = 40
        combo = self.direct_template_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem('(None)')
        for name in sorted(load_saved_templates().keys()):
            if len(name) > max_display:
                combo.addItem(name[:max_display] + '\u2026')
                combo.setItemData(combo.count() - 1, name, Qt.UserRole)
            else:
                combo.addItem(name)
        selected = load_selected_direct_template_name()
        if selected and len(selected) > max_display:
            display = selected[:max_display] + '\u2026'
            idx = combo.findText(display)
        else:
            idx = combo.findText(selected) if selected else 0
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)
        self._update_direct_template_combo_tooltip()

    def _on_direct_template_combo_changed(self) -> None:
        """Handle direct template combo selection change."""
        text = self.direct_template_combo.currentText()
        if text == '(None)':
            save_selected_direct_template_name('')
            self._update_direct_template_combo_tooltip()
            return
        idx = self.direct_template_combo.currentIndex()
        full_name = self.direct_template_combo.itemData(idx, Qt.UserRole)
        save_selected_direct_template_name(full_name if full_name else text)
        self._update_direct_template_combo_tooltip()

    def _update_direct_template_combo_tooltip(self) -> None:
        """Set combo tooltip to the full template name when truncated."""
        combo = self.direct_template_combo
        idx = combo.currentIndex()
        full_name = combo.itemData(idx, Qt.UserRole) if idx >= 0 else None
        if full_name:
            combo.setToolTip(full_name)
        else:
            combo.setToolTip(QUICK_MSG_TEMPLATE_TOOLTIP)

    def _apply_tooltips_setting(self) -> None:
        """Sync the tooltip app with the current preference."""
        if hasattr(self, '_tooltip_app'):
            self._tooltip_app.tooltips_enabled = self._prefs.get('show_tooltips', True)
