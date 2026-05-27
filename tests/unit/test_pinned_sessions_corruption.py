"""Tests for corruption-tolerance in the ``pinned_sessions.json`` readers.

Two functions read the file outside of ``LeapServer._load_pinned_auto_send_mode``:

* ``leap.server.validation.validate_pinned_session`` — runs at every
  server startup before the snapshot, so a crash here aborts the
  session before it can begin.
* ``leap.monitor.pr_tracking.config.load_pinned_sessions`` — runs once
  at monitor startup; a crash here brings the GUI down.

Both used to catch only ``(json.JSONDecodeError, OSError)`` and were
brittle against:

* non-UTF-8 bytes (``UnicodeDecodeError`` — subclass of ``ValueError``)
* non-dict root (``.get(tag)`` on a list / number)
* non-dict tag entry (``entry.get(...)`` on a string; or, in
  ``load_pinned_sessions``, ``k in session`` raising ``TypeError`` when
  the value is an int)

These tests pin the hardened behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import leap.utils.constants as constants_module
from leap.monitor.pr_tracking import config as pr_config
from leap.server.validation import build_auth_fetch_url, validate_pinned_session
from leap.utils.constants import load_settings


@pytest.fixture
def pinned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Point ``PINNED_SESSIONS_FILE`` at an isolated tmp path."""
    target = tmp_path / 'pinned_sessions.json'
    monkeypatch.setattr(pr_config, 'PINNED_SESSIONS_FILE', target)
    return target


# --------------------------------------------------------------------------
# load_pinned_sessions
# --------------------------------------------------------------------------


class TestUpdatePinnedSessionField:
    """``update_pinned_session_field`` does a targeted read-modify-write so
    a monitor-side toggle of one tag's ``auto_send_mode`` can't silently
    overwrite OTHER tags' recent server-side writes via the monitor's
    stale in-memory cache.

    Without it, the original cross-session leak would re-open through the
    monitor: ``_set_auto_send_mode`` used to call
    ``save_pinned_sessions(self._pinned_sessions)`` which writes the
    WHOLE in-memory map.  If another session's server had just written
    its ``auto_send_mode`` between the last refresh and now, that
    server's write was lost.  Per-session toggles must stay
    per-session — that means PER-TAG WRITES.
    """

    def test_creates_file_when_missing(self, pinned_file: Path) -> None:
        assert not pinned_file.exists()
        pr_config.update_pinned_session_field('mytag', 'auto_send_mode', 'always')
        assert json.loads(pinned_file.read_text()) == {
            'mytag': {'auto_send_mode': 'always'},
        }

    def test_creates_entry_when_tag_missing(self, pinned_file: Path) -> None:
        pinned_file.write_text(json.dumps({
            'othertag': {'project_path': '/x', 'auto_send_mode': 'pause'},
        }))
        pr_config.update_pinned_session_field('mytag', 'auto_send_mode', 'always')
        data = json.loads(pinned_file.read_text())
        assert data['mytag'] == {'auto_send_mode': 'always'}
        assert data['othertag'] == {'project_path': '/x', 'auto_send_mode': 'pause'}

    def test_preserves_other_tags(self, pinned_file: Path) -> None:
        """The core regression guard: toggling tag A must not touch
        tag B's pin entry on disk."""
        pinned_file.write_text(json.dumps({
            'A': {'auto_send_mode': 'pause', 'project_path': '/a'},
            'B': {'auto_send_mode': 'always', 'project_path': '/b', 'branch': 'main'},
        }))
        pr_config.update_pinned_session_field('A', 'auto_send_mode', 'always')
        data = json.loads(pinned_file.read_text())
        assert data['A']['auto_send_mode'] == 'always'
        assert data['B'] == {
            'auto_send_mode': 'always', 'project_path': '/b', 'branch': 'main',
        }

    def test_preserves_other_fields_in_same_entry(
        self, pinned_file: Path,
    ) -> None:
        pinned_file.write_text(json.dumps({
            'mytag': {
                'auto_send_mode': 'pause',
                'project_path': '/x',
                'ide': 'JetBrains',
                'branch': 'main',
            },
        }))
        pr_config.update_pinned_session_field('mytag', 'auto_send_mode', 'always')
        assert json.loads(pinned_file.read_text())['mytag'] == {
            'auto_send_mode': 'always',
            'project_path': '/x',
            'ide': 'JetBrains',
            'branch': 'main',
        }

    def test_noop_when_value_matches(self, pinned_file: Path) -> None:
        pinned_file.write_text(json.dumps({
            'mytag': {'auto_send_mode': 'always', 'extra': 1},
        }))
        original_mtime = pinned_file.stat().st_mtime_ns
        pr_config.update_pinned_session_field('mytag', 'auto_send_mode', 'always')
        assert pinned_file.stat().st_mtime_ns == original_mtime

    def test_invalid_utf8_does_not_raise(self, pinned_file: Path) -> None:
        pinned_file.write_bytes(b'\xff\xfe{"mytag": {}}')
        # Must not raise.
        pr_config.update_pinned_session_field('mytag', 'auto_send_mode', 'always')

    def test_corrupt_json_does_not_raise(self, pinned_file: Path) -> None:
        pinned_file.write_text('{not valid')
        pr_config.update_pinned_session_field('mytag', 'auto_send_mode', 'always')

    def test_non_dict_root_does_not_raise(self, pinned_file: Path) -> None:
        pinned_file.write_text(json.dumps([]))
        pr_config.update_pinned_session_field('mytag', 'auto_send_mode', 'always')

    def test_non_dict_entry_replaced(self, pinned_file: Path) -> None:
        """If the entry is hand-edited to a string, replace it with a
        fresh dict carrying just the field — better than crashing."""
        pinned_file.write_text(json.dumps({
            'mytag': 'not-a-dict',
            'goodtag': {'auto_send_mode': 'pause'},
        }))
        pr_config.update_pinned_session_field('mytag', 'auto_send_mode', 'always')
        data = json.loads(pinned_file.read_text())
        assert data['mytag'] == {'auto_send_mode': 'always'}
        assert data['goodtag'] == {'auto_send_mode': 'pause'}


