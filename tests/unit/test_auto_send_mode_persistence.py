"""Tests for ``LeapServer._save_pinned_auto_send_mode`` + the no-global
contract around the ``set_auto_send_mode`` socket handler.

These pin the behavior introduced to fix the cross-session leak where
toggling auto-send on one tag was clobbering ``settings.json`` globally
(via the server handler) — which then bled into every other open
session's Claude ``PermissionRequest`` hook via the hook's per-tag → global
fallback (`_resolve_auto_send_mode` in ``leap-hook-process.py``).

The contract the tests enforce:

* ``_save_pinned_auto_send_mode`` creates the pin file / entry if missing
  so the per-session value isn't silently dropped when the monitor isn't
  running.
* It preserves unrelated fields in the entry (project_path, ide, …) so
  the monitor's data isn't clobbered.
* It is a no-op when the mode is already set to the requested value
  (avoids gratuitous writes on every server startup snapshot).
* The server module no longer imports ``save_settings`` — the only legit
  caller of the global write (the Settings dialog) lives in the monitor.
  This is the regression guard against re-introducing the leak.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import leap.server.server as server_module
import leap.utils.constants as constants_module
from leap.cli_providers.states import CLIState
from leap.server.server import LeapServer


# --------------------------------------------------------------------------
# Fixture: isolated STORAGE_DIR so we never touch the real .storage
# --------------------------------------------------------------------------


@pytest.fixture
def storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Point both the server module's STORAGE_DIR (used by the
    pinned-sessions helpers) and the constants module's SETTINGS_FILE
    (used by ``load_settings`` / ``save_settings`` via module-level
    name lookup) at an isolated tmp dir.

    Redirecting **both** matters for ``TestNoGlobalLeakFromHandler`` —
    without the SETTINGS_FILE redirect, a regression that re-introduces
    ``save_settings(...)`` in the ``set_auto_send_mode`` handler would
    quietly write to the developer's real ``.storage/settings.json``
    instead of tmp_path, and the test would still pass (false negative).
    """
    monkeypatch.setattr(server_module, 'STORAGE_DIR', tmp_path)
    monkeypatch.setattr(
        constants_module, 'SETTINGS_FILE', tmp_path / 'settings.json',
    )
    return tmp_path


def _read_pinned(storage_dir: Path) -> dict:
    """Read pinned_sessions.json or return {} if missing/empty."""
    pinned_file = storage_dir / 'pinned_sessions.json'
    if not pinned_file.exists():
        return {}
    return json.loads(pinned_file.read_text())


# --------------------------------------------------------------------------
# _save_pinned_auto_send_mode — happy paths + edge cases
# --------------------------------------------------------------------------


