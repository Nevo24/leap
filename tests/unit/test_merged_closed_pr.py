"""Tests for the Merged / Closed PR badge lifecycle in PRTrackingMixin.

These exercise the pure state transforms (persist closed/merged, re-open,
stop-tracking, revisit-tag selection) and the open->NO_PR / re-open detection
in ``_on_scm_results`` — all by calling the unbound mixin methods against a
lightweight fake ``self`` whose helpers (timer sync, table refresh, status
bar) are no-op stubs.  ``write_pinned_session_entry`` is monkeypatched so no
disk I/O happens.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import pytest

import leap.monitor._mixins.pr_tracking_mixin as ptm
from leap.monitor._mixins.pr_tracking_mixin import PRTrackingMixin
from leap.monitor.pr_tracking.base import ClosedPRInfo, PRState, PRStatus


class _FakeMon:
    """Minimal stand-in for MonitorWindow carrying just the state the
    merged/closed lifecycle methods read or mutate."""

    def __init__(self) -> None:
        self._pinned_sessions: dict[str, dict[str, Any]] = {}
        self._cell_cache: dict[tuple, Any] = {}
        self._tracked_tags: set[str] = set()
        self._pr_statuses: dict[str, PRStatus] = {}
        self._pr_widgets: dict[str, Any] = {}
        self._pr_approval_widgets: dict[str, Any] = {}
        self._pr_changed_at: dict[str, Any] = {}
        self._dismissed_pr_new_status: set[str] = set()
        self._dock_badge = SimpleNamespace(discard_tag=lambda t: None)
        self.sessions: list[dict[str, Any]] = []
        self._shutting_down = False
        self._status_msgs: list[str] = []
        self._sync_calls = 0
        self._update_table_calls = 0
        self._update_pr_column_calls = 0
        # recorders for _on_scm_results detection
        self.closed_checks: list[str] = []
        self.reopens: list[tuple[str, PRStatus]] = []

    # --- helper stubs the lifecycle methods call ---
    def _sync_scm_poll_timer(self) -> None:
        self._sync_calls += 1

    def _update_table(self) -> None:
        self._update_table_calls += 1

    def _update_pr_column(self) -> None:
        self._update_pr_column_calls += 1

    def _update_dock_badge(self) -> None:
        pass

    def _show_status(self, msg: str, **kw: Any) -> None:
        self._status_msgs.append(msg)

    def isVisible(self) -> bool:
        return True

    # real revisit-tag logic (used by _revisit_poll_sessions)
    def _revisit_tags(self) -> set[str]:
        return PRTrackingMixin._revisit_tags(self)

    # real fire-snapshot comparison (used by _on_scm_results)
    def _pr_fire_snapshot_changed(self, old: tuple, new: tuple) -> bool:
        return PRTrackingMixin._pr_fire_snapshot_changed(old, new)

    # recorders (replace the real background-launch methods)
    def _check_pr_closed_after_no_pr(self, tag: str) -> None:
        self.closed_checks.append(tag)

    def _reopen_tracked_pr(self, tag: str, status: PRStatus) -> None:
        self.reopens.append((tag, status))


@pytest.fixture(autouse=True)
def _no_disk(monkeypatch: Any) -> list:
    """Capture write_pinned_session_entry calls instead of writing to disk."""
    calls: list = []
    monkeypatch.setattr(
        ptm, 'write_pinned_session_entry',
        lambda tag, entry: calls.append((tag, dict(entry))))
    return calls


def _closed(merged: bool, *, iid: int = 108, title: str = 'T',
            url: str = 'https://h/o/r/pull/108') -> ClosedPRInfo:
    return ClosedPRInfo(pr_iid=iid, pr_title=title, pr_url=url, merged=merged)


# ---------------------------------------------------------------------------
#  _revisit_tags
# ---------------------------------------------------------------------------

class TestRevisitTags:
    def test_merged_pinned_with_branch_is_revisit(self) -> None:
        mon = _FakeMon()
        mon._pinned_sessions = {
            't1': {'pr_merged': True, 'remote_project_path': 'o/r',
                   'branch': 'feat'},
        }
        assert PRTrackingMixin._revisit_tags(mon) == {'t1'}

    def test_closed_pinned_with_branch_is_revisit(self) -> None:
        mon = _FakeMon()
        mon._pinned_sessions = {
            't1': {'pr_closed': True, 'remote_project_path': 'o/r',
                   'branch': 'feat'},
        }
        assert PRTrackingMixin._revisit_tags(mon) == {'t1'}

    def test_missing_branch_excluded(self) -> None:
        mon = _FakeMon()
        mon._pinned_sessions = {
            't1': {'pr_merged': True, 'remote_project_path': 'o/r',
                   'branch': ''},
        }
        assert PRTrackingMixin._revisit_tags(mon) == set()

    def test_plain_tracked_row_not_revisit(self) -> None:
        mon = _FakeMon()
        mon._pinned_sessions = {
            't1': {'pr_tracked': True, 'remote_project_path': 'o/r',
                   'branch': 'feat'},
        }
        assert PRTrackingMixin._revisit_tags(mon) == set()


# ---------------------------------------------------------------------------
#  _persist_closed_pr
# ---------------------------------------------------------------------------

class TestPersistClosedPr:
    def _ctx(self) -> dict[str, Any]:
        return {'remote_project_path': 'o/r', 'host_url': 'https://h',
                'scm_type': 'github', 'branch': 'feat'}

    def test_merged_sets_merged_flag_only(self, _no_disk: list) -> None:
        mon = _FakeMon()
        mon._pinned_sessions['t1'] = {'tag': 't1'}
        PRTrackingMixin._persist_closed_pr(mon, 't1', self._ctx(), _closed(True))
        pin = mon._pinned_sessions['t1']
        assert pin['pr_merged'] is True
        assert pin['pr_closed'] is False
        assert pin['pr_url'] == 'https://h/o/r/pull/108'
        assert pin['pr_iid'] == 108
        assert pin['pr_tracked'] is False
        assert pin['branch'] == 'feat'

    def test_closed_unmerged_sets_closed_flag_only(self, _no_disk: list) -> None:
        mon = _FakeMon()
        mon._pinned_sessions['t1'] = {'tag': 't1'}
        PRTrackingMixin._persist_closed_pr(mon, 't1', self._ctx(), _closed(False))
        pin = mon._pinned_sessions['t1']
        assert pin['pr_merged'] is False
        assert pin['pr_closed'] is True

    def test_persists_and_invalidates_cache_and_syncs_timer(
            self, _no_disk: list) -> None:
        mon = _FakeMon()
        mon._pinned_sessions['t1'] = {'tag': 't1'}
        mon._cell_cache[('t1', 'pr')] = 'stale'
        PRTrackingMixin._persist_closed_pr(mon, 't1', self._ctx(), _closed(True))
        assert ('t1', 'pr') not in mon._cell_cache
        assert _no_disk == [('t1', mon._pinned_sessions['t1'])]
        assert mon._sync_calls == 1
        assert mon._update_table_calls == 1

    def test_exactly_one_state_flag_true(self, _no_disk: list) -> None:
        # The badge render keys off (pr_merged xor pr_closed) + pr_url.
        for merged in (True, False):
            mon = _FakeMon()
            mon._pinned_sessions['t1'] = {'tag': 't1'}
            PRTrackingMixin._persist_closed_pr(
                mon, 't1', self._ctx(), _closed(merged))
            pin = mon._pinned_sessions['t1']
            assert pin['pr_merged'] != pin['pr_closed']
            assert bool(pin['pr_url'])


# ---------------------------------------------------------------------------
#  _reopen_tracked_pr
# ---------------------------------------------------------------------------

class TestReopenTrackedPr:
    def test_clears_stale_flags_and_retracks(self, _no_disk: list) -> None:
        mon = _FakeMon()
        mon._pinned_sessions['t1'] = {
            'tag': 't1', 'pr_merged': True, 'remote_project_path': 'o/r',
            'branch': 'feat', 'pr_url': 'old'}
        status = PRStatus(state=PRState.ALL_RESPONDED, pr_url='new-url',
                          pr_title='New', pr_iid=200)
        PRTrackingMixin._reopen_tracked_pr(mon, 't1', status)
        pin = mon._pinned_sessions['t1']
        assert 'pr_merged' not in pin and 'pr_closed' not in pin
        assert pin['pr_tracked'] is True
        assert pin['pr_url'] == 'new-url'
        assert pin['pr_iid'] == 200
        assert 't1' in mon._tracked_tags

    def test_noop_when_tag_gone(self, _no_disk: list) -> None:
        mon = _FakeMon()
        status = PRStatus(state=PRState.ALL_RESPONDED)
        PRTrackingMixin._reopen_tracked_pr(mon, 'ghost', status)
        assert _no_disk == []
        assert 'ghost' not in mon._tracked_tags


# ---------------------------------------------------------------------------
#  _stop_tracking_closed_pr
# ---------------------------------------------------------------------------

class TestStopTrackingClosedPr:
    def test_drops_pr_fields_keeps_branch(self, _no_disk: list) -> None:
        mon = _FakeMon()
        mon._pinned_sessions['t1'] = {
            'tag': 't1', 'pr_merged': True, 'pr_url': 'u', 'pr_iid': 9,
            'pr_title': 'T', 'pr_tracked': False,
            'remote_project_path': 'o/r', 'branch': 'feat'}
        PRTrackingMixin._stop_tracking_closed_pr(mon, 't1')
        pin = mon._pinned_sessions['t1']
        for k in ('pr_merged', 'pr_closed', 'pr_title', 'pr_url',
                  'pr_iid', 'pr_tracked'):
            assert k not in pin
        # PR-branch keeper survives so the row stays + flips to Track PR.
        assert pin['remote_project_path'] == 'o/r'
        assert pin['branch'] == 'feat'
        assert mon._sync_calls == 1
        assert mon._update_table_calls == 1

    def test_clears_stale_pr_status(self, _no_disk: list) -> None:
        # The row stops being polled, so a lingering NO_PR status must clear.
        mon = _FakeMon()
        mon._pinned_sessions['t1'] = {
            'tag': 't1', 'pr_closed': True, 'pr_url': 'u',
            'remote_project_path': 'o/r', 'branch': 'feat'}
        mon._pr_statuses['t1'] = PRStatus(state=PRState.NO_PR)
        PRTrackingMixin._stop_tracking_closed_pr(mon, 't1')
        assert 't1' not in mon._pr_statuses

    def test_noop_when_tag_gone(self, _no_disk: list) -> None:
        mon = _FakeMon()
        PRTrackingMixin._stop_tracking_closed_pr(mon, 'ghost')
        assert _no_disk == []


# ---------------------------------------------------------------------------
#  _revisit_poll_sessions
# ---------------------------------------------------------------------------

class TestRevisitPollSessions:
    def test_builds_pr_only_status_watcher(self) -> None:
        mon = _FakeMon()
        mon._pinned_sessions = {
            't1': {'pr_merged': True, 'remote_project_path': 'o/r',
                   'branch': 'feat', 'scm_type': 'github'},
        }
        mon.sessions = [{'tag': 't1', 'remote_project_path': 'o/r',
                         'scm_type': 'github', 'branch': 'feat',
                         'pr_branch': 'feat', 'project_path': '/p'}]
        out = PRTrackingMixin._revisit_poll_sessions(mon)
        assert len(out) == 1
        s = out[0]
        # _pr_only is the whole point — must never deliver /leap while closed.
        assert s['_pr_only'] is True
        assert s['remote_project_path'] == 'o/r'
        assert s['scm_type'] == 'github'
        assert s['branch'] == 'feat'

    def test_branch_prefers_pr_branch(self) -> None:
        mon = _FakeMon()
        mon._pinned_sessions = {
            't1': {'pr_closed': True, 'remote_project_path': 'o/r',
                   'branch': 'feat'},
        }
        # Live session whose local branch drifted — poll must use the PR branch.
        mon.sessions = [{'tag': 't1', 'remote_project_path': 'o/r',
                         'pr_branch': 'feat', 'branch': 'drifted-local'}]
        assert PRTrackingMixin._revisit_poll_sessions(mon)[0]['branch'] == 'feat'

    def test_tracked_tag_excluded(self) -> None:
        mon = _FakeMon()
        mon._tracked_tags = {'t1'}
        mon._pinned_sessions = {
            't1': {'pr_merged': True, 'remote_project_path': 'o/r',
                   'branch': 'feat'},
        }
        mon.sessions = [{'tag': 't1', 'remote_project_path': 'o/r',
                         'branch': 'feat', 'pr_branch': 'feat'}]
        assert PRTrackingMixin._revisit_poll_sessions(mon) == []

    def test_pinned_but_no_session_not_polled(self) -> None:
        # _revisit_tags would include it, but with no session dict there's
        # nothing to poll against.
        mon = _FakeMon()
        mon._pinned_sessions = {
            't1': {'pr_merged': True, 'remote_project_path': 'o/r',
                   'branch': 'feat'},
        }
        mon.sessions = []
        assert PRTrackingMixin._revisit_poll_sessions(mon) == []

    def test_does_not_mutate_live_session_dicts(self) -> None:
        # The poll dicts must be fresh copies — never tag the real
        # self.sessions object with _pr_only (other code reads it).
        mon = _FakeMon()
        mon._pinned_sessions = {
            't1': {'pr_merged': True, 'remote_project_path': 'o/r',
                   'branch': 'feat'},
        }
        live = {'tag': 't1', 'remote_project_path': 'o/r',
                'branch': 'feat', 'pr_branch': 'feat'}
        mon.sessions = [live]
        PRTrackingMixin._revisit_poll_sessions(mon)
        assert '_pr_only' not in live


# ---------------------------------------------------------------------------
#  _on_scm_results — open->NO_PR transition + re-open detection
# ---------------------------------------------------------------------------

class TestOnScmResultsDetection:
    def _run(self, mon: _FakeMon, results: dict[str, PRStatus]) -> None:
        PRTrackingMixin._on_scm_results(mon, results)

    def test_tracked_open_to_no_pr_schedules_closed_check(self) -> None:
        mon = _FakeMon()
        mon._tracked_tags = {'t1'}
        mon._pr_statuses['t1'] = PRStatus(state=PRState.UNRESPONDED,
                                          unresponded_count=1)
        self._run(mon, {'t1': PRStatus(state=PRState.NO_PR)})
        assert mon.closed_checks == ['t1']

    def test_no_pr_to_no_pr_does_not_reschedule(self) -> None:
        mon = _FakeMon()
        mon._tracked_tags = {'t1'}
        mon._pr_statuses['t1'] = PRStatus(state=PRState.NO_PR)
        self._run(mon, {'t1': PRStatus(state=PRState.NO_PR)})
        assert mon.closed_checks == []

    def test_first_seen_no_pr_does_not_schedule(self) -> None:
        # No previous status on record -> nothing to confirm "was open".
        mon = _FakeMon()
        mon._tracked_tags = {'t1'}
        self._run(mon, {'t1': PRStatus(state=PRState.NO_PR)})
        assert mon.closed_checks == []

    def test_untracked_no_pr_ignored(self) -> None:
        mon = _FakeMon()
        mon._pr_statuses['t1'] = PRStatus(state=PRState.UNRESPONDED)
        self._run(mon, {'t1': PRStatus(state=PRState.NO_PR)})
        assert mon.closed_checks == []

    def test_revisit_row_with_open_pr_reopens(self) -> None:
        mon = _FakeMon()
        mon._pinned_sessions['t1'] = {'pr_merged': True,
                                      'remote_project_path': 'o/r',
                                      'branch': 'feat'}
        status = PRStatus(state=PRState.ALL_RESPONDED, pr_url='u', pr_iid=5)
        self._run(mon, {'t1': status})
        assert mon.reopens == [('t1', status)]
        # re-open triggers a full table rebuild (badge cell -> tracked cell)
        assert mon._update_table_calls == 1

    def test_revisit_row_still_no_pr_does_not_reopen(self) -> None:
        mon = _FakeMon()
        mon._pinned_sessions['t1'] = {'pr_closed': True,
                                      'remote_project_path': 'o/r',
                                      'branch': 'feat'}
        self._run(mon, {'t1': PRStatus(state=PRState.NO_PR)})
        assert mon.reopens == []
        # no reopen -> fast path only
        assert mon._update_pr_column_calls == 1
        assert mon._update_table_calls == 0


class TestFireOnNewSignals:
    """The 🔥 'recently changed' nudge must fire when changes-requested or
    CI-failed flips (they're part of the change snapshot)."""

    def _run(self, mon: _FakeMon, results: dict[str, PRStatus]) -> None:
        PRTrackingMixin._on_scm_results(mon, results)

    def test_changes_requested_flip_fires(self) -> None:
        mon = _FakeMon()
        base = PRStatus(state=PRState.ALL_RESPONDED)
        self._run(mon, {'t1': base})           # first sight -> seeded epoch 0
        assert mon._pr_changed_at['t1'][1] == 0
        self._run(mon, {'t1': PRStatus(state=PRState.ALL_RESPONDED,
                                       changes_requested=True)})
        assert mon._pr_changed_at['t1'][1] != 0  # snapshot changed -> fire

    def test_checks_failed_flip_fires(self) -> None:
        mon = _FakeMon()
        self._run(mon, {'t1': PRStatus(state=PRState.UNRESPONDED,
                                       unresponded_count=1)})
        assert mon._pr_changed_at['t1'][1] == 0
        self._run(mon, {'t1': PRStatus(state=PRState.UNRESPONDED,
                                       unresponded_count=1,
                                       checks_failed=True)})
        assert mon._pr_changed_at['t1'][1] != 0

    def test_no_change_does_not_fire(self) -> None:
        mon = _FakeMon()
        s = PRStatus(state=PRState.ALL_RESPONDED, changes_requested=True,
                     checks_failed=True)
        self._run(mon, {'t1': s})
        self._run(mon, {'t1': PRStatus(state=PRState.ALL_RESPONDED,
                                       changes_requested=True,
                                       checks_failed=True)})
        assert mon._pr_changed_at['t1'][1] == 0  # identical snapshot -> no fire
