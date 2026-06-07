"""Estimate a session's cumulative token spend and USD cost.

Companion to :mod:`leap.utils.context_usage`.  Where ``context_usage`` answers
"how full is the window *right now*" (a snapshot of the latest turn), this
module answers "how many tokens / dollars has this session burned in total".
Tokens are priced via :mod:`leap.utils.pricing`; the dollar figure is an
estimate (subscription users aren't billed per token).

Per CLI, because each records usage differently:

* **Claude** records neither a dollar amount nor a running total, so we sum
  each main-chain assistant turn's billable tokens.  To avoid re-parsing a
  multi-MB transcript on every poll, the walk is *incremental*: a per-path
  accumulator remembers the byte offset and running totals (and dedups the
  split-line entries that repeat the same usage).  A shrunk/replaced
  transcript resets it.
* **Codex** emits a cumulative ``total_token_usage`` (and ``last_token_usage``)
  on every ``token_count`` event, so no accumulation is needed - read the
  latest event and the model from ``turn_context``.
* **Gemini** records per-turn ``tokens`` but no running total, so we walk the
  (small) chat file and sum each turn.

Defensive throughout - it runs off an external file, so a bad transcript yields
``None``, never an exception.  Copilot exposes only a live window snapshot (no
cumulative / output split) and Cursor exposes nothing, so neither supports
cost.
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from leap.utils.pricing import ModelPricing, cost_usd, price_for


@dataclass(frozen=True)
class CostInfo:
    """Token + USD totals for one session's transcript.

    ``*_cost_usd`` is ``None`` when the relevant turn(s) couldn't be priced
    (unknown model id), so the tooltip shows token counts without dollars
    rather than a fake ``$0.00``.
    """

    last_turn_tokens: int
    last_turn_cost_usd: Optional[float]
    session_tokens: int
    session_cost_usd: Optional[float]


def _as_int(value: object) -> int:
    """Return ``value`` as an int if numeric, else ``0`` (defensive)."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


@dataclass
class _Accum:
    """Mutable per-transcript running state for incremental accumulation."""

    ino: int             # st_ino of the file this state was built from
    offset: int          # bytes fully consumed (up to and incl. the last newline)
    input_t: int
    output_t: int
    cache_write_t: int
    cache_read_t: int
    session_cost: float
    any_priced: bool     # at least one turn had a known model price
    last_turn_tokens: int
    last_turn_cost: Optional[float]
    seen: set            # (message.id, requestId) of already-counted turns

    def total_tokens(self) -> int:
        return self.input_t + self.output_t + self.cache_write_t + self.cache_read_t


# transcript_path -> accumulator.  One session per path; bounded like
# context_usage's cache (a long-lived transcript keeps the same key).
_ACCUM: dict[str, _Accum] = {}


def _new_accum(ino: int) -> _Accum:
    return _Accum(ino=ino, offset=0, input_t=0, output_t=0, cache_write_t=0,
                  cache_read_t=0, session_cost=0.0, any_priced=False,
                  last_turn_tokens=0, last_turn_cost=None, seen=set())


def _consume_turn(acc: _Accum, entry: dict) -> None:
    """Fold one parsed entry into ``acc`` if it is a main-chain assistant turn
    carrying a non-empty ``usage`` block."""
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return
    message = entry.get("message")
    if not isinstance(message, dict):
        return
    usage = message.get("usage")
    if not isinstance(usage, dict) or not usage:
        return
    # Claude splits one assistant message (text + tool_use blocks) across
    # multiple JSONL lines that repeat the SAME message.id / requestId and the
    # SAME usage block.  Count each message's usage once or the total inflates
    # ~2-3x.  Entries with no id at all can't be deduped, so we count them.
    # The key is only recorded once a non-zero turn is actually counted, so a
    # (hypothetical) placeholder/zero first split-line can't suppress the real
    # usage carried by a later line of the same message.
    mid = message.get("id")
    rid = entry.get("requestId")
    key = (mid, rid) if (mid or rid) else None
    if key is not None and key in acc.seen:
        return
    input_t = _as_int(usage.get("input_tokens"))
    cache_write_t = _as_int(usage.get("cache_creation_input_tokens"))
    cache_read_t = _as_int(usage.get("cache_read_input_tokens"))
    output_t = _as_int(usage.get("output_tokens"))
    turn_tokens = input_t + cache_write_t + cache_read_t + output_t
    if turn_tokens <= 0:
        return
    if key is not None:
        acc.seen.add(key)
    acc.input_t += input_t
    acc.output_t += output_t
    acc.cache_write_t += cache_write_t
    acc.cache_read_t += cache_read_t
    model = message.get("model")
    pricing: Optional[ModelPricing] = price_for(model if isinstance(model, str) else "")
    if pricing is not None:
        cost = cost_usd(pricing, new_input=input_t, cache_read=cache_read_t,
                        cache_write=cache_write_t, output=output_t)
        acc.session_cost += cost
        acc.any_priced = True
        acc.last_turn_cost = cost
    else:
        acc.last_turn_cost = None
    acc.last_turn_tokens = turn_tokens


