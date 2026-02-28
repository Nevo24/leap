"""Qt helper widgets for monitor table display.

Contains separator delegates, header views, tooltip overrides, and
column-group boundary constants extracted from app.py.
"""

from typing import Any, Optional

import sip
from PyQt5.QtWidgets import (
    QApplication, QHeaderView, QPushButton, QProxyStyle, QStyle,
    QStyledItemDelegate, QWidget,
)
from PyQt5.QtCore import QEvent, QModelIndex, QObject, Qt, QTimer
from PyQt5.QtGui import QColor, QIcon, QPixmap, QPainter, QPen
from PyQt5.QtSvg import QSvgRenderer


_OPEN_EXTERNAL_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    b'<path d="M384 224v184a40 40 0 0 1-40 40H104a40 40 0 0 1-40-40V168'
    b'a40 40 0 0 1 40-40h184" fill="none" stroke="#aaa" stroke-width="44"'
    b' stroke-linecap="round" stroke-linejoin="round"/>'
    b'<polyline points="336 64 448 64 448 176" fill="none" stroke="#aaa"'
    b' stroke-width="44" stroke-linecap="round" stroke-linejoin="round"/>'
    b'<line x1="448" y1="64" x2="240" y2="272" stroke="#aaa"'
    b' stroke-width="44" stroke-linecap="round"/>'
    b'</svg>'
)

_GIT_BRANCH_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    b'<circle cx="160" cy="96" r="48" fill="none" stroke="#aaa" stroke-width="40"/>'
    b'<circle cx="352" cy="192" r="48" fill="none" stroke="#aaa" stroke-width="40"/>'
    b'<circle cx="160" cy="416" r="48" fill="none" stroke="#aaa" stroke-width="40"/>'
    b'<line x1="160" y1="144" x2="160" y2="368" stroke="#aaa" stroke-width="40"/>'
    b'<path d="M160 208 C160 208 160 192 192 192 L304 192" '
    b'fill="none" stroke="#aaa" stroke-width="40"/>'
    b'</svg>'
)


_THREE_DOT_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    b'<circle cx="256" cy="96" r="48" fill="#aaa"/>'
    b'<circle cx="256" cy="256" r="48" fill="#aaa"/>'
    b'<circle cx="256" cy="416" r="48" fill="#aaa"/>'
    b'</svg>'
)

_SEND_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    b'<path d="M464 48L48 240l168 64 184-208-144 248 64 120z"'
    b' fill="none" stroke="#aaa" stroke-width="40"'
    b' stroke-linejoin="round" stroke-linecap="round"/>'
    b'<line x1="216" y1="304" x2="400" y2="96"'
    b' stroke="#aaa" stroke-width="40" stroke-linecap="round"/>'
    b'</svg>'
)

_HOVER_COLOR = b'#ff4444'


def _render_svg(svg_data: bytes, size: int, color: bytes = b'#aaa') -> QIcon:
    """Render SVG bytes into a QIcon, replacing #aaa with *color*."""
    colored = svg_data.replace(b'#aaa', color).replace(b'#888', color)
    renderer = QSvgRenderer(colored)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


