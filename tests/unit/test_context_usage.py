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
from leap.cli_providers.registry import get_provider
from leap.utils.context_usage import (
    ContextUsage,
    claude_context_usage,
    claude_statusline_context_usage,
    codex_context_usage,
    context_window_for_model,
    gemini_context_usage,
    statusline_context_usage,
)
from leap.utils.resume_store import record_session


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
# claude_context_usage (Claude transcript parsing)
# --------------------------------------------------------------------------

class TestContextUsageForTranscript:
    def test_sums_prompt_tokens_excluding_output(self, tmp_path):
        t = tmp_path / 'session.jsonl'
        _write_jsonl(t, [
            {'type': 'user', 'message': {'content': 'hi'}},
            _assistant('claude-opus-4-8', inp=50_000, cache_create=10_000,
                       cache_read=40_000, out=12_345),
        ])
        usage = claude_context_usage(str(t))
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
        usage = claude_context_usage(str(t))
        assert usage is not None
        assert usage.used_tokens == 50_000  # main-chain turn, not the sidechain
        assert usage.model == 'claude-opus-4-8'

    def test_missing_fields_default_to_zero(self, tmp_path):
        t = tmp_path / 'session.jsonl'
        _write_jsonl(t, [
            {'type': 'assistant',
             'message': {'model': 'claude-x', 'usage': {'input_tokens': 7}}},
        ])
        usage = claude_context_usage(str(t))
        assert usage is not None
        assert usage.used_tokens == 7

    def test_empty_path_returns_none(self):
        assert claude_context_usage('') is None

    def test_nonexistent_file_returns_none(self, tmp_path):
        assert claude_context_usage(str(tmp_path / 'nope.jsonl')) is None

    def test_empty_file_returns_none(self, tmp_path):
        t = tmp_path / 'empty.jsonl'
        t.write_text('')
        assert claude_context_usage(str(t)) is None

    def test_no_assistant_entry_returns_none(self, tmp_path):
        t = tmp_path / 'useronly.jsonl'
        _write_jsonl(t, [{'type': 'user', 'message': {'content': 'hi'}}])
        assert claude_context_usage(str(t)) is None

    def test_assistant_without_usage_returns_none(self, tmp_path):
        t = tmp_path / 'nousage.jsonl'
        _write_jsonl(t, [
            {'type': 'assistant', 'message': {'model': 'm', 'content': []}},
        ])
        assert claude_context_usage(str(t)) is None

    def test_corrupt_lines_skipped_to_find_valid(self, tmp_path):
        t = tmp_path / 'mixed.jsonl'
        good = json.dumps(_assistant('claude-x', inp=1_000, cache_create=0,
                                     cache_read=0))
        # A truncated/garbage final line must not break parsing of the prior
        # valid assistant turn.
        t.write_text(good + '\n' + '{ this is not json' + '\n')
        usage = claude_context_usage(str(t))
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
        usage = claude_context_usage(str(t))
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
        usage = claude_context_usage(str(t))
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
        usage = claude_context_usage(str(t))
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
        usage = claude_context_usage(str(t))
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
        usage = claude_context_usage(str(t))
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
        usage = claude_context_usage(str(t))
        assert usage is not None
        assert usage.window == 1_000_000
        assert usage.percent == 25

    def test_global_fallback_when_project_has_no_record(
        self, tmp_path, fake_claude_config,
    ):
        # 1M is account-wide: when THIS project has no usage record, but the
        # model is run on [1m] in ANY project, treat it as 1M.  (This is what
        # was over-reporting before - a fresh session, or one whose project
        # record Claude momentarily blanked, falling back to 200k.)
        fake_claude_config({
            '/other': {'lastModelUsage': {'claude-opus-4-8[1m]': {}}},
            # '/proj' deliberately absent -> no record for this session's cwd.
        })
        t = tmp_path / 's.jsonl'
        _write_jsonl(t, [
            _assistant('claude-opus-4-8', inp=100_000, cache_create=0,
                       cache_read=0, cwd='/proj'),
        ])
        usage = claude_context_usage(str(t))
        assert usage is not None
        assert usage.window == 1_000_000
        assert usage.percent == 10

    def test_explicit_non_1m_project_overrides_global(
        self, tmp_path, fake_claude_config,
    ):
        # A project with its OWN explicit, non-[1m] record is a genuine 200k
        # project - its precise record wins over the account-wide [1m] signal.
        fake_claude_config({
            '/proj': {'lastModelUsage': {'claude-opus-4-8': {}}},
            '/other': {'lastModelUsage': {'claude-opus-4-8[1m]': {}}},
        })
        t = tmp_path / 's.jsonl'
        _write_jsonl(t, [
            _assistant('claude-opus-4-8', inp=100_000, cache_create=0,
                       cache_read=0, cwd='/proj'),
        ])
        usage = claude_context_usage(str(t))
        assert usage is not None
        assert usage.window == 200_000

    def test_cwd_record_for_other_model_falls_back_to_global(
        self, tmp_path, fake_claude_config,
    ):
        # This cwd has a record, but only for a DIFFERENT model (a prior sonnet
        # session). That says nothing about opus-4-8's window, so we must fall
        # back to the account-wide [1m] signal rather than concluding 200k.
        fake_claude_config({
            '/proj': {'lastModelUsage': {'claude-sonnet-4-6': {}}},
            '/other': {'lastModelUsage': {'claude-opus-4-8[1m]': {}}},
        })
        t = tmp_path / 's.jsonl'
        _write_jsonl(t, [
            _assistant('claude-opus-4-8', inp=97_218, cache_create=0,
                       cache_read=0, cwd='/proj'),
        ])
        usage = claude_context_usage(str(t))
        assert usage is not None
        assert usage.window == 1_000_000  # not 200_000

    def test_global_fallback_is_version_specific(
        self, tmp_path, fake_claude_config,
    ):
        # Only the matching base model counts: an opus-4-8 session is NOT 1M
        # just because opus-4-7[1m] is used elsewhere.
        fake_claude_config({
            '/other': {'lastModelUsage': {'claude-opus-4-7[1m]': {}}},
        })
        t = tmp_path / 's.jsonl'
        _write_jsonl(t, [
            _assistant('claude-opus-4-8', inp=100_000, cache_create=0,
                       cache_read=0, cwd='/proj'),
        ])
        usage = claude_context_usage(str(t))
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
        first = claude_context_usage(str(t))
        second = claude_context_usage(str(t))
        # A cache hit returns the very same object; a re-parse would build a
        # new (equal but distinct) ContextUsage.
        assert first is second

    def test_rewrite_invalidates_cache(self, tmp_path):
        t = tmp_path / 'growing.jsonl'
        _write_jsonl(t, [
            _assistant('claude-x', inp=10_000, cache_create=0, cache_read=0),
        ])
        first = claude_context_usage(str(t))
        assert first is not None and first.used_tokens == 10_000
        # Append a newer assistant turn: size changes -> cache key changes ->
        # re-read picks up the new latest turn.
        _write_jsonl(t, [
            _assistant('claude-x', inp=10_000, cache_create=0, cache_read=0),
            _assistant('claude-x', inp=80_000, cache_create=0, cache_read=0),
        ])
        second = claude_context_usage(str(t))
        assert second is not None and second.used_tokens == 80_000