def _ingest(acc: _Accum, data: bytes) -> int:
    """Parse the newline-terminated JSONL records in ``data``, folding each
    into ``acc``.  Returns the number of bytes consumed (up to the last
    newline); any trailing partial line is left for the next poll."""
    last_nl = data.rfind(b"\n")
    if last_nl < 0:
        return 0
    chunk = data[: last_nl + 1]
    for raw in chunk.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(entry, dict):
            _consume_turn(acc, entry)
    return len(chunk)


def claude_session_cost(transcript_path: str) -> Optional[CostInfo]:
    """Cumulative token + USD estimate for a Claude transcript, or ``None``.

    ``None`` when the path is empty/unreadable or no assistant turn with usage
    has appeared yet (matching ``context_usage``'s "no data" case, so the
    tooltip simply omits the cost lines).  Incremental: re-parses only the
    bytes appended since the previous call.
    """
    if not transcript_path:
        return None
    try:
        st = os.stat(transcript_path)
    except OSError:
        return None
    acc = _ACCUM.get(transcript_path)
    # Reset on first sight, inode change (file replaced), or truncation
    # (our offset is past EOF -> rotated / compacted to something smaller).
    if acc is None or acc.ino != st.st_ino or acc.offset > st.st_size:
        acc = _new_accum(st.st_ino)
        _ACCUM[transcript_path] = acc
    if st.st_size > acc.offset:
        try:
            with open(transcript_path, "rb") as f:
                f.seek(acc.offset)
                data = f.read()
        except OSError:
            data = b""
        acc.offset += _ingest(acc, data)
    if acc.total_tokens() <= 0:
        return None
    return CostInfo(
        last_turn_tokens=acc.last_turn_tokens,
        last_turn_cost_usd=acc.last_turn_cost,
        session_tokens=acc.total_tokens(),
        session_cost_usd=acc.session_cost if acc.any_priced else None,
    )


# ---------------------------------------------------------------------------
# Codex - cumulative usage is given directly (no walk/accumulation needed)
# ---------------------------------------------------------------------------

def _codex_turn_cost(pricing: Optional[ModelPricing], usage: dict,
                     is_session: bool) -> Optional[float]:
    """USD for a Codex usage block (``last_token_usage`` / ``total_token_usage``).

    OpenAI bills cached input at the cache-read rate and does not charge for
    cache writes; ``output_tokens`` already includes reasoning.  For the
    cumulative total we price at base rates (``prompt_tokens=0``): a cumulative
    sum isn't a single request, so a per-request long-context tier can't apply
    - and OpenAI's >272k tier is unreachable anyway (its context windows sit
    below the threshold).  The last turn is a real request, so it tiers on its
    own prompt size.
    """
    if pricing is None:
        return None
    inp = _as_int(usage.get("input_tokens"))
    cached = _as_int(usage.get("cached_input_tokens"))
    out = _as_int(usage.get("output_tokens"))
    return cost_usd(pricing, new_input=max(0, inp - cached), cache_read=cached,
                    output=out, prompt_tokens=0 if is_session else inp)


