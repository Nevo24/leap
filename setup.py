"""
py2app setup script for Leap Monitor
Creates a standalone macOS application bundle with embedded Python
"""
from setuptools import setup, find_packages

# Launcher script for py2app
APP = ['src/scripts/leap_monitor_launcher.py']
DATA_FILES = [
    ('', ['src/leap/monitor/resources/activate_terminal.groovy',
          'assets/leap-icon.png', 'assets/leap-text.png']),
    ('.storage', ['.storage/project-path', '.storage/venv-path'])
]
OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'assets/leap-icon.icns',
    'plist': {
        'CFBundleName': 'Leap Monitor',
        'CFBundleDisplayName': 'Leap Monitor',
        'CFBundleIdentifier': 'com.leap.monitor',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '10.13',
    },
    'packages': ['PyQt5', 'leap', 'ApplicationServices'],
    'includes': ['PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets'],
}

setup(
    name='Leap Monitor',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
    package_dir={'': 'src'},
    packages=find_packages(where='src'),
)
