#!/bin/bash
#
# ClaudeQ Hook Script for Claude Code
#
# Called by Claude Code's hooks system on Stop and Notification events.
# Writes state to a signal file that the CQ server reads.
#
# The state is passed as the first argument by the hook configuration:
#   claudeq-hook.sh idle             (Stop hook)
#   claudeq-hook.sh needs_permission (Notification/permission_prompt)
#   claudeq-hook.sh has_question     (Notification/elicitation_dialog)
#
# Environment variables (set by CQ server via PTY):
#   CQ_TAG        - Session tag name
#   CQ_SIGNAL_DIR - Directory for signal files
#

# Non-CQ sessions: exit silently
[ -z "$CQ_TAG" ] && exit 0
[ -z "$CQ_SIGNAL_DIR" ] && exit 0

STATE="$1"
[ -z "$STATE" ] && exit 0

echo "{\"state\":\"$STATE\"}" > "$CQ_SIGNAL_DIR/$CQ_TAG.signal"
exit 0
