#!/bin/bash
#
# Leap CLI selector - interactive menu to choose CLI provider
# Called by the 'leap' shell function
#
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
STORAGE_DIR="$PROJECT_DIR/.storage"
VENV_PATH_FILE="$STORAGE_DIR/venv-path"

# Handle --help and --update directly (pass to leap-main.sh)
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    exec "$SCRIPT_DIR/leap-main.sh" "$@"
fi
if [ "$1" = "--update" ]; then
    exec "$SCRIPT_DIR/leap-main.sh" "$@"
fi
if [ "$1" = "--reconfigure" ]; then
    exec "$SCRIPT_DIR/leap-main.sh" "$@"
fi
if [ "$1" = "--slack" ]; then
    exec "$SCRIPT_DIR/leap-main.sh" "$@"
fi
if [ "$1" = "--manage-clis" ]; then
    exec "$SCRIPT_DIR/leap-main.sh" "$@"
fi
if [ "$1" = "--resume" ]; then
    exec "$SCRIPT_DIR/leap-main.sh" "$@"
fi

# Honour LEAP_CLI env var: when set (e.g. from a GUI-spawned resume
# terminal that prefixes ``LEAP_RESUME_SESSION_ID=… LEAP_CLI=… leap
# <tag>``, or from one of the per-CLI wrappers like
# ``codex-leap-main.sh``), skip the interactive CLI selector and let
# leap-main.sh resolve the provider from the env var.  Without this,
# the selector pops up, the user's pick gets passed as ``--cli`` —
# which leap-main.sh then treats as overriding LEAP_CLI — and the
# resume hand-off in leap-server.py silently drops the recorded
# session because its ``cli_name == resume_cli`` gate fails.
if [ -n "$LEAP_CLI" ]; then
    exec "$SCRIPT_DIR/leap-main.sh" "$@"
fi

# Find Python (same logic as leap-main.sh)
if [ -f "$VENV_PATH_FILE" ]; then
    PYTHON_CMD="$(cat "$VENV_PATH_FILE")/bin/python3"
elif [ -n "$LEAP_PYTHON" ] && [ -x "$LEAP_PYTHON" ]; then
    PYTHON_CMD="$LEAP_PYTHON"
else
    echo "❌ Error: Leap virtualenv not found. Run 'make install'." >&2
    exit 1
fi

# Extract the tag (first non-flag arg) and forward EVERYTHING else
# verbatim to leap-main.sh, preserving original order.  Reordering args
# here (e.g. flags-before-positionals) would break leap-main.sh's
# `--flag value` pair detection.
TAG=""
REMAINING=()
tag_taken=0
for arg in "$@"; do
    if [ "$tag_taken" -eq 0 ] && [[ "$arg" != --* ]]; then
        TAG="$arg"
        tag_taken=1
    else
        REMAINING+=("$arg")
    fi
done

SOCKET_DIR="$STORAGE_DIR/sockets"

# If a server is already running for this tag, skip CLI selector — just connect
if [ -n "$TAG" ] && [ -S "$SOCKET_DIR/${TAG}.sock" ]; then
    exec "$SCRIPT_DIR/leap-main.sh" "$TAG" "${REMAINING[@]}"
fi

# If no tag provided, prompt for one first (before CLI selector).
# If the chosen tag already has a running server, we skip the CLI selector entirely.
if [ -z "$TAG" ]; then
    TAG=$("$PYTHON_CMD" "$SCRIPT_DIR/leap-select-tag.py")
    EXIT_CODE=$?
    if [ $EXIT_CODE -ne 0 ] || [ -z "$TAG" ]; then
        exit 1
    fi
    # Check again: if a server is already running for this tag, skip CLI selector
    if [ -S "$SOCKET_DIR/${TAG}.sock" ]; then
        exec "$SCRIPT_DIR/leap-main.sh" "$TAG" "${REMAINING[@]}"
    fi
else
    # Tag provided as argument — validate and record in history
    if [[ ! "$TAG" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
        echo "❌ Error: Session name must contain only letters, numbers, hyphens, and underscores" >&2
        exit 1
    fi
    # Record in tag history
    "$PYTHON_CMD" -c "
from pathlib import Path
STORAGE_DIR = Path('$STORAGE_DIR')
HISTORY_FILE = STORAGE_DIR / 'tag_history'
tag = '$TAG'
history = []
if HISTORY_FILE.exists():
    history = [l.strip() for l in HISTORY_FILE.read_text().strip().splitlines() if l.strip()]
history = [t for t in history if t != tag]
history.append(tag)
history = history[-50:]
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE.write_text('\n'.join(history) + '\n')
" 2>/dev/null
fi

# Show interactive CLI selector (only reached if no server is running for this tag)
SELECTED=$("$PYTHON_CMD" "$SCRIPT_DIR/leap-select-cli.py")
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ] || [ -z "$SELECTED" ]; then
    exit 1
fi

# Launch the selected CLI with tag, user flags, and any remaining args.
# --cli tells leap-main.sh the user explicitly chose this CLI (via selector).
# Stored per-CLI flags (cli_flags.json) and LEAP_<CLI>_FLAGS env var
# overrides are applied by pty_handler.py at spawn time — they don't
# travel through this script.  REMAINING preserves original arg order so
# leap-main.sh's `--flag value` pair detection works.
exec "$SCRIPT_DIR/leap-main.sh" "$TAG" --cli "$SELECTED" "${REMAINING[@]}"
