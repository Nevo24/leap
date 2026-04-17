"""Qt helper widgets for monitor table display.

Contains separator delegates, header views, tooltip overrides, and
column-group boundary constants extracted from app.py.
"""

from typing import Any, Optional

import sip
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QProxyStyle, QPushButton, QStyle,
    QStyledItemDelegate, QTableWidget, QToolTip, QVBoxLayout, QWidget,
)
from PyQt5.QtCore import QEvent, QModelIndex, QObject, QPoint, Qt, QTimer
from PyQt5.QtGui import QColor, QCursor, QIcon, QPixmap, QPainter, QPen
from PyQt5.QtSvg import QSvgRenderer

from leap.monitor.themes import current_theme
from leap.monitor.ui.ui_widgets import ElidedLabel


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

def _hover_color() -> bytes:
    """Return the accent red as bytes for SVG icon hover recoloring."""
    return current_theme().accent_red.encode()


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
        self._hover_icon = _render_svg(svg_data, size, _hover_color())
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
        r = t.border_radius
        self.setStyleSheet(
            f'ColorPickerPopup {{'
            f'  background-color: {t.popup_bg};'
            f'  border: 1px solid {t.popup_border};'
            f'}}'
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        grid = QGridLayout()
        grid.setSpacing(4)
        cols = 4
        for i, color in enumerate(ROW_COLOR_PRESETS):
            btn = QPushButton()
            btn.setFixedSize(28, 28)
            border = f'2px solid {t.text_primary}' if color == current_color else f'1px solid {t.popup_border}'
            btn.setStyleSheet(
                f'QPushButton {{ background-color: {color}; border: {border};'
                f' border-radius: {r}px; }}'
                f'QPushButton:hover {{ border: 2px solid {t.accent_blue}; }}'
            )
            btn.setToolTip(color)
            btn.clicked.connect(lambda checked, c=color: self._pick(c))
            grid.addWidget(btn, i // cols, i % cols)
        layout.addLayout(grid)

        clear_btn = QPushButton('Clear')
        clear_btn.setStyleSheet(
            f'QPushButton {{ color: {t.text_primary};'
            f' background: transparent; border: 1px solid {t.popup_border};'
            f' border-radius: {r}px; padding: 4px 12px; }}'
            f'QPushButton:hover {{ border-color: {t.accent_blue};'
            f' background-color: {t.button_hover_bg or t.border_solid}; }}'
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

CELL_BTN_H = 24  # consistent height for all cell buttons


def close_btn_style(fg_override: Optional[str] = None,
                    font_size: Optional[int] = None) -> str:
    """Return stylesheet for close/delete buttons."""
    t = current_theme()
    fg = fg_override or t.text_muted
    fs = font_size if font_size is not None else t.font_size_base
    h = t.accent_red.lstrip('#')
    rr, gg, bb = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (
        f'QPushButton {{ color: {fg}; font-size: {fs}px;'
        f' padding: 0px 6px 1px 6px;'
        f' background-color: {t.button_bg or t.window_bg};'
        f' border: 1px solid {t.button_border or t.border_solid};'
        f' border-radius: {t.border_radius}px; }}'
        f'QPushButton:hover {{ color: {t.accent_red};'
        f' background-color: rgba({rr}, {gg}, {bb}, 0.15);'
        f' border-color: {t.accent_red}; }}'
    )


def active_btn_style(fg_override: Optional[str] = None) -> str:
    """Return stylesheet for active/connected indicator buttons."""
    t = current_theme()
    fg = fg_override or t.accent_green
    h = fg.lstrip('#')
    rr, gg, bb = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (
        f'QPushButton {{ color: {fg};'
        f' background-color: rgba({rr}, {gg}, {bb}, 0.10);'
        f' border: 1px solid rgba({rr}, {gg}, {bb}, 0.30);'
        f' border-radius: {t.border_radius}px;'
        f' padding: 0px 8px; }}'
        f'QPushButton:hover {{ background-color: rgba({rr}, {gg}, {bb}, 0.20);'
        f' border-color: {fg}; }}'
    )


def inactive_btn_style(fg_override: Optional[str] = None) -> str:
    """Return stylesheet for inactive/dead cell buttons (same size as active)."""
    t = current_theme()
    fg = fg_override or t.text_primary
    return (
        f'QPushButton {{ color: {fg};'
        f' background-color: {t.button_bg or t.window_bg};'
        f' border: 1px solid {t.button_border or t.border_solid};'
        f' border-radius: {t.border_radius}px;'
        f' padding: 0px 8px; }}'
        f'QPushButton:hover {{ background-color: {t.button_hover_bg or t.border_solid};'
        f' border-color: {t.accent_blue}; }}'
        f'QPushButton:disabled {{ color: {t.text_muted};'
        f' background-color: {t.button_bg or t.window_bg};'
        f' border-color: {t.button_border or t.border_solid}; }}'
    )


def menu_btn_style(fg_override: Optional[str] = None,
                   font_size: Optional[int] = None) -> str:
    """Return stylesheet for icon/menu buttons in table cells."""
    t = current_theme()
    fg = fg_override or t.icon_color
    hover = fg_override or t.text_primary
    hover_bg = t.button_hover_bg or t.border_solid
    fs = font_size if font_size is not None else t.font_size_base
    return (
        f'QPushButton {{ color: {fg}; font-size: {fs}px;'
        f' padding: 0px 4px;'
        f' background-color: {t.button_bg or t.window_bg};'
        f' border: 1px solid {t.button_border or t.border_solid};'
        f' border-radius: {t.border_radius}px; }}'
        f'QPushButton:hover {{ color: {hover};'
        f' background-color: {hover_bg}; }}'
    )


# Column groups for vertical separators.
# Groups: [X, Tag, CLI, Project] | [Server, Last Msg, Path, ServerBranch, Status, Queue] | [Client] | [Slack] | [PR, PRBranch]
COLUMN_GROUPS: list[list[int]] = [
    [0, 1, 2, 3],          # Info
    [4, 5, 6, 7, 8, 9],    # Server
    [10],                   # Client
    [11],                   # Slack
    [12, 13],               # PR
]

# Precomputed: column index → group index (for fast lookup)
_COL_TO_GROUP: dict[int, int] = {
    col: gi for gi, group in enumerate(COLUMN_GROUPS) for col in group
}


def border_solid_pen() -> QPen:
    """Return a QPen for solid group-boundary separators (2px, fully opaque)."""
    return QPen(QColor(current_theme().border_solid), 2)


def border_subtle_pen() -> QPen:
    """Return a QPen for subtle intra-group separators (1px, semi-transparent)."""
    t = current_theme()
    # Parse rgba() or hex — bump alpha for better visibility
    bs = t.border_subtle
    if bs.startswith('rgba('):
        parts = bs[5:-1].split(',')
        # Increase alpha to make intra-group lines more visible
        alpha = min(255, int(int(parts[3]) * 2.5))
        return QPen(QColor(int(parts[0]), int(parts[1]), int(parts[2]), alpha), 1)
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
    When tooltips_enabled=True: shows explanatory tooltips + truncated text.
    When tooltips_enabled=False: suppresses explanatory tooltips, still shows
    truncated text on hover so users can read clipped content.
    """

    def __init__(self, argv: list) -> None:
        super().__init__(argv)
        self.tooltips_enabled: bool = True
        self._suppress_tooltips: bool = False  # Hard suppress during table rebuild
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
        try:
            if rect is not None:
                QToolTip.showText(global_pos, text, widget, rect, duration)
            else:
                QToolTip.showText(global_pos, text, widget)
        except RuntimeError:
            pass

    def _handle_tooltip(self, obj: QObject, event: QEvent) -> bool:
        """Handle tooltip events, returning True to consume the event."""
        if self._suppress_tooltips:
            return True  # Hard suppress during table rebuild (safety)
        if sip.isdeleted(obj):
            return True
        widget = obj if isinstance(obj, QWidget) else None
        if not widget:
            return False

        parent = widget.parent()
        if sip.isdeleted(parent) if parent is not None else False:
            return True

        # --- Header view viewport (e.g. column headers) ---
        if isinstance(parent, QHeaderView):
            logical = parent.logicalIndexAt(event.pos())
            if logical >= 0:
                table = parent.parent()
                if table is not None and not sip.isdeleted(table):
                    if isinstance(table, QTableWidget):
                        header_item = table.horizontalHeaderItem(logical)
                        if header_item:
                            label = header_item.text()
                            tip = header_item.toolTip()
                            # Always show label when column is too narrow
                            section_w = parent.sectionSize(logical)
                            text_w = parent.fontMetrics().horizontalAdvance(
                                label)
                            truncated = text_w > section_w - 16
                            if truncated and not tip:
                                # No explanatory tooltip — show just the label
                                self._show_tip(
                                    event.globalPos(), label, widget,
                                    parent.rect(),
                                )
                                return True
                            if tip:
                                if truncated or self.tooltips_enabled:
                                    shown = (f'{label}\n\n{tip}'
                                             if truncated else tip)
                                    self._show_tip(
                                        event.globalPos(), shown, widget,
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
                    col_w = parent.columnWidth(index.column())
                    text_w = parent.fontMetrics().horizontalAdvance(
                        str(display)) if display else 0
                    truncated = text_w > col_w - 16
                    if display and str(tip) != str(display):
                        # Tooltip differs from display — show if
                        # tooltips enabled OR display is truncated
                        show = self.tooltips_enabled or truncated
                    elif display:
                        # Same text — only show when truncated
                        show = truncated
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
                        col_w = widget.columnWidth(index.column())
                        text_w = widget.fontMetrics().horizontalAdvance(
                            str(display)) if display else 0
                        truncated = text_w > col_w - 16
                        if display and str(tip) != str(display):
                            show = self.tooltips_enabled or truncated
                        elif display:
                            show = truncated
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

        # ElidedLabel: show tooltip when truncated even if tooltips are off
        if isinstance(widget, ElidedLabel) and widget.toolTip():
            if widget.is_truncated() or self.tooltips_enabled or widget.property('always_tooltip'):
                self._show_tip(
                    event.globalPos(), widget.toolTip(), widget,
                    widget.rect(),
                )
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
        is_hovered = table is not None and index.row() == table.property('_hovered_row')
        if is_hovered:
            painter.fillRect(option.rect, row_hover_bg())
        super().paint(painter, option, index)
        if is_hovered:
            t_h = current_theme()
            # Left accent bar (first column only)
            if index.column() == 0:
                painter.save()
                accent = QColor(t_h.accent_blue)
                painter.fillRect(
                    option.rect.left(), option.rect.top(),
                    3, option.rect.height(),
                    accent,
                )
                painter.restore()
            # Bottom highlight line across all columns
            painter.save()
            bottom_color = QColor(t_h.accent_blue)
            bottom_color.setAlpha(40)
            painter.setPen(QPen(bottom_color, 1))
            y = option.rect.bottom()
            painter.drawLine(option.rect.left(), y, option.rect.right(), y)
            painter.restore()
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
