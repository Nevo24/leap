"""Tests for the Claude USD pricing table and cost helpers.

See ``src/leap/utils/pricing.py``.
"""

import pytest

from leap.utils.pricing import (
    format_usd,
    price_for,
    turn_cost_usd,
)


# ---------------------------------------------------------------------------
# price_for
# ---------------------------------------------------------------------------
class TestPriceFor:
    def test_opus_by_substring(self):
        p = price_for("claude-opus-4-8")
        assert p is not None
        assert p.base.input == 15.0 and p.base.output == 75.0

    def test_sonnet_has_premium_tier(self):
        p = price_for("claude-sonnet-4-5-20250929")
        assert p is not None
        assert p.base.input == 3.0
        assert p.premium is not None and p.premium.input == 6.0

    def test_haiku_4_distinct_from_haiku_35(self):
        h4 = price_for("claude-haiku-4-5")
        h35 = price_for("claude-3-5-haiku-20241022")
        assert h4 is not None and h35 is not None
        assert h4.base.input == 1.0
        assert h35.base.input == 0.80

    def test_case_insensitive(self):
        assert price_for("Claude-OPUS-4-8") is not None

    def test_unknown_model_is_none(self):
        assert price_for("gpt-5-codex") is None

    def test_empty_model_is_none(self):
        assert price_for("") is None


# ---------------------------------------------------------------------------
# turn_cost_usd
# ---------------------------------------------------------------------------
class TestTurnCost:
    def test_opus_base_rate_math(self):
        p = price_for("claude-opus-4-8")
        # 1000*15 + 500*75 + 2000*18.75 + 10000*1.50 = 105000 microUSD-units
        cost = turn_cost_usd(p, input_t=1000, output_t=500,
                             cache_write_t=2000, cache_read_t=10000)
        assert cost == pytest.approx(0.105)

    def test_one_million_input_tokens_is_input_rate(self):
        p = price_for("claude-opus-4-8")
        cost = turn_cost_usd(p, input_t=1_000_000, output_t=0,
                             cache_write_t=0, cache_read_t=0)
        assert cost == pytest.approx(15.0)

    def test_sonnet_premium_applies_over_threshold(self):
        p = price_for("claude-sonnet-4-5")
        # prompt = 250k input > 200k -> premium input rate (6.0), output 22.50
        cost = turn_cost_usd(p, input_t=250_000, output_t=10,
                             cache_write_t=0, cache_read_t=0)
        assert cost == pytest.approx((250_000 * 6.0 + 10 * 22.50) / 1e6)

    def test_sonnet_base_under_threshold(self):
        p = price_for("claude-sonnet-4-5")
        cost = turn_cost_usd(p, input_t=100_000, output_t=0,
                             cache_write_t=0, cache_read_t=0)
        assert cost == pytest.approx(0.3)  # 100k * 3.0 / 1e6

    def test_opus_no_premium_falls_back_to_base(self):
        # Opus has no premium entry: a huge prompt still uses base rates.
        p = price_for("claude-opus-4-8")
        assert p.premium is None
        cost = turn_cost_usd(p, input_t=500_000, output_t=0,
                             cache_write_t=0, cache_read_t=0)
        assert cost == pytest.approx(500_000 * 15.0 / 1e6)


# ---------------------------------------------------------------------------
# format_usd
# ---------------------------------------------------------------------------
class TestFormatUsd:
    def test_dollars(self):
        assert format_usd(4.2105) == "$4.21"

    def test_thousands_separator(self):
        assert format_usd(1234.5) == "$1,234.50"

    def test_cents(self):
        assert format_usd(0.09) == "$0.09"

    def test_sub_cent_shows_less_than(self):
        assert format_usd(0.004) == "<$0.01"

    def test_zero(self):
        assert format_usd(0.0) == "$0.00"
