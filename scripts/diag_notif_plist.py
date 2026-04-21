"""Print current notification state from the ncprefs plist.

Run any time to see what bit 25 says right now for a given bundle id.
Usage:
    python3 scripts/diag_notif_plist.py                        # leap default
    python3 scripts/diag_notif_plist.py com.apple.Notes        # any bundle
"""
import plistlib
import sys
from pathlib import Path

bid = sys.argv[1] if len(sys.argv) > 1 else 'com.leap.monitor'

p = Path.home() / 'Library' / 'Preferences' / 'com.apple.ncprefs.plist'
with open(p, 'rb') as f:
    data = plistlib.load(f)

for entry in data.get('apps', []):
    if isinstance(entry, dict) and entry.get('bundle-id') == bid:
        flags = entry.get('flags', 0)
        auth = entry.get('auth')
        bit25 = bool(flags & 0x02000000)
        print(f'{bid}')
        print(f'  flags = {flags}  (0x{flags:08x})')
        print(f'  bit 25 (Allow Notifications master toggle) = {bit25}')
        print(f'  auth = {auth!r}')
        print(f'  → banner would show "missing" : {not bit25}')
        sys.exit(0)

print(f'{bid}: NOT LISTED in ncprefs.plist (never registered)')
sys.exit(1)
