"""Qt helper widgets for monitor table display.

Contains separator delegates, header views, tooltip overrides, and
column-group boundary constants extracted from app.py.
"""

from typing import Any

import sip
from PyQt5.QtWidgets import (
    QApplication, QHeaderView, QProxyStyle, QStyle, QStyledItemDelegate,
    QWidget,
)
from PyQt5.QtCore import QEvent, QModelIndex, QObject, Qt
from PyQt5.QtGui import QColor, QPen


# Template UI strings (single source of truth for labels, tooltips, hints).
MR_TEMPLATE_LABEL = 'MR thread context:'
MR_TEMPLATE_TOOLTIP = 'Context attached to every MR thread message sent to CQ (single-message only)'
MR_TEMPLATE_HINT = (
    'MR context: This preset is attached to every MR thread message sent to CQ. '
    'Only single-message presets can be used.'
)

QUICK_MSG_TEMPLATE_LABEL = 'Message bundle:'
QUICK_MSG_TEMPLATE_TOOLTIP = 'Preset messages sent via Queue column send button'
QUICK_MSG_TEMPLATE_HINT = (
    'Message bundle: These preset messages are sent as standalone messages '
    '(via the send button in the Queue column).'
)

QUICK_MSG_SEND_NEXT = 'Send message-bundle next'
QUICK_MSG_SEND_AT_END = 'Send message-bundle to end'

APPLY_MR_BTN = 'Apply to MR Context && Close'
APPLY_QUICK_MSG_BTN = 'Apply to Message Bundle && Close'

# Max characters shown in template combo items before truncation with ellipsis
MAX_COMBO_DISPLAY = 40

# Reusable stylesheet constants for cell buttons
CLOSE_BTN_STYLE = (
    'QPushButton { color: #999; font-size: 11px; padding: 0; }'
    'QPushButton:hover { color: #ff4444; font-weight: bold; }'
)
ACTIVE_BTN_STYLE = (
    'QPushButton { color: #00ff00; } '
    'QToolTip { color: #e0e0e0; }'
)

# Column group boundaries for vertical separators.
# Groups: [X, Tag, Project] | [Server, Path, ServerBranch, Status, Queue] | [Client] | [Slack] | [MR, MRBranch]
GROUP_BOUNDARY_COLS = frozenset({0, 2, 7, 8, 9})    # Solid white (between groups)
INTRA_GROUP_COLS = frozenset({1, 3, 4, 5, 6, 10})   # Semi-transparent white (within groups)

BORDER_SOLID = QPen(QColor(255, 255, 255), 1)
BORDER_SUBTLE = QPen(QColor(255, 255, 255, 50), 1)
ROW_HOVER_BG = QColor(255, 255, 255, 20)


class PersistentTooltipStyle(QProxyStyle):
    """Keep tooltips visible until the mouse leaves the widget."""

    def styleHint(
        self, hint: QStyle.StyleHint, option=None,
        widget=None, returnData=None,
    ) -> int:
        if hint == QStyle.SH_ToolTip_WakeUpDelay:
            return 0  # Show immediately
        if hint == QStyle.SH_ToolTip_FallAsleepDelay:
            return 0  # No delay between consecutive tooltips
        return super().styleHint(hint, option, widget, returnData)


