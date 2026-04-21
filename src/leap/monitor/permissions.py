"""macOS system-permission checks for Leap Monitor.

Thin helpers around the Accessibility and Notifications APIs, plus
utilities to open the matching System Settings pane. Used by the main
window to show a banner when a permission is missing.

All checks return ``True`` optimistically if the underlying API is
unavailable (non-macOS, missing pyobjc bindings, bundle load failure) so
that the banner never shows a false positive on an unsupported system.
"""

import logging
import plistlib
import subprocess
from pathlib import Path
from typing import Optional

try:
    import objc
    from AppKit import NSBundle
    from ApplicationServices import (
        AXIsProcessTrusted, AXIsProcessTrustedWithOptions,
    )
    from CoreFoundation import kCFBooleanTrue
    from Foundation import NSDate, NSRunLoop
    _HAS_COCOA = True
except ImportError:  # pragma: no cover — non-macOS / missing pyobjc
    _HAS_COCOA = False

logger = logging.getLogger(__name__)

_ACCESSIBILITY_SETTINGS_URL = (
    'x-apple.systempreferences:com.apple.preference.security'
    '?Privacy_Accessibility'
)
_NOTIFICATIONS_SETTINGS_URL = (
    'x-apple.systempreferences:com.apple.Notifications-Settings.extension'
)

# UNAuthorizationStatus enum values (stable since macOS 10.14).
_UN_STATUS_NOT_DETERMINED = 0
_UN_STATUS_DENIED = 1
_UN_STATUS_AUTHORIZED = 2
_UN_STATUS_PROVISIONAL = 3
_UN_STATUS_EPHEMERAL = 4

_UN_GRANTED_STATUSES = frozenset({
    _UN_STATUS_AUTHORIZED, _UN_STATUS_PROVISIONAL, _UN_STATUS_EPHEMERAL,
})

_un_center_class: Optional[object] = None
_un_load_attempted = False
_un_metadata_registered = False

# Cache for the last successful async notification-auth result.  Populated
# by ``check_notifications`` callbacks and read on timeout so banner state
# survives a slow or missed PyObjC block dispatch.
_cached_notif_granted: Optional[bool] = None

_NCPREFS_PATH = Path.home() / 'Library' / 'Preferences' / 'com.apple.ncprefs.plist'


def _current_bundle_id() -> Optional[str]:
    """Return the current process's bundle identifier, if any.

    For the installed ``Leap Monitor.app`` this is ``com.leap.monitor``.
    When running from source the main bundle usually has no identifier
    (returns ``None``); in that case the plist lookup is skipped.
    """
    if not _HAS_COCOA:
        return None
    try:
        bundle_id = NSBundle.mainBundle().bundleIdentifier()
    except Exception:
        return None
    return str(bundle_id) if bundle_id else None


# Bit 25 of the per-app ``flags`` entry in ``com.apple.ncprefs.plist``
# mirrors the "Allow Notifications" master toggle in System Settings.
# Empirically verified on macOS 14/15 by diffing the plist before/after
# flipping the toggle for multiple apps (com.leap.monitor, Chrome, Notes):
#   bit 25 set   → master toggle ON, notifications may be delivered
#   bit 25 clear → master toggle OFF or app never authorized
# ``auth`` is NOT a reliable signal — it reflects the historical
# response to the initial prompt (auth=7 means "user clicked Allow
# once") and does not change when the user flips the Settings toggle
# afterwards.  ``UNUserNotificationCenter.requestAuthorization`` has
# the same historical-only behavior.
_NCPREFS_ALLOW_BIT = 0x02000000


def _read_notifications_plist_status(bundle_id: str) -> Optional[bool]:
    """Return the live "Allow Notifications" state from ``ncprefs.plist``.

    This is the authoritative live signal — it's what System Settings
    writes through ``cfprefsd`` whenever the user flips the master
    toggle, and ``usernoted`` re-reads it before delivering anything.

    Returns:
        ``True``  — app is listed and its ``flags`` has the master-toggle
                    bit set.
        ``False`` — app is listed but the master-toggle bit is clear
                    (either explicitly turned off, or never authorized).
        ``None``  — plist missing/unreadable or the bundle id isn't
                    listed (caller should fall back to the UN API).
    """
    if not _NCPREFS_PATH.exists():
        return None
    try:
        with open(_NCPREFS_PATH, 'rb') as f:
            data = plistlib.load(f)
    except Exception as exc:
        logger.debug("Could not read %s: %s", _NCPREFS_PATH, exc)
        return None
    apps = data.get('apps') or []
    for entry in apps:
        if not isinstance(entry, dict):
            continue
        if entry.get('bundle-id') == bundle_id:
            flags = entry.get('flags')
            if not isinstance(flags, int):
                return None
            return bool(flags & _NCPREFS_ALLOW_BIT)
    return None


