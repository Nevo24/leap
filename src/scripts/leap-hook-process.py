#!/usr/bin/env python3
"""Leap hook processor — shared across every CLI provider.

The CLI invokes ``leap-hook.sh <state>`` on lifecycle events; that
shell script resolves ``LEAP_TAG`` / ``LEAP_SIGNAL_DIR`` / ``LEAP_PYTHON``
(falling back to walking the PPID chain for a
``<project>/.storage/pid_maps/<ppid>.json`` map when env vars were
stripped by the CLI — the project dir itself is recovered from
``$LEAP_PROJECT_DIR`` or a regex read of ``~/.zshrc`` / ``~/.bashrc``)
and then execs this module with ``<state>`` + the target signal file
path.

Responsibilities:

  1. Write the ``{"state": ...}`` JSON into the signal file ASAP so
     the server wakes up before we do any slower work.
  2. Read the hook's stdin JSON payload (with a 5 s timeout — some CLIs
     don't close stdin promptly).
  3. Ask the CLI provider for the session id and call
     :func:`record_session` — the picker picks it up next time.
  4. Ask the provider for the last assistant message (for Slack), and
     merge any notification text, then rewrite the signal file.

All CLI-specific logic (id extraction, transcript tailing) lives on
``CLIProvider`` — this script only orchestrates.  All errors are
swallowed so the hook never breaks the CLI user's session.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

# Make the ``leap`` package importable regardless of how this script is
# invoked.  Three layouts need to work:
#
#   1. Running directly from the source checkout
#      ``<leap>/src/scripts/leap-hook-process.py`` — ``__file__.parent.parent``
#      is the ``src/`` dir.
#   2. Installed copy under ``~/.claude/hooks/leap-hook-process.py`` (CLAUDE)
#      — ``__file__.parent.parent`` is ``~/.claude/`` and has no ``leap``
#      package, so we need a different anchor.
#   3. Installed copies under ``~/.codex/leap-hooks/`` or other CLI hook
#      dirs — same problem as (2).
#
# For (2)/(3) we recover the leap source from ``LEAP_SIGNAL_DIR``
# (always ``<leap>/.storage/sockets``) or the ``project-path`` file
# ``make install`` writes next to it.  This was the original silent-
# failure root cause behind ``leap --resume`` not seeing new sessions:
# before the May 10 PYTHONPATH-strip commit, the user's shell-exported
# ``PYTHONPATH`` happened to point at ``<leap>/src`` and the bogus
# ``_SRC_DIR = ~/.claude`` was masked.  After the strip, the import
# failed silently for every hook fire.
def _find_leap_src() -> "Path | None":
    # 1. Source-checkout layout.
    cand = Path(__file__).resolve().parent.parent
    if (cand / 'leap').is_dir():
        return cand
    signal_dir = os.environ.get('LEAP_SIGNAL_DIR', '')
    if signal_dir:
        # 2. ``<leap>/.storage/sockets`` → ``<leap>/src``
        cand = Path(signal_dir).parent.parent / 'src'
        if (cand / 'leap').is_dir():
            return cand
        # 3. ``<leap>/.storage/project-path`` file content + ``/src``
        ppf = Path(signal_dir).parent / 'project-path'
        try:
            cand = Path(ppf.read_text().strip()) / 'src'
            if (cand / 'leap').is_dir():
                return cand
        except OSError:
            pass
    return None


_SRC_DIR = _find_leap_src()
if _SRC_DIR is not None and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

try:
    from leap.cli_providers.registry import get_provider
    from leap.utils.resume_store import record_session
except ImportError:
    get_provider = None
    record_session = None


def _read_stdin_with_timeout(timeout: float = 5.0) -> str:
    """Read all of stdin, giving up after ``timeout`` seconds.

    Codex has been observed to leave stdin open past the hook's useful
    lifetime; without a timeout the hook would hang indefinitely.  The
    reader is daemon-threaded so a timed-out read doesn't block exit.
    """
    buf: list[str] = ['']

    def _read() -> None:
        try:
            buf[0] = sys.stdin.read()
        except Exception:
            pass

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return buf[0]


def _parse_hook_data(raw: str) -> dict:
    """Best-effort JSON parse that always returns a dict.

    Guards against payloads that are valid JSON but not objects
    (lists, numbers, strings) — ``.get()`` on those would crash.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _resolve_storage_dir(signal_dir: str) -> Path:
    """``.storage`` is the parent of ``LEAP_SIGNAL_DIR`` (``.storage/sockets``)."""
    return Path(os.path.dirname(signal_dir.rstrip('/')))


