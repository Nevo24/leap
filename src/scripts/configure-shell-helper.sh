#!/bin/bash
#
# Leap Shell Configuration Helper
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
if grep -q "# Leap" "$RC_FILE" 2>/dev/null; then
    echo -e "${YELLOW}⚠ Leap configuration already exists in $RC_FILE${NC}"
    read -p "  Overwrite? (y/N) " -n 1 -r REPLY
    echo

    if [ "$REPLY" = "y" ] || [ "$REPLY" = "Y" ]; then
        # Remove old config
        if grep -q "Leap Configuration START" "$RC_FILE"; then
            sed -i.bak '/Leap Configuration START/,/Leap Configuration END/d' "$RC_FILE"
        else
            sed -i.bak '/# Leap/,/^alias claudel=/d' "$RC_FILE"
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

# Get Poetry venv path (try stored path first, then poetry command)
VENV_PATH_FILE="$REPO_PATH/.storage/venv-path"
if [ -f "$VENV_PATH_FILE" ]; then
    POETRY_VENV=$(cat "$VENV_PATH_FILE")
else
    POETRY_VENV=$(cd "$REPO_PATH" && poetry env info --path 2>/dev/null || echo "")
fi

# Add Leap configuration
cat >> "$RC_FILE" <<'EOF'

# ===== Leap Configuration START - DO NOT REMOVE (needed for uninstall) =====
# Leap - Scrollable in JetBrains IDEs! 🎯
# Uses PTY (no tmux) with native scrolling
# Server in JetBrains, client in any terminal
#
# Usage: leap [tag] [--flags]
#        leap-cleanup
#
# You can modify the content below, but keep the START/END marker lines
# for proper uninstallation.
EOF

echo "export LEAP_PROJECT_DIR=\"$REPO_PATH\"" >> "$RC_FILE"
echo "" >> "$RC_FILE"

# Add JetBrains IDE CLI tools to PATH
echo "# Add JetBrains IDE CLI tools to PATH for monitor support" >> "$RC_FILE"
JETBRAINS_PATHS=""
for pattern in IntelliJ PyCharm WebStorm PhpStorm GoLand RubyMine CLion DataGrip Rider Fleet "Android Studio"; do
    # Search both /Applications (standalone) and ~/Applications (JetBrains Toolbox)
    for app_dir in /Applications "$HOME/Applications"; do
        for app in "$app_dir"/"${pattern}"*.app; do
            if [ -d "$app/Contents/MacOS" ]; then
                JETBRAINS_PATHS="$JETBRAINS_PATHS:$app/Contents/MacOS"
            fi
        done
    done
done

# JetBrains Toolbox shell scripts directory
TOOLBOX_SCRIPTS="$HOME/Library/Application Support/JetBrains/Toolbox/scripts"
if [ -d "$TOOLBOX_SCRIPTS" ]; then
    JETBRAINS_PATHS="$JETBRAINS_PATHS:$TOOLBOX_SCRIPTS"
fi

if [ -n "$JETBRAINS_PATHS" ]; then
    echo "export PATH=\"\$PATH$JETBRAINS_PATHS\"" >> "$RC_FILE"
fi
echo "" >> "$RC_FILE"

# Generate per-CLI default flags from the provider registry
echo "# Default flags per CLI (always passed when starting a server)" >> "$RC_FILE"
PYTHONPATH="$REPO_PATH/src:${PYTHONPATH:-}" "$POETRY_VENV/bin/python3" -c "
from leap.cli_providers.registry import list_providers
for name in list_providers():
    var = 'LEAP_' + name.upper().replace('-', '_') + '_FLAGS'
    print(f'export {var}=\"\"')
" >> "$RC_FILE"

# Add leap function
cat >> "$RC_FILE" <<'EOF'

# Extra flags can also be passed inline: leap my-tag --some-flag
leap() {
    "$LEAP_PROJECT_DIR/src/scripts/leap-select.sh" "$@"
}

# ===== Leap Configuration END - DO NOT REMOVE (needed for uninstall) =====
EOF

echo -e "${GREEN}✓ Added Leap configuration to $RC_FILE${NC}"
echo "  Using Poetry venv: $POETRY_VENV"
