"""Watch com.leap.monitor plist entry for changes.

Prints a line every time `flags` changes. Run this in one terminal,
then in another: toggle "Allow Notifications" for Leap Monitor in
System Settings, or run `make update`, or launch the .app.

Ctrl-C to quit.
"""
import plistlib
import time
from datetime import datetime
from pathlib import Path

PLIST = Path.home() / 'Library' / 'Preferences' / 'com.apple.ncprefs.plist'
BID = 'com.leap.monitor'


def read_flags() -> int | None:
    try:
        with open(PLIST, 'rb') as f:
            data = plistlib.load(f)
    except Exception as exc:
        return None
    for e in data.get('apps', []):
        if isinstance(e, dict) and e.get('bundle-id') == BID:
            return e.get('flags')
    return None


def format_line(flags: int | None) -> str:
    t = datetime.now().strftime('%H:%M:%S')
    if flags is None:
        return f'[{t}] NOT LISTED'
    bit25 = bool(flags & 0x02000000)
    return f'[{t}] flags=0x{flags:08x}  bit25={bit25}  → allowed={bit25}'


print(f'Watching {BID} in {PLIST}')
print('(Toggle the Notifications switch in System Settings to see it change)')
print()
last = object()
while True:
    flags = read_flags()
    if flags != last:
        print(format_line(flags))
        last = flags
    time.sleep(0.5)