class HoverIconButton(QPushButton):
    """QPushButton that swaps to a red icon on hover."""

    def __init__(self, svg_data: bytes, size: int = 14,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._normal_icon = _render_svg(svg_data, size)
        self._hover_icon = _render_svg(svg_data, size, _HOVER_COLOR)
        self.setIcon(self._normal_icon)
        self._hover_timer = QTimer(self)
        self._hover_timer.timeout.connect(self._check_hover)

    def enterEvent(self, event: Any) -> None:
        self.setIcon(self._hover_icon)
        self._hover_timer.start(150)
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self._reset()
        super().leaveEvent(event)

    def _reset(self) -> None:
        self._hover_timer.stop()
        self.setIcon(self._normal_icon)

    def _check_hover(self) -> None:
        if QApplication.activePopupWidget():
            return  # menu is open — stay red
        from PyQt5.QtGui import QCursor
        local_pos = self.mapFromGlobal(QCursor.pos())
        if not self.rect().contains(local_pos):
            self._reset()


def open_external_icon(size: int = 16) -> QIcon:
    """Return a small open-external icon."""
    return _render_svg(_OPEN_EXTERNAL_SVG, size)


def git_branch_icon(size: int = 16) -> QIcon:
    """Return a small git-branch icon."""
    return _render_svg(_GIT_BRANCH_SVG, size)


def send_icon(size: int = 16) -> QIcon:
    """Return a small paper-plane send icon."""
    return _render_svg(_SEND_SVG, size)


# Template UI strings (single source of truth for labels, tooltips, hints).
MR_TEMPLATE_LABEL = 'MR thread context:'
MR_TEMPLATE_TOOLTIP = 'Context attached to every MR thread message sent to CQ (single-message only)'
MR_TEMPLATE_HINT = (
    'MR thread context: This preset is attached to every MR thread message sent to CQ. '
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

APPLY_MR_BTN = 'Apply to MR Thread Context && Close'
APPLY_QUICK_MSG_BTN = 'Apply to Message Bundle && Close'

# Max characters shown in template combo items before truncation with ellipsis
MAX_COMBO_DISPLAY = 40

# Reusable stylesheet constants for cell buttons
CLOSE_BTN_STYLE = (
    'QPushButton { color: #999; font-size: 11px; padding: 0 0 2px 0; }'
    'QPushButton:hover { color: #ff4444; font-weight: bold; }'
)
ACTIVE_BTN_STYLE = (
    'QPushButton { color: #00ff00; } '
    'QToolTip { color: #e0e0e0; }'
)
MENU_BTN_STYLE = (
    'QPushButton { color: #aaa; font-size: 14px; padding: 0; }'
    'QPushButton:hover { color: #ffffff; }'
)

# Column groups for vertical separators.
# Groups: [X, Tag, Project] | [Server, Path, ServerBranch, Status, Queue] | [Client] | [Slack] | [MR, MRBranch]
COLUMN_GROUPS: list[list[int]] = [
    [0, 1, 2],          # Info
    [3, 4, 5, 6, 7],    # Server
    [8],                 # Client
    [9],                 # Slack
    [10, 11],            # MR
]

# Precomputed: column index → group index (for fast lookup)
_COL_TO_GROUP: dict[int, int] = {
    col: gi for gi, group in enumerate(COLUMN_GROUPS) for col in group
}

# Legacy constants kept for backward compatibility (used by _set_cell_widget static fallback)
GROUP_BOUNDARY_COLS = frozenset({0, 2, 7, 8, 9})    # Solid white (between groups)
INTRA_GROUP_COLS = frozenset({1, 3, 4, 5, 6, 10})   # Semi-transparent white (within groups)

BORDER_SOLID = QPen(QColor(255, 255, 255), 1)
BORDER_SUBTLE = QPen(QColor(255, 255, 255, 50), 1)

# Border type constants returned by column_border_type()
BORDER_NONE = 0
BORDER_GROUP = 1    # Solid white — between groups
BORDER_INTRA = 2    # Semi-transparent — within a group


def column_border_type(col: int, table: Any) -> int:
    """Return the border type for a column's right edge given current visibility.

    Examines which columns are hidden to decide dynamically whether *col*
    sits at a group boundary, inside a group, or at the very last visible
    column (no border).
    """
    gi = _COL_TO_GROUP.get(col)
    if gi is None:
        return BORDER_NONE

    group = COLUMN_GROUPS[gi]

    # Find visible columns in this group that come *after* col
    has_later_in_group = any(
        c > col and not table.isColumnHidden(c) for c in group
    )

    if has_later_in_group:
        return BORDER_INTRA

    # col is the last visible column in its group.
    # Draw a solid border only if there's at least one visible column
    # in a later group.
    for later_gi in range(gi + 1, len(COLUMN_GROUPS)):
        if any(not table.isColumnHidden(c) for c in COLUMN_GROUPS[later_gi]):
            return BORDER_GROUP

    # Last visible column overall — no border
    return BORDER_NONE
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
                handled = self._handle_tooltip(obj, event)
                if handled:
                    return True
                # Not handled by us — let Qt dispatch normally
                # (needed for e.g. QMenu action tooltips via
                # setToolTipsVisible).
                return super().notify(obj, event)
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

        # --- Header view viewport (e.g. column headers) ---
        if isinstance(parent, QHeaderView):
            logical = parent.logicalIndexAt(event.pos())
            if logical >= 0:
                table = parent.parent()
                if table is not None and not sip.isdeleted(table):
                    from PyQt5.QtWidgets import QTableWidget
                    if isinstance(table, QTableWidget):
                        header_item = table.horizontalHeaderItem(logical)
                        if header_item:
                            tip = header_item.toolTip()
                            if tip:
                                if self.tooltips_enabled:
                                    _QToolTip.showText(
                                        event.globalPos(), tip, widget,
                                        parent.rect(), 2_147_483_647,
                                    )
                                return True
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
                        final_tip = tip
                        if self.tooltips_enabled:
                            from PyQt5.QtWidgets import QTableWidget
                            if isinstance(table_view, QTableWidget):
                                cell_w = table_view.cellWidget(
                                    index.row(), index.column())
                                if cell_w and not sip.isdeleted(cell_w):
                                    extra = cell_w.property(
                                        '_extra_tooltip')
                                    if extra:
                                        final_tip = f'{tip} | {extra}'
                        from PyQt5.QtWidgets import QToolTip as _QToolTip
                        _QToolTip.showText(
                            event.globalPos(), final_tip, viewport,
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
        border = column_border_type(col, table) if table is not None else BORDER_NONE
        if border == BORDER_GROUP:
            painter.save()
            painter.setPen(BORDER_SOLID)
            x = option.rect.right()
            painter.drawLine(x, option.rect.top(), x, option.rect.bottom())
            painter.restore()
        elif border == BORDER_INTRA:
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
        table = self.parent()
        border = column_border_type(logicalIndex, table) if table is not None else BORDER_NONE
        if border == BORDER_GROUP:
            painter.setPen(BORDER_SOLID)
            painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
        elif border == BORDER_INTRA:
            painter.setPen(BORDER_SUBTLE)
            painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
