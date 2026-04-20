"""Shared test fixtures for Leap tests.

Exposes the :class:`PTYFixture` helper used by every integration test
under ``tests/integration/``.  It spawns a real bash process via
pexpect, wires its output into a :class:`ClaudeStateTracker`, and
gives each test a small, expressive API:

* ``send_input(bytes)`` — real user typing (feeds both the PTY and the
  tracker's ``on_input``).
* ``send_line(str)`` — programmatic ``bash`` command (no ``on_input``).
* ``drain_to_tracker(timeout)`` — read all PTY output and feed
  ``on_output``; mirrors the server's ``_output_filter`` loop.
* ``write_signal(state)`` — write the JSON signal file a CLI hook
  would produce.
* ``get_state()`` / ``wait_for_state(expected)`` — polling helpers.

The unit tests under ``tests/unit/`` use the fake-clock tracker
constructor directly and don't need this fixture.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import pexpect
import pytest

from leap.cli_providers.base import CLIProvider
from leap.server.state_tracker import ClaudeStateTracker


class PTYFixture:
    """Wraps a pexpect-spawned bash process + a wired state tracker."""

    def __init__(
        self,
        signal_file: Path,
        provider: Optional[CLIProvider] = None,
        dimensions: tuple[int, int] = (24, 80),
    ) -> None:
        self.signal_file = signal_file
        if provider is not None:
            self.tracker = ClaudeStateTracker(
                signal_file=signal_file, provider=provider,
            )
        else:
            self.tracker = ClaudeStateTracker(signal_file=signal_file)
        self.child = pexpect.spawn(
            '/bin/bash', ['--norc', '--noprofile'],
            dimensions=dimensions,
            encoding=None,
        )
        time.sleep(0.3)
        self._drain_initial()

    # -- Input / output plumbing -----------------------------------------

    def send_input(self, data: bytes) -> None:
        """Simulate a real keystroke (updates tracker + sends to PTY)."""
        self.tracker.on_input(data)
        self.child.send(data)

    def send_line(self, text: str) -> None:
        """Send a shell command without going through ``on_input``.

        Simulates the CLI emitting output on its own (e.g. when bash
        runs a command after an Enter that we've already processed).
        """
        self.child.sendline(text)

    def feed_output(self, data: bytes) -> None:
        """Feed raw bytes directly to the tracker (no PTY involved).

        Use when a test needs precise control over screen content
        (ANSI sequences, dialog patterns, compact indicators) that
        bash can't reliably produce byte-for-byte.
        """
        self.tracker.on_output(data)

    def drain_to_tracker(self, timeout: float = 0.5) -> bytes:
        """Read all PTY output until the timeout and feed the tracker.

        Mirrors ``PTYHandler._output_filter`` in the real server.
        """
        collected = b''
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = self.child.read_nonblocking(4096, timeout=0.1)
                if data:
                    self.tracker.on_output(data)
                    collected += data
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                break
        return collected

    # -- Signal file plumbing --------------------------------------------

    def write_signal(self, state: str, **extra: str) -> None:
        """Write a JSON signal file matching what a CLI hook would."""
        payload: dict[str, str] = {'state': state}
        payload.update(extra)
        self.signal_file.write_text(json.dumps(payload))

    def clear_signal(self) -> None:
        """Delete the signal file (ignoring missing)."""
        try:
            self.signal_file.unlink()
        except FileNotFoundError:
            pass

    # -- State polling ---------------------------------------------------

    def get_state(self) -> str:
        """One poll cycle (what the server's auto-sender does)."""
        return self.tracker.get_state(pty_alive=self.child.isalive())

    def wait_for_state(
        self,
        expected: str,
        timeout: float = 5.0,
        poll_interval: float = 0.1,
    ) -> str:
        """Poll until ``expected`` or timeout — returns the last state."""
        deadline = time.time() + timeout
        last = self.get_state()
        while time.time() < deadline:
            last = self.get_state()
            if last == expected:
                return last
            time.sleep(poll_interval)
        return last

    # -- Lifecycle -------------------------------------------------------

    def resize(self, rows: int, cols: int) -> None:
        """Resize the PTY and notify the tracker."""
        self.child.setwinsize(rows, cols)
        self.tracker.on_resize(rows, cols)

    def close(self) -> None:
        """Terminate the bash process."""
        if self.child.isalive():
            self.child.close(force=True)

    def _drain_initial(self) -> None:
        """Drain the initial bash prompt."""
        try:
            self.child.read_nonblocking(4096, timeout=0.3)
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass


@pytest.fixture
def pty(tmp_path: Path) -> PTYFixture:
    """A default PTY fixture with the Claude provider."""
    fixture = PTYFixture(signal_file=tmp_path / 'test.signal')
    yield fixture
    fixture.close()


@pytest.fixture
def pty_factory(tmp_path: Path):
    """Factory yielding fresh PTY fixtures (for provider overrides /
    multi-fixture tests)."""
    created: list[PTYFixture] = []

    def _make(
        provider: Optional[CLIProvider] = None,
        dimensions: tuple[int, int] = (24, 80),
        tag: str = 'test',
    ) -> PTYFixture:
        fixture = PTYFixture(
            signal_file=tmp_path / f'{tag}.signal',
            provider=provider,
            dimensions=dimensions,
        )
        created.append(fixture)
        return fixture

    yield _make
    for fixture in created:
        fixture.close()
