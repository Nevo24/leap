#!/usr/bin/env python3
"""
ClaudeQ Monitor - GUI to view and manage active claudeq sessions
"""
import sys
import os
import subprocess
from pathlib import Path
import FreeSimpleGUI as sg

SOCKET_DIR = Path.home() / ".claude-sockets"
QUEUE_DIR = Path.home() / ".claude-queues"


def query_server_status(socket_path):
    """Query server for status via socket (same as client does)"""
    try:
        import socket as sock
        import json

        client_socket = sock.socket(sock.AF_UNIX, sock.SOCK_STREAM)
        client_socket.settimeout(1.0)
        client_socket.connect(str(socket_path))

        data = {
            'type': 'status',
            'message': ''
        }

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

        # Query server status via socket (same as client does)
        status_response = query_server_status(socket_file)

        if not status_response:
            # Server not responding, skip this session
            continue

        # Get queue size and ready status from server
        queue_size = status_response.get('queue_size', 0)
        is_ready = status_response.get('ready', True)
        # "Running" means Claude is busy (NOT ready to accept next message)
        claude_busy = not is_ready

        # Load metadata to get project info and branch
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
            'alive': True,  # We already confirmed it's alive via status query
            'claude_busy': claude_busy,
            'queue_size': queue_size,
            'socket': str(socket_file),
            'project': project_name or 'N/A',
            'branch': branch_name or 'N/A'
        })

    return sorted(sessions, key=lambda x: x['tag'])


def _try_jetbrains(title_pattern, preferred_ide=None, project_path=None, terminal_title=None):
    """Helper to try JetBrains IDEs"""
    script_dir = Path(__file__).parent
    groovy_script = script_dir / "activate_terminal.groovy"

    cmd_to_process = {
        'idea': 'IntelliJ IDEA',
        'pycharm': 'PyCharm',
        'webstorm': 'WebStorm',
        'phpstorm': 'PhpStorm',
        'goland': 'GoLand',
        'rubymine': 'RubyMine',
        'clion': 'CLion',
        'datagrip': 'DataGrip'
    }

    # If we have a preferred IDE, check only that one first
    if preferred_ide:
        for cmd, process_name in cmd_to_process.items():
            if process_name == preferred_ide or preferred_ide.lower() in process_name.lower():
                try:
                    # Quick check if running
                    result = subprocess.run(
                        ['osascript', '-e', f'tell application "System Events" to return exists (process "{process_name}")'],
                        capture_output=True,
                        text=True
                    )
                    if result.returncode == 0 and 'true' in result.stdout:
                        # Check CLI exists
                        if subprocess.run(['which', cmd], capture_output=True).returncode == 0:
                            return _activate_jetbrains_ide(cmd, process_name, project_path, terminal_title, groovy_script)
                except:
                    pass

    return False


def _activate_jetbrains_ide(idea_cmd, ide_app_name, project_path, terminal_title, groovy_script):
    """Activate a specific JetBrains IDE"""
    try:
        # Use the IDE CLI to directly open/focus the project
        if project_path:
            subprocess.run(
                [idea_cmd, project_path],
                capture_output=True,
                text=True
            )
        else:
            # Just bring the IDE to front
            subprocess.run(
                ['osascript', '-e', f'tell application "System Events" to tell process "{ide_app_name}" to set frontmost to true'],
                capture_output=True
            )

        # Small delay to let the window come to front
        import time
        time.sleep(0.2)

        # Try to activate the Terminal in this IDE
        import tempfile

        if project_path or terminal_title:
            # Create temporary groovy script with hardcoded values
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

            groovy_to_use = tmp_script_path
        else:
            groovy_to_use = str(groovy_script)

        try:
            result = subprocess.run(
                [idea_cmd, 'ideScript', groovy_to_use],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                return 'jetbrains'
        finally:
            if groovy_to_use != str(groovy_script) and os.path.exists(groovy_to_use):
                try:
                    os.unlink(groovy_to_use)
                except:
                    pass
    except:
        pass

    return False


def find_terminal_with_title(title_pattern, preferred_ide=None, project_path=None, terminal_title=None):
    """Find terminal window/tab with matching title using AppleScript

    Args:
        title_pattern: The terminal title pattern to search for
        preferred_ide: The IDE name from metadata (e.g., 'PyCharm', 'GoLand')
        project_path: The project directory path from metadata
        terminal_title: The terminal tab title to search for (e.g., 'cq-server tag')
    """
    # If we have a preferred IDE, try JetBrains first for speed
    if preferred_ide and any(ide in preferred_ide for ide in ['PyCharm', 'IntelliJ', 'GoLand', 'WebStorm', 'PhpStorm', 'RubyMine', 'CLion', 'DataGrip']):
        result = _try_jetbrains(title_pattern, preferred_ide, project_path, terminal_title)
        if result:
            return result

    # Try Terminal.app first
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
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True
        )
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
        result = subprocess.run(
            ['osascript', '-e', script_iterm],
            capture_output=True,
            text=True
        )
        if result.returncode == 0 and 'true' in result.stdout:
            return True
    except:
        pass

    # Try VS Code
    script_vscode = f'''
    tell application "System Events"
        if exists (process "Code") then
            tell process "Code"
                set frontmost to true
                return true
            end tell
        end if
    end tell
    return false
    '''

    try:
        result = subprocess.run(
            ['osascript', '-e', script_vscode],
            capture_output=True,
            text=True
        )
        if result.returncode == 0 and 'true' in result.stdout:
            return 'vscode'
    except:
        pass

    # Try JetBrains IDEs as fallback
    result = _try_jetbrains(title_pattern, preferred_ide, project_path, terminal_title)
    if result:
        return result

    return False