def _debug_log(event: str, **fields) -> None:
    """Append a JSONL line to ``.storage/logs/hook-debug.log`` if that
    directory already exists.  Opt-in — users ``mkdir -p .storage/logs``
    when they need to diagnose resume-recording issues.
    """
    try:
        signal_dir = os.environ.get('LEAP_SIGNAL_DIR', '')
        if not signal_dir:
            return
        log_dir = _resolve_storage_dir(signal_dir) / 'logs'
        if not log_dir.is_dir():
            return
        line = {'ts': time.time(), 'event': event, **fields}
        with open(log_dir / 'hook-debug.log', 'a') as f:
            f.write(json.dumps(line) + '\n')
    except Exception:
        pass


def _record(cli: str, tag: str, hook_data: dict) -> None:
    """Ask the provider for the session id and persist it."""
    if get_provider is None or record_session is None:
        _debug_log('record-skip', reason='no-leap-import')
        return
    if not (cli and tag):
        _debug_log('record-skip', reason='missing-tag-or-cli', cli=cli, tag=tag)
        return
    try:
        provider = get_provider(cli)
    except ValueError:
        _debug_log('record-skip', reason='unknown-cli', cli=cli)
        return
    if provider is None or not provider.supports_resume:
        _debug_log('record-skip', reason='provider-no-resume', cli=cli)
        return
    session_id = provider.extract_session_id(hook_data)
    if not session_id:
        _debug_log('record-skip', reason='no-session-id',
                   cli=cli,
                   has_transcript=bool(hook_data.get('transcript_path')),
                   has_sid_field=bool(hook_data.get('session_id')),
                   hook_keys=sorted(hook_data.keys()))
        return
    signal_dir = os.environ.get('LEAP_SIGNAL_DIR', '')
    storage_dir = _resolve_storage_dir(signal_dir)
    # The live server already detected and persisted the terminal/IDE
    # app + project root at startup into ``<tag>.meta``'s ``ide`` and
    # ``project_path`` fields.  Snapshot both into the resume record so
    # the GUI resume dialog can reopen the session in the same terminal
    # app AND open the IDE at the *project root* (not the deeper cwd
    # subdir, which may have its own ``.idea/`` and would be opened as
    # a separate project — see Move-to-IDE's split).  Best-effort: a
    # missing or corrupt meta file just means we record empty fields
    # for this hook fire — ``record_session`` then preserves the
    # previously-recorded values so a transient failure doesn't lose data.
    terminal_app = ''
    project_path = ''
    try:
        with open(storage_dir / 'sockets' / f'{tag}.meta') as f:
            data = json.load(f)
        if isinstance(data, dict):
            terminal_app = data.get('ide') or ''
            project_path = data.get('project_path') or ''
    except (OSError, json.JSONDecodeError):
        pass
    transcript_path = hook_data.get('transcript_path', '') or ''
    # Cursor Agent resumes by chat UUID (``--resume <id>``) and stores chats
    # in a cwd-derived directory, not a transcript file - the rest of the
    # resume machinery assumes its records carry no transcript_path (see
    # relocate_records / leap-resume.py).  If Cursor's hook ever sends one,
    # the picker's path-based stale filter (os.path.getsize) would drop a
    # perfectly resumable chat before session_exists could confirm it.
    # Enforce the convention at write time so read-time stays consistent.
    if cli == 'cursor-agent':
        transcript_path = ''
    _debug_log('record-ok', cli=cli, tag=tag, session_id=session_id)
    record_session(
        storage_dir, cli, tag,
        session_id=session_id,
        transcript_path=transcript_path,
        cwd=hook_data.get('cwd', '') or os.getcwd(),
        terminal_app=terminal_app,
        project_path=project_path,
    )


def _last_assistant_message(cli: str, hook_data: dict) -> str:
    """Defer to the provider; cheap fallback if anything is off."""
    if get_provider is None or not cli:
        return hook_data.get('last_assistant_message', '') or ''
    try:
        provider = get_provider(cli)
    except ValueError:
        return hook_data.get('last_assistant_message', '') or ''
    return provider.extract_last_assistant_message(hook_data)


def _resolve_auto_send_mode(tag: str, storage_dir: Path) -> str:
    """Mirror the server's per-tag-then-global lookup for ``auto_send_mode``.

    Per-tag value in ``pinned_sessions.json[tag].auto_send_mode`` wins;
    otherwise fall back to the global default in ``settings.json``;
    otherwise ``'pause'``.  Returned values are the literal string forms
    of :class:`leap.cli_providers.states.AutoSendMode` (``'always'`` /
    ``'pause'``) — kept as plain strings so this script stays importable
    without the leap package on PATH.
    """
    pinned_file = storage_dir / 'pinned_sessions.json'
    try:
        if pinned_file.is_file():
            with open(pinned_file) as f:
                pinned = json.load(f)
            if isinstance(pinned, dict):
                entry = pinned.get(tag, {})
                if isinstance(entry, dict):
                    mode = entry.get('auto_send_mode')
                    if isinstance(mode, str) and mode:
                        return mode
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    settings_file = storage_dir / 'settings.json'
    try:
        if settings_file.is_file():
            with open(settings_file) as f:
                settings = json.load(f)
            if isinstance(settings, dict):
                mode = settings.get('auto_send_mode')
                if isinstance(mode, str) and mode:
                    return mode
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return 'pause'


