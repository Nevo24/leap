#!/usr/bin/env python3
"""Leap status line for the GitHub Copilot CLI.

Copilot CLI is the one supported CLI that exposes no per-turn token usage in
its transcript - the live context-window numbers are only available to a
**status line** command, which Copilot pipes a JSON payload to on stdin every
render.  Leap installs this script as that status line (see
``CopilotProvider.configure_hooks``) so the monitor's "Context" column can show
how full a Copilot session's context window is.

Two jobs, both best-effort and never fatal (the status line must always return
valid output or Copilot's UI breaks):

1. **Record usage** - extract the current context tokens / window from the
   payload and atomically write ``$LEAP_SIGNAL_DIR/<LEAP_TAG>.context`` (the
   same dir the monitor reads).  Skipped for non-Leap Copilot sessions (no
   ``LEAP_TAG``), so installing this globally is harmless.
2. **Preserve the user's status line** - if the user already had one, Leap
   saved its command to ``leap-statusline-chain`` next to this script; we run
   it with the same stdin and echo its output, so their status line still
   renders.  With no prior status line we emit a small context indicator.

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
    """Map Copilot's status-line JSON payload to ``{used_tokens, window, model}``.

    Returns None when there's no usable window size.  Defensive about field
    names/types.

    Copilot 1.0.60 nests the token/window fields under a ``context_window``
    object (verified against a live payload); we read from there, falling back
    to the top level so a future flattened schema still works.
    """
    if not isinstance(payload, dict):
        return None
    cw = payload.get('context_window')
    if not isinstance(cw, dict):
        cw = payload  # tolerate a flattened schema
    used = _int(cw.get('current_context_tokens'))
    # Use the practical limit Copilot itself shows in its UI: Copilot computes
    # its own "% used" (current_context_used_percentage, and triggers
    # auto-compaction) against displayed_context_limit, not the raw model
    # maximum - so matching it makes Leap's % agree with what the user sees in
    # Copilot.  Fall back to the raw context_window_size if it's absent.
    window = _int(cw.get('displayed_context_limit')) or _int(
        cw.get('context_window_size'))
    model_field = payload.get('model')
    if isinstance(model_field, dict):
        model = model_field.get('display_name') or model_field.get('id') or ''
    elif isinstance(model_field, str):
        model = model_field
    else:
        model = ''
    if window <= 0:
        return None
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


def _render(raw: bytes, state: Optional[dict]) -> None:
    """Emit the status-line text: the chained user line, else a context line."""
    try:
        with open(_CHAIN_FILE) as f:
            prev = f.read().strip()
    except OSError:
        prev = ''
    if prev:
        try:
            result = subprocess.run(prev, shell=True, input=raw,
                                    capture_output=True, timeout=5)
            sys.stdout.buffer.write(result.stdout)
            return
        except Exception:
            return  # never let a broken chained command break the status line
    if state:
        window = state['window']
        pct = round(100 * state['used_tokens'] / window) if window else 0
        model = state['model']
        sys.stdout.write(f'Context {pct}% ({model})' if model
                         else f'Context {pct}%')


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
    _render(raw, state)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # A status line must never crash Copilot's UI.
        pass
