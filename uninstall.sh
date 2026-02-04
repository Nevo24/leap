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

    # Check for current installation with proper markers
    if grep -q "ClaudeQ Configuration START" "$RC_FILE" 2>/dev/null; then
        # Backup RC file
        cp "$RC_FILE" "$RC_FILE.backup-uninstall-$(date +%Y%m%d-%H%M%S)"

        # Remove ClaudeQ configuration block (from START marker to END marker)
        # This removes everything between markers, even if user modified content
        # As long as the marker lines are intact, uninstall will work
        sed -i.bak '/ClaudeQ Configuration START/,/ClaudeQ Configuration END/d' "$RC_FILE"
        rm -f "$RC_FILE.bak"

        echo -e "${GREEN}✓ Configuration removed from $RC_FILE${NC}"
        echo "  (Backup saved)"
    # Check for legacy ClaudeQ markers
    elif grep -q "# ClaudeQ" "$RC_FILE" 2>/dev/null; then
        echo -e "${YELLOW}⚠ Found legacy ClaudeQ installation${NC}"
        cp "$RC_FILE" "$RC_FILE.backup-uninstall-$(date +%Y%m%d-%H%M%S)"

        # Try to remove legacy format (multiple patterns for compatibility)
        sed -i.bak '/# ClaudeQ/,/# End ClaudeQ/d' "$RC_FILE"
        sed -i.bak '/# ClaudeQ/,/^alias cq/d' "$RC_FILE"
        rm -f "$RC_FILE.bak"

        echo -e "${GREEN}✓ Configuration removed from $RC_FILE${NC}"
        echo "  (Backup saved)"
    else
        echo "  No ClaudeQ configuration found in $RC_FILE"
    fi
fi

# Remove queue data and sockets
echo ""
echo "🧹 Cleaning up data directories..."
rm -rf ~/.claude-queues/ ~/.claude-sockets/
echo -e "${GREEN}✓ Data directories removed${NC}"

# Remove Poetry venv
echo ""
echo "🧹 Removing Poetry virtual environment..."
if command -v poetry &> /dev/null; then
    cd "$(dirname "$0")" && poetry env remove --all 2>/dev/null || true
    echo -e "${GREEN}✓ Poetry venv removed${NC}"
else
    echo -e "${YELLOW}⚠ Poetry not found, skipping venv removal${NC}"
fi

# Remove cache directories
echo ""
echo "🧹 Cleaning up cache directories..."
SCRIPT_DIR="$(dirname "$0")"
rm -rf "$SCRIPT_DIR/.pytest_cache" "$SCRIPT_DIR/.coverage" "$SCRIPT_DIR/coverage.xml" \
       "$SCRIPT_DIR/.ruff_cache" "$SCRIPT_DIR/.mypy_cache"
echo -e "${GREEN}✓ Cache directories removed${NC}"

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
echo -e "${GREEN}✓ ClaudeQ fully uninstalled!${NC}"
echo "================================================"
echo "Project is now in clean state (like just cloned)"
echo ""
echo "Please restart your terminal or run:"
echo "  source $RC_FILE"
echo ""
