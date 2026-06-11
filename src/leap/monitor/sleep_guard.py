"""macOS sleep prevention.

This module exposes two cooperating guards that the monitor activates
together while any session is in ``RUNNING`` state:

:class:`SleepGuard`
    Spawns ``caffeinate -i -w <monitor-pid>`` to block idle sleep.
    Self-cleans on parent death thanks to ``-w``; survives a crash.

:class:`LidCloseGuard`
    Optional layer (gated by the second checkbox).  Calls
    ``sudo pmset -a disablesleep 1/0`` via :class:`SudoManager` to
    additionally block lid-close sleep.  ``disablesleep`` is a sticky
    kernel setting and can't be tied to a process lifetime, so we lean
    on a marker file (``.storage/disablesleep.marker``) to detect and
    recover from a crashed-while-active state on the next monitor
    startup.

The :class:`SleepGuard` is idempotent: ``start()`` while already
active and ``stop()`` while inactive are both no-ops.
"""

import logging
import os
import socket
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from leap.monitor.sudo_manager import SudoManager
from leap.utils.constants import STORAGE_DIR

logger = logging.getLogger(__name__)

# Marker written while we hold ``disablesleep=1`` so a crashed monitor
# can be detected on the next launch and the kernel state cleaned up.
_DISABLESLEEP_MARKER = STORAGE_DIR / 'disablesleep.marker'

# Internet-connectivity probe targets, used by the monitor to decide
# whether it's still worth blocking sleep.  We open a raw TCP socket to
# public anycast DNS resolvers on port 443 (HTTPS): 443 is almost never
# firewalled (unlike port 53, which managed/corporate networks often
# block), a bare handshake needs no DNS lookup, and it isn't fooled by
# captive-portal DNS/HTTP hijacking.  The asymmetry is deliberate — a
# false "offline" would sleep the Mac and pause running sessions, so we
# only report offline when a socket genuinely can't be opened.
#
# IPv4 first (the common case, so we usually short-circuit on the first
# host), then the same resolvers' IPv6 anycast addresses so an
# IPv6-only / NAT64 network isn't mistaken for "offline".  On an IPv4
# network the v6 entries are never reached; on a v6-only network the v4
# entries fail fast with ENETUNREACH rather than timing out.
_CONNECTIVITY_HOSTS: Tuple[Tuple[str, int], ...] = (
    ('1.1.1.1', 443),                    # Cloudflare (IPv4)
    ('8.8.8.8', 443),                    # Google (IPv4)
    ('2606:4700:4700::1111', 443),       # Cloudflare (IPv6)
    ('2001:4860:4860::8888', 443),       # Google (IPv6)
)
_CONNECTIVITY_TIMEOUT_SECONDS = 2.0


def have_internet() -> bool:
    """Return True iff a TCP handshake to a public host succeeds.

    Tries each host in turn with a short timeout, returning on the first
    success.  Runs fast when online; on a hard outage it blocks for up
    to ``len(_CONNECTIVITY_HOSTS) * _CONNECTIVITY_TIMEOUT_SECONDS``
    seconds, so callers MUST invoke it off the UI thread.
    """
    for host, port in _CONNECTIVITY_HOSTS:
        try:
            with socket.create_connection(
                (host, port), timeout=_CONNECTIVITY_TIMEOUT_SECONDS
            ):
                return True
        except OSError:
            continue
    return False


