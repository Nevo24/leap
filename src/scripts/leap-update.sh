#!/bin/bash
#
# Leap Update - Phase 1: Pre-pull checks + git pull
#
# After pulling, exec's into `make .update-after-pull` so that Phase 2
# (deps, shell config, hooks, IDE config) runs from the FRESHLY PULLED
# Makefile.  This means changes to the update flow itself take effect
# on the same `leap --update` run, not the next one.
#
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
PROMPT_PREFIX="→"

SKIP_IF_CURRENT=false
if [ "$1" = "--skip-if-current" ]; then
    SKIP_IF_CURRENT=true
    shift
fi
PROJECT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

# Detect shell RC file
SHELL_NAME=$(basename "$SHELL")
if [ "$SHELL_NAME" = "zsh" ]; then
    RC_FILE="$HOME/.zshrc"
elif [ "$SHELL_NAME" = "bash" ]; then
    RC_FILE="$HOME/.bashrc"
else
    RC_FILE=""
fi

echo -e "$PROMPT_PREFIX Updating Leap..."

# Check if Leap is installed
if [ -z "$RC_FILE" ] || [ ! -f "$RC_FILE" ] || ! grep -qE "(Leap|ClaudeQ) Configuration" "$RC_FILE"; then
    echo -e "${YELLOW}⚠ Leap does not appear to be installed${NC}"
    echo "  No Leap or ClaudeQ configuration found in ${RC_FILE:-your shell config}"
    echo ""
    echo "Please run 'make install' first to install Leap."
    echo "After installation, you can use 'make update' to update to newer versions."
    exit 1
fi

cd "$PROJECT_DIR"

# Restore poetry.lock if modified by a previous Poetry version mismatch
git checkout -- poetry.lock 2>/dev/null || true

# Check for uncommitted changes
if [ -n "$(git status --porcelain)" ]; then
    echo -e "${YELLOW}⚠ You have uncommitted local changes:${NC}"
    git status --short
    echo ""
    echo "Please commit or stash your changes before updating."
    exit 1
fi

# Check for unpushed commits
UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || true)
if [ -n "$UPSTREAM" ]; then
    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse "$UPSTREAM" 2>/dev/null || true)
    BASE=$(git merge-base HEAD "$UPSTREAM" 2>/dev/null || true)
    if [ "$LOCAL" != "$REMOTE" ] && [ "$REMOTE" = "$BASE" ]; then
        echo -e "${YELLOW}⚠ You have local commits that haven't been pushed:${NC}"
        git log --oneline "$UPSTREAM"..HEAD
        echo ""
        read -p "  Continue updating anyway? Your commits may conflict. (y/N) " -n 1 -r REPLY
        echo
        if [ "$REPLY" != "y" ] && [ "$REPLY" != "Y" ]; then
            echo "Update cancelled. Push your changes first, then retry."
            exit 1
        fi
    fi
fi

# Phase 1: Pull latest code
echo -e "$PROMPT_PREFIX Pulling latest code from git..."
PRE_PULL_HEAD=$(git rev-parse HEAD)
git pull || (echo -e "${YELLOW}⚠ Git pull failed. Please resolve conflicts and try again.${NC}" && exit 1)
POST_PULL_HEAD=$(git rev-parse HEAD)

if [ "$SKIP_IF_CURRENT" = true ] && [ "$PRE_PULL_HEAD" = "$POST_PULL_HEAD" ]; then
    echo ""
    echo -e "${GREEN}✓ Leap is already up to date${NC}"
    exit 0
fi

# Phase 2: Run post-pull steps from the FRESHLY PULLED Makefile
exec make -C "$PROJECT_DIR" .update-after-pull