# --------------------------------------------------------------------------
# Codex: token_count rollout events (window is in the data)
# --------------------------------------------------------------------------

def _codex_token_count(input_tokens: int, window, model: str = 'gpt-5.5') -> list[dict]:
    """A minimal Codex turn: turn_context (model) then a token_count event."""
    info: dict = {
        'last_token_usage': {
            'input_tokens': input_tokens,
            'cached_input_tokens': input_tokens // 2,
            'output_tokens': 5,
            'total_tokens': input_tokens + 5,
        },
        'total_token_usage': {'input_tokens': input_tokens * 3, 'total_tokens': input_tokens * 3},
    }
    if window is not None:
        info['model_context_window'] = window
    return [
        {'type': 'turn_context', 'payload': {'model': model, 'cwd': '/x'}},
        {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': info}},
    ]


class TestCodexContextUsage:
    def test_uses_last_input_and_window_from_data(self, tmp_path):
        t = tmp_path / 'rollout.jsonl'
        _write_jsonl(t, [
            {'type': 'session_meta', 'payload': {'id': 'abc'}},
            *_codex_token_count(12_920, window=258_400, model='gpt-5.5'),
        ])
        u = codex_context_usage(str(t))
        assert u is not None
        assert u.used_tokens == 12_920          # last_token_usage.input_tokens
        assert u.window == 258_400              # info.model_context_window
        assert u.model == 'gpt-5.5'
        assert u.percent == 5

    def test_latest_token_count_wins(self, tmp_path):
        t = tmp_path / 'rollout.jsonl'
        _write_jsonl(t, [
            *_codex_token_count(5_000, window=200_000),
            *_codex_token_count(50_000, window=200_000),  # newer turn
        ])
        u = codex_context_usage(str(t))
        assert u is not None and u.used_tokens == 50_000 and u.percent == 25

    def test_skips_empty_trailing_token_count(self, tmp_path):
        # A real turn followed by an empty token_count (no last_token_usage):
        # context must skip the empty one and use the real turn's usage, not
        # blank the cell.
        t = tmp_path / 'rollout.jsonl'
        _write_jsonl(t, [
            *_codex_token_count(12_920, window=258_400),
            {'type': 'event_msg', 'payload': {
                'type': 'token_count',
                'info': {'model_context_window': 258_400}}},  # empty
        ])
        u = codex_context_usage(str(t))
        assert u is not None
        assert u.used_tokens == 12_920 and u.percent == 5

    def test_missing_context_window_falls_back_to_default(self, tmp_path):
        t = tmp_path / 'rollout.jsonl'
        _write_jsonl(t, _codex_token_count(2_560, window=None))
        u = codex_context_usage(str(t))
        assert u is not None and u.window == 256_000 and u.percent == 1

    def test_no_token_count_event_returns_none(self, tmp_path):
        t = tmp_path / 'rollout.jsonl'
        _write_jsonl(t, [
            {'type': 'session_meta', 'payload': {'id': 'abc'}},
            {'type': 'response_item', 'payload': {'role': 'assistant'}},
        ])
        assert codex_context_usage(str(t)) is None

    def test_model_defaults_when_no_turn_context(self, tmp_path):
        t = tmp_path / 'rollout.jsonl'
        _write_jsonl(t, [
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'last_token_usage': {'input_tokens': 1_000},
                'model_context_window': 200_000,
            }}},
        ])
        u = codex_context_usage(str(t))
        assert u is not None and u.model == 'codex' and u.used_tokens == 1_000


