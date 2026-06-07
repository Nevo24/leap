"""Tests for the per-CLI session cost/token computers.

See ``src/leap/utils/cost_usage.py``.
"""

import json
import time

import pytest

import leap.utils.cost_usage as cu
import leap.utils.pricing as pr
from leap.utils.cost_usage import (
    CostInfo,
    claude_session_cost,
    claude_session_cost_cached,
    codex_session_cost,
    gemini_session_cost,
)
from leap.utils.pricing import cost_usd, price_for


def _wait_idle(timeout: float = 2.0) -> None:
    """Block until the background cost pool has drained (test-only).

    ``_store_result`` writes ``_RESULT`` before clearing ``_INFLIGHT`` (both
    under the lock), so an empty ``_INFLIGHT`` guarantees the result is visible.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with cu._LOCK:
            if not cu._INFLIGHT:
                return
        time.sleep(0.005)
    raise AssertionError("background cost compute did not finish in time")


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch, tmp_path):
    """Each test starts with empty accumulator + async-wrapper caches, and
    pricing isolated to the vendored snapshot (no background network fetch)."""
    # Block pricing's background refresh and pin it to the vendored snapshot so
    # cost math is deterministic and no network thread is spawned.
    monkeypatch.setattr(pr, "ensure_fresh_prices", lambda: None)
    monkeypatch.setattr(pr, "MODEL_PRICES_CACHE", tmp_path / "prices.json")
    monkeypatch.setattr(pr, "_prices", None)
    monkeypatch.setattr(pr, "_prices_sig", None)

    def _reset():
        cu._ACCUM.clear()
        cu._RESULT.clear()
        cu._RESULT_SIG.clear()
        cu._INFLIGHT.clear()
    _reset()
    yield
    _wait_idle()  # let any scheduled compute finish before clearing
    _reset()


def _assistant(usage: dict, model: str = "claude-opus-4-8",
               sidechain: bool = False) -> str:
    entry = {"type": "assistant", "message": {"model": model, "usage": usage}}
    if sidechain:
        entry["isSidechain"] = True
    return json.dumps(entry)


def _assistant_id(usage: dict, mid: str, rid: str,
                  model: str = "claude-opus-4-8") -> str:
    return json.dumps({
        "type": "assistant",
        "requestId": rid,
        "message": {"id": mid, "model": model, "usage": usage},
    })


def _usage(inp: int = 0, out: int = 0, cw: int = 0, cr: int = 0) -> dict:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr,
    }


def _write(path, *lines: str) -> None:
    path.write_text("".join(line + "\n" for line in lines))


# ---------------------------------------------------------------------------
# Basic aggregation
# ---------------------------------------------------------------------------
class TestSessionCost:
    def test_sums_tokens_and_costs_over_turns(self, tmp_path):
        t = tmp_path / "c.jsonl"
        _write(
            t,
            _assistant(_usage(inp=1000, out=500, cw=2000, cr=10000)),
            _assistant(_usage(inp=2, out=100, cw=0, cr=13500)),
        )
        info = claude_session_cost(str(t))
        assert isinstance(info, CostInfo)
        # session tokens = sum of every billable token across both turns
        assert info.session_tokens == (1000 + 500 + 2000 + 10000) + (2 + 100 + 13500)
        # last turn reflects the most recent turn only
        assert info.last_turn_tokens == 2 + 100 + 13500
        # Cost is derived from the live price table (not a hardcoded number),
        # so the test verifies accumulation/dedup, not the rates themselves.
        rates = price_for("claude-opus-4-8")
        turn1 = cost_usd(rates, new_input=1000, output=500, cache_write=2000, cache_read=10000)
        turn2 = cost_usd(rates, new_input=2, output=100, cache_write=0, cache_read=13500)
        assert info.session_cost_usd == pytest.approx(turn1 + turn2)
        assert info.last_turn_cost_usd == pytest.approx(turn2)

    def test_skips_sidechain_subagent_turn(self, tmp_path):
        t = tmp_path / "c.jsonl"
        _write(
            t,
            _assistant(_usage(inp=1000, out=10)),
            _assistant(_usage(inp=999999, out=999999), sidechain=True),
        )
        info = claude_session_cost(str(t))
        assert info.session_tokens == 1010  # sidechain excluded
        assert info.last_turn_tokens == 1010

    def test_unknown_model_keeps_tokens_drops_cost(self, tmp_path):
        t = tmp_path / "c.jsonl"
        _write(t, _assistant(_usage(inp=1000, out=500), model="totally-made-up-model"))
        info = claude_session_cost(str(t))
        assert info.session_tokens == 1500
        assert info.last_turn_tokens == 1500
        assert info.session_cost_usd is None
        assert info.last_turn_cost_usd is None

    def test_mixed_known_unknown_models(self, tmp_path):
        t = tmp_path / "c.jsonl"
        _write(
            t,
            _assistant(_usage(inp=1000, out=0), model="claude-opus-4-8"),
            _assistant(_usage(inp=500, out=0), model="mystery-model"),
        )
        info = claude_session_cost(str(t))
        # session cost reflects only the priced (opus) turn; last turn unpriced
        opus = price_for("claude-opus-4-8")
        assert info.session_cost_usd == pytest.approx(cost_usd(opus, new_input=1000))
        assert info.last_turn_cost_usd is None
        assert info.session_tokens == 1500

    def test_turn_without_usage_ignored(self, tmp_path):
        t = tmp_path / "c.jsonl"
        _write(
            t,
            json.dumps({"type": "assistant", "message": {"model": "x"}}),
            _assistant(_usage(inp=10, out=5)),
        )
        info = claude_session_cost(str(t))
        assert info.session_tokens == 15

    def test_corrupt_lines_skipped(self, tmp_path):
        t = tmp_path / "c.jsonl"
        t.write_text(
            "not json\n"
            + _assistant(_usage(inp=10, out=5)) + "\n"
            + "{bad\n"
        )
        info = claude_session_cost(str(t))
        assert info.session_tokens == 15


# ---------------------------------------------------------------------------
# None / empty cases
# ---------------------------------------------------------------------------
class TestNoneCases:
    def test_empty_path(self):
        assert claude_session_cost("") is None

    def test_nonexistent_file(self, tmp_path):
        assert claude_session_cost(str(tmp_path / "nope.jsonl")) is None

    def test_empty_file(self, tmp_path):
        t = tmp_path / "c.jsonl"
        t.write_text("")
        assert claude_session_cost(str(t)) is None

    def test_no_assistant_turns(self, tmp_path):
        t = tmp_path / "c.jsonl"
        _write(t, json.dumps({"type": "user", "message": {}}))
        assert claude_session_cost(str(t)) is None


# ---------------------------------------------------------------------------
# Incremental accumulation
# ---------------------------------------------------------------------------
class TestIncremental:
    def test_appended_turns_accumulate(self, tmp_path):
        t = tmp_path / "c.jsonl"
        _write(t, _assistant(_usage(inp=1000, out=100)))
        first = claude_session_cost(str(t))
        assert first.session_tokens == 1100

        # Append a second turn; re-poll should add to the running total.
        with open(t, "a") as f:
            f.write(_assistant(_usage(inp=2000, out=200)) + "\n")
        second = claude_session_cost(str(t))
        assert second.session_tokens == 1100 + 2200
        assert second.last_turn_tokens == 2200

    def test_partial_trailing_line_not_counted_until_complete(self, tmp_path):
        t = tmp_path / "c.jsonl"
        _write(t, _assistant(_usage(inp=1000, out=100)))
        claude_session_cost(str(t))
        # Append a line WITHOUT a trailing newline (mid-write).
        with open(t, "a") as f:
            f.write(_assistant(_usage(inp=5000, out=500)))
        mid = claude_session_cost(str(t))
        assert mid.session_tokens == 1100  # partial line not yet folded
        # Complete the line; now it counts.
        with open(t, "a") as f:
            f.write("\n")
        done = claude_session_cost(str(t))
        assert done.session_tokens == 1100 + 5500

    def test_truncation_resets_accumulator(self, tmp_path):
        t = tmp_path / "c.jsonl"
        _write(
            t,
            _assistant(_usage(inp=1000, out=100)),
            _assistant(_usage(inp=2000, out=200)),
        )
        claude_session_cost(str(t))
        # Rewrite the file smaller (offset now past EOF) -> full re-walk.
        _write(t, _assistant(_usage(inp=7, out=3)))
        reset = claude_session_cost(str(t))
        assert reset.session_tokens == 10
        assert reset.last_turn_tokens == 10


# ---------------------------------------------------------------------------
# Duplicate-entry dedup (Claude repeats usage across split message lines)
# ---------------------------------------------------------------------------
class TestDedup:
    def test_split_message_lines_counted_once(self, tmp_path):
        t = tmp_path / "c.jsonl"
        u = _usage(inp=1000, out=500, cw=2000, cr=10000)
        _write(
            t,
            _assistant_id(u, "msg_1", "req_1"),
            _assistant_id(u, "msg_1", "req_1"),  # duplicate split line
            _assistant_id(u, "msg_1", "req_1"),  # duplicate split line
            _assistant_id(_usage(inp=2, out=100, cr=13500), "msg_2", "req_2"),
        )
        info = claude_session_cost(str(t))
        assert info.session_tokens == (1000 + 500 + 2000 + 10000) + (2 + 100 + 13500)
        assert info.last_turn_tokens == 2 + 100 + 13500

    def test_dedup_persists_across_incremental_polls(self, tmp_path):
        t = tmp_path / "c.jsonl"
        u = _usage(inp=1000, out=100)
        _write(t, _assistant_id(u, "msg_1", "req_1"))
        assert claude_session_cost(str(t)).session_tokens == 1100
        # A duplicate split-line of the same message arrives in a later poll.
        with open(t, "a") as f:
            f.write(_assistant_id(u, "msg_1", "req_1") + "\n")
        assert claude_session_cost(str(t)).session_tokens == 1100  # ignored

    def test_distinct_request_ids_both_counted(self, tmp_path):
        t = tmp_path / "c.jsonl"
        u = _usage(inp=1000, out=100)
        _write(
            t,
            _assistant_id(u, "msg_1", "req_1"),
            _assistant_id(u, "msg_2", "req_2"),  # same usage, different request
        )
        assert claude_session_cost(str(t)).session_tokens == 2200

    def test_entries_without_ids_not_deduped(self, tmp_path):
        # Identical usage but no id/requestId -> can't dedup safely, count both.
        t = tmp_path / "c.jsonl"
        _write(
            t,
            _assistant(_usage(inp=100, out=50)),
            _assistant(_usage(inp=100, out=50)),
        )
        assert claude_session_cost(str(t)).session_tokens == 300


# ---------------------------------------------------------------------------
# Non-blocking cached wrapper
# ---------------------------------------------------------------------------
class TestCachedWrapper:
    def test_none_first_then_result_after_compute(self, tmp_path):
        t = tmp_path / "c.jsonl"
        _write(t, _assistant_id(_usage(inp=1000, out=100), "m1", "r1"))
        # First call schedules the background compute and returns nothing yet.
        assert claude_session_cost_cached(str(t)) is None
        _wait_idle()
        # Result is now cached and returned without touching the file inline.
        info = claude_session_cost_cached(str(t))
        assert info is not None and info.session_tokens == 1100

    def test_empty_path_returns_none(self):
        assert claude_session_cost_cached("") is None

    def test_nonexistent_path_returns_none(self, tmp_path):
        assert claude_session_cost_cached(str(tmp_path / "nope.jsonl")) is None

    def test_growth_triggers_recompute(self, tmp_path):
        t = tmp_path / "c.jsonl"
        _write(t, _assistant_id(_usage(inp=1000, out=100), "m1", "r1"))
        claude_session_cost_cached(str(t))
        _wait_idle()
        assert claude_session_cost_cached(str(t)).session_tokens == 1100
        # Append a new message; the changed size schedules a fresh compute.
        with open(t, "a") as f:
            f.write(_assistant_id(_usage(inp=2000, out=200), "m2", "r2") + "\n")
        claude_session_cost_cached(str(t))  # schedules recompute, returns stale
        _wait_idle()
        assert claude_session_cost_cached(str(t)).session_tokens == 1100 + 2200

    def test_empty_transcript_settles_to_none_without_busy_reschedule(self, tmp_path):
        t = tmp_path / "c.jsonl"
        t.write_text("")
        claude_session_cost_cached(str(t))
        _wait_idle()
        # Unchanged empty file: sig matches, so no new compute is scheduled.
        assert claude_session_cost_cached(str(t)) is None
        with cu._LOCK:
            assert not cu._INFLIGHT


# ===========================================================================
# Codex - cumulative total_token_usage given directly (no walk)
# ===========================================================================
def _codex_usage(inp: int, cached: int, out: int) -> dict:
    return {"input_tokens": inp, "cached_input_tokens": cached,
            "output_tokens": out, "reasoning_output_tokens": 0,
            "total_tokens": inp + out}


def _codex_token_count(total: dict, last: dict, window: int = 258400) -> str:
    return json.dumps({"type": "event_msg", "payload": {
        "type": "token_count",
        "info": {"total_token_usage": total, "last_token_usage": last,
                 "model_context_window": window}}})


def _codex_turn_context(model: str = "gpt-5.5") -> str:
    return json.dumps({"type": "turn_context", "payload": {"model": model}})


class TestCodex:
    def test_reads_cumulative_total_and_last(self, tmp_path):
        t = tmp_path / "r.jsonl"
        _write(
            t,
            _codex_turn_context("gpt-5.5"),
            _codex_token_count(_codex_usage(13000, 11000, 20),
                               _codex_usage(13000, 11000, 20)),
            _codex_token_count(_codex_usage(26000, 23000, 30),   # latest wins
                               _codex_usage(13000, 12000, 10)),
        )
        info = codex_session_cost(str(t))
        assert info.session_tokens == 26030    # 26000 + 30
        assert info.last_turn_tokens == 13010   # 13000 + 10
        p = price_for("gpt-5.5")
        exp_sess = cost_usd(p, new_input=26000 - 23000, cache_read=23000,
                            output=30, prompt_tokens=0)
        exp_last = cost_usd(p, new_input=13000 - 12000, cache_read=12000,
                            output=10, prompt_tokens=13000)
        assert info.session_cost_usd == pytest.approx(exp_sess)
        assert info.last_turn_cost_usd == pytest.approx(exp_last)

    def test_unknown_model_tokens_no_cost(self, tmp_path):
        t = tmp_path / "r.jsonl"
        _write(t, _codex_turn_context("totally-made-up"),
               _codex_token_count(_codex_usage(100, 0, 50),
                                  _codex_usage(100, 0, 50)))
        info = codex_session_cost(str(t))
        assert info.session_tokens == 150
        assert info.session_cost_usd is None
        assert info.last_turn_cost_usd is None

    def test_no_token_count_returns_none(self, tmp_path):
        t = tmp_path / "r.jsonl"
        _write(t, _codex_turn_context("gpt-5.5"))
        assert codex_session_cost(str(t)) is None

    def test_empty_token_count_does_not_clobber_usage(self, tmp_path):
        # An early real event followed by an empty one (no tokens yet): the
        # last *meaningful* event must win, not the trailing empty one.
        t = tmp_path / "r.jsonl"
        empty = json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"model_context_window": 258400}}})
        _write(
            t,
            _codex_turn_context("gpt-5.5"),
            _codex_token_count(_codex_usage(26000, 23000, 30),
                               _codex_usage(13000, 12000, 10)),
            empty,
        )
        info = codex_session_cost(str(t))
        assert info is not None
        assert info.session_tokens == 26030

    def test_empty_and_missing(self, tmp_path):
        assert codex_session_cost("") is None
        assert codex_session_cost(str(tmp_path / "nope.jsonl")) is None


# ===========================================================================
# Gemini - per-turn tokens, walk + sum
# ===========================================================================
def _gem(model: str, inp: int, out: int, cached: int = 0,
         thoughts: int = 0, tool: int = 0) -> str:
    return json.dumps({"type": "gemini", "model": model, "tokens": {
        "input": inp, "output": out, "cached": cached,
        "thoughts": thoughts, "tool": tool,
        "total": inp + out + thoughts}})


class TestGemini:
    def test_sums_turns(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write(
            t,
            _gem("gemini-3-flash-preview", 1000, 50, thoughts=10),
            _gem("gemini-3-flash-preview", 2000, 80, thoughts=20),
        )
        info = gemini_session_cost(str(t))
        assert info.session_tokens == (1000 + 50 + 10) + (2000 + 80 + 20)
        assert info.last_turn_tokens == 2000 + 80 + 20
        p = price_for("gemini-3-flash-preview")
        t1 = cost_usd(p, new_input=1000, output=50, reasoning=10, prompt_tokens=1000)
        t2 = cost_usd(p, new_input=2000, output=80, reasoning=20, prompt_tokens=2000)
        assert info.session_cost_usd == pytest.approx(t1 + t2)
        assert info.last_turn_cost_usd == pytest.approx(t2)

    def test_cached_subset_priced_at_cache_rate(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write(t, _gem("gemini-3-flash-preview", 1000, 0, cached=400))
        info = gemini_session_cost(str(t))
        p = price_for("gemini-3-flash-preview")
        exp = cost_usd(p, new_input=600, cache_read=400, output=0, prompt_tokens=1000)
        assert info.session_cost_usd == pytest.approx(exp)

    def test_unknown_model_tokens_no_cost(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write(t, _gem("made-up-gemini", 100, 10))
        info = gemini_session_cost(str(t))
        assert info.session_tokens == 110
        assert info.session_cost_usd is None

    def test_skips_non_gemini_entries(self, tmp_path):
        t = tmp_path / "s.jsonl"
        _write(t, json.dumps({"type": "user", "tokens": {"input": 999}}),
               _gem("gemini-3-flash-preview", 100, 10))
        info = gemini_session_cost(str(t))
        assert info.session_tokens == 110

    def test_empty_and_missing(self, tmp_path):
        assert gemini_session_cost("") is None
        assert gemini_session_cost(str(tmp_path / "nope.jsonl")) is None
