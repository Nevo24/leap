"""
ClaudeQ Monitor GUI application.

PyQt5-based GUI for viewing and managing active ClaudeQ sessions.
"""

import sys
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QCheckBox, QHeaderView, QMessageBox
)
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QIcon

from claudeq.utils.constants import SOCKET_DIR
from claudeq.monitor.session_manager import (
    get_active_sessions,
    load_session_metadata,
    session_exists
)
from claudeq.monitor.navigation import find_terminal_with_title


def _find_icon() -> Path | None:
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


class MonitorWindow(QMainWindow):
    """Main window for ClaudeQ Monitor."""

    def __init__(self) -> None:
        """Initialize the monitor window."""
        super().__init__()
        self.sessions: list[dict] = []

        # Setup auto-refresh timer before init_ui
        self.timer = QTimer()
        self.timer.timeout.connect(self._auto_refresh)

        self._init_ui()
        self._refresh_data()

        # Start timer after UI is initialized
        self.timer.start(1000)

    def _init_ui(self) -> None:
        """Initialize the user interface."""
        self.setWindowTitle('ClaudeQ Monitor')
        self.setGeometry(100, 100, 900, 600)

        # Set app icon
        self._set_window_icon()

        # Main widget and layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout()
        main_widget.setLayout(layout)

        # Help text
        help_label = QLabel(
            'Setup Tips:\n'
            '• JetBrains: Settings > Tools > Terminal > Engine: Classic\n'
            '  Advanced Settings > Terminal > ☑ "Show application title"'
        )
        help_label.setStyleSheet('color: #FFA500; font-size: 10px;')
        layout.addWidget(help_label)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            'Tag', 'Project', 'Branch', 'Status', 'Queue', 'Server', 'Client'
        ])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        layout.addWidget(self.table)

        # Bottom controls
        bottom_layout = QHBoxLayout()

        self.refresh_btn = QPushButton('Refresh')
        self.refresh_btn.clicked.connect(self._refresh_data)
        bottom_layout.addWidget(self.refresh_btn)

        self.auto_check = QCheckBox('Auto (1s)')
        self.auto_check.setChecked(True)
        self.auto_check.stateChanged.connect(self._toggle_auto_refresh)
        bottom_layout.addWidget(self.auto_check)

        bottom_layout.addStretch()

        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.close)
        bottom_layout.addWidget(close_btn)

        layout.addLayout(bottom_layout)

    def _set_window_icon(self) -> None:
        """Set the window icon."""
        icon_path = _find_icon()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))

    def _refresh_data(self) -> None:
        """Refresh session data and update table."""
        self.sessions = get_active_sessions()
        self._update_table()

    def _update_table(self) -> None:
        """Update table with current sessions."""
        self.table.setRowCount(len(self.sessions))

        if not self.sessions:
            self.table.setRowCount(1)
            self.table.setItem(0, 0, QTableWidgetItem('No active sessions'))
            return

        for row, session in enumerate(self.sessions):
            # Tag
            self.table.setItem(row, 0, QTableWidgetItem(session['tag']))

            # Project
            self.table.setItem(row, 1, QTableWidgetItem(session['project']))

            # Branch
            self.table.setItem(row, 2, QTableWidgetItem(session['branch']))

            # Status
            status = '✅ Running' if session['claude_busy'] else '⚪ Idle'
            self.table.setItem(row, 3, QTableWidgetItem(status))

            # Queue
            self.table.setItem(row, 4, QTableWidgetItem(str(session['queue_size'])))

            # Server button
            server_btn = QPushButton('Server')
            server_btn.clicked.connect(
                lambda checked, t=session['tag']: focus_session(t, 'server')
            )
            self.table.setCellWidget(row, 5, server_btn)

            # Client button
            client_btn = QPushButton('Client')
            client_btn.clicked.connect(
                lambda checked, t=session['tag']: focus_session(t, 'client')
            )
            self.table.setCellWidget(row, 6, client_btn)

    def _auto_refresh(self) -> None:
        """Auto-refresh callback."""
        self._refresh_data()

    def _toggle_auto_refresh(self, state: int) -> None:
        """Toggle auto-refresh on/off."""
        if state == Qt.Checked:
            self.timer.start(1000)
        else:
            self.timer.stop()


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

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
