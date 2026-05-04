"""Unit tests for _resolve_cli_flags — stored/env default flag merging."""
import os
import pytest
from unittest.mock import patch
from leap.server.pty_handler import _resolve_cli_flags


def test_no_stored_no_env_returns_explicit() -> None:
    with patch('leap.server.pty_handler.get_cli_flags', return_value=''):
        result = _resolve_cli_flags('claude', ['--verbose'])
    assert result == ['--verbose']


def test_stored_flags_prepended() -> None:
    with patch('leap.server.pty_handler.get_cli_flags', return_value='--dangerously-skip-permissions'):
        result = _resolve_cli_flags('claude', [])
    assert result == ['--dangerously-skip-permissions']


def test_quoted_flag_value_parsed_correctly() -> None:
    """--model "opus[1m]" must not pass literal quotes to the CLI."""
    stored = '--dangerously-skip-permissions --model "opus[1m]"'
    with patch('leap.server.pty_handler.get_cli_flags', return_value=stored):
        result = _resolve_cli_flags('claude', [])
    assert result == ['--dangerously-skip-permissions', '--model', 'opus[1m]']


def test_explicit_flags_come_after_stored() -> None:
    """Explicit flags follow stored flags so they win on duplicates."""
    with patch('leap.server.pty_handler.get_cli_flags', return_value='--model opus[1m]'):
        result = _resolve_cli_flags('claude', ['--model', 'claude-sonnet-4-6'])
    assert result == ['--model', 'opus[1m]', '--model', 'claude-sonnet-4-6']


def test_env_var_overrides_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEAP_CLAUDE_FLAGS', '--model claude-sonnet-4-6')
    with patch('leap.server.pty_handler.get_cli_flags', return_value='--model opus[1m]') as mock_get:
        result = _resolve_cli_flags('claude', [])
    mock_get.assert_not_called()
    assert result == ['--model', 'claude-sonnet-4-6']


def test_empty_env_var_disables_stored_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEAP_CLAUDE_FLAGS', '')
    with patch('leap.server.pty_handler.get_cli_flags', return_value='--model opus[1m]') as mock_get:
        result = _resolve_cli_flags('claude', ['--verbose'])
    mock_get.assert_not_called()
    assert result == ['--verbose']


def test_env_var_name_normalisation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider name 'cursor-agent' → env var LEAP_CURSOR_AGENT_FLAGS."""
    monkeypatch.setenv('LEAP_CURSOR_AGENT_FLAGS', '--some-flag')
    with patch('leap.server.pty_handler.get_cli_flags', return_value='') as mock_get:
        result = _resolve_cli_flags('cursor-agent', [])
    mock_get.assert_not_called()
    assert result == ['--some-flag']


def test_no_env_var_falls_back_to_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LEAP_CLAUDE_FLAGS', raising=False)
    with patch('leap.server.pty_handler.get_cli_flags', return_value='--stored-flag'):
        result = _resolve_cli_flags('claude', [])
    assert result == ['--stored-flag']
