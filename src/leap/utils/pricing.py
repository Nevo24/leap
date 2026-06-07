"""Model USD pricing for the monitor's Context-cell cost estimate.

Prices are NOT hardcoded.  They come from LiteLLM's community-maintained
``model_prices_and_context_window.json`` (the same source ``ccusage`` uses),
which keys every model exactly (``claude-opus-4-8``, ``gpt-5.5``,
``gemini-3-flash-preview`` ...) with per-token costs for each token class.

Two layers, so it's correct offline *and* self-updating:
  1. **Vendored snapshot** - ``assets/model_prices.json`` (a trim of the
     LiteLLM file to the model families the supported CLIs report -
     ``claude-*`` / ``gpt-*`` / ``o*`` / ``gemini-*``) ships in the repo, so
     prices are right on first run and with no network.
  2. **Background refresh** - :func:`ensure_fresh_prices` (called lazily the
     first time a price is looked up) fetches the latest LiteLLM file in a
     daemon thread when the cache is stale, trims it, and writes
     ``.storage/model_prices.json``.  That cache overlays the vendored snapshot
     and is picked up automatically (the loader keys on the file's mtime).  A
     failed/blocked fetch is silently ignored - the vendored snapshot stands.

The dollar figure is still an *estimate* labeled "(est.)" in the UI:
subscription users (Pro / Max / Team / ChatGPT plan) pay a flat fee, not per
token.

Pricing is data-driven per model: :class:`ModelPricing` wraps the raw LiteLLM
cost fields, ``rate()`` applies the right long-context tier
(``*_above_<N>k_tokens`` - Anthropic uses 200k, OpenAI 272k, Gemini 200k), and
:func:`cost_usd` sums the token classes.  Costs are per-token (LiteLLM's native
unit), so it's a plain dot product with no scaling.

Defensive throughout - any IO/parse failure falls back to the vendored data or
to "no price" (the tooltip then shows token counts without dollars).
"""

from __future__ import annotations

import json
import re
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

# Bare model-id prefixes the supported CLIs write into their transcripts
# (Claude / OpenAI-Codex / Gemini).  Provider-prefixed Bedrock/Vertex/Azure
# duplicates (``anthropic.``, ``vertex_ai/`` ...) are excluded - the CLIs use
# the bare ids, and price_for() strips a leading ``provider/`` as a fallback.
_MODEL_PREFIXES = ("claude-", "gpt-", "o1", "o3", "o4", "chatgpt-", "gemini-")

# LiteLLM per-token cost fields we price on.  Each may also appear with a
# ``_above_<N>k_tokens`` long-context-tier suffix, kept by the trim and applied
# by ModelPricing.rate().  Everything else (flex/priority/batch/audio/search)
# is dropped to keep the snapshot small.
_KEEP_BASES = frozenset({
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_creation_input_token_cost",
    "cache_read_input_token_cost",
    "output_cost_per_reasoning_token",
})

# Matches a long-context tier field, e.g. ``input_cost_per_token_above_272k_tokens``.
_ABOVE_RE = re.compile(r"^(?P<base>.+)_above_(?P<n>\d+)k_tokens$")


@dataclass(frozen=True)
class ModelPricing:
    """Per-token USD cost fields for one model (LiteLLM's native shape)."""

    fields: Dict[str, float]

    def rate(self, base_field: str, prompt_tokens: int) -> float:
        """Per-token USD for ``base_field``, applying the highest published
        long-context tier the prompt qualifies for.

        Absent field -> 0.0 (correct for classes a provider doesn't bill, e.g.
        OpenAI/Gemini have no cache-creation charge).
        """
        rate = _as_float(self.fields.get(base_field))
        best_thr = 0
        for name, val in self.fields.items():
            m = _ABOVE_RE.match(name)
            if m and m.group("base") == base_field:
                thr = int(m.group("n")) * 1000
                if prompt_tokens > thr and thr > best_thr:
                    fval = _as_float(val)
                    if fval is not None:
                        rate, best_thr = fval, thr
        return rate or 0.0


def _as_float(value: object) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


# ---------------------------------------------------------------------------
# Trim (shared by the vendored-snapshot generator and the runtime refresh)
# ---------------------------------------------------------------------------

def trim_models(raw: Dict[str, dict]) -> Dict[str, dict]:
    """Reduce a full LiteLLM price map to the model families the CLIs report.

    Keeps bare ``claude-*`` / ``gpt-*`` / ``o*`` / ``chatgpt-*`` / ``gemini-*``
    ids (the form the CLIs write into transcripts) and, per model, only the
    per-token cost fields in :data:`_KEEP_BASES` plus their
    ``*_above_<N>k_tokens`` tier variants.  Used both to generate the vendored
    ``assets/model_prices.json`` and to shrink the runtime-fetched file before
    caching it, so the two always share a shape.
    """
    out: Dict[str, dict] = {}
    for model, entry in raw.items():
        if not (isinstance(model, str) and model.startswith(_MODEL_PREFIXES)):
            continue
        if not isinstance(entry, dict):
            continue
        kept = {}
        for field, val in entry.items():
            if _as_float(val) is None:
                continue
            m = _ABOVE_RE.match(field)
            base = m.group("base") if m else field
            if base in _KEEP_BASES:
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
    trimmed = trim_models(raw)
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

def price_for(model: str) -> Optional[ModelPricing]:
    """Pricing for a model id, or ``None`` if unknown.

    Exact match on the transcript's model id (e.g. ``claude-opus-4-8``,
    ``gpt-5.5``, ``gemini-3-flash-preview``), with a provider-prefix-stripped
    fallback (``anthropic/claude-...``).  Unknown ids return ``None`` so the
    caller shows tokens without a dollar figure rather than a fabricated $0.
    """
    if not model:
        return None
    prices = _load_prices()
    entry = prices.get(model)
    if entry is None and "/" in model:
        entry = prices.get(model.split("/")[-1])
    if not isinstance(entry, dict) or not entry:
        return None
    return ModelPricing(fields=entry)


def cost_usd(
    pricing: ModelPricing,
    *,
    new_input: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    output: int = 0,
    reasoning: int = 0,
    prompt_tokens: Optional[int] = None,
) -> float:
    """USD for one turn given its token classes (rates are per-token).

    ``new_input`` is uncached input; ``cache_read``/``cache_write`` are the
    cached-prefix read/creation tokens; ``output`` is generated tokens;
    ``reasoning`` is thinking/reasoning tokens priced at the dedicated reasoning
    rate when the model has one (Gemini), else the output rate.  ``prompt_tokens``
    (the full request prompt) decides the long-context tier; it defaults to
    ``new_input + cache_read + cache_write``.
    """
    if prompt_tokens is None:
        prompt_tokens = new_input + cache_read + cache_write
    total = (
        new_input * pricing.rate("input_cost_per_token", prompt_tokens)
        + cache_read * pricing.rate("cache_read_input_token_cost", prompt_tokens)
        + cache_write * pricing.rate("cache_creation_input_token_cost", prompt_tokens)
        + output * pricing.rate("output_cost_per_token", prompt_tokens)
    )
    if reasoning:
        field = ("output_cost_per_reasoning_token"
                 if "output_cost_per_reasoning_token" in pricing.fields
                 else "output_cost_per_token")
        total += reasoning * pricing.rate(field, prompt_tokens)
    return total


def format_usd(amount: float) -> str:
    """Human dollar string: ``$1.23``, ``$0.04``, ``<$0.01``, ``$0.00``."""
    if amount >= 0.01:
        return f"${amount:,.2f}"
    if amount > 0:
        return "<$0.01"
    return "$0.00"