class TestWritePinnedSessionEntry:
    """``write_pinned_session_entry`` is the targeted upsert used by every
    monitor-side write that isn't a pure field flip.

    The critical invariant: server-owned ``auto_send_mode`` on disk is
    preserved across monitor writes that ship a stale in-memory value.
    Without this, ``_merge_sessions``-style writes could re-open the
    cross-session leak by overwriting a fresh server-side toggle of
    another tag's mode.
    """

    def test_creates_file_when_missing(self, pinned_file: Path) -> None:
        assert not pinned_file.exists()
        pr_config.write_pinned_session_entry('mytag', {
            'tag': 'mytag', 'project_path': '/x',
        })
        assert json.loads(pinned_file.read_text()) == {
            'mytag': {'tag': 'mytag', 'project_path': '/x'},
        }

    def test_creates_entry_when_tag_missing(self, pinned_file: Path) -> None:
        pinned_file.write_text(json.dumps({
            'othertag': {'project_path': '/o', 'auto_send_mode': 'always'},
        }))
        pr_config.write_pinned_session_entry('mytag', {'project_path': '/x'})
        data = json.loads(pinned_file.read_text())
        assert data['mytag'] == {'project_path': '/x'}
        assert data['othertag'] == {
            'project_path': '/o', 'auto_send_mode': 'always',
        }

    def test_replaces_existing_entry(self, pinned_file: Path) -> None:
        pinned_file.write_text(json.dumps({
            'mytag': {'tag': 'mytag', 'project_path': '/x', 'ide': 'Vim'},
        }))
        pr_config.write_pinned_session_entry('mytag', {
            'tag': 'mytag', 'project_path': '/y', 'branch': 'main',
        })
        # Replaced — no 'ide' anymore.
        assert json.loads(pinned_file.read_text())['mytag'] == {
            'tag': 'mytag', 'project_path': '/y', 'branch': 'main',
        }

    def test_preserves_disk_auto_send_mode_over_callers(
        self, pinned_file: Path,
    ) -> None:
        """Core invariant — disk's ``auto_send_mode`` wins over the
        caller's stale in-memory copy.  Without this, a monitor refresh
        that includes a tag's ``pin_data`` carrying an old
        ``auto_send_mode`` would clobber a fresh server-side toggle."""
        pinned_file.write_text(json.dumps({
            'mytag': {'project_path': '/x', 'auto_send_mode': 'always'},
        }))
        pr_config.write_pinned_session_entry('mytag', {
            'tag': 'mytag', 'project_path': '/x',
            'auto_send_mode': 'pause',  # stale!
        })
        # Disk's 'always' won.
        assert json.loads(pinned_file.read_text())['mytag']['auto_send_mode'] == 'always'

    def test_uses_callers_auto_send_mode_if_disk_lacks_one(
        self, pinned_file: Path,
    ) -> None:
        """If disk has no ``auto_send_mode`` yet (fresh entry), accept
        the caller's value so the server's snapshot at session start
        actually reaches disk."""
        pinned_file.write_text(json.dumps({
            'mytag': {'project_path': '/x'},  # no auto_send_mode
        }))
        pr_config.write_pinned_session_entry('mytag', {
            'tag': 'mytag', 'project_path': '/x', 'auto_send_mode': 'always',
        })
        assert json.loads(pinned_file.read_text())['mytag']['auto_send_mode'] == 'always'

    def test_preserves_other_tags_byte_for_byte(
        self, pinned_file: Path,
    ) -> None:
        """The cross-session-leak guard: writing one tag must not touch
        any other tag's pin entry on disk."""
        pinned_file.write_text(json.dumps({
            'A': {'auto_send_mode': 'pause', 'project_path': '/a'},
            'B': {'auto_send_mode': 'always', 'project_path': '/b', 'branch': 'main'},
        }))
        pr_config.write_pinned_session_entry('A', {
            'tag': 'A', 'project_path': '/a-new',
        })
        data = json.loads(pinned_file.read_text())
        # A updated, A's disk auto_send_mode preserved.
        assert data['A'] == {
            'tag': 'A', 'project_path': '/a-new', 'auto_send_mode': 'pause',
        }
        # B untouched.
        assert data['B'] == {
            'auto_send_mode': 'always', 'project_path': '/b', 'branch': 'main',
        }

    def test_invalid_utf8_does_not_raise(self, pinned_file: Path) -> None:
        pinned_file.write_bytes(b'\xff\xfe{"mytag": {}}')
        pr_config.write_pinned_session_entry('mytag', {'project_path': '/x'})

    def test_corrupt_json_does_not_raise(self, pinned_file: Path) -> None:
        pinned_file.write_text('{not valid')
        pr_config.write_pinned_session_entry('mytag', {'project_path': '/x'})

    def test_non_dict_root_does_not_raise(self, pinned_file: Path) -> None:
        pinned_file.write_text(json.dumps([]))
        pr_config.write_pinned_session_entry('mytag', {'project_path': '/x'})

    def test_noop_when_disk_already_matches(
        self, pinned_file: Path,
    ) -> None:
        """No fsync when disk's tag entry already equals the computed
        ``new_entry``.  Important because ``_merge_sessions`` can fire
        this helper many times per refresh; without the short-circuit
        each idle refresh costs one fsync per tag."""
        pinned_file.write_text(json.dumps({
            'mytag': {'tag': 'mytag', 'project_path': '/x', 'auto_send_mode': 'pause'},
        }))
        original_mtime = pinned_file.stat().st_mtime_ns
        # Caller's entry happens to compute to identical new_entry once
        # the helper merges in disk's ``auto_send_mode``.
        pr_config.write_pinned_session_entry('mytag', {
            'tag': 'mytag', 'project_path': '/x', 'auto_send_mode': 'pause',
        })
        assert pinned_file.stat().st_mtime_ns == original_mtime

    def test_recovers_from_corrupt_disk(self, pinned_file: Path) -> None:
        """Pre-fix ``save_pinned_sessions`` overwrote the disk
        unconditionally — a corrupt file would self-heal after one
        write.  Post-fix targeted writes were initially silent on
        corrupt disk, which left the file broken forever (the monitor's
        in-memory state could never reach disk again).  The recovery
        path treats a corrupt file as empty so the next write produces
        a valid JSON file with at least the current tag's entry."""
        pinned_file.write_text('{not valid json')
        pr_config.write_pinned_session_entry('mytag', {
            'tag': 'mytag', 'project_path': '/x',
        })
        # Disk now parses + has our entry.
        assert json.loads(pinned_file.read_text()) == {
            'mytag': {'tag': 'mytag', 'project_path': '/x'},
        }

    def test_recovers_from_invalid_utf8(self, pinned_file: Path) -> None:
        """Same recovery path for ``UnicodeDecodeError`` (also a
        ``ValueError``)."""
        pinned_file.write_bytes(b'\xff\xfe{"mytag": {}}')
        pr_config.write_pinned_session_entry('mytag', {
            'tag': 'mytag', 'project_path': '/x',
        })
        assert json.loads(pinned_file.read_text()) == {
            'mytag': {'tag': 'mytag', 'project_path': '/x'},
        }


