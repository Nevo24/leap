"""Tests for the Copilot status-line integration.

Copilot is the one CLI whose context usage comes from a status line rather than
a transcript: Leap installs ``leap-copilot-statusline.py`` (which writes the
per-tag ``.context`` state file) and registers it in ``~/.copilot/settings.json``
via ``CopilotProvider.configure_hooks``, preserving any existing status line.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import leap.cli_providers.copilot as copilot_mod
from leap.cli_providers.registry import get_provider


def _load_statusline():
    spec = importlib.util.spec_from_file_location(
        'leap_copilot_statusline', 'src/scripts/leap-copilot-statusline.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestExtractState:
    def test_real_nested_payload(self):
        # The real Copilot 1.0.60 schema: token/window fields nested under
        # "context_window"; the % must match Copilot's own
        # current_context_used_percentage (used / displayed_context_limit).
        sl = _load_statusline()
        payload = {
            'model': {'id': 'gpt-5-mini', 'display_name': 'gpt-5-mini · medium'},
            'context_window': {
                'current_context_tokens': 27_395,
                'displayed_context_limit': 192_000,
                'context_window_size': 264_000,
                'current_context_used_percentage': 14,
                'used_percentage': 10,
            },
        }
        st = sl.extract_state(payload)
        assert st == {'used_tokens': 27_395, 'window': 192_000,
                      'model': 'gpt-5-mini · medium'}
        # round(27395/192000*100) == 14 == Copilot's current_context_used_percentage
        assert round(100 * st['used_tokens'] / st['window']) == 14

    def test_nested_window_falls_back_to_raw(self):
        sl = _load_statusline()
        assert sl.extract_state({
            'model': {'id': 'gpt-5'},
            'context_window': {'current_context_tokens': 1,
                               'context_window_size': 264_000},
        }) == {'used_tokens': 1, 'window': 264_000, 'model': 'gpt-5'}

    def test_flattened_schema_still_works(self):
        # Defensive: a future flattened schema (no context_window wrapper).
        sl = _load_statusline()
        assert sl.extract_state({
            'current_context_tokens': 50_000,
            'displayed_context_limit': 168_000,
            'model': 'gpt-5',
        }) == {'used_tokens': 50_000, 'window': 168_000, 'model': 'gpt-5'}

    def test_no_window_returns_none(self):
        sl = _load_statusline()
        # Init-time payload with no token fields (what Copilot sends before a
        # turn has consumed context) -> None -> blank cell, not a crash.
        assert sl.extract_state({'context_window': {}}) is None
        assert sl.extract_state({'current_context_tokens': 5}) is None

    def test_non_dict_returns_none(self):
        sl = _load_statusline()
        assert sl.extract_state('nope') is None
        assert sl.extract_state(None) is None


class TestConfigureStatusLine:
    def _prep(self, tmp_path, monkeypatch):
        """Create a hook dir with the status-line script and point the
        provider's settings file at a tmp settings.json."""
        hook = tmp_path / 'leap-hook.sh'
        hook.write_text('#!/bin/sh\n')
        (tmp_path / 'leap-copilot-statusline.py').write_text('#!/usr/bin/env python3\n')
        settings = tmp_path / 'settings.json'
        monkeypatch.setattr(copilot_mod, 'COPILOT_SETTINGS_FILE', settings)
        return hook, settings

    def test_installs_status_line(self, tmp_path, monkeypatch):
        hook, settings = self._prep(tmp_path, monkeypatch)
        get_provider('copilot').configure_hooks(str(hook))
        data = json.loads(settings.read_text())
        assert data['statusLine']['type'] == 'command'
        assert data['statusLine']['command'].endswith('leap-copilot-statusline.py')

    def test_preserves_existing_settings_and_chains_prior(self, tmp_path, monkeypatch):
        hook, settings = self._prep(tmp_path, monkeypatch)
        settings.write_text(json.dumps({
            'statusLine': {'type': 'command', 'command': '/my/custom/line.sh'},
            'theme': 'dark',  # an unrelated user setting
        }))
        get_provider('copilot').configure_hooks(str(hook))
        data = json.loads(settings.read_text())
        # Our status line is installed; the unrelated setting survives.
        assert data['statusLine']['command'].endswith('leap-copilot-statusline.py')
        assert data['theme'] == 'dark'
        # The user's prior command is saved for chaining.
        chain = (tmp_path / 'leap-statusline-chain').read_text()
        assert chain == '/my/custom/line.sh'

    def test_reconfigure_does_not_self_chain(self, tmp_path, monkeypatch):
        hook, settings = self._prep(tmp_path, monkeypatch)
        p = get_provider('copilot')
        p.configure_hooks(str(hook))
        p.configure_hooks(str(hook))  # second run: must not chain to our own script
        assert not (tmp_path / 'leap-statusline-chain').exists()

    def test_no_script_no_settings_written(self, tmp_path, monkeypatch):
        # If the installer didn't place the status-line script, do nothing.
        hook = tmp_path / 'leap-hook.sh'
        hook.write_text('#!/bin/sh\n')
        settings = tmp_path / 'settings.json'
        monkeypatch.setattr(copilot_mod, 'COPILOT_SETTINGS_FILE', settings)
        get_provider('copilot').configure_hooks(str(hook))
        assert not settings.exists()
