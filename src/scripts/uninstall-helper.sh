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

# Remove the Headroom integration (`leap --headroom`): the running proxy + health
# watchdog, the enable marker, and any Headroom routing left in per-CLI env. The
# autostart lines live INSIDE the managed block (gated on the marker), so
# remove_shell_config already strips them; we also strip the legacy standalone
# block for users who installed before it was folded in. The headroom tool
# itself (pipx), the repo scripts, and ~/.headroom state are left for the user.
remove_headroom() {
    local RC_FILE="$1"
    if [ -n "$RC_FILE" ] && [ -f "$RC_FILE" ] && grep -q "# >>> leap-headroom >>>" "$RC_FILE" 2>/dev/null; then
        sed_inplace '/# >>> leap-headroom >>>/,/# <<< leap-headroom <<</d' "$RC_FILE"
        echo -e "${GREEN}✓ Removed legacy Headroom autostart from $RC_FILE${NC}"
    fi
    # Tear down the background health watcher (pkill -f on the script path is the
    # authoritative stop - no PID-reuse risk) and remove the enable marker so a
    # later reconfigure won't re-add the autostart.
    pkill -f "leap-headroom-watchdog.sh" 2>/dev/null && echo "  Stopped Headroom health watchdog" || true
    rm -rf "$HOME/.headroom/watchdog.lock" "$HOME/.headroom/up.lock"
    rm -f "$REPO_PATH/.storage/headroom_enabled"
    # Stop the background proxy we started (matched narrowly, won't touch other procs)
    pkill -f "headroom proxy --port 8787" 2>/dev/null && echo "  Stopped Headroom proxy" || true
    # Strip Headroom routing from per-CLI env so leftover entries can't break CLIs later
    local ENVF="$REPO_PATH/.storage/cli_env.json"
    if [ -f "$ENVF" ] && command -v python3 >/dev/null 2>&1; then
        python3 - "$ENVF" <<'PY' 2>/dev/null || true
import json, sys
f = sys.argv[1]
try:
    d = json.load(open(f))
except Exception:
    sys.exit(0)
changed = False
for cli, env in list(d.items()):
    if not isinstance(env, dict):
        continue
    routed = any(":8787" in str(env.get(k, "")) for k in
                 ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "COPILOT_PROVIDER_BASE_URL"))
    if not routed:
        continue
    for k in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "COPILOT_PROVIDER_BASE_URL", "COPILOT_PROVIDER_TYPE"):
        if k in env:
            del env[k]; changed = True
    if not env:
        del d[cli]
if changed:
    json.dump(d, open(f, "w"), indent=2)
    print("  Cleaned Headroom routing from cli_env.json")
PY
    fi
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
remove_headroom "$RC_FILE"
remove_storage "$REPO_PATH"