class TooltipApp(QApplication):
    """QApplication subclass that controls tooltip behavior.

    Overrides notify() to intercept ToolTip events on ALL widgets.
    When tooltips_enabled=True: shows with max duration (no auto-dismiss).
    When tooltips_enabled=False: suppresses all tooltips.
    """

    def __init__(self, argv: list) -> None:
        super().__init__(argv)
        self.tooltips_enabled: bool = True
        self._in_tooltip: bool = False

    def notify(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.ToolTip:
            # Suppress re-entrant tooltip handling — _handle_tooltip
            # can trigger Qt to dispatch nested ToolTip events while
            # widgets are being inspected, causing segfaults in
            # mapTo() / coordinate translation.
            if self._in_tooltip:
                return True
            self._in_tooltip = True
            try:
                return self._handle_tooltip(obj, event)
            except RuntimeError:
                return True
            finally:
                self._in_tooltip = False
        if sip.isdeleted(obj):
            return True
        try:
            return super().notify(obj, event)
        except RuntimeError:
            return True

    def _handle_tooltip(self, obj: QObject, event: QEvent) -> bool:
        """Handle tooltip events, returning True to consume the event."""
        if sip.isdeleted(obj):
            return True
        widget = obj if isinstance(obj, QWidget) else None
        if not widget:
            return False

        from PyQt5.QtWidgets import QAbstractItemView, QToolTip as _QToolTip
        parent = widget.parent()
        if sip.isdeleted(parent) if parent is not None else False:
            return True

        # --- Direct viewport of a table/tree view ---
        if isinstance(parent, QAbstractItemView):
            index = parent.indexAt(event.pos())
            if index.isValid():
                tip = index.data(Qt.ToolTipRole)
                display = index.data(Qt.DisplayRole)
                if tip and str(tip) != '':
                    show = False
                    if display and str(tip) != str(display):
                        # Explanatory tooltip (differs from display)
                        show = self.tooltips_enabled
                    elif display:
                        # Same text — only show when truncated
                        col_w = parent.columnWidth(index.column())
                        text_w = parent.fontMetrics().horizontalAdvance(str(display))
                        show = text_w > col_w - 16
                    if show:
                        _QToolTip.showText(
                            event.globalPos(), str(tip), widget,
                            parent.visualRect(index),
                            2_147_483_647,
                        )
            return True

        # --- obj IS the QAbstractItemView itself (not its viewport) ---
        if isinstance(widget, QAbstractItemView):
            vp = widget.viewport()
            if vp and not sip.isdeleted(vp):
                pos = event.pos() - vp.pos()
                index = widget.indexAt(pos)
                if index.isValid():
                    tip = index.data(Qt.ToolTipRole)
                    display = index.data(Qt.DisplayRole)
                    if tip and str(tip) != '':
                        show = False
                        if display and str(tip) != str(display):
                            show = self.tooltips_enabled
                        elif display:
                            col_w = widget.columnWidth(index.column())
                            text_w = widget.fontMetrics().horizontalAdvance(str(display))
                            show = text_w > col_w - 16
                        if show:
                            _QToolTip.showText(
                                event.globalPos(), str(tip), vp,
                                widget.visualRect(index),
                                2_147_483_647,
                            )
            return True

        # Cell widget inside a table — check if the underlying
        # item has a tooltip (e.g. elided branch text).
        table_view = None
        ancestor = parent
        while ancestor is not None:
            if sip.isdeleted(ancestor):
                return True
            if isinstance(ancestor, QAbstractItemView):
                table_view = ancestor
                break
            ancestor = ancestor.parent()
        if table_view is not None:
            viewport = table_view.viewport()
            if sip.isdeleted(viewport):
                return True
            # Use globalPos → viewport mapping instead of
            # widget.mapTo(viewport) — avoids walking the widget
            # hierarchy which can segfault when a cell widget is
            # partially destroyed during a table rebuild.
            pos = viewport.mapFromGlobal(event.globalPos())
            index = table_view.indexAt(pos)
            if index.isValid():
                display = index.data(Qt.DisplayRole)
                tip = index.data(Qt.ToolTipRole)
                if not display and tip:
                    # Only show if the text is actually truncated.
                    # Use the widget's own width (not the full cell)
                    # since cell widgets share space with buttons.
                    if sip.isdeleted(widget):
                        return True
                    fm = widget.fontMetrics()
                    text_w = fm.horizontalAdvance(tip)
                    if text_w > widget.width():
                        from PyQt5.QtWidgets import QToolTip as _QToolTip
                        _QToolTip.showText(
                            event.globalPos(), tip, viewport,
                            table_view.visualRect(index),
                            2_147_483_647,
                        )
                        return True
            # Fall through to normal widget tooltip handling

        # Always show full-name tooltip on truncated template combo items
        from PyQt5.QtWidgets import QComboBox
        combo = widget if isinstance(widget, QComboBox) else None
        if combo is None and isinstance(parent, QComboBox):
            combo = parent
        if combo is not None and combo.objectName() in (
            'template_combo', 'direct_template_combo',
        ):
            idx = combo.currentIndex()
            full_name = combo.itemData(idx, Qt.UserRole)
            if full_name:
                from PyQt5.QtWidgets import QToolTip as _QToolTip
                _QToolTip.showText(
                    event.globalPos(), full_name, combo,
                    combo.rect(), 2_147_483_647,
                )
                return True
            # Not truncated — fall through to normal tooltips_enabled check

        if not self.tooltips_enabled and not widget.property('always_tooltip'):
            return True  # Suppress
        if widget.toolTip():
            from PyQt5.QtWidgets import QToolTip as _QToolTip
            _QToolTip.showText(
                event.globalPos(), widget.toolTip(), widget,
                widget.rect(), 2_147_483_647,
            )
            return True
        return False


class SeparatorDelegate(QStyledItemDelegate):
    """Delegate that draws vertical separator lines between column groups."""

    def paint(self, painter: Any, option: Any, index: QModelIndex) -> None:
        table = self.parent()
        if table is not None and index.row() == table.property('_hovered_row'):
            painter.fillRect(option.rect, ROW_HOVER_BG)
        super().paint(painter, option, index)
        col = index.column()
        if col in GROUP_BOUNDARY_COLS:
            painter.save()
            painter.setPen(BORDER_SOLID)
            x = option.rect.right()
            painter.drawLine(x, option.rect.top(), x, option.rect.bottom())
            painter.restore()
        elif col in INTRA_GROUP_COLS:
            painter.save()
            painter.setPen(BORDER_SUBTLE)
            x = option.rect.right()
            painter.drawLine(x, option.rect.top(), x, option.rect.bottom())
            painter.restore()


class SeparatorHeaderView(QHeaderView):
    """Header view with vertical separators between column groups."""

    def paintSection(self, painter: Any, rect: Any, logicalIndex: int) -> None:
        painter.save()
        super().paintSection(painter, rect, logicalIndex)
        painter.restore()
        if logicalIndex in GROUP_BOUNDARY_COLS:
            painter.setPen(BORDER_SOLID)
            painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
        elif logicalIndex in INTRA_GROUP_COLS:
            painter.setPen(BORDER_SUBTLE)
            painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