class TestSavePinnedAutoSendMode:
    def test_creates_file_when_missing(self, storage: Path) -> None:
        """Server runs before monitor — no pinned file yet.  The save
        must still persist the value, otherwise the snapshot-at-startup
        would be a no-op and the hook would fall back to global."""
        assert not (storage / 'pinned_sessions.json').exists()
        LeapServer._save_pinned_auto_send_mode('mytag', 'always')
        assert _read_pinned(storage) == {
            'mytag': {'auto_send_mode': 'always'},
        }

    def test_creates_entry_when_tag_missing(self, storage: Path) -> None:
        """Pinned file exists but this tag isn't in it (CLI-only usage
        before the monitor has seen this session).  Don't silently drop
        the value."""
        (storage / 'pinned_sessions.json').write_text(
            json.dumps({'othertag': {'project_path': '/x'}}),
        )
        LeapServer._save_pinned_auto_send_mode('mytag', 'always')
        pinned = _read_pinned(storage)
        assert pinned['mytag'] == {'auto_send_mode': 'always'}
        # And the other tag must remain untouched.
        assert pinned['othertag'] == {'project_path': '/x'}

    def test_preserves_other_fields_in_entry(self, storage: Path) -> None:
        """Monitor populates project_path, ide, branch, … in the same
        entry.  Server-side mode persistence must not clobber them."""
        (storage / 'pinned_sessions.json').write_text(
            json.dumps({
                'mytag': {
                    'tag': 'mytag',
                    'project_path': '/Users/x/proj',
                    'ide': 'JetBrains',
                    'branch': 'main',
                    'cli_provider': 'claude',
                },
            }),
        )
        LeapServer._save_pinned_auto_send_mode('mytag', 'always')
        assert _read_pinned(storage)['mytag'] == {
            'tag': 'mytag',
            'project_path': '/Users/x/proj',
            'ide': 'JetBrains',
            'branch': 'main',
            'cli_provider': 'claude',
            'auto_send_mode': 'always',
        }

    def test_noop_when_mode_already_matches(self, storage: Path) -> None:
        """No write when the persisted mode equals the requested mode.
        Avoids re-writing pinned_sessions.json on every server startup
        snapshot — keeps mtime stable and dodges write/read races with
        the monitor's ``_merge_sessions`` refresh loop."""
        pinned_file = storage / 'pinned_sessions.json'
        pinned_file.write_text(json.dumps({
            'mytag': {'auto_send_mode': 'always', 'extra': 1},
        }))
        original_mtime = pinned_file.stat().st_mtime_ns
        LeapServer._save_pinned_auto_send_mode('mytag', 'always')
        assert pinned_file.stat().st_mtime_ns == original_mtime

    def test_overwrites_when_mode_differs(self, storage: Path) -> None:
        (storage / 'pinned_sessions.json').write_text(json.dumps({
            'mytag': {'auto_send_mode': 'pause', 'extra': 1},
        }))
        LeapServer._save_pinned_auto_send_mode('mytag', 'always')
        assert _read_pinned(storage)['mytag'] == {
            'auto_send_mode': 'always',
            'extra': 1,
        }

    def test_non_dict_entry_is_replaced_with_dict(self, storage: Path) -> None:
        """Hand-edited or corrupt entry shape — replace with a fresh
        dict carrying just the mode, don't raise."""
        (storage / 'pinned_sessions.json').write_text(json.dumps({
            'mytag': 'not-a-dict',
        }))
        LeapServer._save_pinned_auto_send_mode('mytag', 'always')
        assert _read_pinned(storage)['mytag'] == {'auto_send_mode': 'always'}

    def test_non_dict_root_does_not_raise(self, storage: Path) -> None:
        """Corrupt pinned file with a list at root — start from an
        empty dict rather than crashing the server boot path."""
        (storage / 'pinned_sessions.json').write_text(json.dumps([]))
        LeapServer._save_pinned_auto_send_mode('mytag', 'always')
        assert _read_pinned(storage) == {
            'mytag': {'auto_send_mode': 'always'},
        }

    def test_corrupt_json_does_not_raise(self, storage: Path) -> None:
        """Half-written pinned file — the save MUST fail silently
        rather than crashing the server, because the server calls this
        in its ``__init__`` snapshot path."""
        (storage / 'pinned_sessions.json').write_text('{not valid json')
        # Must not raise.
        LeapServer._save_pinned_auto_send_mode('mytag', 'always')

    def test_invalid_utf8_does_not_raise(self, storage: Path) -> None:
        """A pin file with non-UTF-8 bytes (corrupted disk write, partial
        hex edit, …) raises ``UnicodeDecodeError``, which is a subclass
        of ``ValueError``.  ``__init__``'s snapshot calls this — a crash
        here would block session startup.  Must catch and skip."""
        (storage / 'pinned_sessions.json').write_bytes(b'\xff\xfe{"a": 1}')
        # Must not raise.
        LeapServer._save_pinned_auto_send_mode('mytag', 'always')


# --------------------------------------------------------------------------
# Regression guard: the server's set_auto_send_mode handler MUST NOT
# write to the global settings.json.  The leak that caused "I changed
# one session and all my sessions auto-approved" was exactly this:
# the handler called save_settings() on every per-session toggle, and
# the Claude PermissionRequest hook's per-tag → global fallback meant
# every other session inherited the toggle silently.
# --------------------------------------------------------------------------


