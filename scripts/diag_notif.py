"""One-shot interactive diagnostic for the notifications toggle.

Walks you through four state captures and prints a summary at the end.
Just run it and follow the prompts.

    poetry run python scripts/diag_notif.py
"""
import plistlib
import subprocess
from pathlib import Path

PLIST = Path.home() / 'Library' / 'Preferences' / 'com.apple.ncprefs.plist'
BID = 'com.leap.monitor'


def snapshot() -> dict:
    with open(PLIST, 'rb') as f:
        data = plistlib.load(f)
    for e in data.get('apps', []):
        if isinstance(e, dict) and e.get('bundle-id') == BID:
            flags = e.get('flags', 0)
            return {
                'listed': True,
                'flags_hex': f'0x{flags:08x}',
                'bit25_set': bool(flags & 0x02000000),
                'auth': e.get('auth'),
            }
    return {'listed': False}


def show(label: str, snap: dict) -> None:
    if not snap['listed']:
        print(f'  {label:<18s}: NOT LISTED')
        return
    mark = '✓ ON ' if snap['bit25_set'] else '✗ OFF'
    print(f'  {label:<18s}: bit25={mark}  flags={snap["flags_hex"]}  auth={snap["auth"]}')


def pause(msg: str) -> None:
    input(f'\n>>> {msg} then press Enter… ')


print('=' * 60)
print(' Leap Monitor notification-toggle diagnostic')
print('=' * 60)

print('\nStep 0: current state')
s0 = snapshot()
show('now', s0)

pause(
    'Open System Settings → Notifications → Leap Monitor.\n'
    '    Note what the row shows ("Off" or a list).\n'
    '    Then flip "Allow Notifications" **OFF** if it is on,\n'
    '    or **ON** if it is off.  (i.e. toggle it.)'
)
s1 = snapshot()
show('after 1st toggle', s1)

pause('Now toggle it the OTHER way in Settings.')
s2 = snapshot()
show('after 2nd toggle', s2)

pause(
    'Leave Settings alone.  In a SEPARATE terminal run:\n\n'
    '        make update\n\n'
    '    (answer "n" to the Accessibility prompt).\n'
    '    Wait for it to finish, then come back here.'
)
s3 = snapshot()
show('after make update', s3)

print('\n' + '=' * 60)
print(' Summary')
print('=' * 60)
show('start', s0)
show('1st toggle', s1)
show('2nd toggle', s2)
show('make update', s3)

print()
if s1['listed'] and s2['listed'] and s1['bit25_set'] != s2['bit25_set']:
    print('  ✓ Bit 25 DOES track the Settings toggle (hypothesis holds).')
else:
    print('  ✗ Bit 25 does NOT track the Settings toggle — need a different bit.')

if s2['listed'] and s3['listed'] and s2['bit25_set'] != s3['bit25_set']:
    print('  ⚠ `make update` CHANGED bit 25 as a side effect — that is the install-flow bug.')
else:
    print('  ✓ `make update` did not change bit 25.')