class TestRemovePinnedSessionTag:
    """``remove_pinned_session_tag`` is the targeted removal for monitor-
    side deletions (user closed/deleted a row, dead-row cleanup in
    ``_merge_sessions``, etc.)."""

    def test_removes_the_tag(self, pinned_file: Path) -> None:
        pinned_file.write_text(json.dumps({
            'mytag': {'auto_send_mode': 'pause'},
        }))
        pr_config.remove_pinned_session_tag('mytag')
        assert json.loads(pinned_file.read_text()) == {}

    def test_preserves_other_tags(self, pinned_file: Path) -> None:
        pinned_file.write_text(json.dumps({
            'A': {'auto_send_mode': 'pause', 'project_path': '/a'},
            'B': {'auto_send_mode': 'always', 'project_path': '/b'},
        }))
        pr_config.remove_pinned_session_tag('A')
        data = json.loads(pinned_file.read_text())
        assert 'A' not in data
        assert data['B'] == {'auto_send_mode': 'always', 'project_path': '/b'}

    def test_noop_when_file_missing(self, pinned_file: Path) -> None:
        assert not pinned_file.exists()
        pr_config.remove_pinned_session_tag('mytag')
        # Must not have created a file just to remove from it.
        assert not pinned_file.exists()

    def test_noop_when_tag_missing(self, pinned_file: Path) -> None:
        pinned_file.write_text(json.dumps({'other': {'project_path': '/o'}}))
        original_mtime = pinned_file.stat().st_mtime_ns
        pr_config.remove_pinned_session_tag('mytag')
        # Tag wasn't there — no rewrite (mtime stable).
        assert pinned_file.stat().st_mtime_ns == original_mtime

    def test_invalid_utf8_does_not_raise(self, pinned_file: Path) -> None:
        pinned_file.write_bytes(b'\xff\xfe{"mytag": {}}')
        pr_config.remove_pinned_session_tag('mytag')

    def test_corrupt_json_does_not_raise(self, pinned_file: Path) -> None:
        pinned_file.write_text('{not valid')
        pr_config.remove_pinned_session_tag('mytag')

    def test_non_dict_root_does_not_raise(self, pinned_file: Path) -> None:
        pinned_file.write_text(json.dumps([]))
        pr_config.remove_pinned_session_tag('mytag')


