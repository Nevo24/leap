#!/bin/bash
#
# Leap Hook Script for CLI providers (Claude Code, Codex, Cursor Agent, Gemini CLI, etc.)
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
# Gemini CLI includes prompt, prompt_response, and transcript_path.
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
[ -z "$STATE" ] && echo '{}' && exit 0

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

# Non-Leap sessions: exit silently (echo '{}' for CLIs that expect JSON stdout)
[ -z "$LEAP_TAG" ] && echo '{}' && exit 0
[ -z "$LEAP_SIGNAL_DIR" ] && echo '{}' && exit 0

SIGNAL_FILE="$LEAP_SIGNAL_DIR/$LEAP_TAG.signal"

# All processing in Python — reads stdin with timeout to handle CLIs
# that may not close stdin promptly (e.g. Codex).
"$PYTHON" -c "
import json, sys, os, threading, time

state = sys.argv[1]
signal_file = sys.argv[2]
leap_tag = os.environ.get('LEAP_TAG', '')
leap_signal_dir = os.environ.get('LEAP_SIGNAL_DIR', '')

signal = {'state': state}

# Write signal file IMMEDIATELY so the Leap server detects the state
# change without waiting for stdin/transcript reading.  The file is
# updated later with the assistant message text for Slack.
with open(signal_file, 'w') as f:
    json.dump(signal, f)


def _record_claude_session(transcript_path, session_cwd):
    '''Record the Claude session UUID for this Leap tag so that
    \`leap --resume\` can offer it later.  Silently ignores failures —
    this is best-effort bookkeeping, not critical path.
    '''
    if not (leap_tag and leap_signal_dir and transcript_path):
        return
    try:
        session_id = os.path.basename(transcript_path)
        if session_id.endswith('.jsonl'):
            session_id = session_id[:-6]
        if not session_id:
            return
        # .storage is the parent of the signal dir (sockets/)
        storage_dir = os.path.dirname(leap_signal_dir.rstrip('/'))
        sessions_dir = os.path.join(storage_dir, 'cli_sessions', 'claude')
        os.makedirs(sessions_dir, exist_ok=True)
        tag_file = os.path.join(sessions_dir, leap_tag + '.json')
        entries = []
        if os.path.isfile(tag_file):
            try:
                with open(tag_file) as f:
                    entries = json.load(f)
            except (json.JSONDecodeError, OSError):
                entries = []
            if not isinstance(entries, list):
                entries = []
        # Dedupe by session_id so the latest entry always wins
        entries = [e for e in entries if isinstance(e, dict) and e.get('session_id') != session_id]
        entries.append({
            'session_id': session_id,
            'transcript_path': transcript_path,
            'cwd': session_cwd or os.getcwd(),
            'last_seen': time.time(),
        })
        # Cap history per tag
        entries = entries[-20:]
        tmp = tag_file + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(entries, f, indent=2)
        os.replace(tmp, tag_file)
    except (OSError, ValueError):
        pass

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

    # Record the Claude session so \`leap --resume\` can offer it later.
    # Claude's transcript_path lives under ~/.claude/projects/<slug>/<uuid>.jsonl;
    # we use that marker to avoid recording for other CLIs (Codex/Cursor/Gemini)
    # until per-provider support is added.
    transcript_path = hook_data.get('transcript_path', '')
    if transcript_path and '.claude/projects/' in transcript_path:
        _record_claude_session(transcript_path, hook_data.get('cwd', ''))

    # If we already have the message from the hook payload, skip
    # transcript reading entirely (faster, and avoids format mismatches).
    if not direct_msg:
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

# Output empty JSON for CLIs that expect stdout response (e.g. Gemini CLI).
# Harmless for CLIs that ignore hook stdout (Claude, Codex, Cursor).
echo '{}'

exit 0
