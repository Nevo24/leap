"""
ClaudeQ Monitor GUI application.

PyQt5-based GUI for viewing and managing active ClaudeQ sessions.
"""

import logging
import math
import os
import signal
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from PyQt5 import sip
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QCheckBox, QHeaderView, QMessageBox,
    QInputDialog
)
from PyQt5.QtCore import QTimer, Qt, QThread, pyqtSignal
from PyQt5.QtGui import QIcon, QCursor, QMouseEvent, QCloseEvent

from claudeq.utils.constants import GITLAB_MAX_CONCURRENT_POLLS, GITLAB_POLL_INTERVAL, SOCKET_DIR
from claudeq.utils.socket_utils import send_socket_request
from claudeq.monitor.session_manager import (
    get_active_sessions,
    load_session_metadata,
    session_exists
)
from claudeq.monitor.navigation import close_terminal_with_title, find_terminal_with_title
from claudeq.monitor.mr_tracking.base import MRState, MRStatus
from claudeq.monitor.mr_tracking.config import load_gitlab_config, load_monitor_prefs, save_monitor_prefs
from claudeq.monitor.mr_tracking.git_utils import get_git_remote_info

logger = logging.getLogger(__name__)


def _find_icon() -> Optional[Path]:
    """Find the app icon, works both from source and .app bundle."""
    # From source: src/claudeq/monitor/app.py → project_root/assets/
    candidate = Path(__file__).parent.parent.parent.parent / "assets" / "claudeq-icon.png"
    if candidate.exists():
        return candidate

    # From .app bundle: walk up to Contents/Resources/
    for parent in Path(__file__).parents:
        if parent.name == 'Resources' and parent.parent.name == 'Contents':
            candidate = parent / "claudeq-icon.png"
            if candidate.exists():
                return candidate
            break

    return None


