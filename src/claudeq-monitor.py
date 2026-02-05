#!/usr/bin/env python3
"""
ClaudeQ Monitor - GUI to view and manage active claudeq sessions (PyQt5 version)
"""
import sys
import os
import subprocess
from pathlib import Path
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QTableWidget, QTableWidgetItem,
                             QPushButton, QLabel, QCheckBox, QHeaderView, QMessageBox)
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QIcon

SOCKET_DIR = Path.home() / ".claude-sockets"
QUEUE_DIR = Path.home() / ".claude-queues"


def query_server_status(socket_path):
    """Query server for status via socket"""
    try:
        import socket as sock
        import json

        client_socket = sock.socket(sock.AF_UNIX, sock.SOCK_STREAM)
        client_socket.settimeout(1.0)
        client_socket.connect(str(socket_path))

        data = {'type': 'status', 'message': ''}
        client_socket.send(json.dumps(data).encode('utf-8'))
        response = client_socket.recv(4096).decode('utf-8')
        client_socket.close()

        return json.loads(response)
    except:
        return None


def get_active_sessions():
    """Get list of active claudeq sessions"""
    sessions = []

    if not SOCKET_DIR.exists():
        return sessions

    for socket_file in SOCKET_DIR.glob("*.sock"):
        tag = socket_file.stem
        status_response = query_server_status(socket_file)

        if not status_response:
            continue

        queue_size = status_response.get('queue_size', 0)
        is_ready = status_response.get('ready', True)
        claude_busy = not is_ready

        project_name = None
        branch_name = None

        metadata_file = SOCKET_DIR / f"{tag}.meta"
        if metadata_file.exists():
            try:
                import json
                with open(metadata_file, 'r') as f:
                    metadata = json.load(f)
                    project_path = metadata.get('project_path', '')
                    if project_path:
                        project_name = os.path.basename(project_path)
                    branch_name = metadata.get('branch')
            except:
                pass

        sessions.append({
            'tag': tag,
            'claude_busy': claude_busy,
            'queue_size': queue_size,
            'project': project_name or 'N/A',
            'branch': branch_name or 'N/A',
            'project_path': metadata.get('project_path', '') if metadata_file.exists() else None,
            'ide': metadata.get('ide', '') if metadata_file.exists() else None,
        })

    return sorted(sessions, key=lambda x: x['tag'])


def find_terminal_with_title(title_pattern, preferred_ide=None, project_path=None, terminal_title=None):
    """Find terminal window/tab with matching title"""
    # Try JetBrains IDEs first if preferred IDE is specified
    if preferred_ide and any(ide in preferred_ide for ide in ['PyCharm', 'IntelliJ', 'GoLand', 'WebStorm', 'PhpStorm']):
        script_dir = Path(__file__).parent
        groovy_script = script_dir / "activate_terminal.groovy"

        # Check for groovy script in Resources if running from .app
        if not groovy_script.exists():
            groovy_script = script_dir.parent / "Resources" / "activate_terminal.groovy"

        if groovy_script.exists():
            ide_cmd_map = {
                'PyCharm': 'pycharm',
                'IntelliJ IDEA': 'idea',
                'GoLand': 'goland',
                'WebStorm': 'webstorm',
                'PhpStorm': 'phpstorm',
            }

            ide_cmd = ide_cmd_map.get(preferred_ide)
            if ide_cmd:
                try:
                    import tempfile
                    # Create a temporary groovy script with hardcoded values
                    with open(groovy_script, 'r') as f:
                        template_content = f.read()

                    custom_script = template_content
                    if project_path:
                        custom_script = custom_script.replace(
                            'var projectPath = System.getenv("CLAUDEQ_PROJECT_PATH")',
                            f'var projectPath = "{project_path}"'
                        )
                    if terminal_title:
                        custom_script = custom_script.replace(
                            'var terminalTabName = System.getenv("CLAUDEQ_TERMINAL_TITLE")',
                            f'var terminalTabName = "{terminal_title}"'
                        )

                    with tempfile.NamedTemporaryFile(mode='w', suffix='.groovy', delete=False) as tmp:
                        tmp.write(custom_script)
                        tmp_script_path = tmp.name

                    try:
                        # Expand PATH to include JetBrains CLI tools
                        import glob
                        env = os.environ.copy()
                        jetbrains_paths = []

                        # Find JetBrains .app bundles
                        for pattern in ['IntelliJ*.app', 'PyCharm*.app', 'WebStorm*.app',
                                       'PhpStorm*.app', 'GoLand*.app', 'RubyMine*.app',
                                       'CLion*.app', 'DataGrip*.app', 'Rider*.app', 'Fleet*.app']:
                            for app in glob.glob(f'/Applications/{pattern}'):
                                jetbrains_paths.append(f'{app}/Contents/MacOS')

                        if jetbrains_paths:
                            env['PATH'] = ':'.join(jetbrains_paths) + ':' + env.get('PATH', '')

                        # First, open/focus the project if we have a project path
                        if project_path:
                            subprocess.run(
                                [ide_cmd, project_path],
                                capture_output=True,
                                env=env,
                                timeout=5
                            )
                            # Give IDE time to open/focus
                            import time
                            time.sleep(0.3)

                        # Then run the groovy script to activate terminal
                        result = subprocess.run(
                            [ide_cmd, 'ideScript', tmp_script_path],
                            capture_output=True,
                            timeout=5,
                            env=env
                        )
                        if result.returncode == 0:
                            return True
                    finally:
                        try:
                            os.unlink(tmp_script_path)
                        except:
                            pass
                except:
                    pass

    # Try Terminal.app
    script = f'''
    tell application "Terminal"
        repeat with w in windows
            repeat with t from 1 to count of tabs of w
                set tabName to custom title of tab t of w
                if tabName contains "{title_pattern}" then
                    set frontmost of w to true
                    set selected of tab t of w to true
                    activate
                    return true
                end if
            end repeat
        end repeat
    end tell
    return false
    '''

    try:
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
        if result.returncode == 0 and 'true' in result.stdout:
            return True
    except:
        pass

    # Try iTerm2
    script_iterm = f'''
    tell application "iTerm"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if name of s contains "{title_pattern}" then
                        select w
                        select t
                        select s
                        activate
                        return true
                    end if
                end repeat
            end repeat
        end repeat
    end tell
    return false
    '''

    try:
        result = subprocess.run(['osascript', '-e', script_iterm], capture_output=True, text=True)
        if result.returncode == 0 and 'true' in result.stdout:
            return True
    except:
        pass

    return False


