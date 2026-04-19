"""Tests for the `leap --resume` feature.

Covers the CLIProvider resume protocol (Claude / Codex / Cursor) and
the shared resume_store read/write layer.  The picker UI itself is
stateful / terminal-dependent so we leave that out of the unit layer.
"""

from __future__ import annotations

import json
import time

import pytest

from leap.cli_providers.registry import get_provider
from leap.utils.resume_store import (
    MAX_ENTRIES_PER_TAG,
    SessionRecord,
    TagRow,
    load_tag_rows,
    record_session,
)


# --------------------------------------------------------------------------
# Provider protocol
# --------------------------------------------------------------------------

class TestClaudeProviderResume:
    def test_supports_resume(self):
        p = get_provider("claude")
        assert p.supports_resume is True

    def test_extract_session_id_from_transcript_path(self):
        p = get_provider("claude")
        sid = p.extract_session_id({
            "transcript_path": "/Users/me/.claude/projects/myproj/abc123-4567.jsonl",
        })
        assert sid == "abc123-4567"

    def test_extract_session_id_ignores_non_claude_path(self):
        p = get_provider("claude")
        sid = p.extract_session_id({
            "transcript_path": "/Users/me/.codex/sessions/2026/uuid.jsonl",
        })
        assert sid is None

    def test_extract_session_id_empty_payload(self):
        p = get_provider("claude")
        assert p.extract_session_id({}) is None

    def test_resume_args_uses_equals_form(self):
        # Single-token `--resume=<id>` is required — space form would be
        # dropped by an older leap-server flag filter (see claude.py note).
        p = get_provider("claude")
        assert p.resume_args("abc") == ["--resume=abc"]


class TestCodexProviderResume:
    def test_supports_resume(self):
        p = get_provider("codex")
        assert p.supports_resume is True

    def test_extract_session_id_from_direct_field(self):
        p = get_provider("codex")
        sid = p.extract_session_id({"session_id": "019d-codex-uuid"})
        assert sid == "019d-codex-uuid"

    def test_extract_session_id_from_transcript_fallback(self, tmp_path):
        p = get_provider("codex")
        transcript = tmp_path / ".codex/sessions/2026/rollout.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": "codex-fallback-uuid"},
        }) + "\n")
        sid = p.extract_session_id({
            "transcript_path": str(transcript),
        })
        assert sid == "codex-fallback-uuid"

    def test_extract_session_id_ignores_non_codex_path(self, tmp_path):
        p = get_provider("codex")
        transcript = tmp_path / "elsewhere.jsonl"
        transcript.write_text("{}")
        sid = p.extract_session_id({"transcript_path": str(transcript)})
        assert sid is None

    def test_resume_args_is_subcommand_form(self):
        # Codex resume is a positional subcommand — must be prepended
        # to the argv list so it stays in front of any user flags.
        p = get_provider("codex")
        assert p.resume_args("abc") == ["resume", "abc"]


class TestCursorAgentProviderResume:
    def test_supports_resume(self):
        p = get_provider("cursor-agent")
        assert p.supports_resume is True

    def test_extract_session_id_from_conversation_id(self):
        # Cursor's official stop-hook payload uses `conversation_id`.
        p = get_provider("cursor-agent")
        sid = p.extract_session_id({"conversation_id": "cursor-conv-uuid"})
        assert sid == "cursor-conv-uuid"

    def test_extract_session_id_from_chatid_fallback(self):
        p = get_provider("cursor-agent")
        sid = p.extract_session_id({"chatId": "cursor-chat-uuid"})
        assert sid == "cursor-chat-uuid"

    def test_extract_session_id_from_transcript_path(self):
        p = get_provider("cursor-agent")
        sid = p.extract_session_id({
            "transcript_path": "/Users/me/.cursor/chats/projhash/abc12345/msg.jsonl",
        })
        assert sid == "abc12345"

    def test_resume_args_is_space_form(self):
        p = get_provider("cursor-agent")
        assert p.resume_args("abc") == ["--resume", "abc"]


