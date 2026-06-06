"""Tests for _send_banner_notifications' coalescing key.

The banner/sound coalescing must not collapse distinct *user* notifications
(review-requested / assigned / mentioned) into a single banner.  Those events
carry tag='' (they aren't tied to a Leap session), so a (tag, type) key would
make every review request after the first silently drop its banner while the
window is inactive.  PR/session events, which DO carry a tag and re-fire every
poll, must still coalesce by (tag, type).

Pure-logic: the mixin method is called on a hand-built fake self (mirroring
test_pr_markers.py) — no window is shown.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from leap.monitor._mixins.pr_display_mixin import PRDisplayMixin
from leap.monitor.ui.dock_badge import NotificationEvent, NotificationType


def _fake_self() -> Any:
    fs = SimpleNamespace(
        _prefs={},           # get_notification_prefs -> defaults (banner=True)
        sessions=[],
        banners=[],          # recorder
    )
    fs.isActiveWindow = lambda: False
    fs._show_status = lambda msg, url=None: None
    fs._format_banner_text = PRDisplayMixin._format_banner_text
    fs._send_macos_notification = (
        lambda subtitle, body, sound_name='None', tag='',
        notif_type=None, has_client=False:
        fs.banners.append((subtitle, body, notif_type))
    )
    fs._play_notification_sound = lambda name: fs.banners.append(('sound', name, None))
    return fs


def _review(url: str, title: str = 'Fix it') -> NotificationEvent:
    return NotificationEvent(
        type=NotificationType.REVIEW_REQUESTED,
        tag='',                      # user notifications are not tied to a tag
        url=url,
        notification_title=title,
        project_name='o/r',
    )


def _send(fs: Any, events: list) -> None:
    PRDisplayMixin._send_banner_notifications(fs, events)


class TestUserNotificationBannerKey:
    def test_distinct_review_requests_each_fire(self) -> None:
        fs = _fake_self()
        _send(fs, [
            _review('https://github.com/o/r/pull/1'),
            _review('https://github.com/o/r/pull/2'),
        ])
        # Both distinct PRs must produce a banner — not coalesced to one.
        assert len(fs.banners) == 2

    def test_same_target_review_requests_coalesce(self) -> None:
        fs = _fake_self()
        url = 'https://github.com/o/r/pull/7'
        _send(fs, [_review(url), _review(url)])
        # Same target + reason -> the coalescing safety net keeps it to one.
        assert len(fs.banners) == 1

    def test_distinct_across_separate_calls_still_fire(self) -> None:
        # Window stays inactive across two poll cycles: a second, different
        # review request must still fire (the first didn't poison the key).
        fs = _fake_self()
        _send(fs, [_review('https://github.com/o/r/pull/1')])
        _send(fs, [_review('https://github.com/o/r/pull/2')])
        assert len(fs.banners) == 2


class TestSessionEventBannerKey:
    def test_repeated_session_event_same_tag_coalesces(self) -> None:
        fs = _fake_self()
        ev1 = NotificationEvent(type=NotificationType.SESSION_COMPLETED, tag='mytag')
        ev2 = NotificationEvent(type=NotificationType.SESSION_COMPLETED, tag='mytag')
        _send(fs, [ev1])
        _send(fs, [ev2])
        # Same (tag, type) re-firing each poll must coalesce to one banner.
        assert len(fs.banners) == 1

    def test_different_tags_each_fire(self) -> None:
        fs = _fake_self()
        _send(fs, [NotificationEvent(type=NotificationType.SESSION_COMPLETED, tag='a')])
        _send(fs, [NotificationEvent(type=NotificationType.SESSION_COMPLETED, tag='b')])
        assert len(fs.banners) == 2