def focus_session(tag, session_type='server'):
    """Focus the terminal with the given session"""
    # Load metadata
    metadata_file = SOCKET_DIR / f"{tag}.meta"
    metadata = None
    if metadata_file.exists():
        try:
            import json
            with open(metadata_file, 'r') as f:
                metadata = json.load(f)
        except:
            pass

    preferred_ide = metadata.get('ide') if metadata else None
    project_path = metadata.get('project_path') if metadata else None
    title_pattern = f"cq-{session_type} {tag}"
    terminal_title = title_pattern

    # Check if session exists
    if session_type == 'client':
        client_lock = SOCKET_DIR / f"{tag}.client.lock"
        if not client_lock.exists():
            reply = QMessageBox.question(None, 'Client Not Found',
                                        f'Client not found for: {tag}\n\nGo to server instead?',
                                        QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                focus_session(tag, 'server')
            return
    elif session_type == 'server':
        socket_file = SOCKET_DIR / f"{tag}.sock"
        if not socket_file.exists():
            reply = QMessageBox.question(None, 'Server Not Found',
                                        f'Server not found for: {tag}\n\nGo to client instead?',
                                        QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.Yes:
                focus_session(tag, 'client')
            return

    # Try to find and focus the terminal
    result = find_terminal_with_title(title_pattern, preferred_ide, project_path, terminal_title)

    if not result:
        QMessageBox.warning(None, 'Navigation Failed',
                           f'Could not navigate to {session_type}: {tag}\n\n'
                           'Make sure terminal tab titles are configured correctly.')


class MonitorWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.sessions = []

        # Setup auto-refresh timer BEFORE init_ui (so signal handler can access it)
        self.timer = QTimer()
        self.timer.timeout.connect(self.auto_refresh)

        self.init_ui()
        self.refresh_data()

        # Start timer after UI is initialized
        self.timer.start(1000)  # Start with 1 second interval

    def init_ui(self):
        self.setWindowTitle('ClaudeQ Monitor')
        self.setGeometry(100, 100, 900, 600)

        # Set app icon
        icon_paths = [
            Path(__file__).parent.parent / "assets" / "claudeq-icon.png",
            Path(__file__).parent / "claudeq-icon.png",
        ]
        for icon_path in icon_paths:
            if icon_path.exists():
                self.setWindowIcon(QIcon(str(icon_path)))
                break

        # Main widget and layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout()
        main_widget.setLayout(layout)

        # Help text
        help_label = QLabel('JetBrains Users: Enable CQ to name your tabs:\n'
                           '1. Settings > Tools > Terminal > Engine: Classic\n'
                           '2. Advanced Settings > Terminal > ☑ "Show application title"')
        help_label.setStyleSheet('color: #FFA500; font-size: 10px;')
        layout.addWidget(help_label)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(['Tag', 'Project', 'Branch', 'Status', 'Queue', 'Server', 'Client'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        layout.addWidget(self.table)

        # Bottom controls
        bottom_layout = QHBoxLayout()

        self.refresh_btn = QPushButton('Refresh')
        self.refresh_btn.clicked.connect(self.refresh_data)
        bottom_layout.addWidget(self.refresh_btn)

        self.auto_check = QCheckBox('Auto (1s)')
        self.auto_check.setChecked(True)  # Default to enabled
        self.auto_check.stateChanged.connect(self.toggle_auto_refresh)
        bottom_layout.addWidget(self.auto_check)

        bottom_layout.addStretch()

        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.close)
        bottom_layout.addWidget(close_btn)

        layout.addLayout(bottom_layout)

    def refresh_data(self):
        """Refresh session data and update table"""
        self.sessions = get_active_sessions()
        self.update_table()

    def update_table(self):
        """Update table with current sessions"""
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
            server_btn.clicked.connect(lambda checked, t=session['tag']: focus_session(t, 'server'))
            self.table.setCellWidget(row, 5, server_btn)

            # Client button
            client_btn = QPushButton('Client')
            client_btn.clicked.connect(lambda checked, t=session['tag']: focus_session(t, 'client'))
            self.table.setCellWidget(row, 6, client_btn)

    def auto_refresh(self):
        """Auto-refresh callback"""
        self.refresh_data()

    def toggle_auto_refresh(self, state):
        """Toggle auto-refresh on/off"""
        if state == Qt.Checked:
            self.timer.start(1000)  # 1 second
        else:
            self.timer.stop()


def main():
    """Main entry point"""
    app = QApplication(sys.argv)
    app.setApplicationName('ClaudeQ Monitor')

    # Set app icon for Dock
    icon_paths = [
        Path(__file__).parent.parent / "assets" / "claudeq-icon.png",
        Path(__file__).parent / "claudeq-icon.png",
    ]
    for icon_path in icon_paths:
        if icon_path.exists():
            app.setWindowIcon(QIcon(str(icon_path)))
            break

    window = MonitorWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
