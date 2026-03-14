"""
Leap Monitor GUI application.

PyQt5-based GUI for viewing and managing active Leap sessions.
"""

import logging
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Optional

from PyQt5.QtWidgets import (
    QAction, QApplication, QComboBox, QFrame, QGridLayout, QMainWindow, QMenu,
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QStackedLayout, QTableWidget,
    QTableWidgetItem, QPushButton, QCheckBox, QHeaderView, QMessageBox,
    QProgressBar,
)
from PyQt5.QtCore import QEvent, QMimeData, QPoint, QProcess, QRect, QTimer, Qt
from PyQt5.QtGui import (
    QColor, QCursor, QDrag, QIcon, QCloseEvent, QPalette, QPixmap, QResizeEvent,
)

from leap.monitor.pr_tracking.base import PRStatus, SCMProvider
from leap.monitor.pr_tracking.config import (
    load_monitor_prefs, load_notification_seen,
    load_pinned_sessions, save_monitor_prefs,
)
from leap.monitor.themes import THEMES, current_theme, set_theme
from leap.monitor.scm_polling import (
    CollectThreadsWorker, SCMOneShotWorker, SCMPollerWorker,
    SendThreadsCombinedWorker, SendThreadsWorker, SessionRefreshWorker,
)
from leap.monitor.session_manager import get_active_sessions
from leap.monitor.monitor_utils import find_icon, load_shell_env
from leap.monitor.server_launcher import ServerLauncher
from leap.monitor.ui.dock_badge import DockBadge
from leap.monitor.ui.log_history import LogHistory, LogHistoryDialog
from leap.monitor.ui.table_helpers import (
    PR_PRESET_LABEL, PR_PRESET_TOOLTIP,
    QUICK_MSG_PRESET_LABEL, QUICK_MSG_PRESET_TOOLTIP,
    PersistentTooltipStyle, SeparatorDelegate, SeparatorHeaderView, TooltipApp,
)
from leap.monitor.ui.ui_widgets import PulsingLabel, IndicatorLabel

from leap.slack.config import is_slack_installed
from leap.monitor._mixins.actions_menu_mixin import ActionsMenuMixin
from leap.monitor._mixins.scm_config_mixin import SCMConfigMixin
from leap.monitor._mixins.session_mixin import SessionMixin
from leap.monitor._mixins.pr_tracking_mixin import PRTrackingMixin
from leap.monitor._mixins.pr_display_mixin import PRDisplayMixin
from leap.monitor._mixins.notifications_mixin import NotificationsMixin
from leap.monitor._mixins.table_builder_mixin import TableBuilderMixin

logger = logging.getLogger(__name__)


