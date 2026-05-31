"""Tests for ``leap.utils.claude_session_move``.

Verifies the safety properties of the cross-cwd relocation helper:

* Slug encoding matches Claude Code's empirically-observed algorithm.
* Happy paths leave the destination valid and source deleted.
* Failure paths leave the **source intact** — never partially deleted.
* The ``on_committed`` callback runs after dst is verified but
  *before* src is deleted, so callback failure also leaves src intact.

The helper monkey-patches its module-level ``CLAUDE_PROJECTS_ROOT`` to
a tmp dir so the tests never touch the real ``~/.claude/projects/``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from leap.utils import claude_session_move as csm
from leap.utils.claude_session_move import (
    RelocationError,
    relocate_claude_session,
    slugify,
)


# --------------------------------------------------------------------------
# Slugify
# --------------------------------------------------------------------------

class TestSlugify:
    def test_simple_path(self):
        assert slugify("/Users/me/proj") == "-Users-me-proj"

    def test_dot_in_username(self):
        # Empirical: ``/Users/Nevo.Mashiach`` → ``-Users-Nevo-Mashiach``.
        assert slugify("/Users/Nevo.Mashiach") == "-Users-Nevo-Mashiach"

    def test_consecutive_separators_not_collapsed(self):
        # Empirical: ``/Users/me/.claude`` → ``-Users-me--claude``
        # (slash + dot become two separate dashes, not one).
        assert slugify("/Users/me/.claude") == "-Users-me--claude"

    def test_existing_hyphens_preserved(self):
        assert slugify("/Users/me/my-app") == "-Users-me-my-app"

    def test_underscores_become_dash(self):
        # Empirical: Claude replaces ``_`` with ``-`` (it is NOT
        # preserved).  Verified against real ~/.claude/projects dirs,
        # e.g. ``mwb-manifests_3`` → ``mwb-manifests-3``.
        assert slugify("/Users/me/my_app") == "-Users-me-my-app"

    def test_numbers_preserved(self):
        assert slugify("/Users/me/proj42") == "-Users-me-proj42"

    def test_spaces_become_dash(self):
        assert slugify("/Users/me/My Project") == "-Users-me-My-Project"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def fake_projects_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``CLAUDE_PROJECTS_ROOT`` so we can't accidentally touch
    the user's real ``~/.claude/projects/`` while testing."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(csm, "CLAUDE_PROJECTS_ROOT", root)
    return root


def _make_session(
    root: Path,
    cwd: str,
    session_id: str,
    *,
    transcript_body: bytes = b"line1\nline2\n",
    sidecar: dict[str, bytes] | None = None,
) -> tuple[Path, Path | None]:
    """Create a mock Claude session under root/<slug(cwd)>/.

    Returns (jsonl_path, sidecar_dir or None).
    """
    proj = root / slugify(cwd)
    proj.mkdir(parents=True, exist_ok=True)
    jsonl = proj / f"{session_id}.jsonl"
    jsonl.write_bytes(transcript_body)
    sc_dir: Path | None = None
    if sidecar is not None:
        sc_dir = proj / session_id
        sc_dir.mkdir()
        for rel, data in sidecar.items():
            target = sc_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return jsonl, sc_dir


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------

class TestHappyPath:
    def test_relocate_jsonl_only(self, fake_projects_root: Path):
        src_cwd = "/src/project"
        dst_cwd = "/dst/project"
        sid = "abc-123"
        body = b"transcript bytes\n"
        src_jsonl, _ = _make_session(
            fake_projects_root, src_cwd, sid, transcript_body=body,
        )
        new_path = relocate_claude_session(sid, src_cwd, dst_cwd)
        # Source gone, destination present with identical bytes.
        assert not src_jsonl.exists()
        dst_jsonl = Path(new_path)
        assert dst_jsonl.exists()
        assert dst_jsonl.read_bytes() == body
        # Destination dir is named with the dst slug.
        assert dst_jsonl.parent == fake_projects_root / slugify(dst_cwd)

    def test_relocate_with_sidecar(self, fake_projects_root: Path):
        src_cwd = "/src/A"
        dst_cwd = "/dst/B"
        sid = "uuid-with-sidecar"
        sidecar = {
            "subagents/agent-1.jsonl": b"agent-jsonl-bytes\n",
            "subagents/agent-1.meta.json": b'{"agent":1}',
            "tool-results/foo.txt": b"a" * 5000,
        }
        src_jsonl, src_dir = _make_session(
            fake_projects_root, src_cwd, sid, sidecar=sidecar,
        )
        assert src_dir is not None and src_dir.is_dir()
        new_path = relocate_claude_session(sid, src_cwd, dst_cwd)
        # Source jsonl + sidecar both gone.
        assert not src_jsonl.exists()
        assert not src_dir.exists()
        # Destination jsonl + sidecar both present with identical content.
        dst_jsonl = Path(new_path)
        dst_dir = dst_jsonl.parent / sid
        assert dst_jsonl.exists()
        for rel, data in sidecar.items():
            f = dst_dir / rel
            assert f.exists(), f"missing sidecar file {rel}"
            assert f.read_bytes() == data

    def test_same_slug_is_noop(self, fake_projects_root: Path):
        # ``/a/b`` and ``/a-b`` slugify to the same string ``-a-b`` —
        # the helper should detect this and return the source path
        # without doing any work.
        src_cwd = "/a/b"
        dst_cwd = "/a-b"
        assert slugify(src_cwd) == slugify(dst_cwd)
        sid = "noop-uuid"
        src_jsonl, _ = _make_session(fake_projects_root, src_cwd, sid)
        new_path = relocate_claude_session(sid, src_cwd, dst_cwd)
        assert Path(new_path) == src_jsonl
        assert src_jsonl.exists()  # untouched


# --------------------------------------------------------------------------
# Pre-flight aborts (source intact)
# --------------------------------------------------------------------------

class TestPreFlightAbort:
    def test_missing_source_jsonl(self, fake_projects_root: Path):
        # Project dir exists but no jsonl in it.
        src_cwd = "/src/proj"
        (fake_projects_root / slugify(src_cwd)).mkdir(parents=True)
        with pytest.raises(RelocationError, match="source transcript not found"):
            relocate_claude_session("nope", src_cwd, "/dst/proj")

    def test_missing_source_project_dir(self, fake_projects_root: Path):
        # Slug computed for a cwd that has no on-disk dir at all.
        with pytest.raises(RelocationError, match="source project directory not found"):
            relocate_claude_session("any", "/no/such/cwd", "/dst")

    def test_destination_jsonl_collision(self, fake_projects_root: Path):
        sid = "uuid-collide"
        src_cwd = "/src"
        dst_cwd = "/dst"
        src_jsonl, _ = _make_session(fake_projects_root, src_cwd, sid)
        # Pre-create a colliding dst jsonl.
        dst_proj = fake_projects_root / slugify(dst_cwd)
        dst_proj.mkdir(parents=True)
        (dst_proj / f"{sid}.jsonl").write_bytes(b"existing")
        with pytest.raises(RelocationError, match="destination transcript already exists"):
            relocate_claude_session(sid, src_cwd, dst_cwd)
        # Source untouched.
        assert src_jsonl.exists()

    def test_destination_sidecar_collision(self, fake_projects_root: Path):
        sid = "uuid-sidecar-collide"
        src_cwd = "/src"
        dst_cwd = "/dst"
        src_jsonl, src_dir = _make_session(
            fake_projects_root, src_cwd, sid, sidecar={"a.txt": b"a"},
        )
        assert src_dir is not None
        dst_proj = fake_projects_root / slugify(dst_cwd)
        dst_proj.mkdir(parents=True)
        (dst_proj / sid).mkdir()
        with pytest.raises(RelocationError, match="destination sidecar directory already exists"):
            relocate_claude_session(sid, src_cwd, dst_cwd)
        # Source jsonl + sidecar untouched.
        assert src_jsonl.exists()
        assert src_dir.exists()
        assert (src_dir / "a.txt").read_bytes() == b"a"

    def test_missing_session_id(self, fake_projects_root: Path):
        with pytest.raises(RelocationError, match="missing session id"):
            relocate_claude_session("", "/src", "/dst")

    def test_missing_src_cwd(self, fake_projects_root: Path):
        with pytest.raises(RelocationError, match="missing source cwd"):
            relocate_claude_session("sid", "", "/dst")

    def test_missing_dst_cwd(self, fake_projects_root: Path):
        with pytest.raises(RelocationError, match="missing destination cwd"):
            relocate_claude_session("sid", "/src", "")


# --------------------------------------------------------------------------
# on_committed callback
# --------------------------------------------------------------------------

class TestOnCommitted:
    def test_called_with_new_path(self, fake_projects_root: Path):
        sid = "cb-uuid"
        src_jsonl, _ = _make_session(fake_projects_root, "/src", sid)
        captured: list[str] = []

        def cb(new_path: str) -> None:
            captured.append(new_path)

        new_path = relocate_claude_session(sid, "/src", "/dst", on_committed=cb)
        assert captured == [new_path]

    def test_runs_before_source_delete(self, fake_projects_root: Path):
        # The callback's contract is: dst is verified in place, src
        # still exists.  Verify both states from inside the callback.
        sid = "order-uuid"
        src_jsonl, _ = _make_session(fake_projects_root, "/src", sid)
        observations: dict[str, bool] = {}

        def cb(new_path: str) -> None:
            observations["src_exists"] = src_jsonl.exists()
            observations["dst_exists"] = Path(new_path).exists()

        relocate_claude_session(sid, "/src", "/dst", on_committed=cb)
        assert observations == {"src_exists": True, "dst_exists": True}
        # And after the call, src is gone.
        assert not src_jsonl.exists()

    def test_callback_failure_leaves_source_intact(self, fake_projects_root: Path):
        # If on_committed raises, src is NOT deleted — caller can
        # recover from the duplicate state.
        sid = "cb-fail-uuid"
        src_jsonl, _ = _make_session(fake_projects_root, "/src", sid)

        def cb(_new_path: str) -> None:
            raise RuntimeError("bookkeeping kaboom")

        with pytest.raises(RelocationError, match="bookkeeping callback failed"):
            relocate_claude_session(sid, "/src", "/dst", on_committed=cb)
        # Source intact.
        assert src_jsonl.exists()
        # Destination *is* committed (callback fires after commit).  This
        # is the "recoverable duplicate" state documented on the helper.
        dst = fake_projects_root / slugify("/dst") / f"{sid}.jsonl"
        assert dst.exists()


# --------------------------------------------------------------------------
# Cleanup of stale tmp files
# --------------------------------------------------------------------------

class TestStaleTempCleanup:
    def test_clears_orphan_tmp_jsonl(self, fake_projects_root: Path):
        # Simulate a previous crashed run with the SAME PID that left a
        # ``.leap-relocate-tmp`` at the destination.  Tmp paths now
        # include ``os.getpid()`` so this collision only happens after
        # PID recycling — but when it does, the helper must not be
        # tripped by the leftover file.
        sid = "tmp-orphan-uuid"
        src_jsonl, _ = _make_session(fake_projects_root, "/src", sid)
        dst_proj = fake_projects_root / slugify("/dst")
        dst_proj.mkdir(parents=True)
        orphan = dst_proj / f"{sid}.jsonl.{os.getpid()}.leap-relocate-tmp"
        orphan.write_bytes(b"stale")
        new_path = relocate_claude_session(sid, "/src", "/dst")
        assert not orphan.exists()
        assert Path(new_path).exists()


# --------------------------------------------------------------------------
# Defensive validation
# --------------------------------------------------------------------------

class TestSessionIdValidation:
    @pytest.mark.parametrize("bad_id", [
        "../escape",
        "with/slash",
        "with space",
        "-leading-dash",  # _SAFE_SESSION_ID_RE requires alphanumeric start
        "",
    ])
    def test_rejects_unsafe_session_ids(self, fake_projects_root: Path, bad_id: str):
        # Even when the on-disk slug exists, a crafted session id must
        # never be allowed to compute a path that joins outside the
        # project dir.
        proj = fake_projects_root / slugify("/any")
        proj.mkdir()
        if bad_id:  # empty triggers the "missing session id" branch
            err_match = "session id has unexpected format"
        else:
            err_match = "missing session id"
        with pytest.raises(RelocationError, match=err_match):
            relocate_claude_session(bad_id, "/any", "/other")


# --------------------------------------------------------------------------
# Sidecar with symlinks — copytree(symlinks=False) materializes the
# target, and our verifier must accept that as identical content.
# --------------------------------------------------------------------------

class TestSidecarSymlinks:
    def test_relocate_with_symlink_in_sidecar(self, fake_projects_root: Path, tmp_path: Path):
        # Build a sidecar where one of the files is a symlink to an
        # external file.  copytree resolves it; verification must
        # accept the materialized regular file as a match.
        target = tmp_path / "external.txt"
        target.write_bytes(b"target-content\n")
        src_cwd = "/sym/src"
        dst_cwd = "/sym/dst"
        sid = "uuid-with-symlink"

        proj = fake_projects_root / slugify(src_cwd)
        proj.mkdir(parents=True)
        (proj / f"{sid}.jsonl").write_bytes(b"main-transcript\n")
        sidecar = proj / sid
        sidecar.mkdir()
        (sidecar / "regular.txt").write_bytes(b"regular-content\n")
        os.symlink(str(target), str(sidecar / "linked.txt"))

        new_path = relocate_claude_session(sid, src_cwd, dst_cwd)

        dst_dir = Path(new_path).parent / sid
        # Both files are present on dst, with the symlink resolved into
        # a regular file holding the target's bytes.
        assert (dst_dir / "regular.txt").read_bytes() == b"regular-content\n"
        materialized = dst_dir / "linked.txt"
        assert materialized.is_file() and not materialized.is_symlink()
        assert materialized.read_bytes() == b"target-content\n"
        # External target is untouched.
        assert target.read_bytes() == b"target-content\n"
