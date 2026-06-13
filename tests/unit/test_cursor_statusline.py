"""Tests for leap-cursor-statusline.py extract_state() and _record().

The script is stdlib-only and self-contained, so we import it directly
via importlib without bringing in the Leap package.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Import the script module without executing __main__
# ---------------------------------------------------------------------------

_SCRIPT = (Path(__file__).resolve().parent.parent.parent
           / "src" / "scripts" / "leap-cursor-statusline.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("leap_cursor_statusline", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_m = _load_module()
extract_state = _m.extract_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payload(*, window=1_000_000, used_pct=33.9, model_id="composer-2.5",
             display_name="Composer", current_usage=None,
             total_input=None) -> dict:
    """Build a minimal cursor-agent status-line payload.

    Mirrors the real shape (verified against the 2026.06.12 bundle):
    cursor-agent derives total_input_tokens from used_percentage x window.
    """
    if total_input is None and used_pct is not None and window:
        total_input = round(used_pct / 100 * window)
    cw: dict = {
        "context_window_size": window,
        "used_percentage": used_pct,
        "total_input_tokens": total_input,
        "total_output_tokens": 1234,
        "current_usage": current_usage,
    }
    return {"model": {"id": model_id, "display_name": display_name},
            "context_window": cw}


# ---------------------------------------------------------------------------
# extract_state
# ---------------------------------------------------------------------------

class TestExtractState:
    def test_typical_payload(self):
        s = extract_state(_payload(window=1_000_000, used_pct=33.9))
        assert s == {"used_tokens": 339_000, "window": 1_000_000,
                     "model": "composer-2.5"}

    def test_current_usage_dict_preferred(self):
        s = extract_state(_payload(current_usage={
            "input_tokens": 1_000,
            "cache_creation_input_tokens": 2_000,
            "cache_read_input_tokens": 3_000,
        }))
        assert s is not None and s["used_tokens"] == 6_000

    def test_current_usage_number_preferred(self):
        s = extract_state(_payload(current_usage=42_000))
        assert s is not None and s["used_tokens"] == 42_000

    def test_falls_back_to_used_percentage(self):
        p = _payload(window=100_000, used_pct=25.0)
        del p["context_window"]["total_input_tokens"]
        s = extract_state(p)
        assert s is not None and s["used_tokens"] == 25_000

    def test_no_window_returns_none(self):
        p = _payload()
        p["context_window"]["context_window_size"] = None
        assert extract_state(p) is None

    def test_zero_window_returns_none(self):
        assert extract_state(_payload(window=0)) is None

    def test_no_context_window_returns_none(self):
        assert extract_state({"model": {"id": "composer-2.5"}}) is None

    def test_no_usage_signal_returns_none(self):
        p = _payload()
        cw = p["context_window"]
        del cw["total_input_tokens"]
        cw["used_percentage"] = None
        assert extract_state(p) is None

    def test_model_falls_back_to_display_name(self):
        p = _payload()
        p["model"] = {"display_name": "Auto"}
        s = extract_state(p)
        assert s is not None and s["model"] == "Auto"

    def test_non_dict_payload_returns_none(self):
        assert extract_state(None) is None
        assert extract_state("nope") is None
        assert extract_state([]) is None


# ---------------------------------------------------------------------------
# _record (the .context state file the monitor reads)
# ---------------------------------------------------------------------------

class TestRecord:
    def test_writes_state_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LEAP_TAG", "mytag")
        monkeypatch.setenv("LEAP_SIGNAL_DIR", str(tmp_path))
        _m._record({"used_tokens": 5, "window": 10, "model": "composer-2.5"})
        data = json.loads((tmp_path / "mytag.context").read_text())
        assert data == {"used_tokens": 5, "window": 10,
                        "model": "composer-2.5"}

    def test_no_env_is_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LEAP_TAG", raising=False)
        monkeypatch.setenv("LEAP_SIGNAL_DIR", str(tmp_path))
        _m._record({"used_tokens": 5, "window": 10, "model": "m"})
        assert list(tmp_path.iterdir()) == []
