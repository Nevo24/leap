"""Tests for _extract_menu_options — the numbered-option parser used by
both the server (select_option/custom_answer handlers) and the monitor
(right-click permission menu).
"""

import pytest

from claudeq.server.server import _extract_menu_options


class TestExtractMenuOptions:
    """Core extraction logic."""

    def test_simple_options(self) -> None:
        prompt = (
            "Allow Claude to use Bash?\n"
            "❯ 1. Allow once\n"
            "  2. Allow always\n"
            "  3. Deny\n"
        )
        assert _extract_menu_options(prompt) == [
            (1, "Allow once"),
            (2, "Allow always"),
            (3, "Deny"),
        ]

    def test_plan_content_above_options(self) -> None:
        """Numbered plan steps above the actual menu must be ignored."""
        prompt = (
            "Live Preview Implementation\n"
            "1. SettingsDialog.__init__ receives a callback\n"
            "2. Theme combo currentTextChanged triggers on_theme_change(name)\n"
            "3. MonitorWindow passes self._apply_theme as the callback\n"
            "4. On Cancel/reject, SettingsDialog.done() calls on_theme_change\n"
            "\n"
            "Verification\n"
            "1. poetry run python -c 'check themes'\n"
            "2. make run-monitor\n"
            "3. Verify: switching to Dawn\n"
            "4. Verify: closing settings\n"
            "5. Verify: theme persists\n"
            "6. poetry run pytest tests/ -v\n"
            "\n"
            "Claude has written up a plan. Would you like to proceed?\n"
            "❯ 1. Yes, clear context (38% used) and bypass permissions\n"
            "  2. Yes, and bypass permissions\n"
            "  3. Yes, manually approve edits\n"
            "  4. Type here to tell Claude what to change\n"
        )
        assert _extract_menu_options(prompt) == [
            (1, "Yes, clear context (38% used) and bypass permissions"),
            (2, "Yes, and bypass permissions"),
            (3, "Yes, manually approve edits"),
            (4, "Type here to tell Claude what to change"),
        ]

    def test_single_numbered_block(self) -> None:
        """When there's only one group of numbered lines, return all."""
        prompt = (
            "Would you like to proceed?\n"
            "❯ 1. Yes\n"
            "  2. No\n"
        )
        assert _extract_menu_options(prompt) == [
            (1, "Yes"),
            (2, "No"),
        ]

    def test_empty_output(self) -> None:
        assert _extract_menu_options("") == []

    def test_no_numbered_lines(self) -> None:
        prompt = "Some text without any numbers.\nAnother line.\n"
        assert _extract_menu_options(prompt) == []

    def test_multiple_restarts_picks_last(self) -> None:
        """Three groups starting from 1 — only the last one counts."""
        prompt = (
            "1. First group item A\n"
            "2. First group item B\n"
            "3. First group item C\n"
            "\n"
            "1. Second group item A\n"
            "2. Second group item B\n"
            "\n"
            "❯ 1. Actual option A\n"
            "  2. Actual option B\n"
        )
        assert _extract_menu_options(prompt) == [
            (1, "Actual option A"),
            (2, "Actual option B"),
        ]

    def test_non_contiguous_sequence_stops(self) -> None:
        """If numbering jumps (1, 2, 5), stop after the gap."""
        prompt = (
            "❯ 1. Option A\n"
            "  2. Option B\n"
            "  5. Option E\n"
        )
        assert _extract_menu_options(prompt) == [
            (1, "Option A"),
            (2, "Option B"),
        ]

    def test_no_number_one_fallback(self) -> None:
        """If no line starts with 1, return all matches as fallback."""
        prompt = (
            "  2. Option B\n"
            "  3. Option C\n"
        )
        assert _extract_menu_options(prompt) == [
            (2, "Option B"),
            (3, "Option C"),
        ]

    def test_cursor_marker_stripped(self) -> None:
        """The ❯ cursor prefix should be handled transparently."""
        prompt = (
            "  1. Not selected\n"
            "❯ 2. Selected\n"
            "  3. Also not selected\n"
        )
        result = _extract_menu_options(prompt)
        assert result == [
            (1, "Not selected"),
            (2, "Selected"),
            (3, "Also not selected"),
        ]

    def test_type_something_option(self) -> None:
        """'Type something' options are returned normally for callers to handle."""
        prompt = (
            "❯ 1. Allow\n"
            "  2. Deny\n"
            "  3. Type something to tell Claude what to change\n"
        )
        result = _extract_menu_options(prompt)
        assert len(result) == 3
        assert result[2] == (3, "Type something to tell Claude what to change")
