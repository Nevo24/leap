"""Compute a CLI session's context-window usage from its transcript.

Each supported CLI writes per-turn token usage into its own transcript format,
so each gets its own parser.  This module reads the latest turn and expresses
it as a percentage of the model's context window, letting the monitor show how
close a session is to auto-compaction ("compression").

Per-CLI support:
  - Claude  : ~/.claude/projects/<slug>/<id>.jsonl, ``message.usage``
              (input + cache_creation + cache_read).  Window 200k, or 1M when
              the [1m] beta is detected from ~/.claude.json (the transcript's
              model id drops the suffix).
  - Codex   : ~/.codex/sessions/.../rollout-*.jsonl, ``event_msg`` /
              ``token_count`` -> ``info.last_token_usage.input_tokens``; the
              context window is in the data (``info.model_context_window``).
  - Gemini  : ~/.gemini/tmp/<slug>/chats/session-*.jsonl, ``gemini`` entries'
              ``tokens.input``; window mapped by model (Gemini 2.5/3 are ~1M).
  - Copilot : its transcript exposes no live usage, but its **status line**
              receives the live context numbers on stdin every render.  Leap
              installs a status-line script (``leap-copilot-statusline.py``)
              that writes ``<storage>/sockets/<tag>.context`` (JSON:
              ``{used_tokens, window, model}``); ``statusline_context_usage``
              reads that file.  Claude also writes the same file via its own
              status-line script and prefers it over the transcript (it is the
              only place Claude exposes the resolved 1M-vs-200K window).
  - Cursor  : NOT supported - the CLI exposes no token usage at all and chats
              live in an undocumented content-addressed SQLite blob store
              (no transcript_path recorded).  Renders N/A.

Each CLI measures the same thing: the size of the prompt sent on the most
recent turn (the conversation loaded into the window), NOT the model's reply.
Note the semantics differ per CLI: Claude reports the uncached new tokens
separately from the cached prefix (so we sum them), whereas Codex/Gemini report
``input`` as the full prompt with the cached count as a subset (so we don't).

Mostly side-effect-free, with two small process-local caches (the transcript
parse keyed on its (mtime, size), and Claude's ~/.claude.json keyed on a short
TTL); no Qt, no provider imports.  Defensively parsed throughout so a bad
transcript can never throw into the monitor's render thread.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

# Tail size mirrors claude.py's transcript reader -- the latest turn's usage is
# always within the last few KiB for every CLI's format.
_TRANSCRIPT_TAIL_BYTES = 32768

# --- Claude context windows ---
# Every Claude model shipped to date exposes a 200k-token context window.  Map
# any future exceptions here by model id; everything else uses the default.
_DEFAULT_CONTEXT_WINDOW = 200_000
_ONE_M_CONTEXT_WINDOW = 1_000_000
_MODEL_CONTEXT_WINDOW: dict[str, int] = {}

# --- Codex / Gemini context windows ---
# Codex carries the real window in the rollout (info.model_context_window); this
# is only a fallback if a build ever omits it.
_CODEX_DEFAULT_WINDOW = 256_000
# Gemini 2.5 / 3 expose a 1M-token window.  Map exceptions by model id.
_GEMINI_DEFAULT_WINDOW = 1_048_576
_GEMINI_MODEL_WINDOW: dict[str, int] = {}

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
# invalidates the entry.  Keyed by path (one CLI per path) so it can't grow
# unbounded as a long-lived transcript keeps appending.
_USAGE_CACHE: dict[str, "tuple[int, int, Optional[ContextUsage]]"] = {}


@dataclass(frozen=True)
class ContextUsage:
    """A single context-window measurement for one CLI session."""

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


def _as_str(value: object) -> str:
    """Return ``value`` if it is a string, else ``''`` (defensive coercion)."""
    return value if isinstance(value, str) else ''


def _as_int(value: object) -> int:
    """Return ``value`` as an int if it is numeric, else ``0`` (defensive)."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


