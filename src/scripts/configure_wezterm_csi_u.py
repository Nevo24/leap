#!/usr/bin/env python3
"""Enable CSI u key encoding in WezTerm.

This allows Shift+Enter to send a distinct escape sequence so Leap's
client can distinguish it from plain Enter (used for newline insertion).

Adds `enable_csi_u_key_encoding = true` to the WezTerm Lua config file.
If no config file exists, creates one with the setting.

Safe to run multiple times (idempotent).
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

# WezTerm config file locations (checked in order)
_CONFIG_PATHS = [
    Path.home() / ".wezterm.lua",
    Path.home() / ".config" / "wezterm" / "wezterm.lua",
]

_SETTING = "enable_csi_u_key_encoding"


def _is_wezterm_installed() -> bool:
    """Check if WezTerm is installed."""
    # macOS .app bundle in standard locations
    if (Path("/Applications/WezTerm.app").is_dir()
            or Path.home().joinpath("Applications/WezTerm.app").is_dir()):
        return True
    # CLI binary on PATH
    if shutil.which("wezterm") is not None:
        return True
    # Spotlight fallback — finds the app anywhere on disk
    try:
        result = subprocess.run(
            ["mdfind", 'kMDItemCFBundleIdentifier == "com.github.wez.wezterm"'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except (subprocess.TimeoutExpired, OSError):
        pass
    return False


def _is_wezterm_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-x", "wezterm-gui"],
        capture_output=True,
    )
    return result.returncode == 0


def _find_config() -> Optional[Path]:
    """Find an existing WezTerm config file."""
    for path in _CONFIG_PATHS:
        if path.is_file():
            return path
    return None


def configure() -> bool:
    """Enable CSI u in WezTerm config. Returns True if changes were made."""
    if not _is_wezterm_installed():
        return False

    config_path = _find_config()

    if config_path is not None:
        text = config_path.read_text()

        # Already configured — check for the setting (enabled or disabled)
        if re.search(rf'^\s*(?:config\.)?{_SETTING}\s*=', text, re.MULTILINE):
            # Setting exists — make sure it's true
            updated = re.sub(
                rf'^(\s*(?:config\.)?{_SETTING}\s*=\s*)false',
                r'\g<1>true',
                text,
                flags=re.MULTILINE,
            )
            if updated == text:
                # Already true
                return False
            # Backup and write
            _backup(config_path)
            config_path.write_text(updated)
            return True

        # Setting not present — inject it before `return config`
        _backup(config_path)
        return_match = re.search(r'^(\s*return\s+config\b)', text, re.MULTILINE)
        if return_match:
            indent = re.match(r'^\s*', return_match.group(1)).group()
            insertion = f"{indent}config.{_SETTING} = true\n\n"
            new_text = text[:return_match.start()] + insertion + text[return_match.start():]
            config_path.write_text(new_text)
        else:
            # No `return config` — append at end
            if not text.endswith('\n'):
                text += '\n'
            text += f"\n-- Leap: enable CSI u for Shift+Enter support\n{_SETTING} = true\n"
            config_path.write_text(text)
        return True

    # No config file exists — create one at the primary location
    config_path = _CONFIG_PATHS[0]
    config_path.write_text(
        "local wezterm = require 'wezterm'\n"
        "local config = wezterm.config_builder()\n"
        "\n"
        "-- Leap: enable CSI u for Shift+Enter support\n"
        f"config.{_SETTING} = true\n"
        "\n"
        "return config\n"
    )
    return True


def _backup(path: Path) -> None:
    """Create a one-time backup before first modification."""
    backup = path.with_suffix(path.suffix + ".leap-backup")
    if not backup.exists():
        shutil.copy2(path, backup)


def main() -> None:
    if not _is_wezterm_installed():
        return

    changed = configure()
    if changed:
        print("  \033[0;32m\u2713 WezTerm: enabled CSI u (Shift+Enter support)\033[0m")
        if _is_wezterm_running():
            print("  \033[1;33m\u26a0 Restart WezTerm for the change to take effect\033[0m")
    else:
        print("  \u2713 WezTerm CSI u already enabled")


if __name__ == "__main__":
    main()
