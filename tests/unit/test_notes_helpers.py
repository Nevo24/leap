"""Characterization tests for the pure / pure-ish helpers in
``leap.monitor.dialogs.notes_dialog``.

These tests exist to pin current behavior **before** the planned refactor
that splits ``notes_dialog.py`` (5,679 lines) into a sub-package.  They
exercise only module-level helpers that don't require a Qt event loop
or a live ``NotesDialog`` instance, so they run as fast unit tests.

Per ``CLAUDE.md`` the project conventions allow the same monkey-patch
pattern used by ``test_notes_undo.py``:
``monkeypatch.setattr(nd, 'NOTES_DIR', tmp)`` and friends.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import leap.monitor.dialogs.notes_dialog as nd
from leap.monitor.dialogs.notes import image_helpers as nd_images
from leap.monitor.dialogs.notes import ordering as nd_ordering
from leap.monitor.dialogs.notes import persistence as nd_persist


# ---------------------------------------------------------------------------
# Fixture — redirect FS roots into a tmp dir
# ---------------------------------------------------------------------------
# Helpers that read NOTES_DIR / NOTE_IMAGES_DIR / _NOTES_META_FILE bind those
# names from their own module's namespace, so patching only `nd` would miss
# helpers that have moved to a sub-module.  Patch every module that imports
# one of those constants directly.

@pytest.fixture
def notes_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / 'notes'
    d.mkdir()
    img_d = tmp_path / 'note_images'
    img_d.mkdir()
    meta = d / '.notes_meta.json'
    monkeypatch.setattr(nd, 'NOTES_DIR', d)
    monkeypatch.setattr(nd, 'NOTE_IMAGES_DIR', img_d)
    monkeypatch.setattr(nd_persist, 'NOTES_DIR', d)
    monkeypatch.setattr(nd_persist, '_NOTES_META_FILE', meta)
    monkeypatch.setattr(nd_ordering, 'NOTES_DIR', d)
    monkeypatch.setattr(nd_images, 'NOTES_DIR', d)
    monkeypatch.setattr(nd_images, 'NOTE_IMAGES_DIR', img_d)
    return d


# ===========================================================================
# Text / markdown helpers
# ===========================================================================

class TestStripMarkdownLinks:
    def test_strips_simple_link(self) -> None:
        assert nd._strip_markdown_links(
            'before [link](http://x.com) after'
        ) == 'before link after'

    def test_preserves_image_marker(self) -> None:
        # Negative lookbehind on '!' protects ![image](…) from being stripped.
        assert nd._strip_markdown_links('![image](abc.png)') == '![image](abc.png)'

    def test_strips_multiple_links(self) -> None:
        assert nd._strip_markdown_links(
            '[a](slack://b) and [c](https://d)'
        ) == 'a and c'

    def test_unparseable_link_is_left_alone(self) -> None:
        # No URL part — not a markdown link.
        assert nd._strip_markdown_links('[notalink]') == '[notalink]'
        # Missing scheme — _LINK_RE requires `://`.
        assert nd._strip_markdown_links('[a](no-scheme)') == '[a](no-scheme)'

    def test_empty_string(self) -> None:
        assert nd._strip_markdown_links('') == ''


class TestStripInlineFormats:
    def test_strips_bold_markers(self) -> None:
        assert nd._strip_inline_formats('\x02bold\x03') == 'bold'

    def test_strips_inline_bold(self) -> None:
        assert nd._strip_inline_formats('a\x02bold\x03b') == 'aboldb'

    def test_strips_links(self) -> None:
        assert nd._strip_inline_formats('[link](http://x.com)') == 'link'

    def test_strips_bold_around_link(self) -> None:
        assert nd._strip_inline_formats(
            '\x02[link](http://x.com)\x03'
        ) == 'link'


class TestUrlInTextAtCol:
    """`_url_in_text_at_col` returns the URL when *col* is anywhere
    inside a markdown link's full ``[text](url)`` span (display text
    AND syntax) or anywhere inside a bare URL.  Half-open interval:
    ``m.start() <= col < m.end()``.
    """

    text = 'before [link](http://x.com) after'

    def test_outside_link_returns_none(self) -> None:
        # Plain text region.
        assert nd._url_in_text_at_col(self.text, 0) is None
        # Trailing space immediately after the closing paren.
        assert nd._url_in_text_at_col(self.text, 27) is None

    def test_at_open_bracket(self) -> None:
        assert nd._url_in_text_at_col(self.text, 7) == 'http://x.com'

    def test_inside_display_text(self) -> None:
        assert nd._url_in_text_at_col(self.text, 8) == 'http://x.com'

    def test_inside_url_part(self) -> None:
        assert nd._url_in_text_at_col(self.text, 14) == 'http://x.com'

    def test_at_close_paren_inclusive(self) -> None:
        # Half-open: m.start() <= col < m.end().  Close paren index 26 is
        # inside; index 27 (the space) is not.
        assert nd._url_in_text_at_col(self.text, 26) == 'http://x.com'
        assert nd._url_in_text_at_col(self.text, 27) is None

    def test_bare_url(self) -> None:
        text = 'see http://example.com here'
        assert nd._url_in_text_at_col(text, 4) == 'http://example.com'
        # The space between URL and "here" is not part of the URL.
        assert nd._url_in_text_at_col(text, 22) is None

    def test_no_url_at_all(self) -> None:
        assert nd._url_in_text_at_col('plain text', 5) is None


class TestFindMarkdownLinkAt:
    """`_find_markdown_link_at` is more permissive than
    `_url_in_text_at_col` — it uses inclusive end (``col <= m.end()``)
    so a cursor sitting *just after* the closing paren still reports
    the link.  Drives the Cmd+K-with-empty-URL = unlink flow.
    """

    text = 'before [link](http://x.com) after'

    def test_outside_returns_none(self) -> None:
        assert nd._find_markdown_link_at(self.text, 0) is None
        assert nd._find_markdown_link_at(self.text, 6) is None
        # Inclusive end means col=27 (right after `)`) still matches —
        # but col=28 (over the 'a' of 'after') does not.
        assert nd._find_markdown_link_at(self.text, 28) is None

    def test_returns_span_and_display(self) -> None:
        assert nd._find_markdown_link_at(self.text, 7) == (7, 27, 'link')
        assert nd._find_markdown_link_at(self.text, 12) == (7, 27, 'link')
        # Inclusive of m.end().
        assert nd._find_markdown_link_at(self.text, 27) == (7, 27, 'link')


class TestLinkAtStrippedPos:
    """`_link_at_stripped_pos` maps a position in the *stripped* display
    text back to the URL of the markdown link that covers it (if any).
    Used by checklist line-edit clicks where ``[…](url)`` syntax is
    hidden but the underlying raw still contains it.
    """

    raw = 'before [link](http://x.com) after'
    # stripped: 'before link after' (17 chars)

    def test_outside_link_returns_none(self) -> None:
        assert nd._link_at_stripped_pos(self.raw, 0) is None
        assert nd._link_at_stripped_pos(self.raw, 6) is None
        assert nd._link_at_stripped_pos(self.raw, 11) is None
        assert nd._link_at_stripped_pos(self.raw, 17) is None

    def test_inside_display_returns_url(self) -> None:
        # Display "link" spans stripped positions 7..10 (half-open).
        assert nd._link_at_stripped_pos(self.raw, 7) == 'http://x.com'
        assert nd._link_at_stripped_pos(self.raw, 9) == 'http://x.com'
        assert nd._link_at_stripped_pos(self.raw, 10) == 'http://x.com'

    def test_strips_bold_markers_first(self) -> None:
        # STX/ETX bold wrappers are dropped before the position math runs,
        # so a bold-wrapped link still resolves correctly.
        raw = '\x02[link](http://x.com)\x03'
        # Stripped display = "link" (4 chars).  Pos 0..3 inside.
        assert nd._link_at_stripped_pos(raw, 0) == 'http://x.com'
        assert nd._link_at_stripped_pos(raw, 3) == 'http://x.com'
        assert nd._link_at_stripped_pos(raw, 4) is None


class TestDisplayToRawPos:
    """`_display_to_raw_pos` maps a cursor offset in the *display*
    text (link syntax stripped, STX/ETX invisible) to the corresponding
    cursor offset in the *raw* text.  Cursor positions are between
    characters, so display_pos=N means "after N display chars".
    """

    def test_no_inline_format_passes_through(self) -> None:
        raw = 'plain text'
        for i in range(len(raw) + 1):
            assert nd._display_to_raw_pos(raw, i) == i

    def test_with_link(self) -> None:
        raw = 'hello [world](http://x.com)'
        # display = "hello world" (11 chars)
        assert nd._display_to_raw_pos(raw, 0) == 0
        assert nd._display_to_raw_pos(raw, 5) == 5     # space
        # Cursor at display 6 is between space and 'w' (display).  In raw,
        # that maps to position 6 — between space and the '[' that opens
        # the link.  (Cursor lands BEFORE the link span, not inside it.)
        assert nd._display_to_raw_pos(raw, 6) == 6
        # Once display_pos lands strictly inside the link's display text,
        # the raw position points inside the bracketed group.
        assert nd._display_to_raw_pos(raw, 7) == 8     # 'o'
        assert nd._display_to_raw_pos(raw, 10) == 11   # past 'd', at ']'
        # Cursor at the END of the display text → raw end of the link span.
        assert nd._display_to_raw_pos(raw, 11) == len(raw)

    def test_past_end_returns_raw_len(self) -> None:
        raw = 'hello [world](http://x.com)'
        assert nd._display_to_raw_pos(raw, 100) == len(raw)

    def test_skips_bold_markers(self) -> None:
        raw = '\x02hi\x03'
        # display = "hi" (2 chars).  STX/ETX contribute zero display chars.
        # NOTE: display_pos=0 returns 0 (BEFORE the STX), not 1 (inside
        # the bold span) — the loop's "already at display_pos" check fires
        # before the STX-skip.  Matches current behaviour.
        assert nd._display_to_raw_pos(raw, 0) == 0
        assert nd._display_to_raw_pos(raw, 1) == 2     # after 'h'
        assert nd._display_to_raw_pos(raw, 2) == 3     # at \x03
        assert nd._display_to_raw_pos(raw, 3) == 4     # past end


# ===========================================================================
# Checklist parse/serialize round-trip
# ===========================================================================

class TestParseChecklist:
    def test_unchecked(self) -> None:
        assert nd._parse_checklist('- [ ] todo') == [
            {'text': 'todo', 'checked': False, 'bold': False},
        ]

    def test_checked_lowercase(self) -> None:
        assert nd._parse_checklist('- [x] hello') == [
            {'text': 'hello', 'checked': True, 'bold': False},
        ]

    def test_checked_uppercase(self) -> None:
        # Capital X is also accepted as checked.
        assert nd._parse_checklist('- [X] capital') == [
            {'text': 'capital', 'checked': True, 'bold': False},
        ]

    def test_empty_unchecked(self) -> None:
        # No trailing space — still a valid empty unchecked item.
        assert nd._parse_checklist('- [ ]') == [
            {'text': '', 'checked': False, 'bold': False},
        ]

    def test_empty_checked(self) -> None:
        assert nd._parse_checklist('- [x]') == [
            {'text': '', 'checked': True, 'bold': False},
        ]

    def test_plain_line_becomes_unchecked(self) -> None:
        assert nd._parse_checklist('plain text') == [
            {'text': 'plain text', 'checked': False, 'bold': False},
        ]

    def test_unrecognised_bracket_marker_treated_as_plain(self) -> None:
        # "- [?]" is not a checkbox — kept verbatim as plain text.
        assert nd._parse_checklist('- [?] unknown') == [
            {'text': '- [?] unknown', 'checked': False, 'bold': False},
        ]

    def test_blank_input_yields_empty(self) -> None:
        assert nd._parse_checklist('') == []
        assert nd._parse_checklist('   ') == []
        assert nd._parse_checklist('\n\n\n') == []

    def test_whitespace_only_lines_are_skipped(self) -> None:
        assert nd._parse_checklist('- [x] a\n\n  \n- [ ] b') == [
            {'text': 'a', 'checked': True, 'bold': False},
            {'text': 'b', 'checked': False, 'bold': False},
        ]

    def test_indentation_is_stripped(self) -> None:
        # Lines are stripped before pattern matching.
        assert nd._parse_checklist('  - [x] indented') == [
            {'text': 'indented', 'checked': True, 'bold': False},
        ]

    def test_inline_bold_preserved_in_text(self) -> None:
        # STX/ETX markers stay in `text`; bold flag stays False.
        assert nd._parse_checklist('- [x] \x02bold\x03') == [
            {'text': '\x02bold\x03', 'checked': True, 'bold': False},
        ]

    def test_inline_link_preserved_in_text(self) -> None:
        assert nd._parse_checklist('- [ ] [link](http://x.com)') == [
            {'text': '[link](http://x.com)', 'checked': False, 'bold': False},
        ]


class TestSerializeChecklist:
    def test_unchecked(self) -> None:
        items = [{'text': 'a', 'checked': False, 'bold': False}]
        assert nd._serialize_checklist(items) == '- [ ] a'

    def test_checked_with_text(self) -> None:
        items = [{'text': 'a', 'checked': True, 'bold': False}]
        assert nd._serialize_checklist(items) == '- [x] a'

    def test_empty_unchecked_item_dropped(self) -> None:
        items = [{'text': '', 'checked': False, 'bold': False}]
        assert nd._serialize_checklist(items) == ''

    def test_empty_checked_item_kept(self) -> None:
        # Empty CHECKED items survive — only unchecked-and-empty are dropped.
        items = [{'text': '', 'checked': True, 'bold': False}]
        assert nd._serialize_checklist(items) == '- [x] '

    def test_legacy_bold_flag_wraps_in_stx_etx(self) -> None:
        items = [{'text': 'b', 'checked': True, 'bold': True}]
        assert nd._serialize_checklist(items) == '- [x] \x02b\x03'

    def test_legacy_bold_flag_skipped_if_already_inline(self) -> None:
        # If the text already contains STX, don't double-wrap.
        items = [{'text': '\x02already\x03', 'checked': False, 'bold': True}]
        assert nd._serialize_checklist(items) == '- [ ] \x02already\x03'

    def test_multiline(self) -> None:
        items = [
            {'text': 'a', 'checked': False, 'bold': False},
            {'text': 'b', 'checked': True, 'bold': False},
        ]
        assert nd._serialize_checklist(items) == '- [ ] a\n- [x] b'


class TestChecklistRoundTrip:
    @pytest.mark.parametrize('text', [
        '- [ ] a',
        '- [x] a',
        '- [ ] a\n- [x] b\n- [ ] c',
        '- [x] \x02bold\x03',
        '- [ ] [link](http://x.com)',
        '- [x] inline \x02bold\x03 mid-text',
    ])
    def test_round_trip_preserves_text(self, text: str) -> None:
        assert nd._serialize_checklist(nd._parse_checklist(text)) == text


# ===========================================================================
# Image-ref helpers
# ===========================================================================

class TestCollectImageRefs:
    def test_empty(self) -> None:
        assert nd._collect_image_refs('') == set()

    def test_single_ref(self) -> None:
        assert nd._collect_image_refs(
            '![image](abc123.png)'
        ) == {'abc123.png'}

    def test_multiple_unique(self) -> None:
        text = '![image](aaa.png) and ![image](bbb.png)'
        assert nd._collect_image_refs(text) == {'aaa.png', 'bbb.png'}

    def test_dedups(self) -> None:
        text = '![image](aaa.png) ![image](bbb.png) ![image](aaa.png)'
        assert nd._collect_image_refs(text) == {'aaa.png', 'bbb.png'}

    def test_no_leading_bang_means_no_match(self) -> None:
        # That's a markdown LINK, not an image embed.
        assert nd._collect_image_refs('[image](abc.png)') == set()

    def test_only_lowercase_hex_matches(self) -> None:
        # Regex is `[a-f0-9]+\.png` — uppercase hex is rejected.
        assert nd._collect_image_refs('![image](ABC.png)') == set()
        assert nd._collect_image_refs('![image](abc.png)') == {'abc.png'}


class TestAllNoteImageRefs:
    def test_empty_dir(self, notes_dir: Path) -> None:
        assert nd._all_note_image_refs() == set()

    def test_collects_from_top_level(self, notes_dir: Path) -> None:
        (notes_dir / 'a.txt').write_text(
            '![image](aaa.png) ![image](bbb.png)', encoding='utf-8',
        )
        (notes_dir / 'b.txt').write_text(
            '![image](ccc.png)', encoding='utf-8',
        )
        assert nd._all_note_image_refs() == {'aaa.png', 'bbb.png', 'ccc.png'}

    def test_recurses_into_folders(self, notes_dir: Path) -> None:
        (notes_dir / 'sub').mkdir()
        (notes_dir / 'a.txt').write_text(
            '![image](aaa.png)', encoding='utf-8',
        )
        (notes_dir / 'sub' / 'c.txt').write_text(
            '![image](ddd.png)', encoding='utf-8',
        )
        assert nd._all_note_image_refs() == {'aaa.png', 'ddd.png'}

    def test_excludes_named_note(self, notes_dir: Path) -> None:
        (notes_dir / 'a.txt').write_text(
            '![image](aaa.png)', encoding='utf-8',
        )
        (notes_dir / 'b.txt').write_text(
            '![image](bbb.png)', encoding='utf-8',
        )
        # exclude_name uses the relative path (no .txt suffix).
        assert nd._all_note_image_refs(exclude_name='a') == {'bbb.png'}

    def test_excludes_named_note_in_folder(self, notes_dir: Path) -> None:
        (notes_dir / 'sub').mkdir()
        (notes_dir / 'a.txt').write_text(
            '![image](aaa.png)', encoding='utf-8',
        )
        (notes_dir / 'sub' / 'c.txt').write_text(
            '![image](ccc.png)', encoding='utf-8',
        )
        assert nd._all_note_image_refs(exclude_name='sub/c') == {'aaa.png'}


class TestCleanupOrphanedImages:
    # Image filenames must match the `_IMAGE_MARKER_RE` regex
    # (`[a-f0-9]+\.png`) — `_save_note_image` uses a 12-char MD5 prefix.
    # Tests use lowercase-hex names to mirror real-world filenames.

    def test_unlinks_truly_orphaned(self, notes_dir: Path) -> None:
        img = notes_dir.parent / 'note_images' / 'aaa111bbb222.png'
        img.write_bytes(b'fake')
        # Note 'a' previously referenced the image; new content drops it.
        # No other notes reference it — should be unlinked.
        nd._cleanup_orphaned_images(
            current_text='',
            previous_text='![image](aaa111bbb222.png)',
            note_name='a',
        )
        assert not img.exists()

    def test_keeps_image_used_by_other_note(self, notes_dir: Path) -> None:
        img = notes_dir.parent / 'note_images' / 'aaa111bbb222.png'
        img.write_bytes(b'fake')
        (notes_dir / 'b.txt').write_text(
            '![image](aaa111bbb222.png)', encoding='utf-8',
        )
        # Note 'a' drops the image, but note 'b' still references it.
        nd._cleanup_orphaned_images(
            current_text='',
            previous_text='![image](aaa111bbb222.png)',
            note_name='a',
        )
        assert img.exists()

    def test_includes_pasted_session_set(self, notes_dir: Path) -> None:
        # An image pasted this session may not appear in `previous_text`
        # if it was pasted-then-deleted before save; `pasted` covers that.
        img = notes_dir.parent / 'note_images' / 'ccc333.png'
        img.write_bytes(b'fake')
        nd._cleanup_orphaned_images(
            current_text='', previous_text='',
            note_name='a', pasted={'ccc333.png'},
        )
        assert not img.exists()

    def test_deferred_set_collects_instead_of_deleting(
        self, notes_dir: Path,
    ) -> None:
        img = notes_dir.parent / 'note_images' / 'aaa111bbb222.png'
        img.write_bytes(b'fake')
        deferred: set[str] = set()
        nd._cleanup_orphaned_images(
            current_text='',
            previous_text='![image](aaa111bbb222.png)',
            note_name='a', deferred=deferred,
        )
        # File still on disk; caller is responsible for unlinking later.
        assert img.exists()
        assert deferred == {'aaa111bbb222.png'}


# ===========================================================================
# Order / folder persistence helpers
# ===========================================================================

class TestLoadSaveOrder:
    def test_load_when_no_meta_file(self, notes_dir: Path) -> None:
        assert nd._load_order() == {}

    def test_save_then_load_round_trip(self, notes_dir: Path) -> None:
        order = {'': ['a', 'b'], 'sub': ['x']}
        nd._save_order(order)
        assert nd._load_order() == order

    def test_save_empty_drops_order_key(self, notes_dir: Path) -> None:
        # Pre-existing notes meta should survive.
        nd._save_notes_meta({
            '_order': {'': ['a']},
            'note': {'mode': 'text'},
        })
        nd._save_order({})
        on_disk = json.loads(
            (notes_dir / '.notes_meta.json').read_text(encoding='utf-8'),
        )
        assert '_order' not in on_disk
        assert on_disk['note'] == {'mode': 'text'}


class TestRenameInOrder:
    def test_renames_leaf(self, notes_dir: Path) -> None:
        nd._save_order({'': ['a', 'b', 'c']})
        nd._rename_in_order('', 'b', 'B')
        assert nd._load_order() == {'': ['a', 'B', 'c']}

    def test_rename_in_subfolder(self, notes_dir: Path) -> None:
        nd._save_order({'foo': ['x', 'y']})
        nd._rename_in_order('foo', 'y', 'Y')
        assert nd._load_order() == {'foo': ['x', 'Y']}

    def test_unknown_leaf_is_silent_noop(self, notes_dir: Path) -> None:
        nd._save_order({'': ['a']})
        nd._rename_in_order('', 'missing', 'X')
        assert nd._load_order() == {'': ['a']}

    def test_unknown_folder_is_silent_noop(self, notes_dir: Path) -> None:
        nd._save_order({})
        nd._rename_in_order('nope', 'a', 'b')
        assert nd._load_order() == {}


class TestRemoveFromOrder:
    def test_removes_leaf(self, notes_dir: Path) -> None:
        nd._save_order({'': ['a', 'b', 'c']})
        nd._remove_from_order('', 'b')
        assert nd._load_order() == {'': ['a', 'c']}

    def test_removing_last_leaf_drops_folder_key(
        self, notes_dir: Path,
    ) -> None:
        nd._save_order({'sub': ['only']})
        nd._remove_from_order('sub', 'only')
        assert nd._load_order() == {}

    def test_unknown_leaf_is_silent_noop(self, notes_dir: Path) -> None:
        nd._save_order({'': ['a']})
        nd._remove_from_order('', 'missing')
        assert nd._load_order() == {'': ['a']}


class TestRenameOrderKeys:
    def test_renames_self_and_descendants(self, notes_dir: Path) -> None:
        nd._save_order({
            'foo': ['x'],
            'foo/bar': ['y'],
            'foo/bar/baz': ['z'],
            'other': ['q'],
        })
        nd._rename_order_keys('foo', 'FOO')
        assert nd._load_order() == {
            'FOO': ['x'],
            'FOO/bar': ['y'],
            'FOO/bar/baz': ['z'],
            'other': ['q'],
        }

    def test_no_match_is_silent_noop(self, notes_dir: Path) -> None:
        nd._save_order({'foo': ['x']})
        nd._rename_order_keys('bar', 'BAR')
        assert nd._load_order() == {'foo': ['x']}


class TestDeleteOrderKeys:
    def test_deletes_self_and_descendants(self, notes_dir: Path) -> None:
        nd._save_order({
            'foo': ['x'],
            'foo/bar': ['y'],
            'baz': ['z'],
        })
        nd._delete_order_keys('foo')
        assert nd._load_order() == {'baz': ['z']}

    def test_does_not_match_prefix_substring(
        self, notes_dir: Path,
    ) -> None:
        # "foo" must NOT delete "foobar" — the slash boundary protects.
        nd._save_order({'foo': ['x'], 'foobar': ['y']})
        nd._delete_order_keys('foo')
        assert nd._load_order() == {'foobar': ['y']}


class TestListFolders:
    def test_empty(self, notes_dir: Path) -> None:
        assert nd._list_folders() == []

    def test_alphabetical(self, notes_dir: Path) -> None:
        (notes_dir / 'zfolder').mkdir()
        (notes_dir / 'afolder').mkdir()
        (notes_dir / 'afolder' / 'inner').mkdir()
        assert nd._list_folders() == ['afolder', 'afolder/inner', 'zfolder']

    def test_files_excluded(self, notes_dir: Path) -> None:
        (notes_dir / 'a.txt').write_text('', encoding='utf-8')
        (notes_dir / 'sub').mkdir()
        assert nd._list_folders() == ['sub']


class TestRenameFolderMeta:
    def test_renames_matching_keys(self, notes_dir: Path) -> None:
        nd._save_notes_meta({
            'foo/n1': {'mode': 'checklist'},
            'foo/sub/n2': {'mode': 'text'},
            'other': {'mode': 'text'},
        })
        nd._rename_folder_meta('foo', 'FOO')
        assert nd._load_notes_meta() == {
            'FOO/n1': {'mode': 'checklist'},
            'FOO/sub/n2': {'mode': 'text'},
            'other': {'mode': 'text'},
        }

    def test_no_match_is_noop(self, notes_dir: Path) -> None:
        nd._save_notes_meta({'note': {'mode': 'text'}})
        nd._rename_folder_meta('bar', 'BAR')
        assert nd._load_notes_meta() == {'note': {'mode': 'text'}}


class TestDeleteFolderMeta:
    def test_deletes_matching_keys(self, notes_dir: Path) -> None:
        nd._save_notes_meta({
            'foo/n1': {},
            'foo/sub/n2': {},
            'other': {},
        })
        nd._delete_folder_meta('foo')
        assert nd._load_notes_meta() == {'other': {}}

    def test_does_not_match_substring(self, notes_dir: Path) -> None:
        nd._save_notes_meta({'foo': {}, 'foobar': {}})
        nd._delete_folder_meta('foo')
        assert nd._load_notes_meta() == {'foobar': {}}


# ===========================================================================
# Note metadata
# ===========================================================================

class TestNoteMetaIO:
    def test_load_when_file_missing(self, notes_dir: Path) -> None:
        assert nd._load_notes_meta() == {}

    def test_load_corrupt_json(self, notes_dir: Path) -> None:
        (notes_dir / '.notes_meta.json').write_text('not json', encoding='utf-8')
        assert nd._load_notes_meta() == {}

    def test_save_round_trip(self, notes_dir: Path) -> None:
        nd._save_notes_meta({'a': {'mode': 'checklist'}})
        assert nd._load_notes_meta() == {'a': {'mode': 'checklist'}}


class TestGetSetMode:
    def test_default_is_text(self, notes_dir: Path) -> None:
        assert nd._get_note_mode('unknown') == 'text'

    def test_set_then_get(self, notes_dir: Path) -> None:
        nd._set_note_mode('n', 'checklist')
        assert nd._get_note_mode('n') == 'checklist'

    def test_set_preserves_other_meta(self, notes_dir: Path) -> None:
        nd._save_notes_meta({'n': {'created_at': 12345}})
        nd._set_note_mode('n', 'checklist')
        meta = nd._load_notes_meta()
        assert meta['n'] == {'created_at': 12345, 'mode': 'checklist'}


class TestRemoveNoteMeta:
    def test_remove_existing(self, notes_dir: Path) -> None:
        nd._save_notes_meta({'a': {'mode': 'text'}, 'b': {'mode': 'text'}})
        nd._remove_note_meta('a')
        assert nd._load_notes_meta() == {'b': {'mode': 'text'}}

    def test_remove_missing_is_noop(self, notes_dir: Path) -> None:
        # No file at all.
        nd._remove_note_meta('a')
        # File should still not exist (no spurious save with empty data).
        assert not (notes_dir / '.notes_meta.json').exists()


class TestRenameNoteMeta:
    def test_rename_existing(self, notes_dir: Path) -> None:
        nd._save_notes_meta({'a': {'mode': 'checklist'}})
        nd._rename_note_meta('a', 'B')
        assert nd._load_notes_meta() == {'B': {'mode': 'checklist'}}

    def test_rename_missing_is_noop(self, notes_dir: Path) -> None:
        nd._save_notes_meta({'b': {'mode': 'text'}})
        nd._rename_note_meta('a', 'A')
        assert nd._load_notes_meta() == {'b': {'mode': 'text'}}


# ===========================================================================
# Misc — RTL, mtime, list_notes, _note_path, created_at
# ===========================================================================

class TestNotePath:
    def test_appends_txt(self, notes_dir: Path) -> None:
        assert nd._note_path('foo') == notes_dir / 'foo.txt'

    def test_supports_subfolder(self, notes_dir: Path) -> None:
        assert nd._note_path('sub/foo') == notes_dir / 'sub' / 'foo.txt'


class TestListNotes:
    def test_empty(self, notes_dir: Path) -> None:
        assert nd._list_notes() == []

    def test_recursive_with_relative_paths(self, notes_dir: Path) -> None:
        (notes_dir / 'a.txt').write_text('', encoding='utf-8')
        (notes_dir / 'sub').mkdir()
        (notes_dir / 'sub' / 'b.txt').write_text('', encoding='utf-8')
        # Returns relative paths without `.txt` suffix.
        assert set(nd._list_notes()) == {'a', 'sub/b'}

    def test_sorted_by_mtime_descending(self, notes_dir: Path) -> None:
        import os
        (notes_dir / 'old.txt').write_text('', encoding='utf-8')
        (notes_dir / 'mid.txt').write_text('', encoding='utf-8')
        (notes_dir / 'new.txt').write_text('', encoding='utf-8')
        os.utime(notes_dir / 'old.txt', (1700000000, 1700000000))
        os.utime(notes_dir / 'mid.txt', (1700000100, 1700000100))
        os.utime(notes_dir / 'new.txt', (1700000200, 1700000200))
        assert nd._list_notes() == ['new', 'mid', 'old']


class TestFormatMtime:
    def test_known_timestamp(self, notes_dir: Path) -> None:
        import os
        p = notes_dir / 'a.txt'
        p.write_text('', encoding='utf-8')
        ts = 1700000000
        os.utime(p, (ts, ts))
        # Match the same fromtimestamp call the helper makes — keeps the
        # test timezone-independent.
        expected = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
        assert nd._format_mtime(p) == expected

    def test_missing_file_returns_empty(self, notes_dir: Path) -> None:
        assert nd._format_mtime(notes_dir / 'missing.txt') == ''


class TestFolderMtime:
    def test_picks_latest_under_folder(self, notes_dir: Path) -> None:
        import os
        (notes_dir / 'sub').mkdir()
        a = notes_dir / 'sub' / 'a.txt'
        b = notes_dir / 'sub' / 'b.txt'
        a.write_text('', encoding='utf-8')
        b.write_text('', encoding='utf-8')
        os.utime(a, (1700000000, 1700000000))
        os.utime(b, (1700000200, 1700000200))
        expected = datetime.fromtimestamp(1700000200).strftime(
            '%Y-%m-%d %H:%M',
        )
        assert nd._folder_mtime('sub') == expected

    def test_empty_folder_returns_empty(self, notes_dir: Path) -> None:
        (notes_dir / 'sub').mkdir()
        assert nd._folder_mtime('sub') == ''

    def test_missing_folder_returns_empty(self, notes_dir: Path) -> None:
        assert nd._folder_mtime('nope') == ''


class TestGetNoteCreatedAt:
    def test_known_value(self, notes_dir: Path) -> None:
        ts = 1700000000
        nd._save_notes_meta({'a': {'created_at': ts}})
        expected = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
        assert nd._get_note_created_at('a') == expected

    def test_unknown_note(self, notes_dir: Path) -> None:
        assert nd._get_note_created_at('a') == ''

    def test_corrupted_value(self, notes_dir: Path) -> None:
        nd._save_notes_meta({'a': {'created_at': 'not-an-int'}})
        assert nd._get_note_created_at('a') == ''


class TestTextIsRtl:
    def test_pure_ltr(self) -> None:
        assert nd._text_is_rtl('hello') is False

    def test_pure_rtl_hebrew(self) -> None:
        assert nd._text_is_rtl('שלום') is True

    def test_pure_rtl_arabic(self) -> None:
        assert nd._text_is_rtl('العربية') is True

    def test_no_letter_returns_none(self) -> None:
        assert nd._text_is_rtl('') is None
        assert nd._text_is_rtl('123') is None
        assert nd._text_is_rtl('   !@#') is None

    def test_skips_leading_spaces_to_first_letter(self) -> None:
        assert nd._text_is_rtl('  שלום') is True
        assert nd._text_is_rtl('  hello') is False

    def test_skips_leading_digits_to_first_letter(self) -> None:
        # Digits do not count as a directional letter — first L/R/AL/AN wins.
        assert nd._text_is_rtl('5 שלום') is True
        assert nd._text_is_rtl('A שלום') is False
