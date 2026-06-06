"""Tests for PRTrackingMixin._pr_fire_snapshot_changed.

The 🔥 "recently changed" nudge must re-arm on real PR changes but NOT on a
transiently-failed-then-recovered approvals fetch (unknown -> known), mirroring
the dock/banner approval_known guard.

Snapshot layout: (state, unresponded_count, approved, approved_by,
approval_known, changes_requested, checks_failed).
"""

from __future__ import annotations

from leap.monitor._mixins.pr_tracking_mixin import PRTrackingMixin
from leap.monitor.pr_tracking.base import PRState

_changed = PRTrackingMixin._pr_fire_snapshot_changed


def _snap(state=PRState.ALL_RESPONDED, count=0, approved=False,
          approved_by=(), known=True, cr=False, cf=False) -> tuple:
    return (state, count, approved, tuple(approved_by), known, cr, cf)


class TestApprovalKnownMasking:
    def test_unknown_to_known_approval_is_not_a_change(self) -> None:
        # Poll 1: approvals fetch failed (unknown, looks like "no approvers").
        old = _snap(approved=False, approved_by=(), known=False)
        # Poll 2: fetch recovered and shows an approval.
        new = _snap(approved=True, approved_by=('Yarden Goor',), known=True)
        assert _changed(old, new) is False

    def test_known_to_unknown_approval_is_not_a_change(self) -> None:
        old = _snap(approved=True, approved_by=('Yarden Goor',), known=True)
        new = _snap(approved=False, approved_by=(), known=False)
        assert _changed(old, new) is False

    def test_real_new_approver_when_both_known_is_a_change(self) -> None:
        old = _snap(approved=False, approved_by=(), known=True)
        new = _snap(approved=True, approved_by=('Yarden Goor',), known=True)
        assert _changed(old, new) is True


class TestNonApprovalFields:
    def test_unresponded_count_change_fires(self) -> None:
        assert _changed(_snap(count=1), _snap(count=2)) is True

    def test_state_change_fires(self) -> None:
        old = _snap(state=PRState.ALL_RESPONDED)
        new = _snap(state=PRState.UNRESPONDED, count=1)
        assert _changed(old, new) is True

    def test_changes_requested_fires(self) -> None:
        assert _changed(_snap(cr=False), _snap(cr=True)) is True

    def test_checks_failed_fires(self) -> None:
        assert _changed(_snap(cf=False), _snap(cf=True)) is True

    def test_identical_snapshot_is_no_change(self) -> None:
        assert _changed(_snap(), _snap()) is False

    def test_non_approval_change_fires_even_when_approval_unknown(self) -> None:
        # A real count change must still fire even if approval is unknown.
        old = _snap(count=1, known=False)
        new = _snap(count=2, known=False)
        assert _changed(old, new) is True
