"""Integration tests for the ``leap --resume`` mechanism.

The resume pipeline crosses four real processes and several file-system
hand-offs:

1. The **CLI** fires a hook (Stop / Notification / SessionStart) which
   runs ``leap-hook.sh`` → ``leap-hook-process.py``.  The hook processor
   writes ``.storage/cli_sessions/<cli>/<tag>.json`` via
   :func:`leap.utils.resume_store.record_session`.
2. The user runs ``leap --resume``; ``leap-main.sh`` dispatches to
   ``leap-resume.py`` which reads every ``cli_sessions/<cli>/*.json``,
   shows a picker, and ``execvpe``'s back into ``leap-main.sh <tag>``
   with ``LEAP_RESUME_SESSION_ID`` / ``LEAP_RESUME_CLI`` exported.
3. ``leap-main.sh`` forwards those env vars to ``leap-server.py``.
4. ``leap-server.main()`` pops the env vars, asks the provider for
   :meth:`CLIProvider.resume_args`, prepends them to the CLI argv, and
   spawns the CLI — so ``claude --resume=<uuid>`` or
   ``codex resume <uuid>`` starts talking to the right session.

The unit tests under ``tests/unit/test_resume.py`` cover each function
in isolation with stubs.  These integration tests exercise the
cross-process boundaries end-to-end: real ``subprocess`` invocations of
the hook processor, real ``leap-server.main()`` wired up to a stubbed
``LeapServer``, real ``leap-resume.main()`` driven against a tmp
storage dir, and the PPID-walk fallback that ``leap-hook.sh`` uses
when a CLI strips env vars from hook subprocesses.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Optional

import pytest

from leap.cli_providers.registry import (
    _PROVIDERS,
    reload_custom_clis,
)
from leap.utils.resume_store import (
    MAX_ENTRIES_PER_TAG,
    load_tag_rows,
    record_session,
)


REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent
SRC_DIR: Path = REPO_ROOT / "src"
HOOK_PROCESS_SCRIPT: Path = SRC_DIR / "scripts" / "leap-hook-process.py"
HOOK_SH_SCRIPT: Path = SRC_DIR / "scripts" / "leap-hook.sh"
LEAP_RESUME_SCRIPT: Path = SRC_DIR / "scripts" / "leap-resume.py"
LEAP_MAIN_SH: Path = SRC_DIR / "scripts" / "leap-main.sh"


# ===========================================================================
# Helpers
# ===========================================================================


def _subprocess_env(extra: Optional[dict] = None) -> dict:
    """A clean env with PYTHONPATH pointed at src/ so subprocesses can
    ``import leap.*``.  Inherits PATH so ``python3`` / ``bash`` resolve.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C"),
        "PYTHONPATH": str(SRC_DIR),
    }
    if extra:
        env.update(extra)
    return env


def _invoke_hook_process(
    *,
    tag: str,
    cli: str,
    storage_dir: Path,
    state: str = "idle",
    hook_payload: Optional[dict] = None,
    stdin: Optional[str] = None,
    stdin_delay: Optional[float] = None,
    env_overrides: Optional[dict] = None,
    timeout: float = 15.0,
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run ``leap-hook-process.py`` as a real subprocess.

    Mirrors how ``leap-hook.sh`` invokes the processor in production.
    Returns ``(completed_process, signal_file_path)``.
    """
    socket_dir = storage_dir / "sockets"
    socket_dir.mkdir(parents=True, exist_ok=True)
    signal_file = socket_dir / f"{tag}.signal"

    env = _subprocess_env({
        "LEAP_TAG": tag,
        "LEAP_SIGNAL_DIR": str(socket_dir),
        "LEAP_CLI_PROVIDER": cli,
    })
    if env_overrides is not None:
        for k, v in env_overrides.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v

    if stdin is None:
        stdin_bytes = json.dumps(hook_payload or {})
    else:
        stdin_bytes = stdin

    if stdin_delay is not None:
        # Hold stdin open past the processor's 5 s internal timeout to
        # verify it bails rather than hanging forever.  The processor's
        # stdin reader is a daemon thread joined with a 5 s deadline —
        # after that it gives up, writes the enriched signal, prints
        # ``{}``, and exits.  Our write into the pipe after that point
        # races the process exit: BrokenPipeError here is the expected
        # confirmation that the process has already moved on.
        proc = subprocess.Popen(
            [sys.executable, str(HOOK_PROCESS_SCRIPT), state, str(signal_file)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            time.sleep(stdin_delay)
            try:
                proc.stdin.write(stdin_bytes.encode())
            except (BrokenPipeError, OSError):
                pass
            # communicate() flushes stdin itself, so DON'T pre-close it.
            # Pass empty input (we've already written what we want) —
            # communicate will close the pipe and then drain stdout/stderr.
            try:
                out, err = proc.communicate(input=b'', timeout=timeout)
            except ValueError:
                # stdin is already closed/flushed by our write above
                # (e.g. BrokenPipe).  Fall back to wait + read.
                out, err = proc.stdout.read(), proc.stderr.read()
                proc.wait(timeout=timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.stdout.read() if proc.stdout else b''
            err = proc.stderr.read() if proc.stderr else b''
            returncode = -1
        result = subprocess.CompletedProcess(
            args=proc.args,
            returncode=returncode,
            stdout=out.decode(errors='replace'),
            stderr=err.decode(errors='replace'),
        )
    else:
        result = subprocess.run(
            [sys.executable, str(HOOK_PROCESS_SCRIPT), state, str(signal_file)],
            input=stdin_bytes,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    return result, signal_file


def _load_records(storage_dir: Path, cli: str, tag: str) -> list[dict]:
    """Return the on-disk ``cli_sessions/<cli>/<tag>.json`` list."""
    path = storage_dir / "cli_sessions" / cli / f"{tag}.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text())


def _load_picker_module(storage_dir: Path):
    """Load ``leap-resume.py`` with module-level paths redirected at a
    tmp storage dir.  Used by tests that drive ``main()`` in-process.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "leap_resume_integration", LEAP_RESUME_SCRIPT,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STORAGE_DIR = storage_dir
    mod.SOCKET_DIR = storage_dir / "sockets"
    mod.SOCKET_DIR.mkdir(parents=True, exist_ok=True)
    return mod


# ===========================================================================
# 1. Hook processor subprocess → records
# ===========================================================================


class TestHookProcessRecordsClaude:
    """End-to-end: Claude hook fires → JSONL transcript path → session
    recorded under ``cli_sessions/claude/<tag>.json``.
    """

    def test_records_from_transcript_path(self, tmp_path):
        transcript = tmp_path / ".claude/projects/myproj/abc-def-1234.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
        )
        result, _ = _invoke_hook_process(
            tag="claudetag", cli="claude", storage_dir=tmp_path,
            hook_payload={
                "transcript_path": str(transcript),
                "cwd": str(tmp_path),
            },
        )
        assert result.returncode == 0, result.stderr
        records = _load_records(tmp_path, "claude", "claudetag")
        assert len(records) == 1
        assert records[0]["session_id"] == "abc-def-1234"
        assert records[0]["transcript_path"] == str(transcript)
        assert records[0]["cwd"] == str(tmp_path)
        assert records[0]["last_seen"] > 0

    def test_records_session_even_without_transcript_file(self, tmp_path):
        """A hook may fire before the CLI finishes writing its first
        turn — the record should still land so the picker can show
        *something*.  The picker's stale-filter will drop it later if
        the transcript never materialises.
        """
        # Path references a transcript that doesn't exist on disk.
        result, _ = _invoke_hook_process(
            tag="fresh", cli="claude", storage_dir=tmp_path,
            hook_payload={
                "transcript_path": "/Users/nobody/.claude/projects/proj/ghost.jsonl",
                "cwd": str(tmp_path),
            },
        )
        assert result.returncode == 0, result.stderr
        records = _load_records(tmp_path, "claude", "fresh")
        assert len(records) == 1
        assert records[0]["session_id"] == "ghost"


