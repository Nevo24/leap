#!/usr/bin/env python3
"""
Helper script to configure JetBrains IDE XML settings.
"""
import os
import re
import sys


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
        with open(file_path, 'w') as f:
            f.write(content)
        return

    # Update existing file
    with open(file_path, 'r') as f:
        content = f.read()

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

    with open(file_path, 'w') as f:
        f.write(content)


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
        with open(file_path, 'w') as f:
            f.write(content)
        return

    # Update existing file
    with open(file_path, 'r') as f:
        content = f.read()

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

    with open(file_path, 'w') as f:
        f.write(content)


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
