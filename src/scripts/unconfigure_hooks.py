#!/usr/bin/env python3
"""Symmetric counterpart to configure_hooks.py.

Removes Leap's hook configuration from every registered CLI provider by
calling provider.deconfigure_hooks().  Best-effort: a failure for one CLI
never aborts removal for the others.

Usage:
    unconfigure_hooks.py <provider-name>
    unconfigure_hooks.py --all
"""

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC_DIR = _SCRIPT_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from leap.cli_providers.registry import get_provider, list_providers


def _deconfigure(provider_name: str) -> bool:
    """Call deconfigure_hooks() for one provider.

    Returns True if the call completed (even if it was a no-op), False if
    an unexpected exception prevented even attempting it.
    """
    try:
        provider = get_provider(provider_name)
        provider.deconfigure_hooks()
        return True
    except Exception:
        return False


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: unconfigure_hooks.py <provider|--all>")
        sys.exit(1)

    target = sys.argv[1]

    if target == "--all":
        for name in list_providers():
            ok = _deconfigure(name)
            try:
                label = get_provider(name).display_name
            except Exception:
                label = name
            if ok:
                print(f"  Removed {label} hooks")
            else:
                print(f"  Could not remove {label} hooks (skipped)")
    else:
        ok = _deconfigure(target)
        try:
            label = get_provider(target).display_name
        except Exception:
            label = target
        if ok:
            print(f"  Removed {label} hooks")
        else:
            print(f"  Could not remove {label} hooks")


if __name__ == "__main__":
    main()