# ===========================================================================
# Claude context-window resolution (incl. 1M-beta detection)
# ===========================================================================

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
    """True if Claude is running ``<model>`` on the 1M context window.

    The 1M choice is account-wide (you run a given model on ``[1m]`` across
    projects), but Claude only records it in the volatile per-project
    ``lastModelUsage`` (which it rewrites constantly and sometimes blanks).
    So:

    - If THIS project's record mentions THIS model, trust it precisely -
      ``<model>[1m]`` present -> 1M; the plain ``<model>`` without it -> a
      genuine 200k project.
    - Otherwise (no record yet, Claude blanked it, or the record names only
      *other* models - e.g. a prior sonnet session in the same cwd), fall
      back to the account-wide signal: is ``<model>[1m]`` recorded in ANY
      project?  This survives the per-project record being wiped or being
      about a different model.

    Version-specific (``opus-4-8`` only matches ``opus-4-8[1m]``, not 4-7).
    """
    if not model or not cwd:
        return False
    target = f'{model}[1m]'
    by_cwd = _project_model_ids()
    cwd_ids = by_cwd.get(cwd)
    if cwd_ids and (target in cwd_ids or model in cwd_ids):
        # This project explicitly ran THIS model (1M or plain) -> trust it.
        return target in cwd_ids
    # This cwd has no record for THIS model (absent / blanked / records only
    # other models) -> fall back to the account-wide signal.
    return any(target in ids for ids in by_cwd.values())


def _resolve_claude_window(model: str, cwd: str, used: int) -> int:
    """Best-effort Claude context window: 1M when detected, else the base.

    1M is detected either from Claude's persisted model usage (the ``[1m]``
    suffix -- the session's own project if it has a record, else account-wide;
    see :func:`_is_one_m_model`) or, as a safety net, from usage that has
    already exceeded the base window -- which a 200k session could never reach
    (Claude auto-compacts well before then).
    """
    if _is_one_m_model(model, cwd):
        return _ONE_M_CONTEXT_WINDOW
    base = context_window_for_model(model)
    if used > base:
        return _ONE_M_CONTEXT_WINDOW
    return base


# ===========================================================================
# Per-CLI tail parsers: bytes (JSONL tail) -> Optional[ContextUsage]
# ===========================================================================

def _claude_usage_from_tail(tail: bytes) -> Optional[ContextUsage]:
    """Latest main-chain assistant turn's usage in a Claude transcript tail."""
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
        # Claude reports new (uncached) input separately from the cached prefix,
        # so the full prompt = input + cache_creation + cache_read.
        used = (_as_int(usage.get('input_tokens'))
                + _as_int(usage.get('cache_creation_input_tokens'))
                + _as_int(usage.get('cache_read_input_tokens')))
        model = _as_str(message.get('model'))
        cwd = _as_str(entry.get('cwd'))
        return ContextUsage(used_tokens=used,
                            window=_resolve_claude_window(model, cwd, used),
                            model=model)
    return None


def _codex_usage_from_tail(tail: bytes) -> Optional[ContextUsage]:
    """Latest token_count event's usage in a Codex rollout tail.

    Codex emits an ``event_msg`` of ``payload.type == 'token_count'`` at the end
    of every turn whose ``info`` carries ``last_token_usage`` (that request's
    full prompt) and ``model_context_window`` (the denominator).  The model id
    lives on ``turn_context`` entries, so grab the most recent of each.
    """
    info: Optional[dict] = None
    model = ''
    for raw in reversed(tail.split(b'\n')):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        payload = entry.get('payload')
        if not isinstance(payload, dict):
            continue
        if (info is None and entry.get('type') == 'event_msg'
                and payload.get('type') == 'token_count'):
            cand = payload.get('info')
            # Skip an empty token_count (no last_token_usage) - an early one can
            # appear before any tokens, and using it would blank the cell while
            # the cost path (which skips it) still computes.  Keep looking back
            # for the latest event that actually carries usage.
            if isinstance(cand, dict) and isinstance(
                    cand.get('last_token_usage'), dict):
                info = cand
        elif not model and entry.get('type') == 'turn_context':
            model = _as_str(payload.get('model'))
        if info is not None and model:
            break
    if not isinstance(info, dict):
        return None
    last = info.get('last_token_usage')
    if not isinstance(last, dict):
        return None
    # OpenAI's input_tokens is the FULL prompt; cached_input_tokens is a subset.
    used = _as_int(last.get('input_tokens'))
    window = _as_int(info.get('model_context_window')) or _CODEX_DEFAULT_WINDOW
    return ContextUsage(used_tokens=used, window=window, model=model or 'codex')


