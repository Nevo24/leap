"""
py2app setup script for ClaudeQ Monitor
Creates a standalone macOS application bundle with embedded Python
"""
from setuptools import setup

APP = ['src/claudeq-monitor.py']
DATA_FILES = [('', ['src/activate_terminal.groovy'])]
OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'assets/claudeq-icon.icns',
    'plist': {
        'CFBundleName': 'ClaudeQ Monitor',
        'CFBundleDisplayName': 'ClaudeQ Monitor',
        'CFBundleIdentifier': 'com.claudeq.monitor',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '10.13',
    },
    'packages': ['PyQt5'],
    'includes': ['PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets'],
}

setup(
    name='ClaudeQ Monitor',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
