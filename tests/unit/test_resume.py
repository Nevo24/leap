"""Tests for the `leap --resume` feature.

Covers the CLIProvider resume protocol (Claude / Codex / Cursor) and
the shared resume_store read/write layer.  The picker UI itself is
stateful / terminal-dependent so we leave that out of the unit layer.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from leap.cli_providers.registry import (
    CustomCLIProvider,
    get_provider,
    resume_cwd_for_record,
)
from leap.utils.claude_session_move import slugify
from leap.utils.resume_store import (
    MAX_ENTRIES_PER_TAG,
    SessionRecord,
    TagRow,
    latest_transcript_for,
    load_raw_tag_rows,
    load_tag_rows,
    prune_stale,
    record_session,
    relocate_records,
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


def _make_claude_transcript(tmp_path, start_cwd, uuid="0123abcd-ef45", lines=None):
    """Create a Claude transcript under ``<tmp>/.claude/projects/<slug>/``.

    The dir slug encodes ``start_cwd`` (matching Claude's on-disk layout).
    ``lines`` are JSONL records written in order; defaults to a single record
    carrying ``start_cwd``.
    """
    proj = tmp_path / ".claude" / "projects" / slugify(start_cwd)
    proj.mkdir(parents=True, exist_ok=True)
    tp = proj / f"{uuid}.jsonl"
    if lines is None:
        lines = [{"type": "user", "cwd": start_cwd}]
    tp.write_text("".join(json.dumps(rec) + "\n" for rec in lines))
    return str(tp)


class TestClaudeResumeCwdReconcile:
    """``resume_cwd_for_transcript`` heals a recorded cwd that drifted from the
    transcript's anchored slug dir (e.g. after a mid-session ``cd``)."""

    def test_no_drift_returns_cwd_without_reading(self, tmp_path):
        # cwd slug already matches the transcript dir -> returned as-is, even
        # though the transcript file does not exist (the match short-circuits
        # before any read).
        p = get_provider("claude")
        start = "/Users/me/workspace/repo"
        tp = str(tmp_path / ".claude" / "projects" / slugify(start) / "x.jsonl")
        assert p.resume_cwd_for_transcript(tp, start) == start

    def test_drift_recovers_original_from_transcript(self, tmp_path):
        p = get_provider("claude")
        start = "/Users/me/workspace/repo"
        tp = _make_claude_transcript(
            tmp_path, start,
            lines=[{"cwd": start}, {"cwd": "/Users/me/elsewhere"}],
        )
        assert p.resume_cwd_for_transcript(tp, "/Users/me/elsewhere") == start

    def test_drift_skips_leading_records_without_cwd(self, tmp_path):
        p = get_provider("claude")
        start = "/Users/me/workspace/repo"
        tp = _make_claude_transcript(
            tmp_path, start, lines=[{"type": "summary"}, {"cwd": start}],
        )
        assert p.resume_cwd_for_transcript(tp, "/Users/me/elsewhere") == start

    def test_drift_unrecoverable_missing_transcript_returns_cwd(self, tmp_path):
        # Slug dir name encodes `start`, but the file isn't there to recover
        # from -> fall back to the (drifted) cwd, never invent a path.
        p = get_provider("claude")
        start = "/Users/me/workspace/repo"
        tp = str(tmp_path / ".claude" / "projects" / slugify(start) / "x.jsonl")
        assert p.resume_cwd_for_transcript(tp, "/Users/me/elsewhere") \
            == "/Users/me/elsewhere"

    def test_drift_first_cwd_slug_mismatch_returns_cwd(self, tmp_path):
        # The transcript's first cwd doesn't slug-match its own dir (a corrupt
        # or hand-moved transcript) -> don't trust it.
        p = get_provider("claude")
        start = "/Users/me/workspace/repo"
        tp = _make_claude_transcript(
            tmp_path, start, lines=[{"cwd": "/totally/different/place"}],
        )
        assert p.resume_cwd_for_transcript(tp, "/Users/me/elsewhere") \
            == "/Users/me/elsewhere"

    def test_drift_skips_giant_line_before_cwd(self, tmp_path):
        # A ~600 KB record before the cwd line must not break recovery: it's
        # read truncated (per-line cap), fails json, gets skipped, and the cwd
        # on the next line is still found — without loading the giant line whole.
        p = get_provider("claude")
        start = "/Users/me/workspace/repo"
        big = {"type": "x", "blob": "A" * (600 * 1024)}
        tp = _make_claude_transcript(tmp_path, start, lines=[big, {"cwd": start}])
        assert p.resume_cwd_for_transcript(tp, "/Users/me/elsewhere") == start

    def test_drift_total_cap_bounds_scan(self, tmp_path):
        # cwd buried after >2 MiB of content is not recovered (the scan is
        # bounded) -> falls back to the given cwd rather than reading unboundedly.
        p = get_provider("claude")
        start = "/Users/me/workspace/repo"
        filler = {"type": "x", "blob": "A" * (300 * 1024)}
        tp = _make_claude_transcript(
            tmp_path, start, lines=[filler] * 12 + [{"cwd": start}],
        )
        assert p.resume_cwd_for_transcript(tp, "/fallback") == "/fallback"

    def test_non_claude_transcript_path_returns_cwd(self):
        p = get_provider("claude")
        assert p.resume_cwd_for_transcript(
            "/Users/me/.codex/sessions/x.jsonl", "/some/cwd") == "/some/cwd"

    def test_empty_transcript_path_returns_cwd(self):
        p = get_provider("claude")
        assert p.resume_cwd_for_transcript("", "/some/cwd") == "/some/cwd"

    def test_record_pins_to_start_cwd(self, tmp_path):
        # Claude record-time delegates to resume-time (base default), so the
        # record is pinned to the session's start cwd (clean picker display).
        p = get_provider("claude")
        start = "/Users/me/workspace/repo"
        tp = _make_claude_transcript(tmp_path, start, lines=[{"cwd": start}])
        assert p.record_cwd_for_transcript(tp, "/Users/me/elsewhere") == start

    def test_base_default_is_noop(self):
        # A provider that doesn't override (Cursor) returns cwd unchanged.
        p = get_provider("cursor-agent")
        assert p.resume_cwd_for_transcript(
            "/anything/.cursor/chats/h/c/x.jsonl", "/some/cwd") == "/some/cwd"
        assert p.record_cwd_for_transcript(
            "/anything/.cursor/chats/h/c/x.jsonl", "/some/cwd") == "/some/cwd"


class TestGeminiResumeCwdReconcile:
    """``resume_cwd_for_transcript`` heals a Gemini cwd that drifted from the
    session's registry slug dir (e.g. after a mid-session ``cd``).  Gemini maps
    cwd->slug in ``projects.json`` and stores sessions under
    ``tmp/<slug>/chats/``, so recovery is a registry reverse-lookup."""

    _REG = "leap.utils.gemini_session_move.GEMINI_PROJECTS_REGISTRY"

    def _registry(self, tmp_path, monkeypatch, mapping):
        reg = tmp_path / "projects.json"
        reg.write_text(json.dumps({"projects": mapping}))
        monkeypatch.setattr(self._REG, reg)

    def _tp(self, slug):
        return f"/home/u/.gemini/tmp/{slug}/chats/session-2026-01-01T00-00-abcd.jsonl"

    def test_no_drift_returns_cwd(self, tmp_path, monkeypatch):
        self._registry(tmp_path, monkeypatch, {"/work/repo": "repo"})
        g = get_provider("gemini")
        assert g.resume_cwd_for_transcript(self._tp("repo"), "/work/repo") == "/work/repo"

    def test_drift_recovers_via_registry(self, tmp_path, monkeypatch):
        # recorded cwd drifted to the subdir (slug "sub"); the session lives
        # under "repo" -> recover the original cwd from the registry.
        self._registry(tmp_path, monkeypatch,
                       {"/work/repo": "repo", "/work/repo/sub": "sub"})
        g = get_provider("gemini")
        assert g.resume_cwd_for_transcript(self._tp("repo"), "/work/repo/sub") \
            == "/work/repo"

    def test_drift_ambiguous_slug_returns_cwd(self, tmp_path, monkeypatch):
        # Two cwds mapping to the same slug (shouldn't happen given Gemini's
        # -N dedup, but never guess) -> fall back to the given cwd.
        self._registry(tmp_path, monkeypatch, {"/a/repo": "repo", "/b/repo": "repo"})
        g = get_provider("gemini")
        assert g.resume_cwd_for_transcript(self._tp("repo"), "/somewhere") == "/somewhere"

    def test_unknown_slug_returns_cwd(self, tmp_path, monkeypatch):
        self._registry(tmp_path, monkeypatch, {"/work/repo": "repo"})
        g = get_provider("gemini")
        assert g.resume_cwd_for_transcript(self._tp("ghost"), "/work/x") == "/work/x"

    def test_non_gemini_path_returns_cwd(self, tmp_path, monkeypatch):
        self._registry(tmp_path, monkeypatch, {"/work/repo": "repo"})
        g = get_provider("gemini")
        assert g.resume_cwd_for_transcript(
            "/home/u/.claude/projects/-x/u.jsonl", "/work/x") == "/work/x"

    def test_empty_transcript_returns_cwd(self):
        g = get_provider("gemini")
        assert g.resume_cwd_for_transcript("", "/work/x") == "/work/x"


class TestCodexResumeCwdReconcile:
    """Codex resumes by UUID, but the picker still ``chdir``s and passes
    ``-C <cwd>``; resuming in the session's start cwd (from ``session_meta``)
    keeps Codex from re-prompting 'Choose working directory'.  Record time must
    NOT pin — Codex's logical relocate owns the recorded cwd."""

    def _rollout(self, tmp_path, start_cwd, *, sid="019eb287-aaaa-bbbb",
                 meta=True, cwd_value=None):
        d = tmp_path / ".codex" / "sessions" / "2026" / "06" / "10"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"rollout-2026-06-10T20-14-25-{sid}.jsonl"
        if meta:
            payload = {"id": sid}
            payload["cwd"] = start_cwd if cwd_value is None else cwd_value
            head = {"type": "session_meta", "payload": payload}
        else:
            head = {"type": "message", "payload": {"role": "user"}}
        f.write_text(json.dumps(head) + "\n")
        return str(f)

    def test_resume_recovers_start_cwd(self, tmp_path):
        # Start cwd exists -> prefer it (silences Codex's dir prompt).
        c = get_provider("codex")
        start = str(tmp_path / "repo")
        os.makedirs(start)
        tp = self._rollout(tmp_path, start)
        assert c.resume_cwd_for_transcript(tp, "/some/drifted") == start

    def test_resume_deleted_start_cwd_falls_back(self, tmp_path):
        # session_meta points at a dir that no longer exists -> don't force it;
        # Codex resumes by UUID, so a deleted start dir must not block resume.
        c = get_provider("codex")
        tp = self._rollout(tmp_path, "/gone/nowhere/repo")
        assert c.resume_cwd_for_transcript(tp, str(tmp_path)) == str(tmp_path)

    def test_record_does_not_pin_start_cwd(self, tmp_path):
        # Critical: record must keep the given cwd, so a logical "stay in
        # current" relocation isn't clobbered by the next hook.
        c = get_provider("codex")
        tp = self._rollout(tmp_path, "/work/repo")
        assert c.record_cwd_for_transcript(tp, "/work/relocated") == "/work/relocated"

    def test_resume_no_session_meta_falls_back(self, tmp_path):
        c = get_provider("codex")
        tp = self._rollout(tmp_path, "/work/repo", meta=False)
        assert c.resume_cwd_for_transcript(tp, "/fallback") == "/fallback"

    def test_resume_non_string_cwd_falls_back(self, tmp_path):
        c = get_provider("codex")
        tp = self._rollout(tmp_path, "/work/repo", cwd_value=["bad"])
        assert c.resume_cwd_for_transcript(tp, "/fallback") == "/fallback"

    def test_resume_missing_file_falls_back(self, tmp_path):
        c = get_provider("codex")
        tp = str(tmp_path / ".codex" / "sessions" / "nope" / "rollout-z.jsonl")
        assert c.resume_cwd_for_transcript(tp, "/fallback") == "/fallback"

    def test_resume_non_codex_path_falls_back(self):
        c = get_provider("codex")
        assert c.resume_cwd_for_transcript(
            "/x/.claude/projects/s/u.jsonl", "/fb") == "/fb"


class TestResumeCwdForRecord:
    """Shared healing entry point used by every resume launcher (the
    ``leap --resume`` picker and the monitor's GUI resume paths)."""

    def test_unknown_cli_returns_cwd(self):
        assert resume_cwd_for_record(
            "nope", "/x/.codex/sessions/r.jsonl", "/cwd") == "/cwd"

    def test_empty_transcript_returns_cwd(self):
        assert resume_cwd_for_record("claude", "", "/cwd") == "/cwd"

    def test_delegates_to_provider_and_heals(self, tmp_path):
        start = "/Users/me/workspace/repo"
        tp = _make_claude_transcript(tmp_path, start, lines=[{"cwd": start}])
        assert resume_cwd_for_record("claude", tp, "/Users/me/elsewhere") == start

    def test_raising_provider_is_swallowed(self, monkeypatch):
        # A heal failure must never break resume -> fall back to the given cwd.
        def boom(self, transcript_path, cwd):
            raise RuntimeError("boom")
        monkeypatch.setattr(
            "leap.cli_providers.claude.ClaudeProvider.resume_cwd_for_transcript",
            boom,
        )
        assert resume_cwd_for_record(
            "claude", "/x/.claude/projects/s/u.jsonl", "/cwd") == "/cwd"


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

    def test_resume_args_is_subcommand_form_with_cd(self):
        # Codex resume is a positional subcommand prefixed with
        # ``-C <cwd>`` so codex doesn't fire its own
        # "Choose working directory to resume" prompt on startup.
        # The cwd is captured at call time (``os.getcwd()``) — at
        # leap-server.py startup, that's the cwd leap-resume.py
        # already chdir'd into.
        import os
        p = get_provider("codex")
        args = p.resume_args("abc")
        assert args[:2] == ["-C", os.getcwd()]
        assert args[2:] == ["resume", "abc"]


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

    def test_extract_session_id_rejects_non_string_value(self):
        # A truthy non-string id (odd hook payload) must NOT be returned -
        # it would later be Path-joined by session_exists/find_chat_dir and
        # crash the whole `leap --resume` picker.  None is the safe result.
        p = get_provider("cursor-agent")
        assert p.extract_session_id({"conversation_id": ["x"]}) is None
        assert p.extract_session_id({"chatId": {"id": "x"}}) is None
        assert p.extract_session_id({"session_id": 12345}) is None

    def test_extract_session_id_skips_bad_value_for_next_valid(self):
        # A non-string in the first key must be skipped in favour of a
        # valid string in a later key, not abort the lookup.
        p = get_provider("cursor-agent")
        sid = p.extract_session_id(
            {"conversation_id": ["bad"], "chatId": "good-uuid"})
        assert sid == "good-uuid"

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
        # Ownership checks read **raw** rows (``load_raw_tag_rows``),
        # bypassing the display-layer dedup.  If two Leap tags both
        # have live servers on the same CLI session UUID (physically
        # impossible for today's CLIs, but a future multi-seat resume
        # would enable it), both tags must be reported so the user
        # isn't silently routed past the other owner.
        record_session(tmp_path, "claude", "fork-a",
                       session_id="shared", transcript_path="")
        time.sleep(0.01)
        record_session(tmp_path, "claude", "fork-b",
                       session_id="shared", transcript_path="")
        picker._live_clis = {"fork-a": "claude", "fork-b": "claude"}
        owners = picker._live_session_owners(load_raw_tag_rows(tmp_path))
        assert "shared" in owners
        assert {t for _, t in owners["shared"]} == {"fork-a", "fork-b"}

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

    def test_record_normalizes_relative_transcript_path(self, tmp_path, monkeypatch):
        """A relative ``transcript_path`` must be normalized against the
        hook's cwd at record time — otherwise ``os.path.getsize`` in the
        picker (which runs from a different cwd) would resolve against
        the wrong root.  All built-in CLIs pass absolute paths; this
        guards against a future custom CLI emitting a relative one."""
        # Create a real file inside tmp_path we can reach via a
        # relative name by chdir'ing to tmp_path first.
        tp_abs = tmp_path / "live.jsonl"
        tp_abs.write_bytes(b"x" * 42)
        monkeypatch.chdir(tmp_path)
        record_session(tmp_path, "claude", "rel",
                       session_id="s", transcript_path="live.jsonl")
        monkeypatch.chdir("/")  # picker runs from elsewhere
        rows = load_tag_rows(tmp_path)
        assert len(rows) == 1
        s = rows[0].sessions[0]
        assert os.path.isabs(s.transcript_path)
        assert s.transcript_path == str(tp_abs)
        assert s.size == 42

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

    def test_dedup_same_session_across_tags_keeps_newest(
        self, tmp_path, live_transcript,
    ):
        """Forked resume: tag ``9`` busy → user picks ``9b``.  Both files
        end up with the same session_id; the older tag's copy must drop
        out of the picker so the user sees one row per conversation."""
        tp = live_transcript()
        record_session(tmp_path, "claude", "9",
                       session_id="S1", transcript_path=tp)
        time.sleep(0.01)
        record_session(tmp_path, "claude", "9b",
                       session_id="S1", transcript_path=tp)
        rows = load_tag_rows(tmp_path)
        assert len(rows) == 1, "duplicate CLI session should surface once"
        assert rows[0].tag == "9b", "newest last_seen wins"
        assert [s.session_id for s in rows[0].sessions] == ["S1"]

    def test_dedup_preserves_non_duplicated_sessions_on_older_tag(
        self, tmp_path, live_transcript,
    ):
        """Surgery is per-session: tag ``9`` with [S1, S2, S3] loses only
        the duplicated S1 when ``9b`` picks it up, keeping [S2, S3]."""
        tp = live_transcript()
        record_session(tmp_path, "claude", "9",
                       session_id="S1", transcript_path=tp)
        time.sleep(0.01)
        record_session(tmp_path, "claude", "9",
                       session_id="S2", transcript_path=tp)
        time.sleep(0.01)
        record_session(tmp_path, "claude", "9",
                       session_id="S3", transcript_path=tp)
        time.sleep(0.01)
        record_session(tmp_path, "claude", "9b",
                       session_id="S1", transcript_path=tp)
        rows = sorted(load_tag_rows(tmp_path), key=lambda r: r.tag)
        assert len(rows) == 2
        by_tag = {r.tag: r for r in rows}
        assert sorted(s.session_id for s in by_tag["9"].sessions) == ["S2", "S3"]
        assert [s.session_id for s in by_tag["9b"].sessions] == ["S1"]

    def test_dedup_is_per_cli_not_cross_cli(
        self, tmp_path, live_transcript,
    ):
        """Same UUID recorded under two different CLIs must not collapse —
        Claude's S1 and Codex's S1 are unrelated conversations that happen
        to share a hex string."""
        tp = live_transcript()
        record_session(tmp_path, "claude", "9",
                       session_id="S1", transcript_path=tp)
        record_session(tmp_path, "codex", "9",
                       session_id="S1", transcript_path=tp)
        rows = load_tag_rows(tmp_path)
        assert len(rows) == 2
        assert {r.cli for r in rows} == {"claude", "codex"}

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


# --------------------------------------------------------------------------
# relocate_records — cross-tag bookkeeping after a cwd-relocate
# --------------------------------------------------------------------------

class TestRelocateRecords:
    def test_rewrites_matching_entries_across_tags(self, tmp_path):
        # Two tags hold entries for the same session_id (forked-resume
        # scenario).  After relocate, both should reflect the new cwd.
        old_tp = str(tmp_path / "old/abc.jsonl")
        new_tp = str(tmp_path / "new/abc.jsonl")
        Path(old_tp).parent.mkdir(parents=True, exist_ok=True)
        Path(old_tp).touch()
        record_session(tmp_path, "claude", "tag1",
                       session_id="abc", transcript_path=old_tp, cwd="/old")
        record_session(tmp_path, "claude", "tag2",
                       session_id="abc", transcript_path=old_tp, cwd="/old")
        # Unrelated entry (different session_id) — must NOT be rewritten.
        other_tp = str(tmp_path / "other.jsonl")
        Path(other_tp).touch()
        record_session(tmp_path, "claude", "tag1",
                       session_id="other-id", transcript_path=other_tp, cwd="/other")

        n = relocate_records(tmp_path, "claude",
                             session_id="abc",
                             new_cwd="/new",
                             new_transcript_path=new_tp)
        assert n == 2  # both tag files rewritten

        tag1 = json.loads((tmp_path / "cli_sessions/claude/tag1.json").read_text())
        tag2 = json.loads((tmp_path / "cli_sessions/claude/tag2.json").read_text())
        abc1 = next(e for e in tag1 if e["session_id"] == "abc")
        other = next(e for e in tag1 if e["session_id"] == "other-id")
        assert abc1["transcript_path"] == new_tp
        assert abc1["cwd"] == "/new"
        assert other["transcript_path"] == other_tp
        assert other["cwd"] == "/other"
        abc2 = next(e for e in tag2 if e["session_id"] == "abc")
        assert abc2["transcript_path"] == new_tp
        assert abc2["cwd"] == "/new"

    def test_preserves_empty_transcript_path_when_new_is_empty(self, tmp_path):
        # Cursor-style records: transcript_path stays '' (cursor's hook
        # doesn't expose one, and rewriting it to a directory path
        # would lie about what os.path.getsize can stat).
        record_session(tmp_path, "cursor-agent", "t",
                       session_id="ccc", transcript_path="", cwd="/old-ws")
        n = relocate_records(tmp_path, "cursor-agent",
                             session_id="ccc",
                             new_cwd="/new-ws",
                             new_transcript_path="")  # empty → don't touch path
        assert n == 1
        rec = json.loads((tmp_path / "cli_sessions/cursor-agent/t.json").read_text())[0]
        assert rec["cwd"] == "/new-ws"
        assert rec["transcript_path"] == ""  # preserved

    def test_does_not_bump_last_seen(self, tmp_path):
        old_tp = str(tmp_path / "old.jsonl")
        Path(old_tp).touch()
        record_session(tmp_path, "claude", "t",
                       session_id="abc", transcript_path=old_tp, cwd="/old")
        before = json.loads((tmp_path / "cli_sessions/claude/t.json").read_text())
        last_seen_before = before[0]["last_seen"]

        new_tp = str(tmp_path / "new.jsonl")
        time.sleep(0.01)
        relocate_records(tmp_path, "claude",
                         session_id="abc",
                         new_cwd="/new",
                         new_transcript_path=new_tp)
        after = json.loads((tmp_path / "cli_sessions/claude/t.json").read_text())
        assert after[0]["last_seen"] == last_seen_before

    def test_no_op_when_session_id_unknown(self, tmp_path):
        Path(tmp_path / "cli_sessions/claude").mkdir(parents=True)
        record_session(tmp_path, "claude", "t",
                       session_id="x", transcript_path=str(tmp_path / "x.jsonl"),
                       cwd="/somewhere")
        assert relocate_records(
            tmp_path, "claude",
            session_id="never-recorded",
            new_cwd="/x",
            new_transcript_path="/wherever.jsonl",
        ) == 0

    def test_rejects_unsafe_cli_id(self, tmp_path):
        assert relocate_records(
            tmp_path, "../escape",
            session_id="abc", new_cwd="/c", new_transcript_path="b",
        ) == 0

    def test_rejects_unsafe_session_id(self, tmp_path):
        # Same defense for session_id — crafted values must not escape
        # the cli_sessions/<cli>/<tag>.json layout via the on-disk write.
        assert relocate_records(
            tmp_path, "claude",
            session_id="../escape", new_cwd="/c", new_transcript_path="b",
        ) == 0

    def test_returns_zero_when_cli_dir_missing(self, tmp_path):
        assert relocate_records(
            tmp_path, "claude",
            session_id="abc", new_cwd="/c", new_transcript_path="b",
        ) == 0


# --------------------------------------------------------------------------
# latest_transcript_for — newest recorded transcript path for (cli, tag)
# --------------------------------------------------------------------------

class TestLatestTranscriptFor:
    def test_missing_tag_file_returns_none(self, tmp_path):
        assert latest_transcript_for(tmp_path, "claude", "neverseen") is None

    def test_returns_newest_by_last_seen(self, tmp_path):
        # record_session appends oldest-first; the newest (greatest last_seen)
        # entry must win, not entries[0].
        record_session(tmp_path, "claude", "t",
                       session_id="old", transcript_path="/p/old.jsonl")
        time.sleep(0.01)
        record_session(tmp_path, "claude", "t",
                       session_id="new", transcript_path="/p/new.jsonl")
        assert latest_transcript_for(tmp_path, "claude", "t") == "/p/new.jsonl"

    def test_skips_empty_transcript_path(self, tmp_path):
        # A newer record with no transcript (e.g. a Cursor-style entry) must
        # not shadow the freshest entry that actually has a path.
        record_session(tmp_path, "claude", "t",
                       session_id="real", transcript_path="/p/real.jsonl")
        time.sleep(0.01)
        record_session(tmp_path, "claude", "t",
                       session_id="empty", transcript_path="")
        assert latest_transcript_for(tmp_path, "claude", "t") == "/p/real.jsonl"

    def test_handles_non_numeric_last_seen(self, tmp_path):
        # A corrupt ``last_seen`` must not raise (this is read on the monitor's
        # render thread) -- the entry is still usable, just sorted as oldest.
        d = tmp_path / "cli_sessions" / "claude"
        d.mkdir(parents=True)
        (d / "t.json").write_text(json.dumps([
            {"session_id": "a", "transcript_path": "/p/a.jsonl",
             "last_seen": "not-a-number"},
        ]))
        assert latest_transcript_for(tmp_path, "claude", "t") == "/p/a.jsonl"
