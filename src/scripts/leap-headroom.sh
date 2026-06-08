#!/usr/bin/env bash
# leap-headroom.sh - route selected Leap CLIs through Headroom context compression.
# Invoked by `leap --headroom` (not meant to be run directly by users).
# Opt-in, per CLI. Re-runnable (acts as an on/off toggle). No global env changes.
set -euo pipefail
PORT=8787; URL="http://localhost:${PORT}"
export PATH="$HOME/.local/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Match the rest of the Leap CLI's output style (configure-shell-helper, leap-update, ...)
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'; PROMPT_PREFIX="→"

# --- 1. Install / update headroom ----------------------------------------
echo -e "$PROMPT_PREFIX 1/3 Install / update headroom"
command -v brew >/dev/null || { echo -e "${RED}✗ Need Homebrew: https://brew.sh${NC}"; exit 1; }
command -v pipx >/dev/null || { brew install pipx; pipx ensurepath; }
command -v python3.13 >/dev/null || brew install python@3.13   # headroom's deps don't support 3.14 yet

HEADROOM_UPGRADED=0
if ! pipx list 2>/dev/null | grep -q headroom-ai; then
    pipx install --python python3.13 "headroom-ai[proxy]"
else
    # Already installed. Ask before hitting the network (default No), and only if
    # a newer release exists, offer to upgrade (default Yes). The proxy's wedge
    # bugs are headroom-side, so staying current matters - but the check is opt-in
    # so a routine `leap --headroom` doesn't phone PyPI every time.
    chk=""; read -r -p "  Check for a headroom update? [y/N] " chk || true
    case "$chk" in
        y|Y|yes|YES)
            installed=$(pipx runpip headroom-ai show headroom-ai 2>/dev/null | awk '/^Version:/{print $2}' || true)
            latest=$(curl -fsS -m 10 https://pypi.org/pypi/headroom-ai/json 2>/dev/null \
                     | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])" 2>/dev/null || true)
            if [ -z "$installed" ] || [ -z "$latest" ]; then
                # Couldn't determine one side - don't claim up-to-date we can't prove.
                echo -e "  ${YELLOW}⚠ Update check unavailable (offline?) - keeping current version${NC}"
            elif [ "$installed" != "$latest" ]; then
                echo -e "  ${YELLOW}⚠ Headroom $installed is installed; $latest is available${NC}"
                ans=""; read -r -p "  Upgrade now? [Y/n] " ans || true
                case "$ans" in
                    n|N|no|NO) echo "  Keeping $installed" ;;
                    *)         pipx upgrade headroom-ai && HEADROOM_UPGRADED=1 ;;   # default Yes
                esac
            else
                echo -e "  ${GREEN}✓ Headroom $installed is already up to date${NC}"
            fi
            # The next step opens a full-screen picker that clears the terminal,
            # so pause here to let the update result actually be read.
            read -r -p "  Press Enter to continue... " _ || true
            ;;
        *) : ;;   # skip the check
    esac
fi

# --- 2. Pick which Leap CLIs route through headroom ----------------------
# The proxy auto-start + health watchdog are NOT wired here: their scripts live
# in the repo (src/scripts/leap-headroom-{up,watchdog}.sh) and are launched by
# the managed shell block, which configure-shell-helper.sh regenerates on every
# install/update - gated on the .storage/headroom_enabled marker that step 3
# writes below. Step 3 also refreshes that block and starts/stops the proxy now.
echo -e "$PROMPT_PREFIX 2/3 Choose which Leap CLIs should use headroom"
: "${LEAP_PROJECT_DIR:?Open a new terminal after installing Leap, then re-run}"

# Helper goes in a temp file so its interactive UI reads the terminal
# (a `python3 - <<HEREDOC` would consume stdin and break curses/input()).
HR_PY="$(mktemp "${TMPDIR:-/tmp}/leap-headroom.XXXXXX")"   # no .py suffix: BSD mktemp only randomizes trailing X's
trap 'rm -f "$HR_PY"' EXIT
cat > "$HR_PY" <<'PY'
import json, os, sys

proj, url = sys.argv[1], sys.argv[2]
storage = os.path.join(proj, ".storage")
custom_file = os.path.join(storage, "cli_custom.json")
env_file = os.path.join(storage, "cli_env.json")

# Base type -> env var(s) that route that CLI through headroom (verified vs headroom source).
BASE_ENV = {
    "claude":  {"ANTHROPIC_BASE_URL": url},
    "codex":   {"OPENAI_BASE_URL": url + "/v1"},
    "copilot": {"COPILOT_PROVIDER_TYPE": "anthropic", "COPILOT_PROVIDER_BASE_URL": url},
}
# gemini / cursor-agent: headroom has no reliable env-var route -> shown but not toggleable.

