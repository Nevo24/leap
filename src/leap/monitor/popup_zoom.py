"""Shared zoom for built-in Qt popups (QMessageBox, QInputDialog, QMenu, ...).

One pref ``popup_font_size`` in ``monitor_prefs.json`` controls the font
size of every built-in popup shown by the app.  Users Cmd+scroll or
Cmd+±/0 over a popup to adjust; the new size applies immediately and
to every popup shown from then on.

The manager installs an app-wide ``QApplication`` event filter so it
can intercept wheel/key events that originate inside popup widgets,
regardless of which window spawned them.
"""

from typing import Optional

from PyQt5.QtCore import QEvent, QObject, QTimer, Qt
from PyQt5.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QMenu, QMessageBox, QWidget,
)

from leap.monitor.pr_tracking.config import load_monitor_prefs, save_monitor_prefs
from leap.monitor.themes import current_theme

_MIN = 9
_MAX = 28

_POPUP_TYPES = (QMessageBox, QInputDialog, QFileDialog, QMenu)


def _is_inside_popup(widget: Optional[QWidget]) -> bool:
    """Return True if *widget* (or an ancestor) is a built-in popup type."""
    w = widget
    while w is not None:
        if isinstance(w, _POPUP_TYPES):
            return True
        w = w.parent() if isinstance(w, QObject) else None
    return False


class PopupZoomManager(QObject):
    """Singleton manager for popup font zoom (installs app event filter)."""

    _instance: Optional['PopupZoomManager'] = None

    @classmethod
    def instance(cls) -> 'PopupZoomManager':
        if cls._instance is None:
            cls._instance = PopupZoomManager()
        return cls._instance

    def __init__(self) -> None:
        super().__init__()
        prefs = load_monitor_prefs()
        self._size: int = prefs.get(
            'popup_font_size', current_theme().font_size_base)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    @property
    def font_size(self) -> int:
        return self._size

    def popup_stylesheet_rule(self) -> str:
        """Return the QSS rule that applies the current size to popup widgets.

        Appended to the app-level stylesheet by ``MonitorWindow._apply_theme``
        so it stays last and wins specificity ties.  Uses ``pt`` to match
        the ZoomMixin unit so popups visually align with zoomed dialogs.

        ``QToolTip`` is intentionally *not* in this rule — tooltips track
        the currently-active window's font via ``QToolTip.setFont()``
        (see ``MonitorWindow`` / ``ZoomMixin`` activation handlers).  If
        we included it here the app QSS would override the per-window
        setFont and tooltips would always be at ``popup_font_size``.

        Also covers custom popups by objectName (e.g.
        ``#leapIndicatorPopup`` from ``ui_widgets.IndicatorPopup``).
        """
        pt = self._size
        return (
            f'\n/* popup zoom */\n'
            f'QMessageBox, QMessageBox QLabel, QMessageBox QPushButton,'
            f' QInputDialog, QInputDialog QLabel, QInputDialog QLineEdit,'
            f' QInputDialog QPushButton,'
            f' QFileDialog, QFileDialog QLabel,'
            f' QMenu, QMenu::item,'
            f' QLabel#leapIndicatorPopup'
            f' {{ font-size: {pt}pt; }}\n'
        )

    def _reapply_to_app(self) -> None:
        """Trigger a re-apply of the theme stylesheet so our rule updates."""
        # MonitorWindow owns the theme; ask it to re-run _apply_theme.
        # We use a soft import path to avoid a hard dependency cycle.
        app = QApplication.instance()
        if app is None:
            return
        for w in app.topLevelWidgets():
            cb = getattr(w, '_reapply_theme_stylesheet', None)
            if callable(cb):
                cb()
                return

    def _delta(self, d: int) -> None:
        new = max(_MIN, min(_MAX, self._size + d))
        if new == self._size:
            return
        self._size = new
        self._reapply_to_app()
        self._save_timer.start(300)

    def _reset(self) -> None:
        default = current_theme().font_size_base
        if self._size == default:
            return
        self._size = default
        self._reapply_to_app()
        self._save_timer.start(300)

    def _save(self) -> None:
        prefs = load_monitor_prefs()
        prefs['popup_font_size'] = self._size
        save_monitor_prefs(prefs)

    def eventFilter(self, obj, event):
        etype = event.type()
        if etype == QEvent.Wheel:
            if (event.modifiers() & Qt.ControlModifier
                    and _is_inside_popup(obj if isinstance(obj, QWidget) else None)):
                delta = 1 if event.angleDelta().y() > 0 else -1
                self._delta(delta)
                return True
        elif etype == QEvent.KeyPress:
            if (event.modifiers() & Qt.ControlModifier
                    and _is_inside_popup(obj if isinstance(obj, QWidget) else None)):
                key = event.key()
                if key in (Qt.Key_Equal, Qt.Key_Plus):
                    self._delta(1)
                    return True
                if key == Qt.Key_Minus:
                    self._delta(-1)
                    return True
                if key == Qt.Key_0:
                    self._reset()
                    return True
        return super().eventFilter(obj, event)