class TestHookProcessRecordsCodex:
    def test_records_from_direct_session_id_field(self, tmp_path):
        result, _ = _invoke_hook_process(
            tag="codextag", cli="codex", storage_dir=tmp_path,
            hook_payload={
                "session_id": "019d-codex-uuid",
                "transcript_path": "/Users/me/.codex/sessions/2026/rollout.jsonl",
                "cwd": str(tmp_path),
            },
        )
        assert result.returncode == 0, result.stderr
        records = _load_records(tmp_path, "codex", "codextag")
        assert len(records) == 1
        assert records[0]["session_id"] == "019d-codex-uuid"


class TestHookProcessRecordsCursor:
    def test_records_from_conversation_id(self, tmp_path):
        result, _ = _invoke_hook_process(
            tag="ctag", cli="cursor-agent", storage_dir=tmp_path,
            hook_payload={
                "conversation_id": "cursor-conv-uuid",
                "cwd": str(tmp_path),
            },
        )
        assert result.returncode == 0, result.stderr
        records = _load_records(tmp_path, "cursor-agent", "ctag")
        assert len(records) == 1
        assert records[0]["session_id"] == "cursor-conv-uuid"


class TestHookProcessRecordsGemini:
    def test_records_from_transcript_path(self, tmp_path):
        session_file = tmp_path / ".gemini/tmp/proj/chats/session-123.json"
        session_file.parent.mkdir(parents=True)
        session_file.write_text(json.dumps({
            "sessionId": "gemini-uuid-abc",
            "projectHash": "hash",
            "messages": [],
        }))
        result, _ = _invoke_hook_process(
            tag="gtag", cli="gemini", storage_dir=tmp_path,
            hook_payload={
                "transcript_path": str(session_file),
                "cwd": str(tmp_path),
            },
        )
        assert result.returncode == 0, result.stderr
        records = _load_records(tmp_path, "gemini", "gtag")
        assert len(records) == 1
        assert records[0]["session_id"] == "gemini-uuid-abc"


class TestHookProcessSignalWrites:
    """The processor's contract: write ``{"state": ...}`` into the
    signal file BEFORE it does anything slow (stdin read, transcript
    tail, session recording).  The server's state tracker polls this
    file at ~2 Hz, so delayed writes translate directly into delayed
    UI.
    """

    def test_writes_bare_signal_for_idle(self, tmp_path):
        result, signal_file = _invoke_hook_process(
            tag="sig", cli="claude", storage_dir=tmp_path,
            hook_payload={},
        )
        assert result.returncode == 0, result.stderr
        assert signal_file.is_file()
        data = json.loads(signal_file.read_text())
        assert data["state"] == "idle"

    def test_writes_state_needs_permission(self, tmp_path):
        result, signal_file = _invoke_hook_process(
            state="needs_permission",
            tag="perm", cli="claude", storage_dir=tmp_path,
            hook_payload={},
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(signal_file.read_text())["state"] == "needs_permission"

    def test_enriches_signal_with_last_assistant_message(self, tmp_path):
        # Codex passes the assistant text directly in the hook payload.
        result, signal_file = _invoke_hook_process(
            tag="lam", cli="codex", storage_dir=tmp_path,
            hook_payload={
                "session_id": "x",
                "last_assistant_message": "Here is your answer.",
            },
        )
        assert result.returncode == 0
        data = json.loads(signal_file.read_text())
        assert data["state"] == "idle"
        assert data["last_assistant_message"] == "Here is your answer."

    def test_enriches_signal_with_notification_message(self, tmp_path):
        result, signal_file = _invoke_hook_process(
            state="needs_input",
            tag="notif", cli="claude", storage_dir=tmp_path,
            hook_payload={"message": "Enter API key:"},
        )
        assert result.returncode == 0
        data = json.loads(signal_file.read_text())
        assert data["notification_message"] == "Enter API key:"

    def test_claude_tails_transcript_for_last_assistant_message(self, tmp_path):
        # Claude's provider tails the JSONL transcript for the most
        # recent assistant message.  End-to-end: write a transcript with
        # an assistant entry, fire the hook, read the signal.
        transcript = tmp_path / ".claude/projects/proj/sid-123.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            '\n'.join([
                json.dumps({"type": "user",
                            "message": {"content": [{"type": "text", "text": "hi"}]}}),
                json.dumps({"type": "assistant",
                            "message": {"content": [{"type": "text",
                                                     "text": "hello from claude"}]}}),
                "",
            ])
        )
        result, signal_file = _invoke_hook_process(
            tag="tail", cli="claude", storage_dir=tmp_path,
            hook_payload={"transcript_path": str(transcript)},
        )
        assert result.returncode == 0
        data = json.loads(signal_file.read_text())
        assert data["last_assistant_message"] == "hello from claude"


