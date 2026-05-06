"""RTL (right-to-left) text-direction helpers for the Notes dialog.

Detect the dominant directionality of a string from its first
directional character and apply it to a Qt widget's layout direction.
"""

import unicodedata
from typing import Optional

from PyQt5.QtCore import Qt


def _text_is_rtl(text: str) -> Optional[bool]:
    """Return True if the first letter in *text* is RTL, False if LTR, None if no letter."""
    for ch in text:
        bidi = unicodedata.bidirectional(ch)
        if bidi in ('R', 'AL', 'AN'):
            return True
        if bidi == 'L':
            return False
    return None


def _apply_rtl_direction(widget: 'QWidget', text: str) -> None:
    """Set layout direction on a QLineEdit based on RTL detection of text."""
    rtl = _text_is_rtl(text)
    want = Qt.RightToLeft if rtl is True else Qt.LeftToRight
    if widget.layoutDirection() != want:
        widget.setLayoutDirection(want)
