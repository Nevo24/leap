"""Tests for ``_parent_pid`` — the helper behind the JetBrains navigation's
rename-proof tab fallback.

When a user renames a terminal tab in the IDE, it loses its ``lps <tag>``
title and can no longer be matched by title. ``_navigate_jetbrains`` then
falls back to matching the tab by the PID of the login shell backing it —
which is the Leap server process's *parent*. ``_parent_pid`` resolves that
parent PID; the Groovy side compares it against each tab's shell process.
"""

from __future__ import annotations

import os

from leap.monitor.navigation import _parent_pid


def test_parent_pid_matches_os_getppid() -> None:
    # The parent of this very process must equal the real parent PID.
    assert _parent_pid(os.getpid()) == os.getppid()


def test_parent_pid_returns_none_for_nonexistent_pid() -> None:
    # A PID well above the OS maximum can't exist, so ``ps`` prints nothing
    # and the helper reports "unknown" rather than raising — the caller then
    # simply skips the PID fallback.
    assert _parent_pid(2_000_000_000) is None
