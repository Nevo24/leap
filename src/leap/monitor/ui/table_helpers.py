"""Qt helper widgets for monitor table display.

Contains separator delegates, header views, tooltip overrides, and
column-group boundary constants extracted from app.py.
"""

from typing import Any, Optional

import sip
from PyQt5.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QPushButton, QProxyStyle, QStyle, QStyledItemDelegate,
    QVBoxLayout, QWidget,
)
from PyQt5.QtCore import QEvent, QModelIndex, QObject, QPoint, Qt, QTimer
from PyQt5.QtGui import QColor, QIcon, QPixmap, QPainter, QPen
from PyQt5.QtSvg import QSvgRenderer

from leap.monitor.themes import current_theme


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

_PALETTE_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    b'<defs><clipPath id="r"><rect x="256" y="0" width="256" height="512"/></clipPath></defs>'
    b'<path d="M256 16C192 128 80 224 80 336c0 97 79 176 176 176s176-79 176-176'
    b'C432 224 320 128 256 16z" fill="none" stroke="#aaa" stroke-width="36"'
    b' stroke-linejoin="round"/>'
    b'<path d="M256 16C192 128 80 224 80 336c0 97 79 176 176 176s176-79 176-176'
    b'C432 224 320 128 256 16z" fill="#aaa" clip-path="url(#r)"/>'
    b'</svg>'
)

# Muted background colors that work well with both dark and light themes.
# 16 presets — 4 columns x 4 rows in the picker popup.
ROW_COLOR_PRESETS: list[str] = [
    '#8b0000',  # dark red
    '#a0522d',  # sienna
    '#b8860b',  # dark goldenrod
    '#556b2f',  # dark olive green
    '#2e8b57',  # sea green
    '#008080',  # teal
    '#4682b4',  # steel blue
    '#483d8b',  # dark slate blue
    '#6a5acd',  # slate blue
    '#8b008b',  # dark magenta
    '#800020',  # burgundy
    '#704214',  # sepia
    '#36454f',  # charcoal
    '#2f4f4f',  # dark slate grey
    '#191970',  # midnight blue
    '#3c1414',  # dark bean
]

_HOVER_COLOR = b'#ff4444'


def _render_svg(svg_data: bytes, size: int, color: Optional[bytes] = None) -> QIcon:
    """Render SVG bytes into a QIcon, replacing #aaa with *color*."""
    if color is None:
        color = current_theme().icon_color.encode()
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
        self._svg_data = svg_data
        self._svg_size = size
        self._normal_icon = _render_svg(svg_data, size)
        self._hover_icon = _render_svg(svg_data, size, _HOVER_COLOR)
        self.setIcon(self._normal_icon)
        self._hover_timer = QTimer(self)
        self._hover_timer.timeout.connect(self._check_hover)

    def set_icon_color(self, color: str) -> None:
        """Re-render the normal icon with a specific color for contrast."""
        self._normal_icon = _render_svg(
            self._svg_data, self._svg_size, color.encode())
        if not self.underMouse():
            self.setIcon(self._normal_icon)

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