class TestLoadPinnedSessions:
    def test_returns_empty_when_file_missing(self, pinned_file: Path) -> None:
        assert pr_config.load_pinned_sessions() == {}

    def test_returns_data_for_valid_file(self, pinned_file: Path) -> None:
        pinned_file.write_text(json.dumps({
            'mytag': {'tag': 'mytag', 'project_path': '/x'},
        }))
        assert pr_config.load_pinned_sessions() == {
            'mytag': {'tag': 'mytag', 'project_path': '/x'},
        }

    def test_invalid_utf8_returns_empty(self, pinned_file: Path) -> None:
        """``UnicodeDecodeError`` is a ``ValueError`` subclass — must
        be caught.  Without this, the monitor crashes at startup on
        any pin file with non-UTF-8 bytes."""
        pinned_file.write_bytes(b'\xff\xfe{"mytag": {}}')
        assert pr_config.load_pinned_sessions() == {}

    def test_corrupt_json_returns_empty(self, pinned_file: Path) -> None:
        pinned_file.write_text('{not valid json')
        assert pr_config.load_pinned_sessions() == {}

    def test_non_dict_root_returns_empty(self, pinned_file: Path) -> None:
        pinned_file.write_text(json.dumps([]))
        assert pr_config.load_pinned_sessions() == {}

    def test_non_dict_entry_does_not_raise_during_migration(
        self, pinned_file: Path,
    ) -> None:
        """``k in session`` raises ``TypeError`` when ``session`` is
        an int (hand-edited or corrupt) — the migration loop must
        skip it rather than crash.  Without the skip, the entire
        load returned ``{}`` because the TypeError was uncaught."""
        pinned_file.write_text(json.dumps({
            'goodtag': {'tag': 'goodtag', 'project_path': '/x'},
            'badtag': 42,
        }))
        result = pr_config.load_pinned_sessions()
        # Both entries are returned — the migration just skipped the bad one.
        assert result['goodtag'] == {'tag': 'goodtag', 'project_path': '/x'}
        assert result['badtag'] == 42

    def test_mr_to_pr_migration_still_runs(self, pinned_file: Path) -> None:
        """Sanity check: the isinstance guard didn't break the existing
        mr→pr key rename path."""
        pinned_file.write_text(json.dumps({
            'mytag': {'tag': 'mytag', 'mr_url': 'https://x', 'mr_tracked': True},
        }))
        result = pr_config.load_pinned_sessions()
        assert result['mytag']['pr_url'] == 'https://x'
        assert result['mytag']['pr_tracked'] is True
        assert 'mr_url' not in result['mytag']