# --------------------------------------------------------------------------
# Gemini: chat-session token usage
# --------------------------------------------------------------------------

def _gemini_turn(input_tokens: int, model: str = 'gemini-3-flash-preview') -> dict:
    return {
        'type': 'gemini',
        'content': 'hi',
        'tokens': {'input': input_tokens, 'output': 10, 'cached': 0,
                   'thoughts': 0, 'tool': 0, 'total': input_tokens + 10},
        'model': model,
    }


class TestGeminiContextUsage:
    def test_uses_input_tokens_and_default_1m_window(self, tmp_path):
        t = tmp_path / 'session.jsonl'
        _write_jsonl(t, [
            {'type': 'user', 'content': 'hello'},
            _gemini_turn(104_857, model='gemini-3-flash-preview'),
        ])
        u = gemini_context_usage(str(t))
        assert u is not None
        assert u.used_tokens == 104_857
        assert u.window == 1_048_576
        assert u.model == 'gemini-3-flash-preview'
        assert u.percent == 10

    def test_ignores_non_gemini_entries(self, tmp_path):
        t = tmp_path / 'session.jsonl'
        _write_jsonl(t, [
            _gemini_turn(50_000),
            {'type': 'user', 'content': 'next question'},  # latest, but a user turn
        ])
        u = gemini_context_usage(str(t))
        assert u is not None and u.used_tokens == 50_000

    def test_no_token_info_returns_none(self, tmp_path):
        t = tmp_path / 'session.jsonl'
        _write_jsonl(t, [{'type': 'gemini', 'content': 'no tokens here'}])
        assert gemini_context_usage(str(t)) is None


# --------------------------------------------------------------------------
# Copilot: status-line state file reader
# --------------------------------------------------------------------------