class TestNoGlobalLeakFromHandler:
    def test_save_settings_not_imported_by_server(self) -> None:
        """If ``save_settings`` re-appears in the server module's
        namespace, someone has likely re-introduced a global write —
        and the leak is back.  This is a deliberately blunt structural
        check that catches direct ``from X import save_settings`` and
        ``from X import *``.  It can be defeated by ``import X as Y``
        + ``Y.save_settings(...)`` — the behavioural test below covers
        that, but this check fails fast at import time so the regression
        is loud."""
        assert not hasattr(server_module, 'save_settings'), (
            'leap.server.server.save_settings must stay absent — '
            'per-session toggles must NOT write the global default. '
            'See the auto_send_mode cross-session leak fix.'
        )

    def test_handler_does_not_modify_settings_file(
        self, storage: Path,
    ) -> None:
        """Behavioural regression guard — calls the actual
        ``_handle_message`` branch for ``set_auto_send_mode`` and
        verifies ``settings.json`` is byte-identical afterward.  Catches
        any future re-introduction of a global write (via aliased
        imports, module-level globals, or other paths the structural
        test can't see)."""
        settings_file = storage / 'settings.json'
        before = json.dumps({'auto_send_mode': 'pause', 'other': 42})
        settings_file.write_text(before)

        srv = object.__new__(LeapServer)
        srv.state = MagicMock()
        srv.state.auto_send_mode = 'pause'
        srv.state.current_state = CLIState.IDLE  # skip _try_auto_approve
        srv.tag = 'mytag'
        # Make sure the pin file exists so _save's exists() branch fires.
        (storage / 'pinned_sessions.json').write_text(json.dumps({}))

        response = srv._handle_message({
            'type': 'set_auto_send_mode',
            'mode': 'always',
        })
        assert response == {'status': 'ok', 'auto_send_mode': 'always'}
        # In-memory updated, pin written, GLOBAL UNCHANGED.
        assert srv.state.auto_send_mode == 'always'
        assert settings_file.read_text() == before
        # And the pin got the new mode.
        pinned = json.loads((storage / 'pinned_sessions.json').read_text())
        assert pinned['mytag']['auto_send_mode'] == 'always'

    def test_handler_with_no_settings_file_does_not_create_one(
        self, storage: Path,
    ) -> None:
        """Settings file shouldn't be auto-created by a per-session
        toggle either — even when it doesn't exist."""
        settings_file = storage / 'settings.json'
        assert not settings_file.exists()

        srv = object.__new__(LeapServer)
        srv.state = MagicMock()
        srv.state.auto_send_mode = 'pause'
        srv.state.current_state = CLIState.IDLE
        srv.tag = 'mytag'

        srv._handle_message({'type': 'set_auto_send_mode', 'mode': 'always'})
        assert not settings_file.exists()

    def test_settings_file_redirect_is_wired_up(
        self, storage: Path,
    ) -> None:
        """Sanity check on the fixture itself — if ``save_settings``
        WERE called, would it land in tmp_path (caught by the previous
        test) or in the developer's real ``.storage/settings.json``
        (false negative)?  This test verifies the redirect works by
        actually calling ``save_settings`` and asserting tmp_path
        received the write.  Without this, the previous two tests are
        only as trustworthy as the fixture they depend on."""
        from leap.utils.constants import save_settings
        settings_file = storage / 'settings.json'
        save_settings({'auto_send_mode': 'always'})
        assert settings_file.exists()
        assert json.loads(settings_file.read_text()) == {
            'auto_send_mode': 'always',
        }


# --------------------------------------------------------------------------
# _load_pinned_auto_send_mode — must be robust to corrupt files because
# it sits on the server's __init__ snapshot path: a crash here would
# block session startup, not just silently return the default.
# --------------------------------------------------------------------------


