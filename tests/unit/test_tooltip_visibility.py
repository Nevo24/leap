"""Tests for the table-item tooltip show/hide decision.

Pure logic (no Qt): ``should_show_item_tooltip`` decides whether a table
item's tooltip appears on hover, honoring the global "show tooltips" setting,
display truncation, and the per-column always-on flag (``_always_tooltip_cols``,
used by the Context column so its token counts are never hidden).
"""

from __future__ import annotations

from leap.monitor.ui.table_helpers import should_show_item_tooltip


class TestShouldShowItemTooltip:
    def test_no_tip_never_shows(self):
        assert should_show_item_tooltip('', '28%', False, True, True) is False

    def test_differing_tip_shown_when_enabled(self):
        # tip != display, setting on -> show
        assert should_show_item_tooltip('123 tokens', '28%', False, True, False)

    def test_differing_tip_hidden_when_disabled(self):
        # tip != display, setting off, not truncated, not always -> hidden
        assert not should_show_item_tooltip('123 tokens', '28%', False, False, False)

    def test_always_forces_show_even_when_disabled(self):
        # The Context column case: setting off, not truncated, but always-on.
        assert should_show_item_tooltip('123 tokens', '28%', False, False, True)

    def test_always_does_not_apply_to_na_value(self):
        # N/A is a no-value sentinel: even an always-on column falls back to
        # the normal truncated-only rule (Context shows the token tooltip for a
        # real % but not for N/A when the setting is off).
        assert not should_show_item_tooltip('N/A', 'N/A', False, False, True)

    def test_always_does_not_apply_to_blank_value(self):
        assert not should_show_item_tooltip('anything', '', False, False, True)

    def test_na_still_shows_when_truncated(self):
        # Like every other cell, N/A still shows on hover if it's clipped.
        assert should_show_item_tooltip('N/A', 'N/A', True, False, True)

    def test_truncation_forces_show_even_when_disabled(self):
        assert should_show_item_tooltip('long value', 'long v…', True, False, False)

    def test_echoing_tip_only_shows_when_truncated_or_always(self):
        # tip == display: nothing extra to reveal unless truncated/always.
        assert not should_show_item_tooltip('abc', 'abc', False, True, False)
        assert should_show_item_tooltip('abc', 'abc', True, False, False)
        assert should_show_item_tooltip('abc', 'abc', False, False, True)

    def test_no_display_text_never_shows_here(self):
        # Widget-backed cells (no display role) are handled elsewhere.
        assert not should_show_item_tooltip('tip', '', False, True, True)
        assert not should_show_item_tooltip('tip', None, False, True, True)