class TestStatuslineReader:
    def test_reads_state_file(self, tmp_path):
        f = tmp_path / 'tag.context'
        f.write_text(json.dumps({'used_tokens': 50_000, 'window': 200_000,
                                 'model': 'gpt-5.5'}))
        u = statusline_context_usage(str(f))
        assert u is not None and u.used_tokens == 50_000 and u.percent == 25

    def test_missing_file_returns_none(self, tmp_path):
        assert statusline_context_usage(str(tmp_path / 'nope.context')) is None

    def test_empty_path_returns_none(self):
        assert statusline_context_usage('') is None

    def test_corrupt_json_returns_none(self, tmp_path):
        f = tmp_path / 'bad.context'
        f.write_text('{ not json')
        assert statusline_context_usage(str(f)) is None

    def test_no_window_returns_none(self, tmp_path):
        f = tmp_path / 'now.context'
        f.write_text(json.dumps({'used_tokens': 100}))  # no window
        assert statusline_context_usage(str(f)) is None

    def test_raw_reader_keeps_impossible_window(self, tmp_path):
        # The raw reader (Copilot's path) reports the file verbatim; only the
        # Claude wrapper applies the impossible-window safety net.
        f = tmp_path / 'tag.context'
        f.write_text(json.dumps({'used_tokens': 338_631, 'window': 200_000,
                                 'model': 'gpt-5.5'}))
        u = statusline_context_usage(str(f))
        assert u is not None and u.window == 200_000 and u.percent == 100


# --------------------------------------------------------------------------
# Claude: status-line reader + impossible-window safety net
# --------------------------------------------------------------------------

class TestClaudeStatuslineSafetyNet:
    def test_used_over_window_heals_to_one_m(self, tmp_path):
        # Claude's payload can misreport the 200K base window for a session
        # actually running 1M; live context above the recorded window proves
        # the window wrong.
        f = tmp_path / 'tag.context'
        f.write_text(json.dumps({'used_tokens': 338_631, 'window': 200_000,
                                 'model': 'claude-opus-4-8'}))
        u = claude_statusline_context_usage(str(f))
        assert u is not None
        assert u.window == 1_000_000
        assert u.used_tokens == 338_631
        assert u.model == 'claude-opus-4-8'
        assert u.percent == 34

    def test_used_within_window_unchanged(self, tmp_path):
        f = tmp_path / 'tag.context'
        f.write_text(json.dumps({'used_tokens': 50_000, 'window': 200_000,
                                 'model': 'claude-opus-4-8'}))
        u = claude_statusline_context_usage(str(f))
        assert u is not None and u.window == 200_000 and u.percent == 25

    def test_used_equal_to_window_unchanged(self, tmp_path):
        f = tmp_path / 'tag.context'
        f.write_text(json.dumps({'used_tokens': 200_000, 'window': 200_000,
                                 'model': 'claude-opus-4-8'}))
        u = claude_statusline_context_usage(str(f))
        assert u is not None and u.window == 200_000 and u.percent == 100

    def test_one_m_window_never_shrunk(self, tmp_path):
        # used > window at >= 1M is equally impossible but there is no larger
        # Claude window to heal to - leave the record alone.
        f = tmp_path / 'tag.context'
        f.write_text(json.dumps({'used_tokens': 1_200_000, 'window': 1_000_000,
                                 'model': 'claude-opus-4-8[1m]'}))
        u = claude_statusline_context_usage(str(f))
        assert u is not None and u.window == 1_000_000

    def test_missing_file_returns_none(self, tmp_path):
        assert claude_statusline_context_usage(
            str(tmp_path / 'nope.context')) is None

    def test_cached_raw_result_not_mutated(self, tmp_path):
        # statusline_context_usage caches its result by (mtime, size); the
        # healed value must be a fresh object, not an in-place edit of the
        # cached one Copilot-style readers would also see.
        f = tmp_path / 'tag.context'
        f.write_text(json.dumps({'used_tokens': 338_631, 'window': 200_000,
                                 'model': 'claude-opus-4-8'}))
        healed = claude_statusline_context_usage(str(f))
        raw = statusline_context_usage(str(f))
        assert healed is not None and healed.window == 1_000_000
        assert raw is not None and raw.window == 200_000


# --------------------------------------------------------------------------
# Provider integration: supports_context_usage + context_usage(cli, tag, dir)
# --------------------------------------------------------------------------