def focus_session(tag: str, session_type: str = 'server') -> None:
    """
    Focus the terminal with the given session.

    Args:
        tag: Session tag name.
        session_type: 'server' or 'client'.
    """
    metadata = load_session_metadata(tag)

    preferred_ide = metadata.get('ide') if metadata else None
    project_path = metadata.get('project_path') if metadata else None
    title_pattern = f"cq-{session_type} {tag}"

    # Check if session exists
    if not session_exists(tag, session_type):
        other_type = 'server' if session_type == 'client' else 'client'
        reply = QMessageBox.question(
            None,
            f'{session_type.capitalize()} Not Found',
            f'{session_type.capitalize()} not found for: {tag}\n\n'
            f'Go to {other_type} instead?',
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            focus_session(tag, other_type)
        return

    # Try to find and focus the terminal
    result = find_terminal_with_title(
        title_pattern,
        preferred_ide,
        project_path,
        title_pattern
    )

    if not result:
        QMessageBox.warning(
            None,
            'Navigation Failed',
            f'Could not navigate to {session_type}: {tag}\n\n'
            'Make sure terminal tab titles are configured correctly.'
        )


class PulsingLabel(QLabel):
    """A label that can pulse its text color for attention."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pulsing: bool = False
        self._mr_url: Optional[str] = None
        self._phase: float = 0.0

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(50)
        self._pulse_timer.timeout.connect(self._animate)

        self.setAlignment(Qt.AlignCenter)

    def set_pulsing(self, pulsing: bool) -> None:
        self._pulsing = pulsing
        if pulsing:
            self._phase = 0.0
            self._pulse_timer.start()
        else:
            self._pulse_timer.stop()
            self.setStyleSheet('')

    def set_mr_url(self, url: Optional[str]) -> None:
        self._mr_url = url
        if url:
            self.setCursor(QCursor(Qt.PointingHandCursor))
        else:
            self.setCursor(QCursor(Qt.ArrowCursor))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._mr_url and event.button() == Qt.LeftButton:
            webbrowser.open(self._mr_url)
        else:
            super().mousePressEvent(event)

    def _animate(self) -> None:
        try:
            self._phase += 0.05
            # Oscillate opacity between 0.3 and 1.0
            opacity = 0.65 + 0.35 * math.sin(self._phase)
            r, g, b = 230, 150, 0  # orange
            self.setStyleSheet(f'color: rgba({r}, {g}, {b}, {opacity:.2f}); font-weight: bold;')
        except Exception:
            # Silently stop pulsing if animation fails
            self._pulse_timer.stop()


class GitLabPollerWorker(QThread):
    """Background worker that polls GitLab for MR statuses."""

    results_ready = pyqtSignal(dict)
    cq_commands_ready = pyqtSignal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._provider: Optional['SCMProvider'] = None
        self._sessions: list[dict[str, Any]] = []

    def configure(self, provider: 'SCMProvider', sessions: list[dict[str, Any]]) -> None:
        self._provider = provider
        self._sessions = list(sessions)

    def run(self) -> None:
        if not self._provider:
            return

        results: dict[str, MRStatus] = {}
        all_cq_commands: list[Any] = []

        # Poll sessions in parallel — each session's API calls are independent.
        with ThreadPoolExecutor(max_workers=GITLAB_MAX_CONCURRENT_POLLS) as pool:
            futures = {
                pool.submit(self._poll_session, session): session['tag']
                for session in self._sessions
            }
            for future in as_completed(futures):
                tag = futures[future]
                try:
                    status, cq_commands = future.result()
                    results[tag] = status
                    all_cq_commands.extend(cq_commands)
                except Exception:
                    logger.debug("Error polling session %s", tag, exc_info=True)
                    results[tag] = MRStatus(state=MRState.NO_MR)

        self.results_ready.emit(results)
        if all_cq_commands:
            self.cq_commands_ready.emit(all_cq_commands)

    def _poll_session(self, session: dict[str, Any]) -> tuple[MRStatus, list[Any]]:
        """Poll a single session for MR status and /cq commands."""
        project_path = session.get('project_path')
        if not project_path:
            return MRStatus(state=MRState.NO_MR), []

        remote_info = get_git_remote_info(project_path)
        if not remote_info:
            return MRStatus(state=MRState.NO_MR), []

        try:
            status = self._provider.get_mr_status(
                remote_info.project_path, remote_info.branch
            )
        except Exception:
            logger.debug("Error polling MR for tag %s", session['tag'], exc_info=True)
            status = MRStatus(state=MRState.NO_MR)

        cq_commands: list[Any] = []
        try:
            cq_commands = self._provider.scan_cq_commands(
                remote_info.project_path, remote_info.branch
            )
        except Exception:
            logger.debug("Error scanning /cq for tag %s", session['tag'], exc_info=True)

        return status, cq_commands


class MonitorWindow(QMainWindow):
    """Main window for ClaudeQ Monitor."""

    # Column indices
    COL_TAG = 0
    COL_PROJECT = 1
    COL_BRANCH = 2
    COL_MR = 3
    COL_STATUS = 4
    COL_QUEUE = 5
    COL_SERVER = 6
    COL_CLIENT = 7

    def __init__(self) -> None:
        """Initialize the monitor window."""
        super().__init__()
        self.sessions: list[dict] = []
        self._mr_statuses: dict[str, MRStatus] = {}
        self._mr_widgets: dict[str, PulsingLabel] = {}
        self._gitlab_provider = None
        self._gitlab_worker: Optional[GitLabPollerWorker] = None
        self._polling_in_progress = False
        self._shutting_down = False
        self._tracked_tags: set[str] = set()
        self._prefs = load_monitor_prefs()

        # Setup auto-refresh timer before init_ui
        self.timer = QTimer()
        self.timer.timeout.connect(self._auto_refresh)

        # GitLab poll timer (separate from session refresh)
        self._gitlab_timer = QTimer()
        self._gitlab_timer.timeout.connect(self._start_gitlab_poll)

        self._init_ui()
        self._refresh_data()
        self._init_gitlab_provider()

        # Always start auto-refresh
        self.timer.start(1000)

    def _init_ui(self) -> None:
        """Initialize the user interface."""
        self.setWindowTitle('ClaudeQ Monitor')

        # Restore saved window geometry or center on screen
        saved_geom = self._prefs.get('window_geometry')
        if saved_geom and len(saved_geom) == 4:
            self.setGeometry(*saved_geom)
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
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            'Tag', 'Project', 'Branch', 'MR', 'Status', 'Queue', 'Server', 'Client'
        ])

        # Enable interactive column resizing
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)

        # Restore saved column widths or distribute equally
        col_count = self.table.columnCount()
        saved_widths = self._prefs.get('column_widths')
        if saved_widths and len(saved_widths) == col_count:
            for col, width in enumerate(saved_widths):
                self.table.setColumnWidth(col, width)
        else:
            self._apply_equal_column_widths()

        self.table.setSelectionMode(QTableWidget.NoSelection)

        # Top controls
        top_layout = QHBoxLayout()
        top_layout.addStretch()
        reset_cols_btn = QPushButton('Reset Window Size')
        reset_cols_btn.clicked.connect(self._reset_window_size)
        top_layout.addWidget(reset_cols_btn)
        layout.addLayout(top_layout)

        layout.addWidget(self.table)

        # Bottom controls
        bottom_layout = QHBoxLayout()

        self.refresh_btn = QPushButton('Refresh')
        self.refresh_btn.clicked.connect(self._refresh_data)
        bottom_layout.addWidget(self.refresh_btn)

        self.bots_check = QCheckBox('Include git bots')
        self.bots_check.setChecked(self._prefs.get('include_bots', False))
        self.bots_check.stateChanged.connect(self._toggle_include_bots)
        bottom_layout.addWidget(self.bots_check)

        bottom_layout.addStretch()

        # GitLab connect button
        self.gitlab_btn = QPushButton('Connect GitLab')
        self.gitlab_btn.clicked.connect(self._open_gitlab_setup)
        bottom_layout.addWidget(self.gitlab_btn)

        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.close)
        bottom_layout.addWidget(close_btn)

        layout.addLayout(bottom_layout)

    def _set_window_icon(self) -> None:
        """Set the window icon."""
        icon_path = _find_icon()
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
        """Distribute column widths equally across all columns."""
        col_count = self.table.columnCount()
        if col_count <= 0:
            return
        win_width = self.geometry().width() or 1150
        # Subtract row-header width (~30px) and vertical scrollbar (~20px)
        available = win_width - 50
        col_width = available // col_count
        for col in range(col_count):
            self.table.setColumnWidth(col, col_width)

    def _reset_window_size(self) -> None:
        """Reset window geometry and column widths to defaults."""
        self._center_on_screen()
        self._apply_equal_column_widths()

    def _init_gitlab_provider(self) -> None:
        """Load GitLab config and create provider if configured."""
        config = load_gitlab_config()
        if not config or 'private_token' not in config or 'username' not in config:
            self._gitlab_provider = None
            self._update_gitlab_button()
            return

        try:
            from claudeq.monitor.mr_tracking.gitlab_provider import GitLabProvider
            self._gitlab_provider = GitLabProvider(
                gitlab_url=config.get('gitlab_url', 'https://gitlab.com'),
                private_token=config['private_token'],
                username=config['username'],
                filter_bots=not self._prefs.get('include_bots', False),
            )
            self._update_gitlab_button()
            # Don't start polling — tracking is opt-in per session.
            # The poll timer starts when the user clicks "Track MR".
        except Exception:
            logger.debug("Failed to init GitLab provider", exc_info=True)
            self._gitlab_provider = None
            self._update_gitlab_button()

    def _update_gitlab_button(self) -> None:
        """Update the GitLab button text/style based on connection state."""
        if self._gitlab_provider:
            self.gitlab_btn.setText('GitLab Connected')
            self.gitlab_btn.setStyleSheet('color: green;')
        else:
            self.gitlab_btn.setText('Connect GitLab')
            self.gitlab_btn.setStyleSheet('')

    def _open_gitlab_setup(self) -> None:
        """Open the GitLab setup dialog."""
        from claudeq.monitor.gitlab_setup_dialog import GitLabSetupDialog
        dialog = GitLabSetupDialog(self)
        if dialog.exec_():
            # Re-initialize provider after successful save — reset tracking
            self._gitlab_timer.stop()
            self._gitlab_provider = None
            self._mr_statuses.clear()
            self._tracked_tags.clear()
            self._init_gitlab_provider()

    def _start_gitlab_poll(self) -> None:
        """Start a background GitLab poll for tracked sessions only."""
        if self._shutting_down:
            return
        if not self._gitlab_provider or self._polling_in_progress:
            return
        if not self._tracked_tags:
            return

        tracked_sessions = [s for s in self.sessions if s['tag'] in self._tracked_tags]
        if not tracked_sessions:
            return

        self._polling_in_progress = True
        worker = GitLabPollerWorker(self)
        worker.configure(self._gitlab_provider, tracked_sessions)
        worker.results_ready.connect(self._on_gitlab_results)
        worker.cq_commands_ready.connect(self._on_cq_commands)
        worker.finished.connect(self._on_gitlab_worker_finished)
        self._gitlab_worker = worker
        worker.start()

    def _on_gitlab_worker_finished(self) -> None:
        """Clean up after poller worker completes."""
        self._polling_in_progress = False
        if self._gitlab_worker:
            self._gitlab_worker.deleteLater()
            self._gitlab_worker = None

    def _on_gitlab_results(self, results: dict[str, MRStatus]) -> None:
        """Handle GitLab poll results (runs in main thread via signal)."""
        if self._shutting_down:
            return
        try:
            if not self.isVisible():
                return
            self._mr_statuses.update(results)
            self._update_mr_column()
        except Exception:
            logger.exception("Error handling GitLab results")

    def _on_cq_commands(self, commands: list[Any]) -> None:
        """Handle /cq commands detected during GitLab polling."""
        if self._shutting_down:
            return
        try:
            if not self.isVisible() or not self._gitlab_provider:
                return

            # Pause polling while handling commands (dialogs may block)
            self._gitlab_timer.stop()

            from claudeq.monitor.mr_tracking.cq_command import format_cq_message
            from claudeq.monitor.cq_sender import send_to_cq_session

            for cmd in commands:
                tag, no_match = self._match_session_for_cq(cmd)
                if tag:
                    message = format_cq_message(cmd)
                    sent = send_to_cq_session(tag, message)
                    if sent:
                        logger.info("/cq from MR !%s sent to session '%s'", cmd.mr_iid, tag)
                    else:
                        logger.error("Failed to send /cq message to session '%s'", tag)
                    # Always acknowledge to prevent re-processing on next poll
                    self._gitlab_provider.acknowledge_cq_command(
                        cmd.project_path, cmd.mr_iid, cmd.discussion_id
                    )
                elif no_match:
                    # Truly no sessions found — report on GitLab
                    self._gitlab_provider.report_no_session(
                        cmd.project_path, cmd.mr_iid, cmd.discussion_id
                    )
                    logger.info("No session match for /cq from MR !%s (%s)",
                                cmd.mr_iid, cmd.project_path)
                # else: user cancelled dialog — do nothing, will retry next poll

            # Resume polling
            config = load_gitlab_config()
            interval = config.get('poll_interval', GITLAB_POLL_INTERVAL) if config else GITLAB_POLL_INTERVAL
            self._gitlab_timer.start(interval * 1000)
        except Exception:
            logger.exception("Error handling /cq commands")
            # Ensure polling resumes even if there's an error
            try:
                config = load_gitlab_config()
                interval = config.get('poll_interval', GITLAB_POLL_INTERVAL) if config else GITLAB_POLL_INTERVAL
                self._gitlab_timer.start(interval * 1000)
            except Exception:
                logger.exception("Failed to restart GitLab polling timer")

    def _match_session_for_cq(self, cmd: Any) -> tuple[Optional[str], bool]:
        """Match a /cq command to a CQ session by project path.

        Returns (tag, no_match) where:
        - (tag, False) — matched a session
        - (None, True) — no sessions match at all
        - (None, False) — user cancelled the dialog
        """
        matching = []
        for session in self.sessions:
            if not session.get('project_path'):
                continue
            remote_info = get_git_remote_info(session['project_path'])
            if remote_info and remote_info.project_path == cmd.project_path:
                matching.append(session)

        if len(matching) == 1:
            return matching[0]['tag'], False
        elif len(matching) > 1:
            tags = [s['tag'] for s in matching]
            tag, ok = QInputDialog.getItem(
                self, 'Select Session',
                f'Multiple sessions for {cmd.project_path}.\n'
                f'MR !{cmd.mr_iid}: "{cmd.mr_title}"\n'
                f'Pick one:',
                tags, 0, False
            )
            return (tag, False) if ok else (None, False)
        else:
            return None, True

    def _refresh_data(self) -> None:
        """Refresh session data and update table."""
        self.sessions = get_active_sessions()
        self._update_table()

    def _set_cell_text(self, row: int, col: int, text: str) -> None:
        """Set cell text only if it changed, to avoid flicker."""
        item = self.table.item(row, col)
        if item is None:
            self.table.setItem(row, col, QTableWidgetItem(text))
        elif item.text() != text:
            item.setText(text)

    def _update_table(self) -> None:
        """Update table with current sessions.

        Creates fresh cell widgets each refresh to avoid dangling C++ pointers.
        When setRowCount() shrinks the table, Qt auto-deletes cell widgets in
        removed rows. Caching widgets across refreshes risks SIGSEGV when a
        stale Python reference to a destroyed C++ widget is reused.
        """
        new_count = len(self.sessions)

        self.table.setUpdatesEnabled(False)
        try:
            # Stop pulsing and release all cached widget references BEFORE
            # modifying the table.  This ensures we never hold a Python
            # reference to a widget that Qt is about to destroy.
            for widget in self._mr_widgets.values():
                try:
                    widget.set_pulsing(False)
                except RuntimeError:
                    pass  # C++ object already deleted
            self._mr_widgets.clear()

            if not self.sessions:
                self.table.setRowCount(1)
                self._set_cell_text(0, 0, 'No active sessions')
                return

            self.table.setRowCount(new_count)

            for row, session in enumerate(self.sessions):
                tag = session['tag']

                # Text cells — only update if value changed
                self._set_cell_text(row, self.COL_TAG, tag)
                self._set_cell_text(row, self.COL_PROJECT, session['project'])
                self._set_cell_text(row, self.COL_BRANCH, session['branch'])

                status = '\u2705 Running' if session['claude_busy'] else '\u26aa Idle'
                self._set_cell_text(row, self.COL_STATUS, status)
                self._set_cell_text(row, self.COL_QUEUE, str(session['queue_size']))

                # MR — "Track MR" button for untracked, PulsingLabel for tracked
                if tag in self._tracked_tags:
                    mr_widget = PulsingLabel()
                    self._mr_widgets[tag] = mr_widget
                    self._apply_mr_status(mr_widget, self._mr_statuses.get(tag))
                    self.table.setCellWidget(row, self.COL_MR, mr_widget)
                else:
                    track_btn = QPushButton('Track MR')
                    track_btn.setStyleSheet('font-size: 11px;')
                    track_btn.clicked.connect(
                        lambda checked, t=tag: self._start_tracking(t)
                    )
                    self.table.setCellWidget(row, self.COL_MR, track_btn)

                # Server button + close button
                server_pid = session.get('server_pid')
                server_container = QWidget()
                server_layout = QHBoxLayout(server_container)
                server_layout.setContentsMargins(0, 0, 0, 0)
                server_layout.setSpacing(2)

                server_btn = QPushButton('Server')
                server_btn.clicked.connect(
                    lambda checked, t=tag: focus_session(t, 'server')
                )
                server_layout.addWidget(server_btn)

                server_x = QPushButton('X')
                server_x.setFixedSize(24, server_btn.sizeHint().height())
                server_x.setStyleSheet(
                    'QPushButton { color: #999; font-size: 11px; padding: 0; }'
                    'QPushButton:hover { color: #ff4444; font-weight: bold; }'
                )
                server_x.setToolTip(f'Close server {tag}')
                server_x.clicked.connect(
                    lambda checked, t=tag, pid=server_pid: self._close_server(t, pid)
                )
                server_layout.addWidget(server_x, 0, Qt.AlignVCenter)
                self.table.setCellWidget(row, self.COL_SERVER, server_container)

                # Client button + close button
                client_pid = session.get('client_pid')
                client_container = QWidget()
                client_layout = QHBoxLayout(client_container)
                client_layout.setContentsMargins(0, 0, 0, 0)
                client_layout.setSpacing(2)

                client_btn = QPushButton('Client')
                client_btn.clicked.connect(
                    lambda checked, t=tag: focus_session(t, 'client')
                )
                client_layout.addWidget(client_btn)

                if client_pid is not None:
                    client_x = QPushButton('X')
                    client_x.setFixedSize(24, client_btn.sizeHint().height())
                    client_x.setStyleSheet(
                        'QPushButton { color: #999; font-size: 11px; padding: 0; }'
                        'QPushButton:hover { color: #ff4444; font-weight: bold; }'
                    )
                    client_x.setToolTip(f'Close client {tag}')
                    client_x.clicked.connect(
                        lambda checked, t=tag, pid=client_pid: self._close_client(t, pid)
                    )
                    client_layout.addWidget(client_x, 0, Qt.AlignVCenter)

                self.table.setCellWidget(row, self.COL_CLIENT, client_container)
        finally:
            self.table.setUpdatesEnabled(True)

    def _start_tracking(self, tag: str) -> None:
        """Start MR tracking for a session after a one-shot check."""
        if not self._gitlab_provider:
            QMessageBox.information(
                self, 'GitLab Not Connected',
                'Connect to GitLab first using the button at the bottom.'
            )
            return

        # Find the session data for this tag
        session = next((s for s in self.sessions if s['tag'] == tag), None)
        if not session:
            return

        project_path = session.get('project_path')
        if not project_path:
            QMessageBox.information(self, 'No MR Found', 'No project path for this session.')
            return

        remote_info = get_git_remote_info(project_path)
        if not remote_info:
            QMessageBox.information(self, 'No MR Found', 'Could not determine Git remote info.')
            return

        # One-shot check — may briefly block the UI
        try:
            status = self._gitlab_provider.get_mr_status(
                remote_info.project_path, remote_info.branch
            )
        except Exception:
            QMessageBox.warning(self, 'Error', 'Failed to query GitLab.')
            return

        if status.state == MRState.NO_MR:
            QMessageBox.information(
                self, 'No MR Found',
                f'No open merge request found for branch:\n{remote_info.branch}'
            )
            return

        # MR found — start tracking
        self._tracked_tags.add(tag)
        self._mr_statuses[tag] = status
        self._update_table()

        # Ensure the poll timer is running for ongoing updates
        if not self._gitlab_timer.isActive():
            config = load_gitlab_config()
            interval = config.get('poll_interval', GITLAB_POLL_INTERVAL) if config else GITLAB_POLL_INTERVAL
            self._gitlab_timer.start(interval * 1000)

    def _close_server(self, tag: str, server_pid: Optional[int]) -> None:
        """Prompt for confirmation and close a server session."""
        reply = QMessageBox.question(
            self,
            'Close Server',
            f"Close server '{tag}'?\n\nThis will terminate the Claude CLI session.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        # Kill the process
        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(socket_path, {'type': 'shutdown'}, timeout=3.0)
        if not (response and response.get('status') == 'ok'):
            if server_pid:
                try:
                    os.kill(server_pid, signal.SIGTERM)
                except OSError:
                    pass

        # Close the terminal tab
        metadata = load_session_metadata(tag)
        preferred_ide = metadata.get('ide') if metadata else None
        project_path = metadata.get('project_path') if metadata else None
        close_terminal_with_title(
            f"cq-server {tag}", preferred_ide, project_path, f"cq-server {tag}"
        )

    def _close_client(self, tag: str, client_pid: Optional[int]) -> None:
        """Prompt for confirmation and close a client session."""
        reply = QMessageBox.question(
            self,
            'Close Client',
            f"Close client '{tag}'?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        if client_pid:
            try:
                os.kill(client_pid, signal.SIGTERM)
            except OSError:
                pass

        # Close the terminal tab
        metadata = load_session_metadata(tag)
        preferred_ide = metadata.get('ide') if metadata else None
        project_path = metadata.get('project_path') if metadata else None
        close_terminal_with_title(
            f"cq-client {tag}", preferred_ide, project_path, f"cq-client {tag}"
        )

    def _update_mr_column(self) -> None:
        """Update just the MR column widgets without rebuilding the whole table."""
        for row in range(self.table.rowCount()):
            tag_item = self.table.item(row, self.COL_TAG)
            if not tag_item:
                continue
            tag = tag_item.text()
            mr_widget = self._mr_widgets.get(tag)
            if not mr_widget or sip.isdeleted(mr_widget):
                self._mr_widgets.pop(tag, None)
                continue
            try:
                self._apply_mr_status(mr_widget, self._mr_statuses.get(tag))
            except RuntimeError:
                # Widget was deleted, remove from cache
                self._mr_widgets.pop(tag, None)

    def _apply_mr_status(self, widget: PulsingLabel, status: Optional[MRStatus]) -> None:
        """Apply MR status to a PulsingLabel widget."""
        if not status or not self._gitlab_provider:
            widget.setText('N/A')
            widget.setStyleSheet('color: grey;')
            widget.setToolTip('GitLab not configured')
            widget.set_pulsing(False)
            widget.set_mr_url(None)
            return

        if status.state == MRState.NOT_CONFIGURED:
            widget.setText('N/A')
            widget.setStyleSheet('color: grey;')
            widget.setToolTip('GitLab not configured')
            widget.set_pulsing(False)
            widget.set_mr_url(None)

        elif status.state == MRState.NO_MR:
            widget.setText('No MR')
            widget.setStyleSheet('color: grey;')
            widget.setToolTip('No open MR for this branch')
            widget.set_pulsing(False)
            widget.set_mr_url(None)

        elif status.state == MRState.ALL_RESPONDED:
            widget.setText('✓')
            widget.setStyleSheet('color: green; font-weight: bold;')
            widget.setToolTip(f'MR !{status.mr_iid}: {status.mr_title}\nAll threads responded.')
            widget.set_pulsing(False)
            widget.set_mr_url(status.mr_url)

        elif status.state == MRState.UNRESPONDED:
            widget.setText(f'💬 {status.unresponded_count}')
            widget.setToolTip(
                f'MR !{status.mr_iid}: {status.mr_title}\n'
                f'{status.unresponded_count} unresponded thread(s).'
            )
            widget.set_pulsing(True)
            # Jump directly to first unresolved comment thread
            url = status.mr_url
            if url and status.first_unresponded_note_id:
                url = f'{url}#note_{status.first_unresponded_note_id}'
            widget.set_mr_url(url)

    def _auto_refresh(self) -> None:
        """Auto-refresh callback."""
        if self._shutting_down:
            return
        try:
            self._refresh_data()
        except Exception:
            logger.exception("Error in auto-refresh")

    def _toggle_include_bots(self, state: int) -> None:
        """Toggle bot comment inclusion and persist."""
        include = state == Qt.Checked
        self._prefs['include_bots'] = include
        save_monitor_prefs(self._prefs)
        # Update filter and re-poll tracked sessions
        if self._gitlab_provider:
            self._gitlab_provider._filter_bots = not include
            if self._tracked_tags:
                self._start_gitlab_poll()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Handle window close event - save prefs then force-exit the process.

        QThread.terminate() does not work on Python threads, and the
        GitLabPollerWorker's ThreadPoolExecutor can block indefinitely on
        network I/O.  Instead of trying to join threads gracefully (which
        hangs), we save state and then os._exit() to guarantee the process
        dies immediately.
        """
        # Prevent timers and signal handlers from firing during shutdown
        self._shutting_down = True
        self.timer.stop()
        self._gitlab_timer.stop()

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
    app = QApplication(sys.argv)
    app.setApplicationName('ClaudeQ Monitor')

    # Set app icon for Dock
    icon_path = _find_icon()
    if icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))

    window = MonitorWindow()
    window.show()

    # Handle Ctrl+C gracefully
    def signal_handler(sig: int, frame: Any) -> None:
        window.close()
        app.quit()

    signal.signal(signal.SIGINT, signal_handler)

    # Allow Python to handle signals during Qt event loop
    from PyQt5.QtCore import QTimer
    timer = QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