class TestLoadPinnedAutoSendMode:
    def test_returns_pinned_value(self, storage: Path) -> None:
        (storage / 'pinned_sessions.json').write_text(json.dumps({
            'mytag': {'auto_send_mode': 'always'},
        }))
        assert LeapServer._load_pinned_auto_send_mode('mytag', 'pause') == 'always'

    def test_returns_default_when_tag_missing(self, storage: Path) -> None:
        (storage / 'pinned_sessions.json').write_text(json.dumps({
            'othertag': {'auto_send_mode': 'always'},
        }))
        assert LeapServer._load_pinned_auto_send_mode('mytag', 'pause') == 'pause'

    def test_returns_default_when_file_missing(self, storage: Path) -> None:
        assert LeapServer._load_pinned_auto_send_mode('mytag', 'pause') == 'pause'

    def test_returns_default_on_corrupt_json(self, storage: Path) -> None:
        (storage / 'pinned_sessions.json').write_text('{not valid json')
        assert LeapServer._load_pinned_auto_send_mode('mytag', 'pause') == 'pause'

    def test_returns_default_on_non_dict_root(self, storage: Path) -> None:
        """List at root used to raise AttributeError on ``.get(tag)``,
        which would have crashed ``__init__``'s snapshot call."""
        (storage / 'pinned_sessions.json').write_text(json.dumps([]))
        assert LeapServer._load_pinned_auto_send_mode('mytag', 'pause') == 'pause'

    def test_returns_default_on_non_dict_entry(self, storage: Path) -> None:
        """Hand-edited entry with a string value — used to raise."""
        (storage / 'pinned_sessions.json').write_text(json.dumps({
            'mytag': 'not-a-dict',
        }))
        assert LeapServer._load_pinned_auto_send_mode('mytag', 'pause') == 'pause'

    def test_returns_default_on_non_string_mode(self, storage: Path) -> None:
        """``auto_send_mode`` should never be a non-string in production,
        but if a future schema migration goes wrong, fall back rather
        than propagating a weird value into ``CLIStateTracker``."""
        (storage / 'pinned_sessions.json').write_text(json.dumps({
            'mytag': {'auto_send_mode': 42},
        }))
        assert LeapServer._load_pinned_auto_send_mode('mytag', 'pause') == 'pause'

    def test_returns_default_on_empty_string_mode(self, storage: Path) -> None:
        (storage / 'pinned_sessions.json').write_text(json.dumps({
            'mytag': {'auto_send_mode': ''},
        }))
        assert LeapServer._load_pinned_auto_send_mode('mytag', 'pause') == 'pause'

    def test_returns_default_on_invalid_utf8(self, storage: Path) -> None:
        """``UnicodeDecodeError`` is a ``ValueError`` subclass — the
        except clause must catch it or the snapshot path crashes
        ``__init__``."""
        (storage / 'pinned_sessions.json').write_bytes(b'\xff\xfe{"a": 1}')
        assert LeapServer._load_pinned_auto_send_mode('mytag', 'pause') == 'pause'


# --------------------------------------------------------------------------
# Round-trip — what _save writes, _load reads back.  Pins the symmetry
# between the two halves, so the server's __init__ snapshot path
# (``_save_pinned_auto_send_mode(tag, _load_pinned_auto_send_mode(...))``)
# is genuinely a no-op when the file already has the resolved value.
# --------------------------------------------------------------------------


class TestRoundTrip:
    def test_save_then_load(self, storage: Path) -> None:
        LeapServer._save_pinned_auto_send_mode('mytag', 'always')
        assert LeapServer._load_pinned_auto_send_mode('mytag', 'pause') == 'always'

    def test_save_then_load_preserves_through_overwrite(
        self, storage: Path,
    ) -> None:
        LeapServer._save_pinned_auto_send_mode('mytag', 'always')
        LeapServer._save_pinned_auto_send_mode('mytag', 'pause')
        assert LeapServer._load_pinned_auto_send_mode('mytag', 'always') == 'pause'