class TestProviderContextUsage:
    def test_supports_flag_per_provider(self):
        # Cursor genuinely can't report usage -> N/A; the rest can.
        assert get_provider('cursor-agent').supports_context_usage is False
        for name in ('claude', 'codex', 'gemini', 'copilot'):
            assert get_provider(name).supports_context_usage is True

    def test_copilot_reads_statusline_state_file(self, tmp_path):
        (tmp_path / 'sockets').mkdir()
        (tmp_path / 'sockets' / 'tg.context').write_text(json.dumps(
            {'used_tokens': 120_000, 'window': 200_000, 'model': 'GPT-5.5'}))
        u = get_provider('copilot').context_usage('copilot', 'tg', tmp_path)
        assert u is not None
        assert u.used_tokens == 120_000 and u.window == 200_000
        assert u.percent == 60 and u.model == 'GPT-5.5'

    def test_copilot_missing_state_file_returns_none(self, tmp_path):
        (tmp_path / 'sockets').mkdir()
        assert get_provider('copilot').context_usage('copilot', 'nope', tmp_path) is None

    def test_claude_prefers_statusline_file_over_transcript(self, tmp_path):
        # When a .context file exists it is used and the transcript is ignored.
        (tmp_path / 'sockets').mkdir()
        (tmp_path / 'sockets' / 'tg.context').write_text(json.dumps(
            {'used_tokens': 135_000, 'window': 1_000_000, 'model': 'claude-opus-4-8'}))
        # Also record a transcript with a different token count.
        transcript = tmp_path / 't.jsonl'
        _write_jsonl(transcript, [
            _assistant('claude-opus-4-8', inp=10_000, cache_create=0, cache_read=0),
        ])
        record_session(tmp_path, 'claude', 'tg',
                       session_id='s', transcript_path=str(transcript))
        u = get_provider('claude').context_usage('claude', 'tg', tmp_path)
        assert u is not None
        assert u.used_tokens == 135_000   # from the file, not 10_000 from transcript
        assert u.window == 1_000_000      # authoritative 1M from status line

    def test_claude_falls_back_to_transcript_when_no_context_file(self, tmp_path):
        # No .context file -> transcript heuristic is used.
        (tmp_path / 'sockets').mkdir()  # dir exists but no .context file
        transcript = tmp_path / 't.jsonl'
        _write_jsonl(transcript, [
            _assistant('claude-opus-4-8', inp=50_000, cache_create=0, cache_read=0),
        ])
        record_session(tmp_path, 'claude', 'tg',
                       session_id='s', transcript_path=str(transcript))
        u = get_provider('claude').context_usage('claude', 'tg', tmp_path)
        assert u is not None
        assert u.used_tokens == 50_000   # from transcript

    def test_claude_provider_resolves_recorded_transcript(self, tmp_path):
        # Record a claude session pointing at a synthetic transcript, then ask
        # the provider for usage by (cli_name, tag, storage_dir).
        transcript = tmp_path / 't.jsonl'
        _write_jsonl(transcript, [
            _assistant('claude-x', inp=100_000, cache_create=0, cache_read=0),
        ])
        record_session(tmp_path, 'claude', 'mytag',
                       session_id='s', transcript_path=str(transcript))
        u = get_provider('claude').context_usage('claude', 'mytag', tmp_path)
        assert u is not None
        assert u.used_tokens == 100_000

    def test_provider_returns_none_when_no_record(self, tmp_path):
        # No recorded session -> None (the monitor renders this blank, since
        # the provider DOES support usage; it's just not available yet).
        assert get_provider('claude').context_usage('claude', 'none', tmp_path) is None

    def test_custom_cli_name_reads_its_own_subdir(self, tmp_path):
        # A custom claude-based CLI records under its own name; passing that
        # name resolves the right subdir even though the parser is Claude's.
        transcript = tmp_path / 't.jsonl'
        _write_jsonl(transcript, [
            _assistant('claude-x', inp=60_000, cache_create=0, cache_read=0),
        ])
        record_session(tmp_path, 'myclaude', 'tg',
                       session_id='s', transcript_path=str(transcript))
        u = get_provider('claude').context_usage('myclaude', 'tg', tmp_path)
        assert u is not None and u.used_tokens == 60_000
