#!/bin/bash
#
# Leap Shell Configuration Helper
# Called by: make install, make update
#
set -e

# shellcheck source=sed-inplace.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sed-inplace.sh"

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
    sed_inplace '/^export LEAP_[A-Z_]*_FLAGS="/d' "$RC_FILE"
    sed_inplace '/^# Default flags per CLI/d' "$RC_FILE"
    sed_inplace '/^# Extra flags can also be passed inline/d' "$RC_FILE"
    # Remove legacy leap-cleanup comment (auto-cleanup runs on every leap invocation)
    sed_inplace '/^#        leap-cleanup$/d' "$RC_FILE"
    # Migrate away from the old standalone Headroom block: its autostart is now
    # folded into the managed block below, gated on .storage/headroom_enabled.
    if grep -q "# >>> leap-headroom >>>" "$RC_FILE" 2>/dev/null; then
        sed_inplace '/# >>> leap-headroom >>>/,/# <<< leap-headroom <<</d' "$RC_FILE"
    fi
fi

# Silently strip any existing Leap block. The content is 100% regenerated
# from this script — hand-edits between the START/END markers are not
# protected (users are expected to customize outside the block).
stripped=false
if grep -q "Leap Configuration START" "$RC_FILE" 2>/dev/null; then
    sed_inplace '/Leap Configuration START/,/Leap Configuration END/d' "$RC_FILE"
    stripped=true
elif grep -q "# Leap" "$RC_FILE" 2>/dev/null; then
    # Legacy pre-marker block — fall back to the old heuristic.
    sed_inplace '/# Leap/,/^alias claudel=/d' "$RC_FILE"
    stripped=true
fi

# Collapse trailing blank lines left behind by the strip, so the separator
# blank line in our heredoc doesn't accumulate across repeated installs.
# `replace_file` preserves a symlinked $RC_FILE — a plain `mv` here would
# replace the symlink with a regular file and break dotfile-manager setups.
if [ "$stripped" = true ] && [ -s "$RC_FILE" ]; then
    awk 'NF {for (i=0;i<bl;i++) print ""; bl=0; print; next} {bl++}' \
        "$RC_FILE" > "$RC_FILE.trim" && replace_file "$RC_FILE.trim" "$RC_FILE"
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

# Headroom context-compression proxy auto-start + 5-min health watchdog.
# Emitted only while at least one CLI is routed through Headroom. Living inside
# the managed block means update/reconfigure regenerate it - so it self-heals,
# stays current, and is never stranded on old logic. The scripts run from the
# repo, so `git pull` (i.e. `leap --update`) refreshes their behavior
# automatically.
#
# Reconcile the marker from the real source of truth (cli_env.json): present iff
# a CLI is routed through the proxy (localhost:8787). This both migrates users off
# the old standalone-block design (marker recreated) and clears a stale marker
# left by a hand-edit of cli_env.json. Guarded on the file EXISTING so a missing
# cli_env.json (fresh install, or a transient absence) can never tear down a
# working setup - we only ever reconcile against a file we can actually read.
if [ -f "$REPO_PATH/.storage/cli_env.json" ]; then
    if grep -q "localhost:8787" "$REPO_PATH/.storage/cli_env.json"; then
        : > "$REPO_PATH/.storage/headroom_enabled"
    else
        rm -f "$REPO_PATH/.storage/headroom_enabled"
    fi
fi
if [ -f "$REPO_PATH/.storage/headroom_enabled" ]; then
    cat >> "$RC_FILE" <<'EOF'

# Headroom context-compression proxy (managed by `leap --headroom`)
[ -x "$LEAP_PROJECT_DIR/src/scripts/leap-headroom-up.sh" ] && nohup "$LEAP_PROJECT_DIR/src/scripts/leap-headroom-up.sh" >/dev/null 2>&1 & disown
[ -x "$LEAP_PROJECT_DIR/src/scripts/leap-headroom-watchdog.sh" ] && nohup "$LEAP_PROJECT_DIR/src/scripts/leap-headroom-watchdog.sh" >/dev/null 2>&1 & disown
EOF
fi

cat >> "$RC_FILE" <<'EOF'

# ===== Leap Configuration END - DO NOT REMOVE =====
EOF

echo -e "${GREEN}✓ Added Leap configuration to $RC_FILE${NC}"
echo "  Using Poetry venv: $POETRY_VENV"
