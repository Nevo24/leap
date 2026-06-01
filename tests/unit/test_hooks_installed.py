"""Tests for :meth:`CLIProvider.hooks_installed` across all four
built-in providers.

The check is the symmetric inverse of ``configure_hooks()`` — it
verifies both that the hook script exists in ``hook_config_dir`` AND
that the CLI's settings file references ``leap-hook.sh``.  Tests
monkey-patch ``$HOME`` to an isolated tmp dir so they never touch
the real ``~/.claude``, ``~/.codex``, ``~/.cursor``, or ``~/.gemini``.

Each provider is exercised through five cases:

1. Empty home → ``hooks_installed() == False``.
2. After running ``configure_hooks()`` → ``True``.
3. Hook script wiped, settings file kept → ``False``.
4. Settings file wiped, hook script kept → ``False``.
5. Settings file corrupt (invalid JSON / TOML) → ``False`` (no raise).

For Codex specifically: a sixth case verifies that without the
``codex_hooks`` feature flag in ``config.toml`` we treat it as
"not installed" (Codex 0.121+ silently ignores ``hooks.json``
otherwise — better to flag it loudly via the gate).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, Iterator

import pytest

from leap.cli_providers import cursor_agent as cursor_mod
from leap.cli_providers.claude import ClaudeProvider
from leap.cli_providers.codex import CodexProvider
from leap.cli_providers.cursor_agent import CursorAgentProvider
from leap.cli_providers.gemini import GeminiProvider


# --------------------------------------------------------------------------
# Fixture: isolated $HOME so providers see an empty config dir tree
# --------------------------------------------------------------------------

@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point $HOME at a tmp dir.  Re-imports module-level constants in
    each provider so they re-evaluate ``Path.home()``.

    The four providers cache their config-dir constants at import
    time (``CODEX_CONFIG_DIR``, ``GEMINI_SETTINGS_FILE``, etc.).
    Monkey-patching them is the cleanest way to redirect file I/O
    without touching the provider source.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    # Patch every cached constant to point at the new home.
    from leap.cli_providers import codex as codex_mod
    from leap.cli_providers import cursor_agent as cursor_mod
    from leap.cli_providers import gemini as gemini_mod

    monkeypatch.setattr(codex_mod, "CODEX_CONFIG_DIR", tmp_path / ".codex")
    monkeypatch.setattr(codex_mod, "CODEX_HOOKS_FILE", tmp_path / ".codex" / "hooks.json")
    monkeypatch.setattr(cursor_mod, "CURSOR_CONFIG_DIR", tmp_path / ".cursor")
    monkeypatch.setattr(cursor_mod, "CURSOR_HOOKS_FILE", tmp_path / ".cursor" / "hooks.json")
    monkeypatch.setattr(gemini_mod, "GEMINI_CONFIG_DIR", tmp_path / ".gemini")
    monkeypatch.setattr(gemini_mod, "GEMINI_SETTINGS_FILE", tmp_path / ".gemini" / "settings.json")

    yield tmp_path


# Path inside the source tree — used as the hook script source for
# configure_hooks(); we don't actually run the hook, just install its
# path into settings files.
_REPO_HOOK_SCRIPT = (
    Path(__file__).resolve().parents[2] / "src" / "scripts" / "leap-hook.sh"
)


def _install_hook_script(provider, isolated_home: Path) -> Path:
    """Copy the repo's leap-hook.sh into the provider's hook_config_dir
    so configure_hooks() can install a settings file that references
    a real on-disk path.  Returns the destination path."""
    hook_dir = provider.hook_config_dir
    hook_dir.mkdir(parents=True, exist_ok=True)
    dest = hook_dir / "leap-hook.sh"
    dest.write_text(_REPO_HOOK_SCRIPT.read_text())
    dest.chmod(0o755)
    return dest


# --------------------------------------------------------------------------
# Generic per-provider test parametrisation
# --------------------------------------------------------------------------

PROVIDERS = [
    pytest.param(ClaudeProvider, id="claude"),
    pytest.param(CodexProvider, id="codex"),
    pytest.param(CursorAgentProvider, id="cursor-agent"),
    pytest.param(GeminiProvider, id="gemini"),
]


@pytest.mark.parametrize("provider_cls", PROVIDERS)
def test_empty_home_returns_false(provider_cls, isolated_home: Path) -> None:
    provider = provider_cls()
    assert provider.hooks_installed() is False


@pytest.mark.parametrize("provider_cls", PROVIDERS)
def test_after_configure_hooks_returns_true(
    provider_cls, isolated_home: Path
) -> None:
    provider = provider_cls()
    dest = _install_hook_script(provider, isolated_home)
    provider.configure_hooks(str(dest))
    assert provider.hooks_installed() is True


@pytest.mark.parametrize("provider_cls", PROVIDERS)
def test_hook_script_wiped_returns_false(
    provider_cls, isolated_home: Path
) -> None:
    provider = provider_cls()
    dest = _install_hook_script(provider, isolated_home)
    provider.configure_hooks(str(dest))
    assert provider.hooks_installed() is True
    dest.unlink()
    assert provider.hooks_installed() is False


@pytest.mark.parametrize(
    "provider_cls,settings_relpath",
    [
        (ClaudeProvider, ".claude/settings.json"),
        (CodexProvider, ".codex/hooks.json"),
        (CursorAgentProvider, ".cursor/hooks.json"),
        (GeminiProvider, ".gemini/settings.json"),
    ],
    ids=["claude", "codex", "cursor-agent", "gemini"],
)
def test_settings_file_wiped_returns_false(
    provider_cls, settings_relpath: str, isolated_home: Path
) -> None:
    provider = provider_cls()
    dest = _install_hook_script(provider, isolated_home)
    provider.configure_hooks(str(dest))
    assert provider.hooks_installed() is True
    (isolated_home / settings_relpath).unlink()
    assert provider.hooks_installed() is False


@pytest.mark.parametrize(
    "provider_cls,settings_relpath",
    [
        (ClaudeProvider, ".claude/settings.json"),
        (CodexProvider, ".codex/hooks.json"),
        (CursorAgentProvider, ".cursor/hooks.json"),
        (GeminiProvider, ".gemini/settings.json"),
    ],
    ids=["claude", "codex", "cursor-agent", "gemini"],
)
def test_corrupt_settings_returns_false_not_raise(
    provider_cls, settings_relpath: str, isolated_home: Path
) -> None:
    provider = provider_cls()
    dest = _install_hook_script(provider, isolated_home)
    provider.configure_hooks(str(dest))
    # Replace settings file with garbage — must NOT raise.
    settings_path = isolated_home / settings_relpath
    settings_path.write_text("{not valid json")
    assert provider.hooks_installed() is False


# --------------------------------------------------------------------------
# Defensive: weird-but-valid-JSON shapes in settings files must not raise.
# The session-start gate calls hooks_installed() on every server start, so
# a TypeError or KeyError here would crash the user's session with no clear
# remediation.  Returning False is the correct behaviour — the gate then
# fires its friendly error pointing at `leap --reconfigure`.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "provider_cls,settings_relpath,corrupt_payload",
    [
        # Claude: command field is an int (non-string) — `in` on int raises.
        (
            ClaudeProvider, ".claude/settings.json",
            {"hooks": {"Stop": [{"hooks": [{"command": 42}]}]}},
        ),
        # Codex: hooks list at the entry level is a string instead of list.
        (
            CodexProvider, ".codex/hooks.json",
            {"hooks": {"Stop": [{"hooks": "not-a-list"}]}},
        ),
        # Cursor: command field is None.
        (
            CursorAgentProvider, ".cursor/hooks.json",
            {"version": 1, "hooks": {"stop": [{"command": None}]}},
        ),
        # Gemini: top-level hooks is a list instead of dict.
        (
            GeminiProvider, ".gemini/settings.json",
            {"hooks": ["this should be a dict"]},
        ),
    ],
    ids=["claude-int-command", "codex-string-hooks", "cursor-none-command", "gemini-list-hooks"],
)
def test_weird_but_valid_json_returns_false_not_raise(
    provider_cls, settings_relpath, corrupt_payload, isolated_home: Path
) -> None:
    """Settings files written by a third party (or hand-edited) might
    have valid JSON but the wrong shape.  ``hooks_installed()`` must
    cope without raising — the gate would otherwise crash with a
    TypeError instead of pointing the user at ``leap --reconfigure``.
    """
    import json as _json

    provider = provider_cls()
    # Install hook script so the first half of the check passes.
    hook_dir = provider.hook_config_dir
    hook_dir.mkdir(parents=True, exist_ok=True)
    (hook_dir / "leap-hook.sh").write_text("#!/bin/sh\n")

    settings_path = isolated_home / settings_relpath
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(_json.dumps(corrupt_payload))

    # Codex extra-needs the feature flag in config.toml.
    if provider_cls is CodexProvider:
        (isolated_home / ".codex" / "config.toml").write_text(
            "[features]\ncodex_hooks = true\n"
        )

    # Must return False, must not raise.
    assert provider.hooks_installed() is False


# --------------------------------------------------------------------------
# Codex-specific: missing feature flag in config.toml → False
# --------------------------------------------------------------------------

def test_codex_missing_feature_flag_returns_false(isolated_home: Path) -> None:
    """Codex 0.121+ silently ignores hooks.json without
    ``codex_hooks = true`` in config.toml — gate must catch this."""
    provider = CodexProvider()
    dest = _install_hook_script(provider, isolated_home)
    provider.configure_hooks(str(dest))
    assert provider.hooks_installed() is True

    # Wipe the feature flag from config.toml.  configure_hooks() puts
    # it under ``[features]``; we just truncate the whole file.
    config_toml = isolated_home / ".codex" / "config.toml"
    config_toml.write_text("# no feature flag here\n")
    assert provider.hooks_installed() is False


# --------------------------------------------------------------------------
# base_type defaults — built-in providers return their own name so the
# session-start gate's ``get_provider(provider.base_type).hooks_installed()``
# resolves to themselves.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "provider_cls,expected",
    [
        (ClaudeProvider, "claude"),
        (CodexProvider, "codex"),
        (CursorAgentProvider, "cursor-agent"),
        (GeminiProvider, "gemini"),
    ],
)
def test_built_in_providers_base_type_is_own_name(
    provider_cls, expected: str
) -> None:
    assert provider_cls().base_type == expected


# --------------------------------------------------------------------------
# CustomCLIProvider delegation — a custom Claude wrapper inherits the
# base's hooks_installed() result via __getattribute__ delegation.
# --------------------------------------------------------------------------

def test_custom_cli_provider_inherits_hooks_installed(
    isolated_home: Path,
) -> None:
    """A custom CLI wrapping ClaudeProvider should report
    ``hooks_installed()`` based on the Claude base's state, not its
    own.  This is the entire reason custom providers don't need to
    implement the method themselves.
    """
    from leap.cli_providers.registry import CustomCLIProvider

    base = ClaudeProvider()
    custom = CustomCLIProvider(
        custom_id="my-claude-wrapper",
        base_provider=base,
        custom_display_name="My Claude Wrapper",
    )

    # Empty home — both should be False.
    assert custom.hooks_installed() is False
    assert base.hooks_installed() is False

    # Install hooks for the base (via the custom — they share storage).
    dest = _install_hook_script(base, isolated_home)
    base.configure_hooks(str(dest))

    # Custom now reports True too, via delegation.
    assert base.hooks_installed() is True
    assert custom.hooks_installed() is True

    # base_type — custom returns the base's name (delegation), not
    # its own custom id.
    assert custom.base_type == "claude"


# --------------------------------------------------------------------------
# Claude PermissionRequest hook — canonical auto-approve path that bypasses
# the dialog entirely.  Must be present in settings.json with the negative-
# lookahead matcher that EXCLUDES AskUserQuestion (auto-approving that one
# tells Claude to skip user interaction, and the tool returns an empty
# answer set — corrupting the very flow the user invoked it for).
# --------------------------------------------------------------------------

_PR_MATCHER: Final = "^(?!AskUserQuestion$).*"


def test_claude_configure_hooks_installs_permission_request(
    isolated_home: Path,
) -> None:
    """Verify Claude's configure_hooks() writes a PermissionRequest entry
    with the AskUserQuestion-excluding matcher and command ending in
    ``auto_approve``.  This is the load-bearing hook that fixes the
    multi-agent subagent auto-approve gap — its absence would silently
    revert behaviour to the TUI-menu path (which can lose Notification
    signals during sustained RUNNING).
    """
    provider = ClaudeProvider()
    dest = _install_hook_script(provider, isolated_home)
    provider.configure_hooks(str(dest))

    settings_path = isolated_home / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    pr_entries = settings.get("hooks", {}).get("PermissionRequest", [])
    assert pr_entries, "PermissionRequest hook missing from settings.json"

    # Find the Leap entry (defends against other hooks coexisting).
    leap_entries = [
        e for e in pr_entries
        if any(
            "leap-hook.sh" in (h.get("command") or "")
            for h in e.get("hooks", [])
        )
    ]
    assert len(leap_entries) == 1, (
        f"expected one Leap PermissionRequest entry, got {len(leap_entries)}"
    )
    entry = leap_entries[0]
    assert entry.get("matcher") == _PR_MATCHER, (
        f"PermissionRequest matcher must exclude AskUserQuestion "
        f"({_PR_MATCHER!r}), got {entry.get('matcher')!r}"
    )
    cmd = entry["hooks"][0]["command"]
    assert cmd.endswith(" auto_approve"), (
        f"PermissionRequest command must end with ' auto_approve', got {cmd!r}"
    )


def test_claude_permission_request_matcher_excludes_ask_user_question() -> None:
    """The matcher MUST match every standard tool name (Bash, Edit, Write,
    Task, MCP-namespaced tools, etc.) and reject ONLY ``AskUserQuestion``.

    Auto-approving AskUserQuestion tells Claude to skip user interaction,
    and the tool then returns an empty answer set ("Allowed by
    PermissionRequest hook" with no selections) — which corrupts the very
    flow the user invoked it for.  Pin the negative-lookahead behaviour
    here so a future regex tweak can't silently re-enable it.

    Uses Python's ``re`` as a stand-in for JavaScript regex — both
    support ``^``, negative lookahead ``(?!...)``, and ``.*`` identically
    for the syntactic subset we use here.
    """
    import re
    pattern = re.compile(_PR_MATCHER)
    must_match = [
        "Bash", "Edit", "Write", "Read", "MultiEdit", "Task",
        "Grep", "Glob", "WebFetch", "WebSearch", "TodoWrite",
        "ExitPlanMode", "NotebookEdit",
        "mcp__memory__store", "mcp__github__create_issue",
    ]
    for tool in must_match:
        assert pattern.match(tool), (
            f"matcher {_PR_MATCHER!r} unexpectedly REJECTED {tool!r} — "
            f"auto-approve will no longer fire for it"
        )
    assert not pattern.match("AskUserQuestion"), (
        f"matcher {_PR_MATCHER!r} unexpectedly ACCEPTED 'AskUserQuestion' — "
        f"auto-approving it makes the tool return an empty answer set"
    )
    # Defensive: also reject things that just CONTAIN AskUserQuestion as
    # a prefix or substring (we only want exact-name exclusion).  These
    # should match because they aren't literally the same tool name.
    assert pattern.match("AskUserQuestionX"), (
        f"matcher unexpectedly rejected 'AskUserQuestionX' — the "
        f"exclusion should be exact-name only"
    )
    assert pattern.match("MyAskUserQuestion"), (
        f"matcher unexpectedly rejected 'MyAskUserQuestion' — the "
        f"exclusion should be exact-name only"
    )


def test_claude_configure_hooks_is_idempotent_for_permission_request(
    isolated_home: Path,
) -> None:
    """Running configure_hooks() twice must not duplicate the
    PermissionRequest entry.  The ``upsert`` helper strips existing
    leap-hook.sh references before re-adding — without this property,
    every ``make update`` / ``leap --reconfigure`` would accumulate
    stale entries that all fire in parallel.
    """
    provider = ClaudeProvider()
    dest = _install_hook_script(provider, isolated_home)
    provider.configure_hooks(str(dest))
    provider.configure_hooks(str(dest))

    settings = json.loads(
        (isolated_home / ".claude" / "settings.json").read_text(),
    )
    pr_entries = [
        e for e in settings.get("hooks", {}).get("PermissionRequest", [])
        if any(
            "leap-hook.sh" in (h.get("command") or "")
            for h in e.get("hooks", [])
        )
    ]
    assert len(pr_entries) == 1


@pytest.mark.parametrize(
    "provider_cls",
    [CodexProvider, CursorAgentProvider, GeminiProvider],
    ids=["codex", "cursor-agent", "gemini"],
)
def test_other_providers_do_not_install_permission_request(
    provider_cls, isolated_home: Path,
) -> None:
    """Codex / Cursor / Gemini don't have subagents and don't expose a
    PermissionRequest-equivalent hook.  The fix is Claude-only; make sure
    we don't accidentally inject PermissionRequest entries into the other
    CLIs' settings (Codex would silently ignore unknown event names, but
    Gemini's stricter schema would reject the whole file).
    """
    provider = provider_cls()
    dest = _install_hook_script(provider, isolated_home)
    provider.configure_hooks(str(dest))

    # Each CLI uses a different settings file path/format — read raw.
    settings_paths = {
        "codex": isolated_home / ".codex" / "hooks.json",
        "cursor-agent": isolated_home / ".cursor" / "hooks.json",
        "gemini": isolated_home / ".gemini" / "settings.json",
    }
    raw = settings_paths[provider.name].read_text()
    assert "PermissionRequest" not in raw, (
        f"{provider.name} configure_hooks must not write a "
        f"PermissionRequest entry (Claude-only)"
    )


# --------------------------------------------------------------------------
# Cursor Agent: configure_hooks must survive a malformed hooks.json instead
# of crashing the whole `leap --reconfigure` / `make install` run.
# --------------------------------------------------------------------------


def test_cursor_configure_hooks_survives_non_dict_root(
    isolated_home: Path,
) -> None:
    """A hooks.json whose root is a JSON list (hand-edited / future schema)
    must not raise — it should be rewritten into a valid dict shape with
    our hook installed."""
    provider = CursorAgentProvider()
    dest = _install_hook_script(provider, isolated_home)
    cursor_mod.CURSOR_HOOKS_FILE.write_text("[]")  # non-dict root
    provider.configure_hooks(str(dest))  # must not raise
    assert provider.hooks_installed() is True
    data = json.loads(cursor_mod.CURSOR_HOOKS_FILE.read_text())
    assert isinstance(data, dict)
    assert isinstance(data.get("hooks"), dict)


def test_cursor_configure_hooks_survives_garbage_stop_entries(
    isolated_home: Path,
) -> None:
    """Non-dict junk in hooks.stop (and a non-list stop) must be tolerated:
    foreign dict entries preserved, junk dropped, our hook appended."""
    provider = CursorAgentProvider()
    dest = _install_hook_script(provider, isolated_home)
    cursor_mod.CURSOR_HOOKS_FILE.write_text(json.dumps({
        "version": 1,
        "hooks": {"stop": ["junk", 123, {"command": "/usr/bin/foo bar"}]},
    }))
    provider.configure_hooks(str(dest))  # must not raise
    assert provider.hooks_installed() is True
    data = json.loads(cursor_mod.CURSOR_HOOKS_FILE.read_text())
    cmds = [e.get("command", "") for e in data["hooks"]["stop"]
            if isinstance(e, dict)]
    assert any("/usr/bin/foo bar" in c for c in cmds)   # foreign kept
    assert any("leap-hook.sh" in c for c in cmds)        # ours appended


def test_cursor_configure_hooks_survives_non_dict_hooks_value(
    isolated_home: Path,
) -> None:
    """``hooks`` present but not a dict must be coerced, not indexed-into."""
    provider = CursorAgentProvider()
    dest = _install_hook_script(provider, isolated_home)
    cursor_mod.CURSOR_HOOKS_FILE.write_text(json.dumps(
        {"version": 1, "hooks": "oops"}
    ))
    provider.configure_hooks(str(dest))  # must not raise
    assert provider.hooks_installed() is True
