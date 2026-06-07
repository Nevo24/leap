#!/bin/bash
#
# Leap Uninstall Helper
# Called by: make uninstall
#
set -e

# shellcheck source=sed-inplace.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sed-inplace.sh"

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
        sed_inplace '/Leap Configuration START/,/Leap Configuration END/d' "$RC_FILE"
    elif grep -q "# Leap" "$RC_FILE"; then
        sed_inplace '/# Leap/,/# End Leap/d' "$RC_FILE"
        sed_inplace '/# Leap/,/^alias claudel/d' "$RC_FILE"
    fi

    # Remove ClaudeQ config (old naming)
    if grep -q "ClaudeQ Configuration START" "$RC_FILE"; then
        sed_inplace '/ClaudeQ Configuration START/,/ClaudeQ Configuration END/d' "$RC_FILE"
    elif grep -q "# ClaudeQ" "$RC_FILE"; then
        sed_inplace '/# ClaudeQ/,/^alias cq=/d' "$RC_FILE"
    fi

    # Clean up any stale env vars
    if grep -q "CLAUDEQ_PROJECT_DIR" "$RC_FILE" 2>/dev/null; then
        sed_inplace '/CLAUDEQ_PROJECT_DIR/d' "$RC_FILE"
    fi

    echo -e "${GREEN}✓ Removed shell configuration from $RC_FILE${NC}"
}

# Warn if any Leap server lock directories exist (active or crashed sessions)
check_running_sessions() {
    local SOCKET_DIR="$REPO_PATH/.storage/sockets"
    if [ ! -d "$SOCKET_DIR" ]; then
        return
    fi

    local tags=()
    while IFS= read -r lock_dir; do
        tags+=("$(basename "$lock_dir" .server.lock)")
    done < <(find "$SOCKET_DIR" -maxdepth 1 -name "*.server.lock" -type d 2>/dev/null)

    if [ "${#tags[@]}" -eq 0 ]; then
        return
    fi

    echo ""
    echo -e "${YELLOW}  WARNING: Leap appears to have active sessions:${NC}"
    for tag in "${tags[@]}"; do
        echo "    - $tag"
    done
    echo "  Uninstalling with active sessions can break them mid-use:"
    echo "  hook scripts will be deleted, the venv removed, and (if you"
    echo "  choose to delete .storage) socket files wiped under running servers."
    echo "  Quit all Leap servers and the Monitor app before continuing."
    echo ""
    printf "  Continue anyway? (y/N) "
    read -n 1 -r REPLY
    echo
    if [ "$REPLY" != "y" ] && [ "$REPLY" != "Y" ]; then
        echo "  Uninstall cancelled."
        exit 0
    fi
}

# Ask whether to remove .storage
remove_storage() {
    local REPO_PATH="$1"
    local STORAGE_DIR="$REPO_PATH/.storage"

    if [ ! -d "$STORAGE_DIR" ]; then
        return
    fi

    echo ""
    echo -e "${YELLOW}  WARNING: .storage holds all your persistent Leap data:${NC}"
    echo "    - Notes, saved messages, and presets"
    echo "    - Monitor preferences and PR/SCM token configuration"
    echo "    - Session history, queue data, and pinned sessions"
    echo "    - Slack configuration"
    echo "  Deleting it is permanent and cannot be undone."
    echo ""
    printf "  Type \"yes\" to permanently delete .storage, or press Enter to keep it: "
    read -r REPLY
    echo

    if [ "$REPLY" = "yes" ]; then
        rm -rf "$STORAGE_DIR"
        echo -e "${GREEN}✓ Removed .storage${NC}"
    else
        echo "  Keeping .storage (your data is preserved)."
    fi
}

# Main
check_running_sessions
RC_FILE=$(get_rc_file)
remove_shell_config "$RC_FILE"
remove_storage "$REPO_PATH"
