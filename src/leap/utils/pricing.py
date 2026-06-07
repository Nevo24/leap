"""USD pricing for Claude models, used to estimate a session's API cost.

Anthropic does NOT record dollar amounts in the transcript, so the monitor's
Context-cell tooltip estimates cost from token counts x these published rates.
Rates are USD per 1,000,000 tokens.

IMPORTANT - this is an *estimate* and a maintenance liability:
  * Subscription users (Claude Pro / Max / Team) are billed a flat monthly fee,
    NOT per token, so the figure is "what this session would cost on the API",
    not their actual bill.  The tooltip labels it "(est.)".
  * Prices change and new models ship; verify against
    https://www.anthropic.com/pricing and bump ``_LAST_VERIFIED`` when you do.

Only Claude is priced today (Codex / Gemini are a later step); other families
return ``None`` from :func:`price_for` and the tooltip then shows token counts
without a dollar figure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

# YYYY-MM-DD the rates below were last checked against anthropic.com/pricing.
_LAST_VERIFIED = "2026-06-07"

# Anthropic's long-context surcharge applies when a single request's input
# exceeds this many tokens (the 1M-context beta).  A turn at or below it uses
# base rates; a turn above it uses the model's ``premium`` rates when defined.
_LONG_CONTEXT_THRESHOLD = 200_000


@dataclass(frozen=True)
class PriceRates:
    """USD per 1,000,000 tokens for each billable token class."""

    input: float        # new (uncached) input tokens
    output: float       # output tokens (includes reasoning/thinking output)
    cache_write: float  # cache_creation_input_tokens
    cache_read: float   # cache_read_input_tokens


@dataclass(frozen=True)
class ModelPricing:
    """Base rates plus optional >200K long-context premium rates."""

    base: PriceRates
    premium: Optional[PriceRates] = None  # used for a turn over the threshold


# Within a model generation Anthropic prices are uniform, so family-level keys
# suffice for opus / sonnet; Haiku differs across generations so it has two
# entries.  ``premium`` (the 1M-context surcharge) is populated only where the
# published rate is known: Sonnet 4.x (2x input, 1.5x output).  Opus has no
# confirmed 1M-tier rate yet, so it falls back to base above the threshold -
# verify and add a premium entry if/when Anthropic publishes one.
_OPUS = ModelPricing(PriceRates(15.0, 75.0, 18.75, 1.50))
_SONNET = ModelPricing(
    PriceRates(3.0, 15.0, 3.75, 0.30),
    premium=PriceRates(6.0, 22.50, 7.50, 0.60),
)
_HAIKU_4 = ModelPricing(PriceRates(1.0, 5.0, 1.25, 0.10))
_HAIKU_35 = ModelPricing(PriceRates(0.80, 4.0, 1.00, 0.08))

# Matched in order: the first key that is a substring of the lowercased model
# id wins, so more specific keys come before more general ones
# ('haiku-4' / '3-5-haiku' before the bare 'haiku' fallback).
_CLAUDE_TABLE: List[Tuple[str, ModelPricing]] = [
    ("opus", _OPUS),
    ("sonnet", _SONNET),
    ("haiku-4", _HAIKU_4),
    ("3-5-haiku", _HAIKU_35),
    ("haiku", _HAIKU_35),
]


def price_for(model: str) -> Optional[ModelPricing]:
    """Pricing for a Claude model id, or ``None`` if unknown.

    Tolerant of version/date suffixes: returns the first table entry whose key
    is a substring of the lowercased model id.  Non-Claude / unrecognised ids
    yield ``None``, so the caller shows tokens without a dollar figure rather
    than a fabricated $0.
    """
    if not model:
        return None
    m = model.lower()
    for key, pricing in _CLAUDE_TABLE:
        if key in m:
            return pricing
    return None


def turn_cost_usd(
    pricing: ModelPricing,
    input_t: int,
    output_t: int,
    cache_write_t: int,
    cache_read_t: int,
) -> float:
    """USD for a single turn given its token breakdown.

    The full prompt size (input + cache_write + cache_read) decides whether the
    long-context premium applies to this turn; output is priced at the same
    tier.
    """
    prompt = input_t + cache_write_t + cache_read_t
    rates = (pricing.premium if pricing.premium is not None
             and prompt > _LONG_CONTEXT_THRESHOLD else pricing.base)
    total = (input_t * rates.input
             + output_t * rates.output
             + cache_write_t * rates.cache_write
             + cache_read_t * rates.cache_read)
    return total / 1_000_000.0


def format_usd(amount: float) -> str:
    """Human dollar string: ``$1.23``, ``$0.04``, ``<$0.01``, ``$0.00``."""
    if amount >= 0.01:
        return f"${amount:,.2f}"
    if amount > 0:
        return "<$0.01"
    return "$0.00"
