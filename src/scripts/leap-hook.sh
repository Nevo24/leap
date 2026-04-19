#!/bin/bash
#
# Leap Hook Script for CLI providers (Claude Code, Codex, Cursor Agent, Gemini CLI, etc.)
#
# Called by CLI hooks on Stop and Notification events.
# Writes state (and response text) to a signal file that the Leap server reads.
#
# The state is passed as the first argument by the hook configuration:
#   leap-hook.sh idle             (Stop hook)
#   leap-hook.sh needs_permission (Notification/permission_prompt)
#   leap-hook.sh needs_input      (Notification/elicitation_dialog)
#
# The CLI passes JSON on stdin with session info.  Claude Code includes
# transcript_path; Codex includes last_assistant_message directly.
# Cursor Agent includes status and workspace_roots.
# Gemini CLI includes prompt, prompt_response, and transcript_path.
#
# Environment variables (set by Leap server via PTY):
#   LEAP_TAG          - Session tag name
#   LEAP_SIGNAL_DIR   - Directory for signal files
#   LEAP_CLI_PROVIDER - CLI provider name (routes `leap --resume` recordings)
#   LEAP_PROJECT_DIR  - Leap project root (exported by the user's shell rc)
#
# Fallback: if env vars are missing (some CLIs don't pass the parent
# environment to hook subprocesses), the script recovers context by:
#   1. Regex-reading `export LEAP_PROJECT_DIR="…"` out of the user's
#      `~/.zshrc` / `~/.bashrc` / `~/.bash_profile` — the same line
#      `make install` already writes.
#   2. Walking up the PPID chain looking for
#      `<project>/.storage/pid_maps/<ppid>.json` which the Leap server
#      writes at CLI-spawn time.
#

STATE="$1"
[ -z "$STATE" ] && echo '{}' && exit 0

# Use venv Python if available (set by Leap server), fall back to PATH python3.
# Homebrew-only installs may not have python3 in PATH inside CLI subshells.
PYTHON="${LEAP_PYTHON:-python3}"

