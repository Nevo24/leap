"""
Image handling for ClaudeQ client.

Handles clipboard image detection and saving (macOS).
"""

import os
import subprocess
import tempfile
from typing import Optional

from claudeq.utils.constants import IMAGE_EXTENSIONS


def check_clipboard_has_image() -> bool:
    """
    Check if clipboard contains an image (macOS only).

    Returns:
        True if clipboard contains an image.
    """
    try:
        result = subprocess.run(
            ['osascript', '-e', 'clipboard info'],
            capture_output=True,
            text=True,
            timeout=1
        )
        return 'picture' in result.stdout.lower()
    except (subprocess.SubprocessError, OSError):
        return False


def save_clipboard_image() -> Optional[str]:
    """
    Save clipboard image to a temporary file (macOS only).

    Returns:
        Path to the saved image file, or None on failure.
    """
    try:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.png')
        temp_path = temp_file.name
        temp_file.close()

        script = f'''
        set png_data to the clipboard as «class PNGf»
        set the_file to open for access POSIX file "{temp_path}" with write permission
        write png_data to the_file
        close access the_file
        '''

        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            timeout=5
        )
        if result.returncode == 0 and os.path.exists(temp_path):
            return temp_path
        return None
    except (subprocess.SubprocessError, OSError):
        return None


def is_image_file(path: str) -> bool:
    """
    Check if path points to an image file.

    Args:
        path: File path to check.

    Returns:
        True if path exists and is an image file.
    """
    if not os.path.exists(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    return ext in IMAGE_EXTENSIONS
