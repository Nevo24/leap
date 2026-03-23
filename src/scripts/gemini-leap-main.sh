#!/bin/bash
# Gemini CLI launcher — delegates to leap-main.sh with CLI preset
export LEAP_CLI="gemini"
exec "$(dirname "${BASH_SOURCE[0]}")/leap-main.sh" "$@"
