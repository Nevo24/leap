"""Note-image helpers: save/dedup, reference collection, cleanup, preview popup.

Notes embed images as ``![image](<md5>.png)`` markers; the actual PNG
files live under ``NOTE_IMAGES_DIR`` and are MD5-deduplicated by content
so two notes pasting the same screenshot share the same file.

These helpers handle the full image lifecycle:
* :func:`_save_note_image` — write a ``QImage`` to disk under its content hash.
* :func:`_collect_image_refs` / :func:`_all_note_image_refs` — find which
  images are referenced by which notes (used to gate orphan cleanup).
* :func:`_cleanup_orphaned_images` — delete images dropped from a note
  unless another note still references them.
* :class:`_ImagePreviewPopup` — frameless tooltip showing a larger preview
  on hover.
"""

import hashlib
import re
from typing import Optional

from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import QApplication, QLabel

from leap.utils.constants import NOTE_IMAGES_DIR, NOTES_DIR


_IMAGE_MARKER_RE = re.compile(r'!\[image\]\(([a-f0-9]+\.png)\)')
# In-memory placeholder used while editing a checklist item.  QLineEdit
# can't display inline images, so pasted images are shown as ``[Image #N]``
# tokens until the item is serialised back to ``![image](hash.png)``.
_CHECKLIST_PLACEHOLDER_RE = re.compile(r'\[Image #\d+\]')
_NOTE_IMAGE_MAX_WIDTH = 400
_NOTE_IMAGE_PREVIEW_MAX = 600


# ── Save / dedup ────────────────────────────────────────────────────

def _save_note_image(image: QImage) -> Optional[str]:
    """Save a QImage to .storage/note_images/ with MD5 dedup.

    Returns:
        Filename (e.g. 'abc123.png') on success, None on failure.
    """
    try:
        NOTE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        buf = image.bits().asstring(image.sizeInBytes())
        content_hash = hashlib.md5(buf).hexdigest()[:12]
        filename = f'{content_hash}.png'
        path = NOTE_IMAGES_DIR / filename
        if path.is_file():
            return filename
        if image.save(str(path), 'PNG'):
            return filename
        try:
            path.unlink()
        except OSError:
            pass
        return None
    except (OSError, Exception):
        return None


# ── Reference collection ────────────────────────────────────────────

def _collect_image_refs(text: str) -> set[str]:
    """Return set of image filenames referenced in note text."""
    return set(_IMAGE_MARKER_RE.findall(text))


def _all_note_image_refs(exclude_name: Optional[str] = None) -> set[str]:
    """Scan all notes on disk and return the union of referenced image filenames.

    Args:
        exclude_name: Note name (relative path without .txt) to skip.
    """
    refs: set[str] = set()
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    for p in NOTES_DIR.rglob('*.txt'):
        if not p.is_file():
            continue
        rel = str(p.relative_to(NOTES_DIR).with_suffix(''))
        if exclude_name and rel == exclude_name:
            continue
        try:
            refs |= _collect_image_refs(p.read_text(encoding='utf-8'))
        except OSError:
            pass
    return refs


# ── Orphan cleanup ──────────────────────────────────────────────────

def _cleanup_orphaned_images(
    current_text: str, previous_text: str, note_name: str,
    pasted: Optional[set[str]] = None,
    deferred: Optional[set[str]] = None,
) -> None:
    """Delete images removed from a note, unless still used by another note.

    *pasted* includes images saved to disk this session that may not appear
    in *previous_text* (e.g. pasted then deleted before save).

    When *deferred* is provided (a mutable set), orphaned filenames are
    collected into the set instead of being deleted immediately.  The caller
    is responsible for calling the actual unlink later (e.g. on dialog close).
    """
    old_refs = _collect_image_refs(previous_text)
    if pasted:
        old_refs |= pasted
    new_refs = _collect_image_refs(current_text)
    candidates = old_refs - new_refs
    if not candidates:
        return
    # Check all other notes before deleting
    other_refs = _all_note_image_refs(exclude_name=note_name)
    for filename in candidates - other_refs:
        if deferred is not None:
            deferred.add(filename)
        else:
            try:
                (NOTE_IMAGES_DIR / filename).unlink(missing_ok=True)
            except OSError:
                pass


# ── Hover preview popup ─────────────────────────────────────────────

class _ImagePreviewPopup(QLabel):
    """Frameless popup that shows a larger version of a note image."""

    def __init__(self) -> None:
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setStyleSheet('background: transparent; padding: 0px;')
        self._current_name: Optional[str] = None

    def show_for_image(self, name: str, global_pos: QPoint) -> None:
        """Show the popup near *global_pos* for the image *name*."""
        if name == self._current_name and self.isVisible():
            return
        path = str(NOTE_IMAGES_DIR / name)
        px = QPixmap(path)
        if px.isNull():
            self.hide()
            return
        if px.width() > _NOTE_IMAGE_PREVIEW_MAX or px.height() > _NOTE_IMAGE_PREVIEW_MAX:
            px = px.scaled(
                _NOTE_IMAGE_PREVIEW_MAX, _NOTE_IMAGE_PREVIEW_MAX,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
        self._current_name = name
        self.setPixmap(px)
        self.adjustSize()
        # Position below and to the right of cursor, clamped to screen
        screen = QApplication.screenAt(global_pos)
        if screen:
            sg = screen.availableGeometry()
            x = min(global_pos.x() + 12, sg.right() - self.width())
            y = min(global_pos.y() + 12, sg.bottom() - self.height())
            self.move(max(x, sg.left()), max(y, sg.top()))
        else:
            self.move(global_pos.x() + 12, global_pos.y() + 12)
        self.show()

    def hide_preview(self) -> None:
        self._current_name = None
        self.hide()
