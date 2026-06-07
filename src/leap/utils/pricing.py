"""Model USD pricing for the monitor's Context-cell cost estimate.

Prices are NOT hardcoded.  They come from LiteLLM's community-maintained
``model_prices_and_context_window.json`` (the same source ``ccusage`` uses),
which keys every Claude model exactly (``claude-opus-4-8`` etc.) with per-token
costs for each token class.

Two layers, so it's correct offline *and* self-updating:
  1. **Vendored snapshot** - ``assets/model_prices.json`` (a Claude-only trim
     of the LiteLLM file) ships in the repo, so prices are right on first run
     and with no network.
  2. **Background refresh** - :func:`ensure_fresh_prices` (called lazily the
     first time a price is looked up) fetches the latest LiteLLM file in a
     daemon thread when the cache is stale, trims it, and writes
     ``.storage/model_prices.json``.  That cache overlays the vendored snapshot
     and is picked up automatically (the loader keys on the file's mtime).  A
     failed/blocked fetch is silently ignored - the vendored snapshot stands.

The dollar figure is still an *estimate* labeled "(est.)" in the UI:
subscription users (Pro / Max / Team) pay a flat fee, not per token.

Defensive throughout - any IO/parse failure falls back to the vendored data or
to "no price" (the tooltip then shows token counts without dollars).  Costs are
per-token (LiteLLM's native unit), so :func:`turn_cost_usd` is a plain dot
product with no scaling.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from leap.utils.constants import MODEL_PRICES_CACHE, STORAGE_DIR

# Upstream source of truth (raw GitHub).  ``main`` tracks the latest prices.
_LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
# Refresh the on-disk cache once it is older than this (or missing) -> refetch.
_REFRESH_MAX_AGE_SECONDS = 24 * 60 * 60  # 1 day
_FETCH_TIMEOUT_SECONDS = 20
# Cap the fetched body so a hijacked/pathological response can't OOM the
# monitor.  The real file is ~1.5 MB; a truncated read just fails JSON parse
# and we keep the vendored snapshot.
_MAX_FETCH_BYTES = 25 * 1024 * 1024

# Anthropic's long-context surcharge applies when a single request's input
# exceeds this many tokens.  LiteLLM encodes the surcharged rates in the
# ``*_above_200k_tokens`` fields (present for Sonnet; absent for Opus, which has
# no such tier), so the premium is data-driven, not assumed.
_LONG_CONTEXT_THRESHOLD = 200_000

# The four LiteLLM per-token cost fields we consume, base (suffix "") and the
# >200k surcharge variant ("_above_200k_tokens").
_COST_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_creation_input_token_cost",
    "cache_read_input_token_cost",
)


@dataclass(frozen=True)
class PriceRates:
    """USD per single token for each billable token class."""

    input: float        # new (uncached) input tokens
    output: float       # output tokens (includes reasoning/thinking output)
    cache_write: float  # cache_creation_input_tokens
    cache_read: float   # cache_read_input_tokens


@dataclass(frozen=True)
class ModelPricing:
    """Base rates plus optional >200K long-context premium rates."""

    base: PriceRates
    premium: Optional[PriceRates] = None  # used for a turn over the threshold


# ---------------------------------------------------------------------------
# Trim (shared by the vendored-snapshot generator and the runtime refresh)
# ---------------------------------------------------------------------------

def trim_claude(raw: Dict[str, dict]) -> Dict[str, dict]:
    """Reduce a full LiteLLM price map to the Claude-only subset we need.

    Keeps bare ``claude-*`` model ids (the form Claude Code writes into its
    transcripts) and, per model, only the per-token cost fields plus their
    ``*_above_200k_tokens`` variants.  Used both to generate the vendored
    ``assets/model_prices.json`` and to shrink the runtime-fetched file before
    caching it, so the two always share a shape.
    """
    out: Dict[str, dict] = {}
    for model, entry in raw.items():
        if not (isinstance(model, str) and model.startswith("claude-")):
            continue
        if not isinstance(entry, dict):
            continue
        kept = {}
        for base in _COST_FIELDS:
            for field in (base, f"{base}_above_200k_tokens"):
                val = entry.get(field)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    kept[field] = val
        if kept:
            out[model] = kept
    return out


# ---------------------------------------------------------------------------
# Vendored snapshot + cache loading (mtime-keyed, thread-safe)
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
_prices: Optional[Dict[str, dict]] = None
_prices_sig: Optional[int] = None       # cache-file mtime_ns the merge was built for
_last_check_monotonic: Optional[float] = None  # last time staleness was evaluated
_refresh_in_flight = False
# Re-evaluate cache staleness at most this often.  A price lookup runs per-turn
# during a cost walk (thousands of calls), so without a throttle we'd stat the
# cache file thousands of times; with it, a long-running monitor still notices a
# stale (>1-day-old) cache on its own (no restart needed) by re-checking every
# 10 min.
_STALENESS_CHECK_INTERVAL = 600


def _vendored_path() -> Path:
    """Locate the bundled ``model_prices.json`` in dev or in the .app bundle."""
    name = "model_prices.json"
    here = Path(__file__).resolve()
    src = here.parents[3] / "assets" / name   # <root>/assets in a source tree
    if src.exists():
        return src
    for parent in here.parents:               # Contents/Resources in py2app
        if parent.name == "Resources" and parent.parent.name == "Contents":
            cand = parent / name
            if cand.exists():
                return cand
    return src  # may not exist; _read_json tolerates that


def _read_json(path: Path) -> Dict[str, dict]:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _load_prices() -> Dict[str, dict]:
    """Merged price map: vendored snapshot overlaid by the refreshed cache.

    Cached in-process and rebuilt only when the cache file's mtime changes, so
    a background refresh is picked up automatically on the next lookup.
    """
    ensure_fresh_prices()
    try:
        sig: Optional[int] = MODEL_PRICES_CACHE.stat().st_mtime_ns
    except OSError:
        sig = None
    global _prices, _prices_sig
    with _LOCK:
        if _prices is not None and _prices_sig == sig:
            return _prices
        merged: Dict[str, dict] = {}
        merged.update(_read_json(_vendored_path()))
        if sig is not None:
            merged.update(_read_json(MODEL_PRICES_CACHE))
        merged.pop("_about", None)  # provenance key, never a model id
        _prices = merged
        _prices_sig = sig
        return merged


# ---------------------------------------------------------------------------
# Background refresh
# ---------------------------------------------------------------------------

def _cache_is_stale() -> bool:
    try:
        return (time.time() - MODEL_PRICES_CACHE.stat().st_mtime
                > _REFRESH_MAX_AGE_SECONDS)
    except OSError:
        return True  # missing -> stale


def _refresh_now() -> None:
    """Fetch, trim, and atomically cache the LiteLLM price file (best-effort)."""
    try:
        req = urllib.request.Request(
            _LITELLM_URL, headers={"User-Agent": "leap-monitor"})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
            raw = json.loads(resp.read(_MAX_FETCH_BYTES).decode("utf-8"))
    except Exception:
        return  # offline / blocked / malformed -> keep the vendored snapshot
    if not isinstance(raw, dict):
        return
    trimmed = trim_claude(raw)
    if not trimmed:
        return
    trimmed["_about"] = {
        "source": _LITELLM_URL,
        "fetched_at": int(time.time()),
    }
    try:
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = MODEL_PRICES_CACHE.with_name(MODEL_PRICES_CACHE.name + ".tmp")
        tmp.write_text(json.dumps(trimmed))
        tmp.replace(MODEL_PRICES_CACHE)
    except OSError:
        pass  # next lookup keys on the unchanged mtime and won't reload


def _refresh_then_clear() -> None:
    """Daemon-thread body: refresh, then always clear the in-flight flag."""
    global _refresh_in_flight
    try:
        _refresh_now()
    finally:
        with _LOCK:
            _refresh_in_flight = False


def ensure_fresh_prices() -> None:
    """Schedule a background price refresh if the cache is stale.

    Re-evaluated periodically (not once per process), so a monitor left running
    past the cache's 1-day TTL refreshes on its own without a restart.  Cheap to
    call on any thread and per-turn: the filesystem staleness check is throttled
    to once per ``_STALENESS_CHECK_INTERVAL`` and an already-running fetch is
    never duplicated.  The fetch runs on a daemon thread, so it never blocks the
    caller (or the Qt GUI thread).  A failed/offline fetch simply leaves the
    cache stale, so the next check (~10 min later) retries.
    """
    global _last_check_monotonic, _refresh_in_flight
    now = time.monotonic()
    with _LOCK:
        if _refresh_in_flight:
            return
        if (_last_check_monotonic is not None
                and now - _last_check_monotonic < _STALENESS_CHECK_INTERVAL):
            return
        _last_check_monotonic = now
    if not _cache_is_stale():
        return
    with _LOCK:
        if _refresh_in_flight:
            return
        _refresh_in_flight = True
    threading.Thread(target=_refresh_then_clear, name="leap-pricing",
                     daemon=True).start()


# ---------------------------------------------------------------------------
# Public lookup + cost helpers
# ---------------------------------------------------------------------------

def _rates(entry: dict, suffix: str) -> Optional[PriceRates]:
    """Build PriceRates from an entry's fields for the given tier suffix."""
    def get(field: str) -> Optional[float]:
        val = entry.get(field + suffix)
        return float(val) if isinstance(val, (int, float)) and not isinstance(val, bool) else None

    inp = get("input_cost_per_token")
    out = get("output_cost_per_token")
    if inp is None or out is None:
        return None
    cw = get("cache_creation_input_token_cost")
    cr = get("cache_read_input_token_cost")
    # Anthropic's published ratios when a cache field is absent: write = 1.25x
    # input, read = 0.1x input.
    return PriceRates(
        input=inp, output=out,
        cache_write=cw if cw is not None else inp * 1.25,
        cache_read=cr if cr is not None else inp * 0.10,
    )


