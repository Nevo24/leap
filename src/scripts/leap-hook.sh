#!/bin/bash
#
# Leap Hook Script for CLI providers (Claude Code, Codex, Cursor Agent, etc.)
#
# Called by CLI hooks on Stop and Notification events.
# Writes state (and response text) to a signal file that the Leap server reads.
#
# The state is passed as the first argument by the hook configuration:
#   leap-hook.sh idle             (Stop hook)
#   leap-hook.sh needs_permission (Notification/permission_prompt)
#   leap-hook.sh needs_input      (Notification/elicitation_dialog)
#
# The CLI passes JSON on stdin with session info.  Claude Code includes
# transcript_path; Codex includes last_assistant_message directly.
# Cursor Agent includes status and workspace_roots.
#
# Environment variables (set by Leap server via PTY):
#   LEAP_TAG        - Session tag name
#   LEAP_SIGNAL_DIR - Directory for signal files
#
# Fallback: if env vars are missing (some CLIs don't pass the parent
# environment to hook subprocesses), the script looks for a PID mapping
# file in /tmp written by the Leap server (leap_cli_pid_<PID>.json).
#

STATE="$1"
[ -z "$STATE" ] && exit 0

# Use venv Python if available (set by Leap server), fall back to PATH python3.
# Homebrew-only installs may not have python3 in PATH inside CLI subshells.
PYTHON="${LEAP_PYTHON:-python3}"

# If LEAP_TAG or LEAP_SIGNAL_DIR are missing, try to resolve them from the
# PID mapping file.  The Leap server writes /tmp/leap_cli_pid_<PID>.json
# after spawning the CLI process.  Walk up the parent PID chain to find it.
if [ -z "$LEAP_TAG" ] || [ -z "$LEAP_SIGNAL_DIR" ]; then
    RESOLVED=$("$PYTHON" -c "
import json, os, subprocess

def get_ppid(pid):
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('PPid:'):
                    return int(line.split()[1])
    except (FileNotFoundError, OSError):
        pass
    try:
        r = subprocess.run(['ps', '-o', 'ppid=', '-p', str(pid)],
                           capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            return int(r.stdout.strip())
    except Exception:
        pass
    return None

pid = os.getpid()
for _ in range(10):
    ppid = get_ppid(pid)
    if ppid is None or ppid <= 1:
        break
    path = f'/tmp/leap_cli_pid_{ppid}.json'
    if os.path.isfile(path):
        try:
            d = json.loads(open(path).read())
            tag, sd = d.get('tag',''), d.get('signal_dir','')
            py = d.get('python','')
            if tag and sd:
                print(f'{tag}|{sd}|{py}')
                break
        except Exception:
            pass
    pid = ppid
" < /dev/null 2>/dev/null)

    if [ -n "$RESOLVED" ]; then
        IFS='|' read -r _TAG _DIR _PY <<< "$RESOLVED"
        LEAP_TAG="$_TAG"
        LEAP_SIGNAL_DIR="$_DIR"
        [ -n "$_PY" ] && PYTHON="$_PY"
        export LEAP_TAG LEAP_SIGNAL_DIR
    fi
fi

# Non-Leap sessions: exit silently
[ -z "$LEAP_TAG" ] && exit 0
[ -z "$LEAP_SIGNAL_DIR" ] && exit 0

SIGNAL_FILE="$LEAP_SIGNAL_DIR/$LEAP_TAG.signal"

# All processing in Python — reads stdin with timeout to handle CLIs
# that may not close stdin promptly (e.g. Codex).
"$PYTHON" -c "
import json, sys, os, threading

state = sys.argv[1]
signal_file = sys.argv[2]

signal = {'state': state}

# Write signal file IMMEDIATELY so the Leap server detects the state
# change without waiting for stdin/transcript reading.  The file is
# updated later with the assistant message text for Slack.
with open(signal_file, 'w') as f:
    json.dump(signal, f)

# Read stdin with timeout — some CLIs (e.g. Codex) may not close stdin
# promptly, which would hang the hook.  5s is generous for JSON delivery.
stdin_content = ['']
def _read_stdin():
    try:
        stdin_content[0] = sys.stdin.read()
    except Exception:
        pass
reader = threading.Thread(target=_read_stdin, daemon=True)
reader.start()
reader.join(timeout=5)

try:
    hook_data = json.loads(stdin_content[0]) if stdin_content[0] else {}

    # Capture the notification message (permission question / dialog text)
    notification_msg = hook_data.get('message', '')
    if notification_msg:
        signal['notification_message'] = notification_msg

    # Check for last_assistant_message directly in the hook payload.
    # Codex provides this field; Claude Code does not (uses transcript).
    direct_msg = hook_data.get('last_assistant_message', '')
    if direct_msg:
        signal['last_assistant_message'] = direct_msg

    # If we already have the message from the hook payload, skip
    # transcript reading entirely (faster, and avoids format mismatches).
    if not direct_msg:
        transcript_path = hook_data.get('transcript_path', '')

        if transcript_path:
            # Read the transcript from the END to find the last assistant
            # message efficiently.  Seek backwards in chunks instead of
            # reading the entire file from the start.
            last_msg = ''
            file_size = os.path.getsize(transcript_path)
            chunk_size = 32768  # 32 KB — covers most assistant messages
            with open(transcript_path, 'rb') as f:
                start = max(0, file_size - chunk_size)
                f.seek(start)
                tail = f.read()
            # Parse lines from the tail chunk (Claude Code JSONL format)
            for raw_line in reversed(tail.split(b'\n')):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                    if entry.get('type') == 'assistant':
                        parts = []
                        for c in entry.get('message', {}).get('content', []):
                            if c.get('type') == 'text':
                                parts.append(c['text'])
                        if parts:
                            last_msg = '\n'.join(parts)
                            break
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
            if last_msg:
                signal['last_assistant_message'] = last_msg
except Exception:
    pass

with open(signal_file, 'w') as f:
    json.dump(signal, f)
" "$STATE" "$SIGNAL_FILE" 2>/dev/null

# Fallback if python fails
if [ $? -ne 0 ]; then
    echo "{\"state\":\"$STATE\"}" > "$SIGNAL_FILE"
fi

exit 0