# --------------------------------------------------------------------------
# validate_pinned_session — runs at every server startup
# --------------------------------------------------------------------------


class TestValidatePinnedSession:
    def test_no_file_no_op(self, tmp_path: Path) -> None:
        """No pin file means no validation — just return."""
        # Must not raise, must not sys.exit.
        validate_pinned_session('mytag', tmp_path)

    def test_tag_not_in_pin_no_op(self, tmp_path: Path) -> None:
        (tmp_path / 'pinned_sessions.json').write_text(json.dumps({
            'othertag': {'remote_project_path': 'group/proj'},
        }))
        validate_pinned_session('mytag', tmp_path)

    def test_auto_pinned_row_no_op(self, tmp_path: Path) -> None:
        """Row without ``remote_project_path`` (auto-pinned, not
        PR-pinned) skips validation regardless of repo/branch."""
        (tmp_path / 'pinned_sessions.json').write_text(json.dumps({
            'mytag': {'tag': 'mytag', 'project_path': '/x'},
        }))
        validate_pinned_session('mytag', tmp_path)

    def test_invalid_utf8_no_op(self, tmp_path: Path) -> None:
        """A corrupted pin file must NOT block server startup — fall
        through silently."""
        (tmp_path / 'pinned_sessions.json').write_bytes(
            b'\xff\xfe{"mytag": {}}',
        )
        # Must not raise SystemExit, must not crash.
        validate_pinned_session('mytag', tmp_path)

    def test_corrupt_json_no_op(self, tmp_path: Path) -> None:
        (tmp_path / 'pinned_sessions.json').write_text('{not valid')
        validate_pinned_session('mytag', tmp_path)

    def test_non_dict_root_no_op(self, tmp_path: Path) -> None:
        """Pre-fix: ``pinned_sessions.get(tag)`` raised AttributeError
        on a list root and crashed __init__."""
        (tmp_path / 'pinned_sessions.json').write_text(json.dumps([]))
        validate_pinned_session('mytag', tmp_path)

    def test_non_dict_tag_entry_no_op(self, tmp_path: Path) -> None:
        """Pre-fix: ``entry.get('remote_project_path')`` raised on a
        string entry."""
        (tmp_path / 'pinned_sessions.json').write_text(json.dumps({
            'mytag': 'not-a-dict',
        }))
        validate_pinned_session('mytag', tmp_path)


# --------------------------------------------------------------------------
# load_settings — same critical __init__ path (auto-send mode default
# lookup), and ``{**defaults, **user_settings}`` used to raise TypeError
# when user_settings was a list.  Plus the UnicodeDecodeError gap.
# --------------------------------------------------------------------------


