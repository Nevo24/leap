"""Text / markdown / URL helpers for the Notes dialog.

Notes serialize inline formatting in two forms:
* ``[display](url)`` — markdown-style links (any scheme with ``://``).
* STX/ETX-wrapped spans (``\\x02 … \\x03``) — bold.

These helpers parse, render, and locate those forms in plain strings or
on Qt widgets (``QTextEdit`` / ``QLineEdit``).  Pure-string helpers are
testable in isolation; Qt-dependent helpers (``_url_at_pos``,
``_link_char_format``, ``_try_open_url``, ``_UrlHighlighter``) require
a live event loop and are exercised through manual UI testing.
"""

import re
from typing import Optional

from PyQt5.QtCore import QPoint, QUrl
from PyQt5.QtGui import (
    QColor, QDesktopServices, QSyntaxHighlighter, QTextCharFormat,
)
from PyQt5.QtWidgets import QLineEdit

from leap.monitor.themes import current_theme


# ── Inline-format constants ─────────────────────────────────────────

_URL_RE = re.compile(r'https?://[^\s<>\"\')]+')
# Any scheme (slack://, mailto:, tel:, etc.)
_ANY_URL_RE = re.compile(r'[a-zA-Z][a-zA-Z0-9+.-]*://\S+|mailto:\S+')
# Markdown link: [display text](url) — negative lookbehind excludes ![image](…)
_LINK_RE = re.compile(r'(?<!!)\[([^\]]+)\]\((\S+://[^\s\)]+)\)')
# Bold is delimited on disk by ASCII control chars STX/ETX (U+0002/U+0003).
# These are impossible to type on a keyboard, so any text the user types —
# including literal ``**``, ``<b>``, ``__``, etc. — is preserved exactly.
# The trade-off: if you open a note's .txt externally, bolded spans show
# up as ``^B…^C`` glyphs.  Notes are edited via the Leap UI by design.
_BOLD_START = '\x02'
_BOLD_END = '\x03'
# Combined inline formats: [link](url) OR STX…ETX.  Bold does not span
# newlines (``.`` without re.DOTALL).  Groups: 1=link-text, 2=link-url,
# 3=bold-text.
_INLINE_FORMAT_RE = re.compile(
    r'(?<!!)\[([^\]]+)\]\((\S+://[^\s\)]+)\)'
    f'|{_BOLD_START}(.+?){_BOLD_END}'
)


# ── URL highlighting ────────────────────────────────────────────────


class _UrlHighlighter(QSyntaxHighlighter):
    """Highlights bare URLs and [text](url) links with the theme accent blue."""

    def __init__(self, parent: 'QTextDocument') -> None:
        super().__init__(parent)
        self._link_fmt = QTextCharFormat()
        t = current_theme()
        self._link_fmt.setForeground(QColor(t.accent_blue))
        self._link_fmt.setFontUnderline(True)
        self._muted_fmt = QTextCharFormat()
        self._muted_fmt.setForeground(QColor(t.text_muted))

    def highlightBlock(self, text: str) -> None:
        # Track which ranges are covered by markdown links
        link_ranges: list[tuple[int, int]] = []
        for m in _LINK_RE.finditer(text):
            # Highlight display text as link
            self.setFormat(m.start(1), len(m.group(1)), self._link_fmt)
            # Dim the surrounding syntax: [ ] ( url )
            self.setFormat(m.start(), 1, self._muted_fmt)        # [
            self.setFormat(m.end(1), 2, self._muted_fmt)         # ](
            self.setFormat(m.start(2), len(m.group(2)), self._muted_fmt)  # url
            self.setFormat(m.end(2), 1, self._muted_fmt)         # )
            link_ranges.append((m.start(), m.end()))
        # Highlight bare URLs not inside markdown links
        for m in _URL_RE.finditer(text):
            if not any(s <= m.start() and m.end() <= e for s, e in link_ranges):
                self.setFormat(m.start(), m.end() - m.start(), self._link_fmt)


# ── URL locating ────────────────────────────────────────────────────

def _url_in_text_at_col(text: str, col: int) -> Optional[str]:
    """Return the URL at column position in text (markdown link or bare URL)."""
    # Check markdown links first — click on display text opens the URL
    for m in _LINK_RE.finditer(text):
        if m.start() <= col < m.end():
            return m.group(2)
    for m in _URL_RE.finditer(text):
        if m.start() <= col < m.end():
            return m.group()
    return None


def _url_at_pos(widget: 'QTextEdit', pos: QPoint) -> Optional[str]:
    """Return the URL at viewport position in a QTextEdit, or None.

    Supports three styles of URL: anchor-format char spans (what the
    checklist popup and text-note editor render for ``[word](url)``),
    literal markdown syntax in the plain text, and bare URL matches.
    """
    # anchorAt clamps at the glyph, so reject empty-space clicks.
    href = widget.anchorAt(pos)
    if href:
        return href
    cursor = widget.cursorForPosition(pos)
    fmt = cursor.charFormat()
    if fmt.isAnchor() and fmt.anchorHref():
        return fmt.anchorHref()
    return _url_in_text_at_col(cursor.block().text(), cursor.positionInBlock())


