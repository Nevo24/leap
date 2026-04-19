#!/usr/bin/env python3
"""Unified hook configuration for all CLI providers.

Delegates to the provider's configure_hooks() method, which is the
single source of truth for how hooks are installed for each CLI.

Usage:
    configure_hooks.py <provider-name> <path-to-leap-hook.sh>
    configure_hooks.py --all <path-to-leap-hook.sh>

When --all is used, iterates over every registered provider, skipping
those whose binary is not installed (if requires_binary_for_hooks is True).
"""

import os
import shutil
import sys
from pathlib import Path

# Add src/ to path so leap package can be imported
_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC_DIR = _SCRIPT_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from leap.cli_providers.registry import get_provider, list_providers


def _install_and_configure(provider_name: str, source_hook: str) -> bool:
    """Copy the hook script into the provider's config dir and configure hooks.

    Args:
        provider_name: Registry name of the provider.
        source_hook: Path to the source leap-hook.sh script.

    Returns:
        True if hooks were configured, False if skipped.
    """
    provider = get_provider(provider_name)

    if provider.requires_binary_for_hooks and provider.find_cli() is None:
        return False

    hook_dir = provider.hook_config_dir
    hook_dir.mkdir(parents=True, exist_ok=True)
    dest = hook_dir / "leap-hook.sh"
    shutil.copy2(source_hook, dest)
    os.chmod(str(dest), 0o755)

    # The shell hook delegates to `leap-hook-process.py`; copy it
    # alongside so the hook keeps working from the CLI's isolated
    # config dir without needing to reach back into the Leap repo.
    processor_source = Path(source_hook).with_name("leap-hook-process.py")
    if processor_source.is_file():
        processor_dest = hook_dir / "leap-hook-process.py"
        shutil.copy2(processor_source, processor_dest)
        os.chmod(str(processor_dest), 0o755)

    provider.configure_hooks(str(dest))
    return True


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: configure_hooks.py <provider|--all> <path-to-leap-hook.sh>")
        sys.exit(1)

    target = sys.argv[1]
    hook_path = sys.argv[2]

    if not os.path.isfile(hook_path):
        print(f"Error: Hook script not found: {hook_path}")
        sys.exit(1)

    if target == '--all':
        for name in list_providers():
            if _install_and_configure(name, hook_path):
                print(f"  Configured {get_provider(name).display_name} hooks")
    else:
        if _install_and_configure(target, hook_path):
            print(f"  Configured {get_provider(target).display_name} hooks")
        else:
            print(f"  Skipped {get_provider(target).display_name} (binary not found)")


if __name__ == "__main__":
    main()