def load_session_metadata(tag):
    """Load metadata for a session"""
    metadata_file = SOCKET_DIR / f"{tag}.meta"
    if metadata_file.exists():
        try:
            import json
            with open(metadata_file, 'r') as f:
                return json.load(f)
        except:
            pass
    return None



def focus_session(tag, session_type='server'):
    """Focus the terminal with the given session"""
    title_pattern = f"cq-{session_type} {tag}"

    # Try to load metadata to find preferred IDE and project
    metadata = load_session_metadata(tag)
    preferred_ide = metadata.get('ide') if metadata else None
    project_path = metadata.get('project_path') if metadata else None

    # Construct the correct terminal title based on session type
    # Metadata only has server title, so we need to build client title
    terminal_title = f"cq-{session_type} {tag}"

    # Check if the requested session type exists
    if session_type == 'client':
        client_lock = SOCKET_DIR / f"{tag}.client.lock"
        if not client_lock.exists():
            # Client not found, offer to go to server instead
            response = sg.popup_yes_no(
                f'Client not found for: {tag}\n\n'
                f'Go to server instead?',
                title='Client Not Found'
            )
            if response == 'Yes':
                focus_session(tag, 'server')
            return
    elif session_type == 'server':
        socket_file = SOCKET_DIR / f"{tag}.sock"
        if not socket_file.exists():
            # Server not found, offer to go to client instead
            response = sg.popup_yes_no(
                f'Server not found for: {tag}\n\n'
                f'Go to client instead?',
                title='Server Not Found'
            )
            if response == 'Yes':
                focus_session(tag, 'client')
            return

    result = find_terminal_with_title(title_pattern, preferred_ide, project_path, terminal_title)

    # Only show error popup if something went wrong
    # Success cases: the terminal/IDE is now in front, don't steal focus back
    if result == False and result != 'vscode' and result != 'jetbrains' and result != 'jetbrains_fallback':
        sg.popup_error(f'Could not find terminal for {session_type}: {tag}\n\n'
                      f'Make sure the terminal tab title is set correctly.')


