#!/bin/bash
#
# Leap Shell Configuration Helper
# Called by: make install, make update
#
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# `--update` used to gate the overwrite prompt; now the overwrite is always
# silent, so we just consume the flag for backward compatibility with the
# Makefile's `.detect-shell-update` target.
if [ "$1" = "--update" ]; then
    shift
fi
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

# Remove legacy LEAP_*_FLAGS exports (now stored in .storage/cli_flags.json)
if [ -f "$RC_FILE" ]; then
    sed -i.bak '/^export LEAP_[A-Z_]*_FLAGS="/d' "$RC_FILE"
    sed -i.bak '/^# Default flags per CLI/d' "$RC_FILE"
    sed -i.bak '/^# Extra flags can also be passed inline/d' "$RC_FILE"
    # Remove legacy leap-cleanup comment (auto-cleanup runs on every leap invocation)
    sed -i.bak '/^#        leap-cleanup$/d' "$RC_FILE"
    rm -f "$RC_FILE.bak"
fi

# Silently strip any existing Leap block. The content is 100% regenerated
# from this script — hand-edits between the START/END markers are not
# protected (users are expected to customize outside the block).
stripped=false
if grep -q "Leap Configuration START" "$RC_FILE" 2>/dev/null; then
    sed -i.bak '/Leap Configuration START/,/Leap Configuration END/d' "$RC_FILE"
    rm -f "$RC_FILE.bak"
    stripped=true
elif grep -q "# Leap" "$RC_FILE" 2>/dev/null; then
    # Legacy pre-marker block — fall back to the old heuristic.
    sed -i.bak '/# Leap/,/^alias claudel=/d' "$RC_FILE"
    rm -f "$RC_FILE.bak"
    stripped=true
fi

# Collapse trailing blank lines left behind by the strip, so the separator
# blank line in our heredoc doesn't accumulate across repeated installs.
if [ "$stripped" = true ] && [ -s "$RC_FILE" ]; then
    awk 'NF {for (i=0;i<bl;i++) print ""; bl=0; print; next} {bl++}' \
        "$RC_FILE" > "$RC_FILE.trim" && mv "$RC_FILE.trim" "$RC_FILE"
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

# ===== Leap Configuration START - DO NOT REMOVE =====
EOF

echo "export LEAP_PROJECT_DIR=\"$REPO_PATH\"" >> "$RC_FILE"

cat >> "$RC_FILE" <<'EOF'

leap() {
    "$LEAP_PROJECT_DIR/src/scripts/leap-select.sh" "$@"
}
EOF

# Fast-path compdef if the user's framework (oh-my-zsh/prezto/etc) already
# ran compinit — skips a 100–500ms fpath rescan on every shell start.
if [ "$SHELL_NAME" = "zsh" ]; then
    cat >> "$RC_FILE" <<'EOF'

# Tab-complete `leap` flags
if [ -f "$LEAP_PROJECT_DIR/src/scripts/_leap" ]; then
    fpath=("$LEAP_PROJECT_DIR/src/scripts" $fpath)
    if (( $+functions[compdef] )); then
        autoload -Uz _leap && compdef _leap leap
    else
        autoload -Uz compinit && compinit -u
    fi
fi
EOF
fi

cat >> "$RC_FILE" <<'EOF'

# ===== Leap Configuration END - DO NOT REMOVE =====
EOF

echo -e "${GREEN}✓ Added Leap configuration to $RC_FILE${NC}"
echo "  Using Poetry venv: $POETRY_VENV"