def price_for(model: str) -> Optional[ModelPricing]:
    """Pricing for a model id, or ``None`` if unknown.

    Exact match on the transcript's model id (e.g. ``claude-opus-4-8``), with a
    provider-prefix-stripped fallback (``anthropic/claude-...``).  Unknown ids
    return ``None`` so the caller shows tokens without a dollar figure rather
    than a fabricated $0.
    """
    if not model:
        return None
    prices = _load_prices()
    entry = prices.get(model)
    if entry is None and "/" in model:
        entry = prices.get(model.split("/")[-1])
    if not isinstance(entry, dict):
        return None
    base = _rates(entry, "")
    if base is None:
        return None
    return ModelPricing(base=base, premium=_rates(entry, "_above_200k_tokens"))


def turn_cost_usd(
    pricing: ModelPricing,
    input_t: int,
    output_t: int,
    cache_write_t: int,
    cache_read_t: int,
) -> float:
    """USD for a single turn given its token breakdown (rates are per-token).

    The full prompt size (input + cache_write + cache_read) decides whether the
    long-context premium applies to this turn; output is priced at the same
    tier.
    """
    prompt = input_t + cache_write_t + cache_read_t
    rates = (pricing.premium if pricing.premium is not None
             and prompt > _LONG_CONTEXT_THRESHOLD else pricing.base)
    return (input_t * rates.input
            + output_t * rates.output
            + cache_write_t * rates.cache_write
            + cache_read_t * rates.cache_read)


def format_usd(amount: float) -> str:
    """Human dollar string: ``$1.23``, ``$0.04``, ``<$0.01``, ``$0.00``."""
    if amount >= 0.01:
        return f"${amount:,.2f}"
    if amount > 0:
        return "<$0.01"
    return "$0.00"
