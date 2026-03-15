"""
CLI provider abstraction for Leap.

Supports multiple CLI backends (Claude Code, OpenAI Codex) through a
unified provider interface.
"""

from leap.cli_providers.base import CLIProvider
from leap.cli_providers.claude import ClaudeProvider
from leap.cli_providers.codex import CodexProvider
from leap.cli_providers.registry import get_provider, list_installed_providers, list_providers
from leap.cli_providers.states import (
    AutoSendMode,
    CLIState,
    PROMPT_STATES,
    SIGNAL_ALIASES,
    SIGNAL_STATES,
    WAITING_STATES,
)

__all__ = [
    'AutoSendMode',
    'CLIProvider',
    'CLIState',
    'ClaudeProvider',
    'CodexProvider',
    'PROMPT_STATES',
    'SIGNAL_ALIASES',
    'SIGNAL_STATES',
    'WAITING_STATES',
    'get_provider',
    'list_installed_providers',
    'list_providers',
]
