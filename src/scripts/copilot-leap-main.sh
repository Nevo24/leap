#!/bin/bash
# GitHub Copilot launcher — delegates to leap-main.sh with CLI preset
export LEAP_CLI="copilot"
exec "$(dirname "${BASH_SOURCE[0]}")/leap-main.sh" "$@"
