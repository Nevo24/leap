#!/bin/bash
#
# ClaudeQ PTY - Main launcher
# Auto-detects whether to start server or client
# Uses Poetry venv Python
#

# Use Poetry venv Python if available, otherwise fall back to system python3
if [ -n "$CLAUDEQ_PYTHON" ]; then
    PYTHON_CMD="$CLAUDEQ_PYTHON"
else
    PYTHON_CMD="python3"
fi

# Show help if requested
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    cat << 'EOF'
ClaudeQ - Multi-session Claude Code with message queueing

USAGE:
    cq <tag>                     Start server or connect as client
    cq <tag> <message>           Send message to server
    cq <tag> [--flags]           Start server with flags (passed to Claude CLI)
    cq --help, -h                Show this help

FLAGS (server only):
    Flags starting with -- are passed directly to Claude CLI when starting a server.
    They are NOT supported for clients (connecting to existing server).

    Example:
        cq my-tag --dangerously-skip-permissions

EXAMPLES:
    # Terminal 1 (start server)
    cq my-feature

    # Terminal 2 (connect as client and queue messages)
    cq my-feature
    You: How do I fix this bug?
    You: :ip Explain this screenshot

    # Send message directly
    cq my-feature "What is this error?"

CLIENT COMMANDS (when connected as interactive client):
    <message>           Queue message (auto-sends when ready)
    :ip <msg>           Queue with clipboard image
    :d <msg>            Send directly (bypass queue)
    :d :ip <msg>        Send directly with image
    :f                  Force-send next queued message
    :l                  Show queue
    :c                  Clear queue
    :status             Server status
    :x                  Exit client

OTHER COMMANDS:
    cq-mo               Launch monitor GUI
    cq-cleanup          Remove dead sessions

JETBRAINS USERS:
    For automatic tab titles, enable these settings:
    1. Settings → Tools → Terminal → Engine: Classic
    2. Advanced Settings → Terminal → ☑ 'Show application title'

For more info: https://github.com/nevo24/claudeq
EOF
    exit 0
fi

if [ $# -lt 1 ]; then
    echo "Usage: cq <tag> [message...]"
    echo ""
    echo "First terminal (server): cq test"
    echo "Other terminals (client): cq test 'your message'"
    echo ""
    echo "For more info: cq --help"
    exit 1
fi

TAG="$1"

# Validate tag doesn't start with "-"
if [[ "$TAG" == -* ]]; then
    echo "Error: Tag cannot start with '-'" >&2
    echo "Usage: cq <tag> [message...]" >&2
    echo "For help: cq --help" >&2
    exit 1
fi

shift

# Parse arguments to separate flags from messages
# Flags (starting with --) are passed to server only
# Messages are passed to client only
FLAGS=()
ARGS=()
while [ $# -gt 0 ]; do
    if [[ "$1" == --* ]]; then
        FLAGS+=("$1")
    else
        ARGS+=("$1")
    fi
    shift
done

# Restore positional parameters with non-flag arguments
set -- "${ARGS[@]}"

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

SOCKET_PATH="$HOME/.claude-sockets/${TAG}.sock"
SERVER_SCRIPT="$SCRIPT_DIR/claudeq-server.py"
CLIENT_SCRIPT="$SCRIPT_DIR/claudeq-client.py"
SOCKET_DIR="$HOME/.claude-sockets"
QUEUE_DIR="$HOME/.claude-queues"

# Auto-cleanup dead sockets (silent, runs in background)
cleanup_dead_sockets() {
    if [ -d "$SOCKET_DIR" ]; then
        for sock in "$SOCKET_DIR"/*.sock; do
            [ -e "$sock" ] || continue
            local tag=$(basename "$sock" .sock)

            # Check if server process is running for this tag
            if ! ps aux | grep -E "claudeq-server.py $tag\$" | grep -v grep > /dev/null 2>&1; then
                # No server process - socket is dead, remove it silently
                rm -f "$sock" 2>/dev/null
                rm -f "$QUEUE_DIR/$tag.queue" 2>/dev/null
                rm -f "$SOCKET_DIR/$tag.meta" 2>/dev/null
                rm -f "$SOCKET_DIR/$tag.client.lock" 2>/dev/null
            fi
        done
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

        # Flags are silently ignored for clients (only used by server)

        # Set terminal tab name
        echo -ne "\033]0;cq-client ${TAG}\007"
        exec "$CLIENT_SCRIPT" "$TAG" "$@"
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
    echo "  Terminal 1: cq $TAG"
    echo "  Terminal 2: cq $TAG 'your message'"
    exit 1
fi

# No arguments and no server - start server
# Set terminal tab name
echo -ne "\033]0;cq-server ${TAG}\007"
exec "$SERVER_SCRIPT" "$TAG" "${FLAGS[@]}"
