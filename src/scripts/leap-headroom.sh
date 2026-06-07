#!/usr/bin/env bash
# leap-headroom.sh - route selected Leap CLIs through Headroom context compression.
# Invoked by `leap --headroom` (not meant to be run directly by users).
# Opt-in, per CLI. Re-runnable (acts as an on/off toggle). No global env changes.
set -euo pipefail
PORT=8787; URL="http://localhost:${PORT}"
export PATH="$HOME/.local/bin:$PATH"
# Write autostart to the RC file Leap itself targets (matches configure-shell-helper / uninstall).
RC="$HOME/.zshrc"
if [ "$(basename "${SHELL:-zsh}")" = "bash" ]; then RC="$HOME/.bashrc"; fi

# --- 1. Install headroom -------------------------------------------------
echo "==> 1/3 Install headroom"
command -v brew >/dev/null || { echo "Need Homebrew: https://brew.sh"; exit 1; }
command -v pipx >/dev/null || { brew install pipx; pipx ensurepath; }
command -v python3.13 >/dev/null || brew install python@3.13   # headroom's deps don't support 3.14 yet
pipx list 2>/dev/null | grep -q headroom-ai || pipx install --python python3.13 "headroom-ai[proxy]"

# --- 2. Auto-start the proxy (background, no global env changes) ----------
echo "==> 2/3 Auto-start the proxy"
M="# >>> leap-headroom >>>"
grep -qF "$M" "$RC" 2>/dev/null || cat >> "$RC" <<EOF

${M}
headroom-up() { lsof -ti tcp:${PORT} >/dev/null 2>&1 && return; mkdir -p ~/.headroom; nohup headroom proxy --port ${PORT} --no-telemetry > ~/.headroom/proxy.log 2>&1 & disown; }
headroom-up
# <<< leap-headroom <<<
EOF
lsof -ti tcp:${PORT} >/dev/null 2>&1 || { mkdir -p ~/.headroom; nohup headroom proxy --port ${PORT} --no-telemetry > ~/.headroom/proxy.log 2>&1 & disown; }

# --- 3. Pick which Leap CLIs route through headroom ----------------------
echo "==> 3/3 Choose which Leap CLIs should use headroom"
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

echo
echo "Done. Start a routed CLI normally (e.g. 'leap mytag' and pick it). Check savings: headroom perf"
