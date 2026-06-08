"""Tests for leap-claude-statusline.py extract_state().

The script is stdlib-only and self-contained, so we import it directly
via importlib without bringing in the Leap package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import the script module without executing __main__
# ---------------------------------------------------------------------------

_SCRIPT = (Path(__file__).resolve().parent.parent.parent
           / "src" / "scripts" / "leap-claude-statusline.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("leap_claude_statusline", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_m = _load_module()
extract_state = _m.extract_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payload(*, window: int = 1_000_000,
             input_tokens: int = 70_000,
             cache_create: int = 5_000,
             cache_read: int = 60_000,
             output_tokens: int = 500,
             model_id: str = "claude-opus-4-8",
             omit_context_window: bool = False,
             omit_current_usage: bool = False) -> dict:
    """Build a minimal Claude status-line payload."""
    cw: dict = {"context_window_size": window}
    if not omit_current_usage:
        cw["current_usage"] = {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
            "output_tokens": output_tokens,
        }
    else:
        # Provide total_input_tokens as the fallback
        cw["total_input_tokens"] = input_tokens + cache_create + cache_read
    return {"model": {"id": model_id, "display_name": "Opus"},
            "context_window": cw} if not omit_context_window else {"model": {"id": model_id}}


# ---------------------------------------------------------------------------
# window resolution
# ---------------------------------------------------------------------------

class TestWindowResolution:
    def test_1m_window(self):
        s = extract_state(_payload(window=1_000_000))
        assert s is not None
        assert s["window"] == 1_000_000

    def test_200k_window(self):
        s = extract_state(_payload(window=200_000))
        assert s is not None
        assert s["window"] == 200_000

    def test_zero_window_returns_none(self):
        assert extract_state(_payload(window=0)) is None

    def test_negative_window_returns_none(self):
        assert extract_state(_payload(window=-1)) is None

    def test_missing_context_window_key_returns_none(self):
        assert extract_state(_payload(omit_context_window=True)) is None

    def test_non_dict_payload_returns_none(self):
        assert extract_state(None) is None
        assert extract_state("string") is None
        assert extract_state(42) is None


# ---------------------------------------------------------------------------
# used_tokens: current_usage sum (input-only, excludes output)
# ---------------------------------------------------------------------------

class TestUsedTokens:
    def test_current_usage_sum_excludes_output(self):
        # input=70k + cache_create=5k + cache_read=60k = 135k; output=500 excluded
        s = extract_state(_payload(
            input_tokens=70_000, cache_create=5_000, cache_read=60_000,
            output_tokens=500))
        assert s is not None
        assert s["used_tokens"] == 135_000

    def test_zero_used_tokens_allowed(self):
        s = extract_state(_payload(
            input_tokens=0, cache_create=0, cache_read=0, output_tokens=0))
        assert s is not None
        assert s["used_tokens"] == 0

    def test_fallback_to_total_input_tokens_when_current_usage_absent(self):
        # Older Claude builds emit total_input_tokens but no current_usage.
        s = extract_state(_payload(
            input_tokens=90_000, cache_create=5_000, cache_read=30_000,
            omit_current_usage=True))
        assert s is not None
        # _payload sets total_input_tokens = input + cache_create + cache_read
        assert s["used_tokens"] == 125_000

    def test_current_usage_not_dict_falls_back(self):
        p = _payload()
        p["context_window"]["current_usage"] = "bad"
        p["context_window"]["total_input_tokens"] = 77_000
        s = extract_state(p)
        assert s is not None
        assert s["used_tokens"] == 77_000

    def test_missing_fields_in_current_usage_treated_as_zero(self):
        p = {"model": {"id": "m"}, "context_window": {
            "context_window_size": 200_000,
            "current_usage": {}}}
        s = extract_state(p)
        assert s is not None
        assert s["used_tokens"] == 0


# ---------------------------------------------------------------------------
# model field
# ---------------------------------------------------------------------------

class TestModelField:
    def test_model_id_preferred(self):
        s = extract_state(_payload(model_id="claude-opus-4-8"))
        assert s is not None
        assert s["model"] == "claude-opus-4-8"

    def test_display_name_fallback_when_no_id(self):
        p = _payload()
        p["model"] = {"id": "", "display_name": "Opus"}
        s = extract_state(p)
        assert s is not None
        assert s["model"] == "Opus"

    def test_model_string_passthrough(self):
        p = _payload()
        p["model"] = "claude-opus-4-8"
        s = extract_state(p)
        assert s is not None
        assert s["model"] == "claude-opus-4-8"

    def test_missing_model_gives_empty_string(self):
        p = _payload()
        del p["model"]
        s = extract_state(p)
        assert s is not None
        assert s["model"] == ""

    def test_non_dict_non_str_model_gives_empty_string(self):
        p = _payload()
        p["model"] = 42
        s = extract_state(p)
        assert s is not None
        assert s["model"] == ""


# ---------------------------------------------------------------------------
# Robustness: extra / missing / malformed fields
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_extra_top_level_fields_ignored(self):
        p = _payload()
        p["unknown_future_field"] = {"nested": True}
        assert extract_state(p) is not None

    def test_context_window_size_string_treated_as_zero(self):
        p = _payload()
        p["context_window"]["context_window_size"] = "1000000"
        assert extract_state(p) is None  # non-numeric -> 0 -> None

    def test_empty_dict_returns_none(self):
        assert extract_state({}) is None
