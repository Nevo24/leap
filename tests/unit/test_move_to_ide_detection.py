"""Tests for ``detect_supported_ide_for_move`` — the classifier that
decides whether the monitor's "Open in IDE" path action offers the
"move the running session into the IDE's terminal" choice.
"""

from __future__ import annotations

import pytest

from leap.monitor.navigation import (
    detect_supported_ide_for_move, is_jetbrains_app,
)


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


@pytest.mark.parametrize('app_path,expected', [
    # JetBrains family the "move" helper drives → True.
    ('/Applications/PyCharm.app', True),
    ('/Applications/IntelliJ IDEA.app', True),
    ('/Applications/GoLand.app', True),
    ('/Applications/WebStorm.app', True),
    ('/Applications/PhpStorm.app', True),
    ('/Applications/Android Studio.app', True),
    # JetBrains family that detect_supported_ide_for_move returns None for
    # (no driven terminal) — the .idea/.name alias still applies, so True.
    ('/Applications/RubyMine.app', True),
    ('/Applications/CLion.app', True),
    ('/Applications/DataGrip.app', True),
    # Toolbox / trailing slash variants resolve by basename.
    ('/Users/me/Applications/JetBrains Toolbox/PyCharm.app/', True),
    # Non-JetBrains apps → False (no .idea concept).
    ('/Applications/Visual Studio Code.app', False),
    ('/Applications/Cursor.app', False),
    ('/Applications/Sublime Text.app', False),
    ('/Applications/Xcode.app', False),
    ('', False),
    (None, False),
])
def test_is_jetbrains_app(app_path: object, expected: bool) -> None:
    assert is_jetbrains_app(app_path) is expected
