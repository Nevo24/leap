"""First-run default-terminal selection (``leap.utils.terminal``).

On first monitor launch (no ``default_terminal`` pref yet) Leap should pick
iTerm2 when it's installed and otherwise fall back to the always-present Apple
Terminal.  These tests exercise the real path-detection logic and the
selection helper headlessly - no Qt, no monitor window.
"""
from pathlib import Path

from leap.utils import terminal


def test_iterm2_installed_true_when_bundle_dir_exists(tmp_path, monkeypatch):
    iterm = tmp_path / 'iTerm.app'
    iterm.mkdir()
    monkeypatch.setattr(terminal, 'iterm2_app_paths', lambda: [tmp_path / 'missing.app', iterm])
    assert terminal.iterm2_installed() is True


def test_iterm2_installed_false_when_no_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(
        terminal, 'iterm2_app_paths',
        lambda: [tmp_path / 'a.app', tmp_path / 'b.app'],
    )
    assert terminal.iterm2_installed() is False


def test_iterm2_installed_ignores_plain_file(tmp_path, monkeypatch):
    # A regular file at the path must not count as an installed bundle.
    bogus = tmp_path / 'iTerm.app'
    bogus.write_text('not a bundle')
    monkeypatch.setattr(terminal, 'iterm2_app_paths', lambda: [bogus])
    assert terminal.iterm2_installed() is False


def test_iterm2_app_paths_are_standard_locations():
    paths = terminal.iterm2_app_paths()
    assert Path('/Applications/iTerm.app') in paths
    assert Path.home() / 'Applications' / 'iTerm.app' in paths


def test_default_terminal_prefers_iterm2_when_present(monkeypatch):
    monkeypatch.setattr(terminal, 'iterm2_installed', lambda: True)
    assert terminal.default_terminal_for_first_run() == 'iTerm2'


def test_default_terminal_falls_back_to_apple_terminal(monkeypatch):
    monkeypatch.setattr(terminal, 'iterm2_installed', lambda: False)
    assert terminal.default_terminal_for_first_run() == 'Terminal.app'
