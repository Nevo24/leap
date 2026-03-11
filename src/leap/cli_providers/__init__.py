"""
CLI provider abstraction for Leap.

Supports multiple CLI backends (Claude Code, OpenAI Codex) through a
unified provider interface.
"""

from leap.cli_providers.base import CLIProvider
from leap.cli_providers.claude import ClaudeProvider
from leap.cli_providers.codex import CodexProvider
from leap.cli_providers.registry import get_provider, list_providers

__all__ = [
    'CLIProvider',
    'ClaudeProvider',
    'CodexProvider',
    'get_provider',
    'list_providers',
]
