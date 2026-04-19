"""
CLI provider registry.

Maps provider names to provider instances and handles lookup.
Supports user-defined custom CLIs that wrap a base provider with
custom display names, flags, and environment variables.
"""

import json
import os
from pathlib import Path
from typing import Any, Optional

from leap.cli_providers.base import CLIProvider
from leap.cli_providers.claude import ClaudeProvider
from leap.cli_providers.codex import CodexProvider
from leap.cli_providers.cursor_agent import CursorAgentProvider
from leap.cli_providers.gemini import GeminiProvider
from leap.utils.constants import STORAGE_DIR, atomic_json_write


class CustomCLIProvider(CLIProvider):
    """A user-defined CLI that wraps a base provider with custom identity and env vars.

    Delegates ALL behavior to the base provider.  ``__getattribute__``
    intercepts every attribute lookup and forwards it to ``_base`` unless
    the attribute is explicitly overridden on this class (name,
    display_name, get_spawn_env) or is a private/dunder field needed
    for bootstrap.  This correctly delegates properties, methods, and
    plain attributes — unlike ``__getattr__``, which fires too late
    for properties defined on a parent class (Python's MRO finds the
    parent descriptor first).
    """

    # Attributes resolved on *this* instance (not forwarded to _base).
    _OWN_ATTRS: frozenset[str] = frozenset({
        '_custom_id', '_base', '_custom_display_name', '_env_vars',
        'name', 'display_name', 'get_spawn_env',
    })

    def __init__(
        self,
        custom_id: str,
        base_provider: CLIProvider,
        custom_display_name: str,
        env_vars: Optional[dict[str, str]] = None,
    ) -> None:
        self._custom_id = custom_id
        self._base = base_provider
        self._custom_display_name = custom_display_name
        self._env_vars = env_vars or {}

    def __getattribute__(self, attr: str) -> Any:
        """Delegate attribute access to the base provider.

        Resolves on *this* instance only for bootstrap fields (_custom_id,
        _base, etc.), identity overrides (name, display_name), and
        get_spawn_env.  Everything else — including properties and methods
        that CLIProvider defines with defaults — is forwarded to _base.
        """
        if attr.startswith('__') or attr in CustomCLIProvider._OWN_ATTRS:
            return super().__getattribute__(attr)
        base = super().__getattribute__('_base')
        return getattr(base, attr)

    # -- Identity (overridden) -------------------------------------------

    @property
    def name(self) -> str:
        return self._custom_id

    @property
    def display_name(self) -> str:
        return self._custom_display_name

    # -- Abstract methods (must be explicit for ABC) ---------------------
    # Required by ABC even though __getattribute__ handles delegation —
    # Python's ABCMeta checks the class dict at class-creation time.

    @property
    def command(self) -> str:
        return self._base.command

    @property
    def interrupted_pattern(self) -> bytes:
        return self._base.interrupted_pattern

    @property
    def dialog_patterns(self) -> list[bytes]:
        return self._base.dialog_patterns

    @property
    def hook_config_dir(self) -> Path:
        return self._base.hook_config_dir

    def configure_hooks(self, hook_script_path: str) -> None:
        self._base.configure_hooks(hook_script_path)

    # -- Overridden methods ----------------------------------------------

    def get_spawn_env(
        self, tag: Optional[str], signal_dir: Optional[Path],
    ) -> dict[str, str]:
        env = self._base.get_spawn_env(tag, signal_dir)
        # Base's get_spawn_env sets ``LEAP_CLI_PROVIDER`` using its own
        # ``self.name`` — which is the *base* identifier, not this custom
        # CLI's id.  Overwrite so the hook records under
        # ``.storage/cli_sessions/<custom_id>/`` and the picker shows
        # the custom display name instead of the base's.
        env['LEAP_CLI_PROVIDER'] = self._custom_id
        for k, v in self._env_vars.items():
            env[k] = os.path.expanduser(os.path.expandvars(v))
        return env


# -- Built-in providers ---------------------------------------------------

_BUILTIN_PROVIDERS: dict[str, CLIProvider] = {
    'claude': ClaudeProvider(),
    'codex': CodexProvider(),
    'cursor-agent': CursorAgentProvider(),
    'gemini': GeminiProvider(),
}

_PROVIDERS: dict[str, CLIProvider] = dict(_BUILTIN_PROVIDERS)

DEFAULT_PROVIDER: str = 'claude'

CLI_ORDER_FILE = STORAGE_DIR / "cli_order.json"
CLI_FLAGS_FILE = STORAGE_DIR / "cli_flags.json"
CLI_HIDDEN_FILE = STORAGE_DIR / "cli_hidden.json"
CLI_ALIASES_FILE = STORAGE_DIR / "cli_aliases.json"
CLI_CUSTOM_FILE = STORAGE_DIR / "cli_custom.json"
CLI_ENV_FILE = STORAGE_DIR / "cli_env.json"