def _codex_total_tokens(usage: dict) -> int:
    return (_as_int(usage.get("total_tokens"))
            or _as_int(usage.get("input_tokens")) + _as_int(usage.get("output_tokens")))


def codex_session_cost(rollout_path: str) -> Optional[CostInfo]:
    """Token + USD estimate for a Codex rollout, or ``None``.

    Codex emits a ``token_count`` event each turn whose ``info`` carries the
    cumulative ``total_token_usage`` and the turn's ``last_token_usage``; the
    model id lives on ``turn_context`` entries.  A single streaming pass keeps
    the latest of each.
    """
    if not rollout_path:
        return None
    info: Optional[dict] = None
    model = ""
    try:
        with open(rollout_path, "rb") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                payload = entry.get("payload")
                if not isinstance(payload, dict):
                    continue
                if (entry.get("type") == "event_msg"
                        and payload.get("type") == "token_count"):
                    cand = payload.get("info")
                    if isinstance(cand, dict):
                        # Keep the latest event that actually carries usage - an
                        # early token_count can be empty (no tokens yet), and
                        # using it would zero out the cost.
                        tt = cand.get("total_token_usage")
                        lt = cand.get("last_token_usage")
                        if (isinstance(tt, dict) and tt) or (isinstance(lt, dict) and lt):
                            info = cand
                elif entry.get("type") == "turn_context":
                    m = payload.get("model")
                    if isinstance(m, str) and m:
                        model = m
    except OSError:
        return None
    if not isinstance(info, dict):
        return None
    last = info.get("last_token_usage")
    total = info.get("total_token_usage")
    last = last if isinstance(last, dict) else {}
    total = total if isinstance(total, dict) else {}
    # The cumulative is always >= the last turn.  If a token_count carries only
    # last_token_usage (no/empty total - possible on the very first turn), fall
    # back to last so the session line never reads smaller than the last turn.
    if _codex_total_tokens(total) <= 0:
        total = last
    last_tokens = _codex_total_tokens(last)
    session_tokens = _codex_total_tokens(total)
    if last_tokens <= 0 and session_tokens <= 0:
        return None
    pricing = price_for(model)
    return CostInfo(
        last_turn_tokens=last_tokens,
        last_turn_cost_usd=_codex_turn_cost(pricing, last, is_session=False),
        session_tokens=session_tokens,
        session_cost_usd=_codex_turn_cost(pricing, total, is_session=True),
    )


# ---------------------------------------------------------------------------
# Gemini - per-turn tokens, no running total -> walk the (small) chat file
# ---------------------------------------------------------------------------

def gemini_session_cost(transcript_path: str) -> Optional[CostInfo]:
    """Token + USD estimate for a Gemini chat session, or ``None``.

    Each ``type == 'gemini'`` entry carries ``tokens.{input,output,cached,
    thoughts}`` and the model id.  Verified against real turns,
    ``total == input + output + thoughts`` and ``cached`` is a *subset* of
    ``input`` - so input bills at the input rate (the cached subset cheaper),
    output at the output rate, and thinking (``thoughts``, which is additive)
    at the reasoning rate.  ``tool`` is not added separately: it's already part
    of ``input`` (the full prompt), so folding it in would double-count.  Summed
    across turns - Gemini sessions are small, so a full walk per change is fine
    (the cached wrapper only recomputes on a file change).
    """
    if not transcript_path:
        return None
    session_cost = 0.0
    session_tokens = 0
    any_priced = False
    last_tokens = 0
    last_cost: Optional[float] = None
    try:
        with open(transcript_path, "rb") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "gemini":
                    continue
                tokens = entry.get("tokens")
                if not isinstance(tokens, dict):
                    continue
                inp = _as_int(tokens.get("input"))
                out = _as_int(tokens.get("output"))
                cached = _as_int(tokens.get("cached"))
                thoughts = _as_int(tokens.get("thoughts"))
                turn_tokens = (_as_int(tokens.get("total"))
                               or inp + out + thoughts)
                if turn_tokens <= 0:
                    continue
                model = entry.get("model")
                pricing = price_for(model if isinstance(model, str) else "")
                turn_cost: Optional[float] = None
                if pricing is not None:
                    turn_cost = cost_usd(
                        pricing, new_input=max(0, inp - cached),
                        cache_read=cached, output=out, reasoning=thoughts,
                        prompt_tokens=inp)
                    session_cost += turn_cost
                    any_priced = True
                session_tokens += turn_tokens
                last_tokens = turn_tokens
                last_cost = turn_cost
    except OSError:
        return None
    if session_tokens <= 0:
        return None
    return CostInfo(
        last_turn_tokens=last_tokens,
        last_turn_cost_usd=last_cost,
        session_tokens=session_tokens,
        session_cost_usd=session_cost if any_priced else None,
    )