class MonitorWindow(
    ActionsMenuMixin,
    SCMConfigMixin,
    SessionMixin,
    PRTrackingMixin,
    PRDisplayMixin,
    NotificationsMixin,
    TableBuilderMixin,
    QMainWindow,
):
    """Main window for Leap Monitor."""

    # Column indices
    COL_DELETE = 0
    COL_TAG = 1
    COL_CLI = 2
    COL_PROJECT = 3
    COL_SERVER = 4
    COL_PATH = 5
    COL_SERVER_BRANCH = 6
    COL_STATUS = 7
    COL_QUEUE = 8
    COL_CLIENT = 9
    COL_SLACK = 10
    COL_PR = 11
    COL_PR_BRANCH = 12

    _HEADER_LABELS = [
        '', 'Tag', 'CLI', 'Project', 'Server', 'Path', 'Server Branch',
        'Status', 'Queue', 'Client', 'Slack', 'PR', 'PR Branch',
    ]
    _NON_TOGGLEABLE_COLS = frozenset({0, 1})  # Delete and Tag always visible

    def __init__(self) -> None:
        """Initialize the monitor window."""
        super().__init__()
        self.sessions: list[dict] = []
        self._pr_statuses: dict[str, PRStatus] = {}
        self._pr_widgets: dict[str, PulsingLabel] = {}
        self._pr_approval_widgets: dict[str, IndicatorLabel] = {}
        self._cell_cache: dict[tuple[str, str], tuple[tuple, QWidget]] = {}
        self._scm_providers: dict[str, SCMProvider] = {}  # SCMType.value -> provider
        self._scm_worker: Optional[SCMPollerWorker] = None
        self._scm_oneshot_worker: Optional[SCMOneShotWorker] = None
        self._collect_threads_worker: Optional[CollectThreadsWorker] = None
        self._send_threads_worker: Optional[SendThreadsWorker] = None
        self._send_combined_worker: Optional[SendThreadsCombinedWorker] = None
        self._leap_only_collect: bool = False
        self._combined_send: bool = False
        self._refresh_worker: Optional[SessionRefreshWorker] = None
        self._scm_polling = False
        self._scm_poll_started_at: float = 0.0
        self._shutting_down = False
        self._dock_badge = DockBadge()
        self._tracked_tags: set[str] = set()
        self._checking_tags: set[str] = set()
        self._prefs = load_monitor_prefs()
        if 'default_diff_tool' not in self._prefs:
            from leap.monitor.dialogs.settings_dialog import detect_default_difftool
            self._prefs['default_diff_tool'] = detect_default_difftool()
            self._save_prefs()
        self._pinned_sessions: dict[str, dict[str, Any]] = load_pinned_sessions()
        self._deleted_tags: set[str] = set()  # suppress re-pin after explicit delete
        self._starting_tags: set[str] = set()  # guard against double-click server start
        self._ui_ready = False  # suppress resizeEvent during init
        self._state_changed_at: dict[str, tuple[str, float]] = {}  # tag -> (state, timestamp)
        self._dismissed_new_status: set[str] = set()  # tags where user dismissed fire icon
        self._pr_changed_at: dict[str, tuple[tuple, float]] = {}  # tag -> (snapshot, timestamp)
        self._dismissed_pr_new_status: set[str] = set()  # tags where user dismissed PR fire
        self._row_colors: dict[str, str] = self._prefs.get('row_colors', {})
        self._hovered_row: int = -1
        self._pending_tracking_context: dict[str, dict[str, Any]] = {}
        self._silent_tracking_tags: set[str] = set()  # suppress popups for auto-reconnect
        self._log_history = LogHistory()
        self._server_launcher = ServerLauncher(self)
        self._slack_bot_process: Optional[QProcess] = None
        self._slack_bot_was_running: bool = self._is_slack_bot_running()
        self._global_event_monitor: Optional[object] = None
        self._local_event_monitor: Optional[object] = None

        # Row drag-and-drop state
        self._drag_source_row: int = -1
        self._drag_start_pos: QPoint = QPoint()
        self._drop_indicator: Optional[QWidget] = None

        # User notification tracking state
        raw_seen = load_notification_seen()
        self._notification_seen: dict[str, set[str]] = {
            k: set(v) for k, v in raw_seen.items()
        }
        # Track which SCM types have been seeded (first-run per type)
        self._notification_seeded: set[str] = set(raw_seen.keys())

        # Setup auto-refresh timer before init_ui
        self.timer = QTimer()
        self.timer.timeout.connect(self._auto_refresh)

        # SCM poll timer (separate from session refresh)
        self._scm_poll_timer = QTimer()
        self._scm_poll_timer.timeout.connect(self._start_scm_poll)

        self._init_ui()
        # Synchronous initial load — UI needs sessions before first paint
        self.sessions = self._merge_sessions(get_active_sessions())
        self._update_table()
        self._init_scm_providers()
        self._auto_track_pr_pinned()
        self._maybe_start_notification_poll()

        # Auto-start Slack bot if it was enabled and isn't already running
        if self._prefs.get('slack_bot_enabled') and not self._is_slack_bot_running():
            self._start_slack_bot(silent=True)

        # Always start auto-refresh
        self.timer.start(1000)

        # Register global focus shortcut (if configured)
        self._register_global_shortcut()

    def _init_ui(self) -> None:
        """Initialize the user interface."""
        self.setWindowTitle('Leap Monitor')

        # Restore saved window geometry or center on screen
        saved_geom = self._prefs.get('window_geometry')
        if saved_geom and len(saved_geom) == 4:
            # Validate the saved position is on a visible screen
            center = QPoint(saved_geom[0] + saved_geom[2] // 2,
                            saved_geom[1] + saved_geom[3] // 2)
            screen = QApplication.screenAt(center)
            if screen:
                self.setGeometry(*saved_geom)
            else:
                self._center_on_screen()
        else:
            self._center_on_screen()

        # Set app icon
        self._set_window_icon()

        # Main widget and layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout()
        margins = layout.contentsMargins()
        layout.setContentsMargins(margins.left(), 0, margins.right(), margins.bottom())
        main_widget.setLayout(layout)

        # Table
        self.table = QTableWidget()
        self.table.setHorizontalHeader(SeparatorHeaderView(Qt.Horizontal, self.table))
        self.table.setItemDelegate(SeparatorDelegate(self.table))
        self.table.setShowGrid(False)
        self.table.setColumnCount(13)
        self.table.setHorizontalHeaderLabels(self._HEADER_LABELS)

        # Column header tooltip descriptions (applied via _apply_header_tooltips)
        self._col_tooltip_descriptions = {
            self.COL_TAG: 'Leap session name',
            self.COL_CLI: 'AI CLI backend',
            self.COL_PROJECT: 'Git project name',
            self.COL_SERVER: 'Leap server process (green = running)',
            self.COL_PATH: 'Directory where the server is running',
            self.COL_SERVER_BRANCH: 'The git branch the server is running on',
            self.COL_STATUS: (
                'CLI session state:\n'
                '\n'
                '\u25cb Idle \u2014 waiting for input\n'
                '\u25cf Running \u2014 actively processing\n'
                '\u25b2 Permission \u2014 needs your approval\n'
                '\u25c6 Question \u2014 asking a clarifying question\n'
                '\u25c7 Interrupted \u2014 stopped, needs manual resume'
            ),
            self.COL_QUEUE: 'Number of messages waiting in the queue',
            self.COL_CLIENT: 'Leap client process (green = connected)',
            self.COL_SLACK: 'Slack integration (output to DM thread)',
            self.COL_PR: 'Pull request tracking status',
            self.COL_PR_BRANCH: 'PR source branch',
        }
        self._apply_header_tooltips()

        # Enable interactive column resizing
        header = self.table.horizontalHeader()
        header.setStyleSheet('QHeaderView::section { border: none; padding: 4px; }')
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)

        # Hide vertical header (row indices) — delete button is in COL_DELETE
        self.table.verticalHeader().setVisible(False)

        # Delete column: narrow fixed width
        self.table.setColumnWidth(self.COL_DELETE, 30)
        header.setSectionResizeMode(self.COL_DELETE, QHeaderView.Fixed)

        # Hide Slack column when Slack app is not installed
        self._slack_available = is_slack_installed()
        if not self._slack_available:
            self.table.setColumnHidden(self.COL_SLACK, True)

        # Right-click column header → show/hide columns menu
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(
            self._show_column_visibility_menu)

        # Restore user-hidden columns from prefs
        for label in self._prefs.get('hidden_columns', []):
            if label in self._HEADER_LABELS:
                col = self._HEADER_LABELS.index(label)
                if col not in self._NON_TOGGLEABLE_COLS:
                    self.table.setColumnHidden(col, True)

        # Last column: keep Interactive (same as all others) so that
        # resizeEvent scales every column proportionally on window resize.

        # Restore saved column widths or distribute equally
        # Reset saved widths when column layout changes
        col_count = self.table.columnCount()
        saved_widths = self._prefs.get('column_widths')
        if saved_widths and len(saved_widths) == col_count:
            for col, width in enumerate(saved_widths):
                if col == self.COL_DELETE:
                    continue  # keep fixed
                self.table.setColumnWidth(col, width)
        else:
            self._apply_equal_column_widths()

        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.cellClicked.connect(self._on_cell_clicked)

        # App-level event filter for double-click-to-copy and row drag-and-drop
        # (both need to intercept events on cell widgets).
        QApplication.instance().installEventFilter(self)
        self.table.setAcceptDrops(True)

        # Drop indicator line (positioned during drag, hidden otherwise)
        self._drop_indicator = QWidget(self.table.viewport())
        self._drop_indicator.setFixedHeight(2)
        self._drop_indicator.setStyleSheet(
            f'background-color: {current_theme().accent_blue};')
        self._drop_indicator.setVisible(False)
        self._drop_indicator.setAttribute(Qt.WA_TransparentForMouseEvents)

        # Row hover highlight — poll cursor position to track hovered row
        self.table.setProperty('_hovered_row', -1)
        # Row color state for SeparatorDelegate and cell contrast
        self.table.setProperty('_row_colors', self._row_colors)
        self.table.setProperty('_row_tags', [])
        self._hover_timer = QTimer()
        self._hover_timer.timeout.connect(self._check_row_hover)
        self._hover_timer.start(50)

        # Logo row: buttons on sides, logo absolutely centered on window
        logo_container = QFrame()
        logo_container.setFrameShape(QFrame.NoFrame)
        logo_container.setContentsMargins(0, 0, 0, 0)
        logo_container.setFixedHeight(50)
        stacked = QStackedLayout(logo_container)
        stacked.setContentsMargins(0, 0, 0, 0)
        stacked.setStackingMode(QStackedLayout.StackAll)

        # Layer 1 (bottom): logo centered in the full width — pass-through clicks
        logo_center_widget = QWidget()
        logo_center_widget.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        logo_center_layout = QHBoxLayout(logo_center_widget)
        logo_center_layout.setContentsMargins(0, 0, 0, 0)
        logo_center_layout.addStretch()
        # Logo banner — check source tree first, then .app bundle Resources/
        logo_path = Path(__file__).parent.parent.parent.parent / "assets" / "leap-text.png"
        if not logo_path.exists():
            for parent in Path(__file__).parents:
                if parent.name == 'Resources' and parent.parent.name == 'Contents':
                    logo_path = parent / "leap-text.png"
                    break
        if logo_path.exists():
            logo_pixmap = QPixmap(str(logo_path)).scaledToHeight(
                40, Qt.SmoothTransformation)
            logo_label = QLabel()
            logo_label.setPixmap(logo_pixmap)
            logo_center_layout.addWidget(logo_label)
        logo_center_layout.addStretch()
        stacked.addWidget(logo_center_widget)

        # Layer 2 (top): buttons on left and right edges — receives clicks
        buttons_widget = QWidget()
        buttons_layout = QHBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)

        settings_btn = QPushButton('\u2699  Settings')
        settings_btn.setToolTip('Monitor settings')
        settings_btn.clicked.connect(self._open_settings)
        buttons_layout.addWidget(settings_btn)

        buttons_layout.addStretch()

        reset_cols_btn = QPushButton('Reset Window Sizes')
        reset_cols_btn.setToolTip('Reset all window and column sizes to defaults')
        reset_cols_btn.clicked.connect(self._reset_window_size)
        buttons_layout.addWidget(reset_cols_btn)
        stacked.addWidget(buttons_widget)

        layout.addWidget(logo_container)

        # Top controls (presets) — centered
        top_layout = QHBoxLayout()
        top_layout.addStretch()

        edit_preset_btn = QPushButton('\u270e  Presets')
        edit_preset_btn.setToolTip('Edit presets')
        edit_preset_btn.clicked.connect(self._open_preset_editor)
        top_layout.addWidget(edit_preset_btn)

        preset_grid = QGridLayout()
        preset_grid.setSpacing(4)

        pr_label = QLabel(PR_PRESET_LABEL)
        preset_grid.addWidget(pr_label, 0, 0)

        self.preset_combo = QComboBox()
        self.preset_combo.setObjectName('preset_combo')
        self.preset_combo.setMinimumWidth(180)
        self.preset_combo.setMaximumWidth(300)
        self.preset_combo.setToolTip(PR_PRESET_TOOLTIP)
        self._populate_preset_combo()
        self.preset_combo.currentIndexChanged.connect(
            self._on_preset_combo_changed)
        preset_grid.addWidget(self.preset_combo, 0, 1)

        direct_label = QLabel(QUICK_MSG_PRESET_LABEL)
        preset_grid.addWidget(direct_label, 1, 0)

        self.direct_preset_combo = QComboBox()
        self.direct_preset_combo.setObjectName('direct_preset_combo')
        self.direct_preset_combo.setMinimumWidth(180)
        self.direct_preset_combo.setMaximumWidth(300)
        self.direct_preset_combo.setToolTip(QUICK_MSG_PRESET_TOOLTIP)
        self._populate_direct_preset_combo()
        self.direct_preset_combo.currentIndexChanged.connect(
            self._on_direct_preset_combo_changed)
        preset_grid.addWidget(self.direct_preset_combo, 1, 1)

        top_layout.addLayout(preset_grid)
        top_layout.addStretch()
        layout.addLayout(top_layout)

        add_row_layout = QHBoxLayout()
        add_btn = QPushButton('+')
        add_btn.setFixedWidth(30)
        add_btn.setToolTip('Add session from Git URL or local path')
        add_btn.clicked.connect(self._add_row_menu)
        add_row_layout.addWidget(add_btn)
        add_row_layout.addStretch()
        layout.addLayout(add_row_layout)

        layout.addWidget(self.table)

        # Bottom controls
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(8, 0, 0, 0)

        self.bots_check = QCheckBox('Include git bots')
        self.bots_check.setToolTip('Count bot comments as responses in PR thread detection')
        self.bots_check.setChecked(self._prefs.get('include_bots', False))
        self.bots_check.stateChanged.connect(self._toggle_include_bots)
        bottom_layout.addWidget(self.bots_check)

        self.auto_leap_check = QCheckBox("Auto '/leap' fetch")
        self.auto_leap_check.setToolTip(
            'Automatically send /leap-tagged PR threads to Leap sessions each poll cycle'
        )
        self.auto_leap_check.setChecked(self._prefs.get('auto_fetch_leap', True))
        self.auto_leap_check.stateChanged.connect(self._toggle_auto_fetch_leap)
        bottom_layout.addWidget(self.auto_leap_check)

        bottom_layout.addStretch()

        # SCM connect buttons
        self.gitlab_btn = QPushButton('Connect GitLab')
        self.gitlab_btn.setToolTip('Configure GitLab connection for PR tracking')
        self.gitlab_btn.clicked.connect(self._open_gitlab_setup)
        bottom_layout.addWidget(self.gitlab_btn)

        self.github_btn = QPushButton('Connect GitHub')
        self.github_btn.setToolTip('Configure GitHub connection for PR tracking')
        self.github_btn.clicked.connect(self._open_github_setup)
        bottom_layout.addWidget(self.github_btn)

        self.slack_bot_btn = QPushButton('Slack Bot')
        self.slack_bot_btn.setToolTip('Start/stop the Slack bot daemon')
        self.slack_bot_btn.clicked.connect(self._toggle_slack_bot)
        self.slack_bot_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        self.slack_bot_btn.customContextMenuRequested.connect(
            self._slack_bot_context_menu)
        self.slack_bot_btn.setVisible(self._slack_available)
        bottom_layout.addWidget(self.slack_bot_btn)

        layout.addLayout(bottom_layout)

        # Status / log bar at the very bottom
        status_layout = QHBoxLayout()

        full_log_btn = QPushButton('Logs')
        full_log_btn.setToolTip('View full status message history')
        full_log_btn.clicked.connect(self._open_log_history)
        status_layout.addWidget(full_log_btn)

        self._log_label = QLabel('')
        self._log_label.setStyleSheet(
            f'color: {current_theme().text_secondary}; font-size: 11px;'
        )
        self._log_label.setOpenExternalLinks(True)
        status_layout.addWidget(self._log_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)  # indeterminate
        self._progress_bar.setFixedHeight(12)
        self._progress_bar.setMaximumWidth(120)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setVisible(False)
        self._busy_count: int = 0
        status_layout.addWidget(self._progress_bar)

        status_layout.addStretch()

        close_btn = QPushButton('Close')
        close_btn.setToolTip('Close the monitor')
        close_btn.clicked.connect(self._confirm_close)
        status_layout.addWidget(close_btn)

        layout.addLayout(status_layout)

    # ------------------------------------------------------------------
    #  Core utilities
    # ------------------------------------------------------------------

    def _open_log_history(self) -> None:
        """Open the log history dialog."""
        dialog = LogHistoryDialog(self._log_history, self)
        dialog.exec_()

    def _show_status(self, msg: str, timeout_ms: int = 5000,
                     url: Optional[str] = None) -> None:
        """Log a status message and update the inline log labels."""
        self._log_history.append(msg, url=url)
        self._refresh_log_labels()

    def _refresh_log_labels(self) -> None:
        """Update the inline log label with the most recent entry."""
        entries = self._log_history.entries()
        if entries:
            e = entries[-1]
            ts = time.strftime('%H:%M:%S', time.localtime(e.timestamp))
            if e.url:
                display_msg = e.message.replace(
                    '[Notification]',
                    '<span style="color: cyan;">[Notification]</span>',
                    1,
                ) if '[Notification]' in e.message else e.message
                self._log_label.setText(
                    f'[{ts}] {display_msg} '
                    f'<a href="{e.url}" style="color: #5B9BD5;">(link)</a>'
                )
            else:
                self._log_label.setText(f'[{ts}] {e.message}')
        else:
            self._log_label.setText('')

    def _set_busy(self, busy: bool) -> None:
        """Show or hide the indeterminate progress bar (ref-counted)."""
        if busy:
            self._busy_count += 1
        else:
            self._busy_count = max(0, self._busy_count - 1)
        self._progress_bar.setVisible(self._busy_count > 0)

    # ------------------------------------------------------------------
    #  Row reordering (drag-and-drop)
    # ------------------------------------------------------------------

    def _perform_row_drag(self, source_row: int) -> None:
        """Initiate a QDrag for row reordering."""
        if source_row < 0 or source_row >= len(self.sessions):
            return

        tag = self.sessions[source_row]['tag']

        drag = QDrag(self.table)
        mime = QMimeData()
        mime.setData('application/x-leap-row', str(source_row).encode())
        drag.setMimeData(mime)

        # Capture a snapshot of the row as the drag pixmap
        row_y = self.table.rowViewportPosition(source_row)
        row_h = self.table.rowHeight(source_row)
        viewport_w = self.table.viewport().width()
        pixmap = self.table.viewport().grab(
            QRect(0, row_y, viewport_w, row_h))
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))

        # Pause auto-refresh during drag
        self.timer.stop()
        logger.debug("Row drag started: row=%d tag=%s", source_row, tag)
        drag.exec_(Qt.MoveAction)
        self.timer.start(1000)
        self._hide_drop_indicator()

    def _update_drop_indicator(self, pos: QPoint) -> None:
        """Position the drop indicator line at the nearest row boundary."""
        if not self._drop_indicator:
            return
        target_row = self.table.rowAt(pos.y())
        if target_row < 0:
            last_row = self.table.rowCount() - 1
            if last_row < 0:
                self._drop_indicator.setVisible(False)
                return
            y = (self.table.rowViewportPosition(last_row)
                 + self.table.rowHeight(last_row))
        else:
            row_y = self.table.rowViewportPosition(target_row)
            row_h = self.table.rowHeight(target_row)
            if pos.y() > row_y + row_h // 2:
                y = row_y + row_h
            else:
                y = row_y
        viewport_w = self.table.viewport().width()
        self._drop_indicator.setGeometry(0, y - 1, viewport_w, 2)
        self._drop_indicator.setVisible(True)
        self._drop_indicator.raise_()

    def _hide_drop_indicator(self) -> None:
        """Hide the row drop indicator line."""
        if self._drop_indicator:
            self._drop_indicator.setVisible(False)

    def _drop_target_row(self, pos: QPoint) -> tuple[int, bool]:
        """Compute the target row and whether the drop is below it."""
        target_row = self.table.rowAt(pos.y())
        if target_row < 0:
            return len(self.sessions) - 1, True
        row_y = self.table.rowViewportPosition(target_row)
        row_h = self.table.rowHeight(target_row)
        drop_below = pos.y() > row_y + row_h // 2
        return target_row, drop_below

    def _on_row_moved(self, source_row: int, target_row: int,
                      drop_below: bool) -> None:
        """Handle row reorder from drag-and-drop."""
        if source_row < 0 or target_row < 0:
            return
        if source_row >= len(self.sessions) or target_row >= len(self.sessions):
            return

        # Compute insertion index, adjusting for the pop shift
        insert_at = target_row + (1 if drop_below else 0)
        if source_row < insert_at:
            insert_at -= 1

        if source_row == insert_at:
            return

        session = self.sessions.pop(source_row)
        self.sessions.insert(insert_at, session)

        self._prefs['row_order'] = [s['tag'] for s in self.sessions]
        self._save_prefs()
        self._update_table()

    # ------------------------------------------------------------------
    #  Window geometry
    # ------------------------------------------------------------------

    def _set_window_icon(self) -> None:
        """Set the window icon."""
        icon_path = find_icon()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))

    def _center_on_screen(self) -> None:
        """Resize to default dimensions and center on screen."""
        self.resize(1150, 600)
        screen = QApplication.primaryScreen().availableGeometry()
        x = (screen.width() - 1150) // 2 + screen.x()
        y = (screen.height() - 600) // 2 + screen.y()
        self.move(x, y)

    def _apply_equal_column_widths(self) -> None:
        """Distribute column widths equally across visible resizable columns."""
        col_count = self.table.columnCount()
        if col_count <= 0:
            return
        viewport_w = self.table.viewport().width()
        if viewport_w <= 0:
            # Viewport not ready yet — estimate from window geometry
            viewport_w = (self.geometry().width() or 1150) - 50
        delete_w = self.table.columnWidth(self.COL_DELETE)
        available = viewport_w - delete_w
        # Only count visible, non-fixed columns (skip hidden like Slack)
        visible_cols = [
            col for col in range(col_count)
            if col != self.COL_DELETE and not self.table.isColumnHidden(col)
        ]
        col_width = available // max(len(visible_cols), 1)
        for col in visible_cols:
            self.table.setColumnWidth(col, col_width)

    def _reset_window_size(self) -> None:
        """Reset window geometry, column widths, and dialog sizes.

        Column visibility (hidden columns) is preserved — only sizes
        and positions are reset.
        """
        self._prefs.pop('dialog_geometry', None)
        save_monitor_prefs(self._prefs)

        self._center_on_screen()
        self._apply_equal_column_widths()

    # ------------------------------------------------------------------
    #  Column visibility
    # ------------------------------------------------------------------

    def _show_column_visibility_menu(self, pos: QPoint) -> None:
        """Show a context menu to toggle column visibility."""
        menu = QMenu(self)
        for col, label in enumerate(self._HEADER_LABELS):
            if col in self._NON_TOGGLEABLE_COLS:
                continue
            # Skip Slack entry when Slack is not installed
            if col == self.COL_SLACK and not self._slack_available:
                continue
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(not self.table.isColumnHidden(col))
            action.toggled.connect(
                lambda checked, c=col, lbl=label: self._toggle_column(
                    c, lbl, checked))
            menu.addAction(action)
        header = self.table.horizontalHeader()
        menu.exec_(header.mapToGlobal(pos))

    def _toggle_column(self, col: int, label: str, visible: bool) -> None:
        """Toggle a column's visibility and persist the choice."""
        self.table.setColumnHidden(col, not visible)

        hidden: list[str] = self._prefs.get('hidden_columns', [])
        if visible:
            hidden = [h for h in hidden if h != label]
        else:
            if label not in hidden:
                hidden.append(label)
        self._prefs['hidden_columns'] = hidden
        self._save_prefs()

        self._apply_equal_column_widths()

    # ------------------------------------------------------------------
    #  App-level event filter (double-click-to-copy + row drag-and-drop)
    # ------------------------------------------------------------------

    def _is_in_table(self, widget: object) -> bool:
        """Check if a widget is inside the session table."""
        while widget is not None:
            if widget is self.table:
                return True
            widget = widget.parent() if hasattr(widget, 'parent') else None
        return False

    def eventFilter(self, obj: object, event: QEvent) -> bool:
        """Intercept events on table cell widgets for copy and drag."""
        etype = event.type()

        # ── Row drag-and-drop ────────────────────────────────────────
        if etype == QEvent.MouseButtonPress:
            if event.button() == Qt.LeftButton and self._is_in_table(obj):
                pos = self.table.viewport().mapFromGlobal(event.globalPos())
                row = self.table.rowAt(pos.y())
                col = self.table.columnAt(pos.x())
                if row >= 0 and col != self.COL_DELETE:
                    self._drag_source_row = row
                    self._drag_start_pos = event.globalPos()
                else:
                    self._drag_source_row = -1

        elif etype == QEvent.MouseMove:
            if (self._drag_source_row >= 0
                    and event.buttons() & Qt.LeftButton
                    and self._is_in_table(obj)):
                dist = (event.globalPos() - self._drag_start_pos).manhattanLength()
                if dist >= QApplication.startDragDistance():
                    self._perform_row_drag(self._drag_source_row)
                    self._drag_source_row = -1
                    return True

        elif etype == QEvent.MouseButtonRelease:
            self._drag_source_row = -1

        elif etype == QEvent.DragEnter and obj is self.table.viewport():
            if event.mimeData().hasFormat('application/x-leap-row'):
                event.acceptProposedAction()
                return True

        elif etype == QEvent.DragMove and obj is self.table.viewport():
            if event.mimeData().hasFormat('application/x-leap-row'):
                self._update_drop_indicator(event.pos())
                event.acceptProposedAction()
                return True

        elif etype == QEvent.DragLeave and obj is self.table.viewport():
            self._hide_drop_indicator()

        elif etype == QEvent.Drop and obj is self.table.viewport():
            mime = event.mimeData()
            if mime.hasFormat('application/x-leap-row'):
                self._hide_drop_indicator()
                source_row = int(
                    bytes(mime.data('application/x-leap-row')).decode())
                target_row, drop_below = self._drop_target_row(event.pos())
                self._on_row_moved(source_row, target_row, drop_below)
                event.acceptProposedAction()
                return True

        # ── Double-click-to-copy ─────────────────────────────────────
        if etype != QEvent.MouseButtonDblClick:
            return super().eventFilter(obj, event)
        if not self._is_in_table(obj):
            return super().eventFilter(obj, event)
        pos = self.table.viewport().mapFromGlobal(event.globalPos())
        row = self.table.rowAt(pos.y())
        col = self.table.columnAt(pos.x())
        if row < 0 or col < 0 or col == self.COL_DELETE:
            return super().eventFilter(obj, event)
        if self._copy_cell_to_clipboard(row, col):
            return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    #  Window lifecycle
    # ------------------------------------------------------------------

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Scale all resizable columns proportionally on window resize."""
        super().resizeEvent(event)
        if not self._ui_ready:
            return
        viewport_w = self.table.viewport().width()
        if viewport_w <= 0:
            return
        col_count = self.table.columnCount()
        delete_w = self.table.columnWidth(self.COL_DELETE)
        # Only scale visible columns (skip hidden like Slack)
        resizable_cols = [
            c for c in range(col_count)
            if c != self.COL_DELETE and not self.table.isColumnHidden(c)
        ]
        resizable_total = sum(self.table.columnWidth(c) for c in resizable_cols)
        if resizable_total <= 0:
            return
        available = viewport_w - delete_w
        # Cumulative rounding: compute each column's target from cumulative
        # proportions so every column shifts evenly, even for tiny changes.
        cumulative_old = 0
        used = 0
        for col in resizable_cols:
            cumulative_old += self.table.columnWidth(col)
            target = round(available * cumulative_old / resizable_total)
            w = max(30, target - used)
            self.table.setColumnWidth(col, w)
            used += w

    def changeEvent(self, event: QEvent) -> None:
        """Reset dock badge when window becomes active."""
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            self._clear_dock_badge()

    def _auto_refresh(self) -> None:
        """Auto-refresh callback."""
        if self._shutting_down:
            return
        try:
            self._refresh_data()
        except Exception:
            logger.exception("Error in auto-refresh")

    def _save_prefs(self) -> None:
        """Save self._prefs to disk, preserving dialog geometries.

        Dialog done() methods save their geometry directly to disk via
        save_dialog_geometry(), which bypasses self._prefs.  Before writing
        self._prefs, merge the latest dialog_geometry from disk so those
        saves are not overwritten.
        """
        disk_prefs = load_monitor_prefs()
        disk_geom = disk_prefs.get('dialog_geometry')
        if disk_geom:
            self._prefs['dialog_geometry'] = disk_geom
        save_monitor_prefs(self._prefs)

    def _apply_theme(self, theme_name: str) -> None:
        """Switch the active theme and rebuild the UI to reflect new colors.

        Uses QPalette for base colors so native macOS widget rendering
        (buttons, checkboxes, spinbox arrows, etc.) is preserved.  Only
        applies minimal QSS for things the palette can't control (table
        grid, tooltips, header sections).
        """
        if theme_name not in THEMES:
            return
        set_theme(theme_name)
        t = current_theme()

        # Set macOS appearance (dark/light) — must come before palette
        try:
            from AppKit import NSAppearance, NSApplication
            appearance_name = (
                'NSAppearanceNameDarkAqua' if t.is_dark
                else 'NSAppearanceNameAqua'
            )
            appearance = NSAppearance.appearanceNamed_(appearance_name)
            if appearance:
                NSApplication.sharedApplication().setAppearance_(appearance)
        except Exception:
            pass

        app = QApplication.instance()

        # Set palette — this controls native widget colors without
        # replacing the platform style engine the way QSS does.
        pal = QPalette()
        pal.setColor(QPalette.Window, QColor(t.window_bg))
        pal.setColor(QPalette.WindowText, QColor(t.text_primary))
        pal.setColor(QPalette.Base, QColor(t.input_bg))
        pal.setColor(QPalette.AlternateBase, QColor(t.cell_bg_alt))
        pal.setColor(QPalette.Text, QColor(t.text_primary))
        pal.setColor(QPalette.Button, QColor(t.window_bg))
        pal.setColor(QPalette.ButtonText, QColor(t.text_primary))
        pal.setColor(QPalette.Highlight, QColor(t.accent_blue))
        pal.setColor(QPalette.HighlightedText, QColor('#ffffff' if t.is_dark else '#000000'))
        pal.setColor(QPalette.ToolTipBase, QColor(t.popup_bg))
        pal.setColor(QPalette.ToolTipText, QColor(t.text_primary))
        pal.setColor(QPalette.PlaceholderText, QColor(t.text_muted))
        pal.setColor(QPalette.Link, QColor(t.accent_blue))
        # Disabled state
        pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(t.text_muted))
        pal.setColor(QPalette.Disabled, QPalette.Text, QColor(t.text_muted))
        pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(t.text_muted))
        app.setPalette(pal)

        # Minimal QSS — only for things QPalette can't control.
        # NO QWidget/QPushButton/QCheckBox rules so native rendering
        # is fully preserved.
        app.setStyleSheet(f"""
            QTableWidget {{
                background-color: {t.cell_bg};
                alternate-background-color: {t.cell_bg_alt};
                gridline-color: transparent;
            }}
            QHeaderView::section {{
                border: none;
                padding: 4px;
            }}
        """)

        # Clear cell cache to force full rebuild with new colors
        self._cell_cache.clear()
        self._update_table()

        # Re-apply SCM button styles
        self._update_scm_buttons()
        self._update_slack_bot_button()

        # Update status log label color
        self._log_label.setStyleSheet(
            f'color: {t.text_secondary}; font-size: 11px;'
        )

        # Update drop indicator color
        if self._drop_indicator:
            self._drop_indicator.setStyleSheet(
                f'background-color: {t.accent_blue};')

    # ------------------------------------------------------------------
    #  Global keyboard shortcut
    # ------------------------------------------------------------------

    def _register_global_shortcut(self) -> None:
        """Register (or re-register) the global focus shortcut from prefs."""
        self._unregister_global_shortcut()

        shortcut_str = self._prefs.get('global_shortcut', '')
        if not shortcut_str:
            return

        try:
            from AppKit import NSEvent, NSKeyDownMask
        except ImportError:
            logger.debug("AppKit not available — global shortcut disabled")
            return

        seq = __import__('PyQt5.QtGui', fromlist=['QKeySequence']).QKeySequence(shortcut_str)
        if seq.isEmpty():
            return

        # Decompose the QKeySequence into key + modifiers
        combined = seq[0]
        qt_mods = int(combined) & 0xFE000000  # upper bits = modifiers
        qt_key = int(combined) & 0x01FFFFFF    # lower bits = key code

        # Map Qt modifier flags → NSEvent modifier flags
        ns_flags = 0
        # Qt.ControlModifier (physical Cmd on macOS) → NSCommandKeyMask
        if qt_mods & 0x04000000:  # Qt.ControlModifier
            ns_flags |= 1 << 20   # NSEventModifierFlagCommand
        # Qt.MetaModifier (physical Ctrl on macOS) → NSControlKeyMask
        if qt_mods & 0x10000000:  # Qt.MetaModifier
            ns_flags |= 1 << 18   # NSEventModifierFlagControl
        # Qt.AltModifier (Option) → NSAlternateKeyMask
        if qt_mods & 0x08000000:  # Qt.AltModifier
            ns_flags |= 1 << 19   # NSEventModifierFlagOption
        # Qt.ShiftModifier → NSShiftKeyMask
        if qt_mods & 0x02000000:  # Qt.ShiftModifier
            ns_flags |= 1 << 17   # NSEventModifierFlagShift

        # Map character → macOS hardware virtual key code (layout-independent).
        # Using keyCode() instead of charactersIgnoringModifiers() so the
        # shortcut works regardless of the active keyboard input source
        # (Hebrew, Arabic, Russian, etc.).
        _CHAR_TO_KEYCODE: dict[str, int] = {
            'a': 0x00, 's': 0x01, 'd': 0x02, 'f': 0x03, 'h': 0x04,
            'g': 0x05, 'z': 0x06, 'x': 0x07, 'c': 0x08, 'v': 0x09,
            'b': 0x0B, 'q': 0x0C, 'w': 0x0D, 'e': 0x0E, 'r': 0x0F,
            'y': 0x10, 't': 0x11, '1': 0x12, '2': 0x13, '3': 0x14,
            '4': 0x15, '6': 0x16, '5': 0x17, '=': 0x18, '9': 0x19,
            '7': 0x1A, '-': 0x1B, '8': 0x1C, '0': 0x1D, ']': 0x1E,
            'o': 0x1F, 'u': 0x20, '[': 0x21, 'i': 0x22, 'p': 0x23,
            'l': 0x25, 'j': 0x26, "'": 0x27, 'k': 0x28, ';': 0x29,
            '\\': 0x2A, ',': 0x2B, '/': 0x2C, 'n': 0x2D, 'm': 0x2E,
            '.': 0x2F, ' ': 0x31, '`': 0x32,
        }
        char = chr(qt_key).lower() if 0x20 <= qt_key <= 0x7E else None
        if char is None or char not in _CHAR_TO_KEYCODE:
            logger.warning("Global shortcut: unsupported key code %d", qt_key)
            return
        expected_keycode = _CHAR_TO_KEYCODE[char]

        def _handler(event: object) -> object:
            """NSEvent handler — check modifiers + hardware key code."""
            try:
                ev_flags = event.modifierFlags() & 0x00FF0000  # device-independent
                if event.keyCode() == expected_keycode and ev_flags == ns_flags:
                    from PyQt5.QtCore import QTimer
                    QTimer.singleShot(0, self._on_global_shortcut_triggered)
            except Exception:
                pass
            return event

        self._global_event_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSKeyDownMask, _handler,
        )
        self._local_event_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSKeyDownMask, _handler,
        )
        logger.debug("Global shortcut registered: %s", shortcut_str)

    def _unregister_global_shortcut(self) -> None:
        """Remove any active NSEvent monitors."""
        try:
            from AppKit import NSEvent
        except ImportError:
            return
        if self._global_event_monitor is not None:
            NSEvent.removeMonitor_(self._global_event_monitor)
            self._global_event_monitor = None
        if self._local_event_monitor is not None:
            NSEvent.removeMonitor_(self._local_event_monitor)
            self._local_event_monitor = None

    def _on_global_shortcut_triggered(self) -> None:
        """Bring the monitor window to the foreground."""
        try:
            from AppKit import NSApplication
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            pass
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _confirm_close(self) -> None:
        """Ask for confirmation before closing the monitor."""
        reply = QMessageBox.question(
            self, 'Close Monitor',
            'Are you sure you want to close the monitor?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Handle window close event - save prefs then force-exit the process.

        QThread.terminate() does not work on Python threads, and the
        SCMPollerWorker's ThreadPoolExecutor can block indefinitely on
        network I/O.  Instead of trying to join threads gracefully (which
        hangs), we save state and then os._exit() to guarantee the process
        dies immediately.
        """
        # Prevent timers and signal handlers from firing during shutdown
        self._shutting_down = True
        self.timer.stop()
        self._scm_poll_timer.stop()
        self._hover_timer.stop()
        self._unregister_global_shortcut()
        self._clear_dock_badge()

        # Save window geometry and column widths
        try:
            geom = self.geometry()
            self._prefs['window_geometry'] = [geom.x(), geom.y(), geom.width(), geom.height()]
            self._prefs['column_widths'] = [
                self.table.columnWidth(col) for col in range(self.table.columnCount())
            ]
            self._save_prefs()
        except Exception:
            logger.debug("Failed to save monitor prefs on close", exc_info=True)

        # Terminate Slack bot if we started it
        if self._slack_bot_process and self._slack_bot_process.state() != QProcess.NotRunning:
            self._slack_bot_process.terminate()
            if not self._slack_bot_process.waitForFinished(500):
                self._slack_bot_process.kill()
                self._slack_bot_process.waitForFinished(2000)

        # Accept the close event, then hard-exit.  os._exit() skips atexit
        # handlers and thread joins — the only reliable way to exit when
        # background threads may be stuck in blocking network calls.
        event.accept()
        os._exit(0)


