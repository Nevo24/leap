"""Parse and serialize the on-disk checklist format.

Checklists serialize as one-item-per-line markdown:

    - [ ] todo item
    - [x] done item
    - [X] also done

Inline formatting (markdown links, STX/ETX bold spans) is preserved
verbatim inside the item text — display-stripping happens at render
time, not at parse/serialize time.
"""

from leap.monitor.dialogs.notes.text_helpers import _BOLD_END, _BOLD_START


def _parse_checklist(text: str) -> list[dict]:
    """Parse markdown-style checklist text into item dicts.

    ``item['text']`` preserves inline formatting verbatim — markdown
    link syntax and STX/ETX bold markers both stay in the text so the
    rich-text overlay can render partial styling.  Display stripping
    happens at render time via ``_strip_inline_formats``.

    ``item['bold']`` is retained as ``False`` for a transitional API
    shape — partial-item bold is expressed inline rather than via a
    single flag.  Legacy disk format (whole item wrapped in STX/ETX)
    is preserved as-is in ``text``; the overlay renders the bold span.
    """
    items: list[dict] = []
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in ('- [x]', '- [X]') or stripped.startswith('- [x] ') or stripped.startswith('- [X] '):
            raw = stripped[6:] if len(stripped) > 6 else ''
            items.append({'text': raw, 'checked': True, 'bold': False})
        elif stripped == '- [ ]' or stripped.startswith('- [ ] '):
            raw = stripped[6:] if len(stripped) > 6 else ''
            items.append({'text': raw, 'checked': False, 'bold': False})
        else:
            items.append({'text': stripped, 'checked': False, 'bold': False})
    return items


def _serialize_checklist(items: list[dict]) -> str:
    """Serialize item dicts to markdown-style checklist text.

    ``item['text']`` is written as-is — it already carries any inline
    STX/ETX bold markers and markdown link spans.  The deprecated
    ``bold`` flag (legacy whole-item toggle) is still respected for
    migration: if set, the text is wrapped in STX/ETX.
    """
    lines: list[str] = []
    for item in items:
        if not item['text'] and not item['checked']:
            continue  # skip empty unchecked items
        mark = 'x' if item['checked'] else ' '
        text = item['text']
        if (item.get('bold') and text
                and _BOLD_START not in text):
            # Legacy flag-based bold — migrate to inline on write.
            text = f'{_BOLD_START}{text}{_BOLD_END}'
        lines.append(f'- [{mark}] {text}')
    return '\n'.join(lines)
