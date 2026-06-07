"""Compute a Claude session's context-window usage from its transcript.

Claude Code writes a ``message.usage`` block into every ``type=="assistant"``
entry of its transcript JSONL.  How full the context window currently is -- the
size of the prompt sent on the most recent turn -- is::

    input_tokens + cache_creation_input_tokens + cache_read_input_tokens

``output_tokens`` is the model's reply, not part of the prompt, so it is
excluded.  This module reads the latest such entry and expresses it as a
percentage of the model's context window, so the monitor can show how much
room is left before Claude auto-compacts ("compresses") the conversation.

Mostly side-effect-free, with two small process-local caches (the transcript
parse keyed on its (mtime, size), and Claude's config keyed on a short TTL);
no Qt, no provider imports.  The monitor calls it from its 1s refresh, so an
unchanged transcript costs a single ``os.stat``.

The 1M-token context beta is NOT written into the transcript's model id, so the
window is resolved from two extra signals: (1) Claude's own ``~/.claude.json``
records per-project model usage keyed by the full model id *including* the
``[1m]`` suffix (``projects.<cwd>.lastModelUsage``), and (2) if observed usage
ever exceeds the 200k base window it must be a larger one.  Both are
best-effort; absent any signal we assume the 200k default.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

# Tail size mirrors claude.py's transcript reader -- the latest assistant turn
# (with its usage block) is always within the last few KiB.
_TRANSCRIPT_TAIL_BYTES = 32768

# Every Claude model shipped to date exposes a 200k-token context window.  Map
# any future exceptions here by model id; everything else uses the default.
_DEFAULT_CONTEXT_WINDOW = 200_000
_ONE_M_CONTEXT_WINDOW = 1_000_000
_MODEL_CONTEXT_WINDOW: dict[str, int] = {}

# Claude persists per-project model usage in ``~/.claude.json`` keyed by the
# full model id -- with the ``[1m]`` suffix when the 1M context window was
# active.  That suffix is the only on-disk trace of the 1M selection (the
# transcript drops it), so we read it best-effort.  Cached for a short TTL:
# the file is rewritten constantly during a session, but the 1M choice changes
# rarely, so re-parsing every poll would be wasteful.
_CLAUDE_CONFIG_PATH = os.path.expanduser('~/.claude.json')
_CONFIG_TTL_SECONDS = 60.0
_one_m_projects_cache: Optional[dict[str, set]] = None  # cwd -> set of model ids
_one_m_cache_at = 0.0

# transcript_path -> (st_mtime_ns, st_size, result).  Both hits and misses
# (None) are cached; any append to the transcript bumps mtime/size and
# invalidates the entry.  Keyed by path (one entry per session) so it can't
# grow unbounded as a long-lived transcript keeps appending.
_USAGE_CACHE: dict[str, "tuple[int, int, Optional[ContextUsage]]"] = {}


@dataclass(frozen=True)
class ContextUsage:
    """A single context-window measurement for one Claude session."""

    used_tokens: int
    window: int
    model: str

    @property
    def percent(self) -> int:
        """Used tokens as a whole-number percent of the window, clamped 0..100."""
        if self.window <= 0:
            return 0
        pct = round(100 * self.used_tokens / self.window)
        return max(0, min(100, pct))


def context_window_for_model(model: str) -> int:
    """Context-window size for a Claude model id, defaulting to 200k.

    Tolerant of version suffixes: an exact map hit wins, otherwise any mapped
    key that is a substring of ``model`` matches, else the default.
    """
    if not model:
        return _DEFAULT_CONTEXT_WINDOW
    if model in _MODEL_CONTEXT_WINDOW:
        return _MODEL_CONTEXT_WINDOW[model]
    for key, window in _MODEL_CONTEXT_WINDOW.items():
        if key in model:
            return window
    return _DEFAULT_CONTEXT_WINDOW


def _project_model_ids() -> dict[str, set]:
    """Map of project cwd -> set of model ids from Claude's config.

    Reads ``~/.claude.json``'s ``projects.<cwd>.lastModelUsage`` keys, which
    carry the full model id INCLUDING the ``[1m]`` suffix when the 1M context
    window was active.  Best-effort and TTL-cached (see module note).
    """
    global _one_m_projects_cache, _one_m_cache_at
    now = time.monotonic()
    if (_one_m_projects_cache is not None
            and now - _one_m_cache_at < _CONFIG_TTL_SECONDS):
        return _one_m_projects_cache
    result: dict[str, set] = {}
    try:
        with open(_CLAUDE_CONFIG_PATH) as f:
            data = json.load(f)
        projects = data.get('projects') or {}
        for cwd, ent in projects.items():
            if not isinstance(ent, dict):
                continue
            lmu = ent.get('lastModelUsage') or {}
            if isinstance(lmu, dict):
                result[cwd] = set(lmu.keys())
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        result = {}
    _one_m_projects_cache = result
    _one_m_cache_at = now
    return result


def _is_one_m_model(model: str, cwd: str) -> bool:
    """True if Claude's config shows ``<model>[1m]`` was used in ``cwd``."""
    if not model or not cwd:
        return False
    return f'{model}[1m]' in _project_model_ids().get(cwd, set())


