"""Tests for the data-driven, multi-CLI model pricing module.

Prices come from the vendored ``assets/model_prices.json`` snapshot (a trim of
LiteLLM's price file to claude-/gpt-/o*/gemini- ids), optionally overlaid by a
refreshed ``.storage`` cache.  See ``src/leap/utils/pricing.py``.
"""

import json
import time

import pytest

import leap.utils.pricing as pr
from leap.utils.pricing import (
    cost_usd,
    format_usd,
    price_for,
    trim_models,
)

# Capture the real function before the autouse fixture stubs it to a no-op, so
# the refresh-scheduling tests can exercise the genuine logic.
_REAL_ENSURE = pr.ensure_fresh_prices


def _wait_until(pred, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met in time")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Isolate every test: no background fetch, a fresh (absent) cache file so
    only the vendored snapshot loads, and a cleared in-process price cache."""
    monkeypatch.setattr(pr, "ensure_fresh_prices", lambda: None)  # block bg refresh
    monkeypatch.setattr(pr, "MODEL_PRICES_CACHE", tmp_path / "cache.json")
    monkeypatch.setattr(pr, "_prices", None)
    monkeypatch.setattr(pr, "_prices_sig", None)
    yield


# ---------------------------------------------------------------------------
# price_for (against the vendored snapshot) - all three CLI families
# ---------------------------------------------------------------------------
class TestPriceFor:
    def test_claude_opus_4_8(self):
        p = price_for("claude-opus-4-8")
        assert p is not None
        assert p.rate("input_cost_per_token", 0) == pytest.approx(5e-06)
        assert p.rate("output_cost_per_token", 0) == pytest.approx(2.5e-05)

    def test_codex_gpt_5_5(self):
        p = price_for("gpt-5.5")
        assert p is not None
        assert p.rate("input_cost_per_token", 0) == pytest.approx(5e-06)

    def test_gemini_3_flash(self):
        p = price_for("gemini-3-flash-preview")
        assert p is not None
        assert p.rate("input_cost_per_token", 0) == pytest.approx(5e-07)
        assert "output_cost_per_reasoning_token" in p.fields

    def test_prefix_fallback(self):
        assert price_for("anthropic/claude-opus-4-8") is not None

    def test_unknown_and_empty(self):
        assert price_for("totally-made-up-model") is None
        assert price_for("") is None


# ---------------------------------------------------------------------------
# ModelPricing.rate - data-driven long-context tiers
# ---------------------------------------------------------------------------
class TestRateTiers:
    def test_opus_has_no_tier(self):
        p = price_for("claude-opus-4-8")
        # No _above_* fields -> base rate at any prompt size.
        assert p.rate("input_cost_per_token", 5_000_000) == pytest.approx(5e-06)

    def test_openai_272k_tier(self):
        p = price_for("gpt-5.5")
        base = p.rate("input_cost_per_token", 100_000)
        above = p.rate("input_cost_per_token", 300_000)  # > 272k
        assert above > base
        assert above == pytest.approx(1e-05)

    def test_gemini_pro_200k_tier(self):
        p = price_for("gemini-3-pro-preview")
        assert p.rate("input_cost_per_token", 100_000) == pytest.approx(2e-06)
        assert p.rate("input_cost_per_token", 250_000) == pytest.approx(4e-06)

    def test_absent_field_is_zero(self):
        # OpenAI has no cache-creation charge -> field absent -> 0.
        p = price_for("gpt-5.5")
        assert p.rate("cache_creation_input_token_cost", 0) == 0.0


# ---------------------------------------------------------------------------
# cost_usd - token-class dot product across CLIs
# ---------------------------------------------------------------------------
class TestCostUsd:
    def test_claude_turn(self):
        p = price_for("claude-opus-4-8")
        cost = cost_usd(p, new_input=1000, cache_read=0, cache_write=2000, output=500)
        assert cost == pytest.approx(1000 * 5e-6 + 2000 * 6.25e-6 + 500 * 2.5e-5)

    def test_codex_cumulative_base_rates(self):
        p = price_for("gpt-5.5")
        # cumulative -> prompt_tokens=0 -> base rates; cached at cache-read rate
        cost = cost_usd(p, new_input=3482, cache_read=23296, output=30, prompt_tokens=0)
        assert cost == pytest.approx(3482 * 5e-6 + 23296 * 5e-7 + 30 * 3e-5)

    def test_gemini_reasoning_uses_reasoning_rate(self):
        p = price_for("gemini-3-flash-preview")
        cost = cost_usd(p, new_input=10840, output=12, reasoning=252, prompt_tokens=10840)
        # reasoning priced at output_cost_per_reasoning_token (3e-6)
        assert cost == pytest.approx(10840 * 5e-7 + 12 * 3e-6 + 252 * 3e-6)

    def test_reasoning_falls_back_to_output_rate(self):
        # A model without a dedicated reasoning rate prices reasoning as output.
        p = price_for("claude-opus-4-8")
        assert "output_cost_per_reasoning_token" not in p.fields
        cost = cost_usd(p, reasoning=100)
        assert cost == pytest.approx(100 * 2.5e-5)

    def test_tier_applies_in_cost_usd(self):
        p = price_for("gpt-5.5")
        cost = cost_usd(p, new_input=300_000, prompt_tokens=300_000)  # > 272k
        assert cost == pytest.approx(300_000 * 1e-5)


# ---------------------------------------------------------------------------
# trim_models (shared by snapshot generator + runtime refresh)
# ---------------------------------------------------------------------------
class TestTrimModels:
    def test_keeps_supported_families_and_cost_fields(self):
        raw = {
            "claude-opus-4-8": {"input_cost_per_token": 5e-06,
                                "output_cost_per_token": 2.5e-05,
                                "max_input_tokens": 200000,        # dropped
                                "litellm_provider": "anthropic"},  # dropped
            "gpt-5.5": {"input_cost_per_token": 5e-06,
                        "input_cost_per_token_above_272k_tokens": 1e-05,
                        "input_cost_per_token_flex": 2.5e-06},     # dropped (flex)
            "gemini-3-flash-preview": {"input_cost_per_token": 5e-07,
                                       "output_cost_per_reasoning_token": 3e-06},
            "mistral-large": {"input_cost_per_token": 1e-06},      # dropped (family)
            "anthropic.claude-opus-4-8-v1:0": {"input_cost_per_token": 5e-06},  # bedrock prefix
        }
        out = trim_models(raw)
        assert set(out) == {"claude-opus-4-8", "gpt-5.5", "gemini-3-flash-preview"}
        assert set(out["claude-opus-4-8"]) == {"input_cost_per_token", "output_cost_per_token"}
        assert "input_cost_per_token_above_272k_tokens" in out["gpt-5.5"]
        assert "input_cost_per_token_flex" not in out["gpt-5.5"]
        assert "output_cost_per_reasoning_token" in out["gemini-3-flash-preview"]


# ---------------------------------------------------------------------------
# Cache overlay
# ---------------------------------------------------------------------------
class TestCacheOverlay:
    def test_cache_overrides_vendored(self, monkeypatch):
        base = price_for("claude-opus-4-8").rate("input_cost_per_token", 0)
        pr.MODEL_PRICES_CACHE.write_text(json.dumps({
            "claude-opus-4-8": {"input_cost_per_token": base * 2,
                                "output_cost_per_token": 2.5e-05},
        }))
        monkeypatch.setattr(pr, "_prices", None)
        monkeypatch.setattr(pr, "_prices_sig", None)
        assert price_for("claude-opus-4-8").rate("input_cost_per_token", 0) == pytest.approx(base * 2)

    def test_about_key_never_matched_as_model(self, monkeypatch):
        pr.MODEL_PRICES_CACHE.write_text(json.dumps({
            "_about": {"source": "x"},
            "gpt-5.5": {"input_cost_per_token": 5e-06, "output_cost_per_token": 3e-05},
        }))
        monkeypatch.setattr(pr, "_prices", None)
        monkeypatch.setattr(pr, "_prices_sig", None)
        assert price_for("_about") is None
        assert price_for("gpt-5.5") is not None


# ---------------------------------------------------------------------------
# Background refresh scheduling (periodic re-check, not once-per-process)
# ---------------------------------------------------------------------------
class TestRefreshScheduling:
    def test_rechecks_after_interval_and_guards_inflight(self, monkeypatch):
        calls = []
        monkeypatch.setattr(pr, "_refresh_now", lambda: calls.append(1))
        monkeypatch.setattr(pr, "_cache_is_stale", lambda: True)
        monkeypatch.setattr(pr, "_last_check_monotonic", None)
        monkeypatch.setattr(pr, "_refresh_in_flight", False)

        _REAL_ENSURE()
        _wait_until(lambda: len(calls) == 1 and not pr._refresh_in_flight)

        _REAL_ENSURE()  # within the throttle window -> no new fetch
        time.sleep(0.02)
        assert len(calls) == 1

        monkeypatch.setattr(pr, "_last_check_monotonic",
                            time.monotonic() - pr._STALENESS_CHECK_INTERVAL - 1)
        _REAL_ENSURE()
        _wait_until(lambda: len(calls) == 2)

    def test_fresh_cache_skips_fetch(self, monkeypatch):
        calls = []
        monkeypatch.setattr(pr, "_refresh_now", lambda: calls.append(1))
        monkeypatch.setattr(pr, "_cache_is_stale", lambda: False)
        monkeypatch.setattr(pr, "_last_check_monotonic", None)
        monkeypatch.setattr(pr, "_refresh_in_flight", False)
        _REAL_ENSURE()
        time.sleep(0.03)
        assert calls == []


# ---------------------------------------------------------------------------
# format_usd
# ---------------------------------------------------------------------------
class TestFormatUsd:
    def test_dollars(self):
        assert format_usd(4.2105) == "$4.21"

    def test_thousands_separator(self):
        assert format_usd(1234.5) == "$1,234.50"

    def test_cents(self):
        assert format_usd(0.26) == "$0.26"

    def test_sub_cent(self):
        assert format_usd(0.004) == "<$0.01"

    def test_zero(self):
        assert format_usd(0.0) == "$0.00"
