#!/bin/bash
#
# ClaudeQ Shell Configuration Helper
# Called by: make install, make update
#
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

REPO_PATH="${1:-$(git rev-parse --show-toplevel 2>/dev/null)}"

# Get shell RC file
SHELL_NAME=$(basename "$SHELL")
if [ "$SHELL_NAME" = "zsh" ]; then
    RC_FILE="$HOME/.zshrc"
elif [ "$SHELL_NAME" = "bash" ]; then
    RC_FILE="$HOME/.bashrc"
else
    echo -e "${YELLOW}⚠ Unknown shell: $SHELL_NAME${NC}"
    echo "  Please manually add configuration to your shell RC file."
    exit 0
fi

# Check if config already exists
if grep -q "# ClaudeQ" "$RC_FILE" 2>/dev/null; then
    echo -e "${YELLOW}⚠ ClaudeQ configuration already exists in $RC_FILE${NC}"
    read -p "  Overwrite? (y/N) " -n 1 -r REPLY
    echo

    if [ "$REPLY" = "y" ] || [ "$REPLY" = "Y" ]; then
        # Remove old config
        if grep -q "ClaudeQ Configuration START" "$RC_FILE"; then
            sed -i.bak '/ClaudeQ Configuration START/,/ClaudeQ Configuration END/d' "$RC_FILE"
        else
            sed -i.bak '/# ClaudeQ/,/^alias cq=/d' "$RC_FILE"
        fi
        rm -f "$RC_FILE.bak"
        echo -e "${GREEN}✓ Removed old configuration${NC}"
    else
        echo "  Skipping shell configuration."
        exit 0
    fi
fi

# Backup RC file
if [ -f "$RC_FILE" ]; then
    cp "$RC_FILE" "$RC_FILE.backup-$(date +%Y%m%d-%H%M%S)"
    echo -e "${GREEN}✓ Backed up $RC_FILE${NC}"
else
    echo -e "${GREEN}✓ Creating new $RC_FILE${NC}"
fi

# Get Poetry venv path
POETRY_VENV=$(cd "$REPO_PATH" && poetry env info --path 2>/dev/null || echo "")

# Add ClaudeQ configuration
cat >> "$RC_FILE" <<'EOF'

# ===== ClaudeQ Configuration START - DO NOT REMOVE (needed for uninstall) =====
# ClaudeQ - Scrollable in JetBrains IDEs! 🎯
# Uses PTY (no tmux) with native scrolling
# Server in JetBrains, client in any terminal
#
# Usage: claudeq <tag> [message] (or: cq)
#        claudeq-cleanup (or: cqc)
#
# You can modify the content below, but keep the START/END marker lines
# for proper uninstallation.
EOF

echo "export CLAUDEQ_PROJECT_DIR=\"$REPO_PATH\"" >> "$RC_FILE"
echo "" >> "$RC_FILE"

# Add JetBrains IDE CLI tools to PATH
echo "# Add JetBrains IDE CLI tools to PATH for monitor support" >> "$RC_FILE"
JETBRAINS_PATHS=""
for pattern in IntelliJ PyCharm WebStorm PhpStorm GoLand RubyMine CLion DataGrip Rider Fleet; do
    for app in /Applications/${pattern}*.app; do
        if [ -d "$app/Contents/MacOS" ]; then
            JETBRAINS_PATHS="$JETBRAINS_PATHS:$app/Contents/MacOS"
        fi
    done
done

if [ -n "$JETBRAINS_PATHS" ]; then
    echo "export PATH=\"\$PATH$JETBRAINS_PATHS\"" >> "$RC_FILE"
fi
echo "" >> "$RC_FILE"

# Add claudeq function
cat >> "$RC_FILE" <<'EOF'
claudeq() {
    if [ $# -eq 0 ]; then
        echo "Error: Tag is required"
        echo "Usage: claudeq <tag> [message]"
        echo "Example (server): claudeq my-feature"
        echo "Example (client): claudeq my-feature 'hello Claude'"
        return 1
    fi
    # Flags (starting with --) can be passed and will be used by server only
    # Example: claudeq my-tag --dangerously-skip-permissions
    "$CLAUDEQ_PROJECT_DIR/src/scripts/claudeq-main.sh" "$@"
}

claudeq-cleanup() {
    "$CLAUDEQ_PROJECT_DIR/src/scripts/claudeq-cleanup.sh"
}

alias cq='claudeq'
alias cqc='claudeq-cleanup'
# ===== ClaudeQ Configuration END - DO NOT REMOVE (needed for uninstall) =====
EOF

echo -e "${GREEN}✓ Added ClaudeQ configuration to $RC_FILE${NC}"
echo "  Using Poetry venv: $POETRY_VENV"