class TestLoadSettings:
    @pytest.fixture
    def settings_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> Path:
        target = tmp_path / 'settings.json'
        monkeypatch.setattr(constants_module, 'SETTINGS_FILE', target)
        return target

    def test_returns_defaults_when_file_missing(
        self, settings_file: Path,
    ) -> None:
        assert load_settings()['auto_send_mode'] == 'pause'

    def test_merges_user_settings_over_defaults(
        self, settings_file: Path,
    ) -> None:
        settings_file.write_text(json.dumps({'auto_send_mode': 'always'}))
        result = load_settings()
        assert result['auto_send_mode'] == 'always'
        # Default keys still present.
        assert 'show_auto_sent_notifications' in result

    def test_invalid_utf8_returns_defaults(self, settings_file: Path) -> None:
        """``UnicodeDecodeError`` on the global settings file used to
        crash server ``__init__`` (called at line 134 to resolve the
        auto-send mode default)."""
        settings_file.write_bytes(b'\xff\xfe{"a": 1}')
        result = load_settings()
        assert result['auto_send_mode'] == 'pause'  # defaults

    def test_corrupt_json_returns_defaults(
        self, settings_file: Path,
    ) -> None:
        settings_file.write_text('{not valid json')
        assert load_settings()['auto_send_mode'] == 'pause'

    def test_non_dict_root_returns_defaults(
        self, settings_file: Path,
    ) -> None:
        """``{**defaults, **user_settings}`` raises TypeError when
        ``user_settings`` is a list — pre-fix, this crashed __init__."""
        settings_file.write_text(json.dumps([1, 2, 3]))
        assert load_settings()['auto_send_mode'] == 'pause'


# --------------------------------------------------------------------------
# build_auth_fetch_url — config-file readers on the __init__ critical
# path via validate_pinned_session.  Used to crash on UnicodeDecodeError
# and on non-string tokens (``quote(int)`` raises TypeError).
# --------------------------------------------------------------------------


class TestBuildAuthFetchUrl:
    def _pinned_gitlab(self) -> dict:
        return {
            'host_url': 'https://gitlab.example.com',
            'remote_project_path': 'group/proj',
            'scm_type': 'gitlab',
        }

    def test_returns_none_when_no_token_file(self, tmp_path: Path) -> None:
        assert build_auth_fetch_url(self._pinned_gitlab(), tmp_path) is None

    def test_returns_url_with_valid_token(self, tmp_path: Path) -> None:
        (tmp_path / 'gitlab_config.json').write_text(json.dumps({
            'private_token': 'glpat-xxx',
        }))
        url = build_auth_fetch_url(self._pinned_gitlab(), tmp_path)
        assert url == (
            'https://oauth2:glpat-xxx@gitlab.example.com/group/proj.git'
        )

    def test_invalid_utf8_returns_none(self, tmp_path: Path) -> None:
        """A corrupt config file must NOT crash ``__init__`` via the
        validator's authenticated-fetch path."""
        (tmp_path / 'gitlab_config.json').write_bytes(b'\xff\xfe{"a": 1}')
        # Must not raise.
        assert build_auth_fetch_url(self._pinned_gitlab(), tmp_path) is None

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / 'gitlab_config.json').write_text('{not valid')
        assert build_auth_fetch_url(self._pinned_gitlab(), tmp_path) is None

    def test_non_dict_config_returns_none(self, tmp_path: Path) -> None:
        """``cfg.get(...)`` on a list root would raise AttributeError."""
        (tmp_path / 'gitlab_config.json').write_text(json.dumps([]))
        assert build_auth_fetch_url(self._pinned_gitlab(), tmp_path) is None

    def test_non_string_token_returns_none(self, tmp_path: Path) -> None:
        """``quote(int)`` raises TypeError.  A hand-edited config with
        a numeric token used to crash; now we treat it as no-token."""
        (tmp_path / 'gitlab_config.json').write_text(json.dumps({
            'private_token': 42,
        }))
        assert build_auth_fetch_url(self._pinned_gitlab(), tmp_path) is None

    def test_empty_token_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / 'gitlab_config.json').write_text(json.dumps({
            'private_token': '',
        }))
        assert build_auth_fetch_url(self._pinned_gitlab(), tmp_path) is None
