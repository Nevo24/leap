#!/usr/bin/env python3
"""
Helper script to configure JetBrains IDE XML settings.
"""
import os
import re
import sys
from typing import NoReturn


def _bail(file_path: str, e: Exception) -> NoReturn:
    """Report a clean one-line warning and exit non-zero (no traceback).

    JetBrains options files/dirs can be non-accessible (e.g. owned by
    another user, locked while the IDE is running, or restrictive perms
    left by JetBrains Toolbox) or non-UTF-8. A raw traceback looks like
    an install failure even though it's non-fatal, so we degrade to a
    one-line warning and a non-zero exit the caller can ignore.

    ``strerror`` only exists on OSError; UnicodeError carries its message
    in ``str(e)``, so we fall back to that rather than assuming the attr.
    """
    detail = getattr(e, 'strerror', None) or e
    print(f"  ⚠ Skipped {file_path}: {detail}", file=sys.stderr)
    sys.exit(1)


def _read(file_path: str) -> str:
    """Read a file, degrading to a clean warning instead of a traceback.

    Encoding is pinned to UTF-8 (JetBrains writes its options XML as
    UTF-8) so a legacy/ascii process locale can't turn a perfectly valid
    file into a UnicodeDecodeError. We still catch UnicodeError for the
    pathological non-UTF-8 file: skipping it cleanly beats a traceback,
    and neither it nor OSError is an instance of the other.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except (OSError, UnicodeError) as e:
        _bail(file_path, e)


def _write(file_path: str, content: str) -> None:
    """Write a file, degrading to a clean warning instead of a traceback."""
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except (OSError, UnicodeError) as e:
        _bail(file_path, e)


def update_terminal_xml(file_path: str) -> None:
    """Update terminal.xml to set engine to CLASSIC."""
    # Create new file if it doesn't exist
    if not os.path.exists(file_path):
        content = '''<application>
  <component name="TerminalOptionsProvider">
    <option name="terminalEngine" value="CLASSIC" />
  </component>
</application>
'''
        _write(file_path, content)
        return

    # Update existing file
    content = _read(file_path)

    if '<component name="TerminalOptionsProvider">' not in content:
        # Add the component
        content = content.replace(
            '</application>',
            '  <component name="TerminalOptionsProvider">\n'
            '    <option name="terminalEngine" value="CLASSIC" />\n'
            '  </component>\n'
            '</application>'
        )
    else:
        if '<option name="terminalEngine"' not in content:
            # Add the option inside existing component
            content = re.sub(
                r'(<component name="TerminalOptionsProvider">)',
                r'\1\n    <option name="terminalEngine" value="CLASSIC" />',
                content
            )
        else:
            # Update existing option
            content = re.sub(
                r'<option name="terminalEngine" value="[^"]*" />',
                '<option name="terminalEngine" value="CLASSIC" />',
                content
            )

    _write(file_path, content)


def update_advanced_settings_xml(file_path: str) -> None:
    """Update advancedSettings.xml to enable show application title."""
    # Create new file if it doesn't exist
    if not os.path.exists(file_path):
        content = '''<application>
  <component name="AdvancedSettings">
    <option name="settings">
      <map>
        <entry key="terminal.show.application.title" value="true" />
      </map>
    </option>
  </component>
</application>
'''
        _write(file_path, content)
        return

    # Update existing file
    content = _read(file_path)

    if 'terminal.show.application.title' not in content:
        # Add the entry
        content = re.sub(
            r'(</map>)',
            r'        <entry key="terminal.show.application.title" value="true" />\n      \1',
            content
        )
    else:
        # Update existing entry
        content = re.sub(
            r'<entry key="terminal.show.application.title" value="[^"]*" />',
            '<entry key="terminal.show.application.title" value="true" />',
            content
        )

    _write(file_path, content)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Usage: configure_jetbrains_xml.py <terminal|advanced> <file_path>")
        sys.exit(1)

    xml_type = sys.argv[1]
    file_path = sys.argv[2]

    if xml_type == 'terminal':
        update_terminal_xml(file_path)
    elif xml_type == 'advanced':
        update_advanced_settings_xml(file_path)
    else:
        print(f"Unknown XML type: {xml_type}")
        sys.exit(1)
