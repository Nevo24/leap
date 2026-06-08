"""Tests for Codex/Gemini ``extract_last_user_prompt``.

These drive the monitor's "Last Msg" column: reading the clean prompt from the
transcript instead of Leap's PTY ``recently_sent`` capture (which can carry
stray echoed keystrokes, e.g. a leading ``2`` -> "2hi").
See ``cli_providers/codex.py`` and ``cli_providers/gemini.py``.
"""

import json

from leap.cli_providers.codex import CodexProvider
from leap.cli_providers.gemini import GeminiProvider
from leap.utils.resume_store import record_session


def _write_jsonl(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _record(storage, cli, tag, transcript):
    record_session(storage, cli, tag, session_id="s1",
                   transcript_path=str(transcript))


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------
def _codex_user(msg):
    return {"type": "event_msg", "payload": {"type": "user_message", "message": msg}}


class TestCodexLastPrompt:
    def test_returns_latest_clean_user_message(self, tmp_path):
        roll = tmp_path / "rollout.jsonl"
        _write_jsonl(roll, [
            {"type": "response_item", "payload": {"role": "user", "content": [
                {"type": "input_text", "text": "<environment_context>\n  <cwd>/x</cwd>"}]}},
            _codex_user("first question"),
            _codex_user("hi"),  # latest
        ])
        _record(tmp_path, "codex", "t", roll)
        assert CodexProvider().extract_last_user_prompt("", "t", tmp_path) == "hi"

    def test_skips_environment_context_user_message(self, tmp_path):
        roll = tmp_path / "rollout.jsonl"
        _write_jsonl(roll, [
            _codex_user("hi"),
            # an injected env-context block must not become the "last prompt"
            _codex_user("<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"),
        ])
        _record(tmp_path, "codex", "t", roll)
        assert CodexProvider().extract_last_user_prompt("", "t", tmp_path) == "hi"

    def test_no_record_returns_empty(self, tmp_path):
        assert CodexProvider().extract_last_user_prompt("", "missing", tmp_path) == ""

    def test_no_storage_returns_empty(self):
        assert CodexProvider().extract_last_user_prompt("", "t", None) == ""

    def test_cli_name_resolves_custom_subdir(self, tmp_path):
        # A custom CLI built atop Codex records under its own cli_sessions
        # subdir; passing cli_name resolves it (self.name='codex' would miss).
        roll = tmp_path / "rollout.jsonl"
        _write_jsonl(roll, [_codex_user("custom hi")])
        record_session(tmp_path, "my-codex", "t", session_id="s1",
                       transcript_path=str(roll))
        c = CodexProvider()
        assert c.extract_last_user_prompt("", "t", tmp_path, cli_name="my-codex") == "custom hi"
        # Without cli_name it looks in cli_sessions/codex/ and finds nothing.
        assert c.extract_last_user_prompt("", "t", tmp_path) == ""

    def test_corrupt_lines_skipped(self, tmp_path):
        roll = tmp_path / "rollout.jsonl"
        roll.write_text("not json\n" + json.dumps(_codex_user("hi")) + "\n{bad\n")
        _record(tmp_path, "codex", "t", roll)
        assert CodexProvider().extract_last_user_prompt("", "t", tmp_path) == "hi"

    def test_cached_then_invalidated_on_change(self, tmp_path):
        # Per-refresh calls must not re-parse an unchanged rollout; a changed
        # file (new mtime/size) must invalidate and re-read.
        import leap.cli_providers.codex as cx
        cx._LAST_PROMPT_CACHE.clear()
        roll = tmp_path / "rollout.jsonl"
        _write_jsonl(roll, [_codex_user("hi")])
        _record(tmp_path, "codex", "t", roll)
        p = CodexProvider()
        assert p.extract_last_user_prompt("", "t", tmp_path) == "hi"
        assert len(cx._LAST_PROMPT_CACHE) == 1
        assert p.extract_last_user_prompt("", "t", tmp_path) == "hi"  # cache hit
        assert len(cx._LAST_PROMPT_CACHE) == 1  # bounded to one entry per path
        _write_jsonl(roll, [_codex_user("hi"), _codex_user("newer")])
        assert p.extract_last_user_prompt("", "t", tmp_path) == "newer"
        assert len(cx._LAST_PROMPT_CACHE) == 1


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
def _gem_user(text):
    return {"type": "user", "content": [{"text": text}]}


class TestGeminiLastPrompt:
    def test_returns_latest_user_text(self, tmp_path):
        sess = tmp_path / "session.jsonl"
        _write_jsonl(sess, [
            {"type": "info", "content": "x"},
            _gem_user("hello"),
            {"type": "gemini", "model": "gemini-3-flash-preview", "tokens": {}},
            _gem_user("do the thing"),  # latest
        ])
        _record(tmp_path, "gemini", "t", sess)
        assert GeminiProvider().extract_last_user_prompt("", "t", tmp_path) == "do the thing"

    def test_joins_multipart_content(self, tmp_path):
        sess = tmp_path / "session.jsonl"
        _write_jsonl(sess, [
            {"type": "user", "content": [{"text": "part one "}, {"text": "part two"}]},
        ])
        _record(tmp_path, "gemini", "t", sess)
        assert GeminiProvider().extract_last_user_prompt("", "t", tmp_path) == "part one part two"

    def test_no_record_returns_empty(self, tmp_path):
        assert GeminiProvider().extract_last_user_prompt("", "missing", tmp_path) == ""

    def test_no_storage_returns_empty(self):
        assert GeminiProvider().extract_last_user_prompt("", "t", None) == ""