def _handle_auto_approve() -> None:
    """Emit a ``PermissionRequest`` decision based on ``auto_send_mode``.

    Fires from Claude's ``PermissionRequest`` hook (matcher ``.*``)
    BEFORE the permission dialog is rendered.  When the session's mode
    is ``ALWAYS``, we write the canonical "allow" decision to stdout and
    Claude skips the dialog entirely — including for tool calls
    originating inside subagents, which fixes the long-standing gap
    where the older ``Notification(permission_prompt)`` path could lose
    the signal during sustained RUNNING (no ``Stop`` hook fires for
    subagents, so the Late Notification guard had no
    ``_last_running_snapshot`` fallback and dropped the signal when the
    dialog hadn't pyte-rendered yet).

    PAUSE mode falls through to the trailing ``print('{}')`` — empty
    response means "no decision", so Claude shows the dialog as normal.

    Deliberately does NOT touch the signal file: this is a hook
    decision, not a state transition.  Leap's state tracker stays
    RUNNING throughout, just as if no permission had ever been needed.
    """
    tag = os.environ.get('LEAP_TAG', '')
    signal_dir = os.environ.get('LEAP_SIGNAL_DIR', '')
    if not tag or not signal_dir:
        # No Leap context — likely a non-Leap session that happens to
        # have inherited our hook.  Defer the decision to Claude.
        _debug_log('auto-approve-skip', reason='missing-context',
                   leap_tag=tag, leap_signal_dir=signal_dir)
        return
    storage_dir = _resolve_storage_dir(signal_dir)
    mode = _resolve_auto_send_mode(tag, storage_dir)
    _debug_log('auto-approve-decision', tag=tag, mode=mode)
    if mode != 'always':
        return
    decision = {
        'hookSpecificOutput': {
            'hookEventName': 'PermissionRequest',
            'decision': {'behavior': 'allow'},
        },
    }
    # ``print`` (not ``sys.stdout.write``) for the trailing newline —
    # matches the module's other JSON-on-stdout emit (the trailing
    # ``print('{}')`` in ``__main__``) and avoids any ambiguity with
    # newline-delimited hook protocols.  SystemExit propagates past
    # the module-level ``except Exception`` (it inherits from
    # BaseException), so the trailing ``print('{}')`` is skipped and
    # our decision is the only JSON on stdout.
    print(json.dumps(decision))
    sys.exit(0)


def main() -> None:
    if len(sys.argv) < 3:
        # Leap's contract: arg1 = state, arg2 = signal file path.
        sys.exit(0)
    state = sys.argv[1]
    signal_file = sys.argv[2]

    # ``python_exe`` / ``leap_python`` are how we'd diagnose another
    # "every hook silently skips recording" regression like the May 10
    # pexpect-import one: tail this log and check whether the running
    # interpreter is the venv (with leap's deps) or a bare PATH python3.
    _debug_log('hook-enter',
               state=state,
               leap_tag=os.environ.get('LEAP_TAG', '<unset>'),
               leap_cli_provider=os.environ.get('LEAP_CLI_PROVIDER', '<unset>'),
               leap_signal_dir=os.environ.get('LEAP_SIGNAL_DIR', '<unset>'),
               python_exe=sys.executable,
               leap_python=os.environ.get('LEAP_PYTHON', '<unset>'))

    if state == 'auto_approve':
        _handle_auto_approve()
        return

    signal: dict = {'state': state}

    # Flush a bare-minimum signal immediately so the server's state
    # tracker reacts before we do any stdin/transcript work.
    try:
        with open(signal_file, 'w') as f:
            json.dump(signal, f)
    except OSError:
        pass

    hook_data = _parse_hook_data(_read_stdin_with_timeout())

    notification_msg = hook_data.get('message', '')
    if isinstance(notification_msg, str) and notification_msg:
        signal['notification_message'] = notification_msg

    cli = os.environ.get('LEAP_CLI_PROVIDER', '')
    last_msg = _last_assistant_message(cli, hook_data)
    if last_msg:
        signal['last_assistant_message'] = last_msg

    tag = os.environ.get('LEAP_TAG', '')
    if tag and cli and os.environ.get('LEAP_SIGNAL_DIR', ''):
        _record(cli, tag, hook_data)

    # Final, enriched signal write.
    try:
        with open(signal_file, 'w') as f:
            json.dump(signal, f)
    except OSError:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # The hook must never fail in a way that breaks the CLI.
        pass
    # Some CLIs (Gemini) expect JSON on stdout; harmless for the others.
    print('{}')
