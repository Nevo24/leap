"""
CLI provider registry.

Maps provider names to provider instances and handles lookup.
"""

from typing import Optional

from leap.cli_providers.base import CLIProvider
from leap.cli_providers.claude import ClaudeProvider
from leap.cli_providers.codex import CodexProvider
from leap.cli_providers.cursor_agent import CursorAgentProvider
from leap.cli_providers.gemini import GeminiProvider

_PROVIDERS: dict[str, CLIProvider] = {
    'claude': ClaudeProvider(),
    'codex': CodexProvider(),
    'cursor-agent': CursorAgentProvider(),
    'gemini': GeminiProvider(),
}

DEFAULT_PROVIDER: str = 'claude'


def get_provider(name: Optional[str] = None) -> CLIProvider:
    """Get a CLI provider by name.

    Args:
        name: Provider name ('claude', 'codex', 'cursor-agent', 'gemini'). Defaults to 'claude'.

    Returns:
        The requested CLIProvider instance.

    Raises:
        ValueError: If the provider name is unknown.
    """
    name = name or DEFAULT_PROVIDER
    provider = _PROVIDERS.get(name)
    if provider is None:
        available = ', '.join(sorted(_PROVIDERS.keys()))
        raise ValueError(f"Unknown CLI provider '{name}'. Available: {available}")
    return provider


def list_providers() -> list[str]:
    """Return sorted list of available provider names."""
    return sorted(_PROVIDERS.keys())


def list_installed_providers() -> list[str]:
    """Return sorted list of provider names whose CLI binary is on PATH."""
    return sorted(name for name, provider in _PROVIDERS.items() if provider.is_installed())
