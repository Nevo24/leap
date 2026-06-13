"""Tests for the cursor-agent provider's context-usage support.

Covers the statusLine registration in ``~/.cursor/cli-config.json``
(``_configure_statusline`` / ``_deconfigure_statusline`` - chain
preservation, merge-not-clobber, corrupt-file safety) and the
``context_usage`` read of the ``<tag>.context`` state file the status-line
script writes.  All paths are isolated to tmp_path - the real ``~/.cursor``
is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from leap.cli_providers import cursor_agent as cursor_mod
from leap.cli_providers.cursor_agent import CursorAgentProvider


@pytest.fixture()
def isolated(tmp_path: Path,
             monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every cursor-agent config path into tmp_path."""
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir()
    monkeypatch.setattr(cursor_mod, "CURSOR_CONFIG_DIR", cursor_dir)
    monkeypatch.setattr(cursor_mod, "CURSOR_HOOKS_FILE",
                        cursor_dir / "hooks.json")
    monkeypatch.setattr(cursor_mod, "CURSOR_CLI_CONFIG_FILE",
                        cursor_dir / "cli-config.json")
    yield cursor_dir


def _hook_with_statusline(cursor_dir: Path) -> str:
    """Place the hook + statusline scripts like the installer does and
    return the hook path configure_hooks receives."""
    hook = cursor_dir / "leap-hook.sh"
    hook.write_text("#!/bin/sh\n")
    (cursor_dir / "leap-cursor-statusline.py").write_text("# script\n")
    return str(hook)


def _config(cursor_dir: Path) -> dict:
    return json.loads((cursor_dir / "cli-config.json").read_text())


# ---------------------------------------------------------------------------
# _configure_statusline
# ---------------------------------------------------------------------------

class TestConfigureStatusline:
    def test_registers_in_fresh_config(self, isolated):
        hook = _hook_with_statusline(isolated)
        CursorAgentProvider().configure_hooks(hook)
        sl = _config(isolated)["statusLine"]
        assert sl["type"] == "command"
        assert sl["command"].endswith("leap-cursor-statusline.py")

    def test_merges_into_existing_config(self, isolated):
        (isolated / "cli-config.json").write_text(json.dumps(
            {"version": 1, "model": {"modelId": "composer-2.5"},
             "permissions": {"allow": ["Shell(ls)"]}}))
        CursorAgentProvider().configure_hooks(_hook_with_statusline(isolated))
        cfg = _config(isolated)
        assert cfg["version"] == 1
        assert cfg["model"] == {"modelId": "composer-2.5"}
        assert cfg["permissions"] == {"allow": ["Shell(ls)"]}
        assert "statusLine" in cfg

    def test_preserves_user_statusline_in_chain(self, isolated):
        (isolated / "cli-config.json").write_text(json.dumps(
            {"statusLine": {"type": "command", "command": "/usr/bin/mine"}}))
        CursorAgentProvider().configure_hooks(_hook_with_statusline(isolated))
        chain = (isolated / "leap-statusline-chain").read_text()
        assert chain == "/usr/bin/mine"
        assert "leap-cursor-statusline" in _config(isolated)["statusLine"]["command"]

    def test_never_chains_to_itself(self, isolated):
        ours = str(isolated / "leap-cursor-statusline.py")
        (isolated / "cli-config.json").write_text(json.dumps(
            {"statusLine": {"type": "command", "command": ours}}))
        CursorAgentProvider().configure_hooks(_hook_with_statusline(isolated))
        assert not (isolated / "leap-statusline-chain").exists()

    def test_corrupt_config_left_untouched(self, isolated):
        (isolated / "cli-config.json").write_text("{not json")
        CursorAgentProvider().configure_hooks(_hook_with_statusline(isolated))
        # The whole CLI config (permissions, model, ...) lives in this file;
        # never clobber what we can't safely read.
        assert (isolated / "cli-config.json").read_text() == "{not json"

    def test_missing_script_is_noop(self, isolated):
        hook = isolated / "leap-hook.sh"
        hook.write_text("#!/bin/sh\n")  # no statusline copied alongside
        CursorAgentProvider().configure_hooks(str(hook))
        assert not (isolated / "cli-config.json").exists()


# ---------------------------------------------------------------------------
# _deconfigure_statusline
# ---------------------------------------------------------------------------

class TestDeconfigureStatusline:
    def test_removes_our_statusline(self, isolated):
        CursorAgentProvider().configure_hooks(_hook_with_statusline(isolated))
        CursorAgentProvider().deconfigure_hooks()
        assert "statusLine" not in _config(isolated)
        assert not (isolated / "leap-cursor-statusline.py").exists()

    def test_restores_prior_statusline(self, isolated):
        (isolated / "cli-config.json").write_text(json.dumps(
            {"statusLine": {"type": "command", "command": "/usr/bin/mine"}}))
        CursorAgentProvider().configure_hooks(_hook_with_statusline(isolated))
        CursorAgentProvider().deconfigure_hooks()
        assert _config(isolated)["statusLine"]["command"] == "/usr/bin/mine"
        assert not (isolated / "leap-statusline-chain").exists()

    def test_foreign_statusline_left_alone(self, isolated):
        (isolated / "cli-config.json").write_text(json.dumps(
            {"statusLine": {"type": "command", "command": "/usr/bin/mine"}}))
        CursorAgentProvider().deconfigure_hooks()
        assert _config(isolated)["statusLine"]["command"] == "/usr/bin/mine"


# ---------------------------------------------------------------------------
# context_usage
# ---------------------------------------------------------------------------

class TestContextUsage:
    def test_supports_context_usage(self):
        assert CursorAgentProvider().supports_context_usage is True

    def test_reads_statusline_state_file(self, tmp_path):
        sockets = tmp_path / "sockets"
        sockets.mkdir()
        (sockets / "mytag.context").write_text(json.dumps(
            {"used_tokens": 33_900, "window": 100_000,
             "model": "composer-2.5"}))
        usage = CursorAgentProvider().context_usage(
            "cursor-agent", "mytag", tmp_path)
        assert usage is not None
        assert usage.used_tokens == 33_900
        assert usage.window == 100_000
        assert usage.model == "composer-2.5"
        assert usage.percent == 34

    def test_missing_state_file_returns_none(self, tmp_path):
        (tmp_path / "sockets").mkdir()
        assert CursorAgentProvider().context_usage(
            "cursor-agent", "mytag", tmp_path) is None
