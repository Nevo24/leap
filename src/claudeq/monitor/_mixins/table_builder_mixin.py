"""Table construction, refresh, settings, and template editor methods."""

from __future__ import annotations

import logging
import subprocess
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PyQt5 import sip
from PyQt5.QtWidgets import (
    QAction, QApplication, QComboBox, QHBoxLayout, QLabel, QMenu,
    QMessageBox, QPushButton, QTableWidgetItem, QWidget,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor

from claudeq.monitor.mr_tracking.base import MRState
from claudeq.monitor.mr_tracking.config import (
    get_dock_enabled, get_notification_prefs, load_cq_direct_template,
    load_saved_templates, load_selected_direct_template_name,
    load_selected_template_name, save_monitor_prefs,
    save_selected_direct_template_name, save_selected_template_name,
)
from claudeq.monitor.session_manager import get_active_sessions
from claudeq.utils.socket_utils import send_socket_request
from claudeq.monitor.scm_polling import SessionRefreshWorker
from claudeq.monitor.ui.ui_widgets import ElidedLabel, IndicatorLabel, PulsingLabel
from claudeq.monitor.ui.table_helpers import (
    ACTIVE_BTN_STYLE, CLOSE_BTN_STYLE, GROUP_BOUNDARY_COLS, INTRA_GROUP_COLS,
    MAX_COMBO_DISPLAY, MR_TEMPLATE_TOOLTIP, QUICK_MSG_SEND_DIRECTLY,
    QUICK_MSG_SEND_TO_QUEUE, QUICK_MSG_TEMPLATE_TOOLTIP,
)

if TYPE_CHECKING:
    from claudeq.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object

logger = logging.getLogger(__name__)


class TableBuilderMixin(_Base):
    """Methods for table construction, cell helpers, refresh, settings, and template editor."""

    _CENTER_COLS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10})  # All data columns

    def _set_cell_widget(self, row: int, col: int, widget: QWidget) -> None:
        """Set a cell widget wrapped in a hover-aware container.

        All cell widgets are wrapped so the row hover highlight can be
        toggled uniformly via the ``_hover`` dynamic property.  Columns
        at group boundaries additionally get a right border.
        """
        if col in GROUP_BOUNDARY_COLS or col in INTRA_GROUP_COLS:
            border = ('1px solid white' if col in GROUP_BOUNDARY_COLS
                      else '1px solid rgba(255, 255, 255, 50)')
            border_css = f'border-right: {border}; '
        else:
            border_css = ''
        wrapper = QWidget()
        wrapper.setObjectName('_cqSep')
        wrapper.setStyleSheet(
            f'#_cqSep {{ {border_css}background: transparent; }}'
        )
        lay = QHBoxLayout(wrapper)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(widget)
        self.table.setCellWidget(row, col, wrapper)

    def _apply_hover_to_row(self, row: int, highlight: bool) -> None:
        """Toggle the hover background on all cell widgets in a row.

        The delegate paints the hover background for every cell (text
        and widget).  Widget cells need their children made transparent
        so the delegate background shows through uniformly.
        """
        if row < 0 or row >= self.table.rowCount():
            return
        for col in range(self.table.columnCount()):
            w = self.table.cellWidget(row, col)
            if not w or w.objectName() != '_cqSep':
                continue
            # Make buttons/labels transparent so delegate bg shows
            # through.  Skip PulsingLabel / IndicatorLabel (animated
            # stylesheets that must not be overridden).
            for child in w.findChildren((QPushButton, QLabel)):
                if isinstance(child, (PulsingLabel, IndicatorLabel)):
                    continue
                if highlight:
                    orig = child.property('_origSS')
                    if orig is None:
                        orig = child.styleSheet()
                        child.setProperty('_origSS', orig)
                    if isinstance(child, QPushButton):
                        rule = ' QPushButton { background: transparent; }'
                    else:
                        rule = ' QLabel { background: transparent; }'
                    child.setStyleSheet(orig + rule)
                else:
                    orig = child.property('_origSS')
                    if orig is not None:
                        child.setStyleSheet(orig)

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
                    del_btn.setStyleSheet(CLOSE_BTN_STYLE)
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
                    # Remove any queue widget from a previous live state
                    self.table.removeCellWidget(row, self.COL_QUEUE)
                else:
                    self._set_cell_text(row, self.COL_PROJECT, session['project'])
                    self._set_cell_text(row, self.COL_SERVER_BRANCH, server_branch)

                    claude_state = session.get('claude_state', 'idle')
                    state_display = {
                        'idle': ('\u25cb Idle', None),
                        'running': ('\u25cf Running', QColor(76, 175, 80)),
                        'needs_permission': ('\u25b2 Permission', QColor(255, 152, 0)),
                        'has_question': ('\u25c6 Question', QColor(100, 181, 246)),
                        'interrupted': ('\u25c7 Interrupted', QColor(255, 213, 79)),
                    }
                    text, color = state_display.get(claude_state, (claude_state, None))
                    self._set_cell_text(row, self.COL_STATUS, text)
                    item = self.table.item(row, self.COL_STATUS)
                    if item and color:
                        item.setForeground(color)
                    elif item:
                        item.setForeground(QColor(255, 255, 255))
                    state_explanations = {
                        'idle': 'Claude is waiting for input — will accept next queued message',
                        'running': 'Claude is actively processing a request',
                        'needs_permission': 'Claude needs your permission',
                        'has_question': 'Claude is asking a clarifying question',
                        'interrupted': 'Claude was interrupted — will accept next queued message',
                    }
                    if self._prefs.get('show_tooltips', True):
                        explanation = state_explanations.get(claude_state, '')
                        if explanation and item:
                            item.setToolTip(explanation)

                    # Queue column with right-click context menu
                    auto_send_mode = session.get('auto_send_mode', 'pause')
                    queue_size = session['queue_size']
                    q_state = (queue_size, auto_send_mode)
                    if not self._cell_cached(tag, 'queue', q_state,
                                             row, self.COL_QUEUE):
                        q_label = QLabel(str(queue_size))
                        q_label.setAlignment(Qt.AlignCenter)
                        q_label.setContextMenuPolicy(Qt.CustomContextMenu)
                        q_label.customContextMenuRequested.connect(
                            lambda pos, lbl=q_label, t=tag:
                                self._show_queue_context_menu(lbl, pos, t)
                        )
                        # Clear underlying item text
                        item = self.table.item(row, self.COL_QUEUE)
                        if item:
                            item.setText('')
                        self._set_cell_widget(row, self.COL_QUEUE, q_label)
                        self._cache_cell(tag, 'queue', q_state,
                                         row, self.COL_QUEUE)

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
                        server_x.setStyleSheet(CLOSE_BTN_STYLE)
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
                            server_btn.setStyleSheet(ACTIVE_BTN_STYLE)
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
                        client_x.setStyleSheet(CLOSE_BTN_STYLE)
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
                            client_btn.setStyleSheet(ACTIVE_BTN_STYLE)
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

                # ── Slack column ──────────────────────────────────
                slack_enabled = session.get('slack_enabled', False)
                slack_installed = self._is_slack_installed()
                bot_running = self._is_slack_bot_running()
                slack_state = (is_dead, slack_installed, bot_running,
                               slack_enabled)
                if not self._cell_cached(tag, 'slack', slack_state,
                                         row, self.COL_SLACK):
                    if not slack_installed:
                        slack_btn = QPushButton('Slack')
                        slack_btn.setEnabled(False)
                        slack_btn.setToolTip(
                            'Install Slack app first (make install-slack-app)')
                        self._set_cell_widget(row, self.COL_SLACK, slack_btn)
                    elif is_dead:
                        slack_btn = QPushButton('Slack')
                        slack_btn.setEnabled(False)
                        slack_btn.setToolTip('Start server first')
                        self._set_cell_widget(row, self.COL_SLACK, slack_btn)
                    elif not bot_running:
                        # Bot not running — grey button, prompt to start bot
                        slack_btn = QPushButton('Slack')
                        tip = ('Slack bot is not running — will reconnect '
                               'when started' if slack_enabled
                               else 'Start the Slack bot first')
                        slack_btn.setToolTip(tip)
                        slack_btn.clicked.connect(
                            lambda checked:
                                self._show_slack_bot_not_running()
                        )
                        self._set_cell_widget(row, self.COL_SLACK, slack_btn)
                    elif slack_enabled:
                        slack_container = QWidget()
                        slack_layout = QHBoxLayout(slack_container)
                        slack_layout.setContentsMargins(0, 0, 0, 0)
                        slack_layout.setSpacing(2)

                        slack_x = QPushButton('X')
                        slack_x.setFixedSize(24, slack_x.sizeHint().height())
                        slack_x.setStyleSheet(CLOSE_BTN_STYLE)
                        slack_x.setToolTip(f'Disconnect Slack for {tag}')
                        slack_x.clicked.connect(
                            lambda checked, t=tag:
                                self._toggle_slack(t, False)
                        )
                        slack_layout.addWidget(slack_x, 0, Qt.AlignVCenter)

                        slack_btn = QPushButton('Slack')
                        slack_btn.setStyleSheet(ACTIVE_BTN_STYLE)
                        slack_btn.setToolTip(
                            f'Open Slack thread for {tag}')
                        slack_btn.clicked.connect(
                            lambda checked, t=tag:
                                self._open_slack_thread(t)
                        )
                        slack_layout.addWidget(slack_btn)
                        self._set_cell_widget(
                            row, self.COL_SLACK, slack_container)
                    else:
                        slack_btn = QPushButton('Slack')
                        slack_btn.setToolTip(
                            f'Enable Slack integration for {tag}')
                        slack_btn.clicked.connect(
                            lambda checked, t=tag:
                                self._toggle_slack(t, True)
                        )
                        self._set_cell_widget(row, self.COL_SLACK, slack_btn)
                    self._cache_cell(tag, 'slack', slack_state,
                                     row, self.COL_SLACK)

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
                        mr_x.setStyleSheet(CLOSE_BTN_STYLE)
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
                            mr_br_x.setStyleSheet(CLOSE_BTN_STYLE)
                            mr_br_x.setToolTip(
                                f'Clear pinned MR data for {tag}')
                            mr_br_x.clicked.connect(
                                lambda checked, t=tag:
                                    self._clear_pinned_mr_data(t)
                            )
                            mr_br_layout.addWidget(
                                mr_br_x, 0, Qt.AlignVCenter)

                            mr_br_label = ElidedLabel(mr_branch)
                            mr_br_label.setAlignment(Qt.AlignCenter)
                            mr_br_label.setToolTip(mr_branch)
                            mr_br_layout.addWidget(mr_br_label, 1)

                            # Ensure a table item exists with the tooltip
                            # so the cell-widget tooltip path can find it.
                            # Clear display text so it doesn't render
                            # through behind the widget.
                            item = self.table.item(row, self.COL_MR_BRANCH)
                            if not item:
                                item = QTableWidgetItem('')
                                self.table.setItem(
                                    row, self.COL_MR_BRANCH, item)
                            item.setText('')
                            item.setToolTip(mr_branch)
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
            # Re-apply row hover highlight (widgets were replaced during rebuild)
            if getattr(self, '_hovered_row', -1) >= 0:
                self._apply_hover_to_row(self._hovered_row, True)

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
        # Dynamically show/hide Slack column if install state changed
        slack_now = self._is_slack_installed()
        if slack_now != self._slack_available:
            self._slack_available = slack_now
            self.table.setColumnHidden(self.COL_SLACK, not slack_now)
        self._update_table()
        self._update_slack_bot_button()
        self._check_slack_bot_transition()
        dock_enabled = get_dock_enabled(self._prefs)
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

    def _show_queue_context_menu(
        self, label: QLabel, pos: 'QPoint', tag: str,
    ) -> None:
        """Show right-click context menu on the Queue column label."""
        # Read live session data so the checkmark and force-send state
        # are always accurate.
        current_mode = 'pause'
        queue_size = 0
        for s in self.sessions:
            if s['tag'] == tag:
                current_mode = s.get('auto_send_mode', 'pause')
                queue_size = s.get('queue_size', 0)
                break

        menu = QMenu(self)

        force_action = menu.addAction('Force-send next queued message')
        force_action.setEnabled(queue_size > 0)
        force_action.triggered.connect(
            lambda _checked, t=tag: self._force_send_next(t)
        )

        menu.addSeparator()

        pause_action = menu.addAction('Pause on input (default)')
        pause_action.setCheckable(True)
        pause_action.setChecked(current_mode == 'pause')
        pause_action.triggered.connect(
            lambda _checked, t=tag: self._set_auto_send_mode(t, 'pause')
        )

        always_action = menu.addAction('Always send')
        always_action.setCheckable(True)
        always_action.setChecked(current_mode == 'always')
        always_action.triggered.connect(
            lambda _checked, t=tag: self._set_auto_send_mode(t, 'always')
        )

        menu.exec_(label.mapToGlobal(pos))

    def _set_auto_send_mode(self, tag: str, mode: str) -> None:
        """Send set_auto_send_mode to the CQ server."""
        from claudeq.utils.constants import SOCKET_DIR

        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'set_auto_send_mode', 'mode': mode},
        )
        if response and response.get('status') == 'ok':
            # Update local session data immediately so the next menu
            # open (before the background refresh) shows the new mode.
            for s in self.sessions:
                if s['tag'] == tag:
                    s['auto_send_mode'] = mode
                    break
            # Invalidate cache so next refresh rebuilds with new mode
            self._cell_cache.pop((tag, 'queue'), None)
            self._show_status(f'Auto-send mode: {mode}')
        else:
            self._show_status(f'Failed to set auto-send mode for {tag}')

    def _force_send_next(self, tag: str) -> None:
        """Force-send the next queued message to the CQ server."""
        from claudeq.utils.constants import SOCKET_DIR

        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'force_send'},
        )
        if response and response.get('status') == 'sent':
            self._show_status(f'Force-sent queued message for {tag}')
            self._refresh_data()
        elif response and response.get('status') == 'empty':
            self._show_status(f'No queued messages for {tag}')
        else:
            self._show_status(f'Failed to force-send for {tag}')

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

    # -- Template combo helpers (shared logic for MR and direct combos) ----

    @staticmethod
    def _populate_combo(
        combo: 'QComboBox',
        load_selected_fn: 'Callable[[], str]',
        default_tooltip: str,
    ) -> None:
        """Populate a template combo from saved presets.

        Args:
            combo: The QComboBox to populate.
            load_selected_fn: Function returning the currently selected name.
            default_tooltip: Tooltip when no truncated name is active.
        """
        combo.blockSignals(True)
        combo.clear()
        combo.addItem('(None)')
        for name in sorted(load_saved_templates().keys()):
            if len(name) > MAX_COMBO_DISPLAY:
                combo.addItem(name[:MAX_COMBO_DISPLAY] + '\u2026')
                combo.setItemData(combo.count() - 1, name, Qt.UserRole)
            else:
                combo.addItem(name)
        selected = load_selected_fn()
        if selected and len(selected) > MAX_COMBO_DISPLAY:
            display = selected[:MAX_COMBO_DISPLAY] + '\u2026'
            idx = combo.findText(display)
        else:
            idx = combo.findText(selected) if selected else 0
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)
        TableBuilderMixin._update_combo_tooltip(combo, default_tooltip)

    @staticmethod
    def _on_combo_changed(
        combo: 'QComboBox',
        save_fn: 'Callable[[str], None]',
        default_tooltip: str,
    ) -> None:
        """Handle a template combo selection change.

        Args:
            combo: The QComboBox that changed.
            save_fn: Function to persist the selected name.
            default_tooltip: Tooltip when no truncated name is active.
        """
        text = combo.currentText()
        if text == '(None)':
            save_fn('')
            TableBuilderMixin._update_combo_tooltip(combo, default_tooltip)
            return
        idx = combo.currentIndex()
        full_name = combo.itemData(idx, Qt.UserRole)
        save_fn(full_name if full_name else text)
        TableBuilderMixin._update_combo_tooltip(combo, default_tooltip)

    @staticmethod
    def _update_combo_tooltip(combo: 'QComboBox', default_tooltip: str) -> None:
        """Set combo tooltip to the full name when truncated, else default."""
        idx = combo.currentIndex()
        full_name = combo.itemData(idx, Qt.UserRole) if idx >= 0 else None
        combo.setToolTip(full_name if full_name else default_tooltip)

    # -- Public combo wrappers (called by app and signals) ----------------

    def _populate_template_combo(self) -> None:
        """Reload template combo items from saved presets and selection."""
        self._populate_combo(
            self.template_combo, load_selected_template_name,
            MR_TEMPLATE_TOOLTIP,
        )

    def _on_template_combo_changed(self) -> None:
        """Handle template combo selection change."""
        self._on_combo_changed(
            self.template_combo, save_selected_template_name,
            MR_TEMPLATE_TOOLTIP,
        )

    def _populate_direct_template_combo(self) -> None:
        """Reload direct template combo items from saved presets and selection."""
        self._populate_combo(
            self.direct_template_combo, load_selected_direct_template_name,
            QUICK_MSG_TEMPLATE_TOOLTIP,
        )

    def _on_direct_template_combo_changed(self) -> None:
        """Handle direct template combo selection change."""
        self._on_combo_changed(
            self.direct_template_combo, save_selected_direct_template_name,
            QUICK_MSG_TEMPLATE_TOOLTIP,
        )

    def _apply_tooltips_setting(self) -> None:
        """Sync the tooltip app with the current preference."""
        if hasattr(self, '_tooltip_app'):
            self._tooltip_app.tooltips_enabled = self._prefs.get('show_tooltips', True)

    def _is_slack_installed(self) -> bool:
        """Check if the Slack app config file exists."""
        from claudeq.slack.config import is_slack_installed
        return is_slack_installed()

    def _show_slack_bot_not_running(self) -> None:
        """Show an informational popup when the Slack bot is not running."""
        QMessageBox.information(
            self, 'No Slack Bot Running',
            'Start the Slack bot using the Slack Bot button in the toolbar,\n'
            'or run  cq --slack  in a terminal.',
        )

    def _check_slack_bot_transition(self) -> None:
        """Detect Slack bot start/stop transitions and show status messages."""
        bot_running = self._is_slack_bot_running()
        was_running = self._slack_bot_was_running
        if bot_running == was_running:
            return
        self._slack_bot_was_running = bot_running

        slack_sessions = [
            s for s in self.sessions
            if s.get('slack_enabled') and not s.get('is_dead', True)
        ]
        count = len(slack_sessions)

        if not bot_running and count:
            self._show_status(
                f'Slack bot stopped — {count} session(s) disconnected')
        elif bot_running and count:
            self._show_status(
                f'Slack bot reconnected — {count} session(s) restored')

    def _toggle_slack(self, tag: str, enabled: bool) -> None:
        """Send set_slack to the CQ server to enable/disable Slack."""
        from claudeq.utils.constants import SOCKET_DIR

        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'set_slack', 'enabled': enabled},
        )
        if response and response.get('status') == 'ok':
            # Invalidate cache so next refresh rebuilds
            self._cell_cache.pop((tag, 'slack'), None)
            action = 'enabled' if enabled else 'disabled'
            self._show_status(f'Slack {action} for {tag}')
        else:
            self._show_status(f'Failed to toggle Slack for {tag}')

    def _open_slack_thread(self, tag: str) -> None:
        """Open the Slack thread for a session in the Slack app or browser.

        Prefers the native Slack app via ``slack://channel`` deep link.
        Falls back to the web client URL when the app is not installed.
        """
        from claudeq.slack.config import (
            load_slack_config, load_slack_sessions, resolve_team_id,
        )

        config = load_slack_config()
        channel_id = config.get('dm_channel_id', '')

        if not channel_id:
            self._show_status('Slack not configured (missing dm_channel_id)')
            return

        team_id = resolve_team_id()
        sessions = load_slack_sessions()
        thread_ts = sessions.get(tag, {}).get('thread_ts', '')

        # Try native Slack app first
        slack_app_installed = any(
            p.is_dir() for p in (
                Path('/Applications/Slack.app'),
                Path.home() / 'Applications' / 'Slack.app',
            )
        )

        if slack_app_installed and team_id:
            deep = f'slack://channel?team={team_id}&id={channel_id}'
            if thread_ts:
                # Thread-level: use message permalink format
                ts_no_dot = thread_ts.replace('.', '')
                deep = (f'slack://channel?team={team_id}'
                        f'&id={channel_id}&message={ts_no_dot}')
            subprocess.Popen(
                ['open', deep],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return

        # Fallback: open in browser
        if team_id:
            url = f'https://app.slack.com/client/{team_id}/{channel_id}'
            if thread_ts:
                url += f'/thread/{channel_id}-{thread_ts}'
        else:
            url = f'https://app.slack.com/client/{channel_id}'

        webbrowser.open(url)

    def _check_row_hover(self) -> None:
        """Poll cursor position to track which table row is hovered."""
        from PyQt5.QtGui import QCursor

        # Keep hover locked while a context menu is open
        if QApplication.activePopupWidget():
            return

        viewport = self.table.viewport()
        local_pos = viewport.mapFromGlobal(QCursor.pos())

        if viewport.rect().contains(local_pos):
            index = self.table.indexAt(local_pos)
            row = index.row() if index.isValid() else -1
        else:
            row = -1

        if row != self._hovered_row:
            old = self._hovered_row
            self._hovered_row = row
            self.table.setProperty('_hovered_row', row)
            self._apply_hover_to_row(old, False)
            self._apply_hover_to_row(row, True)
            viewport.update()
