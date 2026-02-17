"""
ClaudeQ Monitor GUI application.

PyQt5-based GUI for viewing and managing active ClaudeQ sessions.
"""

import logging
import os
import signal
import sys
import time
from typing import Any, Optional

from PyQt5.QtWidgets import (
    QApplication, QComboBox, QGridLayout, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QPushButton,
    QCheckBox, QHeaderView, QMessageBox, QProgressBar,
)
from PyQt5.QtCore import QEvent, QTimer, Qt
from PyQt5.QtGui import QCursor, QIcon, QCloseEvent, QResizeEvent

from claudeq.monitor.mr_tracking.base import MRStatus, SCMProvider
from claudeq.monitor.mr_tracking.config import (
    load_monitor_prefs, load_notification_seen, load_pinned_sessions,
    save_monitor_prefs,
)
from claudeq.monitor.scm_polling import (
    CollectThreadsWorker, SCMOneShotWorker, SCMPollerWorker,
    SendThreadsCombinedWorker, SendThreadsWorker, SessionRefreshWorker,
)
from claudeq.monitor.session_manager import get_active_sessions
from claudeq.monitor.monitor_utils import find_icon, load_shell_env
from claudeq.monitor.server_launcher import ServerLauncher
from claudeq.monitor.ui.dock_badge import DockBadge
from claudeq.monitor.ui.status_log import StatusLog, StatusLogDialog
from claudeq.monitor.ui.table_helpers import (
    MR_TEMPLATE_LABEL, MR_TEMPLATE_TOOLTIP,
    QUICK_MSG_TEMPLATE_LABEL, QUICK_MSG_TEMPLATE_TOOLTIP,
    PersistentTooltipStyle, SeparatorDelegate, SeparatorHeaderView, TooltipApp,
)
from claudeq.monitor.ui.ui_widgets import PulsingLabel, IndicatorLabel

from claudeq.monitor._mixins.scm_config_mixin import SCMConfigMixin
from claudeq.monitor._mixins.session_mixin import SessionMixin
from claudeq.monitor._mixins.mr_tracking_mixin import MRTrackingMixin
from claudeq.monitor._mixins.mr_display_mixin import MRDisplayMixin
from claudeq.monitor._mixins.notifications_mixin import NotificationsMixin
from claudeq.monitor._mixins.table_builder_mixin import TableBuilderMixin

logger = logging.getLogger(__name__)


