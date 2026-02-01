#!/bin/bash
#
# ClaudeQ Auto - Automatically start server or client based on session existence
# Usage: claudeq <tag> [flags]
#

# Check if tag provided
if [ -z "$1" ]; then
    echo "Usage: claudeq <tag> [flags]"
    echo ""
    echo "Examples:"
    echo "  claudeq backend                    # Starts server or connects as client"
    echo "  claudeq backend --verbose          # Pass flags to Claude (server only)"
    echo "  claudeq frontend --some-flag       # Flags only apply in server mode"
    echo ""
    echo "This automatically determines whether to start a new Claude session"
    echo "or connect to an existing one."
    exit 1
fi

TAG="$1"
shift  # Remove tag from arguments
CLAUDE_FLAGS="$@"  # Remaining arguments are flags for Claude
SESSION_NAME="claude-$TAG"

# Check if tmux is installed
if ! command -v tmux &> /dev/null; then
    echo "Error: tmux is required but not installed"
    echo "Install with: brew install tmux"
    exit 1
fi

# Check if session exists AND Claude is actually running in it
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    # Session exists - check if anyone is attached to it
    # If session is detached (no clients), it means the tab was closed - kill and restart
    ATTACHED_CLIENTS=$(tmux list-clients -t "$SESSION_NAME" 2>/dev/null | wc -l | tr -d ' ')

    if [ "$ATTACHED_CLIENTS" = "0" ]; then
        echo "Session '$TAG' exists but is detached (tab was closed) - cleaning up..."
        tmux kill-session -t "$SESSION_NAME" 2>/dev/null
        echo "Starting fresh SERVER mode"
        echo ""
        exec ~/.local/bin/claudeq-server.sh "$TAG" $CLAUDE_FLAGS
    fi

    # iTerm2 fix: Check if attached clients have valid terminals
    # Sometimes iTerm2 doesn't properly disconnect, leaving stale clients
    CURRENT_TTY=$(tty 2>/dev/null)
    CLIENT_TTYS=$(tmux list-clients -t "$SESSION_NAME" -F '#{client_tty}' 2>/dev/null)
    VALID_CLIENT_COUNT=0

    for tty in $CLIENT_TTYS; do
        # Check if this tty is the current terminal (we're trying to connect)
        if [ "$tty" = "$CURRENT_TTY" ]; then
            continue
        fi

        # Extract just the tty name (e.g., "ttys029" from "/dev/ttys029")
        tty_name=$(basename "$tty")

        # Check if there are any active processes using this TTY
        active_procs=$(ps -t "$tty_name" -o pid= 2>/dev/null | wc -l | tr -d ' ')

        if [ "$active_procs" -gt 0 ]; then
            VALID_CLIENT_COUNT=$((VALID_CLIENT_COUNT + 1))
        fi
    done

    if [ "$VALID_CLIENT_COUNT" = "0" ]; then
        echo "Session '$TAG' has stale clients - cleaning up..."
        tmux kill-session -t "$SESSION_NAME" 2>/dev/null
        echo "Starting fresh SERVER mode"
        echo ""
        exec ~/.local/bin/claudeq-server.sh "$TAG" $CLAUDE_FLAGS
    fi

    # Session exists and has clients - check if Claude process is actually running

    # Get the PID of the process running in the pane
    PANE_PID=$(tmux display-message -t "$SESSION_NAME" -p '#{pane_pid}' 2>/dev/null)

    # Check if Claude is running (the pane PID itself might BE Claude due to exec)
    if [ -n "$PANE_PID" ]; then
        # Check if the process is actually alive (not just a stale command name)
        if ps -p "$PANE_PID" > /dev/null 2>&1; then
            # Process exists, check its command
            PANE_CMD=$(ps -o command= -p "$PANE_PID" 2>/dev/null)
            # Also check process state (R=running, S=sleeping, Z=zombie, etc)
            PANE_STATE=$(ps -o state= -p "$PANE_PID" 2>/dev/null | tr -d ' ')

            # Check if it's Claude and in a running/sleeping state (not zombie/stopped)
            if echo "$PANE_CMD" | grep -E "claude" | grep -vE "claude-client|claude-auto|claude-with-tag" | grep -q .; then
                if [[ "$PANE_STATE" =~ ^[RSI] ]]; then
                    # Process is alive - verify Claude TUI is actually showing
                    PANE_CONTENT=$(tmux capture-pane -t "$SESSION_NAME" -p 2>/dev/null)

                    # Check for Claude-specific TUI elements
                    if echo "$PANE_CONTENT" | grep -qE "Claude Code|❯.*Try|bypass permissions|⏵⏵"; then
                        # Claude TUI is visible - start CLIENT
                        echo "Session '$TAG' is running (Claude TUI active) - starting CLIENT mode"
                        echo ""
                        # Warn if flags were provided (they only apply to server mode)
                        if [ -n "$CLAUDE_FLAGS" ]; then
                            echo "⚠️  Note: Flags '$CLAUDE_FLAGS' are ignored in CLIENT mode."
                            echo "   Flags only apply when starting a new SERVER session."
                            echo ""
                        fi
                        exec ~/.local/bin/claudeq-client.py "$TAG"
                    fi
                fi
            fi
        fi
    fi

    # Session exists but Claude is not running - clean up
    echo "Session '$TAG' exists but Claude is not running - cleaning up..."
    tmux kill-session -t "$SESSION_NAME" 2>/dev/null
    echo "Starting fresh SERVER mode"
    echo ""
    exec ~/.local/bin/claudeq-server.sh "$TAG" $CLAUDE_FLAGS
else
    # Session doesn't exist - start SERVER
    echo "Session '$TAG' doesn't exist - starting SERVER mode"
    echo ""
    exec ~/.local/bin/claudeq-server.sh "$TAG" $CLAUDE_FLAGS
fi
