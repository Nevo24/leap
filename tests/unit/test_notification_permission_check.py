"""Tests for the notification-permission signal logic in permissions.py.

macOS 26 removed ``com.apple.ncprefs.plist`` (the previously
authoritative live "Allow Notifications" signal), so
``check_notifications`` now falls back to
``UNNotificationSettings.authorizationStatus`` before the historical-only
``requestAuthorization`` last resort.  These pin the status mapping, the
plist reader's tri-state contract, and the signal priority.
"""

import plistlib
from pathlib import Path
from typing import Any, Optional

from leap.monitor import permissions
from leap.monitor.permissions import (
    _NCPREFS_ALLOW_BIT,
    _UN_STATUS_AUTHORIZED,
    _UN_STATUS_DENIED,
    _UN_STATUS_EPHEMERAL,
    _UN_STATUS_NOT_DETERMINED,
    _UN_STATUS_PROVISIONAL,
    _read_notifications_plist_status,
    _status_allows_banners,
    check_notifications,
)

_BUNDLE = 'com.leap.monitor'


class TestStatusAllowsBanners:
    def test_denied_blocks(self) -> None:
        assert _status_allows_banners(_UN_STATUS_DENIED) is False

    def test_granted_statuses_allow(self) -> None:
        for status in (_UN_STATUS_AUTHORIZED, _UN_STATUS_PROVISIONAL,
                       _UN_STATUS_EPHEMERAL):
            assert _status_allows_banners(status) is True

    def test_not_determined_is_optimistic(self) -> None:
        # Pre-first-prompt state must not flash the warning banner.
        assert _status_allows_banners(_UN_STATUS_NOT_DETERMINED) is True

    def test_unknown_future_status_is_optimistic(self) -> None:
        assert _status_allows_banners(99) is True


class TestPlistReader:
    def _write_plist(self, tmp_path: Path, apps: list) -> Path:
        path = tmp_path / 'ncprefs.plist'
        with open(path, 'wb') as f:
            plistlib.dump({'apps': apps}, f)
        return path

    def test_missing_plist_returns_none(self, tmp_path: Path,
                                        monkeypatch: Any) -> None:
        # The macOS 26 case: the plist no longer exists at all.
        monkeypatch.setattr(permissions, '_NCPREFS_PATH',
                            tmp_path / 'does-not-exist.plist')
        assert _read_notifications_plist_status(_BUNDLE) is None

    def test_unlisted_bundle_returns_none(self, tmp_path: Path,
                                          monkeypatch: Any) -> None:
        path = self._write_plist(
            tmp_path, [{'bundle-id': 'com.other.app', 'flags': 0}])
        monkeypatch.setattr(permissions, '_NCPREFS_PATH', path)
        assert _read_notifications_plist_status(_BUNDLE) is None

    def test_toggle_on_returns_true(self, tmp_path: Path,
                                    monkeypatch: Any) -> None:
        path = self._write_plist(
            tmp_path, [{'bundle-id': _BUNDLE, 'flags': _NCPREFS_ALLOW_BIT}])
        monkeypatch.setattr(permissions, '_NCPREFS_PATH', path)
        assert _read_notifications_plist_status(_BUNDLE) is True

    def test_toggle_off_returns_false(self, tmp_path: Path,
                                      monkeypatch: Any) -> None:
        path = self._write_plist(
            tmp_path, [{'bundle-id': _BUNDLE, 'flags': 0}])
        monkeypatch.setattr(permissions, '_NCPREFS_PATH', path)
        assert _read_notifications_plist_status(_BUNDLE) is False


class TestCheckNotificationsPriority:
    """Signal priority: plist > live UN settings > historical fallback."""

    def _patch(self, monkeypatch: Any, bundle: Optional[str],
               plist: Optional[bool], un_status: Optional[int]) -> None:
        monkeypatch.setattr(permissions, '_current_bundle_id',
                            lambda: bundle)
        monkeypatch.setattr(permissions, '_read_notifications_plist_status',
                            lambda b: plist)
        monkeypatch.setattr(permissions, '_read_un_settings_status',
                            lambda: un_status)

    def test_plist_wins_over_un_settings(self, monkeypatch: Any) -> None:
        self._patch(monkeypatch, _BUNDLE, plist=False,
                    un_status=_UN_STATUS_AUTHORIZED)
        assert check_notifications() is False

    def test_un_settings_used_when_plist_missing(self,
                                                 monkeypatch: Any) -> None:
        # The macOS 26 path: plist gone, live UN settings say denied.
        self._patch(monkeypatch, _BUNDLE, plist=None,
                    un_status=_UN_STATUS_DENIED)
        assert check_notifications() is False

    def test_un_settings_authorized_when_plist_missing(
            self, monkeypatch: Any) -> None:
        self._patch(monkeypatch, _BUNDLE, plist=None,
                    un_status=_UN_STATUS_AUTHORIZED)
        assert check_notifications() is True

    def test_denied_detected_without_bundle_id(self,
                                               monkeypatch: Any) -> None:
        # Source runs have no bundle id; the UN signal still applies.
        self._patch(monkeypatch, None, plist=True,
                    un_status=_UN_STATUS_DENIED)
        assert check_notifications() is False

    def test_optimistic_when_no_signal_available(self,
                                                 monkeypatch: Any) -> None:
        self._patch(monkeypatch, None, plist=None, un_status=None)
        # Kill the requestAuthorization last resort too.
        monkeypatch.setattr(permissions, '_load_user_notifications',
                            lambda: None)
        monkeypatch.setattr(permissions, '_cached_notif_granted', None)
        assert check_notifications() is True
