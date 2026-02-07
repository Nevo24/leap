"""
py2app setup script for ClaudeQ Monitor
Creates a standalone macOS application bundle with embedded Python
"""
from setuptools import setup, find_packages

# Launcher script for py2app
APP = ['src/scripts/claudeq_monitor_launcher.py']
DATA_FILES = [
    ('', ['src/claudeq/monitor/resources/activate_terminal.groovy',
          'assets/claudeq-icon.png']),
    ('.storage', ['.storage/project-path', '.storage/venv-path'])
]
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
    'packages': ['PyQt5', 'claudeq'],
    'includes': ['PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets'],
}

setup(
    name='ClaudeQ Monitor',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
    package_dir={'': 'src'},
    packages=find_packages(where='src'),
)
