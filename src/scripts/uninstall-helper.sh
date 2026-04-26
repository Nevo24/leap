#!/bin/bash
#
# Leap Uninstall Helper
# Called by: make uninstall
#
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

REPO_PATH="${1:-$(git rev-parse --show-toplevel 2>/dev/null)}"

# Get shell RC file
get_rc_file() {
    SHELL_NAME=$(basename "$SHELL")
    if [ "$SHELL_NAME" = "zsh" ]; then
        echo "$HOME/.zshrc"
    elif [ "$SHELL_NAME" = "bash" ]; then
        echo "$HOME/.bashrc"
    else
        echo ""
    fi
}

# Remove shell configuration
remove_shell_config() {
    local RC_FILE="$1"

    if [ ! -f "$RC_FILE" ]; then
        echo "  No RC file found at $RC_FILE"
        return
    fi

    if ! grep -qE "(Leap|ClaudeQ) Configuration" "$RC_FILE" 2>/dev/null; then
        echo "  No Leap or ClaudeQ configuration found in $RC_FILE"
        return
    fi

    echo "  Removing shell configuration from $RC_FILE..."

    # Remove Leap config (current naming)
    if grep -q "Leap Configuration START" "$RC_FILE"; then
        sed -i.bak '/Leap Configuration START/,/Leap Configuration END/d' "$RC_FILE"
        rm -f "$RC_FILE.bak"
    elif grep -q "# Leap" "$RC_FILE"; then
        sed -i.bak '/# Leap/,/# End Leap/d' "$RC_FILE"
        sed -i.bak '/# Leap/,/^alias claudel/d' "$RC_FILE"
        rm -f "$RC_FILE.bak"
    fi

    # Remove ClaudeQ config (old naming)
    if grep -q "ClaudeQ Configuration START" "$RC_FILE"; then
        sed -i.bak '/ClaudeQ Configuration START/,/ClaudeQ Configuration END/d' "$RC_FILE"
        rm -f "$RC_FILE.bak"
    elif grep -q "# ClaudeQ" "$RC_FILE"; then
        sed -i.bak '/# ClaudeQ/,/^alias cq=/d' "$RC_FILE"
        rm -f "$RC_FILE.bak"
    fi

    # Clean up any stale env vars
    if grep -q "CLAUDEQ_PROJECT_DIR" "$RC_FILE" 2>/dev/null; then
        sed -i.bak '/CLAUDEQ_PROJECT_DIR/d' "$RC_FILE"
        rm -f "$RC_FILE.bak"
    fi

    echo -e "${GREEN}✓ Removed shell configuration from $RC_FILE${NC}"
}

# Main
RC_FILE=$(get_rc_file)
remove_shell_config "$RC_FILE"
