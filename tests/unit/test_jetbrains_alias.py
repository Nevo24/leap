"""Tests for ``ActionsMenuMixin._maybe_set_jetbrains_alias`` - the prompt
that writes ``<project>/.idea/.name`` so a JetBrains IDE labels the project
tab when "Open in IDE" launches it.

Exercises the real file mechanism against a temp directory; only the GUI
primitive (``QInputDialog``) and the JetBrains gates are stubbed, so no
window is ever shown.
"""

from __future__ import annotations

import os
import subprocess
import types
from typing import Optional, Tuple

import pytest

import leap.monitor._mixins.actions_menu_mixin as M

_ALIAS = M.ActionsMenuMixin._maybe_set_jetbrains_alias


class _FakeDialog:
    """Stub QInputDialog; records what the code set on it."""

    def __init__(self, reply: str, accepted: bool) -> None:
        self._reply = reply
        self._accepted = accepted
        self.shown = False
        self.label: Optional[str] = None
        self.default: Optional[str] = None
        self.cancel_text: Optional[str] = None

    def setWindowTitle(self, *_: object) -> None:
        pass

    def setLabelText(self, s: str) -> None:
        self.label = s

    def setTextValue(self, s: str) -> None:
        self.default = s

    def setCancelButtonText(self, s: str) -> None:
        self.cancel_text = s

    def exec_(self) -> int:
        self.shown = True
        return M.QDialog.Accepted if self._accepted else 0

    def textValue(self) -> str:
        return self._reply


def _run(
    tmp_path, *, existing: Optional[str], reply: str, accepted: bool,
    is_jb: bool = True, already_open: bool = False, tag: str = 'mytag',
    monkeypatch=None,
) -> Tuple[_FakeDialog, Optional[str]]:
    """Invoke the alias method against *tmp_path*; return (dialog, .name)."""
    proj = str(tmp_path)
    name_file = os.path.join(proj, '.idea', '.name')
    if existing is not None:
        os.makedirs(os.path.join(proj, '.idea'), exist_ok=True)
        with open(name_file, 'w') as f:
            f.write(existing)

    dlg = _FakeDialog(reply, accepted)
    monkeypatch.setattr(M, 'QInputDialog', lambda parent: dlg)
    monkeypatch.setattr(M, 'is_jetbrains_app', lambda p: is_jb)
    monkeypatch.setattr(
        M, 'is_jetbrains_project_open', lambda a, p: already_open)

    fake_self = types.SimpleNamespace(_show_status=lambda *a, **k: None)
    _ALIAS(fake_self, tag, '/Applications/PyCharm.app', proj)

    content = None
    if os.path.exists(name_file):
        with open(name_file) as f:
            content = f.read()
    return dlg, content


def test_suggested_default_uses_git_project_name(tmp_path, monkeypatch) -> None:
    subprocess.run(['git', 'init', '-q'], cwd=str(tmp_path))
    subprocess.run(
        ['git', 'remote', 'add', 'origin',
         'git@github.com:foo/titanium.git'], cwd=str(tmp_path))
    dlg, _ = _run(tmp_path, existing=None, reply='', accepted=False,
                  tag='nevo', monkeypatch=monkeypatch)
    assert dlg.default == 'titanium (nevo)'
    # Cancel button is left as the plain default (never relabelled).
    assert dlg.cancel_text is None


def test_suggested_default_falls_back_to_folder_name(
        tmp_path, monkeypatch) -> None:
    # No git remote -> proj name falls back to the directory basename.
    dlg, _ = _run(tmp_path, existing=None, reply='', accepted=False,
                  tag='t', monkeypatch=monkeypatch)
    assert dlg.default == f'{tmp_path.name} (t)'


def test_value_plus_ok_writes_alias(tmp_path, monkeypatch) -> None:
    _, content = _run(tmp_path, existing=None, reply='Backend',
                      accepted=True, monkeypatch=monkeypatch)
    assert content == 'Backend'


def test_blank_ok_removes_existing_alias(tmp_path, monkeypatch) -> None:
    _, content = _run(tmp_path, existing='Old', reply='   ',
                      accepted=True, monkeypatch=monkeypatch)
    assert content is None


def test_blank_ok_with_no_alias_is_noop(tmp_path, monkeypatch) -> None:
    _, content = _run(tmp_path, existing=None, reply='', accepted=True,
                      monkeypatch=monkeypatch)
    assert content is None


def test_cancel_keeps_existing_alias(tmp_path, monkeypatch) -> None:
    _, content = _run(tmp_path, existing='Old', reply='ignored',
                      accepted=False, monkeypatch=monkeypatch)
    assert content == 'Old'


def test_value_plus_ok_overwrites_existing(tmp_path, monkeypatch) -> None:
    _, content = _run(tmp_path, existing='Old', reply='New',
                      accepted=True, monkeypatch=monkeypatch)
    assert content == 'New'


def test_non_jetbrains_app_no_prompt(tmp_path, monkeypatch) -> None:
    dlg, content = _run(tmp_path, existing=None, reply='x', accepted=True,
                        is_jb=False, monkeypatch=monkeypatch)
    assert not dlg.shown
    assert content is None


def test_already_open_skips_prompt(tmp_path, monkeypatch) -> None:
    dlg, content = _run(tmp_path, existing='Old', reply='x', accepted=True,
                        already_open=True, monkeypatch=monkeypatch)
    assert not dlg.shown
    assert content == 'Old'


def test_missing_project_dir_no_prompt(tmp_path, monkeypatch) -> None:
    dlg = _FakeDialog('x', True)
    monkeypatch.setattr(M, 'QInputDialog', lambda parent: dlg)
    monkeypatch.setattr(M, 'is_jetbrains_app', lambda p: True)
    monkeypatch.setattr(M, 'is_jetbrains_project_open', lambda a, p: False)
    fake_self = types.SimpleNamespace(_show_status=lambda *a, **k: None)
    _ALIAS(fake_self, 'tag', '/Applications/PyCharm.app',
           str(tmp_path / 'does-not-exist'))
    assert not dlg.shown


def test_label_mentions_previous_alias_and_default(
        tmp_path, monkeypatch) -> None:
    dlg, _ = _run(tmp_path, existing='Old', reply='ignored',
                  accepted=False, monkeypatch=monkeypatch)
    assert "'Old'" in dlg.label
    assert tmp_path.name in dlg.label  # the "back to '<folder>'" part
