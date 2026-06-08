#!/usr/bin/env bash
#
# leap-headroom-watchdog.sh - single-instance background loop that re-runs the
# Headroom health check every 5 minutes, so a proxy that wedges mid-session
# self-heals without waiting for a new shell.
#
# A user-owned loop (not launchd) because on managed Macs ~/Library/LaunchAgents
# is root-owned and a user agent would need sudo. An atomic mkdir lock holding
# the loop's pid - with a PID-identity check that guards against PID reuse (the
# lock dir survives reboots) - keeps exactly one watcher alive no matter how many
# shells start it. Finds its companion check script relative to its own path, so
# it does not depend on $LEAP_PROJECT_DIR at runtime.
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCKDIR="$HOME/.headroom/watchdog.lock"

# Ensure the state dir exists before locking. The managed shell block launches
# this and leap-headroom-up.sh concurrently; without this, a watchdog that wins
# the race before up.sh creates ~/.headroom would fail to make its lock dir and
# bail, leaving no periodic watcher.
mkdir -p "$HOME/.headroom"

if ! mkdir "$LOCKDIR" 2>/dev/null; then
  # Claim = mkdir (atomic) + the pid write below. Retry-read the pid so a loser
  # in a simultaneous-launch race (e.g. several terminals restoring at login)
  # doesn't read it before the winner writes it and wrongly reclaim.
  opid=""
  for _ in 1 2 3 4 5; do
    opid=$(cat "$LOCKDIR/pid" 2>/dev/null)
    [ -n "$opid" ] && break
    sleep 0.2
  done
  # Owned only if that pid is genuinely a running watchdog - guards against PID
  # reuse (e.g. after a reboot the lock dir persists but the pid is recycled).
  if [ -n "$opid" ] && ps -p "$opid" -o command= 2>/dev/null | grep -q "leap-headroom-watchdog.sh"; then
    exit 0
  fi
  rm -rf "$LOCKDIR"; mkdir "$LOCKDIR" 2>/dev/null || exit 0   # stale - reclaim, or lose the race
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT

while true; do
  "$SELF_DIR/leap-headroom-up.sh"
  sleep 300
done
