#!/usr/bin/env python3
"""Enable CSI u (Kitty keyboard protocol) in all iTerm2 profiles.

This allows Shift+Enter to send a distinct escape sequence so Leap's
client can distinguish it from plain Enter (used for newline insertion).

Modifies ~/Library/Preferences/com.googlecode.iterm2.plist by setting
"Use libtickit protocol" = True on every profile in "New Bookmarks".

Safe to run multiple times (idempotent).
"""

import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


PLIST = Path.home() / "Library" / "Preferences" / "com.googlecode.iterm2.plist"
KEY = "Use libtickit protocol"


def _is_iterm2_installed() -> bool:
    return (
        Path("/Applications/iTerm.app").is_dir()
        or Path.home().joinpath("Applications/iTerm.app").is_dir()
    )


def _is_iterm2_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-x", "iTerm2"],
        capture_output=True,
    )
    return result.returncode == 0


def configure() -> bool:
    """Enable CSI u in all iTerm2 profiles. Returns True if changes were made."""
    if not _is_iterm2_installed():
        return False

    if not PLIST.exists():
        print("  iTerm2 installed but no plist found, skipping")
        return False

    try:
        with open(PLIST, "rb") as f:
            data = plistlib.load(f)
    except Exception as e:
        print(f"  Could not read iTerm2 plist: {e}")
        return False

    bookmarks = data.get("New Bookmarks", [])
    if not bookmarks:
        print("  No iTerm2 profiles found, skipping")
        return False

    changed = False
    for profile in bookmarks:
        if not profile.get(KEY, False):
            profile[KEY] = True
            changed = True

    if not changed:
        return False

    # Backup and write
    backup = PLIST.with_suffix(".plist.leap-backup")
    if not backup.exists():
        shutil.copy2(PLIST, backup)

    try:
        with open(PLIST, "wb") as f:
            plistlib.dump(data, f, fmt=plistlib.FMT_BINARY)
    except Exception as e:
        print(f"  Could not write iTerm2 plist: {e}")
        return False

    return True


def main() -> None:
    if not _is_iterm2_installed():
        return

    changed = configure()
    if changed:
        print("  \033[0;32m✓ iTerm2: enabled CSI u (Shift+Enter support)\033[0m")
        if _is_iterm2_running():
            print("  \033[1;33m⚠ Restart iTerm2 for the change to take effect\033[0m")
    else:
        print("  ✓ iTerm2 CSI u already enabled")


if __name__ == "__main__":
    main()
