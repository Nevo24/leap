"""Tests for cmux terminal support: runtime detection + navigation routing.

cmux is a Ghostty-based macOS terminal.  Its own shells report
``TERM_PROGRAM='ghostty'`` but the shell integration also exports
``CMUX_*`` identifiers, so ``detect_ide`` must prefer ``'cmux'`` over the
generic ``'Ghostty'`` branch — while NOT misfiring for a child terminal
(VS Code, iTerm) launched from cmux that merely inherits leaked vars.

Navigation / open / close are driven through cmux's AppleScript
dictionary (``cmux.sdef``); those helpers need a live app plus an
Automation grant, so the unit tests here only assert that the dispatchers
route to the cmux helpers (and that the generic fallback disables the
surface-probe) — the AppleScript bodies themselves are not exercised.
"""

import pytest

import leap.monitor.navigation as nav
from leap.utils.ide_detection import detect_ide

# Every env var detect_ide() consults — cleared before each test so the
# terminal the suite actually runs in can't leak into assertions.
_ENV_KEYS = (
    'TERM_PROGRAM', 'TERMINAL_EMULATOR', '__CFBundleIdentifier',
    'CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID', 'CMUX_PANEL_ID',
    'CMUX_TAB_ID', 'CMUX_SOCKET_PATH', 'CMUX_BUNDLE_ID',
    'CMUX_BUNDLED_CLI_PATH', 'CMUX_SHELL_INTEGRATION_DIR',
)


@pytest.fixture
def clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


class TestDetectCmux:
    def test_cmux_wins_over_ghostty(self, clean_env):
        # A cmux shell looks like Ghostty (TERM_PROGRAM) but carries CMUX_*.
        clean_env.setenv('TERM_PROGRAM', 'ghostty')
        clean_env.setenv('CMUX_PANEL_ID', 'abc')
        assert detect_ide() == 'cmux'

    @pytest.mark.parametrize('var', [
        'CMUX_PANEL_ID',              # per-surface id actually seen in the wild
        'CMUX_BUNDLE_ID',            # set by the shell integration
        'CMUX_BUNDLED_CLI_PATH',
        'CMUX_SHELL_INTEGRATION_DIR',
        'CMUX_SURFACE_ID',           # documented; not always present
        'CMUX_WORKSPACE_ID',
        'CMUX_SOCKET_PATH',
    ])
    def test_any_cmux_marker_detects_cmux(self, clean_env, var):
        # Detection keys off the presence of ANY CMUX_* var, not a fixed
        # name — which vars cmux sets varies by surface type and version.
        clean_env.setenv('TERM_PROGRAM', 'ghostty')
        clean_env.setenv(var, 'x')
        assert detect_ide() == 'cmux'

    def test_plain_ghostty_unaffected(self, clean_env):
        clean_env.setenv('TERM_PROGRAM', 'ghostty')
        assert detect_ide() == 'Ghostty'

    def test_term_program_cmux_detected(self, clean_env):
        # Future-proofing: a cmux build that reports its own TERM_PROGRAM.
        clean_env.setenv('TERM_PROGRAM', 'cmux')
        assert detect_ide() == 'cmux'

    def test_leaked_cmux_vars_do_not_override_vscode(self, clean_env):
        # VS Code launched *from* a cmux terminal inherits CMUX_* vars but
        # reports TERM_PROGRAM=vscode — must stay 'VS Code', not 'cmux'.
        clean_env.setenv('TERM_PROGRAM', 'vscode')
        clean_env.setenv('CMUX_PANEL_ID', 'leaked')
        assert detect_ide() == 'VS Code'

    def test_leaked_cmux_vars_do_not_override_iterm(self, clean_env):
        clean_env.setenv('TERM_PROGRAM', 'iTerm.app')
        clean_env.setenv('CMUX_BUNDLE_ID', 'com.cmuxterm.app')
        assert detect_ide() == 'iTerm2'

    def test_other_terminal_unaffected(self, clean_env):
        clean_env.setenv('TERM_PROGRAM', 'iTerm.app')
        assert detect_ide() == 'iTerm2'


class TestNavigationRouting:
    """Dispatchers must route to the cmux helpers, and the generic
    fallback must disable the disruptive surface-probe."""

    def test_find_routes_to_navigate_cmux(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            nav, '_navigate_cmux',
            lambda p, probe=True: calls.append(('cmux', p, probe)) or True)
        # The preferred branch must win — the generic fallback helpers
        # (Terminal.app / iTerm2) must not be consulted first.
        monkeypatch.setattr(nav, '_navigate_iterm2',
                            lambda p: calls.append(('iterm2', p)) or True)
        monkeypatch.setattr(nav, '_navigate_terminal_app',
                            lambda p: calls.append(('term', p)) or True)
        assert nav.find_terminal_with_title(
            'lps mytag', preferred_ide='cmux') is True
        # Preferred-cmux path: probe enabled, generic helpers untouched.
        assert calls == [('cmux', 'lps mytag', True)]

    def test_find_falls_back_to_cmux_without_probe(self, monkeypatch):
        # Unknown recorded terminal: cmux is in the fallback chain, but
        # the probe must be OFF so a failed non-cmux jump doesn't focus-
        # cycle every cmux surface.
        calls = []
        for name in ('_navigate_terminal_app', '_navigate_iterm2',
                     '_navigate_warp', '_navigate_wezterm'):
            monkeypatch.setattr(nav, name, lambda p: False)
        monkeypatch.setattr(
            nav, '_navigate_cmux',
            lambda p, probe=True: calls.append((p, probe)) or True)
        assert nav.find_terminal_with_title(
            'lps mytag', preferred_ide='Kitty') is True
        assert calls == [('lps mytag', False)]

    def test_open_routes_to_open_cmux(self, monkeypatch):
        calls = []
        monkeypatch.setattr(nav, '_open_cmux_terminal',
                            lambda c: calls.append(c) or True)
        outcome = {}
        assert nav.open_terminal_with_command(
            'leap mytag', preferred_ide='cmux', outcome=outcome) is True
        assert calls == ['leap mytag']
        assert outcome.get('used') == 'cmux'

    def test_open_uses_cmux_as_default_terminal(self, monkeypatch):
        # cmux selected as the default terminal, recorded app unsupported.
        calls = []
        monkeypatch.setattr(nav, '_open_cmux_terminal',
                            lambda c: calls.append(c) or True)
        outcome = {}
        assert nav.open_terminal_with_command(
            'leap mytag', preferred_ide='Kitty',
            fallback_terminal='cmux', outcome=outcome) is True
        assert calls == ['leap mytag']
        assert outcome.get('used') == 'cmux'

    def test_close_routes_to_close_cmux(self, monkeypatch):
        calls = []
        monkeypatch.setattr(nav, '_close_cmux',
                            lambda p: calls.append(p) or True)
        assert nav.close_terminal_with_title(
            'lps mytag', preferred_ide='cmux') is True
        assert calls == ['lps mytag']


