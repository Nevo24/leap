#!/usr/bin/env bash
#
# leap-headroom-up.sh - bring the Headroom proxy up, or recycle it if unhealthy.
#
# Run from the repo (via $LEAP_PROJECT_DIR) by the managed shell block at every
# shell start, and by leap-headroom-watchdog.sh every 5 minutes. It owns the
# health policy; runtime state (locks, marker, log) lives under ~/.headroom.
#
# "Unhealthy" means any of:
#   - not answering on its port, OR
#   - its log shows headroom's HTTP/2 stream-exhaustion wedge
#     ("Max outbound streams is 100"), where the port still LISTENS and a bare
#     GET / still returns 421 but every upstream request fails - so the port
#     answering is not sufficient evidence of health, OR
#   - it has been up > 24h (a backstop for any wedge the log scan misses).
# Only a restart clears the wedge. Age is tracked via a start-time marker file
# (portable: BSD/macOS ps has no `etimes`).
export PATH="$HOME/.local/bin:$PATH"
PORT=8787
HRDIR="$HOME/.headroom"
MARKER="$HRDIR/started_at"
LOG="$HRDIR/proxy.log"
UPLOCK="$HRDIR/up.lock"
mkdir -p "$HRDIR"

# Serialize: never let two checks (the watcher and a shell-start, which the
# managed block launches near-simultaneously) recycle or start a proxy in
# parallel. The claim is mkdir (atomic) + a pid write that follows it; a loser
# that reads the pid before the winner writes it would otherwise see "empty ->
# stale" and wrongly reclaim. So retry-read the pid briefly: a live claimer
# writes within microseconds (-> defer); only a crash mid-claim leaves it empty
# this long (-> reclaim).
if ! mkdir "$UPLOCK" 2>/dev/null; then
  opid=""
  for _ in 1 2 3 4 5; do
    opid=$(cat "$UPLOCK/pid" 2>/dev/null)
    [ -n "$opid" ] && break
    sleep 0.2
  done
  if [ -n "$opid" ] && kill -0 "$opid" 2>/dev/null; then exit 0; fi
  rm -rf "$UPLOCK"; mkdir "$UPLOCK" 2>/dev/null || exit 0   # stale, or lost the race
fi
echo $$ > "$UPLOCK/pid"
trap 'rm -rf "$UPLOCK"' EXIT

start_proxy() {
  date +%s > "$MARKER"
  # --no-telemetry: no anonymous usage beacon to the author (headroomlabs/supabase).
  # --no-subscription-tracking: no usage/quota poller to api.anthropic.com /
  #   api.github.com (your own providers, but unneeded chatter + local state we
  #   don't use). License phone-home never runs - it requires a license key.
  nohup headroom proxy --port "$PORT" --no-telemetry --no-subscription-tracking > "$LOG" 2>&1 &
}

now=$(date +%s)
started=$(cat "$MARKER" 2>/dev/null)
case "$started" in ''|*[!0-9]*) started=0;; esac   # treat empty/corrupt marker as ancient

if lsof -ti tcp:$PORT >/dev/null 2>&1; then
  code=$(curl -sS -m 3 -o /dev/null -w "%{http_code}" "http://localhost:$PORT/" 2>/dev/null)
  if [ -n "$code" ] && [ "$code" != "000" ] && [ $((now - started)) -lt 86400 ] \
     && ! tail -n 200 "$LOG" 2>/dev/null | grep -q "Max outbound streams"; then
    exit 0   # listening, answering, not wedged, and fresh - leave it alone
  fi
  # Recycle: kill EVERY pid holding the port (parent + any worker that inherited
  # the listening socket), not just the first - otherwise the survivor keeps the
  # port and the fresh proxy can't bind.
  pids=$(lsof -ti tcp:$PORT 2>/dev/null); [ -n "$pids" ] && kill $pids 2>/dev/null
  sleep 1
  pids=$(lsof -ti tcp:$PORT 2>/dev/null); [ -n "$pids" ] && kill -9 $pids 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do lsof -ti tcp:$PORT >/dev/null 2>&1 || break; sleep 0.3; done
  start_proxy
else
  # Not listening. Either it's down, or a very recent start is still cold-loading
  # its model (not yet bound) - in that case don't pile on a second proxy.
  [ $((now - started)) -ge 90 ] && start_proxy
fi