def _url_at_line_edit_pos(widget: QLineEdit, pos: QPoint) -> Optional[str]:
    """Return the URL at position in a QLineEdit, or None."""
    col = widget.cursorPositionAt(pos)
    return _url_in_text_at_col(widget.text(), col)


def _find_markdown_link_at(text: str, col: int) -> Optional[tuple[int, int, str]]:
    """Return (start, end, display_text) of the markdown link covering col.

    Used by the "Cmd+K with empty URL = unlink" flow for plain-text
    targets (checklist line edit / expand popup) where links are stored
    as literal ``[text](url)`` rather than rich-text anchors.
    """
    for m in _LINK_RE.finditer(text):
        if m.start() <= col <= m.end():
            return m.start(), m.end(), m.group(1)
    return None


def _link_char_format(url: str) -> QTextCharFormat:
    """Return a QTextCharFormat styled as a clickable link.

    Weight is deliberately left unset so callers that merge this
    format into an existing selection keep the existing bold/italic
    attributes — a link applied to a bold word stays bold + becomes
    blue/underlined.  Fresh inserts (no prior format) default to
    normal weight as Qt would.
    """
    fmt = QTextCharFormat()
    t = current_theme()
    fmt.setForeground(QColor(t.accent_blue))
    fmt.setFontUnderline(True)
    fmt.setAnchor(True)
    fmt.setAnchorHref(url)
    return fmt


def _try_open_url(url: str) -> None:
    """Open a URL in the default browser."""
    QDesktopServices.openUrl(QUrl(url))


# ── Display ↔ raw position math ─────────────────────────────────────

def _strip_markdown_links(text: str) -> str:
    """Return *text* with markdown ``[display](url)`` spans reduced to ``display``."""
    return _LINK_RE.sub(r'\1', text)


def _display_to_raw_pos(raw: str, display_pos: int) -> int:
    """Map a position in the stripped display text to the raw text.

    Walks the raw string, counting display characters.  Link syntax
    ``[text](url)`` contributes ``len(text)`` display chars; STX/ETX
    bold markers contribute zero.  Returns the raw-string position
    that corresponds to *display_pos*.  Used by the checklist line
    edit's Cmd+K fallback so link insertion slices ``_raw_text`` at
    the right offset even when the raw contains existing markdown.
    """
    display_count = 0
    pos = 0
    while pos < len(raw):
        if display_count >= display_pos:
            return pos
        # Skip STX/ETX markers (invisible in display)
        if raw[pos] in (_BOLD_START, _BOLD_END):
            pos += 1
            continue
        # Handle [display](url) as a unit
        m = _LINK_RE.match(raw, pos)
        if m:
            display_len = len(m.group(1))
            if display_count + display_len > display_pos:
                # display_pos lands inside this link's display text —
                # return the position within the link's bracketed text
                return m.start(1) + (display_pos - display_count)
            display_count += display_len
            pos = m.end()
            continue
        # Plain character
        pos += 1
        display_count += 1
    return pos


def _strip_inline_formats(text: str) -> str:
    """Strip markdown link syntax AND STX/ETX bold markers for plain display.

    Use in place of ``_strip_markdown_links`` wherever the raw item
    text is shown in a QLineEdit (which can't render per-character
    styling) — the rich-text overlay handles styled rendering.
    """
    text = _LINK_RE.sub(r'\1', text)
    return text.replace(_BOLD_START, '').replace(_BOLD_END, '')


def _link_at_stripped_pos(raw: str, stripped_pos: int) -> Optional[str]:
    """If *stripped_pos* falls within a markdown link's display text in
    *raw*, return that link's URL.  Otherwise ``None``.

    Used so clicks on the rendered "word" in a checklist item's line
    edit (which hides the ``[…](url)`` syntax) can still open the link.
    Strips STX/ETX bold markers from ``raw`` first so bold-wrapped
    links (``\\x02[word](url)\\x03``) map correctly between raw and
    display positions.
    """
    raw = raw.replace(_BOLD_START, '').replace(_BOLD_END, '')
    stripped_count = 0
    raw_pos = 0
    for m in _LINK_RE.finditer(raw):
        before_len = m.start() - raw_pos
        if stripped_count + before_len > stripped_pos:
            return None  # plain text before link
        stripped_count += before_len
        display_len = len(m.group(1))
        if stripped_count <= stripped_pos < stripped_count + display_len:
            return m.group(2)
        stripped_count += display_len
        raw_pos = m.end()
    return None
