"""Headless (offscreen) tests for _apply_pr_status's marker/status rendering.

These exercise the real Qt method - which the rest of the suite never calls -
against a hand-built cell container (a PulsingLabel status + three
IndicatorLabel markers found by objectName + an approval IndicatorLabel),
mirroring what _render_tracked_pr_cell builds.  No window is shown
(QT_QPA_PLATFORM=offscreen), so this is a pure-logic check of:
  - which markers show/hide per state + flags,
  - the conflict marker being orange while the ✓ stays GREEN (the bug the
    user hit),
  - the 👍 / 👎 approval indicator.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

from PyQt5.QtWidgets import QApplication, QHBoxLayout, QWidget

from leap.monitor._mixins.pr_display_mixin import PRDisplayMixin
from leap.monitor.pr_tracking.base import PRState, PRStatus
from leap.monitor.themes import current_theme
from leap.monitor.ui.ui_widgets import (
    IndicatorLabel, IndicatorPopup, PulsingLabel,
)

_MARKER_NAMES = ('_draftMarker', '_conflictMarker', '_checksMarker')


@pytest.fixture(scope='module')
def _qapp() -> Any:
    app = QApplication.instance() or QApplication([])
    yield app


def _build_cell() -> tuple:
    """Mirror the relevant slice of _render_tracked_pr_cell's layout."""
    container = QWidget()
    lay = QHBoxLayout(container)
    approval = IndicatorLabel()
    pr_widget = PulsingLabel()
    markers = {}
    lay.addWidget(approval)
    for nm in _MARKER_NAMES:
        m = IndicatorLabel()
        m.setObjectName(nm)
        m.setVisible(False)
        lay.addWidget(m)
        markers[nm] = m
    lay.addWidget(pr_widget)
    return container, pr_widget, approval, markers


def _fake_self() -> Any:
    fs = SimpleNamespace(_scm_providers={'github': object()})
    fs._format_approval_line = PRDisplayMixin._format_approval_line
    return fs


def _apply(pr_widget: Any, approval: Any, status: Any) -> None:
    PRDisplayMixin._apply_pr_status(_fake_self(), pr_widget, approval, status)


def _shown(marker: Any) -> bool:
    # isVisible() is False while the container isn't shown; isHidden()
    # reflects the explicit setVisible state we care about.
    return not marker.isHidden()


class TestMarkerVisibility:
    def test_conflict_only(self, _qapp: Any) -> None:
        _c, pr, appr, m = _build_cell()
        _apply(pr, appr, PRStatus(state=PRState.ALL_RESPONDED,
                                  has_conflicts=True))
        assert _shown(m['_conflictMarker'])
        assert not _shown(m['_draftMarker'])
        assert not _shown(m['_checksMarker'])
        assert '⚠' in m['_conflictMarker'].text()

    def test_draft_and_checks_on_unresponded(self, _qapp: Any) -> None:
        _c, pr, appr, m = _build_cell()
        _apply(pr, appr, PRStatus(state=PRState.UNRESPONDED,
                                  unresponded_count=2, draft=True,
                                  checks_failed=True))
        assert _shown(m['_draftMarker'])
        assert _shown(m['_checksMarker'])
        assert not _shown(m['_conflictMarker'])

    def test_no_flags_all_hidden(self, _qapp: Any) -> None:
        _c, pr, appr, m = _build_cell()
        _apply(pr, appr, PRStatus(state=PRState.ALL_RESPONDED))
        assert all(not _shown(m[n]) for n in _MARKER_NAMES)

    def test_no_pr_hides_markers(self, _qapp: Any) -> None:
        _c, pr, appr, m = _build_cell()
        # markers were left "shown" by a prior conflict state...
        _apply(pr, appr, PRStatus(state=PRState.ALL_RESPONDED,
                                  has_conflicts=True))
        assert _shown(m['_conflictMarker'])
        # ...and must hide when the PR goes away.
        _apply(pr, appr, PRStatus(state=PRState.NO_PR))
        assert all(not _shown(m[n]) for n in _MARKER_NAMES)
        assert pr.text() == 'No PR'

    def test_flags_ignored_when_not_open(self, _qapp: Any) -> None:
        # draft/conflict/checks are meaningless without an open PR.
        _c, pr, appr, m = _build_cell()
        _apply(pr, appr, PRStatus(state=PRState.NO_PR, has_conflicts=True,
                                  draft=True, checks_failed=True))
        assert all(not _shown(m[n]) for n in _MARKER_NAMES)


