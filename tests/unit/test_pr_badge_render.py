"""Headless (offscreen) tests for the Merged/Closed PR badge + its SVG icons.

Like test_pr_markers, these drive real Qt code the rest of the suite never
touches: _render_closed_pr_cell (builds the badge widget) and the new
git_merge_icon / git_pr_closed_icon (SVG -> QSvgRenderer -> QPixmap).  No
window is shown.  They catch runtime bugs that py_compile + logic tests miss:
a broken icon render, a bad QSize/property/signal wiring, wrong label/color/
tooltip, or a recolor token that doesn't match the SVG.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

from PyQt5.QtGui import qAlpha
from PyQt5.QtWidgets import QApplication, QPushButton

from leap.monitor._mixins.table_builder_mixin import TableBuilderMixin
from leap.monitor.themes import current_theme
from leap.monitor.ui.table_helpers import git_merge_icon, git_pr_closed_icon


@pytest.fixture(scope='module')
def _qapp() -> Any:
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------
#  SVG icon rendering
# ---------------------------------------------------------------------------

def _image(icon: Any, size: int = 16) -> Any:
    pm = icon.pixmap(size, size)
    assert not pm.isNull()
    return pm.toImage()


def _has_visible_pixels(img: Any) -> bool:
    return any(qAlpha(img.pixel(x, y)) > 0
               for x in range(img.width()) for y in range(img.height()))


class TestBadgeIcons:
    def test_merge_icon_renders_non_blank(self, _qapp: Any) -> None:
        icon = git_merge_icon(16, b'#a371f7')
        assert not icon.isNull()
        assert _has_visible_pixels(_image(icon)), 'git-merge SVG rendered blank'

    def test_pr_closed_icon_renders_non_blank(self, _qapp: Any) -> None:
        icon = git_pr_closed_icon(16, b'#cf222e')
        assert not icon.isNull()
        assert _has_visible_pixels(_image(icon)), 'pr-closed SVG rendered blank'

    def test_merge_icon_recolor_takes_effect(self, _qapp: Any) -> None:
        # Two different stroke colors must produce different pixels - proves
        # the #aaa recolor token actually matches the SVG stroke.
        a = _image(git_merge_icon(16, b'#a371f7'))
        b = _image(git_merge_icon(16, b'#ff0000'))
        assert any(a.pixel(x, y) != b.pixel(x, y)
                   for x in range(16) for y in range(16))

    def test_closed_icon_recolor_takes_effect(self, _qapp: Any) -> None:
        a = _image(git_pr_closed_icon(16, b'#cf222e'))
        b = _image(git_pr_closed_icon(16, b'#00ff00'))
        assert any(a.pixel(x, y) != b.pixel(x, y)
                   for x in range(16) for y in range(16))


# ---------------------------------------------------------------------------
#  _render_closed_pr_cell
# ---------------------------------------------------------------------------

def _fake_self() -> Any:
    fs = SimpleNamespace()
    fs.COL_PR = 13
    fs.table = SimpleNamespace(columnSpan=lambda r, c: 1,
                               setSpan=lambda *a: None)
    fs._cell_cached = lambda *a, **k: False
    fs._zoomed_size = lambda off=0: 13 + off
    fs._zoomed_btn_w = lambda w: w
    fs._captured = None

    def _scw(r: int, c: int, w: Any) -> None:
        fs._captured = w
    fs._set_cell_widget = _scw
    fs._apply_row_color_to_widget = lambda w, rc: None
    fs._cache_cell = lambda *a, **k: None
    fs._stop_tracking_closed_pr = lambda t: None
    return fs


def _render(fs: Any, kind: str, *, pr_url: str = 'https://h/pr/9',
            pr_iid: Any = 9, pr_title: str = 'My PR',
            row_color: Any = None) -> Any:
    TableBuilderMixin._render_closed_pr_cell(
        fs, 0, 'mytag', kind, pr_url, pr_iid, pr_title, row_color)
    return fs._captured


def _badge(container: Any, label: str) -> Any:
    return next(b for b in container.findChildren(QPushButton)
                if b.text() == label)


class TestRenderClosedPrCell:
    def test_merged_badge(self, _qapp: Any) -> None:
        c = _render(_fake_self(), 'merged')
        assert c is not None
        badge = _badge(c, 'Merged')
        assert current_theme().pr_merged_color in badge.styleSheet()
        assert badge.property('_btn_role') == 'pr_accent'
        assert badge.property('_pr_accent_color') == current_theme().pr_merged_color
        assert not badge.icon().isNull()
        assert badge.toolTip() == 'Open merged PR !9: My PR'

    def test_closed_badge(self, _qapp: Any) -> None:
        c = _render(_fake_self(), 'closed')
        badge = _badge(c, 'Closed')
        assert current_theme().accent_red in badge.styleSheet()
        assert badge.property('_pr_accent_color') == current_theme().accent_red
        assert not badge.icon().isNull()

    def test_close_x_button_present(self, _qapp: Any) -> None:
        c = _render(_fake_self(), 'merged')
        x = _badge(c, '×')
        assert x.property('_btn_role') == 'close'
        assert 'mytag' in x.toolTip()

    def test_tooltip_iid_only(self, _qapp: Any) -> None:
        c = _render(_fake_self(), 'merged', pr_title='')
        assert _badge(c, 'Merged').toolTip() == 'Open merged PR !9'

    def test_tooltip_no_iid(self, _qapp: Any) -> None:
        c = _render(_fake_self(), 'closed', pr_iid=None, pr_title='')
        assert _badge(c, 'Closed').toolTip() == 'Open closed PR in browser'

    def test_cache_hit_skips_build(self, _qapp: Any) -> None:
        fs = _fake_self()
        fs._cell_cached = lambda *a, **k: True
        TableBuilderMixin._render_closed_pr_cell(
            fs, 0, 'mytag', 'merged', 'u', 1, 'T', None)
        assert fs._captured is None  # build short-circuited
