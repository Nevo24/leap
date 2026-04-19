"""Tests for the `leap --resume` feature.

Covers the CLIProvider resume protocol (Claude / Codex / Cursor) and
the shared resume_store read/write layer.  The picker UI itself is
stateful / terminal-dependent so we leave that out of the unit layer.
"""

from __future__ import annotations

import json
import time

import pytest

from leap.cli_providers.registry import (
    CustomCLIProvider,
    get_provider,
)
from leap.utils.resume_store import (
    MAX_ENTRIES_PER_TAG,
    SessionRecord,
    TagRow,
    load_tag_rows,
    prune_stale,
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
# Custom CLI names: wrapping a base provider for resume
# --------------------------------------------------------------------------

class TestLiveSessionOwners:
    """Verify ``leap-resume.py``'s decision logic for in-use CLI sessions.

    The picker must distinguish three cases:
      1. The picked session_id is currently held by a live Leap server →
         block and tell the user which tag to attach to.
      2. The picked tag's server is alive but holding a *different*
         session → prompt for a new tag to spawn under.
      3. Nothing is in the way → normal resume.
    """

    @pytest.fixture
    def picker(self, tmp_path):
        """Load the picker module with SOCKET_DIR / STORAGE_DIR pointed at
        tmp_path and ``_live_tag_cli_map`` replaced by a configurable stub.

        Tests set ``picker._live_clis`` to a ``{tag: cli}`` dict to
        control which live servers exist and what CLI they're running —
        this mirrors the real ``<tag>.meta`` file's authoritative role.
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'leap_resume', 'src/scripts/leap-resume.py',
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.STORAGE_DIR = tmp_path
        mod.SOCKET_DIR = tmp_path / "sockets"
        mod.SOCKET_DIR.mkdir(exist_ok=True)
        mod._live_clis = {}
        mod._live_tag_cli_map = lambda: dict(mod._live_clis)
        mod._server_alive = lambda tag: tag in mod._live_clis
        return mod

    def test_live_owner_maps_session_to_tag(self, tmp_path, picker):
        record_session(tmp_path, "claude", "livetag",
                       session_id="sess-A", transcript_path="")
        picker._live_clis = {"livetag": "claude"}
        owners = picker._live_session_owners(load_tag_rows(tmp_path))
        assert owners == {"sess-A": [("claude", "livetag")]}

    def test_live_owner_ignores_dead_tags(self, tmp_path, picker):
        record_session(tmp_path, "claude", "ghosttag",
                       session_id="sess-B", transcript_path="")
        # _live_clis stays empty → tag is dead
        owners = picker._live_session_owners(load_tag_rows(tmp_path))
        assert owners == {}

    def test_live_owner_points_at_the_forked_tag(self, tmp_path, picker):
        # Same session UUID recorded under two tags; only the fork is live.
        # Caller should be steered at the live fork.
        record_session(tmp_path, "claude", "orig",
                       session_id="sess-A", transcript_path="")
        record_session(tmp_path, "claude", "fork",
                       session_id="sess-A", transcript_path="")
        picker._live_clis = {"fork": "claude"}
        owners = picker._live_session_owners(load_tag_rows(tmp_path))
        assert owners == {"sess-A": [("claude", "fork")]}

    def test_live_owner_uses_newest_session_per_tag(self, tmp_path, picker):
        # The tag's CURRENT session is the newest entry — picking an
        # older session from the same tag is case 2, not case 1.
        import time
        record_session(tmp_path, "claude", "tagX",
                       session_id="sess-Old", transcript_path="")
        time.sleep(0.01)
        record_session(tmp_path, "claude", "tagX",
                       session_id="sess-New", transcript_path="")
        picker._live_clis = {"tagX": "claude"}
        owners = picker._live_session_owners(load_tag_rows(tmp_path))
        assert "sess-New" in owners
        assert "sess-Old" not in owners

    def test_live_owner_stale_transcript_drops_from_tracking(self, tmp_path, picker):
        # Defence: a row whose newest session has a deleted transcript
        # is filtered by load_tag_rows BEFORE we scan — so even a live
        # server can't produce a bogus "in use" entry for a gone session.
        import os
        t = tmp_path / "scratch.jsonl"
        t.write_text("x")
        record_session(tmp_path, "claude", "stale",
                       session_id="sess-gone", transcript_path=str(t))
        os.unlink(t)
        picker._live_clis = {"stale": "claude"}
        assert picker._live_session_owners(load_tag_rows(tmp_path)) == {}

    def test_live_owner_lists_every_live_fork_of_the_same_session(
        self, tmp_path, picker,
    ):
        # If the user /resume-d the same session into two different tags
        # and both are live now, both should show up in the error
        # message so they can pick whichever is closest.
        record_session(tmp_path, "claude", "fork-a",
                       session_id="shared", transcript_path="")
        record_session(tmp_path, "claude", "fork-b",
                       session_id="shared", transcript_path="")
        picker._live_clis = {"fork-a": "claude", "fork-b": "claude"}
        owners = picker._live_session_owners(load_tag_rows(tmp_path))
        assert len(owners["shared"]) == 2
        owner_tags = {t for _, t in owners["shared"]}
        assert owner_tags == {"fork-a", "fork-b"}

    def test_live_owner_ignores_stale_cli_records_for_same_tag(
        self, tmp_path, picker,
    ):
        # REGRESSION: tag 9 has a Claude record from a previous run PLUS
        # a Gemini record from today.  The live server is running Gemini
        # (per `.meta`).  An old Claude record under the same tag MUST
        # NOT be reported as owner — otherwise the picker sends users
        # to a ``leap 9 (Claude Code)`` that actually runs Gemini.
        record_session(tmp_path, "claude", "9",
                       session_id="old-claude-sess", transcript_path="")
        record_session(tmp_path, "gemini", "9",
                       session_id="current-gemini-sess", transcript_path="")
        picker._live_clis = {"9": "gemini"}
        owners = picker._live_session_owners(load_tag_rows(tmp_path))
        # Gemini session is rightly tracked as in-use …
        assert owners == {"current-gemini-sess": [("gemini", "9")]}
        # … and the stale Claude session is NOT reported as in-use.
        assert "old-claude-sess" not in owners

    def test_live_tag_cli_map_reads_meta_files(self, tmp_path):
        """End-to-end: ``_live_tag_cli_map`` reads the real ``<tag>.meta``
        JSON and combines it with ``_server_alive``.  Mocks out the
        liveness check so we can run without real Unix sockets.
        """
        import importlib.util, json as _json
        spec = importlib.util.spec_from_file_location(
            'leap_resume', 'src/scripts/leap-resume.py',
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sockets = tmp_path / "sockets"
        sockets.mkdir()
        mod.SOCKET_DIR = sockets
        mod._server_alive = lambda t: t in {"live1", "live2"}
        # Create a .sock + .meta for each "live" tag (we mock _server_alive
        # so the files don't need to actually be sockets — glob just
        # needs to find them).
        (sockets / "live1.sock").touch()
        (sockets / "live1.meta").write_text(_json.dumps({"cli_provider": "claude"}))
        (sockets / "live2.sock").touch()
        (sockets / "live2.meta").write_text(_json.dumps({"cli_provider": "gemini"}))
        # And a "dead" tag — socket file exists but _server_alive returns False
        (sockets / "dead.sock").touch()
        (sockets / "dead.meta").write_text(_json.dumps({"cli_provider": "claude"}))
        result = mod._live_tag_cli_map()
        assert result == {"live1": "claude", "live2": "gemini"}

    def test_live_tag_cli_map_survives_missing_or_bad_meta(self, tmp_path):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'leap_resume', 'src/scripts/leap-resume.py',
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sockets = tmp_path / "sockets"
        sockets.mkdir()
        mod.SOCKET_DIR = sockets
        mod._server_alive = lambda t: True  # treat every sock file as live
        (sockets / "no-meta.sock").touch()  # .meta missing entirely
        (sockets / "bad-meta.sock").touch()
        (sockets / "bad-meta.meta").write_text("this is not json")
        (sockets / "no-cli.sock").touch()
        (sockets / "no-cli.meta").write_text('{"tag": "no-cli"}')  # missing cli_provider
        # None of these contribute → empty map, no crash
        assert mod._live_tag_cli_map() == {}


class TestPromptNewTag:
    """Verify ``_prompt_new_tag``'s validation loop.  It should reject
    ill-formed, duplicate-of-old, and already-running tags, re-prompt
    until the user either supplies a valid one or cancels with blank /
    Ctrl+C / EOF.
    """

    @pytest.fixture
    def picker(self, tmp_path):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'leap_resume', 'src/scripts/leap-resume.py',
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod._live_tags = set()
        mod._server_alive = lambda tag: tag in mod._live_tags
        return mod

    def _run(self, picker, inputs, live_tags=frozenset()):
        from unittest.mock import patch
        picker._live_tags = set(live_tags)
        with patch('builtins.input', side_effect=inputs):
            return picker._prompt_new_tag('orig-tag')

    def test_happy_path(self, picker):
        assert self._run(picker, ['newtag']) == 'newtag'

    def test_loops_on_invalid_format(self, picker):
        # Three bad shapes, then good
        result = self._run(picker, ['has space', 'slash/tag', '!', 'valid-tag'])
        assert result == 'valid-tag'

    def test_loops_on_same_as_old(self, picker):
        assert self._run(picker, ['orig-tag', 'newtag']) == 'newtag'

    def test_loops_on_tag_already_live(self, picker):
        assert self._run(picker, ['busy', 'freetag'], live_tags={'busy'}) == 'freetag'

    def test_empty_line_cancels(self, picker):
        assert self._run(picker, ['']) is None

    def test_keyboardinterrupt_cancels(self, picker):
        assert self._run(picker, [KeyboardInterrupt()]) is None

    def test_eof_cancels(self, picker):
        assert self._run(picker, [EOFError()]) is None

    def test_invalid_then_empty_cancels(self, picker):
        assert self._run(picker, ['bad char!', '']) is None

    def test_accepts_underscores_and_digits(self, picker):
        assert self._run(picker, ['A_1-b']) == 'A_1-b'


class TestCustomCliResume:
    """Users can register a custom CLI via `leap --manage-clis` that wraps
    one of the built-in providers with a custom id / display name /
    environment.  Resume must treat the custom CLI as its own distinct
    identity so sessions land under `cli_sessions/<custom-id>/` and the
    picker shows `[<Custom Display Name>]` instead of the base's label.
    """

    def _wrap_claude(self, custom_id="my-claude", display="My Claude"):
        base = get_provider("claude")
        return CustomCLIProvider(
            custom_id=custom_id,
            base_provider=base,
            custom_display_name=display,
            env_vars={"CUSTOM_KEY": "value"},
        )

    def test_name_is_custom_id(self):
        p = self._wrap_claude()
        assert p.name == "my-claude"

    def test_display_name_is_custom(self):
        p = self._wrap_claude()
        assert p.display_name == "My Claude"

    def test_supports_resume_inherits_from_base(self):
        p = self._wrap_claude()
        assert p.supports_resume is True

    def test_resume_args_delegate_to_base(self):
        p = self._wrap_claude()
        assert p.resume_args("abc") == ["--resume=abc"]  # Claude's = form

    def test_extract_session_id_delegates_to_base(self):
        p = self._wrap_claude()
        sid = p.extract_session_id({
            "transcript_path": "/u/.claude/projects/proj/abcdef.jsonl",
        })
        assert sid == "abcdef"

    def test_spawn_env_uses_custom_id_not_base_name(self, tmp_path):
        # Regression: previously ``get_spawn_env`` delegated to the base,
        # which sets LEAP_CLI_PROVIDER=<base.name>.  That made custom CLI
        # hook firings record under the base's cli_sessions/ subdir and
        # hid the custom identity from the picker.
        p = self._wrap_claude()
        env = p.get_spawn_env(tag="t", signal_dir=tmp_path)
        assert env["LEAP_CLI_PROVIDER"] == "my-claude"
        assert env["CUSTOM_KEY"] == "value"
        assert env["LEAP_TAG"] == "t"

    def test_record_session_lands_under_custom_subdir(self, tmp_path, live_transcript):
        # End-to-end: a custom CLI's identity flows all the way through to
        # the on-disk ``cli_sessions/<custom-id>/<tag>.json`` layout.
        tp = live_transcript()
        record_session(tmp_path, "my-claude", "mytag",
                       session_id="sid-x", transcript_path=tp)
        rows = load_tag_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0].cli == "my-claude"
        assert rows[0].tag == "mytag"
        # And physically in the expected path
        assert (tmp_path / "cli_sessions" / "my-claude" / "mytag.json").exists()

    def test_custom_id_with_hyphens_and_digits(self, tmp_path, live_transcript):
        # Custom ids generated by leap-manage-clis normalise to
        # [a-z0-9][a-z0-9-]*; ensure the resume_store's safety regex
        # accepts that shape.
        tp = live_transcript()
        for cid in ("my-cli-2", "a1", "claude-pro", "custom-123"):
            record_session(tmp_path, cid, "t",
                           session_id=f"s-{cid}", transcript_path=tp)
        rows = load_tag_rows(tmp_path)
        clis = {r.cli for r in rows}
        assert clis == {"my-cli-2", "a1", "claude-pro", "custom-123"}


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

    def test_prune_stale_removes_all_dead_files(self, tmp_path, live_transcript):
        """Files where every entry's transcript is deleted get pruned."""
        tp_dead = live_transcript("dead.jsonl")
        record_session(tmp_path, "claude", "deadtag",
                       session_id="s", transcript_path=tp_dead)
        import os as _os
        _os.unlink(tp_dead)  # now every entry is stale
        assert prune_stale(tmp_path) == 1
        # File is gone
        assert not (tmp_path / "cli_sessions" / "claude" / "deadtag.json").exists()
        # And load_tag_rows returns nothing
        assert load_tag_rows(tmp_path) == []

    def test_prune_keeps_files_with_any_live_entry(self, tmp_path, live_transcript):
        """A file with at least one live entry must NOT be deleted —
        the stale entries will self-heal via the 20-cap as new sessions
        push them out.
        """
        tp_live = live_transcript("live.jsonl")
        tp_dead = live_transcript("will-delete.jsonl")
        record_session(tmp_path, "claude", "tag",
                       session_id="stale", transcript_path=tp_dead)
        record_session(tmp_path, "claude", "tag",
                       session_id="live", transcript_path=tp_live)
        import os as _os
        _os.unlink(tp_dead)
        assert prune_stale(tmp_path) == 0
        assert (tmp_path / "cli_sessions" / "claude" / "tag.json").exists()
        rows = load_tag_rows(tmp_path)
        # Only the live entry surfaces (stale is still on disk but filtered)
        assert len(rows) == 1 and len(rows[0].sessions) == 1
        assert rows[0].sessions[0].session_id == "live"

    def test_prune_respects_entries_without_transcript_path(self, tmp_path):
        """Entries with no transcript_path are treated as 'live' (we can't
        prove they're dead), so their files aren't pruned.  Reserved for
        future CLIs that might record only ids without a transcript file.
        """
        record_session(tmp_path, "claude", "no-path",
                       session_id="sid", transcript_path="")
        assert prune_stale(tmp_path) == 0
        assert (tmp_path / "cli_sessions" / "claude" / "no-path.json").exists()

    def test_prune_survives_concurrent_write_race(self, tmp_path, live_transcript):
        """CRITICAL: if the hook atomically writes a fresh entry AFTER
        prune decides "all dead" but BEFORE it unlinks, the fresh entry
        must not be lost.  The mtime re-check before unlink must catch
        this.
        """
        import threading, time, os as _os
        t_dead = live_transcript("dead.jsonl")
        t_live = live_transcript("live.jsonl")
        record_session(tmp_path, "claude", "racy",
                       session_id="stale", transcript_path=t_dead)
        _os.unlink(t_dead)

        race_file = tmp_path / "cli_sessions" / "claude" / "racy.json"

        # Force the race: slow the stat on our race_file so prune has a
        # predictable decision window we can slip a write into.
        from leap.utils import resume_store
        original_stat = _os.stat
        def slow_stat(p, *a, **k):
            r = original_stat(p, *a, **k)
            if str(p).endswith("racy.json"):
                time.sleep(0.15)
            return r
        resume_store.os.stat = slow_stat
        try:
            pt = threading.Thread(target=resume_store.prune_stale, args=(tmp_path,))
            pt.start()
            time.sleep(0.05)  # let prune's "is any live?" loop start
            # Inject a live entry while prune is still deciding
            record_session(tmp_path, "claude", "racy",
                           session_id="fresh", transcript_path=t_live)
            pt.join()
        finally:
            resume_store.os.stat = original_stat

        assert race_file.exists(), "file deleted despite concurrent fresh write"
        entries = json.loads(race_file.read_text())
        sids = {e["session_id"] for e in entries}
        assert "fresh" in sids, f"fresh entry lost: {sids}"

    def test_prune_treats_unstatable_transcript_as_live(self, tmp_path):
        """Defensive: if ``os.stat`` on the transcript fails with anything
        other than FileNotFoundError (permission denied, I/O error,
        network filesystem hiccup), we must NOT delete the record —
        only a confirmed 'file gone' (ENOENT) means dead.
        """
        import os as _os
        from unittest.mock import patch
        target = "/mnt/remote/fs/unreachable.jsonl"
        record_session(tmp_path, "claude", "maybe",
                       session_id="s", transcript_path=target)
        from leap.utils import resume_store

        real_stat = _os.stat
        def stat_selective(p, *a, **k):
            # Raise PermissionError only for the target transcript path;
            # let every other stat (the tag-file mtime guard, pathlib
            # internals, etc.) go through untouched.
            if str(p) == target:
                raise PermissionError("simulated I/O error")
            return real_stat(p, *a, **k)

        with patch.object(resume_store.os, "stat", side_effect=stat_selective):
            removed = resume_store.prune_stale(tmp_path)
        assert removed == 0, "must not delete on non-ENOENT stat errors"
        assert (tmp_path / "cli_sessions" / "claude" / "maybe.json").exists()

    def test_prune_survives_permission_error_on_root(self, tmp_path):
        """If even the outer ``.storage/cli_sessions/`` read fails
        (unusual permission setup, bad mount), prune returns 0 without
        raising — best-effort contract.
        """
        import os as _os
        from unittest.mock import patch
        from leap.utils import resume_store
        record_session(tmp_path, "claude", "t", session_id="s", transcript_path="")
        with patch.object(resume_store.Path, "is_dir",
                          side_effect=PermissionError("denied")):
            assert resume_store.prune_stale(tmp_path) == 0

    def test_prune_concurrent_invocations_end_state(self, tmp_path):
        """Multiple ``prune_stale`` calls racing on the same store must:
        1. Leave the on-disk state correct (all stale files gone).
        2. Never raise.

        The *count* under concurrency is not checked — macOS APFS's
        ``unlink`` can report success from multiple threads racing on
        the same file, so the returned total can exceed the actual
        number of deletions.  That's noted in ``prune_stale`` docstring
        and harmless (caller discards the count).
        """
        import threading
        from leap.utils import resume_store
        for i in range(15):
            record_session(tmp_path, "claude", f"t{i}",
                           session_id="s", transcript_path="/definitely-nonexistent")
        errors: list[BaseException] = []
        def worker():
            try:
                resume_store.prune_stale(tmp_path)
            except BaseException as e:
                errors.append(e)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors, f"concurrent prune raised: {errors}"
        # Final on-disk state: no files left
        claude_dir = tmp_path / "cli_sessions" / "claude"
        remaining = list(claude_dir.glob("*.json")) if claude_dir.exists() else []
        assert not remaining, f"leaked stale files: {remaining}"

    def test_prune_across_multiple_clis(self, tmp_path, live_transcript):
        tp = live_transcript("t.jsonl")
        record_session(tmp_path, "claude", "alive",
                       session_id="s", transcript_path=tp)
        record_session(tmp_path, "codex", "dead",
                       session_id="s", transcript_path=tp)
        import os as _os
        # Only kill codex's transcript (we're using the same file — re-link)
        # Actually use distinct transcripts
        tp2 = live_transcript("t2.jsonl")
        record_session(tmp_path, "codex", "dead",
                       session_id="s2", transcript_path=tp2)
        _os.unlink(tp2)
        # codex/dead.json has entries pointing at: tp (still alive) and tp2 (dead)
        # → has a live entry → NOT pruned
        assert prune_stale(tmp_path) == 0
        # Now delete tp too — both codex entries become stale
        _os.unlink(tp)
        # codex/dead now fully stale; claude/alive also fully stale now.
        # Both files should go.
        assert prune_stale(tmp_path) == 2

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
