#!/bin/bash
#
# ClaudeQ Hook Script for Claude Code
#
# Called by Claude Code's hooks system on Stop and Notification events.
# Writes state (and response text) to a signal file that the CQ server reads.
#
# The state is passed as the first argument by the hook configuration:
#   claudeq-hook.sh idle             (Stop hook)
#   claudeq-hook.sh needs_permission (Notification/permission_prompt)
#   claudeq-hook.sh has_question     (Notification/elicitation_dialog)
#
# Claude Code passes JSON on stdin with session info including
# transcript_path. For Stop hooks, we read the last assistant message
# from the transcript JSONL file (used by Slack integration).
#
# Environment variables (set by CQ server via PTY):
#   CQ_TAG        - Session tag name
#   CQ_SIGNAL_DIR - Directory for signal files
#

# Non-CQ sessions: exit silently
[ -z "$CQ_TAG" ] && exit 0
[ -z "$CQ_SIGNAL_DIR" ] && exit 0

STATE="$1"
[ -z "$STATE" ] && exit 0

SIGNAL_FILE="$CQ_SIGNAL_DIR/$CQ_TAG.signal"

# Read stdin JSON from Claude Code into a temp file
TMPFILE=$(mktemp)
cat > "$TMPFILE"

# Extract last assistant message from transcript and write signal file
python3 -c "
import json, sys

state = sys.argv[1]
signal_file = sys.argv[2]
tmp_file = sys.argv[3]

signal = {'state': state}

try:
    with open(tmp_file) as f:
        hook_data = json.load(f)

    transcript_path = hook_data.get('transcript_path', '')

    if transcript_path:
        # Read the transcript JSONL and find the last assistant message
        last_msg = ''
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get('type') == 'assistant':
                        parts = []
                        for c in entry.get('message', {}).get('content', []):
                            if c.get('type') == 'text':
                                parts.append(c['text'])
                        if parts:
                            last_msg = '\n'.join(parts)
                except (json.JSONDecodeError, KeyError):
                    continue
        if last_msg:
            signal['last_assistant_message'] = last_msg
except Exception:
    pass

with open(signal_file, 'w') as f:
    json.dump(signal, f)
" "$STATE" "$SIGNAL_FILE" "$TMPFILE" 2>/dev/null

# Fallback if python fails
if [ $? -ne 0 ]; then
    echo "{\"state\":\"$STATE\"}" > "$SIGNAL_FILE"
fi

rm -f "$TMPFILE"
exit 0
