#!/bin/bash
# Cursor Agent launcher — delegates to leap-main.sh with CLI preset
export LEAP_CLI="cursor-agent"
exec "$(dirname "${BASH_SOURCE[0]}")/leap-main.sh" "$@"
