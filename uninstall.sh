#!/bin/bash
#
# ClaudeQ Uninstallation Script
#

set -e

echo "================================================"
echo "  ClaudeQ Uninstaller"
echo "================================================"
echo ""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

INSTALL_DIR="$HOME/.local/bin"

# Detect shell
SHELL_NAME=$(basename "$SHELL")
if [ "$SHELL_NAME" = "zsh" ]; then
    RC_FILE="$HOME/.zshrc"
elif [ "$SHELL_NAME" = "bash" ]; then
    RC_FILE="$HOME/.bashrc"
else
    RC_FILE=""
fi

echo "This will remove:"
echo "  - ClaudeQ configuration from $RC_FILE"
echo "  - Queue data from ~/.claude-queues/"
echo ""
echo "Note: The ClaudeQ project files will remain in place."
echo "      Delete the project directory manually if desired."
echo ""
read -p "Continue? (y/N) " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Uninstallation cancelled."
    exit 0
fi

# Remove shell configuration
if [ -n "$RC_FILE" ] && [ -f "$RC_FILE" ]; then
    echo ""
    echo "🧹 Removing shell configuration..."

    if grep -q "# ClaudeQ - Multi-session Claude" "$RC_FILE" 2>/dev/null; then
        # Backup RC file
        cp "$RC_FILE" "$RC_FILE.backup-uninstall-$(date +%Y%m%d-%H%M%S)"

        # Remove ClaudeQ configuration
        sed -i.bak '/# ClaudeQ - Multi-session Claude/,/^alias claude_client=/d' "$RC_FILE"
        rm -f "$RC_FILE.bak"

        echo -e "${GREEN}✓ Configuration removed from $RC_FILE${NC}"
    else
        echo "  No ClaudeQ configuration found in $RC_FILE"
    fi
fi

# Remove queue data
echo ""
read -p "Remove queue data from ~/.claude-queues/? (y/N) " -n 1 -r
echo

if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf ~/.claude-queues/
    echo -e "${GREEN}✓ Queue data removed${NC}"
else
    echo "  Queue data preserved"
fi

# Kill any running ClaudeQ sessions
echo ""
echo "🔍 Checking for running ClaudeQ sessions..."
CLAUDE_SESSIONS=$(tmux list-sessions 2>/dev/null | grep "^claude-" | cut -d: -f1 || true)

if [ -n "$CLAUDE_SESSIONS" ]; then
    echo "Found sessions:"
    echo "$CLAUDE_SESSIONS" | sed 's/^/  - /'
    echo ""
    read -p "Kill these sessions? (y/N) " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "$CLAUDE_SESSIONS" | while read session; do
            tmux kill-session -t "$session" 2>/dev/null || true
        done
        echo -e "${GREEN}✓ Sessions terminated${NC}"
    fi
else
    echo "  No ClaudeQ sessions found"
fi

echo ""
echo "================================================"
echo -e "${GREEN}✓ ClaudeQ uninstalled successfully${NC}"
echo "================================================"
echo ""
echo "Please restart your terminal or run:"
echo "  source $RC_FILE"
echo ""
