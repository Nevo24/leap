"""
CLI provider abstraction for ClaudeQ.

Supports multiple CLI backends (Claude Code, OpenAI Codex) through a
unified provider interface.
"""

from claudeq.cli_providers.base import CLIProvider
from claudeq.cli_providers.claude import ClaudeProvider
from claudeq.cli_providers.codex import CodexProvider
from claudeq.cli_providers.registry import get_provider, list_providers

__all__ = [
    'CLIProvider',
    'ClaudeProvider',
    'CodexProvider',
    'get_provider',
    'list_providers',
]
