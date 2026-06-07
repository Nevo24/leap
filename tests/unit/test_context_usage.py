"""Tests for the Claude context-window-usage util.

Pure transcript math (no Qt): given a Claude transcript JSONL, compute how
full the context window is from the latest main-chain assistant turn's
``message.usage`` block.  See ``src/leap/utils/context_usage.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import leap.utils.context_usage as cu
from leap.utils.context_usage import (
    ContextUsage,
    context_usage_for_transcript,
    context_window_for_model,
)


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')


def _assistant(model: str, *, inp: int, cache_create: int, cache_read: int,
               out: int = 0, sidechain: bool = False, cwd: str = '') -> dict:
    entry = {
        'type': 'assistant',
        'cwd': cwd,
        'message': {
            'model': model,
            'usage': {
                'input_tokens': inp,
                'cache_creation_input_tokens': cache_create,
                'cache_read_input_tokens': cache_read,
                'output_tokens': out,
            },
        },
    }
    if sidechain:
        entry['isSidechain'] = True
    return entry


@pytest.fixture
def fake_claude_config(tmp_path, monkeypatch):
    """Point the util at a synthetic ``~/.claude.json`` and reset its cache.

    Call the returned function with a ``projects`` mapping, e.g.
    ``{"/proj": {"lastModelUsage": {"claude-opus-4-8[1m]": {}}}}``.
    """
    cfg = tmp_path / '.claude.json'

    def _write(projects: dict) -> None:
        cfg.write_text(json.dumps({'projects': projects}))
        monkeypatch.setattr(cu, '_CLAUDE_CONFIG_PATH', str(cfg))
        monkeypatch.setattr(cu, '_one_m_projects_cache', None)
        monkeypatch.setattr(cu, '_one_m_cache_at', 0.0)

    return _write


# --------------------------------------------------------------------------
# context_window_for_model
# --------------------------------------------------------------------------

class TestContextWindowForModel:
    def test_unknown_model_defaults_to_200k(self):
        assert context_window_for_model('claude-opus-4-8') == 200_000

    def test_empty_model_defaults_to_200k(self):
        assert context_window_for_model('') == 200_000


# --------------------------------------------------------------------------
# ContextUsage.percent
# --------------------------------------------------------------------------

class TestPercent:
    def test_basic_fraction(self):
        assert ContextUsage(100_000, 200_000, 'm').percent == 50

    def test_quarter(self):
        assert ContextUsage(50_000, 200_000, 'm').percent == 25

    def test_clamped_to_100(self):
        assert ContextUsage(999_999, 200_000, 'm').percent == 100

    def test_zero_used(self):
        assert ContextUsage(0, 200_000, 'm').percent == 0

    def test_zero_window_is_safe(self):
        assert ContextUsage(123, 0, 'm').percent == 0


# --------------------------------------------------------------------------
# context_usage_for_transcript
# --------------------------------------------------------------------------

class TestContextUsageForTranscript:
    def test_sums_prompt_tokens_excluding_output(self, tmp_path):
        t = tmp_path / 'session.jsonl'
        _write_jsonl(t, [
            {'type': 'user', 'message': {'content': 'hi'}},
            _assistant('claude-opus-4-8', inp=50_000, cache_create=10_000,
                       cache_read=40_000, out=12_345),
        ])
        usage = context_usage_for_transcript(str(t))
        assert usage is not None
        # output_tokens (12_345) is deliberately excluded.
        assert usage.used_tokens == 100_000
        assert usage.window == 200_000
        assert usage.percent == 50
        assert usage.model == 'claude-opus-4-8'

    def test_skips_sidechain_subagent_turn(self, tmp_path):
        # The latest entry is a sub-agent (Task) turn with huge usage; it
        # must be skipped so the % reflects the main conversation, not the
        # transient sub-agent.
        t = tmp_path / 'session.jsonl'
        _write_jsonl(t, [
            {'type': 'user', 'message': {'content': 'go'}},
            _assistant('claude-opus-4-8', inp=20_000, cache_create=0,
                       cache_read=30_000),
            _assistant('claude-haiku-4-5', inp=190_000, cache_create=5_000,
                       cache_read=4_000, sidechain=True),
        ])
        usage = context_usage_for_transcript(str(t))
        assert usage is not None
        assert usage.used_tokens == 50_000  # main-chain turn, not the sidechain
        assert usage.model == 'claude-opus-4-8'

    def test_missing_fields_default_to_zero(self, tmp_path):
        t = tmp_path / 'session.jsonl'
        _write_jsonl(t, [
            {'type': 'assistant',
             'message': {'model': 'claude-x', 'usage': {'input_tokens': 7}}},
        ])
        usage = context_usage_for_transcript(str(t))
        assert usage is not None
        assert usage.used_tokens == 7

    def test_empty_path_returns_none(self):
        assert context_usage_for_transcript('') is None

    def test_nonexistent_file_returns_none(self, tmp_path):
        assert context_usage_for_transcript(str(tmp_path / 'nope.jsonl')) is None

    def test_empty_file_returns_none(self, tmp_path):
        t = tmp_path / 'empty.jsonl'
        t.write_text('')
        assert context_usage_for_transcript(str(t)) is None

    def test_no_assistant_entry_returns_none(self, tmp_path):
        t = tmp_path / 'useronly.jsonl'
        _write_jsonl(t, [{'type': 'user', 'message': {'content': 'hi'}}])
        assert context_usage_for_transcript(str(t)) is None

    def test_assistant_without_usage_returns_none(self, tmp_path):
        t = tmp_path / 'nousage.jsonl'
        _write_jsonl(t, [
            {'type': 'assistant', 'message': {'model': 'm', 'content': []}},
        ])
        assert context_usage_for_transcript(str(t)) is None

    def test_corrupt_lines_skipped_to_find_valid(self, tmp_path):
        t = tmp_path / 'mixed.jsonl'
        good = json.dumps(_assistant('claude-x', inp=1_000, cache_create=0,
                                     cache_read=0))
        # A truncated/garbage final line must not break parsing of the prior
        # valid assistant turn.
        t.write_text(good + '\n' + '{ this is not json' + '\n')
        usage = context_usage_for_transcript(str(t))
        assert usage is not None
        assert usage.used_tokens == 1_000

    def test_non_object_json_lines_are_skipped(self, tmp_path):
        # Valid JSON that isn't an object (scalar / array) must be skipped,
        # never raise ``AttributeError`` on ``.get`` -- this runs on the
        # monitor's render thread.
        t = tmp_path / 'scalars.jsonl'
        good = json.dumps(_assistant('claude-x', inp=5_000, cache_create=0,
                                     cache_read=0))
        t.write_text('\n'.join([good, '42', '[1, 2, 3]', '"a string"']) + '\n')
        usage = context_usage_for_transcript(str(t))
        assert usage is not None
        assert usage.used_tokens == 5_000

    def test_assistant_with_non_dict_message_skipped(self, tmp_path):
        t = tmp_path / 'badmsg.jsonl'
        good = json.dumps(_assistant('claude-x', inp=3_000, cache_create=0,
                                     cache_read=0))
        bad = json.dumps({'type': 'assistant', 'message': 'oops not a dict'})
        # The latest entry has a malformed message -> skip it, fall back to
        # the prior valid turn.
        t.write_text('\n'.join([good, bad]) + '\n')
        usage = context_usage_for_transcript(str(t))
        assert usage is not None
        assert usage.used_tokens == 3_000

    def test_non_numeric_token_values_ignored(self, tmp_path):
        t = tmp_path / 'badtokens.jsonl'
        entry = {'type': 'assistant', 'message': {'model': 'claude-x', 'usage': {
            'input_tokens': 'oops',
            'cache_creation_input_tokens': 1_000,
            'cache_read_input_tokens': None,
        }}}
        t.write_text(json.dumps(entry) + '\n')
        usage = context_usage_for_transcript(str(t))
        assert usage is not None
        # Only the one numeric field is counted; the others are ignored.
        assert usage.used_tokens == 1_000


# --------------------------------------------------------------------------
# 1M context-window detection
# --------------------------------------------------------------------------

class TestOneMillionWindow:
    def test_detects_1m_from_project_config(self, tmp_path, fake_claude_config):
        # Claude's config recorded the [1m] variant for this project, so a
        # 150k session should read against the 1M window (15%), not 200k.
        fake_claude_config({
            '/proj': {'lastModelUsage': {'claude-opus-4-8[1m]': {}}},
        })
        t = tmp_path / 's.jsonl'
        _write_jsonl(t, [
            _assistant('claude-opus-4-8', inp=150_000, cache_create=0,
                       cache_read=0, cwd='/proj'),
        ])
        usage = context_usage_for_transcript(str(t))
        assert usage is not None
        assert usage.window == 1_000_000
        assert usage.percent == 15

    def test_no_1m_signal_uses_200k(self, tmp_path, fake_claude_config):
        # Config exists but only records the plain (non-1m) model.
        fake_claude_config({
            '/proj': {'lastModelUsage': {'claude-opus-4-8': {}}},
        })
        t = tmp_path / 's.jsonl'
        _write_jsonl(t, [
            _assistant('claude-opus-4-8', inp=100_000, cache_create=0,
                       cache_read=0, cwd='/proj'),
        ])
        usage = context_usage_for_transcript(str(t))
        assert usage is not None
        assert usage.window == 200_000
        assert usage.percent == 50

    def test_usage_over_200k_forces_1m_without_config(self, tmp_path,
                                                      fake_claude_config):
        # No [1m] signal anywhere, but usage exceeds what a 200k window could
        # hold -> it must be a larger window.
        fake_claude_config({})
        t = tmp_path / 's.jsonl'
        _write_jsonl(t, [
            _assistant('claude-opus-4-8', inp=250_000, cache_create=0,
                       cache_read=0, cwd='/proj'),
        ])
        usage = context_usage_for_transcript(str(t))
        assert usage is not None
        assert usage.window == 1_000_000
        assert usage.percent == 25

    def test_config_match_is_per_cwd(self, tmp_path, fake_claude_config):
        # The [1m] usage is recorded for a DIFFERENT project; this session's
        # cwd has no 1m signal, so it stays on 200k.
        fake_claude_config({
            '/other': {'lastModelUsage': {'claude-opus-4-8[1m]': {}}},
        })
        t = tmp_path / 's.jsonl'
        _write_jsonl(t, [
            _assistant('claude-opus-4-8', inp=100_000, cache_create=0,
                       cache_read=0, cwd='/proj'),
        ])
        usage = context_usage_for_transcript(str(t))
        assert usage is not None
        assert usage.window == 200_000


# --------------------------------------------------------------------------
# stat-keyed cache
# --------------------------------------------------------------------------

class TestCache:
    def test_unchanged_file_is_not_reparsed(self, tmp_path):
        t = tmp_path / 'cached.jsonl'
        _write_jsonl(t, [
            _assistant('claude-x', inp=10_000, cache_create=0, cache_read=0),
        ])
        first = context_usage_for_transcript(str(t))
        second = context_usage_for_transcript(str(t))
        # A cache hit returns the very same object; a re-parse would build a
        # new (equal but distinct) ContextUsage.
        assert first is second

    def test_rewrite_invalidates_cache(self, tmp_path):
        t = tmp_path / 'growing.jsonl'
        _write_jsonl(t, [
            _assistant('claude-x', inp=10_000, cache_create=0, cache_read=0),
        ])
        first = context_usage_for_transcript(str(t))
        assert first is not None and first.used_tokens == 10_000
        # Append a newer assistant turn: size changes -> cache key changes ->
        # re-read picks up the new latest turn.
        _write_jsonl(t, [
            _assistant('claude-x', inp=10_000, cache_create=0, cache_read=0),
            _assistant('claude-x', inp=80_000, cache_create=0, cache_read=0),
        ])
        second = context_usage_for_transcript(str(t))
        assert second is not None and second.used_tokens == 80_000
