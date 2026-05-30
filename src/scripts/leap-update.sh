#!/bin/bash
#
# Leap Update - Phase 1: Pre-pull checks + git pull
#
# After pulling, exec's into `make .update-after-pull` so that Phase 2
# (deps, shell config, hooks, IDE config) runs from the FRESHLY PULLED
# Makefile.  This means changes to the update flow itself take effect
# on the same `leap --update` run, not the next one.
#

# Strip env vars that can poison Python before it starts.  PYTHONHOME
# from a stale/abandoned venv triggers ``Failed to import encodings``
# in poetry/python sub-calls; VIRTUAL_ENV would make poetry use the
# wrong project's venv.  Only affects this script's children (including
# the make recipes it execs into), not the user's shell.
unset PYTHONHOME PYTHONPATH VIRTUAL_ENV

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

# Marker file: tells the monitor's WhatsNewDialog and UpdateCheckWorker
# that an update is in progress. Lifecycle:
#   - written here, BEFORE git pull (so it covers the whole pull/phase-2 window)
#   - removed by `trap EXIT` below if phase 1 aborts (pull fails, Ctrl+C, ...)
#   - removed by `.update-after-pull` at the end of phase 2 on success
#   - 30-min stale-timestamp fallback in the readers covers a phase-2 crash
# Readers use it for:
#   - WhatsNewDialog: show <pre_pull_sha>..origin/main instead of HEAD..origin/main
#     so the "see what's new" list is correct even after HEAD has advanced.
#   - UpdateCheckWorker: skip its background `git fetch origin` to avoid
#     racing with our `git pull` (the race causes "cannot lock ref" errors).
MARKER_FILE="$PROJECT_DIR/.storage/update_in_progress"
# Trap set BEFORE the write so a failed/partial write is still cleaned up
# on the inevitable `set -e` exit.
trap 'rm -f "$MARKER_FILE"' EXIT
mkdir -p "$PROJECT_DIR/.storage"
printf '{"pre_pull_sha":"%s","started_at":%s}\n' \
    "$PRE_PULL_HEAD" "$(date +%s)" > "$MARKER_FILE"

# Retry `git pull` up to 3 times with 1s / 3s backoff. Catches concurrent
# fetches from any source (IDE auto-fetch, manual `git fetch` in another
# terminal, etc.) that we don't control. The race causes:
#   error: cannot lock ref 'refs/remotes/origin/main': is at <X> but expected <Y>
# Output is left LIVE (not captured) so:
#   - interactive prompts work (e.g., HTTPS credential prompts);
#   - the user sees real-time progress on long pulls;
#   - on final failure they see git's actual error directly above the
#     "failed after 3 attempts" line — no more misleading "resolve conflicts"
#     wording. Side effect: a retried run shows the first attempt's error
#     before the retry succeeds. The retry message names the likely cause
#     (concurrent fetch) so users aren't alarmed.
for attempt in 1 2 3; do
    pull_exit=0
    git pull || pull_exit=$?
    if [ "$pull_exit" -eq 0 ]; then
        break
    fi
    # User-initiated abort (Ctrl+C). Without this, bash would proceed to
    # the retry message + sleep, forcing the user to Ctrl+C a second time
    # during the sleep to actually exit. Respect the first signal.
    if [ "$pull_exit" -eq 130 ]; then
        echo ""
        echo -e "${YELLOW}⚠ Update interrupted.${NC}"
        exit 130
    fi
    if [ "$attempt" -lt 3 ]; then
        sleep_for=$((attempt * 2 - 1))   # 1s, then 3s
        echo -e "${YELLOW}⚠ git pull failed (attempt $attempt/3) - likely a concurrent fetch; retrying in ${sleep_for}s...${NC}"
        sleep "$sleep_for"
    else
        echo -e "${YELLOW}⚠ Git pull failed after 3 attempts. See errors above and try again.${NC}"
        exit 1
    fi
done

POST_PULL_HEAD=$(git rev-parse HEAD)

if [ "$SKIP_IF_CURRENT" = true ] && [ "$PRE_PULL_HEAD" = "$POST_PULL_HEAD" ]; then
    echo ""
    echo -e "${GREEN}✓ Leap is already up to date${NC}"
    exit 0
fi

echo -e "${GREEN}✓ Code updated${NC}"
echo ""

# Phase 2: Run post-pull steps from the FRESHLY PULLED Makefile
exec make -C "$PROJECT_DIR" .update-after-pull
