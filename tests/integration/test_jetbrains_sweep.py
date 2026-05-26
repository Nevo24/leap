"""Regression tests for ``_jetbrains_sweep_stale_tabs``.

Yuval reported (Slack, 2026-05-25): after a full computer restart, the
``lps <tag>`` prefix stayed on JetBrains terminal tabs whose Leap servers
had died with the reboot.  Root cause: the sweep used ``os.kill(pid, 0)``
to test session liveness, which can't tell a real Leap server apart from
an unrelated process the kernel reassigned the old PID to.

The fix swaps that for a Unix-socket ``connect()`` probe — a dead server
can't be accepting connections regardless of which process holds the PID
number.  These tests pin both the helper (``_server_alive_via_socket``)
and the end-to-end sweep behaviour (``liveTags`` set in the generated
Groovy script).
"""

import json
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from leap.utils import terminal as terminal_module
from leap.utils.terminal import (
    _jetbrains_sweep_stale_tabs,
    _server_alive_via_socket,
)


@pytest.fixture
def short_storage() -> Iterator[Path]:
    """A tmp storage dir at a short path so AF_UNIX's 104-byte macOS
    path limit isn't exceeded.  Same trick as ``test_resume_pipeline``.
    """
    d = Path(tempfile.mkdtemp(prefix='lps_', dir='/tmp'))
    (d / 'sockets').mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _write_meta(
    storage: Path,
    tag: str,
    *,
    ide: str = 'GoLand',
    project_path: str = '/x/proj',
    pid: int = 12345,
) -> Path:
    """Write a session ``.meta`` file."""
    meta_path = storage / 'sockets' / f'{tag}.meta'
    meta_path.write_text(json.dumps({
        'ide': ide,
        'tag': tag,
        'project_path': project_path,
        'pid': pid,
        'terminal_title': f'lps {tag}',
    }))
    return meta_path


def _bind_listening_socket(storage: Path, tag: str) -> socket.socket:
    """Bind + listen on ``<storage>/sockets/<tag>.sock`` so the connect
    probe sees a live server.  Caller must ``.close()`` it."""
    sock_path = storage / 'sockets' / f'{tag}.sock'
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(1)
    return srv


class TestServerAliveViaSocket:
    """The helper used in place of ``os.kill(pid, 0)``."""

    def test_listening_socket_is_alive(self, short_storage: Path) -> None:
        sock_path = short_storage / 'sockets' / 'live.sock'
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(sock_path))
            srv.listen(1)
            assert _server_alive_via_socket(sock_path) is True
        finally:
            srv.close()

    def test_missing_sock_file_is_dead(self, short_storage: Path) -> None:
        sock_path = short_storage / 'sockets' / 'absent.sock'
        assert _server_alive_via_socket(sock_path) is False

    def test_regular_file_at_sock_path_is_dead(
        self, short_storage: Path,
    ) -> None:
        # A leftover with the right name but the wrong file type
        # (e.g., something else stomped on the path) must not be
        # mistaken for a live server.
        sock_path = short_storage / 'sockets' / 'phantom.sock'
        sock_path.write_text('not a socket')
        assert _server_alive_via_socket(sock_path) is False

    def test_unbound_socket_file_is_dead(
        self, short_storage: Path,
    ) -> None:
        # bind() without listen()/accept() leaves the path on disk but
        # there's nobody accepting — exactly the post-crash state.
        sock_path = short_storage / 'sockets' / 'stale.sock'
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.close()
        assert _server_alive_via_socket(sock_path) is False


def _run_sweep_capture_groovy(
    storage: Path,
    *,
    ide_name: str = 'GoLand',
    project_path: str = '/x/proj',
    exclude_tag: str = 'newtag',
) -> str:
    """Invoke ``_jetbrains_sweep_stale_tabs`` with the JetBrains CLI
    short-circuited to a fake path, and return the Groovy script that
    *would* have been executed.  The sweep's only side effect we care
    about for testing is the ``liveTags`` Set literal embedded in the
    Groovy — every other behaviour (project lookup, content rename) is
    Groovy that runs *inside* JetBrains and can't be unit-tested here.
    """
    captured: dict[str, str] = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        # argv == [cli_path, 'ideScript', tmp_path]
        tmp_path = argv[2]
        with open(tmp_path) as f:
            captured['groovy'] = f.read()
        # Return a stub result that the sweep doesn't inspect.
        return subprocess.CompletedProcess(argv, 0, b'', b'')

    with patch.object(
        terminal_module, '_resolve_jetbrains_cli',
        return_value='/fake/jetbrains/cli',
    ), patch.object(terminal_module.subprocess, 'run', side_effect=fake_run):
        _jetbrains_sweep_stale_tabs(
            storage, ide_name, project_path, exclude_tag=exclude_tag,
        )
    return captured.get('groovy', '')


