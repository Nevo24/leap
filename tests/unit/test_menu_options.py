"""Tests for extract_menu_options — the numbered-option parser used by
both the server (select_option/custom_answer handlers) and the monitor
(right-click permission menu).
"""

import signal
import time

import pytest

from leap.cli_providers import get_provider
from leap.utils.menu import (
    MENU_OPTION_RE,
    extract_menu_options,
    extract_menu_question,
)


class TestExtractMenuOptions:
    """Core extraction logic."""

    def test_simple_options(self) -> None:
        prompt = (
            "Allow Claude to use Bash?\n"
            "❯ 1. Allow once\n"
            "  2. Allow always\n"
            "  3. Deny\n"
        )
        assert extract_menu_options(prompt) == [
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
        assert extract_menu_options(prompt) == [
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
        assert extract_menu_options(prompt) == [
            (1, "Yes"),
            (2, "No"),
        ]

    def test_empty_output(self) -> None:
        assert extract_menu_options("") == []

    def test_no_numbered_lines(self) -> None:
        prompt = "Some text without any numbers.\nAnother line.\n"
        assert extract_menu_options(prompt) == []

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
        assert extract_menu_options(prompt) == [
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
        assert extract_menu_options(prompt) == [
            (1, "Option A"),
            (2, "Option B"),
        ]

    def test_no_number_one_fallback(self) -> None:
        """If no line starts with 1, return all matches as fallback."""
        prompt = (
            "  2. Option B\n"
            "  3. Option C\n"
        )
        assert extract_menu_options(prompt) == [
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
        result = extract_menu_options(prompt)
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
        result = extract_menu_options(prompt)
        assert len(result) == 3
        assert result[2] == (3, "Type something to tell Claude what to change")

    def test_missing_period_after_digit(self) -> None:
        # When the pyte snapshot is corrupted by overlapping TUI frames,
        # the period after the option number can disappear, leaving just
        # whitespace.  Auto-approve must still find the "Yes" option.
        # 2+ spaces is the signature of the corruption: the original cell
        # gap between digit and label, exposed when the period is gone.
        prompt = (
            "Do you want to create __init__.py?\n"
            " ❯ 1  Yes\n"
            "   2. Yes, allow all edits during this session (shift+tab)\n"
            "   3. No\n"
        )
        assert extract_menu_options(prompt, get_provider('claude')) == [
            (1, "Yes"),
            (2, "Yes, allow all edits during this session (shift+tab)"),
            (3, "No"),
        ]

    def test_corrupted_snapshot_partial_options(self) -> None:
        # Worst-case corruption matching what was observed in the wild:
        # line 1 missing its period (extractable via the 2-space branch),
        # line 2 has an "Es" prefix bleeding from the footer (extracted
        # but with garbage in the label), line 3 clean.  Auto-approve's
        # letters-only Yes-match correctly picks (1, "Yes") and rejects
        # (2, "Yes,nallow...") whose letters don't reduce to "yes".
        prompt = (
            "Do you want to create __init__.py?\n"
            " ❯ 1  Yes\n"
            " Es2. Yes,nallow all editseduring this session (shift+tab)\n"
            "   3. No\n"
        )
        result = extract_menu_options(prompt, get_provider('claude'))
        assert (1, "Yes") in result
        # Verify (2, ...) has corrupted label that fails the letters-only
        # "Yes" check — auto-approve must NOT pick this broader option.
        for num, label in result:
            if num == 2:
                letters = ''.join(c for c in label if c.isalpha()).lower()
                assert letters != 'yes', (
                    f'option 2 label {label!r} reduces to {letters!r} '
                    f'and would be incorrectly picked as Yes'
                )

    def test_single_space_after_digit_not_a_menu(self) -> None:
        # Conversational/status text like "1 file changed" must NOT parse
        # as a menu option (would cause auto-approve to pick the wrong
        # row when prose appears anywhere in the snapshot).
        prompt = "1 file changed\n12 minutes remaining\n"
        assert extract_menu_options(prompt, get_provider('claude')) == []

    def test_bordered_dialog_with_cursor(self) -> None:
        # When Claude's Ink TUI wraps the permission dialog in a bordered
        # box, the SELECTED row in pyte's buffer reads as
        # "│ ❯ 1. Yes" — TWO non-digit-non-space clusters (border + cursor)
        # separated by whitespace before the digit.  Without multi-cluster
        # prefix support, only the unselected rows (single border cluster)
        # parsed, so the parser returned [(2, "Yes, allow..."), (3, "No")]
        # and auto-approve's letters-only "Yes" search came up empty —
        # the dialog sat indefinitely even with Always-send mode on.
        prompt = (
            "Do you want to proceed?\n"
            "│ ❯ 1. Yes\n"
            "│   2. Yes, allow reading from v1/ during this session\n"
            "│   3. No\n"
        )
        assert extract_menu_options(prompt, get_provider('claude')) == [
            (1, "Yes"),
            (2, "Yes, allow reading from v1/ during this session"),
            (3, "No"),
        ]

    def test_double_cursor_overdraw(self) -> None:
        # Ink TUI overdraw can leave a stale cursor glyph from a previous
        # frame next to the new cursor on the selected row — visually you
        # see "❯ ❯ 1. Yes" because Ink doesn't always clear-to-EOL when
        # re-rendering.  Auto-approve must still find option 1's "Yes".
        prompt = (
            "Do you want to proceed?\n"
            "❯ ❯ 1. Yes\n"
            "    2. Yes, allow always\n"
            "    3. No\n"
        )
        assert extract_menu_options(prompt, get_provider('claude')) == [
            (1, "Yes"),
            (2, "Yes, allow always"),
            (3, "No"),
        ]

    def test_inline_prose_with_numbered_list_does_not_auto_approve(
        self,
    ) -> None:
        # Safety net for the relaxed multi-cluster prefix: an assistant
        # response that happens to embed "<words> 1. <text>" on one line
        # now parses as a menu option (label = everything after "1. ").
        # That's harmless because auto-approve's letters-only "Yes"
        # check refuses to act on a label whose letters don't reduce to
        # exactly "yes".  Pin that contract here so a future regex
        # tweak can't silently make auto-approve fire on prose.
        prompt = "The plan is: 1. read file, 2. modify it, 3. commit\n"
        options = extract_menu_options(prompt, get_provider('claude'))
        # The parser now extracts a (1, ...) tuple — that's fine; what
        # matters is none of the labels reduce to letters == "yes".
        for _num, label in options:
            letters = ''.join(c for c in label if c.isalpha()).lower()
            assert letters != 'yes', (
                f'label {label!r} reduces to {letters!r} — auto-approve '
                f'would incorrectly fire on conversational prose'
            )

    @pytest.mark.skipif(
        not hasattr(signal, 'SIGALRM'),
        reason='SIGALRM not available on Windows',
    )
    def test_no_catastrophic_backtracking_on_periods_in_prose(self) -> None:
        # The multi-cluster prefix relaxation MUST use possessive
        # quantifiers (`++`, `*+`).  A plain `*` would catastrophically
        # backtrack on no-match lines that contain a mid-sentence period
        # — e.g. "Claude has written up a plan. Would you like to
        # proceed?" — because the regex engine explores exponentially
        # many ways to split the prefix into clusters before giving up.
        # This pins the behavior: every line below must resolve in well
        # under a second.  A non-possessive replacement would blow past
        # the 2-second alarm and hang the auto-sender thread in
        # production.
        pathological_lines = [
            'Claude has written up a plan. Would you like to proceed?',
            'I will now do the following. First read. Then write. Then commit.',
            'Step A. Step B. Step C. Step D. Step E. Step F. Step G. Step H.',
            'a.b.c.d.e.f.g.h.i.j.k.l.m.n.o.p.q.r.s.t.u.v.w.x.y.z. Done!',
        ]
        previous = signal.signal(signal.SIGALRM, _raise_timeout)
        try:
            for line in pathological_lines:
                signal.alarm(2)
                start = time.time()
                try:
                    MENU_OPTION_RE.match(line)
                finally:
                    signal.alarm(0)
                elapsed = time.time() - start
                assert elapsed < 0.5, (
                    f'regex took {elapsed:.3f}s on {line!r} — '
                    f'catastrophic backtracking has been reintroduced; '
                    f'possessive quantifiers (`++`/`*+`) are required'
                )
        finally:
            signal.signal(signal.SIGALRM, previous)


def _raise_timeout(_signum: int, _frame: object) -> None:
    raise TimeoutError(
        'regex hung — catastrophic backtracking reintroduced'
    )


class TestSelectedRowBoxFill:
    """Claude's Ink TUI pads the SELECTED option's row with a trailing run
    of box-drawing ``─`` to fill the dialog width.  ``(.+)`` captures it
    into the label and ``.strip()`` (whitespace only) leaves it, so the
    selected option rendered as ``... (keep ID) ──────────`` - text plus
    what looks like a separator line in the monitor's right-click menu.
    The parser must strip the edge box-drawing fill."""

    def test_trailing_box_fill_stripped_from_selected_option(self) -> None:
        prompt = (
            "How should the stub behave?\n"
            "❯ 1. Local literal prefix (keep ID) ────────────────────\n"
            "     Config = WorkflowName, used for logging now.\n"
            "  2. Drop ID from stub\n"
            "  3. Type something.\n"
            "  4. Chat about this\n"
        )
        assert extract_menu_options(prompt) == [
            (1, "Local literal prefix (keep ID)"),
            (2, "Drop ID from stub"),
            (3, "Type something."),
            (4, "Chat about this"),
        ]

    def test_heavy_and_mixed_box_chars_stripped(self) -> None:
        # Different box-drawing glyphs across the U+2500–U+257F block.
        prompt = (
            "❯ 1. Keep it ━━━━━━\n"
            "  2. Drop it ────╴╴\n"
        )
        assert extract_menu_options(prompt) == [
            (1, "Keep it"),
            (2, "Drop it"),
        ]

    def test_ascii_hyphen_in_label_is_preserved(self) -> None:
        # A real ASCII hyphen/dash inside a label must NOT be stripped -
        # only the Unicode box-drawing range is fill.
        prompt = (
            "❯ 1. Use offline-profile prefix\n"
            "  2. Drop it\n"
        )
        assert extract_menu_options(prompt) == [
            (1, "Use offline-profile prefix"),
            (2, "Drop it"),
        ]


class TestExtractMenuQuestion:
    """The question/header text shown above a numbered menu."""

    def test_question_above_options(self) -> None:
        prompt = (
            "⏺ Stub ID\n"
            "Config field becomes WorkflowName, matching siblings. Since the\n"
            "prefix is out of scope for now, how should the stub behave?\n"
            "❯ 1. Local literal prefix (keep ID) ──────────\n"
            "     Config = WorkflowName, used for logging now.\n"
            "  2. Drop ID from stub\n"
            "  3. Type something.\n"
            "  4. Chat about this\n"
        )
        q = extract_menu_question(prompt)
        assert q == (
            "Stub ID Config field becomes WorkflowName, matching siblings. "
            "Since the prefix is out of scope for now, how should the stub "
            "behave?"
        )

    def test_plan_above_options_does_not_leak(self) -> None:
        # A large plan dumped far above, a blank line, then the real
        # question right above the options.  Only the question survives.
        prompt = (
            "Here is the full plan:\n"
            "1. Refactor the handler\n"
            "2. Update the tests\n"
            "3. Wire the registry\n"
            "\n"
            "Claude has written up a plan. Would you like to proceed?\n"
            "❯ 1. Yes, proceed\n"
            "  2. No, keep planning\n"
        )
        assert extract_menu_question(prompt) == (
            "Claude has written up a plan. Would you like to proceed?"
        )
        # And the options are still the final 1..n sequence, not the plan.
        assert extract_menu_options(prompt) == [
            (1, "Yes, proceed"),
            (2, "No, keep planning"),
        ]

    def test_no_text_above_options_returns_empty(self) -> None:
        prompt = (
            "❯ 1. Allow once\n"
            "  2. Deny\n"
        )
        assert extract_menu_question(prompt) == ""

    def test_no_menu_returns_empty(self) -> None:
        assert extract_menu_question("Just some prose, no menu here.") == ""

    def test_box_rule_between_question_and_options_is_skipped(self) -> None:
        # A box-only rule line directly above the options is skipped (it
        # cleans to empty while the block is still empty), so the real
        # question above it is still found.
        prompt = (
            "Pick one:\n"
            "──────────────\n"
            "❯ 1. A\n"
            "  2. B\n"
        )
        assert extract_menu_question(prompt) == "Pick one:"
