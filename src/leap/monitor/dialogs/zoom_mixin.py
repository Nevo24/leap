"""Reusable font-zoom mixin for Leap Monitor dialogs.

Add ``ZoomMixin`` to a QDialog's bases and call ``_init_zoom(pref_key)``
at the end of ``__init__``.  The mixin provides everything automatically
via Python's MRO — no boilerplate overrides needed in the dialog:

* Cmd+Scroll to zoom (wheelEvent)
* Cmd+Plus / Cmd+Minus / Cmd+0 keyboard shortcuts (keyPressEvent)
* Debounced persistence to ``monitor_prefs.json`` under *pref_key*
* Auto-flush on close via done()

For dialogs that close via closeEvent() instead of done() (e.g.
QueueEditDialog), call ``_zoom_flush()`` explicitly in closeEvent().

──────────────────────────────────────────────────────────────────────
Split zoom (content vs. buttons)
──────────────────────────────────────────────────────────────────────

Most dialogs have a primary content area (QTextEdit, QListWidget,
QTreeView, QTableWidget) and secondary chrome (toolbar buttons, combos,
labels).  A user usually wants to enlarge the content without blowing
up the surrounding buttons, and vice versa.

To enable split zoom, pass a second pref key::

    class MyDialog(ZoomMixin, QDialog):
        def __init__(self, ...):
            super().__init__(...)
            self._editor = QTextEdit()
            self._list = QListWidget()
            # ... build UI ...
            self._init_zoom(
                pref_key='my_dialog_font_size',            # buttons/chrome
                content_pref_key='my_dialog_text_font_size',  # content area
                content_widgets=[self._editor, self._list],
            )

Then:
* Cmd+scroll / Cmd+±/0 with the mouse over any of ``content_widgets``
  (or their descendants) adjusts the **content** font.
* Anywhere else → **buttons/chrome** font.

If ``content_widgets`` is a callable (``() -> list[QWidget]``) the
mixin calls it on every event, which is useful when content widgets
are built dynamically (e.g. message cards that rebuild on save).
"""

from typing import Callable, Optional, Sequence, Union

from PyQt5.QtCore import QEvent, QTimer, Qt
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (
    QApplication, QFileDialog, QInputDialog, QMenu, QMessageBox,
    QWidget,
)

from leap.monitor.pr_tracking.config import load_monitor_prefs, save_monitor_prefs
from leap.monitor.themes import current_theme

_MIN_FONT_SIZE = 9
_MAX_FONT_SIZE = 28

# A content-widgets source is either a static list or a callable that
# returns the current list (for dialogs that rebuild content widgets).
_ContentSource = Union[Sequence[QWidget], Callable[[], Sequence[QWidget]]]


