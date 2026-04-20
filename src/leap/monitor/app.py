"""
Leap Monitor GUI application.

PyQt5-based GUI for viewing and managing active Leap sessions.
"""

import faulthandler
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Optional

import objc
from AppKit import (
    NSAppearance, NSApplication, NSEvent,
    NSImage, NSKeyDownMask, NSWindowStyleMaskFullSizeContentView,
)
from Foundation import NSDate, NSRunLoop
from PyQt5.QtWidgets import (
    QAction, QApplication, QComboBox, QDialog, QFrame, QMainWindow, QMenu,
    QScrollBar, QShortcut, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QStackedLayout,
    QTableWidget, QPushButton, QCheckBox, QHeaderView, QMessageBox,
    QProgressBar,
)
from PyQt5.QtCore import QEvent, QMimeData, QPoint, QProcess, QRect, QSize, QTimer, Qt
from PyQt5.QtGui import (
    QColor, QCursor, QDrag, QIcon, QCloseEvent, QKeySequence,
    QPainter, QPainterPath, QPalette, QPen, QPixmap, QResizeEvent,
)
from PyQt5.QtSvg import QSvgRenderer

from leap.monitor.dialogs.settings_dialog import detect_default_difftool
from leap.monitor.popup_zoom import PopupZoomManager
from leap.monitor.pr_tracking.base import PRStatus, SCMProvider
from leap.monitor.pr_tracking.config import (
    load_auto_fetch_preset_name, load_monitor_prefs, load_notification_seen,
    load_pinned_sessions, load_saved_presets, save_auto_fetch_preset_name,
    save_monitor_prefs,
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
    PersistentTooltipStyle, SeparatorDelegate, SeparatorHeaderView, TooltipApp,
)
from leap.monitor.ui.ui_widgets import PulsingLabel, ShimmerBar, IndicatorLabel
from leap.utils.constants import ICON_CACHE_DIR, STORAGE_DIR

from leap.slack.config import is_slack_installed
from leap.monitor._mixins.actions_menu_mixin import ActionsMenuMixin
from leap.monitor._mixins.scm_config_mixin import SCMConfigMixin
from leap.monitor._mixins.session_mixin import SessionMixin
from leap.monitor._mixins.pr_tracking_mixin import PRTrackingMixin
from leap.monitor._mixins.pr_display_mixin import PRDisplayMixin, _setup_modern_notifications
from leap.monitor._mixins.notifications_mixin import NotificationsMixin
from leap.monitor._mixins.table_builder_mixin import TableBuilderMixin

logger = logging.getLogger(__name__)


def _pin_checkbox_min_width(check: QCheckBox) -> None:
    """Pin a QCheckBox's minimum width so its label can never be clipped.

    Computes the width from the text's fontMetrics plus generous room
    for the checkbox indicator and Qt padding. sizeHint() alone comes up
    a few pixels short on macOS when the label contains apostrophes or
    slashes ("Auto '/leap' fetch"), which caused visible truncation
    when the window was resized narrow.
    """
    fm = check.fontMetrics()
    text_width = fm.horizontalAdvance(check.text())
    # ~22px for the checkbox indicator + ~10px padding on each side.
    check.setMinimumWidth(text_width + 44)


