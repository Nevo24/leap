"""Tests for the data-driven model pricing module.

Prices come from the vendored ``assets/model_prices.json`` snapshot (a trim of
LiteLLM's price file), optionally overlaid by a refreshed ``.storage`` cache.
See ``src/leap/utils/pricing.py``.
"""

import json
import time

import pytest

import leap.utils.pricing as pr
from leap.utils.pricing import (
    format_usd,
    price_for,
    trim_claude,
    turn_cost_usd,
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
# price_for (against the vendored snapshot)
# ---------------------------------------------------------------------------
class TestPriceFor:
    def test_opus_4_8_exact_match(self):
        p = price_for("claude-opus-4-8")
        assert p is not None
        # Opus 4.x is $5/$25 per Mtok, i.e. 5e-6/2.5e-5 per token.
        assert p.base.input == pytest.approx(5e-06)
        assert p.base.output == pytest.approx(2.5e-05)
        assert p.premium is None  # Opus has no >200k tier

    def test_sonnet_has_premium_tier(self):
        p = price_for("claude-sonnet-4-5")
        assert p is not None and p.premium is not None
        assert p.premium.input > p.base.input

    def test_prefix_fallback(self):
        assert price_for("anthropic/claude-opus-4-8") is not None

    def test_unknown_model_is_none(self):
        assert price_for("gpt-5-codex") is None

    def test_empty_model_is_none(self):
        assert price_for("") is None


# ---------------------------------------------------------------------------
# turn_cost_usd (per-token rates -> dot product)
# ---------------------------------------------------------------------------
class TestTurnCost:
    def test_opus_turn_math(self):
        p = price_for("claude-opus-4-8")
        # 1989 in + 17 out + 40472 cache-write, 0 cache-read (a fresh session's
        # first turn). Independent recompute from the rates.
        expected = (1989 * p.base.input + 17 * p.base.output
                    + 40472 * p.base.cache_write + 0 * p.base.cache_read)
        assert turn_cost_usd(p, 1989, 17, 40472, 0) == pytest.approx(expected)

    def test_sonnet_premium_applies_over_threshold(self):
        p = price_for("claude-sonnet-4-5")
        cost = turn_cost_usd(p, 250_000, 10, 0, 0)  # prompt 250k > 200k
        assert cost == pytest.approx(250_000 * p.premium.input
                                     + 10 * p.premium.output)

    def test_sonnet_base_under_threshold(self):
        p = price_for("claude-sonnet-4-5")
        cost = turn_cost_usd(p, 100_000, 0, 0, 0)
        assert cost == pytest.approx(100_000 * p.base.input)


# ---------------------------------------------------------------------------
# trim_claude (shared by snapshot generator + runtime refresh)
# ---------------------------------------------------------------------------
class TestTrimClaude:
    def test_keeps_only_claude_and_cost_fields(self):
        raw = {
            "claude-opus-4-8": {
                "input_cost_per_token": 5e-06,
                "output_cost_per_token": 2.5e-05,
                "cache_creation_input_token_cost": 6.25e-06,
                "cache_read_input_token_cost": 5e-07,
                "max_input_tokens": 200000,   # dropped (not a cost field)
                "litellm_provider": "anthropic",  # dropped
            },
            "gpt-5": {"input_cost_per_token": 1e-06},  # dropped (not claude-)
            "anthropic.claude-opus-4-8-v1:0": {"input_cost_per_token": 5e-06},  # dropped (bedrock prefix)
        }
        out = trim_claude(raw)
        assert set(out) == {"claude-opus-4-8"}
        assert set(out["claude-opus-4-8"]) == {
            "input_cost_per_token", "output_cost_per_token",
            "cache_creation_input_token_cost", "cache_read_input_token_cost",
        }

    def test_keeps_above_200k_premium_fields(self):
        raw = {"claude-sonnet-4-5": {
            "input_cost_per_token": 3e-06,
            "output_cost_per_token": 1.5e-05,
            "input_cost_per_token_above_200k_tokens": 6e-06,
            "output_cost_per_token_above_200k_tokens": 2.25e-05,
        }}
        out = trim_claude(raw)
        assert "input_cost_per_token_above_200k_tokens" in out["claude-sonnet-4-5"]


# ---------------------------------------------------------------------------
# Cache overlay (refreshed .storage file wins over the vendored snapshot)
# ---------------------------------------------------------------------------
class TestCacheOverlay:
    def test_cache_overrides_vendored(self, monkeypatch):
        base = price_for("claude-opus-4-8").base.input
        # Write a refreshed cache that changes opus-4-8's input rate.
        pr.MODEL_PRICES_CACHE.write_text(json.dumps({
            "claude-opus-4-8": {
                "input_cost_per_token": base * 2,
                "output_cost_per_token": 2.5e-05,
            }
        }))
        monkeypatch.setattr(pr, "_prices", None)  # force reload
        monkeypatch.setattr(pr, "_prices_sig", None)
        assert price_for("claude-opus-4-8").base.input == pytest.approx(base * 2)

    def test_about_key_never_matched_as_model(self, monkeypatch):
        pr.MODEL_PRICES_CACHE.write_text(json.dumps({
            "_about": {"source": "x"},
            "claude-opus-4-8": {"input_cost_per_token": 5e-06,
                                "output_cost_per_token": 2.5e-05},
        }))
        monkeypatch.setattr(pr, "_prices", None)  # force reload
        monkeypatch.setattr(pr, "_prices_sig", None)
        assert price_for("_about") is None
        assert price_for("claude-opus-4-8") is not None


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

        _REAL_ENSURE()  # first check -> schedules a refresh
        _wait_until(lambda: len(calls) == 1 and not pr._refresh_in_flight)

        _REAL_ENSURE()  # within the throttle window -> no new fetch
        time.sleep(0.02)
        assert len(calls) == 1

        # Pretend the last check was long ago: re-checks and refreshes again,
        # i.e. a long-running monitor refreshes without a restart.
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