class SleepGuard:
    """Holds a ``caffeinate(8)`` child process while active."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None

    @property
    def is_active(self) -> bool:
        """True iff a caffeinate child is currently running."""
        return self._proc is not None and self._proc.poll() is None

    # Hardcoded so the py2app bundle (which can launch with a sanitized
    # PATH) always finds the binary.  ``caffeinate`` has lived at this
    # path on every macOS release since 10.8 — well below our minimum.
    _CAFFEINATE_PATH = '/usr/bin/caffeinate'

    def start(self) -> None:
        """Spawn ``caffeinate -i -w <ppid>`` if not already running.

        ``caffeinate`` exits automatically when the monitor process
        exits (``-w``), so the assertion is always released — even on
        a hard crash or ``os._exit``.  Failure to spawn (binary
        missing, permission denied) is logged once and swallowed; the
        feature degrades to "checkbox does nothing" rather than
        breaking the monitor.
        """
        if self.is_active:
            return
        try:
            self._proc = subprocess.Popen(
                [self._CAFFEINATE_PATH, '-i', '-w', str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                "SleepGuard active (caffeinate pid=%d)", self._proc.pid)
        except OSError:
            logger.exception("Failed to spawn caffeinate")
            self._proc = None

    def stop(self) -> None:
        """Terminate the caffeinate child if running."""
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
            logger.info("SleepGuard released")
        except Exception:
            logger.exception("Error stopping caffeinate")


class LidCloseGuard:
    """Toggles ``pmset -a disablesleep 1/0`` to also block lid-close sleep.

    Unlike :class:`SleepGuard` (a child-process assertion that the
    kernel auto-releases on parent death), ``disablesleep`` is a
    sticky system property that survives reboot.  We therefore pair
    every ``start`` with a marker file write, and every ``stop`` with
    a marker file delete, so the next monitor startup can detect a
    crashed-while-active state and clean it up.

    Each call needs the user's sudo password; on auth failure the
    caller (MonitorWindow) is expected to re-prompt and retry.  The
    methods return ``(success, stderr)`` so the caller can distinguish
    a wrong-password retry from a hard error.
    """

    _PMSET_PATH = '/usr/bin/pmset'

    def __init__(self) -> None:
        self._active: bool = False

    @property
    def is_active(self) -> bool:
        return self._active

    @staticmethod
    def marker_path() -> Path:
        return _DISABLESLEEP_MARKER

    @staticmethod
    def marker_present() -> bool:
        return _DISABLESLEEP_MARKER.exists()

    def start(self, password: str) -> Tuple[bool, str]:
        """Run ``pmset -a disablesleep 1``.

        Idempotent — if we're already active, returns ``(True, '')``
        without touching the system or sudo.
        """
        if self._active:
            return True, ''
        rc, err = SudoManager.run(
            [self._PMSET_PATH, '-a', 'disablesleep', '1'], password)
        if rc == 0:
            self._active = True
            try:
                _DISABLESLEEP_MARKER.touch()
            except OSError:
                logger.exception("Failed to write disablesleep marker")
            logger.info("LidCloseGuard active (disablesleep=1)")
            return True, ''
        logger.error(
            "pmset disablesleep 1 failed (rc=%d): %s", rc, err.strip())
        return False, err

    def stop(self, password: str) -> Tuple[bool, str]:
        """Run ``pmset -a disablesleep 0``.

        Idempotent — if we're not active and no marker is present,
        returns ``(True, '')`` without touching the system.  When the
        marker IS present (recovery from a crashed previous run), we
        invoke pmset even if ``self._active`` is False.
        """
        if not self._active and not _DISABLESLEEP_MARKER.exists():
            return True, ''
        rc, err = SudoManager.run(
            [self._PMSET_PATH, '-a', 'disablesleep', '0'], password)
        if rc == 0:
            self._active = False
            try:
                _DISABLESLEEP_MARKER.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception("Failed to remove disablesleep marker")
            logger.info("LidCloseGuard released (disablesleep=0)")
            return True, ''
        logger.error(
            "pmset disablesleep 0 failed (rc=%d): %s", rc, err.strip())
        return False, err

    def force_inactive(self) -> None:
        """Mark the guard as inactive locally without running pmset.

        Called when the user has cancelled a re-auth dialog or when
        even a freshly-validated password keeps getting rejected by
        ``sudo pmset`` — in either case we've given up trying to
        cleanly release the OS-level assertion.

        Clears both ``self._active`` and the marker file so subsequent
        evaluator ticks don't keep retrying (and re-opening dialogs).
        Trade-off: the next monitor startup loses its orphan-recovery
        signal, so if ``disablesleep=1`` is still set at the OS level
        the user has to clear it by hand.  The caller is expected to
        warn them to that effect via QMessageBox.
        """
        self._active = False
        try:
            _DISABLESLEEP_MARKER.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("Failed to remove disablesleep marker")