# If LEAP_TAG / LEAP_SIGNAL_DIR / LEAP_CLI_PROVIDER are missing (some CLIs
# like Codex strip env vars from hook subprocesses), recover them by
# walking up the PPID chain looking for ``<project>/.storage/pid_maps/<ppid>.json``
# written by the Leap server.  The ``<project>`` dir is found via either:
#   1. ``$LEAP_PROJECT_DIR`` env (set by user's shell rc) — fast path, OR
#   2. regex-reading the same ``export LEAP_PROJECT_DIR="…"`` line that
#      ``make install`` wrote into ``~/.zshrc`` / ``~/.bashrc``.
# That's the single anchor from which every other piece of context is
# discoverable — no ``/tmp``, no separate config file.
if [ -z "$LEAP_TAG" ] || [ -z "$LEAP_SIGNAL_DIR" ] || [ -z "$LEAP_CLI_PROVIDER" ]; then
    RESOLVED=$("$PYTHON" -c "
import json, os, re, subprocess

def get_ppid(pid):
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('PPid:'):
                    return int(line.split()[1])
    except (FileNotFoundError, OSError):
        pass
    try:
        r = subprocess.run(['ps', '-o', 'ppid=', '-p', str(pid)],
                           capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            return int(r.stdout.strip())
    except Exception:
        pass
    return None

def find_project_dir():
    # Prefer the env var if it survived.
    env = os.environ.get('LEAP_PROJECT_DIR', '')
    if env and os.path.isdir(env):
        return env
    # Otherwise regex the shell rc files that \`make install\` edits.
    home = os.path.expanduser('~')
    pat = re.compile(r'^\s*export\s+LEAP_PROJECT_DIR=\"([^\"]+)\"', re.M)
    for rc in ('.zshrc', '.bashrc', '.bash_profile'):
        try:
            with open(os.path.join(home, rc)) as f:
                m = pat.search(f.read())
            if m and os.path.isdir(m.group(1)):
                return m.group(1)
        except OSError:
            continue
    return None

proj = find_project_dir()
if proj is None:
    exit(0)
pid_map_dir = os.path.join(proj, '.storage', 'pid_maps')

pid = os.getpid()
for _ in range(10):
    ppid = get_ppid(pid)
    if ppid is None or ppid <= 1:
        break
    path = os.path.join(pid_map_dir, f'{ppid}.json')
    if os.path.isfile(path):
        try:
            d = json.loads(open(path).read())
            tag, sd = d.get('tag',''), d.get('signal_dir','')
            py = d.get('python','')
            cli = d.get('cli_provider','')
            # Only accept a mapping that identifies the CLI — otherwise
            # an old-format map (pre-leap-resume) would mis-attribute
            # an unrelated child run to the wrong tag/provider.
            if tag and sd and cli:
                print(f'{tag}|{sd}|{py}|{cli}')
                break
        except Exception:
            pass
    pid = ppid
" < /dev/null 2>/dev/null)

    if [ -n "$RESOLVED" ]; then
        IFS='|' read -r _TAG _DIR _PY _CLI <<< "$RESOLVED"
        [ -z "$LEAP_TAG" ] && LEAP_TAG="$_TAG"
        [ -z "$LEAP_SIGNAL_DIR" ] && LEAP_SIGNAL_DIR="$_DIR"
        [ -z "$LEAP_CLI_PROVIDER" ] && [ -n "$_CLI" ] && LEAP_CLI_PROVIDER="$_CLI"
        [ -n "$_PY" ] && PYTHON="$_PY"
        export LEAP_TAG LEAP_SIGNAL_DIR LEAP_CLI_PROVIDER
    fi
fi

# Non-Leap sessions: exit silently (echo '{}' for CLIs that expect JSON stdout)
[ -z "$LEAP_TAG" ] && echo '{}' && exit 0
[ -z "$LEAP_SIGNAL_DIR" ] && echo '{}' && exit 0

SIGNAL_FILE="$LEAP_SIGNAL_DIR/$LEAP_TAG.signal"

# Delegate to the Python helper (leap-hook-process.py) — it does stdin
# parsing, session recording via the provider abstraction, and the
# last-assistant-message extraction for Slack.  Keeping the logic in a
# real .py file (instead of inline `python -c`) avoids shell-escape
# hazards and lets us `import leap.cli_providers.*` normally.
#
# The processor is normally installed alongside this script in each
# CLI's hook dir (~/.codex, ~/.claude/hooks, ...).  If someone is on a
# half-upgraded install where only the .sh copy was refreshed, fall
# back to the source copy via the project-path file the installer
# wrote to LEAP_SIGNAL_DIR/../project-path.
HOOK_DIR="$(dirname "${BASH_SOURCE[0]}")"
HOOK_PROCESSOR="$HOOK_DIR/leap-hook-process.py"
if [ ! -f "$HOOK_PROCESSOR" ]; then
    PROJECT_PATH_FILE="$(dirname "$LEAP_SIGNAL_DIR")/project-path"
    if [ -f "$PROJECT_PATH_FILE" ]; then
        LEAP_ROOT="$(cat "$PROJECT_PATH_FILE")"
        HOOK_PROCESSOR="$LEAP_ROOT/src/scripts/leap-hook-process.py"
    fi
fi

"$PYTHON" "$HOOK_PROCESSOR" "$STATE" "$SIGNAL_FILE" 2>/dev/null

# Fallback if python fails — the helper normally emits '{}' on stdout
# for CLIs (e.g. Gemini) that expect a JSON hook response, so we only
# need to synthesise that here when the helper itself crashed.
if [ $? -ne 0 ]; then
    echo "{\"state\":\"$STATE\"}" > "$SIGNAL_FILE"
    echo '{}'
fi

exit 0
