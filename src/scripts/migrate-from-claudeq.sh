#!/bin/bash
#
# Leap Migration: ClaudeQ → Leap
# Called by: make update, make install
#
# Detects old ClaudeQ installation artifacts and migrates to Leap.
# Idempotent — safe to run multiple times.
#
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

REPO_PATH="${1:-$(git rev-parse --show-toplevel 2>/dev/null)}"
STORAGE_DIR="$REPO_PATH/.storage"

# ── Detect shell RC file ────────────────────────────────────────────
SHELL_NAME=$(basename "$SHELL")
if [ "$SHELL_NAME" = "zsh" ]; then
    RC_FILE="$HOME/.zshrc"
elif [ "$SHELL_NAME" = "bash" ]; then
    RC_FILE="$HOME/.bashrc"
else
    RC_FILE=""
fi

# ── Detection: is there anything to migrate? ────────────────────────
HAS_OLD=false

# Signal 1: Old shell config block
if [ -n "$RC_FILE" ] && [ -f "$RC_FILE" ] && grep -q "ClaudeQ Configuration" "$RC_FILE" 2>/dev/null; then
    HAS_OLD=true
fi

# Signal 2: Old monitor app
if [ -d "/Applications/ClaudeQ Monitor.app" ]; then
    HAS_OLD=true
fi

# Signal 3: Old claude hook file
if [ -f "$HOME/.claude/hooks/claudeq-hook.sh" ]; then
    HAS_OLD=true
fi

# Signal 4: Old codex hook file
if [ -f "$HOME/.codex/claudeq-hook.sh" ]; then
    HAS_OLD=true
fi

# Signal 5: Old source directory still present
if [ -d "$REPO_PATH/src/claudeq" ]; then
    HAS_OLD=true
fi

# Signal 6: Old storage files (cq_* naming)
if ls "$STORAGE_DIR"/cq_* 1>/dev/null 2>&1; then
    HAS_OLD=true
fi

# Signal 7: Old VS Code extension still installed
if command -v code >/dev/null 2>&1; then
    if code --list-extensions 2>/dev/null | grep -q "claudeq.claudeq-terminal-selector"; then
        HAS_OLD=true
    fi
fi

if [ "$HAS_OLD" = false ]; then
    # Nothing to migrate
    exit 0
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "  ${YELLOW}Detected old ClaudeQ installation — migrating to Leap...${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Step 1: Migrate storage files ───────────────────────────────────
# Rename cq_* → leap_* equivalents.
# The existing Python migration chain in config.py handles:
#   leap_selected_ctx → leap_selected_template → leap_selected_preset
#   leap_contexts.json → leap_templates.json → leap_presets.json
# So we rename cq_* → leap_* (the intermediate names) and let Python
# finish the chain on next monitor startup.

MIGRATED_FILES=0

# cq_selected_ctx → leap_selected_ctx (Python chain finishes the rest)
if [ -f "$STORAGE_DIR/cq_selected_ctx" ] && [ ! -f "$STORAGE_DIR/leap_selected_ctx" ] && [ ! -f "$STORAGE_DIR/leap_selected_template" ] && [ ! -f "$STORAGE_DIR/leap_selected_preset" ]; then
    mv "$STORAGE_DIR/cq_selected_ctx" "$STORAGE_DIR/leap_selected_ctx"
    MIGRATED_FILES=$((MIGRATED_FILES + 1))
fi

# cq_contexts.json → leap_contexts.json (Python chain finishes the rest)
if [ -f "$STORAGE_DIR/cq_contexts.json" ] && [ ! -f "$STORAGE_DIR/leap_contexts.json" ] && [ ! -f "$STORAGE_DIR/leap_templates.json" ] && [ ! -f "$STORAGE_DIR/leap_presets.json" ]; then
    mv "$STORAGE_DIR/cq_contexts.json" "$STORAGE_DIR/leap_contexts.json"
    MIGRATED_FILES=$((MIGRATED_FILES + 1))
fi

# cq_selected_template → leap_selected_template (Python chain: → leap_selected_preset)
if [ -f "$STORAGE_DIR/cq_selected_template" ] && [ ! -f "$STORAGE_DIR/leap_selected_template" ] && [ ! -f "$STORAGE_DIR/leap_selected_preset" ]; then
    mv "$STORAGE_DIR/cq_selected_template" "$STORAGE_DIR/leap_selected_template"
    MIGRATED_FILES=$((MIGRATED_FILES + 1))
fi

# cq_selected_direct_template → leap_selected_direct_template (Python chain: → leap_selected_direct_preset)
if [ -f "$STORAGE_DIR/cq_selected_direct_template" ] && [ ! -f "$STORAGE_DIR/leap_selected_direct_template" ] && [ ! -f "$STORAGE_DIR/leap_selected_direct_preset" ]; then
    mv "$STORAGE_DIR/cq_selected_direct_template" "$STORAGE_DIR/leap_selected_direct_template"
    MIGRATED_FILES=$((MIGRATED_FILES + 1))