def _gemini_usage_from_tail(tail: bytes) -> Optional[ContextUsage]:
    """Latest model turn's usage in a Gemini chat-session tail.

    Each ``type == 'gemini'`` entry carries ``tokens.{input,output,cached,...}``
    where ``input`` is the full prompt (cached is a subset) and ``model`` is the
    model id.
    """
    for raw in reversed(tail.split(b'\n')):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict) or entry.get('type') != 'gemini':
            continue
        tokens = entry.get('tokens')
        if not isinstance(tokens, dict):
            continue
        used = _as_int(tokens.get('input'))
        if used <= 0:
            continue  # no usable token info on this turn -- keep looking back
        model = _as_str(entry.get('model'))
        window = _GEMINI_MODEL_WINDOW.get(model, _GEMINI_DEFAULT_WINDOW)
        return ContextUsage(used_tokens=used, window=window, model=model)
    return None


# ===========================================================================
# Generic read + cache, and the public per-CLI entry points
# ===========================================================================

def _context_usage(transcript_path: str,
                   parser: Callable[[bytes], Optional[ContextUsage]],
                   ) -> Optional[ContextUsage]:
    """Read a transcript's tail and parse it with ``parser``.

    Returns None on any IO/parse error, an empty/short transcript, or a turn
    that carries no usage yet.  Results (including None) are cached on the
    transcript's (mtime, size), so steady-state polling of an unchanged
    transcript is a single ``os.stat`` with no re-parse.  ``parser`` is
    defensively written, but it runs on the monitor's render thread reading an
    external file, so a final net guarantees a bad transcript can never throw.
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
    try:
        result = parser(tail)
    except Exception:
        result = None
    _USAGE_CACHE[transcript_path] = (st.st_mtime_ns, st.st_size, result)
    return result


def claude_context_usage(transcript_path: str) -> Optional[ContextUsage]:
    """Context usage for a Claude transcript, or None."""
    return _context_usage(transcript_path, _claude_usage_from_tail)


def codex_context_usage(transcript_path: str) -> Optional[ContextUsage]:
    """Context usage for a Codex rollout transcript, or None."""
    return _context_usage(transcript_path, _codex_usage_from_tail)


def gemini_context_usage(transcript_path: str) -> Optional[ContextUsage]:
    """Context usage for a Gemini chat-session transcript, or None."""
    return _context_usage(transcript_path, _gemini_usage_from_tail)


def statusline_context_usage(state_path: str) -> Optional[ContextUsage]:
    """Context usage from a status-line state file, or None.

    Both Copilot (always) and Claude (preferred over its transcript) get their
    context usage from a small JSON file (``{used_tokens, window, model}``) that
    Leap's status-line script writes, rather than from a CLI transcript.  Cached
    on the file's (mtime, size) like the transcript path.
    """
    if not state_path:
        return None
    try:
        st = os.stat(state_path)
    except OSError:
        return None
    cached = _USAGE_CACHE.get(state_path)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    result: Optional[ContextUsage] = None
    try:
        with open(state_path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            window = _as_int(data.get('window'))
            if window > 0:
                result = ContextUsage(used_tokens=_as_int(data.get('used_tokens')),
                                      window=window,
                                      model=_as_str(data.get('model')))
    except (OSError, json.JSONDecodeError, ValueError):
        result = None
    _USAGE_CACHE[state_path] = (st.st_mtime_ns, st.st_size, result)
    return result


def claude_statusline_context_usage(state_path: str) -> Optional[ContextUsage]:
    """Claude's status-line state, healed when the recorded window is impossible.

    Claude's status-line payload can misreport ``context_window_size`` as the
    base 200K for a session that is actually running on the 1M window (seen on
    long-lived sessions whose conversation kept its original model after a
    mid-session ``/model`` change).  Live context can never exceed the real
    window - the API rejects over-window prompts - so ``used > window`` proves
    the recorded window wrong, and 1M is the only Claude window above the
    base.  Same safety net as :func:`_resolve_claude_window` applies on the
    transcript path.  Claude-only: Copilot reads the raw file via
    :func:`statusline_context_usage`.
    """
    usage = statusline_context_usage(state_path)
    if (usage is not None and usage.used_tokens > usage.window
            and usage.window < _ONE_M_CONTEXT_WINDOW):
        return ContextUsage(used_tokens=usage.used_tokens,
                            window=_ONE_M_CONTEXT_WINDOW,
                            model=usage.model)
    return usage