class ColorPickerPopup(QFrame):
    """Popup with a grid of color swatches and a Clear button."""

    def __init__(
        self,
        current_color: Optional[str],
        on_color_selected: Any,  # Callable[[Optional[str]], None]
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, Qt.Popup)
        self._callback = on_color_selected
        t = current_theme()
        self.setStyleSheet(
            f'ColorPickerPopup {{'
            f'  background-color: {t.popup_bg};'
            f'  border: 1px solid {t.popup_border};'
            f'  border-radius: 4px;'
            f'}}'
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        grid = QGridLayout()
        grid.setSpacing(3)
        cols = 4
        for i, color in enumerate(ROW_COLOR_PRESETS):
            btn = QPushButton()
            btn.setFixedSize(24, 24)
            border = f'2px solid {t.text_primary}' if color == current_color else '1px solid #555'
            btn.setStyleSheet(
                f'QPushButton {{ background-color: {color}; border: {border};'
                f' border-radius: 3px; }}'
                f'QPushButton:hover {{ border: 2px solid {t.accent_blue}; }}'
            )
            btn.setToolTip(color)
            btn.clicked.connect(lambda checked, c=color: self._pick(c))
            grid.addWidget(btn, i // cols, i % cols)
        layout.addLayout(grid)

        clear_btn = QPushButton('Clear')
        clear_btn.setStyleSheet(
            f'QPushButton {{ color: {t.text_primary}; font-size: 11px;'
            f' background: transparent; border: 1px solid {t.popup_border};'
            f' border-radius: 3px; padding: 2px 8px; }}'
            f'QPushButton:hover {{ border-color: {t.accent_blue}; }}'
        )
        clear_btn.clicked.connect(lambda: self._pick(None))
        layout.addWidget(clear_btn)

    def _pick(self, color: Optional[str]) -> None:
        self._callback(color)
        self.close()


# Preset UI strings (single source of truth for labels, tooltips, hints).
PR_PRESET_LABEL = 'PR thread context:'
PR_PRESET_TOOLTIP = 'Context attached to every PR thread message sent to Leap (single-message only)'
PR_PRESET_HINT = (
    'PR thread context: This preset is attached to every PR thread message sent to Leap. '
    'Only single-message presets can be used.'
)

QUICK_MSG_PRESET_LABEL = 'Message bundle:'
QUICK_MSG_PRESET_TOOLTIP = 'Preset messages sent via Queue column send button'
QUICK_MSG_PRESET_HINT = (
    'Message bundle: These preset messages are sent as standalone messages '
    '(via the send button in the Queue column).'
)

QUICK_MSG_SEND_NEXT = 'Send message-bundle next'
QUICK_MSG_SEND_AT_END = 'Send message-bundle to end'

APPLY_PR_BTN = 'Apply to PR Thread Context && Close'
APPLY_QUICK_MSG_BTN = 'Apply to Message Bundle && Close'

# Max characters shown in preset combo items before truncation with ellipsis
MAX_COMBO_DISPLAY = 40

# Theme-aware stylesheet functions for cell buttons

def close_btn_style(fg_override: Optional[str] = None) -> str:
    """Return stylesheet for close/delete buttons."""
    t = current_theme()
    fg = fg_override or t.text_muted
    return (
        f'QPushButton {{ color: {fg}; font-size: 11px; padding: 0 0 2px 0; }}'
        f'QPushButton:hover {{ color: {t.accent_red}; font-weight: bold; }}'
    )


def active_btn_style(fg_override: Optional[str] = None) -> str:
    """Return stylesheet for active/connected indicator buttons."""
    t = current_theme()
    fg = fg_override or t.accent_green
    return f'QPushButton {{ color: {fg}; }}'


def menu_btn_style(fg_override: Optional[str] = None) -> str:
    """Return stylesheet for three-dot menu buttons."""
    t = current_theme()
    fg = fg_override or t.icon_color
    hover = fg_override or t.text_primary
    return (
        f'QPushButton {{ color: {fg}; font-size: 14px; padding: 0; }}'
        f'QPushButton:hover {{ color: {hover}; }}'
    )


# Legacy constants kept for backward compatibility (imports in other files)
CLOSE_BTN_STYLE = (
    'QPushButton { color: #999; font-size: 11px; padding: 0 0 2px 0; }'
    'QPushButton:hover { color: #ff4444; font-weight: bold; }'
)
ACTIVE_BTN_STYLE = 'QPushButton { color: #00ff00; }'
MENU_BTN_STYLE = (
    'QPushButton { color: #aaa; font-size: 14px; padding: 0; }'
    'QPushButton:hover { color: #ffffff; }'
)

# Column groups for vertical separators.
# Groups: [X, Tag, CLI, Project] | [Server, Path, ServerBranch, Status, Queue] | [Client] | [Slack] | [PR, PRBranch]
COLUMN_GROUPS: list[list[int]] = [
    [0, 1, 2, 3],       # Info
    [4, 5, 6, 7, 8],    # Server
    [9],                 # Client
    [10],                # Slack
    [11, 12],            # PR
]

# Precomputed: column index → group index (for fast lookup)
_COL_TO_GROUP: dict[int, int] = {
    col: gi for gi, group in enumerate(COLUMN_GROUPS) for col in group
}

# Legacy constants kept for backward compatibility (used by _set_cell_widget static fallback)
GROUP_BOUNDARY_COLS = frozenset({0, 3, 8, 9, 10})    # Solid white (between groups)
INTRA_GROUP_COLS = frozenset({1, 2, 4, 5, 6, 7, 11})  # Semi-transparent white (within groups)

BORDER_SOLID = QPen(QColor(255, 255, 255), 1)
BORDER_SUBTLE = QPen(QColor(255, 255, 255, 50), 1)


def border_solid_pen() -> QPen:
    """Return a QPen for solid group-boundary separators."""
    return QPen(QColor(current_theme().border_solid), 1)


def border_subtle_pen() -> QPen:
    """Return a QPen for subtle intra-group separators."""
    t = current_theme()
    # Parse rgba() or hex
    bs = t.border_subtle
    if bs.startswith('rgba('):
        parts = bs[5:-1].split(',')
        return QPen(QColor(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])), 1)
    return QPen(QColor(bs), 1)

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