def create_window(sessions, location=None):
    """Create the monitor GUI window"""
    sg.theme('DarkBlue3')

    if not sessions:
        layout = [
            [sg.Text('No active ClaudeQ sessions found', font=('Helvetica', 12))],
            [sg.Text('')],
            [sg.Button('Refresh', key='refresh'), sg.Button('Close')]
        ]
    else:
        header = [
            [sg.Text('ClaudeQ Session Monitor', font=('Helvetica', 17, 'bold'))],
            [sg.Text('JetBrains Users: Enable CQ to name your tabs:', font=('Helvetica', 11), text_color='yellow')],
            [sg.Text('1. Settings > Tools > Terminal > Engine: Classic', font=('Helvetica', 10), text_color='lightblue')],
            [sg.Text('2. Advanced Settings > Terminal > ☑️ "Show application title"', font=('Helvetica', 10), text_color='lightblue')],
            [sg.HorizontalSeparator()],
            # Column headers
            [
                sg.Text('Tag', size=(16, 1), font=('Helvetica', 12, 'bold'), justification='left'),
                sg.Text('Project', size=(19, 1), font=('Helvetica', 12, 'bold'), justification='left'),
                sg.Text('Branch', size=(19, 1), font=('Helvetica', 12, 'bold'), justification='left'),
                sg.Text('Status', size=(13, 1), font=('Helvetica', 12, 'bold'), justification='left'),
                sg.Text('Queue', size=(7, 1), font=('Helvetica', 12, 'bold'), justification='left'),
                sg.Text('Actions', size=(22, 1), font=('Helvetica', 12, 'bold'), justification='left')
            ]
        ]

        session_rows = []
        for session in sessions:
            # Status: Show if Claude is busy processing (running) or ready (idle)
            if session.get('claude_busy', False):
                status = '✅ Running'
            else:
                status = '⚪ Idle'

            project = session.get('project', 'N/A')
            branch = session.get('branch', 'N/A')
            queue_count = session.get('queue_size', 0)

            tag = session['tag']
            row = [
                sg.Text(f"{tag}", size=(16, 1), font=('Helvetica', 13), justification='left'),
                sg.Text(f"{project}", size=(19, 1), font=('Helvetica', 12), justification='left'),
                sg.Text(f"{branch}", size=(19, 1), font=('Helvetica', 12), justification='left'),
                sg.Text(status, size=(13, 1), font=('Helvetica', 12), justification='left', key=f"status_{tag}"),
                sg.Text(f"{queue_count}", size=(7, 1), font=('Helvetica', 12), justification='left', key=f"queue_{tag}"),
                sg.Button('Server', key=f"server_{tag}", size=(10, 1), font=('Helvetica', 13)),
                sg.Button('Client', key=f"client_{tag}", size=(10, 1), font=('Helvetica', 13))
            ]
            session_rows.append(row)

        footer = [
            [sg.HorizontalSeparator()],
            [sg.Button('Refresh', key='refresh', size=(10, 1), font=('Helvetica', 13)),
             sg.Checkbox('Auto (1s)', key='auto_toggle', default=False, enable_events=True, font=('Helvetica', 13)),
             sg.Push(),
             sg.Button('Close', size=(10, 1), font=('Helvetica', 13))]
        ]

        layout = header + session_rows + footer

    return sg.Window('ClaudeQ Monitor', layout, location=location, finalize=True)


def update_window_data(window, sessions):
    """Update the window data without recreating it"""
    for session in sessions:
        tag = session['tag']
        # Check if this session exists in the window
        if f"status_{tag}" not in window.key_dict:
            return False  # Session list changed, need to recreate window

        # Status: Show if Claude is busy processing (running) or ready (idle)
        if session.get('claude_busy', False):
            status = '✅ Running'
        else:
            status = '⚪ Idle'

        queue_count = session.get('queue_size', 0)

        # Update the elements
        window[f"status_{tag}"].update(status)
        window[f"queue_{tag}"].update(str(queue_count))

    return True  # Successfully updated


def main():
    """Main monitor loop"""
    # Set terminal title
    print("\033]0;claudeq-monitor\007", end='', flush=True)

    sessions = get_active_sessions()
    window = create_window(sessions)
    auto_refresh = False
    current_session_tags = set(s['tag'] for s in sessions)

    while True:
        # Timeout of 1000ms (1s) if auto-refresh is enabled, None otherwise
        timeout = 1000 if auto_refresh else None
        event, values = window.read(timeout=timeout)

        if event in (sg.WIN_CLOSED, 'Close'):
            break

        if event == 'refresh' or (auto_refresh and event == sg.TIMEOUT_KEY):
            new_sessions = get_active_sessions()
            new_session_tags = set(s['tag'] for s in new_sessions)

            # Check if session list changed (added or removed)
            if new_session_tags != current_session_tags:
                # Session list changed, need to recreate window
                current_auto_state = values.get('auto_toggle', False) if values else auto_refresh
                current_location = window.current_location()
                window.close()
                window = create_window(new_sessions, location=current_location)
                if current_auto_state:
                    window['auto_toggle'].update(value=True)
                auto_refresh = current_auto_state
                current_session_tags = new_session_tags
            else:
                # Just update the data without recreating window
                update_window_data(window, new_sessions)

            sessions = new_sessions

        if event == 'auto_toggle':
            auto_refresh = values.get('auto_toggle', False)

        if event and event.startswith('server_'):
            tag = event.replace('server_', '')
            focus_session(tag, 'server')

        if event and event.startswith('client_'):
            tag = event.replace('client_', '')
            focus_session(tag, 'client')

    window.close()


if __name__ == '__main__':
    main()