clis = [
    {"key": "claude",       "label": "Claude Code",    "base": "claude"},
    {"key": "codex",        "label": "OpenAI Codex",   "base": "codex"},
    {"key": "copilot",      "label": "GitHub Copilot", "base": "copilot"},
    {"key": "cursor-agent", "label": "Cursor Agent",   "base": "cursor-agent"},
    {"key": "gemini",       "label": "Gemini CLI",     "base": "gemini"},
]
if os.path.exists(custom_file):
    try:
        for e in json.load(open(custom_file)):
            if e.get("id"):
                clis.append({"key": e["id"],
                             "label": (e.get("display_name") or e["id"]) + " (custom)",
                             "base": e.get("base", "")})
    except Exception:
        pass

env = {}
if os.path.exists(env_file):
    try: env = json.load(open(env_file))
    except Exception: env = {}

def supported(c): return c["base"] in BASE_ENV
def is_on(c):
    if not supported(c): return False
    d = env.get(c["key"], {})
    return all(d.get(k) == v for k, v in BASE_ENV[c["base"]].items())

preselected = {i for i, c in enumerate(clis) if is_on(c)}

def curses_pick():
    import curses
    def _ui(stdscr):
        curses.curs_set(0)
        n = len(clis); submit = n           # the Submit button sits just past the last CLI
        idx, checked = 0, set(preselected)
        while True:
            stdscr.erase()
            stdscr.addstr(0, 0, "Route which CLIs through Headroom?")
            stdscr.addstr(1, 0, "up/down move   ENTER toggle   go to [Submit] + ENTER to apply   q cancel")
            for i, c in enumerate(clis):
                sup = supported(c)
                mark = ("[x]" if i in checked else "[ ]") if sup else " - "
                line = f" {mark} {c['label']} ({c['base']})"
                if not sup: line += "   not supported by headroom"
                attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
                if not sup: attr |= curses.A_DIM
                try: stdscr.addstr(i + 3, 0, line[:max(1, curses.COLS - 1)], attr)
                except curses.error: pass
            battr = (curses.A_REVERSE | curses.A_BOLD) if idx == submit else curses.A_BOLD
            try: stdscr.addstr(n + 4, 1, "[ Submit ]", battr)
            except curses.error: pass
            stdscr.refresh()
            k = stdscr.getch()
            if k in (curses.KEY_UP, ord('k')):     idx = (idx - 1) % (n + 1)
            elif k in (curses.KEY_DOWN, ord('j')): idx = (idx + 1) % (n + 1)
            elif k in (10, 13, curses.KEY_ENTER, ord(' ')):
                if idx == submit:
                    return sorted(checked)
                elif supported(clis[idx]):
                    checked.symmetric_difference_update({idx})
            elif k in (ord('q'), 27):
                return None
    return curses.wrapper(_ui)

# Try the checkbox UI; fall back to numbered input when there's no tty
# (piped/non-interactive) or curses can't initialize - avoids leaking escape codes.
chosen, fell_back = None, False
if sys.stdin.isatty() and sys.stdout.isatty():
    try:
        chosen = curses_pick()
    except Exception:
        fell_back = True
else:
    fell_back = True

if fell_back:
    print()
    for i, c in enumerate(clis, 1):
        tag = ("ON" if (i - 1) in preselected else "off") if supported(c) else "n/a"
        s = "supported" if supported(c) else "not supported by headroom"
        print(f"  {i:2}. [{tag:>3}] {c['label']:26} ({c['base']}) - {s}")
    print()
    sel = input("Enter the FULL set of numbers to route (replaces current), blank = cancel: ").strip()
    if not sel:
        print("No changes."); sys.exit(0)
    try:
        idxs = {int(x) - 1 for x in sel.replace(" ", "").split(",") if x}
    except ValueError:
        print("Invalid input, no changes."); sys.exit(0)
    chosen = [i for i in idxs if 0 <= i < len(clis) and supported(clis[i])]
elif chosen is None:
    print("Cancelled, no changes."); sys.exit(0)

chosen = set(chosen)

# Guard against silently clobbering an env var the user already set to a
# DIFFERENT value: show it (old -> new) and confirm before overwriting. On
# "no" we leave those CLIs exactly as they are and apply only the rest.
conflicts = {}
for i, c in enumerate(clis):
    if not supported(c) or i not in chosen or is_on(c):
        continue
    d = env.get(c["key"], {})
    diffs = [(k, d[k], v) for k, v in BASE_ENV[c["base"]].items() if k in d and d[k] != v]
    if diffs:
        conflicts[i] = (c["label"], diffs)