class MonitorWindow(
    SCMConfigMixin,
    SessionMixin,
    MRTrackingMixin,
    MRDisplayMixin,
    NotificationsMixin,
    TableBuilderMixin,
    QMainWindow,
):
    """Main window for ClaudeQ Monitor."""

    # Column indices
    COL_DELETE = 0
    COL_TAG = 1
    COL_PROJECT = 2
    COL_SERVER = 3
    COL_SERVER_BRANCH = 4
    COL_STATUS = 5
    COL_QUEUE = 6
    COL_CLIENT = 7
    COL_MR = 8
    COL_MR_BRANCH = 9

    def __init__(self) -> None:
        """Initialize the monitor window."""
        super().__init__()
        self.sessions: list[dict] = []
        self._mr_statuses: dict[str, MRStatus] = {}
        self._mr_widgets: dict[str, PulsingLabel] = {}
        self._mr_approval_widgets: dict[str, IndicatorLabel] = {}
        self._cell_cache: dict[tuple[str, str], tuple[tuple, QWidget]] = {}
        self._scm_providers: dict[str, SCMProvider] = {}  # SCMType.value -> provider
        self._scm_worker: Optional[SCMPollerWorker] = None
        self._scm_oneshot_worker: Optional[SCMOneShotWorker] = None
        self._collect_threads_worker: Optional[CollectThreadsWorker] = None
        self._send_threads_worker: Optional[SendThreadsWorker] = None
        self._send_combined_worker: Optional[SendThreadsCombinedWorker] = None
        self._cq_only_collect: bool = False
        self._refresh_worker: Optional[SessionRefreshWorker] = None
        self._scm_polling = False
        self._scm_poll_started_at: float = 0.0
        self._shutting_down = False
        self._dock_badge = DockBadge()
        self._tracked_tags: set[str] = set()
        self._checking_tags: set[str] = set()
        self._prefs = load_monitor_prefs()
        self._pinned_sessions: dict[str, dict[str, Any]] = load_pinned_sessions()
        self._deleted_tags: set[str] = set()  # suppress re-pin after explicit delete
        self._starting_tags: set[str] = set()  # guard against double-click server start
        self._ui_ready = False  # suppress resizeEvent during init
        self._hovered_row: int = -1
        self._pending_tracking_context: dict[str, dict[str, Any]] = {}
        self._silent_tracking_tags: set[str] = set()  # suppress popups for auto-reconnect
        self._status_log = StatusLog()
        self._server_launcher = ServerLauncher(self)

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
        self._auto_track_mr_pinned()
        self._maybe_start_notification_poll()

        # Always start auto-refresh
        self.timer.start(1000)

    def _init_ui(self) -> None:
        """Initialize the user interface."""
        self.setWindowTitle('ClaudeQ Monitor')

        # Restore saved window geometry or center on screen
        saved_geom = self._prefs.get('window_geometry')
        if saved_geom and len(saved_geom) == 4:
            # Validate the saved position is on a visible screen
            from PyQt5.QtCore import QPoint
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
        main_widget.setLayout(layout)

        # Table
        self.table = QTableWidget()
        self.table.setHorizontalHeader(SeparatorHeaderView(Qt.Horizontal, self.table))
        self.table.setItemDelegate(SeparatorDelegate(self.table))
        self.table.setShowGrid(False)
        self.table.setColumnCount(10)
        self.table.setHorizontalHeaderLabels([
            '', 'Tag', 'Project', 'Server', 'Server Branch', 'Status', 'Queue',
            'Client', 'MR', 'MR Branch',
        ])

        # Column header tooltips
        _col_tooltips = {
            self.COL_TAG: 'CQ session name',
            self.COL_PROJECT: 'Project directory name',
            self.COL_SERVER: 'CQ server process (green = running)',
            self.COL_SERVER_BRANCH: 'The git branch the server is running on',
            self.COL_STATUS: 'Whether Claude is busy processing or idle',
            self.COL_QUEUE: 'Number of messages waiting in the queue',
            self.COL_CLIENT: 'CQ client process (green = connected)',
            self.COL_MR: 'Merge/pull request tracking status',
            self.COL_MR_BRANCH: 'MR/PR source branch',
        }
        for col, tip in _col_tooltips.items():
            item = self.table.horizontalHeaderItem(col)
            if item:
                item.setToolTip(tip)

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

        # Last column: Fixed prevents draggable right-edge handle;
        # programmatic setColumnWidth() still works in resizeEvent.
        header.setSectionResizeMode(self.COL_MR_BRANCH, QHeaderView.Fixed)

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

        # Row hover highlight — poll cursor position to track hovered row
        self.table.setProperty('_hovered_row', -1)
        self._hover_timer = QTimer()
        self._hover_timer.timeout.connect(self._check_row_hover)
        self._hover_timer.start(50)

        # Top controls
        top_layout = QHBoxLayout()

        settings_btn = QPushButton('\u2699  Settings')
        settings_btn.setToolTip('Monitor settings')
        settings_btn.clicked.connect(self._open_settings)
        top_layout.addWidget(settings_btn)

        top_layout.addStretch()

        edit_template_btn = QPushButton('\u270e  Templates')
        edit_template_btn.setToolTip('Edit template presets')
        edit_template_btn.clicked.connect(self._open_template_editor)
        top_layout.addWidget(edit_template_btn)

        template_grid = QGridLayout()
        template_grid.setSpacing(4)

        tpl_label = QLabel(MR_TEMPLATE_LABEL)
        template_grid.addWidget(tpl_label, 0, 0)

        self.template_combo = QComboBox()
        self.template_combo.setObjectName('template_combo')
        self.template_combo.setMinimumWidth(180)
        self.template_combo.setMaximumWidth(300)
        self.template_combo.setToolTip(MR_TEMPLATE_TOOLTIP)
        self._populate_template_combo()
        self.template_combo.currentIndexChanged.connect(
            self._on_template_combo_changed)
        template_grid.addWidget(self.template_combo, 0, 1)

        direct_label = QLabel(QUICK_MSG_TEMPLATE_LABEL)
        template_grid.addWidget(direct_label, 1, 0)

        self.direct_template_combo = QComboBox()
        self.direct_template_combo.setObjectName('direct_template_combo')
        self.direct_template_combo.setMinimumWidth(180)
        self.direct_template_combo.setMaximumWidth(300)
        self.direct_template_combo.setToolTip(QUICK_MSG_TEMPLATE_TOOLTIP)
        self._populate_direct_template_combo()
        self.direct_template_combo.currentIndexChanged.connect(
            self._on_direct_template_combo_changed)
        template_grid.addWidget(self.direct_template_combo, 1, 1)

        top_layout.addLayout(template_grid)

        top_layout.addStretch()
        reset_cols_btn = QPushButton('Reset Window Size')
        reset_cols_btn.setToolTip('Reset window and column sizes to defaults')
        reset_cols_btn.clicked.connect(self._reset_window_size)
        top_layout.addWidget(reset_cols_btn)
        layout.addLayout(top_layout)

        add_row_layout = QHBoxLayout()
        add_btn = QPushButton('+')
        add_btn.setFixedWidth(30)
        add_btn.setToolTip('Add session from MR/PR URL')
        add_btn.clicked.connect(self._add_row)
        add_row_layout.addWidget(add_btn)
        add_row_layout.addStretch()
        layout.addLayout(add_row_layout)

        layout.addWidget(self.table)

        # Bottom controls
        bottom_layout = QHBoxLayout()

        self.bots_check = QCheckBox('Include git bots')
        self.bots_check.setToolTip('Count bot comments as responses in MR thread detection')
        self.bots_check.setChecked(self._prefs.get('include_bots', False))
        self.bots_check.stateChanged.connect(self._toggle_include_bots)
        bottom_layout.addWidget(self.bots_check)

        self.auto_cq_check = QCheckBox("Auto '/cq' fetch")
        self.auto_cq_check.setToolTip(
            'Automatically send /cq-tagged MR threads to CQ sessions each poll cycle'
        )
        self.auto_cq_check.setChecked(self._prefs.get('auto_fetch_cq', True))
        self.auto_cq_check.stateChanged.connect(self._toggle_auto_fetch_cq)
        bottom_layout.addWidget(self.auto_cq_check)

        bottom_layout.addStretch()

        # SCM connect buttons
        self.gitlab_btn = QPushButton('Connect GitLab')
        self.gitlab_btn.setToolTip('Configure GitLab connection for MR tracking')
        self.gitlab_btn.clicked.connect(self._open_gitlab_setup)
        bottom_layout.addWidget(self.gitlab_btn)

        self.github_btn = QPushButton('Connect GitHub')
        self.github_btn.setToolTip('Configure GitHub connection for PR tracking')
        self.github_btn.clicked.connect(self._open_github_setup)
        bottom_layout.addWidget(self.github_btn)

        layout.addLayout(bottom_layout)

        # Status / log bar at the very bottom
        status_layout = QHBoxLayout()

        full_log_btn = QPushButton('Logs')
        full_log_btn.setToolTip('View full status message history')
        full_log_btn.clicked.connect(self._open_status_log)
        status_layout.addWidget(full_log_btn)

        self._log_label = QLabel('')
        self._log_label.setStyleSheet('color: gray; font-size: 11px;')
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

    def _open_status_log(self) -> None:
        """Open the status log dialog."""
        dialog = StatusLogDialog(self._status_log, self)
        dialog.exec_()

    def _show_status(self, msg: str, timeout_ms: int = 5000,
                     url: Optional[str] = None) -> None:
        """Log a status message and update the inline log labels."""
        self._status_log.append(msg, url=url)
        self._refresh_log_labels()

    def _refresh_log_labels(self) -> None:
        """Update the inline log label with the most recent entry."""
        entries = self._status_log.entries()
        if entries:
            e = entries[-1]
            ts = time.strftime('%H:%M:%S', time.localtime(e.timestamp))
            if e.url:
                self._log_label.setText(
                    f'[{ts}] {e.message} '
                    f'<a href="{e.url}" style="color: cyan;">(link)</a>'
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
        """Distribute column widths equally across resizable columns."""
        col_count = self.table.columnCount()
        if col_count <= 0:
            return
        viewport_w = self.table.viewport().width()
        if viewport_w <= 0:
            # Viewport not ready yet — estimate from window geometry
            viewport_w = (self.geometry().width() or 1150) - 50
        delete_w = self.table.columnWidth(self.COL_DELETE)
        available = viewport_w - delete_w
        resizable = col_count - 1  # exclude COL_DELETE
        col_width = available // max(resizable, 1)
        for col in range(col_count):
            if col == self.COL_DELETE:
                continue
            self.table.setColumnWidth(col, col_width)

    def _reset_window_size(self) -> None:
        """Reset window geometry and column widths to defaults."""
        self._center_on_screen()
        self._apply_equal_column_widths()

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
        resizable_total = sum(
            self.table.columnWidth(col)
            for col in range(col_count) if col != self.COL_DELETE
        )
        if resizable_total <= 0:
            return
        available = viewport_w - delete_w
        # Cumulative rounding: compute each column's target from cumulative
        # proportions so every column shifts evenly, even for tiny changes.
        cumulative_old = 0
        used = 0
        resizable_cols = [c for c in range(col_count) if c != self.COL_DELETE]
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
        self._clear_dock_badge()

        # Save window geometry and column widths
        try:
            geom = self.geometry()
            self._prefs['window_geometry'] = [geom.x(), geom.y(), geom.width(), geom.height()]
            self._prefs['column_widths'] = [
                self.table.columnWidth(col) for col in range(self.table.columnCount())
            ]
            save_monitor_prefs(self._prefs)
        except Exception:
            logger.debug("Failed to save monitor prefs on close", exc_info=True)

        # Accept the close event, then hard-exit.  os._exit() skips atexit
        # handlers and thread joins — the only reliable way to exit when
        # background threads may be stuck in blocking network calls.
        event.accept()
        os._exit(0)


def main() -> None:
    """Main entry point for ClaudeQ Monitor."""
    load_shell_env()
    app = TooltipApp(sys.argv)
    app.setApplicationName('ClaudeQ Monitor')
    app.setStyle(PersistentTooltipStyle(app.style()))

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
    window.show()

    # Enable proportional column scaling after the window is fully shown
    # and all initial resize events have settled.
    QTimer.singleShot(0, lambda: setattr(window, '_ui_ready', True))

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
