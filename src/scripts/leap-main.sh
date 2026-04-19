#!/bin/bash
#
# Leap PTY - Main launcher
# Auto-detects whether to start server or client
# Uses Poetry venv Python
#

# Find and enforce virtualenv Python usage (NEVER use system python3)
# Primary source: .storage/venv-path file (written by make install)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
STORAGE_DIR="$PROJECT_DIR/.storage"
VENV_PATH_FILE="$STORAGE_DIR/venv-path"

if [ -f "$VENV_PATH_FILE" ]; then
    # Read virtualenv path from file (most reliable, always current)
    VENV_BASE=$(cat "$VENV_PATH_FILE")
    PYTHON_CMD="$VENV_BASE/bin/python3"

    if [ ! -x "$PYTHON_CMD" ]; then
        echo "❌ Error: Python not found at $PYTHON_CMD" >&2
        echo "   The venv-path file exists but points to an invalid location." >&2
        echo "   Fix: Run 'make install' in the Leap project directory" >&2
        exit 1
    fi
elif [ -n "$LEAP_PYTHON" ] && [ -x "$LEAP_PYTHON" ]; then
    # Fallback: Use LEAP_PYTHON from .zshrc (legacy support)
    PYTHON_CMD="$LEAP_PYTHON"
else
    # FAIL: No valid Python found
    echo "❌ Error: Leap virtualenv not found!" >&2
    echo "" >&2
    echo "   Missing .storage/venv-path file in project directory." >&2
    echo "   This file is created automatically by 'make install'." >&2
    echo "" >&2
    echo "   Fix: Run 'make install' in: $PROJECT_DIR" >&2
    exit 1
fi

# Run update if requested
if [ "$1" = "--update" ]; then
    exec "$SCRIPT_DIR/leap-update.sh" --skip-if-current "$PROJECT_DIR"
fi

# Manage CLI order if requested
if [ "$1" = "--manage-clis" ]; then
    PYTHONPATH="$PROJECT_DIR/src:${PYTHONPATH:-}" \
        "$PYTHON_CMD" "$PROJECT_DIR/src/scripts/leap-manage-clis.py"
    exit $?
fi

# Resume picker: show tags with recorded Claude sessions, then relaunch
# leap-main.sh with the chosen tag + LEAP_CLAUDE_RESUME_ID env var.
# The picker script handles chdir'ing into the session's original cwd.
if [ "$1" = "--resume" ]; then
    PYTHONPATH="$PROJECT_DIR/src:${PYTHONPATH:-}" \
        exec "$PYTHON_CMD" "$PROJECT_DIR/src/scripts/leap-resume.py"
fi

# Run Slack bot if requested
if [ "$1" = "--slack" ]; then
    shift
    # Set terminal tab name
    echo -ne "\033]0;leap slack-bot\007"
    # Single-instance lock (atomic mkdir, same pattern as server lock)
    SLACK_LOCK_DIR="$STORAGE_DIR/slack/slack-bot.lock"
    mkdir -p "$STORAGE_DIR/slack"
    if ! mkdir "$SLACK_LOCK_DIR" 2>/dev/null; then
        # Check who started it
        if [ -f "$SLACK_LOCK_DIR/source" ]; then
            SOURCE=$(cat "$SLACK_LOCK_DIR/source" 2>/dev/null)
        else
            SOURCE="another terminal"
        fi
        if [ "$SOURCE" = "monitor" ]; then
            echo "❌ Slack bot is already running from the Leap Monitor." >&2
        else
            echo "❌ Slack bot is already running in another terminal." >&2
        fi
        exit 1
    fi
    # Mark who started the bot (monitor sets LEAP_SLACK_SOURCE env var)
    echo "${LEAP_SLACK_SOURCE:-terminal}" > "$SLACK_LOCK_DIR/source"
    # No exec — keep the shell alive so the trap can clean up the lock dir
    trap 'rm -f "$SLACK_LOCK_DIR/source"; rmdir "$SLACK_LOCK_DIR" 2>/dev/null' EXIT INT TERM
    PYTHONPATH="$PROJECT_DIR/src:${PYTHONPATH:-}" \
        "$PYTHON_CMD" "$PROJECT_DIR/src/scripts/leap-slack.py" "$@"
    exit $?