fi

# cq_templates.json → leap_templates.json (Python chain: → leap_presets.json)
if [ -f "$STORAGE_DIR/cq_templates.json" ] && [ ! -f "$STORAGE_DIR/leap_templates.json" ] && [ ! -f "$STORAGE_DIR/leap_presets.json" ]; then
    mv "$STORAGE_DIR/cq_templates.json" "$STORAGE_DIR/leap_templates.json"
    MIGRATED_FILES=$((MIGRATED_FILES + 1))
fi

if [ $MIGRATED_FILES -gt 0 ]; then
    echo -e "  ${GREEN}✓ Migrated $MIGRATED_FILES preset/storage file(s)${NC}"
else
    echo "  ✓ No storage files needed migration"
fi

# ── Step 2: Remove old shell config block ───────────────────────────
if [ -n "$RC_FILE" ] && [ -f "$RC_FILE" ] && grep -q "ClaudeQ Configuration" "$RC_FILE" 2>/dev/null; then
    if grep -q "ClaudeQ Configuration START" "$RC_FILE"; then
        sed -i.bak '/ClaudeQ Configuration START/,/ClaudeQ Configuration END/d' "$RC_FILE"
    elif grep -q "# ClaudeQ" "$RC_FILE"; then
        # Older format without START/END markers
        sed -i.bak '/# ClaudeQ/,/^alias cq=/d' "$RC_FILE"
    fi
    rm -f "$RC_FILE.bak"

    # Also remove any stale CLAUDEQ_PROJECT_DIR export that might be outside the block
    if grep -q "CLAUDEQ_PROJECT_DIR" "$RC_FILE" 2>/dev/null; then
        sed -i.bak '/CLAUDEQ_PROJECT_DIR/d' "$RC_FILE"
        rm -f "$RC_FILE.bak"
    fi

    echo -e "  ${GREEN}✓ Removed old ClaudeQ shell configuration from $RC_FILE${NC}"
else
    echo "  ✓ No old shell configuration to remove"
fi

# ── Step 3: Uninstall old VS Code extension ─────────────────────────
if command -v code >/dev/null 2>&1; then
    if code --list-extensions 2>/dev/null | grep -q "claudeq.claudeq-terminal-selector"; then
        code --uninstall-extension claudeq.claudeq-terminal-selector 2>/dev/null && \
            echo -e "  ${GREEN}✓ Uninstalled old ClaudeQ VS Code extension${NC}" || \
            echo -e "  ${YELLOW}⚠ Could not uninstall old VS Code extension (remove manually)${NC}"
    else
        echo "  ✓ Old VS Code extension not installed"
    fi
else
    echo "  ✓ VS Code not found, skipping extension cleanup"
fi

# ── Step 4: Remove old source directory ─────────────────────────────
if [ -d "$REPO_PATH/src/claudeq" ]; then
    rm -rf "$REPO_PATH/src/claudeq"
    echo -e "  ${GREEN}✓ Removed old src/claudeq/ directory${NC}"
else
    echo "  ✓ Old source directory already removed"
fi

# ── Step 5: Remove old ClaudeQ Monitor.app ──────────────────────────
if [ -d "/Applications/ClaudeQ Monitor.app" ]; then
    sudo rm -rf "/Applications/ClaudeQ Monitor.app" 2>/dev/null && \
        echo -e "  ${GREEN}✓ Removed /Applications/ClaudeQ Monitor.app${NC}" || \
        echo -e "  ${YELLOW}⚠ Could not remove ClaudeQ Monitor.app (run: sudo rm -rf '/Applications/ClaudeQ Monitor.app')${NC}"
    # Signal to the update target that monitor needs rebuilding
    mkdir -p "$STORAGE_DIR"
    touch "$STORAGE_DIR/.migration_had_monitor"
else
    echo "  ✓ Old monitor app not found"
fi

# ── Step 6: Remove old hook files ───────────────────────────────────
HOOKS_CLEANED=false

if [ -f "$HOME/.claude/hooks/claudeq-hook.sh" ]; then
    rm -f "$HOME/.claude/hooks/claudeq-hook.sh"
    HOOKS_CLEANED=true
fi

if [ -f "$HOME/.codex/claudeq-hook.sh" ]; then
    rm -f "$HOME/.codex/claudeq-hook.sh"
    HOOKS_CLEANED=true
fi

if [ "$HOOKS_CLEANED" = true ]; then
    echo -e "  ${GREEN}✓ Removed old hook script files${NC}"
else
    echo "  ✓ No old hook files to remove"
fi

# Hook config entries (claudeq-hook.sh references in settings.json / hooks.json)
# are cleaned up by configure_claude_hooks.py and configure_codex_hooks.py
# which run later in the update/install flow. They detect and remove entries
# matching "claudeq-hook.sh" before adding new "leap-hook.sh" entries.

echo ""
echo -e "  ${GREEN}✓ Migration from ClaudeQ to Leap complete!${NC}"
echo "    New shell configuration will be written next..."
echo ""
