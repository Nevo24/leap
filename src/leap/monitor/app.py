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
    QColor, QCursor, QDrag, QIcon, QImage, QCloseEvent, QPalette, QPixmap,
    QResizeEvent,
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
from leap.monitor.monitor_utils import find_icon, notes_icon, load_shell_env
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
    COL_TASK = 5
    COL_PATH = 6
    COL_SERVER_BRANCH = 7
    COL_STATUS = 8
    COL_QUEUE = 9
    COL_CLIENT = 10
    COL_SLACK = 11
    COL_PR = 12
    COL_PR_BRANCH = 13

    _HEADER_LABELS = [
        '', 'Tag', 'CLI', 'Project', 'Server', 'Last Msg', 'Path',
        'Server Branch', 'Status', 'Queue', 'Client', 'Slack', 'PR', 'PR Branch',
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
        self._aliases: dict[str, str] = self._prefs.get('aliases', {})
        self._hovered_row: int = -1
        self._pending_tracking_context: dict[str, dict[str, Any]] = {}
        self._silent_tracking_tags: set[str] = set()  # suppress popups for auto-reconnect
        self._log_history = LogHistory()
        self._server_launcher = ServerLauncher(self)
        self._slack_bot_process: Optional[QProcess] = None
        self._slack_bot_stopping: bool = False
        self._slack_bot_was_running: bool = self._is_slack_bot_running()
        self._global_event_monitor: Optional[object] = None
        self._local_event_monitor: Optional[object] = None

        # Row drag-and-drop state
        self._drag_source_row: int = -1
        self._drag_start_pos: QPoint = QPoint()
        self._drag_press_time: float = 0.0
        self._drop_indicator: Optional[QWidget] = None

        # User notification tracking state
        raw_seen = load_notification_seen()
        self._notification_seen: dict[str, set[str]] = {
            k: set(v) for k, v in raw_seen.items()
        }
        # Track which SCM types have been seeded (first-run per type).
        # Only count as seeded if the persisted list was non-empty —
        # an empty list (e.g. after prune or all todos resolved) should
        # re-seed on the next poll to avoid firing stale notifications.
        self._notification_seeded: set[str] = {
            k for k, v in raw_seen.items() if v
        }

        # Setup auto-refresh timer before init_ui
        self.timer = QTimer()
        self.timer.timeout.connect(self._auto_refresh)

        # SCM poll timer (separate from session refresh)
        self._scm_poll_timer = QTimer()
        self._scm_poll_timer.timeout.connect(self._start_scm_poll)

        self._init_ui()
        self._apply_window_effects()
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

        # Set up modern notification API (UNUserNotificationCenter) for
        # banner action buttons and click handling
        from leap.monitor._mixins.pr_display_mixin import _setup_modern_notifications
        _setup_modern_notifications(self)

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
        layout.setContentsMargins(12, 0, 12, 8)
        layout.setSpacing(6)
        main_widget.setLayout(layout)

        # Accent stripe — animated gradient bar at the very top (brand identity)
        from leap.monitor.ui.ui_widgets import ShimmerBar
        self._accent_bar = ShimmerBar()
        self._accent_bar.setFixedHeight(3)
        layout.addWidget(self._accent_bar)

        # Table
        self.table = QTableWidget()
        self.table.setHorizontalHeader(SeparatorHeaderView(Qt.Horizontal, self.table))
        self.table.setItemDelegate(SeparatorDelegate(self.table))
        self.table.setShowGrid(False)
        self.table.setColumnCount(14)
        self.table.setHorizontalHeaderLabels(self._HEADER_LABELS)

        # Column header tooltip descriptions (applied via _apply_header_tooltips)
        self._col_tooltip_descriptions = {
            self.COL_TAG: 'Leap session name',
            self.COL_CLI: 'AI CLI engine',
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
            self.COL_TASK: 'The last message sent to the CLI',
            self.COL_QUEUE: 'Number of messages waiting in the queue',
            self.COL_CLIENT: 'Leap client process (green = connected)',
            self.COL_SLACK: 'Slack integration (output to DM thread)',
            self.COL_PR: 'Pull request tracking status',
            self.COL_PR_BRANCH: 'PR source branch',
        }
        self._apply_header_tooltips()

        # Enable interactive column resizing (columns never exceed viewport)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        header = self.table.horizontalHeader()
        header.setStyleSheet('QHeaderView::section { border: none; padding: 6px 4px; }')
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)
        self._resizing_columns = False  # guard against re-entrant sectionResized
        header.sectionResized.connect(self._on_section_resized)

        # Hide vertical header (row indices) — delete button is in COL_DELETE
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)  # taller rows for pill badges

        # Delete column: narrow fixed width
        self.table.setColumnWidth(self.COL_DELETE, 34)
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
        logo_container.setObjectName('_leapLogoBar')
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
        # Logo banner — themed variant per theme
        self._logo_label = QLabel()
        self._update_logo_pixmap()
        logo_center_layout.addWidget(self._logo_label)
        logo_center_layout.addStretch()
        stacked.addWidget(logo_center_widget)

        # Layer 2 (top): buttons on left and right edges — receives clicks
        buttons_widget = QWidget()
        buttons_layout = QHBoxLayout(buttons_widget)
        buttons_layout.setContentsMargins(0, 0, 0, 0)

        settings_btn = QPushButton('\u2699  Settings')
        settings_btn.setObjectName('_leapGhostBtn')
        settings_btn.setToolTip('Monitor settings')
        settings_btn.clicked.connect(self._open_settings)
        buttons_layout.addWidget(settings_btn)

        self._notes_btn = QPushButton(' Notes')
        self._notes_btn.setObjectName('_leapGhostBtn')
        self._notes_btn.setToolTip('Open personal notes')
        _notes_icon = notes_icon(size=16)
        if _notes_icon:
            self._notes_btn.setIcon(_notes_icon)
        self._notes_btn.clicked.connect(self._open_notes)
        buttons_layout.addWidget(self._notes_btn)

        buttons_layout.addStretch()

        reset_cols_btn = QPushButton('Reset Window Sizes')
        reset_cols_btn.setObjectName('_leapGhostBtn')
        reset_cols_btn.setToolTip('Reset all window and column sizes to defaults')
        reset_cols_btn.clicked.connect(self._reset_window_size)
        buttons_layout.addWidget(reset_cols_btn)
        stacked.addWidget(buttons_widget)

        layout.addWidget(logo_container)

        # ═══════════════════════════════════════════════════════════════
        #  PRESETS PANEL — inside a subtle card
        # ═══════════════════════════════════════════════════════════════
        preset_card = QFrame()
        preset_card.setObjectName('_leapCard')
        preset_card_layout = QHBoxLayout(preset_card)
        preset_card_layout.setContentsMargins(16, 10, 16, 10)
        preset_card_layout.addStretch()

        edit_preset_btn = QPushButton('\u270e  Presets')
        edit_preset_btn.setToolTip('Edit presets')
        edit_preset_btn.clicked.connect(self._open_preset_editor)
        preset_card_layout.addWidget(edit_preset_btn)

        preset_grid = QGridLayout()
        preset_grid.setHorizontalSpacing(10)
        preset_grid.setVerticalSpacing(6)

        pr_label = QLabel(PR_PRESET_LABEL)
        pr_label.setObjectName('_leapDimLabel')
        preset_grid.addWidget(pr_label, 0, 0, Qt.AlignRight | Qt.AlignVCenter)

        self.preset_combo = QComboBox()
        self.preset_combo.setObjectName('preset_combo')
        self.preset_combo.setMinimumWidth(220)
        self.preset_combo.setMaximumWidth(340)
        self.preset_combo.setToolTip(PR_PRESET_TOOLTIP)
        self._populate_preset_combo()
        self.preset_combo.currentIndexChanged.connect(
            self._on_preset_combo_changed)
        preset_grid.addWidget(self.preset_combo, 0, 1)

        direct_label = QLabel(QUICK_MSG_PRESET_LABEL)
        direct_label.setObjectName('_leapDimLabel')
        preset_grid.addWidget(direct_label, 1, 0, Qt.AlignRight | Qt.AlignVCenter)

        self.direct_preset_combo = QComboBox()
        self.direct_preset_combo.setObjectName('direct_preset_combo')
        self.direct_preset_combo.setMinimumWidth(220)
        self.direct_preset_combo.setMaximumWidth(340)
        self.direct_preset_combo.setToolTip(QUICK_MSG_PRESET_TOOLTIP)
        self._populate_direct_preset_combo()
        self.direct_preset_combo.currentIndexChanged.connect(
            self._on_direct_preset_combo_changed)
        preset_grid.addWidget(self.direct_preset_combo, 1, 1)

        preset_card_layout.addLayout(preset_grid)
        preset_card_layout.addStretch()
        layout.addWidget(preset_card)

        # ═══════════════════════════════════════════════════════════════
        #  TABLE TOOLBAR — "+ Add Session" prominent on the left
        # ═══════════════════════════════════════════════════════════════
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setContentsMargins(0, 6, 0, 4)

        self._add_btn = QPushButton('  Add Session')
        self._add_btn.setObjectName('_leapAddBtn')
        self._add_btn.setToolTip('Add session from Git URL or local path')
        self._add_btn.setIcon(self._make_plus_icon(
            current_theme().accent_blue.encode()))
        self._add_btn.clicked.connect(self._add_row_menu)
        toolbar_layout.addWidget(self._add_btn)
        toolbar_layout.addStretch()
        layout.addLayout(toolbar_layout)

        # Table (with subtle top/bottom border)
        table_frame = QFrame()
        table_frame.setObjectName('_leapTableFrame')
        table_frame_layout = QVBoxLayout(table_frame)
        table_frame_layout.setContentsMargins(0, 0, 0, 0)
        table_frame_layout.setSpacing(0)
        table_frame_layout.addWidget(self.table)
        layout.addWidget(table_frame, 1)

        # ═══════════════════════════════════════════════════════════════
        #  BOTTOM BAR — options left, connections right
        # ═══════════════════════════════════════════════════════════════
        bottom_card = QFrame()
        bottom_card.setObjectName('_leapCard')
        bottom_inner = QHBoxLayout(bottom_card)
        bottom_inner.setContentsMargins(16, 8, 16, 8)
        bottom_inner.setSpacing(16)

        self.bots_check = QCheckBox('Include git bots')
        self.bots_check.setToolTip('Count bot comments as responses in PR thread detection')
        self.bots_check.setChecked(self._prefs.get('include_bots', False))
        self.bots_check.stateChanged.connect(self._toggle_include_bots)
        bottom_inner.addWidget(self.bots_check, 0, Qt.AlignVCenter)

        self.auto_leap_check = QCheckBox("Auto '/leap' fetch")
        self.auto_leap_check.setToolTip(
            'Automatically send /leap-tagged PR threads to Leap sessions each poll cycle'
        )
        self.auto_leap_check.setChecked(self._prefs.get('auto_fetch_leap', True))
        self.auto_leap_check.stateChanged.connect(self._toggle_auto_fetch_leap)
        bottom_inner.addWidget(self.auto_leap_check, 0, Qt.AlignVCenter)



        bottom_inner.addStretch()

        # SCM connect buttons — grouped in a tight container so they
        # share identical vertical alignment independent of the left-side
        # checkboxes (works around a macOS Qt rendering quirk).
        btn_group = QWidget()
        btn_group_layout = QHBoxLayout(btn_group)
        btn_group_layout.setContentsMargins(0, 0, 0, 0)
        btn_group_layout.setSpacing(8)

        self.gitlab_btn = QPushButton('Connect GitLab')
        self.gitlab_btn.setToolTip('Configure GitLab connection for PR tracking')
        self.gitlab_btn.clicked.connect(self._open_gitlab_setup)
        btn_group_layout.addWidget(self.gitlab_btn)

        self.github_btn = QPushButton('Connect GitHub')
        self.github_btn.setToolTip('Configure GitHub connection for PR tracking')
        self.github_btn.clicked.connect(self._open_github_setup)
        btn_group_layout.addWidget(self.github_btn)

        self.slack_bot_btn = QPushButton('Slack Bot')
        self.slack_bot_btn.setToolTip('Start/stop the Slack bot daemon')
        self.slack_bot_btn.clicked.connect(self._toggle_slack_bot)
        self.slack_bot_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        self.slack_bot_btn.customContextMenuRequested.connect(
            self._slack_bot_context_menu)
        self.slack_bot_btn.setVisible(self._slack_available)
        btn_group_layout.addWidget(self.slack_bot_btn)

        bottom_inner.addWidget(btn_group)

        layout.addWidget(bottom_card)

        # ═══════════════════════════════════════════════════════════════
        #  STATUS BAR — logs + progress left, close right
        # ═══════════════════════════════════════════════════════════════
        status_layout = QHBoxLayout()
        status_layout.setContentsMargins(4, 4, 4, 4)

        full_log_btn = QPushButton('Logs')
        full_log_btn.setObjectName('_leapLogsBtn')
        full_log_btn.setToolTip('View full status message history')
        full_log_btn.clicked.connect(self._open_log_history)
        status_layout.addWidget(full_log_btn)

        self._log_label = QLabel('')
        self._log_label.setObjectName('_leapStatusLabel')
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
        close_btn.setObjectName('_leapCloseBtn')
        close_btn.setToolTip('Close the monitor')
        close_btn.clicked.connect(self._confirm_close)
        status_layout.addWidget(close_btn)

        layout.addLayout(status_layout)

    # ------------------------------------------------------------------
    #  Core utilities
    # ------------------------------------------------------------------

    def _apply_window_effects(self) -> None:
        """Apply macOS-specific visual effects (titlebar blending)."""
        try:
            from AppKit import NSApplication, NSWindow
            from PyQt5.QtGui import QWindow
            # Make the window titlebar transparent and blend with content
            ns_window = None
            win_handle = self.windowHandle()
            if win_handle:
                # Get the NSWindow from the QWindow
                view = int(win_handle.winId())
                for w in NSApplication.sharedApplication().windows():
                    if w.contentView() and int(w.contentView().window().windowNumber()) == int(
                        NSApplication.sharedApplication().keyWindow().windowNumber()
                        if NSApplication.sharedApplication().keyWindow() else -1
                    ):
                        ns_window = w
                        break
            if ns_window is None:
                # Fallback: get the last window
                windows = NSApplication.sharedApplication().windows()
                if windows:
                    ns_window = windows[-1]
            if ns_window:
                # Transparent titlebar that blends with content
                ns_window.setTitlebarAppearsTransparent_(True)
                # Use full-size content view so content extends behind titlebar
                from AppKit import (
                    NSFullSizeContentViewWindowMask,
                    NSWindowStyleMaskFullSizeContentView,
                )
                mask = ns_window.styleMask()
                ns_window.setStyleMask_(mask | NSWindowStyleMaskFullSizeContentView)
        except Exception:
            pass  # Non-macOS or pyobjc not available

    def _update_logo_pixmap(self) -> None:
        """Load the themed logo variant for the current theme."""
        t = current_theme()
        # Map theme name to logo filename suffix
        suffix = t.name.lower().replace(' ', '-')
        # Try themed variant first, fall back to original
        assets = Path(__file__).parent.parent.parent.parent / 'assets'
        bundle_assets: Optional[Path] = None
        for p in Path(__file__).parents:
            if p.name == 'Resources' and p.parent.name == 'Contents':
                bundle_assets = p
                break
        logo_path = assets / f'leap-text-{suffix}.png'
        if not logo_path.exists() and bundle_assets:
            logo_path = bundle_assets / f'leap-text-{suffix}.png'
        if not logo_path.exists():
            logo_path = assets / 'leap-text.png'
        if not logo_path.exists() and bundle_assets:
            logo_path = bundle_assets / 'leap-text.png'
        if logo_path.exists():
            pm = QPixmap(str(logo_path)).scaledToHeight(
                40, Qt.SmoothTransformation)
            self._logo_label.setPixmap(pm)

    @staticmethod
    def _hex_rgb(hex_color: str) -> str:
        """Convert '#rrggbb' to 'r, g, b' for rgba() in QSS."""
        h = hex_color.lstrip('#')
        return f'{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}'

    @staticmethod
    def _make_plus_icon(color: bytes = b'#ffffff', size: int = 16) -> QIcon:
        """Render a plus (+) icon as SVG at the given size and color."""
        from PyQt5.QtSvg import QSvgRenderer
        svg = (
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
            b'<line x1="8" y1="2" x2="8" y2="14" stroke="' + color +
            b'" stroke-width="2.5" stroke-linecap="round"/>'
            b'<line x1="2" y1="8" x2="14" y2="8" stroke="' + color +
            b'" stroke-width="2.5" stroke-linecap="round"/>'
            b'</svg>'
        )
        renderer = QSvgRenderer(svg)
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        from PyQt5.QtGui import QPainter as _P
        p = _P(pm)
        renderer.render(p)
        p.end()
        return QIcon(pm)

    @staticmethod
    def _ensure_chevron_icon(color_hex: str, up: bool = False) -> str:
        """Generate a small chevron PNG for dropdown/spinbox arrows.

        A separate file is generated per color+direction so theme switches work.
        """
        from leap.utils.constants import STORAGE_DIR
        safe_name = color_hex.lstrip('#')
        direction = 'up' if up else 'down'
        path = STORAGE_DIR / f'chevron_{direction}_{safe_name}.png'
        if not path.exists():
            pm = QPixmap(12, 12)
            pm.fill(Qt.transparent)
            from PyQt5.QtGui import QPainter, QPen, QPainterPath
            painter = QPainter(pm)
            painter.setRenderHint(QPainter.Antialiasing)
            pen = QPen(QColor(color_hex))
            pen.setWidth(2)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            arrow = QPainterPath()
            if up:
                arrow.moveTo(2, 8)
                arrow.lineTo(6, 4)
                arrow.lineTo(10, 8)
            else:
                arrow.moveTo(2, 4)
                arrow.lineTo(6, 8)
                arrow.lineTo(10, 4)
            painter.drawPath(arrow)
            painter.end()
            pm.save(str(path), 'PNG')
        return str(path)

    @staticmethod
    def _ensure_checkmark_icon() -> str:
        """Generate a white checkmark PNG for checkbox indicators.

        Returns the file path as a string.  The icon is cached in
        ``.storage/`` so it's only generated once per install.
        """
        from leap.utils.constants import STORAGE_DIR
        path = STORAGE_DIR / 'checkmark.png'
        if not path.exists():
            pm = QPixmap(18, 18)
            pm.fill(Qt.transparent)
            from PyQt5.QtGui import QPainter, QPen
            painter = QPainter(pm)
            painter.setRenderHint(QPainter.Antialiasing)
            pen = QPen(QColor('#ffffff'))
            pen.setWidth(3)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            # Draw checkmark path: ✓
            from PyQt5.QtGui import QPainterPath
            check_path = QPainterPath()
            check_path.moveTo(3.5, 9.5)
            check_path.lineTo(7, 13.5)
            check_path.lineTo(14.5, 4.5)
            painter.drawPath(check_path)
            painter.end()
            pm.save(str(path), 'PNG')
        return str(path)

    @staticmethod
    def _ensure_radio_icon() -> str:
        """Generate a white dot PNG for radio button indicators."""
        from leap.utils.constants import STORAGE_DIR
        path = STORAGE_DIR / 'radio_dot.png'
        if not path.exists():
            pm = QPixmap(18, 18)
            pm.fill(Qt.transparent)
            from PyQt5.QtGui import QPainter
            painter = QPainter(pm)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setBrush(QColor('#ffffff'))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(5, 5, 8, 8)
            painter.end()
            pm.save(str(path), 'PNG')
        return str(path)

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
                t = current_theme()
                display_msg = e.message.replace(
                    '[Notification]',
                    f'<span style="color: {t.accent_blue};">[Notification]</span>',
                    1,
                ) if '[Notification]' in e.message else e.message
                self._log_label.setText(
                    f'[{ts}] {display_msg} '
                    f'<a href="{e.url}" style="color: {t.accent_blue};">(link)</a>'
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
        self.resize(1476, 719)
        screen = QApplication.primaryScreen().availableGeometry()
        x = (screen.width() - 1476) // 2 + screen.x()
        y = (screen.height() - 719) // 2 + screen.y()
        self.move(x, y)

    def _apply_equal_column_widths(self) -> None:
        """Distribute column widths equally across visible resizable columns."""
        col_count = self.table.columnCount()
        if col_count <= 0:
            return
        viewport_w = self.table.viewport().width()
        if viewport_w <= 0:
            # Viewport not ready yet — estimate from window geometry
            viewport_w = (self.geometry().width() or 1476) - 50
        delete_w = self.table.columnWidth(self.COL_DELETE)
        available = viewport_w - delete_w
        # Only count visible, non-fixed columns (skip hidden like Slack)
        visible_cols = [
            col for col in range(col_count)
            if col != self.COL_DELETE and not self.table.isColumnHidden(col)
        ]
        col_width = available // max(len(visible_cols), 1)
        self._resizing_columns = True
        for col in visible_cols:
            self.table.setColumnWidth(col, col_width)
        self._resizing_columns = False

    def _on_section_resized(self, index: int, old_size: int, new_size: int) -> None:
        """Clamp column resizes so total width never exceeds viewport.

        When the user drags a column wider, steal space from subsequent
        visible columns (min 30 px each).  If the column was narrowed,
        give the freed space to the last visible column.
        """
        if self._resizing_columns or not self._ui_ready:
            return
        if index == self.COL_DELETE:
            return

        viewport_w = self.table.viewport().width()
        if viewport_w <= 0:
            return

        col_count = self.table.columnCount()
        delete_w = self.table.columnWidth(self.COL_DELETE)
        available = viewport_w - delete_w

        visible_cols = [
            c for c in range(col_count)
            if c != self.COL_DELETE and not self.table.isColumnHidden(c)
        ]
        total = sum(self.table.columnWidth(c) for c in visible_cols)
        overflow = total - available
        if overflow <= 0:
            # Columns fit — give leftover to the last visible column
            if visible_cols and overflow < 0:
                last = visible_cols[-1]
                self._resizing_columns = True
                self.table.setColumnWidth(
                    last, self.table.columnWidth(last) - overflow)
                self._resizing_columns = False
            return

        # Overflow: shrink columns *after* the resized one
        self._resizing_columns = True
        try:
            after = [c for c in visible_cols if c > index]
            # Try to absorb overflow from columns after the resized one
            for col in after:
                if overflow <= 0:
                    break
                cur = self.table.columnWidth(col)
                shrink = min(overflow, cur - 30)
                if shrink > 0:
                    self.table.setColumnWidth(col, cur - shrink)
                    overflow -= shrink

            # If still overflowing, cap the resized column itself
            if overflow > 0:
                self.table.setColumnWidth(index, max(30, new_size - overflow))
        finally:
            self._resizing_columns = False

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
        """Check if a widget is inside the session table (excludes scrollbars)."""
        from PyQt5.QtWidgets import QScrollBar
        w = widget
        while w is not None:
            if isinstance(w, QScrollBar):
                return False
            if w is self.table:
                return True
            w = w.parent() if hasattr(w, 'parent') else None
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
                    self._drag_press_time = time.time()
                else:
                    self._drag_source_row = -1

        elif etype == QEvent.MouseMove:
            if (self._drag_source_row >= 0
                    and event.buttons() & Qt.LeftButton
                    and self._is_in_table(obj)):
                # Require both distance (30px) and hold time (300ms)
                # to distinguish intentional drag from scroll gestures
                held_ms = (time.time() - self._drag_press_time) * 1000
                dist = (event.globalPos() - self._drag_start_pos).manhattanLength()
                if dist >= 30 and held_ms >= 300:
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
        self._resizing_columns = True
        cumulative_old = 0
        used = 0
        for col in resizable_cols:
            cumulative_old += self.table.columnWidth(col)
            target = round(available * cumulative_old / resizable_total)
            w = max(30, target - used)
            self.table.setColumnWidth(col, w)
            used += w
        self._resizing_columns = False

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

        Applies a comprehensive QSS for a modern look (rounded buttons,
        styled inputs, scrollbars, menus) on top of a QPalette base.
        """
        if theme_name not in THEMES:
            return
        set_theme(theme_name)
        t = current_theme()
        r = t.border_radius

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

        # QPalette — base colors for native widget integration
        pal = QPalette()
        pal.setColor(QPalette.Window, QColor(t.window_bg))
        pal.setColor(QPalette.WindowText, QColor(t.text_primary))
        pal.setColor(QPalette.Base, QColor(t.input_bg))
        pal.setColor(QPalette.AlternateBase, QColor(t.cell_bg_alt))
        pal.setColor(QPalette.Text, QColor(t.text_primary))
        pal.setColor(QPalette.Button, QColor(t.button_bg or t.window_bg))
        pal.setColor(QPalette.ButtonText, QColor(t.text_primary))
        pal.setColor(QPalette.Highlight, QColor(t.accent_blue))
        pal.setColor(QPalette.HighlightedText, QColor('#ffffff' if t.is_dark else '#000000'))
        pal.setColor(QPalette.ToolTipBase, QColor(t.popup_bg))
        pal.setColor(QPalette.ToolTipText, QColor(t.text_primary))
        pal.setColor(QPalette.PlaceholderText, QColor(t.text_muted))
        pal.setColor(QPalette.Link, QColor(t.accent_blue))
        pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(t.text_muted))
        pal.setColor(QPalette.Disabled, QPalette.Text, QColor(t.text_muted))
        pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(t.text_muted))
        app.setPalette(pal)

        # Resolve scrollbar colors (fall back to border colors)
        sb_bg = t.scrollbar_bg or t.window_bg
        sb_handle = t.scrollbar_handle or t.border_solid
        sb_hover = t.scrollbar_handle_hover or t.text_muted
        btn_bg = t.button_bg or t.window_bg
        btn_hover = t.button_hover_bg or t.border_solid
        btn_border = t.button_border or t.border_solid

        # Comprehensive QSS for modern appearance
        app.setStyleSheet(f"""
            /* --- Global font --- */
            * {{
                font-size: {t.font_size_base}px;
            }}

            /* --- Buttons --- */
            QPushButton {{
                background-color: {btn_bg};
                color: {t.text_primary};
                border: 1px solid {btn_border};
                border-radius: {r}px;
                padding: 5px 16px;
                font-size: {t.font_size_base}px;
                font-weight: 500;
                min-height: 18px;
            }}
            QPushButton:hover {{
                background-color: {btn_hover};
                border-color: {t.accent_blue};
                color: {t.text_primary};
            }}
            QPushButton:pressed {{
                background-color: {t.window_bg};
                border-color: {t.accent_blue};
            }}
            QPushButton:disabled {{
                color: {t.text_muted};
                border-color: {btn_border};
                background-color: {btn_bg};
            }}
            QPushButton:flat {{
                background: transparent;
                border: none;
            }}

            /* --- Combo boxes --- */
            QComboBox {{
                background-color: {btn_bg};
                color: {t.text_primary};
                border: 1px solid {btn_border};
                border-radius: {r}px;
                padding: 5px 10px;
                font-size: {t.font_size_base}px;
                min-height: 18px;
            }}
            QComboBox:hover {{
                border-color: {t.accent_blue};
            }}
            QComboBox:focus {{
                border-color: {t.input_focus_border};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 24px;
            }}
            QComboBox::down-arrow {{
                image: url({self._ensure_chevron_icon(t.text_secondary)});
                width: 10px;
                height: 10px;
                margin-right: 6px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {t.popup_bg};
                color: {t.text_primary};
                border: 1px solid {t.popup_border};
                border-radius: {r}px;
                selection-background-color: {btn_hover};
                selection-color: {t.text_primary};
                padding: 4px;
                outline: none;
            }}

            /* --- Check boxes --- */
            QCheckBox {{
                spacing: 8px;
                font-size: {t.font_size_base}px;
                color: {t.text_primary};
            }}
            QCheckBox::indicator {{
                width: 18px;
                height: 18px;
                border: 2px solid {btn_border};
                border-radius: 4px;
                background-color: {btn_bg};
            }}
            QCheckBox::indicator:checked {{
                background-color: {t.accent_blue};
                border-color: {t.accent_blue};
                image: url({self._ensure_checkmark_icon()});
            }}
            QCheckBox::indicator:hover {{
                border-color: {t.accent_blue};
            }}

            /* --- Radio buttons --- */
            QRadioButton {{
                spacing: 8px;
                font-size: {t.font_size_base}px;
                color: {t.text_primary};
            }}
            QRadioButton::indicator {{
                width: 18px;
                height: 18px;
                border: 2px solid {btn_border};
                border-radius: 10px;
                background-color: {btn_bg};
            }}
            QRadioButton::indicator:checked {{
                background-color: {t.accent_blue};
                border-color: {t.accent_blue};
                image: url({self._ensure_radio_icon()});
            }}
            QRadioButton::indicator:hover {{
                border-color: {t.accent_blue};
            }}

            /* --- Line edits --- */
            QLineEdit {{
                background-color: {t.input_bg};
                color: {t.text_primary};
                border: 1px solid {t.input_border};
                border-radius: {r}px;
                padding: 6px 10px;
                font-size: {t.font_size_base}px;
                selection-background-color: {t.accent_blue};
            }}
            QLineEdit:focus {{
                border: 2px solid {t.input_focus_border};
                padding: 5px 9px;
            }}

            /* --- Text edits --- */
            QTextEdit, QPlainTextEdit {{
                background-color: {t.input_bg};
                color: {t.text_primary};
                border: 1px solid {t.input_border};
                border-radius: {r}px;
                padding: 4px;
                font-size: {t.font_size_base}px;
                selection-background-color: {t.accent_blue};
            }}
            QTextEdit:focus, QPlainTextEdit:focus {{
                border: 2px solid {t.input_focus_border};
                padding: 3px;
            }}

            /* --- Spin boxes --- */
            QSpinBox {{
                background-color: {t.input_bg};
                color: {t.text_primary};
                border: 1px solid {t.input_border};
                border-radius: {r}px;
                padding: 4px 24px 4px 8px;
                font-size: {t.font_size_base}px;
            }}
            QSpinBox:focus {{
                border: 2px solid {t.input_focus_border};
                padding: 3px 23px 3px 7px;
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                background-color: {btn_bg};
                border: none;
                border-left: 1px solid {t.input_border};
                width: 20px;
            }}
            QSpinBox::up-button {{
                border-top-right-radius: {r}px;
            }}
            QSpinBox::down-button {{
                border-bottom-right-radius: {r}px;
            }}
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
                background-color: {btn_hover};
            }}
            QSpinBox::up-arrow {{
                image: url({self._ensure_chevron_icon(t.text_secondary, up=True)});
                width: 10px;
                height: 10px;
            }}
            QSpinBox::down-arrow {{
                image: url({self._ensure_chevron_icon(t.text_secondary)});
                width: 10px;
                height: 10px;
            }}

            /* --- Table --- */
            QTableWidget {{
                background-color: {t.cell_bg};
                alternate-background-color: {t.cell_bg_alt};
                gridline-color: transparent;
                border: none;
                font-size: {t.font_size_base}px;
            }}
            QHeaderView::section {{
                background-color: {t.header_bg};
                color: {t.text_muted};
                border: none;
                border-bottom: 2px solid {t.border_solid};
                padding: 8px 6px;
                font-size: {t.font_size_small}px;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 1px;
            }}

            /* --- Menus --- */
            QMenu {{
                background-color: {t.popup_bg};
                color: {t.text_primary};
                border: 1px solid {t.popup_border};
                border-radius: {r + 2}px;
                padding: 6px;
                font-size: {t.font_size_base}px;
            }}
            QMenu::item {{
                padding: 8px 28px 8px 14px;
                border-radius: {r}px;
                margin: 1px 2px;
            }}
            QMenu::item:selected {{
                background-color: {btn_hover};
            }}
            QMenu::item:disabled {{
                color: {t.text_muted};
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {t.popup_border};
                margin: 6px 10px;
            }}

            /* --- Tooltips --- */
            QToolTip {{
                background-color: rgba({self._hex_rgb(t.popup_bg)}, 200);
                color: {t.text_primary};
                border: 1px solid {t.popup_border};
                border-radius: {r - 2}px;
                padding: 3px 6px;
                font-size: {t.font_size_base}px;
            }}

            /* --- Scrollbars (thin modern) --- */
            QScrollBar:vertical {{
                background: {sb_bg};
                width: 8px;
                margin: 0;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {sb_handle};
                min-height: 30px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {sb_hover};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
            QScrollBar:horizontal {{
                background: {sb_bg};
                height: 8px;
                margin: 0;
                border: none;
            }}
            QScrollBar::handle:horizontal {{
                background: {sb_handle};
                min-width: 30px;
                border-radius: 4px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {sb_hover};
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0;
            }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                background: none;
            }}

            /* --- Tab widgets --- */
            QTabWidget::pane {{
                border: 1px solid {t.popup_border};
                border-radius: {r}px;
                background-color: {t.window_bg};
            }}
            QTabBar::tab {{
                background-color: {btn_bg};
                color: {t.text_secondary};
                border: 1px solid {btn_border};
                border-bottom: none;
                padding: 6px 16px;
                border-top-left-radius: {r}px;
                border-top-right-radius: {r}px;
                font-size: {t.font_size_base}px;
            }}
            QTabBar::tab:selected {{
                background-color: {t.window_bg};
                color: {t.text_primary};
                border-color: {t.popup_border};
            }}
            QTabBar::tab:hover:!selected {{
                background-color: {btn_hover};
            }}

            /* --- Progress bar --- */
            QProgressBar {{
                background-color: {btn_bg};
                border: none;
                border-radius: 6px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {t.accent_blue},
                    stop:1 {t.input_focus_border});
                border-radius: 6px;
            }}

            /* --- Dialogs --- */
            QDialog {{
                background-color: {t.window_bg};
            }}

            /* --- Dialog button box (OK/Cancel/Apply) --- */
            QDialogButtonBox QPushButton {{
                min-width: 80px;
            }}

            /* --- Labels (base) --- */
            QLabel {{
                font-size: {t.font_size_base}px;
            }}

            /* --- Group boxes --- */
            QGroupBox {{
                border: 1px solid {t.popup_border};
                border-radius: {r}px;
                margin-top: 8px;
                padding-top: 14px;
                font-size: {t.font_size_base}px;
                color: {t.text_secondary};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 4px;
            }}

            /* --- List widgets --- */
            QListWidget {{
                background-color: {t.cell_bg};
                color: {t.text_primary};
                border: 1px solid {t.input_border};
                border-radius: {r}px;
                font-size: {t.font_size_base}px;
            }}
            QListWidget::item {{
                padding: 4px 8px;
                border-radius: {max(1, r - 2)}px;
            }}
            QListWidget::item:selected {{
                background-color: {btn_hover};
            }}
            QListWidget::item:hover:!selected {{
                background-color: {t.hover_bg};
            }}

            /* --- Message box --- */
            QMessageBox {{
                background-color: {t.window_bg};
            }}

            /* --- Cell wrapper transparency (for table cell widgets) --- */
            #_leapSep {{
                background: transparent;
            }}

            /* --- Section dividers (horizontal & vertical) --- */
            #_leapDivider {{
                color: {t.border_solid};
                background-color: {t.border_solid};
                border: none;
            }}

            /* --- Card panels (presets, bottom bar) --- */
            #_leapCard {{
                background-color: {t.cell_bg_alt};
                border: 1px solid {t.border_solid};
                border-top: 1px solid {t.popup_border};
                border-radius: {r}px;
                margin: 2px 0px;
            }}

            /* --- Table frame (subtle border) --- */
            #_leapTableFrame {{
                border-top: 1px solid {t.border_solid};
                border-bottom: 1px solid {t.border_solid};
            }}

            /* --- Dim labels (preset labels, section hints) --- */
            #_leapDimLabel {{
                color: {t.text_muted};
                font-size: {t.font_size_small}px;
            }}

            /* --- Ghost buttons (toolbar: Settings, Notes, Reset) --- */
            #_leapGhostBtn {{
                color: {t.text_secondary};
                background: transparent;
                border: 1px solid transparent;
                border-radius: {r}px;
                padding: 4px 12px;
                font-weight: normal;
            }}
            #_leapGhostBtn:hover {{
                color: {t.text_primary};
                background-color: {btn_hover};
                border-color: {btn_border};
            }}

            /* --- Add Session button (outlined accent) --- */
            #_leapAddBtn {{
                color: {t.accent_blue};
                background-color: {btn_bg};
                border: 1px solid {t.accent_blue};
                border-radius: {r}px;
                padding: 6px 20px;
                font-weight: bold;
                font-size: {t.font_size_base}px;
            }}
            #_leapAddBtn:hover {{
                background-color: rgba({self._hex_rgb(t.accent_blue)}, 18);
                border-color: {t.input_focus_border};
                color: {t.input_focus_border};
            }}
            #_leapAddBtn:pressed {{
                background-color: rgba({self._hex_rgb(t.accent_blue)}, 30);
            }}

            /* --- Close button (danger outline) --- */
            #_leapCloseBtn {{
                color: {t.accent_red};
                background: transparent;
                border: 1px solid {t.accent_red};
                border-radius: {r}px;
                padding: 4px 14px;
                font-weight: normal;
            }}
            #_leapCloseBtn:hover {{
                background-color: {t.accent_red};
                color: #ffffff;
            }}
            #_leapCloseBtn:pressed {{
                background-color: {t.accent_red};
                border-width: 2px;
                padding: 3px 13px;
            }}

            /* --- Logs button --- */
            #_leapLogsBtn {{
                color: {t.text_secondary};
                background: transparent;
                border: 1px solid {t.text_secondary};
                border-radius: 4px;
                padding: 2px 8px;
                font-weight: normal;
            }}
            #_leapLogsBtn:hover {{
                color: {t.text_primary};
                border-color: {t.text_primary};
            }}

            /* --- Status bar label --- */
            #_leapStatusLabel {{
                color: {t.text_secondary};
                font-size: {t.font_size_small}px;
            }}

            /* --- Logo/toolbar bar --- */
            #_leapLogoBar {{
                background-color: {t.header_bg};
                border-bottom: 1px solid {t.border_solid};
            }}
        """)

        # Clear cell cache to force full rebuild with new colors
        self._cell_cache.clear()
        self._update_table()

        # Re-apply SCM button styles
        self._update_scm_buttons()
        self._update_slack_bot_button()

        # Refresh Notes button icon with new theme color
        _ni = notes_icon(size=16)
        if _ni and hasattr(self, '_notes_btn'):
            self._notes_btn.setIcon(_ni)

        # Log label color is handled by #_leapStatusLabel in the global QSS

        # Update drop indicator color
        if self._drop_indicator:
            self._drop_indicator.setStyleSheet(
                f'background-color: {t.accent_blue};')

        # Update Add Session button icon color
        self._add_btn.setIcon(self._make_plus_icon(t.accent_blue.encode()))

        # Update logo to themed variant
        self._update_logo_pixmap()

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
                # Mask to only the four modifier keys we care about:
                # Shift (1<<17), Control (1<<18), Option (1<<19), Command (1<<20).
                # Ignores CapsLock (1<<16), NumericPad (1<<21), Function (1<<23).
                _MOD_MASK = (1 << 17) | (1 << 18) | (1 << 19) | (1 << 20)
                ev_flags = event.modifierFlags() & _MOD_MASK
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