class TestGeminiProviderResume:
    def test_supports_resume(self):
        p = get_provider("gemini")
        assert p.supports_resume is True

    def test_extract_session_id_from_direct_field(self):
        p = get_provider("gemini")
        assert p.extract_session_id({"sessionId": "gemini-uuid"}) == "gemini-uuid"
        assert p.extract_session_id({"session_id": "alt-uuid"}) == "alt-uuid"

    def test_extract_session_id_from_session_file(self, tmp_path):
        # Gemini writes the full UUID as a top-level `sessionId` field
        # in the per-session JSON file.
        p = get_provider("gemini")
        session_file = tmp_path / ".gemini/tmp/proj/chats/session-2026-04-19T16-09-5ec95d33.json"
        session_file.parent.mkdir(parents=True)
        session_file.write_text(json.dumps({
            "sessionId": "5ec95d33-d405-4b82-9be5-8080824363b5",
            "projectHash": "abc",
            "messages": [],
        }))
        sid = p.extract_session_id({"transcript_path": str(session_file)})
        assert sid == "5ec95d33-d405-4b82-9be5-8080824363b5"

    def test_extract_session_id_works_on_large_session_file(self, tmp_path):
        # Regression: regex-based head scan must still find sessionId on
        # session files too large for ``json.loads`` on a bounded read.
        # Gemini sessions grow unbounded with the history; a busy session
        # can easily exceed 4 KiB.  The sessionId is always near the top
        # of the file in Gemini's serialisation, so this works.
        p = get_provider("gemini")
        session_file = tmp_path / ".gemini/tmp/proj/chats/session-2026-04-19T16-09-5ec95d33.json"
        session_file.parent.mkdir(parents=True)
        session_file.write_text(json.dumps({
            "sessionId": "5ec95d33-d405-4b82-9be5-8080824363b5",
            "projectHash": "abc",
            "messages": [
                {"id": f"{i:03d}", "content": "x" * 200} for i in range(50)
            ],
        }, indent=2))
        assert session_file.stat().st_size > 4096
        sid = p.extract_session_id({"transcript_path": str(session_file)})
        assert sid == "5ec95d33-d405-4b82-9be5-8080824363b5"

    def test_extract_session_id_ignores_non_gemini_path(self, tmp_path):
        p = get_provider("gemini")
        f = tmp_path / "unrelated.json"
        f.write_text(json.dumps({"sessionId": "nope"}))
        assert p.extract_session_id({"transcript_path": str(f)}) is None

    def test_resume_args_is_space_form(self):
        p = get_provider("gemini")
        assert p.resume_args("abc") == ["--resume", "abc"]


# --------------------------------------------------------------------------
# get_spawn_env exports LEAP_CLI_PROVIDER for every provider
# --------------------------------------------------------------------------

class TestSpawnEnv:
    @pytest.mark.parametrize("name", ["claude", "codex", "cursor-agent", "gemini"])
    def test_exports_cli_provider_name(self, name, tmp_path):
        p = get_provider(name)
        env = p.get_spawn_env(tag="some-tag", signal_dir=tmp_path)
        assert env.get("LEAP_CLI_PROVIDER") == name
        assert env.get("LEAP_TAG") == "some-tag"
        assert env.get("LEAP_SIGNAL_DIR") == str(tmp_path)


# --------------------------------------------------------------------------
# resume_store.record_session / load_tag_rows
# --------------------------------------------------------------------------

@pytest.fixture
def live_transcript(tmp_path):
    """Yield a factory that creates a transcript file of the given size."""

    def make(name: str = "transcript.jsonl", size: int = 100) -> str:
        f = tmp_path / name
        f.write_text("x" * size)
        return str(f)

    return make


