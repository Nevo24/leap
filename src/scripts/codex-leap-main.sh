#!/bin/bash
# OpenAI Codex launcher — delegates to leap-main.sh with CLI preset
export LEAP_CLI="codex"
exec "$(dirname "${BASH_SOURCE[0]}")/leap-main.sh" "$@"