class TestCmuxAppleScriptGeneration:
    """Smoke test: the cmux helpers emit structurally valid AppleScript.

    ``subprocess.run`` is faked so NO app is contacted — osascript never
    actually runs, nothing is launched, and no Automation prompt fires.
    We capture the generated ``-e`` script and assert structural
    invariants (balanced ``tell``/``repeat``/``try``/``if`` blocks, the
    target ``tell``, the *escaped* pattern, and probe gating).  This
    catches AppleScript template + escaping regressions that the
    monkeypatched routing tests can't see.
    """

    @staticmethod
    def _capture(monkeypatch):
        scripts: list[str] = []

        class _Result:
            returncode = 1   # non-zero -> helper returns False; we only want the script
            stdout = ''
            stderr = ''

        def fake_run(cmd, *args, **kwargs):
            if cmd and cmd[0] == 'osascript' and '-e' in cmd:
                scripts.append(cmd[cmd.index('-e') + 1])
            return _Result()

        monkeypatch.setattr(nav.subprocess, 'run', fake_run)
        # navigate/close are guarded on cmux running; pretend it is.
        monkeypatch.setattr(nav, '_get_app_pid', lambda _bid: 4242)
        # open tries the CLI first; force the AppleScript fallback path.
        monkeypatch.setattr(nav, '_find_cmux_cli', lambda: None)
        return scripts

    @staticmethod
    def _assert_balanced(script: str) -> None:
        assert 'tell application "cmux"' in script
        lines = [ln.strip() for ln in script.splitlines()]
        block_pairs = (
            (lambda ln: ln.startswith('tell application'),
             lambda ln: ln == 'end tell'),
            (lambda ln: ln.startswith('repeat '),
             lambda ln: ln == 'end repeat'),
            (lambda ln: ln == 'try',
             lambda ln: ln == 'end try'),
            # block-form `if ... then` (one-line `if ... then <stmt>` does
            # not end with " then", so it correctly isn't counted).
            (lambda ln: ln.endswith(' then'),
             lambda ln: ln == 'end if'),
        )
        for is_open, is_close in block_pairs:
            opens = sum(1 for ln in lines if is_open(ln))
            closes = sum(1 for ln in lines if is_close(ln))
            assert opens == closes, f'unbalanced block ({opens} vs {closes}):\n{script}'

    def test_navigate_script_well_formed_with_probe(self, monkeypatch):
        scripts = self._capture(monkeypatch)
        assert nav._navigate_cmux('lps mytag', probe=True) is False
        assert len(scripts) == 1
        script = scripts[0]
        self._assert_balanced(script)
        assert 'lps mytag' in script
        assert script.rstrip().endswith('return false')
        assert 'set surfs to terminals of tb' in script   # probe present

    def test_navigate_script_no_probe_omits_surface_loop(self, monkeypatch):
        scripts = self._capture(monkeypatch)
        assert nav._navigate_cmux('lps mytag', probe=False) is False
        script = scripts[0]
        self._assert_balanced(script)
        assert 'set surfs to terminals of tb' not in script   # probe omitted

    def test_close_script_well_formed(self, monkeypatch):
        scripts = self._capture(monkeypatch)
        assert nav._close_cmux('lps mytag') is False
        self._assert_balanced(scripts[0])
        assert 'close (focused terminal of tb)' in scripts[0]

    def test_open_fallback_script_well_formed(self, monkeypatch):
        scripts = self._capture(monkeypatch)
        # _find_cmux_cli -> None, so the AppleScript fallback is built.
        assert nav._open_cmux_terminal('leap mytag') is False
        assert scripts, 'open should emit an AppleScript fallback'
        script = scripts[-1]
        self._assert_balanced(script)
        assert 'new tab' in script and 'input text' in script
        assert 'leap mytag' in script

    def test_pattern_quotes_are_escaped(self, monkeypatch):
        # Injection guard: a double-quote in the title must be escaped so
        # it can't break out of the AppleScript string literal.
        scripts = self._capture(monkeypatch)
        nav._navigate_cmux('lps we"ird', probe=True)
        assert 'we\\"ird' in scripts[0]