class TestResumeStore:
    def test_record_and_load_single_session(self, tmp_path, live_transcript):
        tp = live_transcript()
        record_session(tmp_path, "claude", "mytag",
                       session_id="sid-1", transcript_path=tp, cwd="/home/me")
        rows = load_tag_rows(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, TagRow)
        assert row.tag == "mytag" and row.cli == "claude"
        assert len(row.sessions) == 1
        s = row.sessions[0]
        assert isinstance(s, SessionRecord)
        assert s.session_id == "sid-1"
        assert s.size == 100
        assert s.cwd == "/home/me"
        assert s.last_seen > 0

    def test_dedup_by_session_id(self, tmp_path, live_transcript):
        tp = live_transcript()
        record_session(tmp_path, "claude", "t", session_id="x", transcript_path=tp)
        time.sleep(0.01)
        record_session(tmp_path, "claude", "t", session_id="x", transcript_path=tp)
        rows = load_tag_rows(tmp_path)
        assert len(rows[0].sessions) == 1, "repeated record should bump, not append"

    def test_cap_keeps_newest(self, tmp_path, live_transcript):
        tp = live_transcript()
        for i in range(MAX_ENTRIES_PER_TAG + 5):
            record_session(tmp_path, "codex", "cap",
                           session_id=f"id-{i}", transcript_path=tp)
        rows = load_tag_rows(tmp_path)
        sessions = rows[0].sessions
        assert len(sessions) == MAX_ENTRIES_PER_TAG
        ids = {s.session_id for s in sessions}
        assert "id-0" not in ids, "oldest should be trimmed"
        assert f"id-{MAX_ENTRIES_PER_TAG + 4}" in ids, "newest should be kept"

    def test_stale_transcript_dropped(self, tmp_path, live_transcript):
        tp = live_transcript()
        record_session(tmp_path, "claude", "stale",
                       session_id="x", transcript_path=tp)
        # Delete the transcript — the picker should filter this row out.
        import os
        os.unlink(tp)
        rows = load_tag_rows(tmp_path)
        assert rows == [], "rows whose only session has a missing transcript should be dropped"

    def test_multiple_clis_same_tag_are_distinct_rows(self, tmp_path, live_transcript):
        tp1 = live_transcript("claude.jsonl")
        tp2 = live_transcript("codex.jsonl")
        record_session(tmp_path, "claude", "dup", session_id="a", transcript_path=tp1)
        record_session(tmp_path, "codex", "dup", session_id="b", transcript_path=tp2)
        rows = load_tag_rows(tmp_path)
        assert len(rows) == 2
        clis = {r.cli for r in rows}
        assert clis == {"claude", "codex"}

    def test_sorted_newest_first(self, tmp_path, live_transcript):
        tp = live_transcript()
        record_session(tmp_path, "codex", "old", session_id="o", transcript_path=tp)
        time.sleep(0.01)
        record_session(tmp_path, "claude", "new", session_id="n", transcript_path=tp)
        rows = load_tag_rows(tmp_path)
        assert [r.tag for r in rows] == ["new", "old"]

    def test_empty_storage_returns_empty_list(self, tmp_path):
        assert load_tag_rows(tmp_path) == []

    def test_rejects_bad_inputs_silently(self, tmp_path, live_transcript):
        # Missing required args → no-op, no crash.
        record_session(tmp_path, "", "tag", session_id="x")
        record_session(tmp_path, "claude", "", session_id="x")
        record_session(tmp_path, "claude", "tag", session_id="")
        assert load_tag_rows(tmp_path) == []

    def test_rejects_path_traversal_in_tag(self, tmp_path, live_transcript):
        tp = live_transcript()
        record_session(tmp_path, "claude", "../evil",
                       session_id="x", transcript_path=tp)
        # Nothing should have been written anywhere under tmp_path's parent.
        import os
        for root, _, files in os.walk(tmp_path):
            for f in files:
                assert "evil" not in f, f"path traversal leaked: {os.path.join(root, f)}"
        assert load_tag_rows(tmp_path) == []

    def test_rejects_path_traversal_in_cli(self, tmp_path, live_transcript):
        tp = live_transcript()
        record_session(tmp_path, "../evil", "sometag",
                       session_id="x", transcript_path=tp)
        # No stray files under the storage dir or escaping it
        assert not (tmp_path.parent / "evil").exists()
        assert load_tag_rows(tmp_path) == []

    def test_rejects_weird_tag_characters(self, tmp_path, live_transcript):
        tp = live_transcript()
        for bad in ("foo/bar", "foo bar", "foo\x00", ".leadingdot"):
            record_session(tmp_path, "claude", bad,
                           session_id="x", transcript_path=tp)
        assert load_tag_rows(tmp_path) == []


# --------------------------------------------------------------------------
# extract_last_assistant_message protocol
# --------------------------------------------------------------------------

class TestLastAssistantMessage:
    def test_default_uses_direct_field(self):
        # Codex / Cursor / Gemini all pass the text directly.
        p = get_provider("codex")
        assert p.extract_last_assistant_message({"last_assistant_message": "hi"}) == "hi"

    def test_default_handles_missing_field(self):
        p = get_provider("codex")
        assert p.extract_last_assistant_message({}) == ""

    def test_default_handles_non_string_field(self):
        # Defensive: a misconfigured CLI could send a non-string here;
        # the base impl strips it to ''.
        p = get_provider("codex")
        assert p.extract_last_assistant_message({"last_assistant_message": 42}) == ""

    def test_claude_tails_transcript(self, tmp_path):
        p = get_provider("claude")
        transcript = tmp_path / ".claude/projects/proj/abc.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("\n".join([
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello from claude"}]}}),
            "",
        ]))
        result = p.extract_last_assistant_message({"transcript_path": str(transcript)})
        assert result == "hello from claude"

    def test_claude_returns_empty_when_no_transcript(self):
        p = get_provider("claude")
        assert p.extract_last_assistant_message({}) == ""
