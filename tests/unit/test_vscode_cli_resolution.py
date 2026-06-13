"""Regression tests for VS Code / Cursor CLI resolution in navigation.

The bug: closing (or jumping to) a Leap session running in VS Code popped
*Cursor* open, because both editors ship a ``code`` CLI and Cursor's
``code`` shim shadows VS Code's on PATH - so ``which('code')`` for a
VS Code session resolved to Cursor's binary, and ``code --reuse-window``
launched Cursor.  The fix pins the CLI to the target editor's own ``.app``
bundle and rejects a PATH candidate that resolves into the other editor.
"""

from __future__ import annotations

import os

import pytest

from leap.monitor import navigation as nav


# ---- _cli_is_other_editor (pure logic) ----------------------------------


def test_cursor_shim_is_wrong_for_vscode():
    # Cursor's `code` shim (symlinked into a Cursor.app bundle) must be
    # rejected when the target editor is VS Code.
    p = '/Applications/Cursor.app/Contents/Resources/app/bin/code'
    assert nav._cli_is_other_editor(p, 'VS Code') is True


def test_vscode_cli_is_right_for_vscode():
    p = '/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code'
    assert nav._cli_is_other_editor(p, 'VS Code') is False


def test_vscode_cli_is_wrong_for_cursor():
    p = '/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code'
    assert nav._cli_is_other_editor(p, 'Cursor') is True


def test_cursor_cli_is_right_for_cursor():
    p = '/Applications/Cursor.app/Contents/Resources/app/bin/code'
    assert nav._cli_is_other_editor(p, 'Cursor') is False


# ---- _vscode_cli_in_bundle (bundle-pinned resolution) -------------------


def _make_bundle(root, app_name: str) -> str:
    cli = os.path.join(root, f'{app_name}.app',
                       'Contents', 'Resources', 'app', 'bin', 'code')
    os.makedirs(os.path.dirname(cli))
    with open(cli, 'w') as f:
        f.write('#!/bin/sh\n')
    return cli


def test_bundle_resolution_picks_the_right_editor(tmp_path, monkeypatch):
    vscode_cli = _make_bundle(str(tmp_path), 'Visual Studio Code')
    cursor_cli = _make_bundle(str(tmp_path), 'Cursor')
    monkeypatch.setattr(nav, '_VSCODE_APP_DIRS', (str(tmp_path),))
    assert nav._vscode_cli_in_bundle('VS Code') == vscode_cli
    assert nav._vscode_cli_in_bundle('Cursor') == cursor_cli


def test_bundle_resolution_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(nav, '_VSCODE_APP_DIRS', (str(tmp_path),))
    assert nav._vscode_cli_in_bundle('VS Code') is None


def test_env_path_prefers_bundle_over_cursor_shim(tmp_path, monkeypatch):
    """The core regression: even if PATH `code` is Cursor's shim, a
    VS Code session resolves VS Code's own bundle CLI."""
    vscode_cli = _make_bundle(str(tmp_path), 'Visual Studio Code')
    _make_bundle(str(tmp_path), 'Cursor')
    monkeypatch.setattr(nav, '_VSCODE_APP_DIRS', (str(tmp_path),))
    # A PATH `code` pointing at Cursor must NOT win.
    monkeypatch.setattr(
        nav.shutil, 'which',
        lambda name, path=None:
            '/Applications/Cursor.app/Contents/Resources/app/bin/code')
    _, code_path = nav._vscode_env_and_path('VS Code')
    assert code_path == vscode_cli
    assert 'Cursor.app' not in code_path


def test_env_path_rejects_cursor_shim_fallback(tmp_path, monkeypatch):
    """No VS Code bundle found -> fall back to PATH, but reject Cursor's
    shim rather than launch the wrong editor."""
    monkeypatch.setattr(nav, '_VSCODE_APP_DIRS', (str(tmp_path),))  # empty
    monkeypatch.setattr(
        nav.shutil, 'which',
        lambda name, path=None:
            '/Applications/Cursor.app/Contents/Resources/app/bin/code')
    _, code_path = nav._vscode_env_and_path('VS Code')
    assert code_path is None  # better no-op than opening Cursor


def test_env_path_cursor_uses_cursor_cli(tmp_path, monkeypatch):
    cursor_cli = _make_bundle(str(tmp_path), 'Cursor')
    monkeypatch.setattr(nav, '_VSCODE_APP_DIRS', (str(tmp_path),))
    _, code_path = nav._vscode_env_and_path('Cursor')
    assert code_path == cursor_cli