class TestStatusColorIndependentOfConflict:
    """The user's core fix: a conflict must NOT tint the ✓ - only the
    separate ⚠ marker is orange; the ✓ stays green."""

    def test_checkmark_green_even_with_conflict(self, _qapp: Any) -> None:
        _c, pr, appr, m = _build_cell()
        _apply(pr, appr, PRStatus(state=PRState.ALL_RESPONDED,
                                  has_conflicts=True))
        green = current_theme().accent_green
        orange = current_theme().accent_orange
        assert pr.text() == '✓'
        assert green in pr.styleSheet()
        assert orange not in pr.styleSheet()
        # ...while the conflict marker IS orange.
        assert orange in m['_conflictMarker'].styleSheet()

    def test_checkmark_green_without_conflict(self, _qapp: Any) -> None:
        _c, pr, appr, m = _build_cell()
        _apply(pr, appr, PRStatus(state=PRState.ALL_RESPONDED))
        assert current_theme().accent_green in pr.styleSheet()


class TestIndicatorPopupSizing:
    """A short tooltip must stay on one line, not collapse to one-word-per-row
    (the word-wrap QLabel sizeHint quirk)."""

    def test_short_text_fits_one_line(self, _qapp: Any) -> None:
        p = IndicatorPopup()
        p.setText('Has merge conflicts')
        # Wide enough for the whole string on one line => no per-word wrap.
        assert p.width() >= p.fontMetrics().horizontalAdvance('Has merge conflicts')

    def test_multiline_uses_widest_line(self, _qapp: Any) -> None:
        p = IndicatorPopup()
        p.setText('short\na considerably longer line here')
        assert p.width() >= p.fontMetrics().horizontalAdvance(
            'a considerably longer line here')

    def test_long_text_capped(self, _qapp: Any) -> None:
        p = IndicatorPopup()
        p.setText('x' * 400)
        assert p.width() <= IndicatorPopup._MAX_WIDTH


class TestApprovalIndicator:
    def test_approved_shows_thumbs_up(self, _qapp: Any) -> None:
        _c, pr, appr, _m = _build_cell()
        _apply(pr, appr, PRStatus(state=PRState.ALL_RESPONDED, approved=True,
                                  approved_by=['alice']))
        assert _shown(appr)
        assert appr.text() == '\U0001f44d'

    def test_changes_requested_shows_thumbs_down(self, _qapp: Any) -> None:
        _c, pr, appr, _m = _build_cell()
        _apply(pr, appr, PRStatus(state=PRState.UNRESPONDED,
                                  unresponded_count=1, changes_requested=True))
        assert _shown(appr)
        assert appr.text() == '\U0001f44e'

    def test_changes_requested_beats_approved(self, _qapp: Any) -> None:
        # 👎 (blocking) wins when a PR is both approved and changes-requested.
        _c, pr, appr, _m = _build_cell()
        _apply(pr, appr, PRStatus(state=PRState.ALL_RESPONDED, approved=True,
                                  approved_by=['alice'],
                                  changes_requested=True))
        assert appr.text() == '\U0001f44e'

    def test_neither_hides_approval(self, _qapp: Any) -> None:
        _c, pr, appr, _m = _build_cell()
        _apply(pr, appr, PRStatus(state=PRState.ALL_RESPONDED))
        assert not _shown(appr)
