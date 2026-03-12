#!/bin/bash
# Claude Code launcher — delegates to leap-main.sh with CLI preset
export LEAP_CLI="claude"
exec "$(dirname "${BASH_SOURCE[0]}")/leap-main.sh" "$@"
