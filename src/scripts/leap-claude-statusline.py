#!/usr/bin/env python3
"""Leap status line for the Claude Code CLI.

Claude's transcript records per-turn token usage but **not** the resolved
context-window size, and the window is genuinely ambiguous: Opus 4.6+ is 1M on
Max/Team/Enterprise plans (auto-upgraded with no ``[1m]`` suffix anywhere on
disk) but 200K elsewhere, and ``CLAUDE_CODE_DISABLE_1M_CONTEXT`` / model aliases
can flip it.  The **only** authoritative signal is the status-line payload
Claude pipes to a configured ``statusLine`` command on stdin every render: its
``context_window.context_window_size`` is the real window (200000 or 1000000),
already resolved by Claude for the plan/env/selection.  Leap installs this
script as that status line (see ``ClaudeProvider.configure_hooks``) so the
monitor's "Context" column shows the true window instead of guessing.

Two jobs, both best-effort and never fatal (a status line must always return
valid output or Claude's UI breaks):

1. **Record usage** - map the payload to ``{used_tokens, window, model}`` and
   atomically write ``$LEAP_SIGNAL_DIR/<LEAP_TAG>.context`` (the same file the
   monitor reads for Copilot).  Skipped for non-Leap Claude sessions (no
   ``LEAP_TAG``), so installing this globally is harmless.  Nothing is written
   when the payload lacks ``context_window`` (older Claude builds) - the monitor
   then falls back to its transcript-based heuristic.
2. **Stay invisible** (capture-only, per Leap's design choice) - if the user
   already had a status line, Leap saved its command to ``leap-statusline-chain``
   next to this script; we run it with the same stdin and echo its output.  With
   no prior status line we print **nothing**, so Leap adds no bar to Claude's TUI.

Self-contained (stdlib only) so it starts fast on every render and needs no
Leap import.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Optional

_CHAIN_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'leap-statusline-chain')


def _int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def extract_state(payload: object) -> Optional[dict]:
    """Map Claude's status-line JSON payload to ``{used_tokens, window, model}``.

    Returns None when there's no usable window (e.g. an older Claude build that
    doesn't emit ``context_window``), so the monitor falls back to the transcript.

    ``used_tokens`` is computed input-only (input + cache_creation + cache_read,
    excluding output) to match both Claude's own ``used_percentage`` and Leap's
    transcript-based ``_claude_usage_from_tail``.  We prefer ``current_usage``
    (unambiguously the live context) and fall back to ``total_input_tokens``
    (which is cumulative on Claude builds before v2.1.132, so it's the second
    choice).  ``window`` is the resolved ``context_window_size``.
    """
    if not isinstance(payload, dict):
        return None
    cw = payload.get('context_window')
    if not isinstance(cw, dict):
        return None
    window = _int(cw.get('context_window_size'))
    if window <= 0:
        return None
    cu = cw.get('current_usage')
    if isinstance(cu, dict):
        used = (_int(cu.get('input_tokens'))
                + _int(cu.get('cache_creation_input_tokens'))
                + _int(cu.get('cache_read_input_tokens')))
    else:
        used = _int(cw.get('total_input_tokens'))
    model_field = payload.get('model')
    if isinstance(model_field, dict):
        model = model_field.get('id') or model_field.get('display_name') or ''
    elif isinstance(model_field, str):
        model = model_field
    else:
        model = ''
    return {'used_tokens': used, 'window': window, 'model': str(model)}


def _record(state: dict) -> None:
    """Atomically write the per-tag context state file Leap's monitor reads."""
    tag = os.environ.get('LEAP_TAG', '')
    signal_dir = os.environ.get('LEAP_SIGNAL_DIR', '')
    if not (tag and signal_dir):
        return
    path = os.path.join(signal_dir, f'{tag}.context')
    tmp = f'{path}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'w') as f:
            f.write(json.dumps(state))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _render(raw: bytes) -> None:
    """Capture-only output: run the user's chained status line if one was
    preserved at install time, otherwise print nothing (Leap stays invisible)."""
    try:
        with open(_CHAIN_FILE) as f:
            prev = f.read().strip()
    except OSError:
        return
    if not prev:
        return
    try:
        result = subprocess.run(prev, shell=True, input=raw,
                                capture_output=True, timeout=5)
        sys.stdout.buffer.write(result.stdout)
    except Exception:
        return  # never let a broken chained command break the status line


def main() -> None:
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        raw = b''
    state: Optional[dict] = None
    try:
        state = extract_state(json.loads(raw.decode('utf-8', 'replace') or '{}'))
    except (json.JSONDecodeError, ValueError, UnicodeError):
        state = None
    if state:
        _record(state)
    _render(raw)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # A status line must never crash Claude's UI.
        pass
