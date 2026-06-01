"""Tests for ``detect_supported_ide_for_move`` — the classifier that
decides whether the monitor's "Open in IDE" path action offers the
"move the running session into the IDE's terminal" choice.
"""

from __future__ import annotations

import pytest

from leap.monitor.navigation import detect_supported_ide_for_move


@pytest.mark.parametrize('app_path,expected', [
    # VS Code (stable + Insiders) → movable as 'VS Code'.
    ('/Applications/Visual Studio Code.app', 'VS Code'),
    ('/Applications/Visual Studio Code - Insiders.app', 'VS Code'),
    # Cursor is a VS Code fork driven by the same plumbing → movable.
    ('/Applications/Cursor.app', 'Cursor'),
    ('/Users/me/Applications/Cursor.app/', 'Cursor'),  # trailing slash
    # JetBrains family → canonical key the downstream helper knows.
    ('/Applications/PyCharm.app', 'PyCharm'),
    ('/Applications/IntelliJ IDEA.app', 'IntelliJ IDEA'),
    # Not drivable → None (fall back to plain "just open the .app").
    ('/Applications/Sublime Text.app', None),
    ('/Applications/Xcode.app', None),
    ('', None),
])
def test_detect_supported_ide_for_move(app_path: str,
                                       expected: object) -> None:
    assert detect_supported_ide_for_move(app_path) == expected


def test_cursor_is_now_movable_not_excluded() -> None:
    # Regression: Cursor used to be deliberately excluded (returned None),
    # which hid the "move session to IDE" option for it.  It must now be
    # offered, exactly like VS Code.
    assert detect_supported_ide_for_move('/Applications/Cursor.app') == 'Cursor'