class TestHookProcessRobustness:
    """The hook must never break the CLI user's session — every failure
    mode is swallowed, every invalid payload produces at most an empty
    record and a bare signal.
    """

    def test_invalid_json_stdin_does_not_crash(self, tmp_path):
        result, signal_file = _invoke_hook_process(
            tag="badjson", cli="claude", storage_dir=tmp_path,
            stdin="{not valid json}",
        )
        assert result.returncode == 0, result.stderr
        # Signal still written at bare-minimum level
        assert json.loads(signal_file.read_text())["state"] == "idle"
        # No record since session_id couldn't be extracted
        assert _load_records(tmp_path, "claude", "badjson") == []

    def test_empty_stdin_does_not_crash(self, tmp_path):
        result, signal_file = _invoke_hook_process(
            tag="empty", cli="claude", storage_dir=tmp_path,
            stdin="",
        )
        assert result.returncode == 0, result.stderr
        assert signal_file.is_file()

    def test_non_object_json_stdin_does_not_crash(self, tmp_path):
        # A bare array / string / number passes json.loads but isn't
        # a dict — the processor defends against that.
        result, _ = _invoke_hook_process(
            tag="arr", cli="claude", storage_dir=tmp_path,
            stdin=json.dumps(["this", "is", "a", "list"]),
        )
        assert result.returncode == 0, result.stderr

    def test_unknown_cli_provider_does_not_crash(self, tmp_path):
        """A typo in LEAP_CLI_PROVIDER (or a stale pid_map pointing at a
        now-removed provider) must not crash the hook — just skip the
        record step.  The signal still gets written because the state
        write happens before provider lookup.
        """
        result, signal_file = _invoke_hook_process(
            tag="t", cli="this-cli-does-not-exist",
            storage_dir=tmp_path,
            hook_payload={"session_id": "x"},
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(signal_file.read_text())["state"] == "idle"
        assert not (tmp_path / "cli_sessions" / "this-cli-does-not-exist").exists()

    def test_missing_leap_tag_skips_recording(self, tmp_path):
        result, signal_file = _invoke_hook_process(
            tag="t", cli="claude", storage_dir=tmp_path,
            hook_payload={"transcript_path":
                          "/Users/x/.claude/projects/p/uuid.jsonl"},
            env_overrides={"LEAP_TAG": None},
        )
        # Hook still runs — signal file write doesn't depend on LEAP_TAG
        # (signal_file path is arg[2], not env-derived).
        assert result.returncode == 0, result.stderr
        # But no record since tag is empty
        assert not (tmp_path / "cli_sessions" / "claude").exists()

    def test_missing_cli_provider_skips_recording(self, tmp_path):
        result, signal_file = _invoke_hook_process(
            tag="t", cli="claude", storage_dir=tmp_path,
            hook_payload={"transcript_path":
                          "/Users/x/.claude/projects/p/uuid.jsonl"},
            env_overrides={"LEAP_CLI_PROVIDER": None},
        )
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "cli_sessions").exists()

    def test_hook_stdout_is_empty_json_object(self, tmp_path):
        """Gemini's hook contract expects JSON on stdout; the processor
        always emits ``{}`` to satisfy that regardless of what happened.
        """
        result, _ = _invoke_hook_process(
            tag="t", cli="claude", storage_dir=tmp_path,
            hook_payload={},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == "{}"

    def test_slow_stdin_does_not_block_past_timeout(self, tmp_path):
        """The processor has a 5 s stdin read timeout so a badly-behaved
        CLI (e.g. Codex keeping stdin open) can't hang the hook forever.
        We close stdin after 7 s — the processor should still exit
        within a couple of seconds after that (having given up on
        reading, written the signal, and printed `{}`).
        """
        t0 = time.time()
        result, signal_file = _invoke_hook_process(
            tag="slow", cli="claude", storage_dir=tmp_path,
            stdin='{"session_id": "x"}',
            stdin_delay=7.0,  # hold stdin past the 5s timeout
            timeout=20.0,
        )
        elapsed = time.time() - t0
        # The processor should have exited shortly after the timeout
        # plus our stdin-delay — capping at 15 s lets us validate it
        # didn't wait indefinitely without being flaky.
        assert elapsed < 15.0, f"processor took {elapsed:.1f}s to exit"
        assert result.returncode == 0
        # Signal file was still written (early-flush behaviour).
        assert json.loads(signal_file.read_text())["state"] == "idle"

    def test_survives_signal_file_path_in_missing_dir(self, tmp_path):
        """If the signal file's directory doesn't exist, writing it
        fails — but the hook is swallow-all so it must still exit 0.
        """
        bogus = tmp_path / "does-not-exist" / "signal"
        env = _subprocess_env({
            "LEAP_TAG": "t",
            "LEAP_SIGNAL_DIR": str(tmp_path),
            "LEAP_CLI_PROVIDER": "claude",
        })
        result = subprocess.run(
            [sys.executable, str(HOOK_PROCESS_SCRIPT), "idle", str(bogus)],
            input="{}",
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr


class TestHookProcessDedupAndCap:
    """Real-subprocess verification of upsert / cap behaviour."""

    def test_multiple_fires_same_session_id_upserts(self, tmp_path):
        transcript = tmp_path / ".claude/projects/p/same.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("x")
        payload = {"transcript_path": str(transcript), "cwd": str(tmp_path)}
        for _ in range(3):
            result, _ = _invoke_hook_process(
                tag="dedup", cli="claude", storage_dir=tmp_path,
                hook_payload=payload,
            )
            assert result.returncode == 0, result.stderr
        records = _load_records(tmp_path, "claude", "dedup")
        assert len(records) == 1, "three hook fires for same UUID should upsert"

    def test_oldest_dropped_past_cap(self, tmp_path):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x")
        # File has no .claude/projects/ segment → session_id must be
        # extracted from the hook payload's ``session_id`` field.
        # Switch to codex, which uses the direct field.
        for i in range(MAX_ENTRIES_PER_TAG + 3):
            result, _ = _invoke_hook_process(
                tag="cap", cli="codex", storage_dir=tmp_path,
                hook_payload={"session_id": f"sid-{i:02d}",
                              "transcript_path": str(transcript),
                              "cwd": str(tmp_path)},
            )
            assert result.returncode == 0, result.stderr
        records = _load_records(tmp_path, "codex", "cap")
        assert len(records) == MAX_ENTRIES_PER_TAG
        ids = [r["session_id"] for r in records]
        assert "sid-00" not in ids
        assert f"sid-{MAX_ENTRIES_PER_TAG + 2:02d}" in ids


class TestHookProcessCustomCli:
    """Custom CLI providers (user-defined wrappers from
    ``leap --manage-clis``) must flow end-to-end: their ``<custom-id>``
    lands as the directory name under ``cli_sessions/`` and the record
    is readable by ``load_tag_rows``.
    """

    def test_custom_cli_records_under_its_own_subdir(self, tmp_path):
        # Register a custom CLI by writing to the registry file and
        # reloading.  This simulates what `leap --manage-clis` does.
        from leap.utils.constants import STORAGE_DIR as real_storage
        from leap.cli_providers.registry import (
            CLI_CUSTOM_FILE, save_custom_clis,
            load_custom_clis as real_load,
        )
        # Snapshot the real list so we can restore it afterwards.
        original = real_load()
        try:
            save_custom_clis(original + [{
                "id": "claude-prod",
                "base": "claude",
                "display_name": "Claude Prod",
                "env": {"EXTRA": "1"},
            }])
            reload_custom_clis()
            assert "claude-prod" in _PROVIDERS

            transcript = tmp_path / ".claude/projects/p/cx.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text("x")

            result, _ = _invoke_hook_process(
                tag="ct", cli="claude-prod", storage_dir=tmp_path,
                hook_payload={"transcript_path": str(transcript),
                              "cwd": str(tmp_path)},
            )
            assert result.returncode == 0, result.stderr
            # Landed under custom subdir, NOT under claude/
            records = _load_records(tmp_path, "claude-prod", "ct")
            assert len(records) == 1
            assert records[0]["session_id"] == "cx"
            assert not _load_records(tmp_path, "claude", "ct")
        finally:
            save_custom_clis(original)
            reload_custom_clis()


# ===========================================================================
# 2. End-to-end: hook writes → load_tag_rows reads → picker consumes
# ===========================================================================


class TestHookToPickerPipeline:
    def test_recorded_session_surfaces_in_load_tag_rows(self, tmp_path):
        transcript = tmp_path / ".claude/projects/p/flow.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("x" * 123)
        result, _ = _invoke_hook_process(
            tag="e2e", cli="claude", storage_dir=tmp_path,
            hook_payload={"transcript_path": str(transcript),
                          "cwd": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        rows = load_tag_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0].tag == "e2e"
        assert rows[0].cli == "claude"
        assert rows[0].sessions[0].session_id == "flow"
        assert rows[0].sessions[0].size == 123

    def test_deleted_transcript_filters_row_out(self, tmp_path):
        transcript = tmp_path / ".claude/projects/p/gone.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("x")
        result, _ = _invoke_hook_process(
            tag="del", cli="claude", storage_dir=tmp_path,
            hook_payload={"transcript_path": str(transcript)},
        )
        assert result.returncode == 0
        assert len(load_tag_rows(tmp_path)) == 1
        os.unlink(transcript)
        assert load_tag_rows(tmp_path) == []

    def test_multiple_clis_coexist(self, tmp_path):
        # Claude
        claude_t = tmp_path / ".claude/projects/p/a.jsonl"
        claude_t.parent.mkdir(parents=True)
        claude_t.write_text("x")
        r, _ = _invoke_hook_process(
            tag="shared", cli="claude", storage_dir=tmp_path,
            hook_payload={"transcript_path": str(claude_t)},
        )
        assert r.returncode == 0

        # Codex (same tag!)
        codex_t = tmp_path / "codex_t.jsonl"
        codex_t.write_text("y")
        r, _ = _invoke_hook_process(
            tag="shared", cli="codex", storage_dir=tmp_path,
            hook_payload={"session_id": "codex-uuid",
                          "transcript_path": str(codex_t)},
        )
        assert r.returncode == 0

        rows = load_tag_rows(tmp_path)
        # Both CLIs get their own row for the same tag (display decision)
        assert len(rows) == 2
        assert {(r.cli, r.tag) for r in rows} == {
            ("claude", "shared"), ("codex", "shared"),
        }


# ===========================================================================
# 3. leap-server main() resume-argv wiring
# ===========================================================================


@pytest.fixture
def fake_leap_server(monkeypatch):
    """Patch ``leap.server.server.LeapServer`` with a capturing stub so
    ``main()`` can be driven end-to-end without opening a real socket.
    """
    from leap.server import server

    captured: dict = {}

    class _FakeLeapServer:
        def __init__(self, tag, flags=None, cli=None):
            captured["tag"] = tag
            captured["flags"] = list(flags) if flags is not None else []
            captured["cli"] = cli

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(server, "LeapServer", _FakeLeapServer)
    # Ensure a clean slate for each test.
    monkeypatch.delenv("LEAP_RESUME_SESSION_ID", raising=False)
    monkeypatch.delenv("LEAP_RESUME_CLI", raising=False)
    return captured


def _run_server_main(monkeypatch, argv, env=None):
    """Invoke ``leap.server.server.main()`` with mocked argv and env."""
    from leap.server import server
    monkeypatch.setattr(sys, "argv", argv)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    server.main()


class TestServerResumeArgvWiring:
    def test_resume_env_prepends_provider_args(
        self, monkeypatch, fake_leap_server,
    ):
        _run_server_main(
            monkeypatch,
            argv=["leap-server", "mytag"],
            env={"LEAP_RESUME_SESSION_ID": "abc123",
                 "LEAP_RESUME_CLI": "claude"},
        )
        assert fake_leap_server["tag"] == "mytag"
        # Claude's form is --resume=<id> (single token)
        assert fake_leap_server["flags"] == ["--resume=abc123"]
        assert fake_leap_server["cli"] == "claude"

    def test_resume_env_prepends_codex_subcommand(
        self, monkeypatch, fake_leap_server,
    ):
        # Codex uses `resume <uuid>` — two positional tokens that must
        # stay at the FRONT of argv even when user flags are present.
        _run_server_main(
            monkeypatch,
            argv=["leap-server", "t", "--verbose"],
            env={"LEAP_RESUME_SESSION_ID": "codex-uuid",
                 "LEAP_RESUME_CLI": "codex"},
        )
        assert fake_leap_server["flags"] == [
            "resume", "codex-uuid", "--verbose",
        ]
        assert fake_leap_server["cli"] == "codex"

    def test_resume_env_popped_after_consumption(
        self, monkeypatch, fake_leap_server,
    ):
        """The server pops the env vars so they don't leak into the
        CLI subprocess (where they'd cause the CLI's own env-var
        introspection to misbehave).
        """
        _run_server_main(
            monkeypatch,
            argv=["leap-server", "t"],
            env={"LEAP_RESUME_SESSION_ID": "x",
                 "LEAP_RESUME_CLI": "claude"},
        )
        assert "LEAP_RESUME_SESSION_ID" not in os.environ
        assert "LEAP_RESUME_CLI" not in os.environ

    def test_explicit_cli_matching_resume_cli_applies(
        self, monkeypatch, fake_leap_server,
    ):
        _run_server_main(
            monkeypatch,
            argv=["leap-server", "t", "--cli", "claude"],
            env={"LEAP_RESUME_SESSION_ID": "x",
                 "LEAP_RESUME_CLI": "claude"},
        )
        assert fake_leap_server["flags"] == ["--resume=x"]
        assert fake_leap_server["cli"] == "claude"

    def test_explicit_cli_mismatching_resume_cli_skips_resume(
        self, monkeypatch, fake_leap_server,
    ):
        """If the user explicitly asked for --cli=codex but the env
        says LEAP_RESUME_CLI=claude, the user's explicit choice wins:
        resume is not applied (the resumed session is for a different
        CLI).  The env vars are still popped so they can't leak.
        """
        _run_server_main(
            monkeypatch,
            argv=["leap-server", "t", "--cli=codex"],
            env={"LEAP_RESUME_SESSION_ID": "x",
                 "LEAP_RESUME_CLI": "claude"},
        )
        assert fake_leap_server["flags"] == []
        assert fake_leap_server["cli"] == "codex"
        assert "LEAP_RESUME_SESSION_ID" not in os.environ
        assert "LEAP_RESUME_CLI" not in os.environ

    def test_resume_infers_cli_when_not_explicit(
        self, monkeypatch, fake_leap_server,
    ):
        _run_server_main(
            monkeypatch,
            argv=["leap-server", "t"],
            env={"LEAP_RESUME_SESSION_ID": "u",
                 "LEAP_RESUME_CLI": "gemini"},
        )
        # --cli wasn't passed, so the resume cli becomes the cli
        assert fake_leap_server["cli"] == "gemini"
        assert fake_leap_server["flags"] == ["--resume", "u"]

    def test_unknown_resume_cli_is_ignored(
        self, monkeypatch, fake_leap_server,
    ):
        _run_server_main(
            monkeypatch,
            argv=["leap-server", "t"],
            env={"LEAP_RESUME_SESSION_ID": "x",
                 "LEAP_RESUME_CLI": "does-not-exist"},
        )
        # No flags added; cli stays unset (defaults to provider lookup
        # in LeapServer, which we've stubbed)
        assert fake_leap_server["flags"] == []
        assert fake_leap_server["cli"] is None

    def test_resume_id_without_cli_is_ignored(
        self, monkeypatch, fake_leap_server,
    ):
        _run_server_main(
            monkeypatch,
            argv=["leap-server", "t"],
            env={"LEAP_RESUME_SESSION_ID": "x"},
        )
        assert fake_leap_server["flags"] == []

    def test_resume_cli_without_id_is_ignored(
        self, monkeypatch, fake_leap_server,
    ):
        _run_server_main(
            monkeypatch,
            argv=["leap-server", "t"],
            env={"LEAP_RESUME_CLI": "claude"},
        )
        assert fake_leap_server["flags"] == []

    def test_no_resume_env_leaves_flags_unchanged(
        self, monkeypatch, fake_leap_server,
    ):
        _run_server_main(
            monkeypatch,
            argv=["leap-server", "t", "--dangerously-skip-permissions"],
        )
        assert fake_leap_server["flags"] == ["--dangerously-skip-permissions"]
        assert fake_leap_server["cli"] is None

    def test_provider_without_resume_support_is_skipped(
        self, monkeypatch, fake_leap_server,
    ):
        """A custom CLI whose base provider doesn't declare
        ``supports_resume`` must not have its env-vars applied.
        (All four built-ins do support resume — we simulate the
        negative case by monkey-patching the provider's flag.)
        """
        from leap.cli_providers import claude as claude_mod
        original = claude_mod.ClaudeProvider.supports_resume
        try:
            # Replace the property at the class level for this test.
            claude_mod.ClaudeProvider.supports_resume = property(
                lambda self: False,
            )
            _run_server_main(
                monkeypatch,
                argv=["leap-server", "t"],
                env={"LEAP_RESUME_SESSION_ID": "x",
                     "LEAP_RESUME_CLI": "claude"},
            )
            assert fake_leap_server["flags"] == []
        finally:
            claude_mod.ClaudeProvider.supports_resume = original


# ===========================================================================
# 4. leap-resume.py picker main() integration
# ===========================================================================


class TestPickerMainExitCodes:
    def test_empty_storage_exits_1_with_message(
        self, tmp_path, capsys, monkeypatch,
    ):
        picker = _load_picker_module(tmp_path)
        # Force the isatty check to pass so we reach the "no rows" branch.
        monkeypatch.setattr(picker.sys.stdin, "isatty", lambda: True,
                            raising=False)
        rc = picker.main()
        assert rc == 1
        _, err = capsys.readouterr()
        assert "No resumable" in err

    def test_non_tty_stdin_exits_1(self, tmp_path, capsys, monkeypatch):
        """Picker requires an interactive terminal — pipe stdin → refuse."""
        picker = _load_picker_module(tmp_path)
        # Seed a single session so the "no rows" branch doesn't fire first.
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x")
        record_session(tmp_path, "claude", "t",
                       session_id="s", transcript_path=str(transcript))
        # Pipe stdin — not a TTY.
        monkeypatch.setattr(picker.sys.stdin, "isatty", lambda: False,
                            raising=False)
        rc = picker.main()
        assert rc == 1
        _, err = capsys.readouterr()
        assert "interactive terminal" in err.lower()

    def test_picked_session_held_by_live_owner_is_blocked(
        self, tmp_path, capsys, monkeypatch,
    ):
        """When the user picks a session that a live Leap server under
        a DIFFERENT tag is holding, the picker prints an error and
        returns 1 without exec'ing into leap-main.sh.
        """
        picker = _load_picker_module(tmp_path)
        monkeypatch.setattr(picker.sys.stdin, "isatty", lambda: True,
                            raising=False)
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x")
        # Same UUID under two tags — the picker's dedup drops the older
        # one from display, but ownership check uses RAW rows.
        record_session(tmp_path, "claude", "tag-a",
                       session_id="shared-uuid", transcript_path=str(transcript))
        time.sleep(0.01)
        record_session(tmp_path, "claude", "tag-b",
                       session_id="shared-uuid", transcript_path=str(transcript))
        # Stub the picker to pick the newest row without interactive I/O.
        rows = picker._load_tag_entries()
        assert rows  # sanity
        monkeypatch.setattr(picker, "_pick_tag", lambda rows: (rows[0], 1))
        # Mark tag-a as live — it holds the session.
        picker._live_tag_cli_map = lambda: {"tag-a": "claude"}
        picker._server_alive = lambda tag: tag == "tag-a"

        rc = picker.main()
        assert rc == 1
        _, err = capsys.readouterr()
        assert "already running under Leap tag" in err
        assert "tag-a" in err

    def test_session_with_missing_cwd_is_blocked(
        self, tmp_path, capsys, monkeypatch,
    ):
        """Claude resume relies on cwd matching (transcripts are stored
        per-cwd).  If the session's recorded cwd no longer exists,
        refuse to resume with a helpful error.
        """
        picker = _load_picker_module(tmp_path)
        monkeypatch.setattr(picker.sys.stdin, "isatty", lambda: True,
                            raising=False)
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x")
        record_session(tmp_path, "claude", "tag",
                       session_id="s", transcript_path=str(transcript),
                       cwd="/this/path/does/not/exist/xyz")
        rows = picker._load_tag_entries()
        monkeypatch.setattr(picker, "_pick_tag", lambda rows: (rows[0], 1))
        # Tag is not live — we reach the cwd check.
        picker._live_tag_cli_map = lambda: {}
        picker._server_alive = lambda tag: False

        # execvpe would run leap-main.sh; patch it so a path-check bail
        # is observable instead of actually replacing our process.
        monkeypatch.setattr(picker.os, "execvpe",
                            lambda *a, **kw: pytest.fail("should not exec"))
        rc = picker.main()
        assert rc == 1
        _, err = capsys.readouterr()
        assert "no longer exists" in err


class TestPickerEnvHandoff:
    """The picker's final step is ``os.execvpe(leap-main.sh, [tag])``
    with ``LEAP_RESUME_SESSION_ID`` / ``LEAP_RESUME_CLI`` / ``LEAP_CLI``
    exported.  We patch execvpe so we can observe the hand-off without
    actually replacing the test process.
    """

    def test_execvpe_receives_env_and_cwd(
        self, tmp_path, capsys, monkeypatch,
    ):
        # The picker calls os.chdir() before execvpe.  We restore the
        # test process's cwd in a finally so the picker's mutation
        # can't leak into later tests (the unit tests load the picker
        # module via a relative path, which would break silently).
        original_cwd = os.getcwd()
        picker = _load_picker_module(tmp_path)
        monkeypatch.setattr(picker.sys.stdin, "isatty", lambda: True,
                            raising=False)
        cwd = tmp_path / "workdir"
        cwd.mkdir()
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("x")
        record_session(tmp_path, "claude", "pickme",
                       session_id="sess-xyz", transcript_path=str(transcript),
                       cwd=str(cwd))
        rows = picker._load_tag_entries()
        monkeypatch.setattr(picker, "_pick_tag", lambda rows: (rows[0], 1))
        picker._live_tag_cli_map = lambda: {}
        picker._server_alive = lambda tag: False

        captured: dict = {}

        def fake_execvpe(cmd, argv, env):
            captured["cmd"] = cmd
            captured["argv"] = argv
            captured["env"] = env
            captured["cwd"] = os.getcwd()
            raise SystemExit(0)  # simulate exec by exiting

        monkeypatch.setattr(picker.os, "execvpe", fake_execvpe)

        try:
            with pytest.raises(SystemExit):
                picker.main()

            # leap-main.sh is invoked with the tag as argv[1]
            assert captured["argv"][-1] == "pickme"
            # Env carries the handoff vars
            assert captured["env"]["LEAP_RESUME_SESSION_ID"] == "sess-xyz"
            assert captured["env"]["LEAP_RESUME_CLI"] == "claude"
            assert captured["env"]["LEAP_CLI"] == "claude"
            # CWD chdir'd into session's recorded cwd
            assert captured["cwd"] == str(cwd)
        finally:
            os.chdir(original_cwd)


# ===========================================================================
# 5. Live Unix socket detection (real socket, not mocked)
# ===========================================================================


@pytest.fixture
def short_storage():
    """A tmp storage dir at a short path so AF_UNIX's 104-byte macOS
    path limit isn't exceeded.  pytest's default ``tmp_path`` lives
    under ``/private/var/folders/.../pytest-of-<user>/...`` which is
    ~80+ bytes before we even add ``/sockets/<tag>.sock``.
    """
    import shutil
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="lps_", dir="/tmp"))
    (d / "sockets").mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


class TestRealSocketLiveness:
    """The picker's ``_server_alive`` connects to the Unix socket at
    ``.storage/sockets/<tag>.sock`` with a 500 ms timeout.  Instead of
    stubbing, spin up a real listening socket and verify detection.

    Needs a short tmp path — AF_UNIX on macOS caps at 104 bytes, which
    pytest's default ``tmp_path`` blows past before we even append the
    sockets subdir.
    """

    def test_live_socket_detected(self, short_storage):
        picker = _load_picker_module(short_storage)
        sock_path = short_storage / "sockets" / "live.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(sock_path))
            srv.listen(1)
            assert picker._server_alive("live") is True
        finally:
            srv.close()
            sock_path.unlink(missing_ok=True)

    def test_regular_file_at_sock_path_is_not_live(self, short_storage):
        picker = _load_picker_module(short_storage)
        sockets = short_storage / "sockets"
        (sockets / "phantom.sock").write_text("not a socket")
        assert picker._server_alive("phantom") is False

    def test_missing_sock_file_is_not_live(self, short_storage):
        picker = _load_picker_module(short_storage)
        assert picker._server_alive("no-such-tag") is False

    def test_unbound_socket_file_is_not_live(self, short_storage):
        """An abandoned socket file (server died, kernel removed its
        listen endpoint but the file entry lingers on some filesystems)
        must read as dead."""
        picker = _load_picker_module(short_storage)
        sock_path = short_storage / "sockets" / "stale.sock"
        # Bind without listen — the path exists but no one is accepting.
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.close()
        # After close without listen/accept, connect should refuse —
        # the file lingers but the endpoint is gone.
        assert picker._server_alive("stale") is False
        sock_path.unlink(missing_ok=True)


# ===========================================================================
# 6. leap-hook.sh PPID-walk fallback
# ===========================================================================


class TestHookShPpidFallback:
    """When LEAP_TAG / LEAP_SIGNAL_DIR / LEAP_CLI_PROVIDER are absent
    from the hook's env (Codex strips env vars from hook subprocesses),
    ``leap-hook.sh`` walks up the PPID chain looking for
    ``<project>/.storage/pid_maps/<ppid>.json`` and recovers context
    from there.  This test fires the hook via a wrapper shell that
    writes a pid_map at its own ``$$`` and then execs the hook.
    """

    def test_recovers_context_from_pid_map(self, tmp_path):
        storage = tmp_path / ".storage"
        socket_dir = storage / "sockets"
        pid_maps = storage / "pid_maps"
        cli_sessions = storage / "cli_sessions"
        socket_dir.mkdir(parents=True)
        pid_maps.mkdir(parents=True)
        cli_sessions.mkdir(parents=True)

        # Stale-map guard: the hook requires the socket file to exist.
        (socket_dir / "recovered.sock").touch()

        wrapper = tmp_path / "wrapper.sh"
        wrapper.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            # Write a pid_map keyed on OUR pid ($$) — that's the ppid
            # the hook will walk up to find.
            cat > "{pid_maps}/$$.json" <<JSON
            {{
              "tag": "recovered",
              "signal_dir": "{socket_dir}",
              "python": "{sys.executable}",
              "cli_provider": "codex"
            }}
            JSON
            # Invoke the hook with env stripped of LEAP_TAG et al. so the
            # recovery path is the only way to find context.
            exec env \\
                -u LEAP_TAG \\
                -u LEAP_SIGNAL_DIR \\
                -u LEAP_CLI_PROVIDER \\
                LEAP_PYTHON="{sys.executable}" \\
                LEAP_PROJECT_DIR="{tmp_path}" \\
                PYTHONPATH="{SRC_DIR}" \\
                bash "{HOOK_SH_SCRIPT}" idle <<'PAYLOAD'
            {{"session_id": "recovered-uuid", "cwd": "{tmp_path}"}}
            PAYLOAD
        """))
        wrapper.chmod(0o755)

        # Inherit enough env for bash / python to find their own deps,
        # but don't pass any LEAP_* vars from the parent (the test env
        # won't have them anyway, but be explicit).
        env = _subprocess_env({"LEAP_PROJECT_DIR": str(tmp_path)})
        for k in ("LEAP_TAG", "LEAP_SIGNAL_DIR", "LEAP_CLI_PROVIDER"):
            env.pop(k, None)

        result = subprocess.run(
            ["/bin/bash", str(wrapper)],
            capture_output=True, text=True, env=env, timeout=15,
        )
        assert result.returncode == 0, (
            f"wrapper failed: stdout={result.stdout!r}\n"
            f"stderr={result.stderr!r}"
        )
        # Check that the hook recovered and recorded under codex/recovered
        records = _load_records(storage, "codex", "recovered")
        assert len(records) == 1, (
            f"record not written. stderr={result.stderr!r}\n"
            f"storage contents: {list(cli_sessions.rglob('*'))}"
        )
        assert records[0]["session_id"] == "recovered-uuid"

    def test_stale_pid_map_without_socket_is_ignored(self, tmp_path):
        """A pid_map from a server that has since died has no
        ``<tag>.sock`` file in its ``signal_dir``.  The fallback must
        skip it (walk up to find a newer live one, or give up) — otherwise
        it would attribute the hook to a long-gone session.
        """
        storage = tmp_path / ".storage"
        socket_dir = storage / "sockets"
        pid_maps = storage / "pid_maps"
        cli_sessions = storage / "cli_sessions"
        socket_dir.mkdir(parents=True)
        pid_maps.mkdir(parents=True)
        cli_sessions.mkdir(parents=True)
        # Deliberately NOT touching the .sock — the guard should reject
        # this pid_map even though the file itself is well-formed.

        wrapper = tmp_path / "w.sh"
        wrapper.write_text(textwrap.dedent(f"""\
            #!/bin/bash
            cat > "{pid_maps}/$$.json" <<JSON
            {{
              "tag": "dead-tag",
              "signal_dir": "{socket_dir}",
              "python": "{sys.executable}",
              "cli_provider": "codex"
            }}
            JSON
            exec env \\
                -u LEAP_TAG \\
                -u LEAP_SIGNAL_DIR \\
                -u LEAP_CLI_PROVIDER \\
                LEAP_PYTHON="{sys.executable}" \\
                LEAP_PROJECT_DIR="{tmp_path}" \\
                PYTHONPATH="{SRC_DIR}" \\
                bash "{HOOK_SH_SCRIPT}" idle <<'PAYLOAD'
            {{"session_id": "should-not-record"}}
            PAYLOAD
        """))
        wrapper.chmod(0o755)

        env = _subprocess_env({"LEAP_PROJECT_DIR": str(tmp_path)})
        for k in ("LEAP_TAG", "LEAP_SIGNAL_DIR", "LEAP_CLI_PROVIDER"):
            env.pop(k, None)
        result = subprocess.run(
            ["/bin/bash", str(wrapper)],
            capture_output=True, text=True, env=env, timeout=15,
        )
        # Hook script always exits 0 (non-Leap session path).
        assert result.returncode == 0
        # No recording happened because the pid_map was rejected.
        assert not (cli_sessions / "codex" / "dead-tag.json").exists()


# ===========================================================================
# 7. Prune cleanup integration
# ===========================================================================


class TestPruneCleanupIntegration:
    """``leap-main.sh`` calls ``prune_stale`` via ``python -c`` on every
    shell invocation.  This test reproduces that exact incantation and
    verifies the on-disk effect.
    """

    def test_python_c_invocation_prunes_dead_files(self, tmp_path):
        # Two rows: one live (transcript exists), one dead.
        live_t = tmp_path / "live.jsonl"
        live_t.write_text("x")
        dead_t = tmp_path / "dead.jsonl"
        dead_t.write_text("x")

        record_session(tmp_path, "claude", "alive",
                       session_id="a", transcript_path=str(live_t))
        record_session(tmp_path, "claude", "dead",
                       session_id="d", transcript_path=str(dead_t))
        dead_t.unlink()

        # Verify both files exist on disk before prune.
        assert (tmp_path / "cli_sessions" / "claude" / "alive.json").exists()
        assert (tmp_path / "cli_sessions" / "claude" / "dead.json").exists()

        # This is the exact line leap-main.sh's cleanup_dead_sockets uses.
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; from pathlib import Path; "
             "from leap.utils.resume_store import prune_stale; "
             "prune_stale(Path(sys.argv[1]))",
             str(tmp_path)],
            env=_subprocess_env(),
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "cli_sessions" / "claude" / "alive.json").exists()
        assert not (tmp_path / "cli_sessions" / "claude" / "dead.json").exists()


# ===========================================================================
# 8. leap-main.sh resume dispatch
# ===========================================================================


class TestLeapMainShResumeDispatch:
    """``leap-main.sh --resume`` dispatches to ``leap-resume.py``.  Full
    resume exec is interactive, so we verify the dispatch surface:
    running with non-TTY stdin and an empty storage dir produces the
    picker's "no resumable sessions" exit.
    """

    def test_no_sessions_reaches_picker(self, tmp_path):
        """With an empty ``cli_sessions`` dir (in the real project), the
        picker script exits 1 with the "No resumable CLI sessions" stderr.
        We invoke the picker directly to avoid spawning a full server.
        """
        env = _subprocess_env()
        # Non-TTY stdin is fine — the picker's TTY check only runs
        # when there ARE sessions.  An empty dir hits the earlier
        # "no rows" branch.
        result = subprocess.run(
            [sys.executable, str(LEAP_RESUME_SCRIPT)],
            capture_output=True, text=True,
            env=env, timeout=10,
            # We can't easily relocate the picker's STORAGE_DIR in a
            # subprocess (it's derived from __file__).  Instead, feed
            # stdin so the TTY check fires if there are real sessions,
            # or the no-rows branch fires if not.  Both exit 1.
            stdin=subprocess.DEVNULL,
        )
        assert result.returncode == 1
        # Either "No resumable" or "interactive terminal" — both are
        # picker-emitted, confirming the dispatch reached it.
        assert (
            "No resumable" in result.stderr
            or "interactive terminal" in result.stderr.lower()
        ), f"unexpected stderr: {result.stderr}"
