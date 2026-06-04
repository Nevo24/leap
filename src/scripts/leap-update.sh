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

# Maintainer identities, one email per line. When `leap --update` finds the
# local history has diverged from origin (someone rewrote main with a
# force-push), it realigns by hard-resetting to origin - which discards local
# commits. To avoid silently dropping a user's OWN work, we look at the author
# of the local tip commit (HEAD): if it's a maintainer the tip is just the
# pre-rewrite upstream commit (a user with their own work would have their
# commit as the tip instead), so the divergence is a stale copy of rewritten
# history -> realign silently; otherwise the user committed on top -> prompt
# first. Add new maintainers here, one per line.
OWNER_EMAILS="nevo24@gmail.com"

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
        echo -e "${YELLOW}⚠ You have unpushed local commits:${NC}"
        git log --oneline "$UPSTREAM"..HEAD
        read -p "  Update anyway? (y/N) " -n 1 -r REPLY
        echo
        if [ "$REPLY" != "y" ] && [ "$REPLY" != "Y" ]; then
            echo "Update cancelled. Push first, then retry."
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

# Fetch the latest refs, then reconcile based on how local and origin relate.
# We fetch (not pull) so we can branch on the relationship:
#   - behind   -> fast-forward (the normal update; cannot conflict)
#   - ahead    -> your own unpushed commits, nothing to pull
#   - diverged -> origin's history was rewritten (owner amended/rebased and
#                 force-pushed). A plain `git pull` fails here with "Need to
#                 specify how to reconcile divergent branches", which would
#                 break the update. We hard-reset to origin instead so the
#                 update keeps working - silently when the local tip commit is
#                 the maintainer's, or after a prompt when the user has
#                 committed on top (see the diverged branch below). The old
#                 HEAD stays in `git reflog`.
#
# This only guards against a FUTURE rewrite: Phase 1 runs from the PRE-pull
# copy of this script (see header), so a rewrite that lands before the user
# has a leap-update.sh containing this block still needs a one-time manual
# `git reset --hard origin/<branch>`.
#
# Retry the fetch up to 3x with 1s / 3s backoff to ride out concurrent fetches
# from other sources (IDE auto-fetch, a manual `git fetch` in another terminal)
# that race on 'refs/remotes/origin/...':
#   error: cannot lock ref 'refs/remotes/origin/main': is at <X> but expected <Y>
# Output is left LIVE (not captured) so HTTPS credential prompts work and the
# user sees real-time progress; on final failure git's actual error shows
# directly above the "failed after 3 attempts" line.
for attempt in 1 2 3; do
    fetch_exit=0
    git fetch origin || fetch_exit=$?
    if [ "$fetch_exit" -eq 0 ]; then
        break
    fi
    # User-initiated abort (Ctrl+C). Respect the first signal instead of
    # falling into the retry message + sleep (which would need a second Ctrl+C).
    if [ "$fetch_exit" -eq 130 ]; then
        echo ""
        echo -e "${YELLOW}⚠ Update interrupted.${NC}"
        exit 130
    fi
    if [ "$attempt" -lt 3 ]; then
        sleep_for=$((attempt * 2 - 1))   # 1s, then 3s
        echo -e "${YELLOW}⚠ git fetch failed (attempt $attempt/3) - likely a concurrent fetch; retrying in ${sleep_for}s...${NC}"
        sleep "$sleep_for"
    else
        echo -e "${YELLOW}⚠ Git fetch failed after 3 attempts. See errors above and try again.${NC}"
        exit 1
    fi
done

# Remote-tracking ref to reconcile against (e.g. origin/main).
REMOTE_REF=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || true)
if [ -z "$REMOTE_REF" ]; then
    REMOTE_REF="origin/$(git rev-parse --abbrev-ref HEAD)"
fi

LOCAL_SHA=$(git rev-parse HEAD)
REMOTE_SHA=$(git rev-parse "$REMOTE_REF")
BASE_SHA=$(git merge-base HEAD "$REMOTE_REF" 2>/dev/null || true)

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ]; then
    # Already current. POST_PULL_HEAD == PRE_PULL_HEAD below drives the
    # --skip-if-current "already up to date" exit.
    :
elif [ "$LOCAL_SHA" = "$BASE_SHA" ]; then
    # Behind origin: fast-forward only (the working tree was verified clean
    # above, so this cannot conflict or create a merge commit).
    git merge --ff-only "$REMOTE_REF"
elif [ "$REMOTE_SHA" = "$BASE_SHA" ]; then
    # Ahead of origin (your own unpushed commits); nothing new to pull.
    echo -e "${GREEN}✓ Local branch is ahead of $REMOTE_REF - nothing to pull${NC}"
else
    # Diverged: someone rewrote origin's history (amend/rebase/squash +
    # force-push), so there's no fast-forward path. Decide silent-vs-prompt by
    # the author of the local tip commit (HEAD):
    #   - a maintainer -> the tip is just the pre-rewrite upstream commit (a
    #     user with their own work would have their commit as the tip instead),
    #     so this is a stale copy of rewritten history; realign silently (-q
    #     hides "HEAD is now at", so a rewrite looks like a normal pull).
    #   - anyone else  -> the user committed on top; list the commits and
    #     prompt (default No) before discarding.
    # Either way the reset is reflog-recoverable, and the dirty-tree gate above
    # already blocked UNCOMMITTED work, so only committed divergence is at play.
    head_author=$(git log -1 --format='%ae' HEAD)
    if printf '%s\n' "$OWNER_EMAILS" | grep -qxF "$head_author"; then
        git reset --hard -q "$REMOTE_REF"
    else
        echo -e "${YELLOW}⚠ Local history diverged from $REMOTE_REF, and you have local commits:${NC}"
        git log --oneline "$REMOTE_REF"..HEAD
        read -p "  Discard them and hard-reset to $REMOTE_REF? (y/N) " -n 1 -r REPLY
        echo
        if [ "$REPLY" != "y" ] && [ "$REPLY" != "Y" ]; then
            echo "Update cancelled. Push or stash your commits, then retry."
            exit 1
        fi
        git reset --hard "$REMOTE_REF"
    fi
fi

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
