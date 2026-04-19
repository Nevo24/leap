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
# invoked.  The hook may run from any cwd (the CLI process's cwd, not
# necessarily the Leap repo), so we resolve ``src/`` from __file__.
_THIS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _THIS_DIR.parent
if str(_SRC_DIR) not in sys.path:
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
    _debug_log('record-ok', cli=cli, tag=tag, session_id=session_id)
    record_session(
        storage_dir, cli, tag,
        session_id=session_id,
        transcript_path=hook_data.get('transcript_path', '') or '',
        cwd=hook_data.get('cwd', '') or os.getcwd(),
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


def main() -> None:
    if len(sys.argv) < 3:
        # Leap's contract: arg1 = state, arg2 = signal file path.
        sys.exit(0)
    state = sys.argv[1]
    signal_file = sys.argv[2]

    _debug_log('hook-enter',
               state=state,
               leap_tag=os.environ.get('LEAP_TAG', '<unset>'),
               leap_cli_provider=os.environ.get('LEAP_CLI_PROVIDER', '<unset>'),
               leap_signal_dir=os.environ.get('LEAP_SIGNAL_DIR', '<unset>'))

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
