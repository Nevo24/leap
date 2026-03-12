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

# Show interactive CLI selector
SELECTED=$("$PYTHON_CMD" "$SCRIPT_DIR/leap-select-cli.py")
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ] || [ -z "$SELECTED" ]; then
    exit 1
fi

# If no tag provided, prompt for one
if [ -z "$TAG" ]; then
    echo -n "  Session name: " >&2
    read -r TAG
    if [ -z "$TAG" ]; then
        echo "❌ Error: Session name is required." >&2
        exit 1
    fi
fi

# Validate tag
if [[ ! "$TAG" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
    echo "❌ Error: Session name must contain only letters, numbers, hyphens, and underscores" >&2
    exit 1
fi

# Launch the selected CLI with tag, flags, and any remaining args
export LEAP_CLI="$SELECTED"
exec "$SCRIPT_DIR/leap-main.sh" "$TAG" "${FLAGS[@]}" "${ARGS[@]}"