def row_hover_bg() -> QColor:
    """Return the QColor for row hover background."""
    t = current_theme()
    h = t.hover_bg
    if h.startswith('rgba('):
        parts = h[5:-1].split(',')
        return QColor(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
    return QColor(h)


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

    def _show_tip(self, global_pos: QPoint, text: str,
                  widget: QWidget, rect: 'Any' = None,
                  duration: int = 2_147_483_647) -> None:
        """Show a tooltip with max duration so it persists while hovered."""
        # Final guard: widget may have been destroyed between the caller's
        # sip.isdeleted() check and this call (e.g. table rebuild triggered
        # a nested tooltip event on a just-deleted cell widget).
        if sip.isdeleted(widget):
            return
        from PyQt5.QtWidgets import QToolTip as _QToolTip
        try:
            if rect is not None:
                _QToolTip.showText(global_pos, text, widget, rect, duration)
            else:
                _QToolTip.showText(global_pos, text, widget)
        except RuntimeError:
            pass

    def _handle_tooltip(self, obj: QObject, event: QEvent) -> bool:
        """Handle tooltip events, returning True to consume the event."""
        if not self.tooltips_enabled:
            return True  # Suppress all tooltips (e.g. during table rebuild)
        if sip.isdeleted(obj):
            return True
        widget = obj if isinstance(obj, QWidget) else None
        if not widget:
            return False

        from PyQt5.QtWidgets import QAbstractItemView
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
                                    self._show_tip(
                                        event.globalPos(), tip, widget,
                                        parent.rect(),
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
                        self._show_tip(
                            event.globalPos(), str(tip), widget,
                            parent.visualRect(index),
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
                            self._show_tip(
                                event.globalPos(), str(tip), vp,
                                widget.visualRect(index),
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
                        if not sip.isdeleted(viewport):
                            self._show_tip(
                                event.globalPos(), final_tip, viewport,
                                table_view.visualRect(index),
                            )
                        return True
            # Fall through to normal widget tooltip handling

        # Always show full-name tooltip on truncated preset combo items
        from PyQt5.QtWidgets import QComboBox
        combo = widget if isinstance(widget, QComboBox) else None
        if combo is None and isinstance(parent, QComboBox):
            combo = parent
        if combo is not None and combo.objectName() in (
            'preset_combo', 'direct_preset_combo',
        ):
            idx = combo.currentIndex()
            full_name = combo.itemData(idx, Qt.UserRole)
            if full_name:
                self._show_tip(
                    event.globalPos(), full_name, combo,
                    combo.rect(),
                )
                return True
            # Not truncated — fall through to normal tooltips_enabled check

        if sip.isdeleted(widget):
            return True
        if not self.tooltips_enabled and not widget.property('always_tooltip'):
            return True  # Suppress
        if widget.toolTip():
            if sip.isdeleted(widget):
                return True
            parent = widget.parent()
            if parent is not None and sip.isdeleted(parent):
                return True
            self._show_tip(
                event.globalPos(), widget.toolTip(), widget,
                widget.rect(),
            )
            return True
        return False


class SeparatorDelegate(QStyledItemDelegate):
    """Delegate that draws vertical separator lines between column groups."""

    def paint(self, painter: Any, option: Any, index: QModelIndex) -> None:
        table = self.parent()
        # Draw row background color (if any) before everything else
        if table is not None:
            row_colors = table.property('_row_colors')
            row_tags = table.property('_row_tags')
            if row_colors and row_tags:
                row = index.row()
                if 0 <= row < len(row_tags):
                    rc = row_colors.get(row_tags[row])
                    if rc:
                        painter.fillRect(option.rect, QColor(rc))
        if table is not None and index.row() == table.property('_hovered_row'):
            painter.fillRect(option.rect, row_hover_bg())
        super().paint(painter, option, index)
        col = index.column()
        border = column_border_type(col, table) if table is not None else BORDER_NONE
        if border == BORDER_GROUP:
            painter.save()
            painter.setPen(border_solid_pen())
            x = option.rect.right()
            painter.drawLine(x, option.rect.top(), x, option.rect.bottom())
            painter.restore()
        elif border == BORDER_INTRA:
            painter.save()
            painter.setPen(border_subtle_pen())
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
            painter.setPen(border_solid_pen())
            painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
        elif border == BORDER_INTRA:
            painter.setPen(border_subtle_pen())
            painter.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())
