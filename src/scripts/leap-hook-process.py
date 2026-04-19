#!/usr/bin/env python3
"""Leap hook processor — shared across every CLI provider.

The CLI invokes ``leap-hook.sh <state>`` on lifecycle events; that
shell script resolves LEAP_TAG / LEAP_SIGNAL_DIR / LEAP_PYTHON and then
execs this module with the state arg + signal path.  This module:

  1. Writes the ``{'state': ...}`` JSON into the signal file ASAP so
     the server wakes up before we do any slower work.
  2. Reads the hook's stdin JSON payload (with a 5 s timeout — some CLIs
     don't close stdin promptly).
  3. Asks the CLI provider for the session id (for ``leap --resume``)
     and records it under ``.storage/cli_sessions/<cli>/<tag>.json``.
  4. Extracts the last assistant message (for the Slack integration),
     either from ``last_assistant_message`` directly or by tailing
     Claude-style JSONL transcripts.
  5. Rewrites the signal file with the enriched data.

All errors are swallowed — the hook must never fail in a way that
interrupts the CLI user.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

# Make the ``leap`` package importable regardless of how this script is
# invoked.  The hook may run from any cwd (it's the CLI process's cwd,
# not necessarily the Leap repo), so we resolve ``src/`` from __file__.
_THIS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _THIS_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

try:
    from leap.cli_providers.registry import get_provider
except ImportError:
    get_provider = None


def _read_stdin_with_timeout(timeout: float = 5.0) -> str:
    """Read all of stdin, giving up after `timeout` seconds.

    Codex has been observed to leave stdin open past the hook's useful
    lifetime; without a timeout the hook would hang indefinitely.
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


def _resolve_storage_dir(signal_dir: str) -> Path:
    """``.storage`` is the parent of ``LEAP_SIGNAL_DIR`` (which points at
    ``.storage/sockets``).  Trim any trailing slash before taking dirname.
    """
    return Path(os.path.dirname(signal_dir.rstrip('/')))


def _record_session(
    storage_dir: Path,
    cli_name: str,
    tag: str,
    hook_data: dict,
) -> None:
    """Ask the CLI provider for the session id and append it (dedupe by id)
    into ``<storage>/cli_sessions/<cli>/<tag>.json``.  Best-effort — any
    failure is silently dropped so the signal file still gets written.
    """
    if get_provider is None:
        _debug_log('record-skip', reason='no-leap-import')
        return
    if not (cli_name and tag):
        _debug_log('record-skip', reason='missing-tag-or-cli', cli=cli_name, tag=tag)
        return
    try:
        provider = get_provider(cli_name)
    except ValueError:
        _debug_log('record-skip', reason='unknown-cli', cli=cli_name)
        return
    if provider is None or not provider.supports_resume:
        _debug_log('record-skip', reason='provider-no-resume', cli=cli_name)
        return
    session_id = provider.extract_session_id(hook_data)
    if not session_id:
        _debug_log('record-skip', reason='no-session-id',
                   cli=cli_name,
                   has_transcript=bool(hook_data.get('transcript_path')),
                   has_sid_field=bool(hook_data.get('session_id')),
                   hook_keys=sorted(hook_data.keys()) if hook_data else [])
        return
    _debug_log('record-ok', cli=cli_name, tag=tag, session_id=session_id)
    sessions_dir = storage_dir / 'cli_sessions' / cli_name
    try:
        sessions_dir.mkdir(parents=True, exist_ok=True)
        tag_file = sessions_dir / f'{tag}.json'
        entries: list = []
        if tag_file.is_file():
            try:
                loaded = json.loads(tag_file.read_text())
                if isinstance(loaded, list):
                    entries = loaded
            except (json.JSONDecodeError, OSError):
                entries = []
        entries = [
            e for e in entries
            if isinstance(e, dict) and e.get('session_id') != session_id
        ]
        entries.append({
            'session_id': session_id,
            'transcript_path': hook_data.get('transcript_path', '') or '',
            'cwd': hook_data.get('cwd', '') or os.getcwd(),
            'last_seen': time.time(),
        })
        entries = entries[-20:]
        tmp = tag_file.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(entries, indent=2))
        os.replace(tmp, tag_file)
    except (OSError, ValueError):
        pass


def _tail_claude_transcript(transcript_path: str) -> str:
    """Scan the last 32 KB of a Claude JSONL transcript for the most
    recent assistant text.  Used by the Slack integration; harmless
    no-op for CLIs that don't have Claude-style transcripts.
    """
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return ''
    try:
        chunk = 32768
        with open(transcript_path, 'rb') as f:
            f.seek(max(0, size - chunk))
            tail = f.read()
    except OSError:
        return ''
    for raw in reversed(tail.split(b'\n')):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if entry.get('type') != 'assistant':
            continue
        parts = [
            c.get('text', '')
            for c in entry.get('message', {}).get('content', [])
            if c.get('type') == 'text'
        ]
        joined = '\n'.join(p for p in parts if p)
        if joined:
            return joined
    return ''


def _debug_log(event: str, **fields) -> None:
    """Append a debug line to ``.storage/logs/hook-debug.log`` if the
    directory already exists.  Used to diagnose resume-recording issues —
    remove or gate behind an env var once stable.
    """
    try:
        leap_signal_dir = os.environ.get('LEAP_SIGNAL_DIR', '')
        if not leap_signal_dir:
            return
        storage = Path(os.path.dirname(leap_signal_dir.rstrip('/')))
        log_dir = storage / 'logs'
        if not log_dir.is_dir():
            return  # user hasn't opted in by creating the dir
        line = {'ts': time.time(), 'event': event, **fields}
        with open(log_dir / 'hook-debug.log', 'a') as f:
            f.write(json.dumps(line) + '\n')
    except Exception:
        pass


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

    # Flush an early "just the state" signal so the server sees the state
    # change without waiting for stdin/transcript work below.
    try:
        with open(signal_file, 'w') as f:
            json.dump(signal, f)
    except OSError:
        pass

    raw_stdin = _read_stdin_with_timeout(timeout=5.0)
    hook_data: dict = {}
    try:
        hook_data = json.loads(raw_stdin) if raw_stdin else {}
    except json.JSONDecodeError:
        hook_data = {}

    # Notification message (permission prompts, elicitation dialogs, etc.)
    notification_msg = hook_data.get('message', '')
    if notification_msg:
        signal['notification_message'] = notification_msg

    # Last assistant message for Slack — prefer the direct field (Codex),
    # otherwise tail the Claude-style transcript.
    direct_msg = hook_data.get('last_assistant_message', '')
    if direct_msg:
        signal['last_assistant_message'] = direct_msg
    else:
        transcript_path = hook_data.get('transcript_path', '')
        if transcript_path:
            tailed = _tail_claude_transcript(transcript_path)
            if tailed:
                signal['last_assistant_message'] = tailed

    # Record the session id for `leap --resume`.
    leap_tag = os.environ.get('LEAP_TAG', '')
    leap_signal_dir = os.environ.get('LEAP_SIGNAL_DIR', '')
    cli_name = os.environ.get('LEAP_CLI_PROVIDER', '')
    if leap_tag and leap_signal_dir and cli_name:
        _record_session(
            _resolve_storage_dir(leap_signal_dir),
            cli_name,
            leap_tag,
            hook_data,
        )

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
        # Absolutely must not fail in a way that breaks the CLI.
        pass
    # Some CLIs (Gemini) expect JSON on stdout; harmless for the others.
    print('{}')
