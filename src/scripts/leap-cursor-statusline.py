#!/usr/bin/env python3
"""Leap status line for the Cursor Agent CLI.

cursor-agent exposes no token usage in its on-disk session store (the
conversation content is encrypted), but it supports a ``statusLine`` command
in ``~/.cursor/cli-config.json`` - exactly like Claude Code's - to which it
pipes a JSON payload on stdin every render (>= 300ms cadence, spawned with
the CLI's env, so ``LEAP_TAG`` / ``LEAP_SIGNAL_DIR`` flow through).  The
payload deliberately mirrors Claude Code's shape::

    {
      "model": {"id": "composer-2.5", "display_name": "Composer", ...},
      "context_window": {
        "total_input_tokens": <round(used_percentage/100 * window)>,
        "total_output_tokens": ...,
        "context_window_size": <window or null>,
        "used_percentage": <0-100 float or null>,
        "remaining_percentage": ...,
        "current_usage": ...
      },
      ...
    }

(Field set verified against the cursor-agent 2026.06.12 bundle: the payload
is built in the TUI and handed to the configured command via stdin; stdout
becomes the rendered status line.)

Two jobs, both best-effort and never fatal:

1. **Record usage** - extract the current context tokens / window / model and
   atomically write ``$LEAP_SIGNAL_DIR/<LEAP_TAG>.context`` (the file the
   monitor's Context column reads).  Skipped for non-Leap cursor-agent
   sessions (no ``LEAP_TAG``), so installing this globally is harmless.
2. **Stay invisible** - cursor-agent already renders its own context percent
   in the prompt footer, so Leap prints nothing.  If the user had their own
   status line before Leap installed this one, its command was preserved in
   ``leap-statusline-chain`` next to this script; we run it with the same
   stdin and echo its output so their status line still renders.

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
    """Map cursor-agent's status-line JSON payload to ``{used_tokens, window, model}``.

    Returns None when there's no usable window size (e.g. before the first
    turn, when cursor-agent reports ``context_window_size: null``), so no
    state file is written and the monitor shows a blank cell.

    ``used_tokens`` is input-only, preferring the most direct field
    available: ``current_usage`` (summed Claude-style when it's an object,
    taken as-is when it's a number), then ``total_input_tokens`` (which
    cursor-agent derives from the live percent - current context, NOT
    cumulative), then ``used_percentage`` x window.
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
    elif isinstance(cu, (int, float)) and not isinstance(cu, bool):
        used = _int(cu)
    elif isinstance(cw.get('total_input_tokens'), (int, float)):
        used = _int(cw.get('total_input_tokens'))
    else:
        pct = cw.get('used_percentage')
        if not isinstance(pct, (int, float)) or isinstance(pct, bool):
            return None
        used = round(float(pct) / 100 * window)
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
    preserved at install time, otherwise print nothing (Leap stays invisible -
    cursor-agent's own footer already shows the context percent)."""
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
        # A status line must never crash cursor-agent's UI.
        pass
