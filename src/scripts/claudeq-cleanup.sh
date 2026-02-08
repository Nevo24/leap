#!/bin/bash
#
# ClaudeQ Cleanup - Remove dead/stale sessions
#

# Find the storage directory (in project root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
STORAGE_DIR="$PROJECT_DIR/.storage"
SOCKET_DIR="$STORAGE_DIR/sockets"
QUEUE_DIR="$STORAGE_DIR/queues"

echo "🧹 Cleaning up dead ClaudeQ sessions..."
echo ""

removed_count=0

if [ -d "$SOCKET_DIR" ]; then
    for socket_file in "$SOCKET_DIR"/*.sock; do
        [ -e "$socket_file" ] || continue

        tag=$(basename "$socket_file" .sock)

        # Check if server process is running for this tag (allow flags after tag)
        if ps aux | grep -E "claudeq-server.py $tag(\s|$)" | grep -v grep > /dev/null 2>&1; then
            # Server process exists - socket is alive, skip it
            continue
        fi

        # No server process - socket is dead, remove it
        echo "  Removing dead session: $tag"
        rm -f "$socket_file"
        rm -f "$QUEUE_DIR/$tag.queue" 2>/dev/null
        rm -f "$SOCKET_DIR/$tag.meta" 2>/dev/null
        rm -f "$SOCKET_DIR/$tag.client.lock" 2>/dev/null
        ((removed_count++))
    done
fi

echo ""
if [ $removed_count -eq 0 ]; then
    echo "✓ No dead sessions found"
else
    echo "✓ Removed $removed_count dead session(s)"
fi
