"""Table construction, refresh, settings, and template editor methods."""

from __future__ import annotations

import logging
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PyQt5 import sip
from PyQt5.QtWidgets import (
    QAction, QApplication, QComboBox, QHBoxLayout, QInputDialog, QLabel,
    QMenu, QMessageBox, QPushButton, QTableWidgetItem, QWidget,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPalette

from claudeq.monitor.mr_tracking.base import MRState
from claudeq.monitor.mr_tracking.config import (
    get_dock_enabled, get_notification_prefs, load_cq_direct_template,
    load_saved_templates, load_selected_direct_template_name,
    load_selected_template_name,
    save_selected_direct_template_name, save_selected_template_name,
)
from claudeq.monitor.session_manager import get_active_sessions
from claudeq.utils.socket_utils import send_socket_request
from claudeq.monitor.scm_polling import SessionRefreshWorker
from claudeq.monitor.ui.ui_widgets import ElidedLabel, IndicatorLabel, PulsingLabel
from claudeq.monitor.ui.table_helpers import (
    ACTIVE_BTN_STYLE, BORDER_GROUP, BORDER_INTRA, CLOSE_BTN_STYLE,
    MAX_COMBO_DISPLAY, MENU_BTN_STYLE, MR_TEMPLATE_TOOLTIP,
    QUICK_MSG_SEND_AT_END, QUICK_MSG_SEND_NEXT, QUICK_MSG_TEMPLATE_TOOLTIP,
    HoverIconButton, column_border_type,
    _GIT_BRANCH_SVG, _OPEN_EXTERNAL_SVG, _SEND_SVG, _THREE_DOT_SVG,
)

if TYPE_CHECKING:
    from claudeq.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object

logger = logging.getLogger(__name__)


class TableBuilderMixin(_Base):
    """Methods for table construction, cell helpers, refresh, settings, and template editor."""

    _CENTER_COLS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11})  # All data columns

    def _set_cell_widget(self, row: int, col: int, widget: QWidget) -> None:
        """Set a cell widget wrapped in a hover-aware container.

        All cell widgets are wrapped so the row hover highlight can be
        toggled uniformly via the ``_hover`` dynamic property.  Columns
        at group boundaries additionally get a right border.
        """
        border = column_border_type(col, self.table)
        if border == BORDER_GROUP:
            border_css = 'border-right: 1px solid white; '
        elif border == BORDER_INTRA:
            border_css = 'border-right: 1px solid rgba(255, 255, 255, 50); '
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

    def _should_show_mr_fire(self, tag: str) -> bool:
        """Return True if the MR fire indicator should be shown for *tag*."""
        threshold = self._prefs.get('new_status_seconds', 60)
        if threshold <= 0:
            return False
        if tag in self._dismissed_mr_new_status:
            return False
        entry = self._mr_changed_at.get(tag)
        if entry is None:
            return False
        changed_at = entry[1]
        return (time.time() - changed_at) < threshold

    def _mr_fire_tooltip(self, tag: str) -> str:
        """Build tooltip text for the MR fire indicator."""
        entry = self._mr_changed_at.get(tag)
        if not entry:
            return ''
        ago = int(time.time() - entry[1])
        return f'MR status changed {ago}s ago \u2014 click to dismiss'

    def _build_path_cell(self, row: int, tag: str, path_text: str) -> None:
        """Build the Path column cell: elided label + 3-dot menu button.

        The 3-dot button and right-click on the label both open the path
        actions menu (Open in Terminal, Open with IDE).  Disabled when
        path_text is 'N/A'.
        """
        path_state = (path_text,)
        if self._cell_cached(tag, 'path', path_state, row, self.COL_PATH):
            return

        has_path = path_text != 'N/A'
        path_container = QWidget()
        path_layout = QHBoxLayout(path_container)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(2)

        path_label = ElidedLabel(path_text)
        path_label.setAlignment(Qt.AlignCenter)
        path_label.setToolTip(path_text)
        if has_path:
            path_label.setContextMenuPolicy(Qt.CustomContextMenu)
            path_label.customContextMenuRequested.connect(
                lambda _pos, t=tag: self._show_path_menu(t)
            )
        path_layout.addWidget(path_label, 1)

        path_menu_btn = HoverIconButton(_OPEN_EXTERNAL_SVG, 14)
        path_menu_btn.setFixedSize(22, path_menu_btn.sizeHint().height())
        path_menu_btn.setStyleSheet(MENU_BTN_STYLE)
        path_menu_btn.setToolTip('Open in Terminal / IDE' if has_path
                                 else 'No project path available')
        path_menu_btn.setEnabled(has_path)
        if has_path:
            path_menu_btn.clicked.connect(
                lambda checked, t=tag: self._show_path_menu(t))
        path_layout.addWidget(path_menu_btn, 0, Qt.AlignVCenter)

        # Ensure a table item exists with the tooltip so the
        # cell-widget tooltip path can show truncated text.
        item = self.table.item(row, self.COL_PATH)
        if not item:
            item = QTableWidgetItem('')
            self.table.setItem(row, self.COL_PATH, item)
        item.setText('')
        item.setToolTip(path_text)
        self._set_cell_widget(row, self.COL_PATH, path_container)
        self._cache_cell(tag, 'path', path_state, row, self.COL_PATH)

    def _build_branch_cell(self, row: int, tag: str, branch_text: str) -> None:
        """Build the Server Branch column cell: label + git icon button.

        The git icon button and right-click on the label both open the git
        changes menu.  Disabled when branch_text is 'N/A'.
        """
        branch_state = (branch_text,)
        if self._cell_cached(tag, 'server_branch', branch_state,
                             row, self.COL_SERVER_BRANCH):
            return

        has_git = branch_text != 'N/A' and self._has_git_project(tag)
        branch_container = QWidget()
        branch_layout = QHBoxLayout(branch_container)
        branch_layout.setContentsMargins(0, 0, 0, 0)
        branch_layout.setSpacing(2)

        branch_label = ElidedLabel(branch_text)
        branch_label.setAlignment(Qt.AlignCenter)
        branch_label.setToolTip(branch_text)
        if has_git:
            branch_label.setContextMenuPolicy(Qt.CustomContextMenu)
            branch_label.customContextMenuRequested.connect(
                lambda _pos, t=tag: self._show_git_menu(t)
            )
        branch_layout.addWidget(branch_label, 1)

        git_btn = HoverIconButton(_GIT_BRANCH_SVG, 14)
        git_btn.setFixedSize(22, git_btn.sizeHint().height())
        git_btn.setStyleSheet(MENU_BTN_STYLE)
        git_btn.setToolTip('Git Changes' if has_git
                           else 'No git project detected')
        git_btn.setEnabled(has_git)
        if has_git:
            git_btn.clicked.connect(
                lambda checked, t=tag: self._show_git_menu(t))
        branch_layout.addWidget(git_btn, 0, Qt.AlignVCenter)

        # Ensure a table item exists with the tooltip so the
        # cell-widget tooltip path can show truncated text.
        item = self.table.item(row, self.COL_SERVER_BRANCH)
        if not item:
            item = QTableWidgetItem('')
            self.table.setItem(row, self.COL_SERVER_BRANCH, item)
        item.setText('')
        item.setToolTip(branch_text)
        self._set_cell_widget(row, self.COL_SERVER_BRANCH, branch_container)
        self._cache_cell(tag, 'server_branch', branch_state,
                         row, self.COL_SERVER_BRANCH)

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

        # Dismiss any active tooltip before destroying/recreating cell
        # widgets — Qt's tooltip timer may hold a reference to a widget
        # that is about to be deleted, causing a SIGSEGV.
        from PyQt5.QtWidgets import QToolTip
        QToolTip.hideText()

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
                    remote_path = pinned_data.get('remote_project_path', '')
                    dead_project = (remote_path.rsplit('/', 1)[-1]
                                    if remote_path
                                    else 'N/A')
                    self._set_cell_text(row, self.COL_PROJECT, dead_project)
                    self._build_path_cell(row, tag, 'N/A')
                    self._build_branch_cell(row, tag, 'N/A')
                    # Remove the live status cell widget (coloured
                    # indicator + label) before switching to plain text,
                    # otherwise the old widget renders on top of "N/A".
                    self.table.removeCellWidget(row, self.COL_STATUS)
                    self._cell_cache.pop((tag, 'status'), None)
                    self._set_cell_text(row, self.COL_STATUS, 'N/A')
                    status_item = self.table.item(row, self.COL_STATUS)
                    if status_item:
                        status_item.setForeground(QColor(255, 255, 255))

                    # Queue N/A with menu button
                    dead_q_state = ('dead', session.get('auto_send_mode', 'pause'))
                    if not self._cell_cached(tag, 'queue', dead_q_state,
                                             row, self.COL_QUEUE):
                        dq_container = QWidget()
                        dq_layout = QHBoxLayout(dq_container)
                        dq_layout.setContentsMargins(0, 0, 0, 0)
                        dq_layout.setSpacing(2)

                        dq_menu_btn = HoverIconButton(_THREE_DOT_SVG, 14)
                        dq_menu_btn.setFixedSize(
                            24, dq_menu_btn.sizeHint().height())
                        dq_menu_btn.setStyleSheet(MENU_BTN_STYLE)
                        dq_menu_btn.setToolTip('Queue options')
                        dq_menu_btn.clicked.connect(
                            lambda checked, btn=dq_menu_btn, t=tag:
                                self._show_queue_context_menu(
                                    btn, btn.rect().bottomLeft(), t)
                        )
                        dq_layout.addWidget(
                            dq_menu_btn, 0, Qt.AlignVCenter)

                        dq_label = QLabel('N/A')
                        dq_label.setAlignment(Qt.AlignCenter)
                        dq_layout.addWidget(dq_label, 1)

                        dq_action_btn = HoverIconButton(_SEND_SVG, 14)
                        dq_action_btn.setFixedSize(
                            24, dq_action_btn.sizeHint().height())
                        dq_action_btn.setStyleSheet(MENU_BTN_STYLE)
                        dq_action_btn.setEnabled(False)
                        dq_action_btn.setToolTip('Send options (server offline)')
                        dq_layout.addWidget(
                            dq_action_btn, 0, Qt.AlignVCenter)

                        item = self.table.item(row, self.COL_QUEUE)
                        if item:
                            item.setText('')
                        self._set_cell_widget(
                            row, self.COL_QUEUE, dq_container)
                        self._cache_cell(tag, 'queue', dead_q_state,
                                         row, self.COL_QUEUE)
                else:
                    self._set_cell_text(row, self.COL_PROJECT, session['project'])
                    live_path = session.get('project_path', '') or ''
                    self._build_path_cell(row, tag, live_path or 'N/A')
                    self._build_branch_cell(row, tag, server_branch)

                    claude_state = session.get('claude_state', 'idle')
                    state_display = {
                        'idle': ('\u25cb Idle', None),
                        'running': ('\u25cf Running', QColor(76, 175, 80)),
                        'needs_permission': ('\u25b2 Permission', QColor(255, 152, 0)),
                        'has_question': ('\u25c6 Question', QColor(100, 181, 246)),
                        'interrupted': ('\u25c7 Interrupted', QColor(255, 213, 79)),
                    }
                    text, color = state_display.get(claude_state, (claude_state, None))

                    # Track state changes and show fire indicator for recent ones
                    prev = self._state_changed_at.get(tag)
                    now = time.time()
                    if prev is None:
                        # First time seeing this tag — seed with epoch 0
                        # so the fire indicator doesn't flash on startup.
                        self._state_changed_at[tag] = (claude_state, 0)
                    elif prev[0] != claude_state:
                        self._state_changed_at[tag] = (claude_state, now)
                        # Reset dismissal when state changes again
                        self._dismissed_new_status.discard(tag)
                    show_fire = False
                    threshold = self._prefs.get('new_status_seconds', 60)
                    if (
                        threshold > 0
                        and claude_state not in ('running', 'interrupted')
                        and tag not in self._dismissed_new_status
                    ):
                        changed_at = self._state_changed_at[tag][1]
                        if (now - changed_at) < threshold:
                            show_fire = True

                    state_explanations = {
                        'idle': 'Claude is waiting for input — will accept next queued message',
                        'running': 'Claude is actively processing a request',
                        'needs_permission': 'Claude needs your permission',
                        'has_question': 'Claude is asking a clarifying question',
                        'interrupted': 'Claude was interrupted — will accept next queued message',
                    }

                    color_key = color.name() if color else 'white'
                    status_state = (text, show_fire, color_key)
                    if not self._cell_cached(tag, 'status', status_state,
                                             row, self.COL_STATUS):
                        container = QWidget()
                        c_layout = QHBoxLayout(container)
                        c_layout.setContentsMargins(0, 0, 2, 0)
                        c_layout.setSpacing(0)

                        # Left spacer balances the fire icon width
                        spacer = QWidget()
                        spacer.setFixedWidth(18)
                        c_layout.addWidget(spacer)

                        # Centered status text (ElidedLabel for "..."
                        # when truncated; palette color so the custom
                        # paintEvent picks it up)
                        status_label = ElidedLabel(text)
                        status_label.setAlignment(Qt.AlignCenter)
                        pal = status_label.palette()
                        pal.setColor(
                            QPalette.WindowText,
                            color if color else QColor(255, 255, 255),
                        )
                        status_label.setPalette(pal)
                        c_layout.addWidget(status_label, 1)

                        # Right-aligned fire icon (always occupies
                        # space; text hidden when inactive to keep
                        # the centered label stable)
                        fire_label = QLabel(
                            '\U0001f525' if show_fire else '')
                        fire_label.setObjectName('_fireLabel')
                        fire_label.setFixedWidth(18)
                        fire_label.setAlignment(
                            Qt.AlignRight | Qt.AlignVCenter)
                        c_layout.addWidget(fire_label)

                        # Click: "interrupted" → force-send menu;
                        # other states → dismiss fire indicator.
                        def _make_click(
                            t: str = tag,
                            st: str = claude_state,
                            w: QWidget = container,
                        ) -> Callable:
                            def _on_click(event: object) -> None:
                                if st == 'interrupted':
                                    self._show_status_action_menu(w, t)
                                elif t not in self._dismissed_new_status:
                                    self._dismissed_new_status.add(t)
                                    self._update_table()
                            return _on_click
                        container.mousePressEvent = _make_click()

                        # Ensure a table item exists so the
                        # cell-widget tooltip path can find it.
                        s_item = self.table.item(row, self.COL_STATUS)
                        if not s_item:
                            s_item = QTableWidgetItem('')
                            self.table.setItem(
                                row, self.COL_STATUS, s_item)
                        s_item.setText('')

                        self._set_cell_widget(
                            row, self.COL_STATUS, container)
                        self._cache_cell(tag, 'status', status_state,
                                         row, self.COL_STATUS)

                    # Update tooltips every refresh (explanation and
                    # fire-ago text can change).
                    # Item tooltip = value only (for truncation path).
                    # _extra_tooltip on cell widget = explanation
                    #   (combined by tooltip handler when truncated).
                    # ElidedLabel tooltip = explanation (shown by
                    #   widget tooltip path when not truncated).
                    s_item = self.table.item(row, self.COL_STATUS)
                    w = self.table.cellWidget(row, self.COL_STATUS)
                    explanation = ''
                    if self._prefs.get('show_tooltips', True):
                        explanation = state_explanations.get(
                            claude_state, '')
                        if show_fire and explanation:
                            ago = int(
                                now - self._state_changed_at[tag][1])
                            explanation += (
                                f' (changed {ago}s ago'
                                ' — click to dismiss)')
                    if s_item:
                        s_item.setToolTip(text)
                    if w:
                        w.setProperty('_extra_tooltip',
                                      explanation or None)
                        label = w.findChild(ElidedLabel)
                        if label:
                            label.setToolTip(explanation)

                    # Queue column with menu button on the left
                    auto_send_mode = session.get('auto_send_mode', 'pause')
                    queue_size = session['queue_size']
                    q_state = (queue_size, auto_send_mode)
                    if not self._cell_cached(tag, 'queue', q_state,
                                             row, self.COL_QUEUE):
                        q_container = QWidget()
                        q_layout = QHBoxLayout(q_container)
                        q_layout.setContentsMargins(0, 0, 0, 0)
                        q_layout.setSpacing(2)

                        q_menu_btn = HoverIconButton(_THREE_DOT_SVG, 14)
                        q_menu_btn.setFixedSize(
                            24, q_menu_btn.sizeHint().height())
                        q_menu_btn.setStyleSheet(MENU_BTN_STYLE)
                        q_menu_btn.setToolTip('Queue options')
                        q_menu_btn.clicked.connect(
                            lambda checked, btn=q_menu_btn, t=tag:
                                self._show_queue_context_menu(
                                    btn, btn.rect().bottomLeft(), t)
                        )
                        q_layout.addWidget(
                            q_menu_btn, 0, Qt.AlignVCenter)

                        q_label = QLabel(str(queue_size))
                        q_label.setAlignment(Qt.AlignCenter)
                        q_layout.addWidget(q_label, 1)

                        q_action_btn = HoverIconButton(_SEND_SVG, 14)
                        q_action_btn.setFixedSize(
                            24, q_action_btn.sizeHint().height())
                        q_action_btn.setStyleSheet(MENU_BTN_STYLE)
                        q_action_btn.setToolTip('Send options')
                        q_action_btn.clicked.connect(
                            lambda checked, btn=q_action_btn, t=tag:
                                self._show_queue_action_menu(
                                    btn, btn.rect().bottomLeft(), t)
                        )
                        q_layout.addWidget(
                            q_action_btn, 0, Qt.AlignVCenter)

                        # Clear underlying item text
                        item = self.table.item(row, self.COL_QUEUE)
                        if item:
                            item.setText('')
                        self._set_cell_widget(
                            row, self.COL_QUEUE, q_container)
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
                            server_btn.setProperty('always_tooltip', True)
                        else:
                            server_btn = QPushButton('Server')
                            server_btn.setStyleSheet(ACTIVE_BTN_STYLE)
                            server_btn.setToolTip(
                                f'Jump to server terminal for {tag}')
                        server_btn.clicked.connect(
                            lambda checked, t=tag:
                                self._focus_session(t, 'server')
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
                    mr_state = ('tracked', self._should_show_mr_fire(tag))
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

                        # Right-aligned fire icon (matches Status column
                        # pattern — always occupies space; text hidden when
                        # inactive to keep the centered label stable)
                        show_mr_fire = self._should_show_mr_fire(tag)
                        mr_fire_label = QLabel(
                            '\U0001f525' if show_mr_fire else '')
                        mr_fire_label.setObjectName('_mrFireLabel')
                        mr_fire_label.setFixedWidth(24)
                        mr_fire_label.setAlignment(
                            Qt.AlignRight | Qt.AlignVCenter)
                        if show_mr_fire:
                            mr_fire_label.setToolTip(
                                self._mr_fire_tooltip(tag))

                        def _make_mr_dismiss(t: str = tag) -> Callable:
                            def _dismiss(event: object) -> None:
                                if t not in self._dismissed_mr_new_status:
                                    self._dismissed_mr_new_status.add(t)
                                    self._update_table()
                            return _dismiss
                        mr_fire_label.mousePressEvent = _make_mr_dismiss()
                        mr_layout.addWidget(mr_fire_label)

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
                    mr_state = ('untracked', is_dead,
                                    bool(pinned_data.get('remote_project_path')))
                    if not self._cell_cached(tag, 'mr', mr_state,
                                             row, self.COL_MR):
                        if self.table.columnSpan(row, self.COL_MR) > 1:
                            self.table.setSpan(row, self.COL_MR, 1, 1)
                        is_mr_pinned_row = bool(
                            pinned_data.get('remote_project_path'))
                        track_btn = QPushButton('Track MR')
                        if is_dead and not is_mr_pinned_row:
                            track_btn.setToolTip(
                                'Start a server first to discover MR from branch')
                            track_btn.setEnabled(False)
                        else:
                            track_btn.setToolTip(
                                f'Start tracking MR/PR for {tag}')
                        track_btn.setStyleSheet('font-size: 11px;')
                        track_btn.clicked.connect(
                            lambda checked, t=tag: self._start_tracking(t)
                        )
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
        """Clean up the refresh worker reference after it completes.

        Uses sender() to identify the actual worker that emitted ``finished``,
        avoiding a race where a *new* worker has already replaced
        ``self._refresh_worker`` (e.g. after sleep/wake timer bursts).
        """
        worker = self.sender()
        if worker is not None:
            worker.deleteLater()
        if self._refresh_worker is worker:
            self._refresh_worker = None

    def _on_sessions_refreshed(self, sessions: list) -> None:
        """Handle background session refresh result."""
        self.sessions = self._merge_sessions(sessions)
        # Dynamically show/hide Slack column if install state changed
        slack_now = self._is_slack_installed()
        if slack_now != self._slack_available:
            self._slack_available = slack_now
            if not slack_now:
                self.table.setColumnHidden(self.COL_SLACK, True)
            else:
                # Only un-hide if user hasn't explicitly hidden it
                hidden = self._prefs.get('hidden_columns', [])
                if 'Slack' not in hidden:
                    self.table.setColumnHidden(self.COL_SLACK, False)
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
        from claudeq.utils.constants import load_settings, save_settings

        server_settings = load_settings()
        dialog = SettingsDialog(
            current_terminal=self._prefs.get('default_terminal', 'Terminal.app'),
            current_repos_dir=self._prefs.get('repos_dir', DEFAULT_REPOS_DIR),
            active_paths_fn=self._get_active_project_paths,
            log_fn=self._show_status,
            show_tooltips=self._prefs.get('show_tooltips', True),
            notification_prefs=get_notification_prefs(self._prefs),
            current_auto_send_mode=server_settings.get('auto_send_mode', 'pause'),
            current_diff_tool=self._prefs.get('default_diff_tool', ''),
            new_status_seconds=self._prefs.get('new_status_seconds', 60),
            current_global_shortcut=self._prefs.get('global_shortcut', ''),
            parent=self,
        )
        if dialog.exec_():
            self._prefs['default_terminal'] = dialog.selected_terminal()
            self._prefs['repos_dir'] = dialog.selected_repos_dir()
            self._prefs['show_tooltips'] = dialog.show_tooltips()
            self._prefs['notifications'] = dialog.notification_prefs()
            self._prefs['default_diff_tool'] = dialog.selected_diff_tool()
            self._prefs['new_status_seconds'] = dialog.new_status_seconds()
            old_shortcut = self._prefs.get('global_shortcut', '')
            new_shortcut = dialog.selected_global_shortcut()
            self._prefs['global_shortcut'] = new_shortcut
            self._save_prefs()
            # Save auto-send mode to server settings (read by new servers)
            server_settings['auto_send_mode'] = dialog.selected_auto_send_mode()
            save_settings(server_settings)
            self._apply_tooltips_setting()
            if new_shortcut != old_shortcut:
                self._register_global_shortcut()
            self._show_status('Settings saved')

    def _show_queue_context_menu(
        self, label: QLabel, pos: 'QPoint', tag: str,
    ) -> None:
        """Show context menu on the Queue column left button."""
        current_mode = 'pause'
        for s in self.sessions:
            if s['tag'] == tag:
                current_mode = s.get('auto_send_mode', 'pause')
                break

        menu = QMenu(self)
        if self._prefs.get('show_tooltips', True):
            menu.setToolTipsVisible(True)

        pause_action = menu.addAction('Pause on input (default)')
        pause_action.setCheckable(True)
        pause_action.setChecked(current_mode == 'pause')
        pause_action.setToolTip(
            'Auto-send queued messages only when Claude is idle.\n'
            '\n'
            '\u25cb Idle — sends next queued message\n'
            '\u25cf Running — waits until finished\n'
            '\u25b2 Permission — waits (does not interrupt)\n'
            '\u25c6 Question — waits (does not interrupt)\n'
            '\u25c7 Interrupted — waits (needs manual resume)')
        pause_action.triggered.connect(
            lambda _checked, t=tag: self._set_auto_send_mode(t, 'pause')
        )

        always_action = menu.addAction('Always send')
        always_action.setCheckable(True)
        always_action.setChecked(current_mode == 'always')
        always_action.setToolTip(
            'Auto-send queued messages whenever Claude is\n'
            'not actively running — even if waiting for input.\n'
            '\n'
            '\u25cb Idle — sends next queued message\n'
            '\u25cf Running — waits until finished\n'
            '\u25b2 Permission — sends (interrupts the prompt)\n'
            '\u25c6 Question — sends (interrupts the prompt)\n'
            '\u25c7 Interrupted — waits (needs manual resume)')
        always_action.triggered.connect(
            lambda _checked, t=tag: self._set_auto_send_mode(t, 'always')
        )

        menu.addSeparator()

        clear_action = menu.addAction('Clear queue')
        clear_action.setToolTip('Delete all queued messages without sending them')
        clear_action.triggered.connect(
            lambda _checked, t=tag: self._clear_queue(t)
        )

        menu.exec_(label.mapToGlobal(pos))
        # Clear stuck hover state after menu closes
        if not sip.isdeleted(label):
            label.setAttribute(Qt.WA_UnderMouse, False)
            label.update()

    def _show_queue_action_menu(
        self, btn: QPushButton, pos: 'QPoint', tag: str,
    ) -> None:
        """Show send-options menu on the Queue column right button."""
        queue_size = 0
        for s in self.sessions:
            if s['tag'] == tag:
                queue_size = s.get('queue_size', 0)
                break

        menu = QMenu(self)
        if self._prefs.get('show_tooltips', True):
            menu.setToolTipsVisible(True)

        force_action = menu.addAction('Force-send next queued message')
        force_action.setEnabled(queue_size > 0)
        force_action.setToolTip(
            'Send the next queued message immediately,\n'
            'even if Claude is still running')
        force_action.triggered.connect(
            lambda _checked, t=tag: self._force_send_next(t)
        )

        menu.addSeparator()

        msg_next_action = menu.addAction('Send message next')
        msg_next_action.setToolTip(
            'Type a message and insert it at the front\n'
            'of the queue (sent before other queued messages)')
        msg_next_action.triggered.connect(
            lambda _checked, t=tag: self._send_immediate_message(t, at_end=False)
        )

        msg_end_action = menu.addAction('Send message to end')
        msg_end_action.setToolTip(
            'Type a message and add it to the end of the queue')
        msg_end_action.triggered.connect(
            lambda _checked, t=tag: self._send_immediate_message(t, at_end=True)
        )

        menu.addSeparator()

        next_action = QAction(QUICK_MSG_SEND_NEXT, self)
        next_action.setToolTip(
            'Send the active message-bundle preset\n'
            'and insert it at the front of the queue')
        next_action.triggered.connect(lambda: self._quick_send_next(tag))
        menu.addAction(next_action)

        end_action = QAction(QUICK_MSG_SEND_AT_END, self)
        end_action.setToolTip(
            'Send the active message-bundle preset\n'
            'and add it to the end of the queue')
        end_action.triggered.connect(lambda: self._quick_send_at_end(tag))
        menu.addAction(end_action)

        menu.exec_(btn.mapToGlobal(pos))

    def _set_auto_send_mode(self, tag: str, mode: str) -> None:
        """Send set_auto_send_mode to the CQ server."""
        from claudeq.utils.constants import SOCKET_DIR
        from claudeq.monitor.mr_tracking.config import save_pinned_sessions

        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'set_auto_send_mode', 'mode': mode},
        )
        # Update local session data immediately so the next menu
        # open (before the background refresh) shows the new mode.
        for s in self.sessions:
            if s['tag'] == tag:
                s['auto_send_mode'] = mode
                break
        # Persist in pinned sessions so dead rows survive refresh cycles
        if tag in self._pinned_sessions:
            self._pinned_sessions[tag]['auto_send_mode'] = mode
            save_pinned_sessions(self._pinned_sessions)
        # Invalidate cache so next refresh rebuilds with new mode
        self._cell_cache.pop((tag, 'queue'), None)
        if response and response.get('status') == 'ok':
            self._show_status(f'Auto-send mode: {mode}')
        else:
            self._show_status(f'Auto-send mode: {mode} (server offline)')

    def _clear_queue(self, tag: str) -> None:
        """Clear all queued messages for a session without sending them."""
        from claudeq.utils.constants import SOCKET_DIR

        reply = QMessageBox.question(
            self, 'Clear Queue',
            f'Delete all queued messages for "{tag}"?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'clear_queue'},
        )
        self._cell_cache.pop((tag, 'queue'), None)
        if response and response.get('status') == 'ok':
            self._show_status('Queue cleared')
        else:
            self._show_status('Failed to clear queue (server offline)')

    def _show_status_action_menu(
        self, widget: QWidget, tag: str,
    ) -> None:
        """Show action menu when clicking an 'interrupted' status cell."""
        queue_size = 0
        for s in self.sessions:
            if s['tag'] == tag:
                queue_size = s.get('queue_size', 0)
                break

        menu = QMenu(self)
        if self._prefs.get('show_tooltips', True):
            menu.setToolTipsVisible(True)

        force_action = menu.addAction('Force-send next queued message')
        force_action.setEnabled(queue_size > 0)
        force_action.setToolTip(
            'Send the next queued message immediately,\n'
            'even if Claude is still running')
        force_action.triggered.connect(
            lambda _checked, t=tag: self._force_send_next(t)
        )

        menu.exec_(widget.mapToGlobal(widget.rect().center()))

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

    def _send_immediate_message(self, tag: str, at_end: bool = True) -> None:
        """Open a dialog to type and queue a message for the session."""
        from claudeq.monitor.cq_sender import prepend_to_cq_queue, send_to_cq_session_raw

        label = 'Message to queue at end:' if at_end else 'Message to queue next:'
        text, ok = QInputDialog.getMultiLineText(
            self, 'Send Message', f'{label} ({tag})', '')
        if ok and text.strip():
            if at_end:
                ok = send_to_cq_session_raw(tag, text.strip())
            else:
                ok = prepend_to_cq_queue(tag, [text.strip()])
            if ok:
                pos = 'end' if at_end else 'next'
                self._show_status(f'Message queued ({pos}) for {tag}')
                self._refresh_data()
            else:
                self._show_status(f'Failed to queue message for {tag}')

    def _quick_send_next(self, tag: str) -> None:
        """Prepend all message bundle messages to the front of the queue.

        Messages are inserted before any existing queued messages so they
        are processed next.
        """
        from claudeq.monitor.cq_sender import prepend_to_cq_queue

        messages = load_cq_direct_template()
        if not messages:
            self._show_status('No message bundle selected')
            return
        if prepend_to_cq_queue(tag, messages):
            self._show_status(f'Bundle queued next for {tag}')
        else:
            self._show_status(f'Bundle send failed for {tag}')

    def _quick_send_at_end(self, tag: str) -> None:
        """Append all message bundle messages to the end of the queue."""
        from claudeq.monitor.cq_sender import send_to_cq_session_raw

        messages = load_cq_direct_template()
        if not messages:
            self._show_status('No message bundle selected')
            return
        ok = all(send_to_cq_session_raw(tag, m) for m in messages)
        if ok:
            self._show_status(f'Bundle queued for {tag}')
        else:
            self._show_status(f'Bundle send failed for {tag}')

    def _open_template_editor(self) -> None:
        """Open the preset editor dialog."""
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
        """Handle MR thread context combo selection change.

        Rejects multi-message presets with a popup and reverts to the
        previous selection, since MR thread context must be single-message.
        """
        text = self.template_combo.currentText()
        idx = self.template_combo.currentIndex()
        if text != '(None)':
            full_name = self.template_combo.itemData(idx, Qt.UserRole)
            name = full_name if full_name else text
            messages = load_saved_templates().get(name, [])
            if len(messages) > 1:
                QMessageBox.warning(
                    self, 'Multi-Message Preset',
                    f"'{name}' has {len(messages)} messages.\n\n"
                    'MR thread context must be a single-message preset. '
                    'Use the Message bundle combo for multi-message presets.',
                )
                # Revert to previous selection
                self.template_combo.blockSignals(True)
                prev = load_selected_template_name()
                if prev:
                    prev_idx = self.template_combo.findText(prev)
                    if prev_idx < 0 and len(prev) > MAX_COMBO_DISPLAY:
                        prev_idx = self.template_combo.findText(
                            prev[:MAX_COMBO_DISPLAY] + '\u2026')
                    self.template_combo.setCurrentIndex(
                        prev_idx if prev_idx >= 0 else 0)
                else:
                    self.template_combo.setCurrentIndex(0)
                self.template_combo.blockSignals(False)
                return
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

    def _on_cell_clicked(self, row: int, col: int) -> None:
        """Handle cell click — dismiss fire indicator on Status column."""
        if col != self.COL_STATUS:
            return
        if row < 0 or row >= len(self.sessions):
            return
        tag = self.sessions[row]['tag']
        if tag in self._state_changed_at and tag not in self._dismissed_new_status:
            self._dismissed_new_status.add(tag)
            self._update_table()

    def _apply_header_tooltips(self) -> None:
        """Set or clear column header tooltips based on show_tooltips preference."""
        enabled = self._prefs.get('show_tooltips', True)
        right_click_hint = 'Right-click header to show/hide columns'
        for col, desc in self._col_tooltip_descriptions.items():
            item = self.table.horizontalHeaderItem(col)
            if not item:
                continue
            if enabled:
                all_lines = desc.split('\n') + [right_click_hint]
                max_len = max(len(line) for line in all_lines)
                separator = '\u2500' * max_len
                item.setToolTip(f'{desc}\n{separator}\n{right_click_hint}')
            else:
                item.setToolTip('')

    def _apply_tooltips_setting(self) -> None:
        """Sync the tooltip app with the current preference."""
        if hasattr(self, '_tooltip_app'):
            self._tooltip_app.tooltips_enabled = self._prefs.get('show_tooltips', True)
        self._apply_header_tooltips()

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