def _resolve_window(model: str, cwd: str, used: int) -> int:
    """Best-effort context window for a turn: 1M when detected, else the base.

    1M is detected either from Claude's persisted per-project model id (the
    ``[1m]`` suffix) or, as a safety net, from usage that has already exceeded
    the base window -- which a 200k session could never reach (Claude
    auto-compacts well before then).
    """
    if _is_one_m_model(model, cwd):
        return _ONE_M_CONTEXT_WINDOW
    base = context_window_for_model(model)
    if used > base:
        return _ONE_M_CONTEXT_WINDOW
    return base


def _as_str(value: object) -> str:
    """Return ``value`` if it is a string, else ``''`` (defensive coercion)."""
    return value if isinstance(value, str) else ''


def _usage_from_tail(tail: bytes) -> Optional[ContextUsage]:
    """Find the latest main-chain assistant turn's usage in a JSONL tail.

    Defensive throughout: a JSONL line can be valid JSON but not an object
    (a truncated leading line, or a scalar), and any field can be the wrong
    type.  Such entries are skipped rather than raising -- this runs on the
    monitor's render thread, so it must never throw.
    """
    for raw in reversed(tail.split(b'\n')):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get('type') != 'assistant':
            continue
        # Sub-agent (Task tool) turns carry ``isSidechain=True``; counting one
        # would report the sub-agent's context, not the main conversation.
        if entry.get('isSidechain'):
            continue
        message = entry.get('message')
        if not isinstance(message, dict):
            continue
        usage = message.get('usage')
        if not isinstance(usage, dict) or not usage:
            continue
        used = 0
        for field in ('input_tokens', 'cache_creation_input_tokens',
                      'cache_read_input_tokens'):
            v = usage.get(field, 0)
            if isinstance(v, (int, float)):
                used += int(v)
        model = _as_str(message.get('model'))
        cwd = _as_str(entry.get('cwd'))
        return ContextUsage(used_tokens=used,
                            window=_resolve_window(model, cwd, used),
                            model=model)
    return None


def context_usage_for_transcript(transcript_path: str) -> Optional[ContextUsage]:
    """Context usage from the newest assistant turn in a transcript, or None.

    Returns None on any IO/parse error, an empty/short transcript, or an
    assistant turn that carries no usage block yet.  Results (including None)
    are cached on the transcript's (mtime, size), so steady-state polling of an
    unchanged transcript is a single ``os.stat`` with no re-parse.
    """
    if not transcript_path:
        return None
    try:
        st = os.stat(transcript_path)
    except OSError:
        return None
    cached = _USAGE_CACHE.get(transcript_path)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    try:
        with open(transcript_path, 'rb') as f:
            f.seek(max(0, st.st_size - _TRANSCRIPT_TAIL_BYTES))
            tail = f.read()
    except OSError:
        _USAGE_CACHE[transcript_path] = (st.st_mtime_ns, st.st_size, None)
        return None
    # _usage_from_tail is defensively written, but this runs on the monitor's
    # render thread reading an external file -- a final net guarantees a bad
    # transcript can never throw into the table refresh.
    try:
        result = _usage_from_tail(tail)
    except Exception:
        result = None
    _USAGE_CACHE[transcript_path] = (st.st_mtime_ns, st.st_size, result)
    return result
