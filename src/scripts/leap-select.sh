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
if [ "$1" = "--slack" ]; then
    exec "$SCRIPT_DIR/leap-main.sh" "$@"
fi
if [ "$1" = "--manage-clis" ]; then
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

# Separate tag from flags and messages
# First non-flag argument is the tag, rest are passed through
TAG=""
FLAGS=()
ARGS=()
for arg in "$@"; do
    if [ -z "$TAG" ] && [[ "$arg" != --* ]]; then
        TAG="$arg"
    elif [[ "$arg" == --* ]]; then
        FLAGS+=("$arg")
    else
        ARGS+=("$arg")
    fi
done

SOCKET_DIR="$STORAGE_DIR/sockets"

# If a server is already running for this tag, skip CLI selector — just connect
if [ -n "$TAG" ] && [ -S "$SOCKET_DIR/${TAG}.sock" ]; then
    exec "$SCRIPT_DIR/leap-main.sh" "$TAG" "${FLAGS[@]}" "${ARGS[@]}"
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
        exec "$SCRIPT_DIR/leap-main.sh" "$TAG" "${FLAGS[@]}" "${ARGS[@]}"
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

# Append default per-CLI flags: .storage/cli_flags.json, overridden by env var if set
CLI_UPPER=$(echo "$SELECTED" | tr '[:lower:]-' '[:upper:]_')
DEFAULT_FLAGS_VAR="LEAP_${CLI_UPPER}_FLAGS"
ENV_FLAGS="${!DEFAULT_FLAGS_VAR+__SET__}"  # check if env var is set (even if empty)
if [ "$ENV_FLAGS" = "__SET__" ]; then
    DEFAULT_FLAGS="${!DEFAULT_FLAGS_VAR}"
else
    # Read from .storage/cli_flags.json
    CLI_FLAGS_FILE="$STORAGE_DIR/cli_flags.json"
    DEFAULT_FLAGS=""
    if [ -f "$CLI_FLAGS_FILE" ]; then
        DEFAULT_FLAGS=$("$PYTHON_CMD" -c "
import json, sys
try:
    data = json.loads(open(sys.argv[1]).read())
    print(data.get(sys.argv[2], ''))
except Exception:
    pass
" "$CLI_FLAGS_FILE" "$SELECTED" 2>/dev/null)
    fi
fi
# shellcheck disable=SC2086
CLI_FLAGS=($DEFAULT_FLAGS)

# Launch the selected CLI with tag, default flags, user flags, and any remaining args
# --cli tells leap-main.sh the user explicitly chose this CLI (via selector).
exec "$SCRIPT_DIR/leap-main.sh" "$TAG" --cli "$SELECTED" "${CLI_FLAGS[@]}" "${FLAGS[@]}" "${ARGS[@]}"
