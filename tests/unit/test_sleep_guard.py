"""Regression tests for the "Prevent sleep while busy" / "Also block lid-close"
guards.

Two layers, both headless (no MonitorWindow is constructed, no window shown):

1. The ``SleepGuard`` / ``LidCloseGuard`` primitives in ``sleep_guard.py`` -
   spawn/idempotency for caffeinate (``subprocess.Popen`` stubbed), and the
   ``disablesleep`` marker-file lifecycle + crash-recovery stop path
   (``SudoManager.run`` stubbed; marker redirected to ``tmp_path``).

2. ``MonitorWindow._evaluate_sleep_guard`` - the per-tick decision that holds
   both guards while any session is RUNNING and releases BOTH once every
   session has stayed out of RUNNING for the 30s grace window.  The real
   method (plus ``_maybe_lid_start`` / ``_maybe_lid_stop``) is bound to a
   lightweight fake ``self`` driven by a fake monotonic clock and fake OS
   guards, so the real branching is exercised without Qt or touching the
   system.  This is the path a user asked us to verify: "does it actually turn
   both off after 30s of all-idle?"
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

import leap.monitor.app as app
import leap.monitor.sleep_guard as sleep_guard_mod
from leap.cli_providers.states import CLIState
from leap.monitor.sleep_guard import LidCloseGuard, SleepGuard

# The grace constant the evaluator releases on (kept in sync with the source).
GRACE: int = int(app.MonitorWindow._SLEEP_GUARD_RUNNING_GRACE_SECONDS)


# ---------------------------------------------------------------------------
#  SleepGuard (caffeinate child) primitive
# ---------------------------------------------------------------------------

class _FakePopen:
    """Minimal stand-in for the caffeinate child process."""

    pid = 4321

    def __init__(self) -> None:
        self._rc: Any = None  # None => still running

    def poll(self) -> Any:
        return self._rc

    def terminate(self) -> None:
        self._rc = 0

    def kill(self) -> None:
        self._rc = -9

    def wait(self, timeout: float = 0.0) -> int:
        return self._rc if self._rc is not None else 0


class TestSleepGuard:
    def test_inactive_with_no_process(self) -> None:
        assert SleepGuard().is_active is False

    def test_stop_while_inactive_is_noop(self) -> None:
        SleepGuard().stop()  # must not raise

    def test_start_spawns_caffeinate_once(self, monkeypatch: Any) -> None:
        calls: list[list[str]] = []

        def fake_popen(args: list[str], **kw: Any) -> _FakePopen:
            calls.append(args)
            return _FakePopen()

        monkeypatch.setattr(sleep_guard_mod.subprocess, 'Popen', fake_popen)
        g = SleepGuard()
        g.start()
        assert g.is_active is True
        assert len(calls) == 1
        assert calls[0][0] == SleepGuard._CAFFEINATE_PATH
        assert '-i' in calls[0] and '-w' in calls[0]
        # Idempotent: a second start while active must not spawn again.
        g.start()
        assert len(calls) == 1
        g.stop()
        assert g.is_active is False

    def test_start_swallows_spawn_failure(self, monkeypatch: Any) -> None:
        def boom(args: list[str], **kw: Any) -> None:
            raise OSError('no caffeinate')

        monkeypatch.setattr(sleep_guard_mod.subprocess, 'Popen', boom)
        g = SleepGuard()
        g.start()  # degrades to "checkbox does nothing", must not raise
        assert g.is_active is False


# ---------------------------------------------------------------------------
#  LidCloseGuard (pmset disablesleep) primitive
# ---------------------------------------------------------------------------

@pytest.fixture
def lid_marker(tmp_path: Any, monkeypatch: Any) -> Any:
    """Redirect the disablesleep marker to a tmp path (no .storage writes)."""
    marker = tmp_path / 'disablesleep.marker'
    monkeypatch.setattr(sleep_guard_mod, '_DISABLESLEEP_MARKER', marker)
    return marker


def _stub_pmset(monkeypatch: Any, rc: int, err: str = '') -> list[list[str]]:
    """Stub ``SudoManager.run`` and return the list it records calls into."""
    runs: list[list[str]] = []

    def fake_run(args: list[str], password: str) -> tuple[int, str]:
        runs.append(args)
        return rc, err

    monkeypatch.setattr(
        sleep_guard_mod.SudoManager, 'run', staticmethod(fake_run))
    return runs


class TestLidCloseGuard:
    def test_start_sets_marker_and_runs_pmset_1(
        self, lid_marker: Any, monkeypatch: Any,
    ) -> None:
        runs = _stub_pmset(monkeypatch, rc=0)
        g = LidCloseGuard()
        ok, _ = g.start('pw')
        assert ok and g.is_active and lid_marker.exists()
        assert runs == [['/usr/bin/pmset', '-a', 'disablesleep', '1']]
        # Idempotent: second start does not re-invoke sudo.
        ok, _ = g.start('pw')
        assert ok and len(runs) == 1

    def test_stop_clears_marker_and_runs_pmset_0(
        self, lid_marker: Any, monkeypatch: Any,
    ) -> None:
        runs = _stub_pmset(monkeypatch, rc=0)
        g = LidCloseGuard()
        g.start('pw')
        ok, _ = g.stop('pw')
        assert ok and not g.is_active and not lid_marker.exists()
        assert runs[-1] == ['/usr/bin/pmset', '-a', 'disablesleep', '0']

    def test_stop_noop_when_inactive_and_no_marker(
        self, lid_marker: Any, monkeypatch: Any,
    ) -> None:
        runs = _stub_pmset(monkeypatch, rc=0)
        ok, _ = LidCloseGuard().stop('pw')
        assert ok and runs == []  # never touched sudo

    def test_stop_recovers_orphan_marker_even_if_inactive(
        self, lid_marker: Any, monkeypatch: Any,
    ) -> None:
        # Marker left on disk by a crashed previous run; guard reports
        # _active=False but stop() must still clear disablesleep.
        lid_marker.touch()
        runs = _stub_pmset(monkeypatch, rc=0)
        ok, _ = LidCloseGuard().stop('pw')
        assert ok and runs and not lid_marker.exists()

    def test_start_failure_leaves_no_marker(
        self, lid_marker: Any, monkeypatch: Any,
    ) -> None:
        _stub_pmset(monkeypatch, rc=1, err='Sorry, try again.')
        g = LidCloseGuard()
        ok, _ = g.start('pw')
        assert not ok and not g.is_active and not lid_marker.exists()

    def test_force_inactive_clears_state_without_sudo(
        self, lid_marker: Any, monkeypatch: Any,
    ) -> None:
        runs = _stub_pmset(monkeypatch, rc=0)
        g = LidCloseGuard()
        g.start('pw')
        runs.clear()
        g.force_inactive()
        assert not g.is_active and not lid_marker.exists()
        assert runs == []  # never calls pmset


# ---------------------------------------------------------------------------
#  MonitorWindow._evaluate_sleep_guard - the 30s release logic
# ---------------------------------------------------------------------------

class _Clock:
    """Controllable stand-in for ``time.monotonic``."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _FakeSleep:
    def __init__(self) -> None:
        self.active = False
        self.starts = 0
        self.stops = 0

    @property
    def is_active(self) -> bool:
        return self.active

    def start(self) -> None:
        if not self.active:
            self.starts += 1
        self.active = True

    def stop(self) -> None:
        if self.active:
            self.stops += 1
        self.active = False