def _load_cli_order() -> list[str]:
    """Load user-defined CLI order from storage. Returns empty list if not set."""
    try:
        if CLI_ORDER_FILE.exists():
            data = json.loads(CLI_ORDER_FILE.read_text())
            if isinstance(data, list):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def save_cli_order(order: list[str]) -> None:
    """Save CLI provider order to storage."""
    atomic_json_write(CLI_ORDER_FILE, order)


def load_cli_flags() -> dict[str, str]:
    """Load per-CLI default flags from storage. Returns {provider_name: flags_string}."""
    try:
        if CLI_FLAGS_FILE.exists():
            data = json.loads(CLI_FLAGS_FILE.read_text())
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_cli_flags(flags: dict[str, str]) -> None:
    """Save per-CLI default flags to storage."""
    atomic_json_write(CLI_FLAGS_FILE, flags)


def get_cli_flags(provider_name: str) -> str:
    """Get default flags for a CLI provider. Returns empty string if none set."""
    return load_cli_flags().get(provider_name, "")


def load_cli_hidden() -> list[str]:
    """Load list of hidden CLI provider names from storage."""
    try:
        if CLI_HIDDEN_FILE.exists():
            data = json.loads(CLI_HIDDEN_FILE.read_text())
            if isinstance(data, list):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def save_cli_hidden(hidden: list[str]) -> None:
    """Save list of hidden CLI provider names to storage."""
    atomic_json_write(CLI_HIDDEN_FILE, hidden)


def load_cli_aliases() -> dict[str, str]:
    """Load user-defined CLI display name aliases. Returns {provider_name: alias}."""
    try:
        if CLI_ALIASES_FILE.exists():
            data = json.loads(CLI_ALIASES_FILE.read_text())
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_cli_aliases(aliases: dict[str, str]) -> None:
    """Save user-defined CLI display name aliases."""
    atomic_json_write(CLI_ALIASES_FILE, aliases)


def load_cli_env() -> dict[str, dict[str, str]]:
    """Load per-CLI environment variables. Returns {provider_name: {KEY: VALUE}}."""
    try:
        if CLI_ENV_FILE.exists():
            data = json.loads(CLI_ENV_FILE.read_text())
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def save_cli_env(env: dict[str, dict[str, str]]) -> None:
    """Save per-CLI environment variables."""
    atomic_json_write(CLI_ENV_FILE, env)


def get_cli_env(provider_name: str) -> dict[str, str]:
    """Get environment variables for a CLI provider. Returns empty dict if none set."""
    return load_cli_env().get(provider_name, {})


def load_custom_clis() -> list[dict[str, Any]]:
    """Load custom CLI definitions from storage.

    Each entry: {"id": str, "base": str, "display_name": str, "env": {str: str}}
    """
    try:
        if CLI_CUSTOM_FILE.exists():
            data = json.loads(CLI_CUSTOM_FILE.read_text())
            if isinstance(data, list):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def save_custom_clis(custom_clis: list[dict[str, Any]]) -> None:
    """Save custom CLI definitions to storage."""
    atomic_json_write(CLI_CUSTOM_FILE, custom_clis)


def _register_custom_clis() -> None:
    """Load custom CLIs from storage and register them in _PROVIDERS."""
    for entry in load_custom_clis():
        custom_id = entry.get('id', '')
        base_name = entry.get('base', '')
        display = entry.get('display_name', custom_id)
        env_vars = entry.get('env', {})
        base = _BUILTIN_PROVIDERS.get(base_name)
        if not base or not custom_id:
            continue
        _PROVIDERS[custom_id] = CustomCLIProvider(
            custom_id=custom_id,
            base_provider=base,
            custom_display_name=display,
            env_vars=env_vars,
        )


def reload_custom_clis() -> None:
    """Re-read custom CLIs from disk and update the registry.

    Removes stale custom entries and adds/updates current ones.
    """
    # Remove old custom entries
    for name in list(_PROVIDERS.keys()):
        if name not in _BUILTIN_PROVIDERS:
            del _PROVIDERS[name]
    _register_custom_clis()


# Load custom CLIs on module import
_register_custom_clis()


def get_display_name(provider_name: str) -> str:
    """Get the display name for a CLI provider, using alias if set.

    This is the single source of truth for CLI display names across
    the entire application (monitor, slack, banners, selection menu).
    """
    aliases = load_cli_aliases()
    alias = aliases.get(provider_name)
    if alias:
        return alias
    try:
        return get_provider(provider_name).display_name
    except (ValueError, AttributeError):
        return provider_name.capitalize() if provider_name else 'Unknown'


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
    """Return list of available provider names, respecting user-defined order."""
    saved = _load_cli_order()
    all_names = set(_PROVIDERS.keys())
    ordered = [name for name in saved if name in all_names]
    remaining = sorted(all_names - set(ordered))
    return ordered + remaining


def list_installed_providers() -> list[str]:
    """Return list of installed and visible provider names, respecting user-defined order."""
    hidden = set(load_cli_hidden())
    installed = {
        name for name, provider in _PROVIDERS.items()
        if provider.is_installed() and name not in hidden
    }
    saved = _load_cli_order()
    ordered = [name for name in saved if name in installed]
    remaining = sorted(installed - set(ordered))
    return ordered + remaining