def _load_user_notifications() -> Optional[object]:
    """Lazily load the UserNotifications framework and return the center class.

    Also registers PyObjC metadata for the two async selectors we call so
    completion blocks are bridged correctly.  Without this, PyObjC raises
    "Argument 2 is a block, but no signature available" and the callback
    never fires.  Cached after the first attempt so repeated checks don't
    reload the bundle.
    """
    global _un_center_class, _un_load_attempted, _un_metadata_registered
    if _un_load_attempted:
        return _un_center_class
    _un_load_attempted = True
    if not _HAS_COCOA:
        return None
    try:
        objc.loadBundle(
            'UserNotifications', globals(),
            '/System/Library/Frameworks/UserNotifications.framework',
        )
        _un_center_class = objc.lookUpClass('UNUserNotificationCenter')
    except Exception as exc:
        logger.debug("Could not load UserNotifications framework: %s", exc)
        _un_center_class = None
        return None

    if not _un_metadata_registered:
        try:
            objc.registerMetaDataForSelector(
                b'UNUserNotificationCenter',
                b'requestAuthorizationWithOptions:completionHandler:',
                {'arguments': {3: {'callable': {
                    'retval': {'type': b'v'},
                    'arguments': {
                        0: {'type': b'^v'},
                        1: {'type': b'Z'},
                        2: {'type': b'@'},
                    },
                }}}},
            )
            objc.registerMetaDataForSelector(
                b'UNUserNotificationCenter',
                b'getNotificationSettingsWithCompletionHandler:',
                {'arguments': {2: {'callable': {
                    'retval': {'type': b'v'},
                    'arguments': {
                        0: {'type': b'^v'},
                        1: {'type': b'@'},
                    },
                }}}},
            )
            _un_metadata_registered = True
        except Exception as exc:
            logger.debug("UN metadata registration failed: %s", exc)
    return _un_center_class


def check_accessibility() -> bool:
    """Return True if this process has macOS Accessibility permission."""
    if not _HAS_COCOA:
        return True
    try:
        return bool(AXIsProcessTrusted())
    except Exception as exc:
        logger.debug("AXIsProcessTrusted failed: %s", exc)
        return True


def check_notifications() -> bool:
    """Return True if notifications are currently allowed for this process.

    The authoritative signal is ``~/Library/Preferences/com.apple.ncprefs.plist``
    — specifically bit 25 of the per-bundle ``flags`` field, which
    mirrors the "Allow Notifications" master toggle in System Settings.
    Unlike ``UNUserNotificationCenter.requestAuthorization`` (which
    only reports the historical response to the first prompt and stays
    "granted" even after the user flips the toggle off), the plist
    reflects live state.

    The UN API is used only as a distant fallback for the exotic case
    where the plist can't be read at all.  Returns ``True``
    optimistically when no signal is available so the banner never
    shows a false positive on an unsupported system.
    """
    bundle_id = _current_bundle_id()
    if bundle_id:
        plist_result = _read_notifications_plist_status(bundle_id)
        if plist_result is not None:
            return plist_result

    # Fallback: UN framework (historical signal, better than nothing if
    # the plist path fails — which doesn't happen on healthy macOS).
    center_cls = _load_user_notifications()
    if center_cls is None:
        return True
    try:
        center = center_cls.currentNotificationCenter()
        done = [False]

        def _on_auth(ok: object, err: object) -> None:
            global _cached_notif_granted  # noqa: PLW0603
            _cached_notif_granted = bool(ok)
            done[0] = True

        center.requestAuthorizationWithOptions_completionHandler_(
            (1 << 0) | (1 << 1) | (1 << 2),  # badge | sound | alert
            _on_auth,
        )

        timeout = 2.0
        step = 0.02
        while not done[0] and timeout > 0:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(step))
            timeout -= step

        if _cached_notif_granted is not None:
            return _cached_notif_granted
        return True
    except Exception as exc:
        logger.debug("Notification UN-fallback check failed: %s", exc)
        if _cached_notif_granted is not None:
            return _cached_notif_granted
        return True


def prompt_accessibility() -> None:
    """Open the Accessibility pane in System Settings.

    Also invokes ``AXIsProcessTrustedWithOptions`` with the prompt flag
    so macOS surfaces its native "add to Accessibility" dialog on top of
    the pane when the process hasn't been added yet.
    """
    if _HAS_COCOA:
        try:
            AXIsProcessTrustedWithOptions(
                {"AXTrustedCheckOptionPrompt": kCFBooleanTrue})
        except Exception as exc:
            logger.debug("AXIsProcessTrustedWithOptions failed: %s", exc)
    subprocess.run(
        ['open', _ACCESSIBILITY_SETTINGS_URL], check=False)


def prompt_notifications() -> None:
    """Nudge the user into granting notification permission.

    The banner button literally says "Open Notifications", so we always
    open the Settings pane — that's the unambiguous right action for
    the ``denied`` state (can't re-prompt via the API) and a reasonable
    action for ``notDetermined`` (the user can see Leap Monitor listed
    and flip the toggle).  In parallel, fire a non-blocking
    ``requestAuthorization`` so the native system prompt *also*
    appears for the ``notDetermined`` case — whichever the user
    interacts with first resolves the state, and the banner updates on
    the next window activation.

    Earlier versions tried to branch on the current status and skip
    opening Settings if the user just clicked Allow, but timing races
    (callback firing before vs after our runloop spin) made the
    behavior inconsistent.  "Always open Settings" is predictable.
    """
    center_cls = _load_user_notifications()
    if center_cls is not None:
        try:
            center = center_cls.currentNotificationCenter()
            # Fire-and-forget — the callback is a no-op; we don't wait
            # for a result.  For notDetermined this surfaces the native
            # system prompt; for determined states it's a cheap no-op
            # that also refreshes the ``_cached_notif_granted`` cache
            # used by ``check_notifications``.
            def _noop(ok: object, err: object) -> None:
                global _cached_notif_granted  # noqa: PLW0603
                _cached_notif_granted = bool(ok)

            center.requestAuthorizationWithOptions_completionHandler_(
                (1 << 0) | (1 << 1) | (1 << 2),  # badge | sound | alert
                _noop,
            )
        except Exception as exc:
            logger.debug("Notification request-authorization failed: %s", exc)

    subprocess.run(
        ['open', _NOTIFICATIONS_SETTINGS_URL], check=False)
