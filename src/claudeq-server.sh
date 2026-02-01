#!/bin/bash
#
# ClaudeQ Server - Start Claude in a tmux session with tag
# Usage: claudeq <tag> [flags]
#

# Find Claude binary
CLAUDE_PATH=""

# Try common locations (avoid shell lookups to prevent picking up aliases)
for path in \
    "$HOME/.nvm/versions/node/v20.19.6/bin/claude" \
    "/usr/local/bin/claude" \
    "/opt/homebrew/bin/claude" \
    "$HOME/.local/bin/claude"; do

    if [ -f "$path" ] && [ -x "$path" ]; then
        CLAUDE_PATH="$path"
        break
    fi
done

# If still not found, error
if [ -z "$CLAUDE_PATH" ]; then
    echo "Error: Claude CLI not found!"
    echo ""
    echo "Please install Claude CLI first:"
    echo "  Visit: https://docs.anthropic.com/en/docs/claude-code/getting-started"
    echo ""
    echo "Or if already installed, set CLAUDE_PATH environment variable:"
    echo "  export CLAUDE_PATH=/path/to/claude"
    exit 1
fi

# Check if tag provided
if [ -z "$1" ]; then
    echo "Usage: claudeq <tag> [flags]"
    echo ""
    echo "Examples:"
    echo "  claudeq my-cool-feature"
    echo "  claudeq my-cool-feature --verbose"
    echo "  claudeq my-cool-feature --some-flag"
    echo ""
    echo "Flags are passed directly to the Claude CLI."
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

# Set terminal tab title
printf "\033]0;claude-server $TAG\007"

# Print banner with ASCII art
cat << 'EOF'
   _____ _                 _       ___
  / ____| |               | |     / _ \
 | |    | | __ _ _   _  __| | ___| | | |
 | |    | |/ _` | | | |/ _` |/ _ \ | | |
 | |____| | (_| | |_| | (_| |  __/ |_| |
  \_____|_|\__,_|\__,_|\__,_|\___|\___\Q

EOF
cat << EOF
======================================================================
  SERVER - Session: $TAG
======================================================================
  Starting Claude with tag: $TAG

  To send messages from another tab, run:
    claudeq $TAG

  All responses will appear HERE in this window.

  Copy text: Select with mouse and copy normally
  Scroll back: Ctrl+B then [ (arrow keys to scroll, q to exit)
  Detach: Ctrl+B then D (session keeps running)
======================================================================

EOF

sleep 1

# Check if session already exists
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "Reattaching to existing session: $TAG"
    echo ""
    sleep 1
    # Attach to existing session
    exec tmux attach-session -t "$SESSION_NAME"
else
    echo "Creating new session: $TAG"
    echo ""
    sleep 1
    # Create new session with Claude and attach (not detached)
    # Set destroy-unattached and detach-on-destroy for proper cleanup with iTerm2
    # The \; separates tmux commands - both execute before attaching
    exec tmux new-session -s "$SESSION_NAME" "$CLAUDE_PATH" $CLAUDE_FLAGS \; \
        set-option -t "$SESSION_NAME" destroy-unattached on \; \
        set-option -t "$SESSION_NAME" detach-on-destroy on
fi
