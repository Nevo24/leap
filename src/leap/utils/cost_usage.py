"""Estimate a Claude session's cumulative token spend and USD cost.

Companion to :mod:`leap.utils.context_usage`.  Where ``context_usage`` answers
"how full is the window *right now*" (a snapshot of the latest turn, read from
the transcript tail), this module answers "how many tokens / dollars has this
session burned in total" - which needs *every* assistant turn, not just the
last one.

Claude records neither a dollar amount nor a running token total in the
transcript, so we sum each main-chain assistant turn's billable tokens and
price them via :mod:`leap.utils.pricing`.  To avoid re-parsing a multi-MB
transcript on every monitor poll, the walk is *incremental*: a per-path
accumulator remembers the byte offset and running totals, and each poll parses
only the bytes appended since last time.  A transcript that shrinks or is
replaced (rotation, or a freshly resumed session reusing the path) resets the
accumulator and re-walks from the start.

Claude only for now (Codex / Gemini are a later step).  Defensive throughout -
it runs on the monitor's render thread reading an external file, so a bad
transcript yields ``None``, never an exception.
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from leap.utils.pricing import ModelPricing, price_for, turn_cost_usd


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
        cost = turn_cost_usd(pricing, input_t, output_t, cache_write_t, cache_read_t)
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
# Non-blocking wrapper for the monitor's render thread
# ---------------------------------------------------------------------------
# ``claude_session_cost`` above reads (and on first call fully parses) the
# transcript synchronously.  The monitor builds the Context cell on the Qt GUI
# thread, so a brand-new session's first parse of a large transcript could
# briefly hitch the UI.  ``claude_session_cost_cached`` keeps the GUI thread
# non-blocking: it returns the last computed result immediately and recomputes
# in a small background pool whenever the file's (inode, size) changes.  The
# background task touches no Qt objects (file IO + the plain dicts below only),
# so it is safe alongside Qt.  Cost lines therefore appear ~one refresh cycle
# after a session first shows up - an acceptable trade for never stalling the
# table build.  Steady-state recomputes are cheap incremental delta reads
# (the same ``_ACCUM`` accumulator), so the background pool stays idle.

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


def claude_session_cost_cached(transcript_path: str) -> Optional[CostInfo]:
    """Non-blocking variant of :func:`claude_session_cost` for the GUI thread.

    Returns the most recently computed :class:`CostInfo` for the transcript
    (``None`` until the first background compute lands) and schedules a
    background recompute whenever the file's (inode, size) has changed since
    the cached result.  Never reads or parses the transcript on the calling
    thread, so it can't stall the monitor's table build.  The scheduling
    decision keys on the stored signature (not on the result being ``None``),
    so an empty/no-usage transcript settles to ``None`` without re-scheduling
    every poll.
    """
    if not transcript_path:
        return None
    try:
        st = os.stat(transcript_path)
    except OSError:
        return None
    sig = (st.st_ino, st.st_size)
    with _LOCK:
        cached = _RESULT.get(transcript_path)
        schedule = (_RESULT_SIG.get(transcript_path) != sig
                    and transcript_path not in _INFLIGHT)
        if schedule:
            _INFLIGHT.add(transcript_path)
    if schedule:
        try:
            fut = _EXECUTOR.submit(claude_session_cost, transcript_path)
        except RuntimeError:
            # Executor shut down (e.g. interpreter exit): don't leak the
            # in-flight marker and never raise into the GUI render thread.
            with _LOCK:
                _INFLIGHT.discard(transcript_path)
            return cached
        fut.add_done_callback(
            lambda f, p=transcript_path, s=sig: _store_result(p, s, f))
    return cached
