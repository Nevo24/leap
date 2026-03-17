"""
Image handling for Leap client.

Handles clipboard image detection and saving (macOS).
"""

import hashlib
import os
import subprocess
import tempfile
from typing import Optional

from leap.utils.constants import IMAGE_EXTENSIONS, IMAGES_DIR


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
    Save clipboard image to .storage/images/ (macOS only).

    Uses an MD5 hash of the file content as the filename so that
    saving the same image twice produces the same file (natural dedup).

    Returns:
        Path to the saved image file, or None on failure.
    """
    try:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)

        # Save to a temp file first, then hash and rename
        fd, tmp_path = tempfile.mkstemp(suffix='.png', dir=str(IMAGES_DIR))
        os.close(fd)

        script = f'''
        set png_data to the clipboard as «class PNGf»
        set the_file to open for access POSIX file "{tmp_path}" with write permission
        write png_data to the_file
        close access the_file
        '''

        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            timeout=5
        )
        if result.returncode != 0 or not os.path.exists(tmp_path):
            return None

        # Hash content and rename to dedup
        with open(tmp_path, 'rb') as f:
            content_hash = hashlib.md5(f.read()).hexdigest()[:12]
        final_path = str(IMAGES_DIR / f'{content_hash}.png')
        if os.path.isfile(final_path):
            os.unlink(tmp_path)  # Already exists — dedup
            return final_path
        os.replace(tmp_path, final_path)
        return final_path
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