fi

# Show help if requested
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    cat << 'EOF'
Leap - Multi-session AI CLI with message queueing

USAGE:
    leap                              Interactive CLI + session name selector
    leap <tag>                        Start server or connect as client
    leap <tag> [--flags]              Start server with flags (passed to CLI)
    leap --help, -h                   Show this help
    leap --update                     Update Leap to latest version
    leap --manage-clis                Manage CLI providers (order, flags, visibility, custom CLIs)
    leap --resume                     Pick a previous Leap session and resume it in the same CLI
                                      (supported for any CLI provider with leap-resume implemented —
                                      e.g. Claude Code, OpenAI Codex)
EOF
    if [ -f "$STORAGE_DIR/slack/config.json" ]; then
        echo "    leap --slack                      Start the Slack bot daemon"
    fi
    cat << 'EOF'

FLAGS (server only):
    Flags starting with -- are passed directly to the CLI when starting a server.
    They are NOT supported for clients (connecting to existing server).

    Example:
        leap my-tag --dangerously-skip-permissions

EXAMPLES:
    # Interactive selector (choose CLI + session name)
    leap

    # Start server for a specific session
    leap my-feature

    # Connect as client and queue messages
    leap my-feature
    You: How do I fix this bug?
    You: [Image #1] Explain this screenshot    # Ctrl+V to paste image

    # Send message directly
    leap my-feature "What is this error?"
EOF
    if [ -f "$STORAGE_DIR/slack/config.json" ]; then
        cat << 'EOF'

    # Start Slack bot daemon
    leap --slack
EOF
    fi
    cat << 'EOF'

For more info: https://github.com/nevo24/leap
EOF
    exit 0
fi

if [ $# -lt 1 ]; then
    echo "Usage: leap <tag> [message...]"
    echo ""
    echo "First terminal (server): leap test"
    echo "Other terminals (client): leap test 'your message'"
    echo ""
    echo "For more info: leap --help"
    exit 1
fi

TAG="$1"

# Validate tag: alphanumeric, hyphens, underscores only
if [[ ! "$TAG" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
    echo "Error: Tag must contain only letters, numbers, hyphens, and underscores" >&2
    echo "Usage: leap <tag> [message...]" >&2
    echo "For help: leap --help" >&2
    exit 1
fi

shift

# Parse arguments to separate flags from messages
# Flags (starting with --) are passed to server only
# Messages are passed to client only
# --cli <name> overrides LEAP_CLI env var (set by claude-leap-main.sh / codex-leap-main.sh / cursor-agent-leap-main.sh / gemini-leap-main.sh)
FLAGS=()
ARGS=()
CLI_FROM_ARG=""
while [ $# -gt 0 ]; do
    if [ "$1" = "--cli" ] && [ -n "$2" ]; then
        CLI_FROM_ARG="$2"
        FLAGS+=("--cli" "$2")
        shift 2
    elif [[ "$1" == --cli=* ]]; then
        CLI_FROM_ARG="${1#--cli=}"
        FLAGS+=("$1")
        shift
    elif [[ "$1" == --* ]]; then
        FLAGS+=("$1")
        shift
    else
        ARGS+=("$1")
        shift
    fi
done

# Apply LEAP_CLI env var if --cli was not explicitly passed
if [ -z "$CLI_FROM_ARG" ] && [ -n "$LEAP_CLI" ]; then
    FLAGS+=("--cli" "$LEAP_CLI")
fi

# Restore positional parameters with non-flag arguments
set -- "${ARGS[@]}"

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Add src directory to PYTHONPATH so leap package can be found
export PYTHONPATH="${SCRIPT_DIR}/..:${PYTHONPATH}"

# Storage paths (centralized in .storage folder at project root)
STORAGE_DIR="$PROJECT_DIR/.storage"
SOCKET_DIR="$STORAGE_DIR/sockets"
QUEUE_DIR="$STORAGE_DIR/queues"
SOCKET_PATH="$SOCKET_DIR/${TAG}.sock"
SERVER_SCRIPT="$SCRIPT_DIR/leap-server.py"
CLIENT_SCRIPT="$SCRIPT_DIR/leap-client.py"

# Ensure storage subdirectories exist (may be missing on first run after install)
mkdir -p "$SOCKET_DIR" "$QUEUE_DIR"

# Auto-cleanup dead sockets and orphaned locks (silent, runs in background)
cleanup_dead_sockets() {
    if [ -d "$SOCKET_DIR" ]; then
        for sock in "$SOCKET_DIR"/*.sock; do
            [ -e "$sock" ] || continue
            local tag=$(basename "$sock" .sock)

            # Check if server process is running for this tag (allow flags after tag)
            if ! ps aux | grep -E "leap-server.py $tag(\s|$)" | grep -v grep > /dev/null 2>&1; then
                # No server process - socket is dead, remove it silently
                rm -f "$sock" 2>/dev/null
                rm -f "$QUEUE_DIR/$tag.queue" 2>/dev/null
                rm -f "$SOCKET_DIR/$tag.meta" 2>/dev/null
                rm -f "$SOCKET_DIR/$tag.signal" 2>/dev/null
                rm -f "$SOCKET_DIR/$tag.client.lock" 2>/dev/null
                rmdir "$SOCKET_DIR/$tag.server.lock" 2>/dev/null
            fi
        done

        # Orphaned .client.lock files whose socket is already gone
        for lock in "$SOCKET_DIR"/*.client.lock; do
            [ -e "$lock" ] || continue
            local tag=$(basename "$lock" .client.lock)
            [ -S "$SOCKET_DIR/$tag.sock" ] && continue
            local pid=$(cat "$lock" 2>/dev/null)
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                continue
            fi
            rm -f "$lock" 2>/dev/null
        done

        # Orphaned .server.lock dirs whose socket is gone and no process running
        for lock_dir in "$SOCKET_DIR"/*.server.lock; do
            [ -d "$lock_dir" ] || continue
            local tag=$(basename "$lock_dir" .server.lock)
            [ -S "$SOCKET_DIR/$tag.sock" ] && continue
            if ps aux | grep -E "leap-server.py $tag(\s|$)" | grep -v grep > /dev/null 2>&1; then
                continue
            fi
            rmdir "$lock_dir" 2>/dev/null
        done
    fi

    # Orphaned Slack bot lock (no slack bot process running)
    local slack_lock="$STORAGE_DIR/slack/slack-bot.lock"
    if [ -d "$slack_lock" ]; then
        if ! ps aux | grep "leap-slack.py" | grep -v grep > /dev/null 2>&1; then
            rmdir "$slack_lock" 2>/dev/null
        fi
    fi
}

# Run cleanup in background to avoid delaying startup
cleanup_dead_sockets &

# Function to test if server is actually running
test_socket_alive() {
    # Use Python to test socket connection
    "$PYTHON_CMD" -c "
import socket
import sys
import os
socket_path = '$SOCKET_PATH'
try:
    if not os.path.exists(socket_path):
        print('Socket file does not exist', file=sys.stderr)
        sys.exit(1)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    s.connect(socket_path)
    s.close()
    print('Socket connection successful', file=sys.stderr)
    sys.exit(0)
except Exception as e:
    print(f'Socket connection failed: {e}', file=sys.stderr)
    sys.exit(1)
"
    return $?
}

# Check if socket exists and is alive
if [ -S "$SOCKET_PATH" ]; then
    # Socket file exists - test if server is actually running
    echo "🔍 Testing socket at $SOCKET_PATH..." >&2
    if test_socket_alive; then
        # Server is alive - launch client (interactive or with message)
        echo "✓ Server is running - launching client" >&2

        # Warn only if the user explicitly passed --cli and it doesn't
        # match the running server.  When connecting as a client without
        # --cli, the user didn't choose a CLI — no warning needed.
        if [ -n "$CLI_FROM_ARG" ]; then
            META_FILE="$SOCKET_DIR/${TAG}.meta"
            if [ -f "$META_FILE" ]; then
                SERVER_CLI=$("$PYTHON_CMD" -c "import json,sys; print(json.load(open('$META_FILE')).get('cli_provider','claude'))" 2>/dev/null || echo "claude")
                if [ "$CLI_FROM_ARG" != "$SERVER_CLI" ]; then
                    echo "" >&2
                    echo -e "  \033[33m⚠ Warning: Server '$TAG' is running with $SERVER_CLI, not $CLI_FROM_ARG\033[0m" >&2
                    echo "" >&2
                fi
            fi
        fi

        # Flags are silently ignored for clients (only used by server)

        # Set terminal tab name (OSC for native terminals; VS Code rename is done from Python)
        echo -ne "\033]0;lpc ${TAG}\007"
        exec "$PYTHON_CMD" "$CLIENT_SCRIPT" "$TAG" "$@"
    else
        # Stale socket - remove it and continue to server check below
        echo "🧹 Removing stale socket for '$TAG'" >&2
        rm -f "$SOCKET_PATH"
    fi
fi

# No socket or stale socket removed - decide server vs error
if [ $# -gt 0 ]; then
    # Has arguments but no server - error
    echo "Error: Server not running for tag '$TAG'"
    echo "Start server first in another terminal:"
    echo "  Terminal 1: leap $TAG"
    echo "  Terminal 2: leap $TAG 'your message'"
    exit 1
fi

# No arguments and no server - acquire exclusive lock before starting server.
# This prevents a race condition where two terminals start a server for the
# same tag simultaneously (e.g., double-clicking Server in the monitor).
# Uses mkdir which is atomic on all filesystems (works on macOS without flock).
LOCK_DIR="$SOCKET_DIR/${TAG}.server.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    # Another process is already starting a server for this tag.
    # Wait briefly for the socket to appear, then connect as client.
    echo "⏳ Another server is starting for '$TAG', waiting..." >&2
    for i in $(seq 1 20); do
        sleep 0.5
        if [ -S "$SOCKET_PATH" ] && test_socket_alive; then
            echo "✓ Server is now running - launching client" >&2
            echo -ne "\033]0;lpc ${TAG}\007"
            exec "$PYTHON_CMD" "$CLIENT_SCRIPT" "$TAG" "$@"
        fi
    done
    echo "❌ Timed out waiting for server '$TAG'" >&2
    # Clean up stale lock in case the first process died
    rmdir "$LOCK_DIR" 2>/dev/null
    exit 1
fi
# Lock acquired — we own server startup for this tag.
# Clean up the lock directory on exit (normal exit, SIGTERM, SIGINT).
trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT INT TERM

# Record tag in history for arrow-up recall
"$PYTHON_CMD" -c "
from pathlib import Path
STORAGE_DIR = Path('$STORAGE_DIR')
HISTORY_FILE = STORAGE_DIR / 'tag_history'
tag = '$TAG'
history = []
if HISTORY_FILE.exists():
    history = [l.strip() for l in HISTORY_FILE.read_text().strip().splitlines() if l.strip()]
history = [t for t in history if t != tag]
history.append(tag)
history = history[-50:]
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE.write_text('\n'.join(history) + '\n')
" 2>/dev/null

# Resume hand-off: when leap-resume.py sets LEAP_RESUME_SESSION_ID +
# LEAP_RESUME_CLI, we just let them pass through to leap-server.py in
# the environment — the server consults the matching CLIProvider
# (claude: `--resume=<id>`, codex: `resume <id>`, …) and prepends the
# right argv tokens before spawning the binary.  No provider-specific
# knowledge lives in this script.

# Start server
# Set terminal tab name (OSC for native terminals; VS Code rename is done from Python)
echo -ne "\033]0;lps ${TAG}\007"
exec "$PYTHON_CMD" "$SERVER_SCRIPT" "$TAG" "${FLAGS[@]}"
