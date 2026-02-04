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

if [ $# -lt 1 ]; then
    echo "Usage: claudeq-main-pty <tag> [message...]"
    echo ""
    echo "First terminal (server): claudeq-main-pty test"
    echo "Other terminals (client): claudeq-main-pty test 'your message'"
    exit 1
fi

TAG="$1"

# Validate tag doesn't start with "-"
if [[ "$TAG" == -* ]]; then
    echo "Error: Tag cannot start with '-'" >&2
    echo "Usage: claudeq-main-pty <tag> [message...]" >&2
    exit 1
fi

shift

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

SOCKET_PATH="$HOME/.claude-sockets/${TAG}.sock"
SERVER_SCRIPT="$SCRIPT_DIR/claudeq-server.py"
CLIENT_SCRIPT="$SCRIPT_DIR/claudeq-client.py"

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
exec "$SERVER_SCRIPT" "$TAG"