class _RefreshableComboBox(QComboBox):
    """QComboBox that repopulates itself each time the popup opens.

    Used for controls whose options can change outside the combo's
    lifecycle (e.g. presets edited in a separate dialog). Avoids the
    need for signal plumbing between the editor and every live combo.
    """

    def __init__(self, refresh_fn: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._refresh_fn = refresh_fn

    def showPopup(self) -> None:
        self._refresh_fn()
        super().showPopup()


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
        self._wipe_icon_cache()
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
        self._notes_focused_monitor: Optional[object] = None
        self._notes_global_monitor: Optional[object] = None

        # Main-window font zoom (Cmd+scroll / Cmd+±/0)
        self._main_font_size: int = self._prefs.get(
            'main_font_size', current_theme().font_size_base)
        self._main_zoom_save_timer: Optional[QTimer] = None

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
        self._register_notes_shortcut()

        # Cmd+Q to quit — works even when modal dialogs are open.
        # macOS Dock Quit is unreliable for non-bundled Python apps,
        # so this ensures the user always has a way to quit.
        quit_shortcut = QShortcut(QKeySequence.Quit, self)
        quit_shortcut.setContext(Qt.ApplicationShortcut)
        quit_shortcut.activated.connect(self.close)

        # Set up modern notification API (UNUserNotificationCenter) for
        # banner action buttons and click handling
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

        # Delete column: auto-size to the X button width so zooming the
        # row-cell font (which scales the X's width) is always wrapped
        # by the column and the button never overflows.
        header.setSectionResizeMode(self.COL_DELETE, QHeaderView.ResizeToContents)

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
        self._logo_container = logo_container
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

        self._presets_btn = QPushButton('\u270e  Presets')
        self._presets_btn.setObjectName('_leapGhostBtn')
        self._presets_btn.setToolTip('Edit presets')
        self._presets_btn.clicked.connect(self._open_preset_editor)
        buttons_layout.addWidget(self._presets_btn)

        buttons_layout.addStretch()

        reset_cols_btn = QPushButton('Reset Window Sizes')
        reset_cols_btn.setObjectName('_leapGhostBtn')
        reset_cols_btn.setToolTip(
            'Reset window, column and dialog sizes to their defaults')
        reset_cols_btn.clicked.connect(self._reset_window_size)
        buttons_layout.addWidget(reset_cols_btn)
        stacked.addWidget(buttons_widget)

        layout.addWidget(logo_container)

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
        self.bots_check.setToolTip(
            'Treat bot-authored comments as real comments when detecting '
            'unresponded PR threads. When unchecked, bot comments are '
            'ignored (a thread that only has bot comments appears as '
            'no-activity).')
        self.bots_check.setChecked(self._prefs.get('include_bots', False))
        self.bots_check.stateChanged.connect(self._toggle_include_bots)
        _pin_checkbox_min_width(self.bots_check)
        bottom_inner.addWidget(self.bots_check, 0, Qt.AlignVCenter)

        # Checkbox + preset combo live in their own sub-layout so the
        # combo reads as part of the checkbox control, closer than the
        # 16px outer bottom_inner spacing but with enough breathing room
        # that the checkbox label doesn't bump into the combo's border
        # on macOS (QCheckBox doesn't add right-padding beyond the text
        # bounding box). The combo is hidden when the checkbox is
        # unchecked, and its popup self-refreshes on open so preset
        # edits made elsewhere show up next time the user opens it.
        auto_leap_group = QWidget()
        auto_leap_layout = QHBoxLayout(auto_leap_group)
        auto_leap_layout.setContentsMargins(0, 0, 0, 0)
        auto_leap_layout.setSpacing(10)

        self.auto_leap_check = QCheckBox("Auto '/leap' fetch")
        self.auto_leap_check.setToolTip(
            "Automatically send /leap-tagged PR comments to Leap sessions each poll cycle"
        )
        self.auto_leap_check.setChecked(self._prefs.get('auto_fetch_leap', False))
        self.auto_leap_check.stateChanged.connect(self._toggle_auto_fetch_leap)
        _pin_checkbox_min_width(self.auto_leap_check)
        auto_leap_layout.addWidget(self.auto_leap_check, 0, Qt.AlignVCenter)

        self.auto_leap_preset_combo = _RefreshableComboBox(
            self._populate_auto_leap_preset_combo)
        self.auto_leap_preset_combo.setToolTip(
            "Preset prepended to auto-fetched /leap comments. Separate "
            "from the 'Context preset' in the Send Comments dialog."
        )
        self.auto_leap_preset_combo.setMinimumWidth(140)
        self.auto_leap_preset_combo.currentIndexChanged.connect(
            self._on_auto_leap_preset_changed)
        self._populate_auto_leap_preset_combo()
        self.auto_leap_preset_combo.setVisible(
            self.auto_leap_check.isChecked())
        auto_leap_layout.addWidget(self.auto_leap_preset_combo, 0, Qt.AlignVCenter)

        bottom_inner.addWidget(auto_leap_group, 0, Qt.AlignVCenter)



        bottom_inner.addStretch()

        # SCM connect buttons — grouped in a tight container so they
        # share identical vertical alignment independent of the left-side
        # checkboxes (works around a macOS Qt rendering quirk).
        btn_group = QWidget()
        btn_group_layout = QHBoxLayout(btn_group)
        btn_group_layout.setContentsMargins(0, 0, 0, 0)
        btn_group_layout.setSpacing(8)

        # Label, style and tooltip for these buttons are set dynamically in
        # _update_scm_buttons() based on the current connection state.
        self.gitlab_btn = QPushButton('Connect GitLab')
        self.gitlab_btn.clicked.connect(self._open_gitlab_setup)
        btn_group_layout.addWidget(self.gitlab_btn)

        self.github_btn = QPushButton('Connect GitHub')
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
        self._progress_bar.setMaximumWidth(180)
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
            base = current_theme().font_size_base
            size = getattr(self, '_main_font_size', base)
            scale = max(0.5, size / base)
            logo_h = max(24, int(40 * scale))
            pm = QPixmap(str(logo_path)).scaledToHeight(
                logo_h, Qt.SmoothTransformation)
            self._logo_label.setPixmap(pm)

    @staticmethod
    def _hex_rgb(hex_color: str) -> str:
        """Convert '#rrggbb' to 'r, g, b' for rgba() in QSS."""
        h = hex_color.lstrip('#')
        return f'{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}'

    @staticmethod
    def _make_plus_icon(color: bytes = b'#ffffff', size: int = 16) -> QIcon:
        """Render a plus (+) icon as SVG at the given size and color."""
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
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        return QIcon(pm)

    @staticmethod
    def _wipe_icon_cache() -> None:
        """Remove all cached icon PNGs so stale theme variants don't accumulate."""
        # Clean up legacy icons from .storage/ root (pre-icon_cache migration)
        for f in STORAGE_DIR.glob('chevron_*.png'):
            f.unlink(missing_ok=True)
        for name in ('checkmark.png', 'radio_dot.png'):
            (STORAGE_DIR / name).unlink(missing_ok=True)
        # Wipe and recreate icon_cache/
        if ICON_CACHE_DIR.is_dir():
            for f in ICON_CACHE_DIR.iterdir():
                if f.suffix == '.png':
                    f.unlink(missing_ok=True)
        ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _ensure_chevron_icon(color_hex: str, up: bool = False) -> str:
        """Generate a small chevron PNG for dropdown/spinbox arrows.

        A separate file is generated per color+direction so theme switches work.
        """
        safe_name = color_hex.lstrip('#')
        direction = 'up' if up else 'down'
        path = ICON_CACHE_DIR / f'chevron_{direction}_{safe_name}.png'
        if not path.exists():
            pm = QPixmap(12, 12)
            pm.fill(Qt.transparent)
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
        ``.storage/icon_cache/`` and regenerated each launch.
        """
        path = ICON_CACHE_DIR / 'checkmark.png'
        if not path.exists():
            pm = QPixmap(18, 18)
            pm.fill(Qt.transparent)
            painter = QPainter(pm)
            painter.setRenderHint(QPainter.Antialiasing)
            pen = QPen(QColor('#ffffff'))
            pen.setWidth(3)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            # Draw checkmark path: ✓
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
        path = ICON_CACHE_DIR / 'radio_dot.png'
        if not path.exists():
            pm = QPixmap(18, 18)
            pm.fill(Qt.transparent)
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
        and positions are reset.  Also resizes any dialog currently open
        (Notes, Settings, CommitList, etc.) back to its ``_DEFAULT_SIZE``
        class attribute — otherwise the dialog would save its current
        size back to disk on close and silently undo this reset.
        """
        self._prefs.pop('dialog_geometry', None)
        save_monitor_prefs(self._prefs)

        self._center_on_screen()
        self._apply_equal_column_widths()

        # Resize any currently-open dialog back to its declared default.
        for dlg in self.findChildren(QDialog):
            if not dlg.isVisible():
                continue
            default = getattr(dlg, '_DEFAULT_SIZE', None)
            if default is not None and len(default) == 2:
                dlg.resize(default[0], default[1])

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

        # ── Main-window font zoom (Cmd+wheel / Cmd+±/0) ─────────────
        # Resolve target by mouse cursor (Qt sometimes routes wheel to
        # the focus widget on macOS), falling back to obj.  Keyboard
        # shares the same routing so the two gestures are consistent.
        if etype == QEvent.Wheel:
            if event.modifiers() & Qt.ControlModifier:
                target = QApplication.widgetAt(QCursor.pos()) or obj
                if self._main_zoom_owns_widget(target):
                    delta = 1 if event.angleDelta().y() > 0 else -1
                    self._zoom_main_delta(delta)
                    return True
        elif etype == QEvent.KeyPress:
            if event.modifiers() & Qt.ControlModifier:
                target = QApplication.widgetAt(QCursor.pos()) or obj
                if self._main_zoom_owns_widget(target):
                    key = event.key()
                    if key in (Qt.Key_Equal, Qt.Key_Plus):
                        self._zoom_main_delta(1)
                        return True
                    if key == Qt.Key_Minus:
                        self._zoom_main_delta(-1)
                        return True
                    if key == Qt.Key_0:
                        self._zoom_main_reset()
                        return True

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
        """Reset dock badge + tooltip font when window becomes active."""
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            self._clear_dock_badge()
            # Restore the tooltip font to the main-window zoom size
            # (dialogs set it to their size while they're active).
            self.set_tooltip_font_size(
                getattr(self, '_main_font_size', 13))

    def _auto_refresh(self) -> None:
        """Auto-refresh callback."""
        if self._shutting_down:
            return
        try:
            self._refresh_data()
        except Exception:
            logger.exception("Error in auto-refresh")

    _AUTO_LEAP_PRESET_NONE = '(None)'

    def _populate_auto_leap_preset_combo(self) -> None:
        """Fill the auto-fetch preset combo with single-message presets.

        Mirrors SendCommentsDialog._populate_ctx_combo's filter
        (``len(messages) <= 1``) and self-heal (clear stale selection
        if the saved preset vanished or grew multi-message) — so a
        preset edited elsewhere doesn't leave the combo in a ghost state.
        """
        combo = self.auto_leap_preset_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(self._AUTO_LEAP_PRESET_NONE)
        names: list[str] = []
        for name, messages in sorted(load_saved_presets().items()):
            if len(messages) <= 1:
                names.append(name)
                combo.addItem(name)

        selected = load_auto_fetch_preset_name()
        if selected and selected in names:
            combo.setCurrentIndex(names.index(selected) + 1)
        else:
            combo.setCurrentIndex(0)
            if selected:
                save_auto_fetch_preset_name('')
        combo.blockSignals(False)

    def _on_auto_leap_preset_changed(self, _idx: int) -> None:
        """Persist the auto-fetch preset selection."""
        text = self.auto_leap_preset_combo.currentText()
        save_auto_fetch_preset_name(
            '' if text == self._AUTO_LEAP_PRESET_NONE else text)

    def _save_prefs(self) -> None:
        """Save self._prefs to disk, preserving keys written by other code.

        Dialog done() methods and other components save directly to disk
        (e.g. dialog_geometry, font sizes, include_completed states).
        Before writing self._prefs, merge all disk-only keys so those
        saves are not overwritten.
        """
        disk_prefs = load_monitor_prefs()
        # Preserve any keys that exist on disk but not in self._prefs
        # (written by dialogs, zoom mixin, etc.)
        for key, value in disk_prefs.items():
            if key not in self._prefs:
                self._prefs[key] = value
        # Always take the latest dialog_geometry from disk
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
        self._theme_base_qss = f"""
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

            /* --- Ghost buttons (toolbar: Settings, Notes, Presets, Reset) --- */
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
        """
        self._reapply_theme_stylesheet()

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

        # Re-apply main-window font zoom (theme change replaces our overlay)
        self._apply_main_font_size()

    # ------------------------------------------------------------------
    #  Main-window font zoom (Cmd+scroll / Cmd+±/0)
    # ------------------------------------------------------------------

    _MAIN_FONT_MIN = 9
    _MAIN_FONT_MAX = 28

    def _zoomed_size(self, offset: int = 0) -> int:
        """Return zoomed font size with *offset* applied (clamped to >=8px)."""
        return max(8, self._main_font_size + offset)

    def _zoomed_btn_w(self, base_w: int) -> int:
        """Return a scaled cell-button width.

        Cell buttons use ``setFixedSize(W, sizeHint().height())`` where the
        height already scales with font (via sizeHint), but the width is a
        hard-coded literal.  This helper scales that literal so the button
        stays roughly square at all zoom levels.
        """
        base = current_theme().font_size_base
        scale = max(0.5, self._main_font_size / base)
        return max(base_w - 4, int(base_w * scale))

    def _reapply_theme_stylesheet(self) -> None:
        """Re-apply the app QSS, appending popup and tooltip zoom rules.

        Called from ``_apply_theme``, from ``PopupZoomManager`` when the
        user adjusts popup font size, and whenever the active window's
        zoom size changes (via ``set_tooltip_font_size``).  The appended
        rules stay last so they win specificity ties.

        The tooltip rule is **required**: the theme's ``* { font-size:
        13px }`` would otherwise override any ``QToolTip.setFont()`` we
        do on window activation (universal selector + widget stylesheet
        beats setFont via Qt's cascade).
        """
        app = QApplication.instance()
        if app is None:
            return
        base = getattr(self, '_theme_base_qss', '')
        rule = PopupZoomManager.instance().popup_stylesheet_rule()
        tooltip_pt = getattr(self, '_tooltip_font_size',
                             self._main_font_size)
        tooltip_rule = (f'\n/* tooltip zoom */\n'
                        f'QToolTip {{ font-size: {tooltip_pt}pt; }}\n')
        app.setStyleSheet(base + rule + tooltip_rule)

    def set_tooltip_font_size(self, pt: int) -> None:
        """Update the global tooltip font size and re-apply the app QSS.

        Called by ``MonitorWindow.changeEvent`` (main window activate),
        ``ZoomMixin._zoom_apply_tooltip_font`` (dialog activate / zoom),
        and Notes' activation/buttons-zoom handlers so tooltips track
        the currently-active window's size.
        """
        if getattr(self, '_tooltip_font_size', None) == pt:
            return
        self._tooltip_font_size = pt
        self._reapply_theme_stylesheet()

    def _apply_main_font_size(self) -> None:
        """Apply the current main-window font size as a stylesheet overlay.

        Scales button padding + min-height proportionally so buttons
        grow/shrink with text, and updates toolbar icon sizes so glyphs
        stay proportional to surrounding text.
        """
        size = self._main_font_size
        base = current_theme().font_size_base
        scale = max(0.5, size / base)

        # Scaled button metrics (match theme's defaults at scale=1.0:
        # padding 5 16, min-height 18, combo 5 10, lineedit 6 10).
        btn_py = max(3, int(5 * scale))
        btn_px = max(8, int(16 * scale))
        btn_min_h = max(12, int(18 * scale))
        combo_py = max(3, int(5 * scale))
        combo_px = max(6, int(10 * scale))
        line_py = max(3, int(6 * scale))
        line_px = max(6, int(10 * scale))

        # Overlay stylesheet on the main window — cascades to all children.
        self.setStyleSheet(
            f'QWidget, QLabel, QPushButton, QComboBox, QLineEdit, QCheckBox,'
            f' QTableWidget, QTableView, QHeaderView, QMenu, QMenuBar,'
            f' QStatusBar, QTabWidget, QToolButton, QTextEdit, QListView,'
            f' QListWidget {{ font-size: {size}px; }}'
            f'\nQPushButton {{ padding: {btn_py}px {btn_px}px;'
            f' min-height: {btn_min_h}px; }}'
            f'\nQComboBox {{ padding: {combo_py}px {combo_px}px; }}'
            f'\nQLineEdit {{ padding: {line_py}px {line_px}px; }}'
        )
        # Scale table row height proportionally to font size
        if hasattr(self, 'table') and self.table is not None:
            self.table.verticalHeader().setDefaultSectionSize(int(36 * scale))

        # Scale toolbar icons so they don't look tiny next to larger text
        icon_px = max(12, int(16 * scale))
        if getattr(self, '_notes_btn', None) is not None:
            ni = notes_icon(size=icon_px)
            if ni is not None:
                self._notes_btn.setIcon(ni)
                self._notes_btn.setIconSize(QSize(icon_px, icon_px))
        if getattr(self, '_add_btn', None) is not None:
            t = current_theme()
            self._add_btn.setIcon(
                self._make_plus_icon(t.accent_blue.encode(), size=icon_px))
            self._add_btn.setIconSize(QSize(icon_px, icon_px))

        # Scale the LEAP text logo proportionally.  The logo container's
        # fixed height is bumped in step so the pixmap isn't clipped.
        if getattr(self, '_logo_label', None) is not None:
            self._update_logo_pixmap()
        if getattr(self, '_logo_container', None) is not None:
            self._logo_container.setFixedHeight(max(50, int(50 * scale)))

        # Scale hover tooltips to match the main-window font.  Dialogs
        # override this via _zoom_apply_tooltip_font when they activate.
        if self.isActiveWindow() or not QApplication.activeWindow():
            self.set_tooltip_font_size(self._main_font_size)

    def _zoom_main_delta(self, delta: int) -> None:
        """Change main font size by *delta* and persist (debounced)."""
        new_size = max(self._MAIN_FONT_MIN,
                       min(self._MAIN_FONT_MAX, self._main_font_size + delta))
        if new_size == self._main_font_size:
            return
        self._main_font_size = new_size
        self._apply_main_font_size()
        self._rebuild_table_for_zoom()
        self._schedule_main_font_save()

    def _zoom_main_reset(self) -> None:
        """Reset main font size to theme default."""
        default = current_theme().font_size_base
        if self._main_font_size == default:
            return
        if (self._main_zoom_save_timer is not None
                and self._main_zoom_save_timer.isActive()):
            self._main_zoom_save_timer.stop()
        self._main_font_size = default
        self._apply_main_font_size()
        self._rebuild_table_for_zoom()
        self._prefs.pop('main_font_size', None)
        self._save_prefs()

    def _rebuild_table_for_zoom(self) -> None:
        """Clear cached cell widgets and rebuild so their inline fonts
        pick up the new zoom level.  Table cells use setFont/setPointSize
        directly (see table_builder_mixin), which a parent stylesheet
        cannot override — a rebuild is the only way to re-apply."""
        if hasattr(self, '_cell_cache'):
            self._cell_cache.clear()
        if hasattr(self, '_update_table'):
            self._update_table()

    def _schedule_main_font_save(self) -> None:
        """Debounce writes to disk while the user rapidly scrolls."""
        if self._main_zoom_save_timer is None:
            self._main_zoom_save_timer = QTimer(self)
            self._main_zoom_save_timer.setSingleShot(True)
            self._main_zoom_save_timer.timeout.connect(self._save_main_font_size)
        self._main_zoom_save_timer.start(300)

    def _save_main_font_size(self) -> None:
        """Persist main font size to monitor prefs."""
        self._prefs['main_font_size'] = self._main_font_size
        self._save_prefs()

    def _main_zoom_owns_widget(self, widget) -> bool:
        """Check if *widget* belongs to the main window (not a dialog/popup)."""
        if widget is None:
            return False
        w = widget
        while w is not None:
            if isinstance(w, QDialog):
                return False
            if isinstance(w, QMenu):
                return False  # let PopupZoomManager handle QMenu zoom
            if w is self:
                return True
            w = w.parent()
        return False

    # ------------------------------------------------------------------
    #  Global keyboard shortcut
    # ------------------------------------------------------------------

    def _register_global_shortcut(self) -> None:
        """Register (or re-register) the global focus shortcut from prefs."""
        self._unregister_global_shortcut()

        shortcut_str = self._prefs.get('global_shortcut', '')
        if not shortcut_str:
            return

        seq = QKeySequence(shortcut_str)
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
        if self._global_event_monitor is not None:
            NSEvent.removeMonitor_(self._global_event_monitor)
            self._global_event_monitor = None
        if self._local_event_monitor is not None:
            NSEvent.removeMonitor_(self._local_event_monitor)
            self._local_event_monitor = None

    def _on_global_shortcut_triggered(self) -> None:
        """Bring the monitor window to the foreground."""
        try:
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            pass
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    # ── Notes shortcut ──────────────────────────────────────────────

    @staticmethod
    def _parse_shortcut_ns(shortcut_str: str) -> Optional[tuple[int, int]]:
        """Convert a Qt shortcut string to (macOS keycode, NSEvent mod flags).

        Returns ``None`` if the shortcut cannot be mapped.
        """
        seq = QKeySequence(shortcut_str)
        if seq.isEmpty():
            return None

        combined = seq[0]
        qt_mods = int(combined) & 0xFE000000
        qt_key = int(combined) & 0x01FFFFFF

        ns_flags = 0
        if qt_mods & 0x04000000:  # Qt.ControlModifier → Cmd
            ns_flags |= 1 << 20
        if qt_mods & 0x10000000:  # Qt.MetaModifier → Ctrl
            ns_flags |= 1 << 18
        if qt_mods & 0x08000000:  # Qt.AltModifier → Option
            ns_flags |= 1 << 19
        if qt_mods & 0x02000000:  # Qt.ShiftModifier
            ns_flags |= 1 << 17

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
            return None
        return _CHAR_TO_KEYCODE[char], ns_flags

    def _register_notes_shortcut(self) -> None:
        """Register NSEvent monitors for the notes shortcuts from prefs."""
        self._unregister_notes_shortcut()

        _MOD_MASK = (1 << 17) | (1 << 18) | (1 << 19) | (1 << 20)

        # Focused shortcut (local monitor only)
        focused_str = self._prefs.get('notes_shortcut_focused', '')
        if focused_str:
            parsed = self._parse_shortcut_ns(focused_str)
            if parsed:
                kc, flags = parsed

                def _focused_handler(event: object) -> object:
                    try:
                        if (event.keyCode() == kc
                                and event.modifierFlags() & _MOD_MASK == flags):
                            QTimer.singleShot(0, self._on_notes_shortcut_focused)
                    except Exception:
                        pass
                    return event

                self._notes_focused_monitor = (
                    NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                        NSKeyDownMask, _focused_handler))

        # Global shortcut (global monitor only)
        global_str = self._prefs.get('notes_shortcut_global', '')
        if global_str:
            parsed = self._parse_shortcut_ns(global_str)
            if parsed:
                kc, flags = parsed

                def _global_handler(event: object) -> object:
                    try:
                        if (event.keyCode() == kc
                                and event.modifierFlags() & _MOD_MASK == flags):
                            QTimer.singleShot(0, self._on_notes_shortcut_global)
                    except Exception:
                        pass
                    return event

                self._notes_global_monitor = (
                    NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                        NSKeyDownMask, _global_handler))

    def _unregister_notes_shortcut(self) -> None:
        """Remove any active notes NSEvent monitors."""
        if self._notes_focused_monitor is not None:
            NSEvent.removeMonitor_(self._notes_focused_monitor)
            self._notes_focused_monitor = None
        if self._notes_global_monitor is not None:
            NSEvent.removeMonitor_(self._notes_global_monitor)
            self._notes_global_monitor = None

    def _on_notes_shortcut_focused(self) -> None:
        """Open notes when the Leap window is focused."""
        self._open_notes()

    def _on_notes_shortcut_global(self) -> None:
        """Bring Leap to front and open notes."""
        try:
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            pass
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        self._open_notes()

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
        # Reject all open child dialogs so they save state (via done())
        # before os._exit. reject() triggers done(Rejected) which runs
        # each dialog's save logic. This covers all dialogs generically.
        for dlg in self.findChildren(QDialog):
            if dlg.isVisible():
                dlg.reject()

        # Prevent timers and signal handlers from firing during shutdown
        self._shutting_down = True
        self.timer.stop()
        self._scm_poll_timer.stop()
        self._hover_timer.stop()
        self._unregister_global_shortcut()
        self._unregister_notes_shortcut()
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