# ---------------------------------------------------------------------------
# Non-blocking wrappers for the monitor's render thread
# ---------------------------------------------------------------------------
# The session-cost computers above read (and may fully parse) a transcript
# synchronously.  The monitor builds the Context cell on the Qt GUI thread, so a
# brand-new session's first parse of a large file could briefly hitch the UI.
# The ``*_cached`` wrappers keep the GUI thread non-blocking: they return the
# last computed result immediately and recompute in a small background pool
# whenever the file's (inode, size) changes.  The background task touches no Qt
# objects (file IO + the plain dicts below only), so it is safe alongside Qt.
# Cost lines therefore appear ~one refresh cycle after a session first shows up
# - an acceptable trade for never stalling the table build.

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="leap-cost")
_LOCK = threading.Lock()
_RESULT: dict[str, Optional[CostInfo]] = {}   # path -> last computed CostInfo
_RESULT_SIG: dict[str, tuple] = {}            # path -> (ino, size) it was computed for
_INFLIGHT: set = set()                        # paths with a compute scheduled


def _store_result(path: str, sig: tuple, fut: Future) -> None:
    """done-callback: stash the background compute's result under the lock."""
    try:
        info = fut.result()
    except Exception:
        info = None
    with _LOCK:
        _RESULT[path] = info
        _RESULT_SIG[path] = sig
        _INFLIGHT.discard(path)


def _session_cost_cached(
    path: str,
    compute: Callable[[str], Optional[CostInfo]],
) -> Optional[CostInfo]:
    """Non-blocking cache around a per-CLI ``compute(path)`` cost function.

    Returns the most recently computed :class:`CostInfo` for ``path`` (``None``
    until the first background compute lands) and schedules a background
    recompute via ``compute`` whenever the file's (inode, size) has changed.
    Never reads/parses on the calling thread, so it can't stall the table build.
    The scheduling decision keys on the stored signature (not on the result
    being ``None``), so an empty/no-usage file settles to ``None`` without
    re-scheduling every poll.  ``_RESULT`` etc. are keyed by path, which is
    unique per session/CLI, so all CLIs share one set of maps safely.
    """
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    sig = (st.st_ino, st.st_size)
    with _LOCK:
        cached = _RESULT.get(path)
        schedule = (_RESULT_SIG.get(path) != sig and path not in _INFLIGHT)
        if schedule:
            _INFLIGHT.add(path)
    if schedule:
        try:
            fut = _EXECUTOR.submit(compute, path)
        except RuntimeError:
            # Executor shut down (e.g. interpreter exit): don't leak the
            # in-flight marker and never raise into the GUI render thread.
            with _LOCK:
                _INFLIGHT.discard(path)
            return cached
        fut.add_done_callback(
            lambda f, p=path, s=sig: _store_result(p, s, f))
    return cached


def claude_session_cost_cached(transcript_path: str) -> Optional[CostInfo]:
    """Non-blocking :func:`claude_session_cost` for the GUI thread."""
    return _session_cost_cached(transcript_path, claude_session_cost)


def codex_session_cost_cached(rollout_path: str) -> Optional[CostInfo]:
    """Non-blocking :func:`codex_session_cost` for the GUI thread."""
    return _session_cost_cached(rollout_path, codex_session_cost)


def gemini_session_cost_cached(transcript_path: str) -> Optional[CostInfo]:
    """Non-blocking :func:`gemini_session_cost` for the GUI thread."""
    return _session_cost_cached(transcript_path, gemini_session_cost)