def main() -> None:
    """Main entry point for Leap Monitor."""
    import faulthandler
    faulthandler.enable()
    load_shell_env()
    app = TooltipApp(sys.argv)
    app.setApplicationName('Leap Monitor')
    app.setStyle(PersistentTooltipStyle(app.style()))

    # Load saved theme before creating the window
    prefs = load_monitor_prefs()
    saved_theme = prefs.get('theme', 'Midnight')
    set_theme(saved_theme)

    # Set macOS appearance based on theme (dark/light)
    t = current_theme()
    try:
        from AppKit import NSAppearance, NSApplication
        appearance_name = (
            'NSAppearanceNameDarkAqua' if t.is_dark
            else 'NSAppearanceNameAqua'
        )
        appearance = NSAppearance.appearanceNamed_(appearance_name)
        if appearance:
            NSApplication.sharedApplication().setAppearance_(appearance)
    except Exception:
        pass

    # Set app icon for Dock and macOS notifications
    icon_path = find_icon()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))
        try:
            from AppKit import NSApplication, NSImage
            ns_image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
            if ns_image:
                NSApplication.sharedApplication().setApplicationIconImage_(ns_image)
        except Exception:
            pass

    window = MonitorWindow()
    window._tooltip_app = app
    window._apply_tooltips_setting()
    # Apply the theme stylesheet (sets global QSS + rebuilds table)
    window._apply_theme(saved_theme)
    window.show()

    # Enable proportional column scaling after the window is fully shown
    # and all initial resize events have settled.  Re-apply equal widths
    # when no saved prefs exist — the viewport is now accurately sized.
    def _finalize_ui() -> None:
        saved_widths = window._prefs.get('column_widths')
        col_count = window.table.columnCount()
        if not saved_widths or len(saved_widths) != col_count:
            window._apply_equal_column_widths()
        window._ui_ready = True

    QTimer.singleShot(0, _finalize_ui)

    # ── Ctrl+C handling ──────────────────────────────────────────────
    # Reclaim the terminal foreground process group so SIGINT from
    # Ctrl+C is delivered to us (make/poetry may have changed it).
    try:
        _tty_fd = sys.stdin.fileno()
        _our_pgid = os.getpgrp()
        if os.tcgetpgrp(_tty_fd) != _our_pgid:
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            os.tcsetpgrp(_tty_fd, _our_pgid)
    except (OSError, AttributeError):
        pass

    def signal_handler(sig: int, frame: Any) -> None:
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Timer trick — force periodic bytecode execution so Python
    # processes pending signals while Qt's C++ event loop runs.
    timer = QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