class _FakeLid:
    def __init__(self) -> None:
        self.active = False
        self.marker = False
        self.starts = 0
        self.stops = 0

    @property
    def is_active(self) -> bool:
        return self.active

    def start(self, pw: str) -> tuple[bool, str]:
        if not self.active:
            self.starts += 1
        self.active = True
        self.marker = True
        return True, ''

    def stop(self, pw: str) -> tuple[bool, str]:
        if self.active or self.marker:
            self.stops += 1
        self.active = False
        self.marker = False
        return True, ''


def _wire(monkeypatch: Any, *, prevent: bool = True,
          lid: bool = True) -> tuple[SimpleNamespace, _Clock]:
    """Build a fake ``self`` wired to the real evaluator + lid helpers."""
    clock = _Clock()
    monkeypatch.setattr(app.time, 'monotonic', clock)
    monkeypatch.setattr(app.SudoManager, 'load', staticmethod(lambda: 'pw'))

    fake = SimpleNamespace()
    fake._shutting_down = False
    fake._prefs = {
        'prevent_sleep_while_busy': prevent,
        'block_lid_close': lid,
    }
    fake.sessions = []
    fake._last_running_at = 0.0
    fake._lid_pw_dialog_open = False
    fake._sleep_guard = _FakeSleep()
    fake._lid_close_guard = _FakeLid()
    fake._SLEEP_GUARD_RUNNING_GRACE_SECONDS = float(GRACE)
    fake._auth_failures: list[bool] = []
    fake._handle_lid_auth_failure = (
        lambda intended_active: fake._auth_failures.append(intended_active))

    # marker_present() reads the fake lid's marker flag.
    monkeypatch.setattr(
        app.LidCloseGuard, 'marker_present',
        staticmethod(lambda: fake._lid_close_guard.marker))
    fake._maybe_lid_start = lambda: app.MonitorWindow._maybe_lid_start(fake)
    fake._maybe_lid_stop = lambda: app.MonitorWindow._maybe_lid_stop(fake)
    return fake, clock


def _tick(fake: SimpleNamespace, clock: _Clock, *, at: float,
          running: bool) -> None:
    """One refresh tick at ``at`` seconds past the clock origin."""
    clock.t = 1000.0 + at
    fake.sessions = [{
        'tag': 'x',
        'cli_state': CLIState.RUNNING if running else CLIState.IDLE,
    }]
    app.MonitorWindow._evaluate_sleep_guard(fake)