class ZoomMixin:
    """Mixin that adds Cmd+scroll / Cmd+± font zoom to any QDialog.

    MRO usage::

        class MyDialog(ZoomMixin, QDialog):
            def __init__(self, ...):
                super().__init__(...)
                # ... build UI ...
                self._init_zoom('my_dialog_font_size')
    """

    def _init_zoom(
        self,
        pref_key: str,
        content_pref_key: Optional[str] = None,
        content_widgets: Optional[_ContentSource] = None,
    ) -> None:
        """Call at the end of __init__ to enable zoom.

        Args:
            pref_key: Monitor-prefs key for the buttons/chrome font size.
            content_pref_key: Optional key for a second ``content`` size.
                If provided, Ctrl+wheel/± over *content_widgets* adjusts
                the content font separately.
            content_widgets: List (or callable returning a list) of
                widgets whose descendants count as "content" for zoom
                routing.  Required iff *content_pref_key* is set.
        """
        self._zoom_pref_key: str = pref_key
        prefs = load_monitor_prefs()
        self._zoom_font_size: int = prefs.get(
            pref_key, current_theme().font_size_base)
        self._apply_zoom_font_size()

        # Optional split: content zoom.
        self._zoom_content_pref_key: Optional[str] = content_pref_key
        self._zoom_content_source: Optional[_ContentSource] = content_widgets
        if content_pref_key:
            self._zoom_content_font_size: int = prefs.get(
                content_pref_key, current_theme().font_size_base)
            self._apply_zoom_content_font_size()

        # Install app-wide event filter so we intercept Ctrl+wheel and
        # Ctrl+±/0 even when a child widget (QTextEdit, QTreeView, …)
        # has focus or is under the cursor.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            # Ensure the filter is removed when the dialog is destroyed,
            # even if done() is never called (e.g. closed via closeEvent).
            # By the time destroyed fires, self's C++ side may be gone —
            # wrap in try/except so RuntimeError doesn't crash the app.
            def _cleanup(*_args, _app=app, _self=self):
                try:
                    _app.removeEventFilter(_self)
                except RuntimeError:
                    pass
            self.destroyed.connect(_cleanup)

    # ── Buttons/chrome font ────────────────────────────────────────

    _ZOOM_MARKER = '/* leap-zoom-buttons */'

    def _apply_zoom_font_size(self) -> None:
        """Apply the current buttons/chrome font size to the dialog.

        Uses both setFont (for widgets that honor parent font inheritance)
        and a stylesheet selector on the dialog itself (for widgets that
        already set their own font via setFont/stylesheet — stylesheet
        rules on a parent still cascade).

        Splits on a marker so external ``setStyleSheet`` calls made after
        ``_init_zoom`` (e.g. to append unrelated styling) are preserved
        across zoom deltas.
        """
        font = self.font()
        font.setPointSize(self._zoom_font_size)
        self.setFont(font)
        # Strip any previous zoom rule from the current stylesheet so we
        # don't accumulate duplicates, then append the fresh rule.
        existing = self.styleSheet() or ''
        base = existing.split(self._ZOOM_MARKER)[0]
        self.setStyleSheet(
            f'{base}\n{self._ZOOM_MARKER}\n'
            f'QLabel, QPushButton, QLineEdit, QTextEdit, QComboBox,'
            f' QCheckBox, QRadioButton, QListWidget, QListView, QTreeWidget,'
            f' QTreeView, QTableWidget, QTableView, QGroupBox, QSpinBox,'
            f' QAbstractSpinBox, QPlainTextEdit, QToolButton'
            f' {{ font-size: {self._zoom_font_size}pt; }}'
        )
        # If this dialog is the active window, update the global QToolTip
        # font so hovering tooltips match the dialog's buttons size.
        if self.isActiveWindow():
            self._zoom_apply_tooltip_font()

    def _zoom_apply_tooltip_font(self) -> None:
        """Set the global tooltip font to match this dialog's zoom.

        Delegates to ``MonitorWindow.set_tooltip_font_size`` so the app
        stylesheet's ``QToolTip`` rule is rebuilt — the theme's
        universal ``* { font-size: 13px }`` otherwise wins over a plain
        ``QToolTip.setFont()``.
        """
        app = QApplication.instance()
        if app is None:
            return
        for w in app.topLevelWidgets():
            cb = getattr(w, 'set_tooltip_font_size', None)
            if callable(cb):
                cb(self._zoom_font_size)
                return

    def _zoom_delta(self, delta: int) -> None:
        """Change buttons font size by *delta* and persist (debounced)."""
        new_size = max(_MIN_FONT_SIZE,
                       min(_MAX_FONT_SIZE, self._zoom_font_size + delta))
        if new_size == self._zoom_font_size:
            return
        self._zoom_font_size = new_size
        self._apply_zoom_font_size()
        if not hasattr(self, '_zoom_save_timer'):
            self._zoom_save_timer = QTimer(self)
            self._zoom_save_timer.setSingleShot(True)
            self._zoom_save_timer.timeout.connect(self._zoom_save)
        self._zoom_save_timer.start(300)

    def _zoom_save(self) -> None:
        """Persist buttons font size to prefs."""
        prefs = load_monitor_prefs()
        prefs[self._zoom_pref_key] = self._zoom_font_size
        save_monitor_prefs(prefs)

    def _zoom_reset(self) -> None:
        """Reset buttons font size to theme default."""
        default = current_theme().font_size_base
        if self._zoom_font_size == default:
            return
        if hasattr(self, '_zoom_save_timer') and self._zoom_save_timer.isActive():
            self._zoom_save_timer.stop()
        self._zoom_font_size = default
        self._apply_zoom_font_size()
        prefs = load_monitor_prefs()
        prefs.pop(self._zoom_pref_key, None)
        save_monitor_prefs(prefs)

    def _zoom_flush(self) -> None:
        """Flush any pending debounced save (buttons + content)."""
        if hasattr(self, '_zoom_save_timer') and self._zoom_save_timer.isActive():
            self._zoom_save_timer.stop()
            self._zoom_save()
        if (hasattr(self, '_zoom_content_save_timer')
                and self._zoom_content_save_timer.isActive()):
            self._zoom_content_save_timer.stop()
            self._zoom_content_save()

    # ── Content font (optional second target) ─────────────────────

    def _zoom_content_widget_list(self) -> Sequence[QWidget]:
        """Resolve the current content widget list (static or callable)."""
        src = self._zoom_content_source
        if src is None:
            return ()
        if callable(src):
            try:
                return src() or ()
            except Exception:
                return ()
        return src

    def _apply_zoom_content_font_size(self) -> None:
        """Apply content font size to every registered content widget.

        Writes a type-selector stylesheet on each content widget so it
        beats the dialog-level buttons stylesheet (which also uses type
        selectors — widget-level stylesheet wins specificity ties).
        """
        pt = self._zoom_content_font_size
        rule = (
            f'/* content-zoom */\n'
            f'QTextEdit, QPlainTextEdit, QListWidget, QListView,'
            f' QTreeWidget, QTreeView, QTableWidget, QTableView,'
            f' QTextBrowser, QLabel'
            f' {{ font-size: {pt}pt; }}'
        )
        for w in self._zoom_content_widget_list():
            if w is None:
                continue
            base = (w.styleSheet() or '').split('/* content-zoom */')[0]
            w.setStyleSheet(base + rule)

    def _zoom_content_delta(self, delta: int) -> None:
        """Change content font size by *delta* and persist (debounced)."""
        if not self._zoom_content_pref_key:
            return
        new_size = max(_MIN_FONT_SIZE,
                       min(_MAX_FONT_SIZE,
                           self._zoom_content_font_size + delta))
        if new_size == self._zoom_content_font_size:
            return
        self._zoom_content_font_size = new_size
        self._apply_zoom_content_font_size()
        if not hasattr(self, '_zoom_content_save_timer'):
            self._zoom_content_save_timer = QTimer(self)
            self._zoom_content_save_timer.setSingleShot(True)
            self._zoom_content_save_timer.timeout.connect(
                self._zoom_content_save)
        self._zoom_content_save_timer.start(300)

    def _zoom_content_save(self) -> None:
        """Persist content font size to prefs."""
        if not self._zoom_content_pref_key:
            return
        prefs = load_monitor_prefs()
        prefs[self._zoom_content_pref_key] = self._zoom_content_font_size
        save_monitor_prefs(prefs)

    def _zoom_content_reset(self) -> None:
        """Reset content font size to theme default."""
        if not self._zoom_content_pref_key:
            return
        default = current_theme().font_size_base
        if self._zoom_content_font_size == default:
            return
        if (hasattr(self, '_zoom_content_save_timer')
                and self._zoom_content_save_timer.isActive()):
            self._zoom_content_save_timer.stop()
        self._zoom_content_font_size = default
        self._apply_zoom_content_font_size()
        prefs = load_monitor_prefs()
        prefs.pop(self._zoom_content_pref_key, None)
        save_monitor_prefs(prefs)

    def _zoom_reapply_content(self) -> None:
        """Call after dynamic content rebuild so new widgets pick up size."""
        if getattr(self, '_zoom_content_pref_key', None):
            self._apply_zoom_content_font_size()

    # ── Event handlers (participate in MRO automatically) ──────────

    def _zoom_owns_widget(self, widget) -> bool:
        """Check if *widget* is inside this dialog and not a built-in popup.

        If *widget* is inside a QMessageBox / QInputDialog / QMenu /
        QFileDialog that happens to be parented to this dialog, we
        let PopupZoomManager handle it (shared popup zoom).
        """
        if widget is None:
            return False
        popup_types = (QMessageBox, QInputDialog, QFileDialog, QMenu)
        w = widget
        while w is not None:
            if isinstance(w, popup_types):
                return False
            if w is self:
                return True
            w = w.parent()
        return False

    def _zoom_obj_is_content(self, widget) -> bool:
        """Return True if *widget* is inside any registered content widget."""
        if widget is None:
            return False
        if not getattr(self, '_zoom_content_pref_key', None):
            return False
        content = list(self._zoom_content_widget_list())
        if not content:
            return False
        w = widget
        while w is not None:
            if w in content:
                return True
            w = w.parent()
        return False

    def eventFilter(self, obj, event):  # type: ignore[override]
        if not hasattr(self, '_zoom_font_size'):
            return super().eventFilter(obj, event)
        etype = event.type()
        # Update the global tooltip font whenever this dialog becomes
        # the active window, so hover tooltips match its buttons size.
        if etype == QEvent.WindowActivate and obj is self:
            self._zoom_apply_tooltip_font()
        if etype == QEvent.Wheel:
            if event.modifiers() & Qt.ControlModifier:
                # Use the widget under the mouse cursor — Qt sometimes
                # delivers wheel events to the focused widget instead of
                # the one under the pointer (macOS Qt in particular),
                # which would otherwise route the zoom to the wrong
                # target.  Notes uses the same trick.  Fall back to
                # the event's obj if widgetAt returns None (headless /
                # offscreen test platforms have no real cursor).
                target = QApplication.widgetAt(QCursor.pos()) or obj
                if self._zoom_owns_widget(target):
                    delta = 1 if event.angleDelta().y() > 0 else -1
                    if self._zoom_obj_is_content(target):
                        self._zoom_content_delta(delta)
                    else:
                        self._zoom_delta(delta)
                    return True
        elif etype == QEvent.KeyPress:
            if event.modifiers() & Qt.ControlModifier:
                # Route by mouse position so keyboard and wheel share the
                # same target-selection rule.  Keys dispatched to focus
                # can never reach the "buttons" target (no button has
                # focus during zoom) — using mouse position fixes that.
                target = QApplication.widgetAt(QCursor.pos()) or obj
                if self._zoom_owns_widget(target):
                    key = event.key()
                    is_content = self._zoom_obj_is_content(target)
                    if key in (Qt.Key_Equal, Qt.Key_Plus):
                        (self._zoom_content_delta if is_content
                         else self._zoom_delta)(1)
                        return True
                    if key == Qt.Key_Minus:
                        (self._zoom_content_delta if is_content
                         else self._zoom_delta)(-1)
                        return True
                    if key == Qt.Key_0:
                        (self._zoom_content_reset if is_content
                         else self._zoom_reset)()
                        return True
        return super().eventFilter(obj, event)

    def done(self, result: int) -> None:
        """Flush zoom before the dialog closes."""
        if hasattr(self, '_zoom_font_size'):
            self._zoom_flush()
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
        super().done(result)