if conflicts:
    print("\nWARNING: enabling Headroom would replace env vars you already set.")
    print("Choose per CLI - overwrite (y) or keep your value (N):")
    for i, (label, diffs) in conflicts.items():
        for k, old, new in diffs:
            print(f"    {label}  {k}:  {old}  ->  {new}")
        try:
            ans = input(f"  Overwrite {label}? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            chosen.discard(i)
            print(f"  -> kept your existing value for {label}")

applied = []
for i, c in enumerate(clis):
    if not supported(c): continue
    vars_ = BASE_ENV[c["base"]]
    if i in chosen:
        if not is_on(c):
            env.setdefault(c["key"], {}).update(vars_)
            applied.append("ON   " + c["label"])
    else:
        # Only strip routing when this CLI is fully OUR config (every one of our
        # keys matches our proxy). Never touch a CLI the user pointed elsewhere,
        # nor a shared-value key (e.g. COPILOT_PROVIDER_TYPE) they may have set.
        if is_on(c):
            d = env[c["key"]]
            for k in vars_:
                d.pop(k, None)
            if not d:
                del env[c["key"]]
            applied.append("OFF  " + c["label"])

if applied:
    os.makedirs(storage, exist_ok=True)
    with open(env_file, "w") as fh:
        json.dump(env, fh, indent=2)
    print("\nHeadroom routing updated:")
    for a in applied: print("  " + a)
else:
    print("\nNo changes.")
PY
python3 "$HR_PY" "$LEAP_PROJECT_DIR" "$URL"

# --- 3. Apply: marker + managed-block refresh + start/stop the proxy -----
# The .storage/headroom_enabled marker is the single switch the managed shell
# block keys off. It's present iff at least one CLI is routed through the proxy
# (port 8787 in cli_env.json). Writing/removing it, then regenerating the block,
# makes the autostart appear/disappear immediately - and persist across updates.
echo -e "$PROMPT_PREFIX 3/3 Apply"
ENV_FILE="$LEAP_PROJECT_DIR/.storage/cli_env.json"
MARKER="$LEAP_PROJECT_DIR/.storage/headroom_enabled"
UP="$SCRIPT_DIR/leap-headroom-up.sh"
WATCH="$SCRIPT_DIR/leap-headroom-watchdog.sh"

# Any routed CLI's base URL contains "localhost:8787" (claude/copilot bare, codex
# with a "/v1" suffix), so match the host:port, not a trailing quote.
if [ -f "$ENV_FILE" ] && grep -q "localhost:${PORT}" "$ENV_FILE"; then
    : > "$MARKER"
    enabled=1
else
    rm -f "$MARKER"
    enabled=0
fi

# Regenerate the managed shell block so the autostart lines now match the marker.
"$SCRIPT_DIR/configure-shell-helper.sh" --update "$LEAP_PROJECT_DIR" >/dev/null

if [ "$enabled" = 1 ]; then
    chmod +x "$UP" "$WATCH" 2>/dev/null || true
    if [ "$HEADROOM_UPGRADED" = 1 ]; then
        # A healthy old-version proxy wouldn't be recycled by the health check,
        # so force it down (and clear the start marker) to bring up the new one.
        pkill -f "headroom proxy --port ${PORT}" 2>/dev/null || true
        rm -f "$HOME/.headroom/started_at"
    fi
    "$UP" || true                              # bring the proxy up now (quick; backgrounds it)
    nohup "$WATCH" >/dev/null 2>&1 & disown    # ensure the health watcher is running
    echo -e "  ${GREEN}✓ Headroom routing is ON - proxy + watchdog running${NC}"
else
    pkill -f "leap-headroom-watchdog.sh" 2>/dev/null || true
    pkill -f "headroom proxy --port ${PORT}" 2>/dev/null || true
    # Clear the start marker too: after a deliberate kill there's no cold-loading
    # proxy, so a later re-enable must not see a "recent start" and skip launching.
    rm -rf "$HOME/.headroom/watchdog.lock" "$HOME/.headroom/up.lock"
    rm -f "$HOME/.headroom/started_at"
    echo -e "  ${YELLOW}⚠ No CLIs routed - proxy + watchdog stopped${NC}"
fi

echo
echo -e "${GREEN}✓ Done${NC} - start a routed CLI normally (e.g. 'leap mytag' and pick it). Check savings: headroom perf"
