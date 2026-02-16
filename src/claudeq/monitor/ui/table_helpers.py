"""Qt helper widgets for monitor table display.

Contains separator delegates, header views, tooltip overrides, and
column-group boundary constants extracted from app.py.
"""

from typing import Any

from PyQt5.QtWidgets import (
    QApplication, QHeaderView, QProxyStyle, QStyle, QStyledItemDelegate,
    QWidget,
)
from PyQt5.QtCore import QEvent, QModelIndex, QObject, Qt
from PyQt5.QtGui import QColor, QPen


# Column group boundaries for vertical separators.
# Groups: [Tag, Project] | [Server, ServerBranch, Status, Queue] | [Client] | [MR, MRBranch]
GROUP_BOUNDARY_COLS = frozenset({0, 2, 6, 7})   # Solid white (between groups)
INTRA_GROUP_COLS = frozenset({1, 3, 4, 5, 8})    # Semi-transparent white (within groups)

BORDER_SOLID = QPen(QColor(255, 255, 255), 1)
BORDER_SUBTLE = QPen(QColor(255, 255, 255, 50), 1)


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

    def notify(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.ToolTip:
            widget = obj if isinstance(obj, QWidget) else None
            if widget:
                # Always show cell tooltips in table views (truncated text)
                from PyQt5.QtWidgets import QAbstractItemView
                parent = widget.parent()
                if isinstance(parent, QAbstractItemView):
                    index = parent.indexAt(event.pos())
                    if index.isValid():
                        display = index.data(Qt.DisplayRole)
                        if display:
                            fm = widget.fontMetrics()
                            text_w = fm.horizontalAdvance(str(display))
                            cell_w = parent.visualRect(index).width()
                            if text_w > cell_w - 10:
                                tip = index.data(Qt.ToolTipRole)
                                if tip:
                                    from PyQt5.QtWidgets import QToolTip as _QToolTip
                                    _QToolTip.showText(
                                        event.globalPos(), tip, widget,
                                        parent.visualRect(index),
                                        2_147_483_647,
                                    )
                    return True

                # Always show full-name tooltip on truncated context combo items
                from PyQt5.QtWidgets import QComboBox
                combo = widget if isinstance(widget, QComboBox) else None
                if combo is None and isinstance(parent, QComboBox):
                    combo = parent
                if combo is not None and combo.objectName() == 'context_combo':
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

                if not self.tooltips_enabled:
                    return True  # Suppress
                if widget.toolTip():
                    from PyQt5.QtWidgets import QToolTip as _QToolTip
                    _QToolTip.showText(
                        event.globalPos(), widget.toolTip(), widget,
                        widget.rect(), 2_147_483_647,
                    )
                    return True
        return super().notify(obj, event)


class SeparatorDelegate(QStyledItemDelegate):
    """Delegate that draws vertical separator lines between column groups."""

    def paint(self, painter: Any, option: Any, index: QModelIndex) -> None:
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
