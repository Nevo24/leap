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

    if ! grep -q "Leap Configuration" "$RC_FILE" 2>/dev/null; then
        echo "  No Leap configuration found in $RC_FILE"
        return
    fi

    echo -e "${YELLOW}⚠ Leap configuration found in $RC_FILE${NC}"
    read -p "  Remove shell configuration? (y/N) " -n 1 -r REPLY
    echo

    if [ "$REPLY" = "y" ] || [ "$REPLY" = "Y" ]; then
        # Backup
        cp "$RC_FILE" "$RC_FILE.backup-uninstall-$(date +%Y%m%d-%H%M%S)"

        # Remove config
        if grep -q "Leap Configuration START" "$RC_FILE"; then
            sed -i.bak '/Leap Configuration START/,/Leap Configuration END/d' "$RC_FILE"
        elif grep -q "# Leap" "$RC_FILE"; then
            sed -i.bak '/# Leap/,/# End Leap/d' "$RC_FILE"
            sed -i.bak '/# Leap/,/^alias claudel/d' "$RC_FILE"
        fi
        rm -f "$RC_FILE.bak"

        echo -e "${GREEN}✓ Removed Leap configuration from $RC_FILE${NC}"
        echo "  Backup created: $RC_FILE.backup-uninstall-$(date +%Y%m%d-%H%M%S)"
    else
        echo "  Skipped shell configuration removal."
        echo "  To manually remove later, delete lines between:"
        echo "    '# ===== Leap Configuration START =====' and"
        echo "    '# ===== Leap Configuration END ====='"
    fi
}

# Main
RC_FILE=$(get_rc_file)
remove_shell_config "$RC_FILE"
