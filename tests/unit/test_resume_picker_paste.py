"""Tests for paste / key parsing in the ``leap --resume`` picker.

The picker reads keys in raw mode.  A paste arrives as a multi-byte burst;
the per-keypress raw-mode toggle used to strand everything after the first
char (``tty.setraw``'s default ``TCSAFLUSH`` discards queued input).  The fix
reads the whole burst in one raw window and parses it with ``_parse_keys``,
stashing extra keys.  ``_parse_keys`` is pure, so it's unit-tested here; the
raw-mode read loop in ``_get_key`` needs a real terminal and is verified
manually (paste into the picker search).

``leap-resume.py`` is a script, so it's loaded via importlib against its path
- same pattern as ``test_permission_request_hook.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "src" / "scripts" / "leap-resume.py"
)


@pytest.fixture(scope="module")
def picker():
    spec = importlib.util.spec_from_file_location("leap_resume_picker", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_paste_prefix_is_nul(picker):
    # Must be a byte that can never be typed or appear in a key name, so the
    # search loops can tell a paste from a real keypress.
    assert picker._PASTE_PREFIX == "\x00"


def test_plain_text_is_per_char(picker):
    # A raw (non-bracketed) paste burst parses to one token per char -> the
    # search loop appends each (this is the case that used to lose all but
    # the first char).
    assert picker._parse_keys("nushi") == list("nushi")


def test_bracketed_paste_is_one_token(picker):
    P = picker._PASTE_PREFIX
    assert picker._parse_keys("\x1b[200~hello\x1b[201~") == [P + "hello"]


def test_bracketed_paste_strips_control_chars(picker):
    # Newlines/tabs dropped so a multi-line paste becomes one filter string
    # rather than submitting on the newline.
    P = picker._PASTE_PREFIX
    assert picker._parse_keys("\x1b[200~a\nb\tc\x1b[201~") == [P + "abc"]


def test_paste_without_end_marker_takes_rest(picker):
    P = picker._PASTE_PREFIX
    assert picker._parse_keys("\x1b[200~partial") == [P + "partial"]


def test_arrows_csi_and_ss3(picker):
    assert picker._parse_keys("\x1b[A\x1b[B\x1bOA\x1bOB") == [
        "up", "down", "up", "down",
    ]


def test_control_keys(picker):
    assert picker._parse_keys("\r") == ["enter"]
    assert picker._parse_keys("\n") == ["enter"]
    assert picker._parse_keys("\x7f") == ["backspace"]
    assert picker._parse_keys("\x08") == ["backspace"]
    assert picker._parse_keys("\x03") == ["quit"]
    assert picker._parse_keys("\x04") == ["quit"]
    assert picker._parse_keys("q") == ["quit"]
    assert picker._parse_keys("\x1b") == ["escape"]


def test_mixed_burst_order_preserved(picker):
    # paste, then an arrow, then a char.
    P = picker._PASTE_PREFIX
    assert picker._parse_keys("\x1b[200~ab\x1b[201~\x1b[Bx") == [
        P + "ab", "down", "x",
    ]


def test_unknown_escape_seq_is_dropped(picker):
    # An unhandled CSI (e.g. Home = ESC[H) must not leak '[' / 'H' as chars
    # into the search query.
    assert picker._parse_keys("\x1b[Hx") == ["x"]


def test_empty_input_is_no_keys(picker):
    assert picker._parse_keys("") == []