def _request_notification_permission() -> None:
    """Request macOS notification permission and exit.

    Called when the app is launched with ``--request-permissions``.
    Registers the bundle with the notification system (so it appears
    in System Settings > Notifications) and presents the native
    "Leap Monitor would like to send you notifications" dialog.

    On macOS 14+ ``UNUserNotificationCenter`` silently declines to
    present the dialog unless a real ``NSApplication`` with
    ``NSApplicationActivationPolicyRegular`` is running, so we set
    that up first.  If authorization still isn't granted (user
    dismissed, macOS suppressed, etc.) we open the System Settings
    Notifications pane as a fallback.

    Idempotency: the bundle is registered in
    ``~/Library/Preferences/com.apple.ncprefs.plist`` the first time
    ``requestAuthorizationWithOptions`` is invoked, regardless of the
    user's answer — ``make install-monitor`` then reads that plist
    and skips this whole prompt on subsequent runs.
    """
    try:
        # Make this process a real app so the UN framework will
        # present the authorization dialog.
        app = NSApplication.sharedApplication()
        try:
            # 0 = NSApplicationActivationPolicyRegular (dock icon, UI).
            app.setActivationPolicy_(0)
            app.activateIgnoringOtherApps_(True)
        except Exception:
            pass

        objc.loadBundle(
            'UserNotifications', globals(),
            '/System/Library/Frameworks/UserNotifications.framework',
        )
        UNUserNotificationCenter = objc.lookUpClass('UNUserNotificationCenter')

        objc.registerMetaDataForSelector(
            b'UNUserNotificationCenter',
            b'requestAuthorizationWithOptions:completionHandler:',
            {'arguments': {3: {'callable': {
                'retval': {'type': b'v'},
                'arguments': {0: {'type': b'^v'}, 1: {'type': b'Z'}, 2: {'type': b'@'}},
            }}}},
        )

        center = UNUserNotificationCenter.currentNotificationCenter()
        done = [False]
        granted = [False]

        def _on_auth(ok: bool, error: object) -> None:
            granted[0] = bool(ok)
            done[0] = True

        center.requestAuthorizationWithOptions_completionHandler_(
            (1 << 0) | (1 << 1) | (1 << 2),  # badge | sound | alert
            _on_auth,
        )

        # Spin the run loop so the completion handler fires and the
        # system dialog can be presented / dismissed.
        timeout = 30.0  # seconds — generous limit
        while not done[0] and timeout > 0:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.25))
            timeout -= 0.25

        # User dismissed / macOS suppressed the dialog / we timed
        # out.  The bundle is already registered with the
        # notification system by the request above, so point the
        # user at the Settings toggle they can flip by hand.
        if not granted[0]:
            subprocess.run(
                ['open',
                 'x-apple.systempreferences:com.apple.Notifications-Settings.extension'],
                check=False,
            )
    except Exception as exc:
        print(f"  Note: Could not request notification permission ({exc})")
        print("  You can grant it later in System Settings > Notifications > Leap Monitor")

    sys.exit(0)


def main() -> None:
    """Main entry point for Leap Monitor."""
    # Handle --request-permissions early, before any GUI setup.
    if '--request-permissions' in sys.argv:
        _request_notification_permission()

    faulthandler.enable()
    load_shell_env()
    app = TooltipApp(sys.argv)
    app.setApplicationName('Leap Monitor')
    app.setStyle(PersistentTooltipStyle(app.style()))

    # Load saved theme before creating the window
    prefs = load_monitor_prefs()
    saved_theme = prefs.get('theme', 'Nord')
    set_theme(saved_theme)

    # Set macOS appearance based on theme (dark/light)
    t = current_theme()
    try:
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
        window.close()

    signal.signal(signal.SIGINT, signal_handler)

    # Dock Quit works when no modal dialog is blocking (e.g. Notes).
    # Modal dialogs (Settings, etc.) block macOS quit — a Qt/macOS
    # limitation for non-bundled Python apps.
    app.aboutToQuit.connect(window.close)

    # Timer trick — force periodic bytecode execution so Python
    # processes pending signals while Qt's C++ event loop runs.
    timer = QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
