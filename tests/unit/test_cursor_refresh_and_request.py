"""Tests for two Cursor-feature fixes:

1. ``SessionRefreshWorker.run`` must still scan Cursor when the (unrelated)
   leap-session fetch fails - otherwise a transient socket error emits
   ``([], [])`` and the monitor prunes every opted-in Cursor PR (those tags
   are in-memory only, so there's no auto-reconnect).
2. ``navigation._write_terminal_request`` must write the extension request
   file atomically (temp + os.replace) and overwrite a stale request, so
   the extension never reads a half-written payload.

Both are exercised without instantiating Qt: ``run`` is called as an
unbound method on a tiny fake ``self`` whose ``sessions_ready`` just records
the emitted args.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from leap.monitor import navigation
from leap.monitor import scm_polling


# --------------------------------------------------------------------------
# 1. Refresh worker: Cursor scan survives a leap-fetch failure
# --------------------------------------------------------------------------


class _RecordingSignal:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    def emit(self, a: Any, b: Any) -> None:
        self.calls.append((a, b))


class _FakeWorker:
    """Just enough of SessionRefreshWorker for run() to execute."""

    def __init__(self, scan_cursor_gui: bool) -> None:
        self._scan_cursor_gui = scan_cursor_gui
        self.sessions_ready = _RecordingSignal()


def _boom() -> Any:
    raise RuntimeError('transient socket error')


def test_cursor_scanned_even_when_leap_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scm_polling, 'get_active_sessions', _boom)
    monkeypatch.setattr(
        scm_polling, 'scan_open_cursor_agents',
        lambda: [{'tag': 'cursor-gui:abc'}],
    )
    w = _FakeWorker(scan_cursor_gui=True)
    scm_polling.SessionRefreshWorker.run(w)  # type: ignore[arg-type]
    # Leap list empty (fetch failed) but the Cursor rows survive, so
    # _on_sessions_refreshed won't prune the user's Cursor PR tracking.
    assert w.sessions_ready.calls == [([], [{'tag': 'cursor-gui:abc'}])]


def test_leap_fetch_failure_without_cursor_scan_emits_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scm_polling, 'get_active_sessions', _boom)
    # Should NOT be called when the toggle is off, but make it loud if it is.
    monkeypatch.setattr(
        scm_polling, 'scan_open_cursor_agents',
        lambda: (_ for _ in ()).throw(AssertionError('should not scan')),
    )
    w = _FakeWorker(scan_cursor_gui=False)
    scm_polling.SessionRefreshWorker.run(w)  # type: ignore[arg-type]
    assert w.sessions_ready.calls == [([], [])]


def test_happy_path_emits_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scm_polling, 'get_active_sessions', lambda: [{'tag': 'real'}],
    )
    monkeypatch.setattr(
        scm_polling, 'scan_open_cursor_agents',
        lambda: [{'tag': 'cursor-gui:abc'}],
    )
    w = _FakeWorker(scan_cursor_gui=True)
    scm_polling.SessionRefreshWorker.run(w)  # type: ignore[arg-type]
    assert w.sessions_ready.calls == [
        ([{'tag': 'real'}], [{'tag': 'cursor-gui:abc'}]),
    ]


# --------------------------------------------------------------------------
# 2. Atomic request-file write
# --------------------------------------------------------------------------


def test_write_terminal_request_writes_content(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('HOME', str(tmp_path))
    assert navigation._write_terminal_request('focusComposer:abc') is True
    target = tmp_path / '.leap-terminal-request'
    assert target.read_text() == 'focusComposer:abc'


def test_write_terminal_request_overwrites_stale(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('HOME', str(tmp_path))
    target = tmp_path / '.leap-terminal-request'
    target.write_text('rename:lps oldtag')  # never-consumed stale request
    assert navigation._write_terminal_request('closeComposer:xyz') is True
    assert target.read_text() == 'closeComposer:xyz'


def test_write_terminal_request_leaves_no_temp_files(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('HOME', str(tmp_path))
    navigation._write_terminal_request('focusComposer:abc')
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name.startswith('.leap-req-')]
    assert leftovers == []
