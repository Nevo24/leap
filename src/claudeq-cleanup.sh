#!/bin/bash
#
# ClaudeQ Cleanup - Remove dead/stale sessions
#

SOCKET_DIR="$HOME/.claude-sockets"
QUEUE_DIR="$HOME/.claude-queues"

echo "🧹 Cleaning up dead ClaudeQ sessions..."
echo ""

removed_count=0

if [ -d "$SOCKET_DIR" ]; then
    for socket_file in "$SOCKET_DIR"/*.sock; do
        [ -e "$socket_file" ] || continue

        tag=$(basename "$socket_file" .sock)

        # Test if socket is alive
        if ! python3 -c "
import socket, sys
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    s.connect('$socket_file')
    s.close()
    sys.exit(0)
except:
    sys.exit(1)
" 2>/dev/null; then
            echo "  Removing dead session: $tag"
            rm -f "$socket_file"
            rm -f "$QUEUE_DIR/$tag.queue" 2>/dev/null
            ((removed_count++))
        fi
    done
fi

echo ""
if [ $removed_count -eq 0 ]; then
    echo "✓ No dead sessions found"
else
    echo "✓ Removed $removed_count dead session(s)"
fi
