"""Tests for the saved-window-geometry sanity gate used on monitor startup.

The monitor restores its window from ``window_geometry`` ([x, y, w, h]) in
``monitor_prefs.json`` at two sites: ``_init_ui`` (Qt ``setGeometry``) and
``_apply_window_effects`` (the exact NSWindow frame).  Older builds restored
from a Qt ``saveGeometry()`` blob instead and filled ``window_geometry`` from
``normalGeometry()``, which is unreliable for a window only ever used
maximized - it leaves a degenerate ~500x500 value on disk.  Once the restore
mechanism switched to reading ``window_geometry`` verbatim, that stale value
reopened the monitor as a tiny window after an update.

``_is_restorable_geometry`` is the shared guard both restore sites gate on:
malformed or sub-floor sizes are rejected so the window falls back to its
default centered size instead of the corrupt one.  These are pure-logic tests
against that guard (no Qt window is created).
"""

from __future__ import annotations

from typing import Any, Optional

from leap.monitor.app import MonitorWindow


class _Stub:
    """Carries just the floor constants the guard reads off ``self``."""

    _MIN_RESTORE_WIDTH = MonitorWindow._MIN_RESTORE_WIDTH
    _MIN_RESTORE_HEIGHT = MonitorWindow._MIN_RESTORE_HEIGHT


def _check(geom: Optional[Any]) -> bool:
    return MonitorWindow._is_restorable_geometry(_Stub(), geom)


class TestIsRestorableGeometry:
    def test_none_rejected(self) -> None:
        assert _check(None) is False

    def test_empty_rejected(self) -> None:
        assert _check([]) is False

    def test_wrong_length_rejected(self) -> None:
        assert _check([0, 0, 500]) is False
        assert _check([0, 0, 800, 600, 1]) is False

    def test_non_sequence_rejected(self) -> None:
        # A scalar-corrupted value: ``len()`` would raise - must not.
        assert _check(5) is False
        assert _check({'x': 0}) is False

    def test_non_numeric_dimensions_rejected(self) -> None:
        # Hand-edited / corrupt prefs with string dimensions must yield False,
        # not raise - ``_init_ui`` calls the guard outside any try/except.
        assert _check(['0', '0', '500', '500']) is False
        assert _check([0, 0, '1500', '800']) is False
        # x/y corrupt but w/h valid: still rejected (``_init_ui`` uses x/y too).
        assert _check(['0', '0', 1500, 800]) is False
        assert _check([None, 0, 1500, 800]) is False

    def test_float_dimensions_rejected(self) -> None:
        # Floats are int-only downstream: QPoint()/setGeometry() raise TypeError
        # on floats, and our own save path always writes ints.  A float-valued
        # (hand-edited) geometry must be rejected, not passed through to crash
        # startup at the un-guarded ``_init_ui`` call site.
        assert _check([0.0, 0.0, 1500.0, 800.0]) is False
        assert _check([0, 0, 1500, 800.0]) is False

    def test_stale_normalgeometry_default_rejected(self) -> None:
        # The exact corrupt value older builds left on disk for a
        # maximized-only window.  This is the regression this guard exists for.
        assert _check([0, 482, 500, 500]) is False

    def test_below_floor_either_dimension_rejected(self) -> None:
        w = MonitorWindow._MIN_RESTORE_WIDTH
        h = MonitorWindow._MIN_RESTORE_HEIGHT
        assert _check([0, 0, w - 1, h]) is False  # width 1px under
        assert _check([0, 0, w, h - 1]) is False  # height 1px under

    def test_floor_exactly_accepted(self) -> None:
        assert _check([0, 0, MonitorWindow._MIN_RESTORE_WIDTH,
                       MonitorWindow._MIN_RESTORE_HEIGHT]) is True

    def test_default_size_accepted(self) -> None:
        assert _check([0, 0, 1476, 719]) is True

    def test_real_maximized_size_accepted(self) -> None:
        assert _check([0, 30, 2560, 1296]) is True
