"""Tests for the ``auto_approve`` state in ``leap-hook-process.py``.

The ``PermissionRequest`` hook is the canonical auto-approve path —
when ALWAYS mode is set, the hook script must emit a
``{"behavior": "allow"}`` decision so Claude skips the dialog
entirely.  These tests pin three properties:

* mode resolution (per-tag pin → global default → ``'pause'``)
* the ALWAYS-mode decision JSON shape (Claude rejects malformed
  responses; one missing nested key and auto-approve silently fails
  for every user permission the moment they update)
* the PAUSE-mode no-op (no stdout decision = dialog renders normally)

The script lives outside the ``leap`` package (it's run by Claude's
hook subprocess against whatever ``python3`` is available), so we
load it via ``importlib`` against its file path rather than ``from
leap.scripts import ...``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import types
from pathlib import Path
from typing import Iterator

import pytest


# --------------------------------------------------------------------------
# Loader: import the dash-named hook script as a module
# --------------------------------------------------------------------------

_HOOK_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "scripts" / "leap-hook-process.py"
)


@pytest.fixture(scope="module")
def hook_mod() -> types.ModuleType:
    """Import ``leap-hook-process.py`` as a module via importlib.

    The hyphen in the filename rules out plain ``import``.  Cached at
    module scope — the script's heavy imports (``leap.cli_providers``)
    only run once across all tests in this file.
    """
    spec = importlib.util.spec_from_file_location(
        "leap_hook_process", _HOOK_SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# _resolve_auto_send_mode — per-tag pinned wins, global next, then 'pause'
# --------------------------------------------------------------------------

class TestResolveAutoSendMode:
    """Resolution order must match ``LeapServer._load_pinned_auto_send_mode``
    + ``load_settings().get('auto_send_mode')`` so the hook never disagrees
    with the live server about which mode is active."""

    def test_pinned_always_wins(
        self, hook_mod, tmp_path: Path,
    ) -> None:
        (tmp_path / "pinned_sessions.json").write_text(
            json.dumps({"mytag": {"auto_send_mode": "always"}}),
        )
        # Global says pause — must be overridden by the per-tag pin.
        (tmp_path / "settings.json").write_text(
            json.dumps({"auto_send_mode": "pause"}),
        )
        assert hook_mod._resolve_auto_send_mode("mytag", tmp_path) == "always"

    def test_pinned_pause_wins(
        self, hook_mod, tmp_path: Path,
    ) -> None:
        (tmp_path / "pinned_sessions.json").write_text(
            json.dumps({"mytag": {"auto_send_mode": "pause"}}),
        )
        (tmp_path / "settings.json").write_text(
            json.dumps({"auto_send_mode": "always"}),
        )
        assert hook_mod._resolve_auto_send_mode("mytag", tmp_path) == "pause"

    def test_falls_back_to_global_when_tag_not_pinned(
        self, hook_mod, tmp_path: Path,
    ) -> None:
        (tmp_path / "pinned_sessions.json").write_text(
            json.dumps({"othertag": {"auto_send_mode": "always"}}),
        )
        (tmp_path / "settings.json").write_text(
            json.dumps({"auto_send_mode": "always"}),
        )
        assert hook_mod._resolve_auto_send_mode("mytag", tmp_path) == "always"

    def test_falls_back_to_pause_when_neither_file_exists(
        self, hook_mod, tmp_path: Path,
    ) -> None:
        assert hook_mod._resolve_auto_send_mode("mytag", tmp_path) == "pause"

    def test_falls_back_to_pause_when_tag_entry_missing_mode(
        self, hook_mod, tmp_path: Path,
    ) -> None:
        # Pin entry exists but lacks auto_send_mode (older pin format).
        (tmp_path / "pinned_sessions.json").write_text(
            json.dumps({"mytag": {"some_other_key": "value"}}),
        )
        assert hook_mod._resolve_auto_send_mode("mytag", tmp_path) == "pause"

    def test_corrupt_pinned_file_does_not_raise(
        self, hook_mod, tmp_path: Path,
    ) -> None:
        # A half-written pinned file must NOT crash the hook —
        # auto-approve failing closed is fine; crashing the hook fails
        # Claude's permission flow.
        (tmp_path / "pinned_sessions.json").write_text("{not valid json")
        (tmp_path / "settings.json").write_text(
            json.dumps({"auto_send_mode": "always"}),
        )
        # Pinned read fails silently, settings is honoured.
        assert hook_mod._resolve_auto_send_mode("mytag", tmp_path) == "always"

    def test_corrupt_settings_file_does_not_raise(
        self, hook_mod, tmp_path: Path,
    ) -> None:
        (tmp_path / "settings.json").write_text("{not valid json")
        # Both lookups fail → default 'pause'.
        assert hook_mod._resolve_auto_send_mode("mytag", tmp_path) == "pause"

    def test_pinned_non_dict_at_root_does_not_raise(
        self, hook_mod, tmp_path: Path,
    ) -> None:
        # pinned_sessions.json that happens to be a JSON list (corrupt
        # write or a future schema migration).
        (tmp_path / "pinned_sessions.json").write_text("[]")
        assert hook_mod._resolve_auto_send_mode("mytag", tmp_path) == "pause"

    def test_pinned_entry_non_dict_does_not_raise(
        self, hook_mod, tmp_path: Path,
    ) -> None:
        # The tag's value is a string (hand-edited file).
        (tmp_path / "pinned_sessions.json").write_text(
            json.dumps({"mytag": "not-a-dict"}),
        )
        (tmp_path / "settings.json").write_text(
            json.dumps({"auto_send_mode": "always"}),
        )
        # Should skip the bad pin and fall back to global.
        assert hook_mod._resolve_auto_send_mode("mytag", tmp_path) == "always"


# --------------------------------------------------------------------------
# _handle_auto_approve — emits decision to stdout based on resolved mode
# --------------------------------------------------------------------------

class TestHandleAutoApprove:
    """The handler must:
    * emit the canonical ``{"hookSpecificOutput": {...}}`` decision JSON
      when ALWAYS, then ``sys.exit(0)`` so the trailing ``print('{}')``
      doesn't append a second JSON object after our decision (Claude
      would parse only the first and we'd lose deterministic behavior)
    * write NOTHING to the signal file (this is a hook decision, not a
      Leap state transition — touching the signal would falsely flip
      the state tracker)
    * fall through silently on PAUSE so Claude renders the dialog
    """

    @pytest.fixture
    def env_capture(
        self,
        hook_mod,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> Iterator[dict]:
        """Set up LEAP_TAG + LEAP_SIGNAL_DIR pointing at tmp_path so
        ``_handle_auto_approve`` reads from our isolated fixture files.
        Storage dir is the parent of LEAP_SIGNAL_DIR — match the
        production layout (``.storage/sockets``).
        """
        storage_dir = tmp_path
        socket_dir = storage_dir / "sockets"
        socket_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("LEAP_TAG", "mytag")
        monkeypatch.setenv("LEAP_SIGNAL_DIR", str(socket_dir))
        yield {
            "storage_dir": storage_dir,
            "socket_dir": socket_dir,
            "signal_file": socket_dir / "mytag.signal",
        }

    def test_always_mode_emits_allow_and_exits(
        self, hook_mod, env_capture, capsys: pytest.CaptureFixture[str],
    ) -> None:
        (env_capture["storage_dir"] / "pinned_sessions.json").write_text(
            json.dumps({"mytag": {"auto_send_mode": "always"}}),
        )
        with pytest.raises(SystemExit) as exc_info:
            hook_mod._handle_auto_approve()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        decision = json.loads(out)
        # Schema is load-bearing — Claude silently ignores malformed
        # responses, which would manifest as "auto-approve works
        # randomly" reports.
        assert decision == {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            },
        }
        # Critical: no signal file written.  The auto_approve path is
        # purely a hook decision; the state machine stays RUNNING.
        assert not env_capture["signal_file"].exists()

    def test_pause_mode_falls_through_no_exit_no_stdout(
        self, hook_mod, env_capture, capsys: pytest.CaptureFixture[str],
    ) -> None:
        (env_capture["storage_dir"] / "pinned_sessions.json").write_text(
            json.dumps({"mytag": {"auto_send_mode": "pause"}}),
        )
        # No SystemExit — returns normally so the script's trailing
        # ``print('{}')`` runs and tells Claude "no decision".
        hook_mod._handle_auto_approve()
        assert capsys.readouterr().out == ""
        assert not env_capture["signal_file"].exists()

    def test_missing_pin_falls_back_to_pause(
        self, hook_mod, env_capture, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # No pinned_sessions.json, no settings.json — default is pause.
        # The hook MUST fail closed (don't auto-approve) when context is
        # ambiguous, otherwise an orphaned hook from a previous Leap
        # install could silently auto-approve every Claude session.
        hook_mod._handle_auto_approve()
        assert capsys.readouterr().out == ""
        assert not env_capture["signal_file"].exists()

    def test_missing_leap_tag_skips_and_returns(
        self,
        hook_mod,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Non-Leap Claude session that happens to inherit our hook.
        # We must not crash and must not emit a decision.
        monkeypatch.delenv("LEAP_TAG", raising=False)
        monkeypatch.setenv("LEAP_SIGNAL_DIR", str(tmp_path / "sockets"))
        hook_mod._handle_auto_approve()
        assert capsys.readouterr().out == ""

    def test_missing_leap_signal_dir_skips_and_returns(
        self,
        hook_mod,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("LEAP_TAG", "mytag")
        monkeypatch.delenv("LEAP_SIGNAL_DIR", raising=False)
        hook_mod._handle_auto_approve()
        assert capsys.readouterr().out == ""

    def test_global_settings_always_works_without_pin(
        self, hook_mod, env_capture, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A user who set ALWAYS as the *global* default in
        ``settings.json`` (and never per-tag-pinned) must still get
        auto-approve — this is the most common configuration."""
        (env_capture["storage_dir"] / "settings.json").write_text(
            json.dumps({"auto_send_mode": "always"}),
        )
        with pytest.raises(SystemExit):
            hook_mod._handle_auto_approve()
        out = capsys.readouterr().out
        assert json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == "allow"
