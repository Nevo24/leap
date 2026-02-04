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


def get_active_sessions():
    """Get list of active claudeq sessions"""
    sessions = []

    if not SOCKET_DIR.exists():
        return sessions

    for socket_file in SOCKET_DIR.glob("*.sock"):
        tag = socket_file.stem

        # Check if socket is actually alive by testing connection
        try:
            import socket as sock
            s = sock.socket(sock.AF_UNIX, sock.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(str(socket_file))
            s.close()
            is_alive = True
        except:
            is_alive = False

        # Get queue size if available
        queue_size = 0
        queue_file = QUEUE_DIR / f"{tag}.queue"
        if queue_file.exists():
            try:
                with open(queue_file, 'r') as f:
                    queue_size = len([line for line in f if line.strip()])
            except:
                pass

        # Only include alive sessions
        if is_alive:
            sessions.append({
                'tag': tag,
                'alive': is_alive,
                'queue_size': queue_size,
                'socket': str(socket_file)
            })

    return sorted(sessions, key=lambda x: x['tag'])


def find_terminal_with_title(title_pattern, preferred_ide=None, project_path=None):
    """Find terminal window/tab with matching title using AppleScript

    Args:
        title_pattern: The terminal title pattern to search for
        preferred_ide: The IDE name from metadata (e.g., 'PyCharm', 'GoLand')
        project_path: The project directory path from metadata
    """
    # Try Terminal.app first
    script = f'''
    tell application "Terminal"
        repeat with w in windows
            repeat with t from 1 to count of tabs of w
                set tabName to custom title of tab t of w
                if tabName contains "{title_pattern}" then
                    set frontmost of w to true
                    set selected of tab t of w to true
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
            text=True,
            timeout=2
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
            text=True,
            timeout=2
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
            text=True,
            timeout=2
        )
        if result.returncode == 0 and 'true' in result.stdout:
            return 'vscode'
    except:
        pass

    # Try JetBrains IDEs using idea ideScript command
    # This activates the Terminal tool window programmatically
    script_dir = Path(__file__).parent
    groovy_script = script_dir / "activate_terminal.groovy"

    # First, find which JetBrains IDEs are actually running
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

    running_ides = []
    for cmd, process_name in cmd_to_process.items():
        try:
            # Check if this IDE is running
            check_script = f'''
            tell application "System Events"
                if exists (process "{process_name}") then
                    return true
                end if
            end tell
            return false
            '''
            result = subprocess.run(
                ['osascript', '-e', check_script],
                capture_output=True,
                text=True,
                timeout=1
            )
            if result.returncode == 0 and 'true' in result.stdout:
                # Check if the CLI command exists
                which_result = subprocess.run(
                    ['which', cmd],
                    capture_output=True,
                    text=True,
                    timeout=1
                )
                if which_result.returncode == 0:
                    running_ides.append((cmd, process_name))
        except:
            continue

    # If we have a preferred IDE from metadata, try it first
    if preferred_ide:
        # Move preferred IDE to front of list
        preferred_entry = None
        for i, (cmd, process_name) in enumerate(running_ides):
            if process_name == preferred_ide or preferred_ide.lower() in process_name.lower():
                preferred_entry = running_ides.pop(i)
                break
        if preferred_entry:
            running_ides.insert(0, preferred_entry)

    # Try each running IDE
    for idea_cmd, ide_app_name in running_ides:
        try:
            # Bring the IDE to front
            applescript = f'''
            tell application "System Events"
                tell process "{ide_app_name}"
                    set frontmost to true
                end tell
            end tell
            '''

            subprocess.run(['osascript', '-e', applescript],
                         capture_output=True, timeout=2)

            # Small delay to let the window come to front
            import time
            time.sleep(0.3)

            # Try to activate the Terminal in this IDE
            # Pass project path as environment variable if available
            env = os.environ.copy()
            if project_path:
                env['CLAUDEQ_PROJECT_PATH'] = project_path
                print(f"DEBUG: Setting CLAUDEQ_PROJECT_PATH={project_path}", file=sys.stderr)
            else:
                print("DEBUG: No project_path in metadata", file=sys.stderr)

            result = subprocess.run(
                [idea_cmd, 'ideScript', str(groovy_script)],
                capture_output=True,
                text=True,
                timeout=3,
                env=env
            )

            # Print any output from groovy script
            if result.stdout:
                print(f"DEBUG stdout: {result.stdout}", file=sys.stderr)
            if result.stderr:
                print(f"DEBUG stderr: {result.stderr}", file=sys.stderr)
            if result.returncode == 0:
                return 'jetbrains'
        except:
            continue

    # Fallback: Just bring IDE to front using AppleScript
    jetbrains_apps = ['IntelliJ IDEA', 'PyCharm', 'WebStorm', 'PhpStorm',
                     'GoLand', 'RubyMine', 'CLion', 'DataGrip']

    for app in jetbrains_apps:
        script_jetbrains = f'''
        tell application "System Events"
            if exists (process "{app}") then
                tell process "{app}"
                    set frontmost to true
                    return true
                end tell
            end if
        end tell
        return false
        '''

        try:
            result = subprocess.run(
                ['osascript', '-e', script_jetbrains],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0 and 'true' in result.stdout:
                return 'jetbrains_fallback'
        except:
            continue

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

    result = find_terminal_with_title(title_pattern, preferred_ide, project_path)

    if result == True:
        sg.popup_quick_message(f'✓ Focused {session_type}: {tag}',
                              background_color='green',
                              text_color='white',
                              auto_close_duration=1)
    elif result == 'vscode':
        sg.popup_quick_message(f'✓ Brought VS Code to front\n\n'
                              f'VS Code is now active!\n'
                              f'You may need to manually switch to the correct terminal tab.',
                              background_color='green',
                              text_color='white',
                              auto_close_duration=2)
    elif result == 'jetbrains':
        sg.popup_quick_message(f'✓ Activated Terminal tool window\n\n'
                              f'Terminal is now active in your IDE!\n'
                              f'Switch tabs with Alt+Right/Left if needed.',
                              background_color='green',
                              text_color='white',
                              auto_close_duration=2)
    elif result == 'jetbrains_fallback':
        sg.popup_quick_message(f'✓ Brought IDE to front\n\n'
                              f'Note: Install "idea" CLI tool for better support:\n'
                              f'Tools > Create Command-Line Launcher',
                              background_color='orange',
                              text_color='white',
                              auto_close_duration=3)
    else:
        sg.popup_error(f'Could not find terminal for {session_type}: {tag}\n\n'
                      f'Make sure the terminal tab title is set correctly.')


def create_window(sessions):
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
            [sg.Text('ClaudeQ Session Monitor', font=('Helvetica', 14, 'bold'))],
            [sg.HorizontalSeparator()]
        ]

        session_rows = []
        for session in sessions:
            status = '🟢 Active' if session['alive'] else '🔴 Dead'
            queue_info = f"Queue: {session['queue_size']}" if session['queue_size'] > 0 else ''

            row = [
                sg.Text(f"{session['tag']}", size=(20, 1), font=('Helvetica', 11)),
                sg.Text(status, size=(12, 1)),
                sg.Text(queue_info, size=(12, 1)),
                sg.Button('Server', key=f"server_{session['tag']}", size=(8, 1)),
                sg.Button('Client', key=f"client_{session['tag']}", size=(8, 1))
            ]
            session_rows.append(row)

        footer = [
            [sg.HorizontalSeparator()],
            [sg.Button('Refresh', key='refresh'),
             sg.Button('Auto-refresh', key='auto_refresh'),
             sg.Checkbox('Auto (5s)', key='auto_toggle', default=False),
             sg.Button('Close')]
        ]

        layout = header + session_rows + footer

    return sg.Window('ClaudeQ Monitor', layout, finalize=True)


def main():
    """Main monitor loop"""
    sessions = get_active_sessions()
    window = create_window(sessions)
    auto_refresh = False

    while True:
        # Timeout of 5000ms (5s) if auto-refresh is enabled, None otherwise
        timeout = 5000 if auto_refresh else None
        event, values = window.read(timeout=timeout)

        if event in (sg.WIN_CLOSED, 'Close'):
            break

        if event == 'refresh' or (auto_refresh and event == sg.TIMEOUT_KEY):
            sessions = get_active_sessions()
            window.close()
            window = create_window(sessions)
            if auto_refresh:
                window['auto_toggle'].update(value=True)

        if event == 'auto_refresh' or event == 'auto_toggle':
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