class TestSweepStaleTabsPidReuse:
    """End-to-end: meta exists, but the recorded PID is either dead OR
    reassigned to an unrelated process (the post-reboot scenario)."""

    def test_dead_session_with_listening_socket_is_kept(
        self, short_storage: Path,
    ) -> None:
        # Baseline: live socket means tag stays in liveTags (tab will
        # NOT be renamed).  Confirms we haven't broken the happy path.
        _write_meta(short_storage, 'alive-tag')
        srv = _bind_listening_socket(short_storage, 'alive-tag')
        try:
            groovy = _run_sweep_capture_groovy(short_storage)
        finally:
            srv.close()
        assert '"alive-tag"' in groovy

    def test_dead_session_with_no_socket_is_dropped(
        self, short_storage: Path,
    ) -> None:
        # The classic crashed-server case: meta lingers but no .sock.
        _write_meta(short_storage, 'crashed-tag')
        groovy = _run_sweep_capture_groovy(short_storage)
        assert '"crashed-tag"' not in groovy

    def test_post_reboot_stale_socket_is_dropped(
        self, short_storage: Path,
    ) -> None:
        # YUVAL'S BUG: meta file exists from before the reboot, the
        # .sock file exists too (FS state preserved), but nothing is
        # listening because the server process died with the reboot.
        # Even if the original PID has been reassigned to an unrelated
        # process and ``os.kill(pid, 0)`` returns success, the connect
        # probe must reject this as dead so the tab gets renamed.
        _write_meta(short_storage, 'pre-reboot-tag')
        # Create a stale .sock file by binding then closing (no listen).
        sock_path = short_storage / 'sockets' / 'pre-reboot-tag.sock'
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.close()
        groovy = _run_sweep_capture_groovy(short_storage)
        assert '"pre-reboot-tag"' not in groovy

    def test_meta_for_other_ide_is_ignored(
        self, short_storage: Path,
    ) -> None:
        # A live session in a different IDE / different project should
        # neither protect nor get renamed by the sweep — its tag must
        # simply not appear in the liveTags set.
        _write_meta(
            short_storage, 'other-ide-tag',
            ide='PyCharm', project_path='/x/proj',
        )
        srv = _bind_listening_socket(short_storage, 'other-ide-tag')
        try:
            groovy = _run_sweep_capture_groovy(short_storage)
        finally:
            srv.close()
        assert '"other-ide-tag"' not in groovy

    def test_exclude_tag_dropped_even_with_live_socket(
        self, short_storage: Path,
    ) -> None:
        # The about-to-start session is force-dropped from liveTags so
        # any pre-existing ``lps <ourTag>`` tab from a previous run
        # gets renamed to bare — freeing the name for our OSC to claim
        # a moment later.  Verify that path still works after the
        # liveness-probe swap.
        _write_meta(short_storage, 'newtag')
        srv = _bind_listening_socket(short_storage, 'newtag')
        try:
            groovy = _run_sweep_capture_groovy(
                short_storage, exclude_tag='newtag',
            )
        finally:
            srv.close()
        assert '"newtag"' not in groovy

    def test_multiple_sessions_filtered_correctly(
        self, short_storage: Path,
    ) -> None:
        # A realistic post-reboot scenario: one live session, two stale
        # ones (no listener), one in a different project.  Only the
        # live one should appear in liveTags.
        _write_meta(short_storage, 'live1')
        _write_meta(short_storage, 'stale1')
        _write_meta(short_storage, 'stale2')
        _write_meta(
            short_storage, 'otherproj',
            project_path='/other/proj',
        )
        srv = _bind_listening_socket(short_storage, 'live1')
        try:
            groovy = _run_sweep_capture_groovy(short_storage)
        finally:
            srv.close()
        assert '"live1"' in groovy
        assert '"stale1"' not in groovy
        assert '"stale2"' not in groovy
        assert '"otherproj"' not in groovy