class TestEvaluateSleepGuard:
    def test_starts_both_when_a_session_is_running(
        self, monkeypatch: Any,
    ) -> None:
        fake, clock = _wire(monkeypatch)
        _tick(fake, clock, at=0, running=True)
        assert fake._sleep_guard.is_active
        assert fake._lid_close_guard.is_active
        assert fake._last_running_at == 1000.0

    def test_holds_through_grace_then_releases_both_at_30s(
        self, monkeypatch: Any,
    ) -> None:
        fake, clock = _wire(monkeypatch)
        # Running burst: last RUNNING tick is at t=5.
        for t in range(0, 6):
            _tick(fake, clock, at=t, running=True)
        # Idle but still inside the grace window (t-5 < 30) => keep holding.
        for t in range(6, 5 + GRACE):  # 6..34
            _tick(fake, clock, at=t, running=False)
        assert fake._sleep_guard.is_active, 'released before grace elapsed'
        assert fake._lid_close_guard.is_active, 'lid released too early'
        # Exactly at last_running + grace (5 + 30 = 35): release BOTH.
        _tick(fake, clock, at=5 + GRACE, running=False)
        assert not fake._sleep_guard.is_active
        assert not fake._lid_close_guard.is_active
        assert fake._sleep_guard.stops == 1
        assert fake._lid_close_guard.stops == 1

    def test_brief_blip_resets_the_grace_clock(
        self, monkeypatch: Any,
    ) -> None:
        fake, clock = _wire(monkeypatch)
        for t in range(0, 3):
            _tick(fake, clock, at=t, running=True)   # last RUNNING = 2
        for t in range(3, 21):
            _tick(fake, clock, at=t, running=False)  # 20-2=18 < 30 => hold
        assert fake._sleep_guard.is_active
        _tick(fake, clock, at=21, running=True)      # blip => last RUNNING = 21
        _tick(fake, clock, at=21 + GRACE - 1, running=False)  # 29 < 30 => hold
        assert fake._sleep_guard.is_active, 'blip did not reset the clock'
        _tick(fake, clock, at=21 + GRACE, running=False)      # 30 => release
        assert not fake._sleep_guard.is_active
        assert not fake._lid_close_guard.is_active

    def test_unticking_prevent_sleep_releases_both_immediately(
        self, monkeypatch: Any,
    ) -> None:
        fake, clock = _wire(monkeypatch)
        _tick(fake, clock, at=0, running=True)
        assert fake._sleep_guard.is_active and fake._lid_close_guard.is_active
        fake._prefs['prevent_sleep_while_busy'] = False
        # Even with a session still RUNNING, the feature being off wins.
        _tick(fake, clock, at=1, running=True)
        assert not fake._sleep_guard.is_active
        assert not fake._lid_close_guard.is_active

    def test_lid_not_started_when_lid_pref_off(
        self, monkeypatch: Any,
    ) -> None:
        fake, clock = _wire(monkeypatch, lid=False)
        _tick(fake, clock, at=0, running=True)
        assert fake._sleep_guard.is_active        # caffeinate only
        assert not fake._lid_close_guard.is_active

    def test_shutting_down_is_a_noop(self, monkeypatch: Any) -> None:
        fake, clock = _wire(monkeypatch)
        fake._shutting_down = True
        _tick(fake, clock, at=0, running=True)
        assert not fake._sleep_guard.is_active
        assert not fake._lid_close_guard.is_active

    def test_release_independent_of_caffeinate_liveness(
        self, monkeypatch: Any,
    ) -> None:
        # caffeinate can die externally while the lid guard still holds
        # disablesleep=1; the grace release must still clear the lid.
        fake, clock = _wire(monkeypatch)
        _tick(fake, clock, at=0, running=True)
        fake._sleep_guard.active = False  # simulate external caffeinate death
        _tick(fake, clock, at=5 + GRACE, running=False)
        assert not fake._lid_close_guard.is_active
        assert fake._lid_close_guard.stops == 1

    def test_open_sudo_dialog_defers_lid_stop_until_closed(
        self, monkeypatch: Any,
    ) -> None:
        # While a re-auth dialog is up, the grace release must not fire a
        # stale pmset stop; caffeinate still releases, lid retries later.
        fake, clock = _wire(monkeypatch)
        _tick(fake, clock, at=0, running=True)
        fake._lid_pw_dialog_open = True
        _tick(fake, clock, at=5 + GRACE, running=False)
        assert not fake._sleep_guard.is_active       # caffeinate released
        assert fake._lid_close_guard.is_active        # lid stop deferred
        fake._lid_pw_dialog_open = False
        _tick(fake, clock, at=6 + GRACE, running=False)
        assert not fake._lid_close_guard.is_active    # released on next tick
