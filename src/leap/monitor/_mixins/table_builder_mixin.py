"""Table construction, refresh, settings, and preset editor methods."""

from __future__ import annotations

import logging
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from PyQt5 import sip
from PyQt5.QtWidgets import (
    QApplication, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QMenu, QMessageBox, QPushButton, QTableWidgetItem, QWidget,
)
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtGui import QColor, QCursor, QFont, QPalette

from leap.monitor.dialogs.notes_dialog import NotesDialog
from leap.monitor.dialogs.queue_edit_dialog import QueueEditDialog
from leap.monitor.dialogs.scm_template_dialog import PresetEditorDialog
from leap.monitor.dialogs.settings_dialog import DEFAULT_REPOS_DIR, SettingsDialog
from leap.monitor.leap_sender import prepend_to_leap_queue, send_to_leap_session_raw
from leap.monitor.pr_tracking.base import PRState, PRStatus
from leap.monitor.pr_tracking.config import (
    get_dock_enabled, get_notification_prefs,
    load_saved_presets, update_pinned_session_field,
)
from leap.cli_providers.registry import DEFAULT_PROVIDER, get_display_name
from leap.cli_providers.states import AutoSendMode, CLIState
from leap.monitor.ui.image_text_edit import SendMessageDialog, SendPresetDialog
from leap.slack.config import (
    is_slack_installed, load_slack_config, load_slack_sessions, resolve_team_id,
)
from leap.utils.constants import SOCKET_DIR, load_settings, save_settings
from leap.utils.menu import extract_menu_options
from leap.utils.socket_utils import send_socket_request
from leap.monitor.scm_polling import BackgroundCallWorker, SessionRefreshWorker
from leap.monitor.cursor_gui_scan import CURSOR_GUI_ROW_TYPE, CURSOR_GUI_TAG_PREFIX
from leap.monitor.navigation import close_cursor_composer, focus_cursor_window
from leap.monitor.ui.ui_widgets import ElidedLabel, IndicatorLabel, PulsingLabel
from leap.monitor.themes import current_theme, ensure_contrast
from leap.monitor.ui.table_helpers import (
    ColorPickerPopup, HoverIconButton,
    CELL_BTN_H, active_btn_style, close_btn_style, inactive_btn_style, menu_btn_style,
    _GIT_BRANCH_SVG, _OPEN_EXTERNAL_SVG, _PALETTE_SVG, _SEND_SVG,
    _THREE_DOT_SVG,
)

if TYPE_CHECKING:
    from leap.monitor.app import MonitorWindow
    _Base = MonitorWindow
else:
    _Base = object

logger = logging.getLogger(__name__)


def _hex_to_rgb_str(hex_color: str) -> str:
    """Convert '#rrggbb' to 'r, g, b' for use in rgba() CSS values."""
    h = hex_color.lstrip('#')
    return f'{int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}'


class TableBuilderMixin(_Base):
    """Methods for table construction, cell helpers, refresh, settings, and preset editor."""

    _CENTER_COLS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14})  # All data columns
    # Columns that display technical/code data — rendered in monospace font
    _MONO_COLS = frozenset({4, 7, 8, 14})  # Project, Path, Server Branch, PR Branch

    def _set_cell_widget(self, row: int, col: int, widget: QWidget) -> None:
        """Set a cell widget wrapped in a hover-aware container.

        All cell widgets are wrapped so the row hover highlight can be
        toggled uniformly via the ``_hover`` dynamic property.  Columns
        at group boundaries additionally get a right border.
        """
        wrapper = QWidget()
        wrapper.setObjectName('_leapSep')
        wrapper.setAttribute(Qt.WA_TranslucentBackground)
        wrapper.setStyleSheet(
            '#_leapSep { background: transparent; }'
        )
        widget.setAttribute(Qt.WA_TranslucentBackground)
        lay = QHBoxLayout(wrapper)
        lay.setContentsMargins(3, 2, 3, 2)
        lay.setSpacing(0)
        # Enforce a consistent height on every QPushButton inside cell
        # widgets (so X/close buttons line up with Terminal/active pills).
        # Scale with the main font so rows grow with zoom.
        cell_h = max(CELL_BTN_H, int(CELL_BTN_H * self._main_font_size
                                     / current_theme().font_size_base))
        for btn in widget.findChildren(QPushButton):
            btn.setFixedHeight(cell_h)
        if isinstance(widget, QPushButton):
            widget.setFixedHeight(cell_h)
        lay.addWidget(widget)
        self.table.setCellWidget(row, col, wrapper)

    def _apply_hover_to_row(self, row: int, highlight: bool) -> None:
        """Toggle the hover background on all cell widgets in a row.

        The delegate paints the hover background for every cell (text
        and widget).  Widget cells need their children made transparent
        so the delegate background shows through uniformly.
        """
        if row < 0 or row >= self.table.rowCount():
            return
        for col in range(self.table.columnCount()):
            w = self.table.cellWidget(row, col)
            if not w or w.objectName() != '_leapSep':
                continue
            # Make buttons/labels transparent so delegate bg shows
            # through.  Skip PulsingLabel / IndicatorLabel (animated
            # stylesheets that must not be overridden).
            for child in w.findChildren((QPushButton, QLabel)):
                if isinstance(child, (PulsingLabel, IndicatorLabel)):
                    continue
                if highlight:
                    orig = child.property('_origSS')
                    if orig is None:
                        orig = child.styleSheet()
                        child.setProperty('_origSS', orig)
                    if isinstance(child, QPushButton):
                        rule = ' QPushButton { background: transparent; }'
                    else:
                        rule = ' QLabel { background: transparent; }'
                    child.setStyleSheet(orig + rule)
                else:
                    orig = child.property('_origSS')
                    if orig is not None:
                        child.setStyleSheet(orig)

    def _set_cell_text(self, row: int, col: int, text: str,
                       row_color: Optional[str] = None) -> None:
        """Set cell text only if it changed, to avoid flicker."""
        item = self.table.item(row, col)
        center = col in self._CENTER_COLS or text == 'N/A'
        if item is None:
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            if center:
                item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, col, item)
        else:
            if item.text() != text:
                item.setText(text)
                item.setToolTip(text)
            alignment = Qt.AlignCenter if center else int(Qt.AlignLeft | Qt.AlignVCenter)
            if item.textAlignment() != alignment:
                item.setTextAlignment(alignment)
        # Apply monospace font to technical columns
        if col in self._MONO_COLS:
            mono = QFont('Menlo')
            mono.setStyleHint(QFont.Monospace)
            mono.setPointSize(max(10, self._zoomed_size(-1)))
            item.setFont(mono)
        # Dim 'N/A' cells
        if text == 'N/A':
            item.setForeground(QColor(current_theme().text_muted))
            return
        # Adjust foreground for row background color contrast
        if row_color:
            t = current_theme()
            fg = ensure_contrast(t.text_primary, row_color)
            item.setForeground(QColor(fg))
        else:
            item.setForeground(QColor(current_theme().text_primary))

    def _cell_cached(self, tag: str, col: str, state: tuple,
                     row: int, table_col: int) -> bool:
        """Check if a cell widget can be reused (state unchanged, same row)."""
        cached = self._cell_cache.get((tag, col))
        return (cached is not None
                and cached[0] == state
                and not sip.isdeleted(cached[1])
                and self.table.cellWidget(row, table_col) is cached[1])

    def _cache_cell(self, tag: str, col: str, state: tuple,
                    row: int, table_col: int) -> None:
        """Store the current cell widget in the cache after building it."""
        self._cell_cache[(tag, col)] = (
            state, self.table.cellWidget(row, table_col))

    def _should_show_pr_fire(self, tag: str) -> bool:
        """Return True if the PR fire indicator should be shown for *tag*."""
        threshold = self._prefs.get('new_status_seconds', 60)
        if threshold <= 0:
            return False
        if tag in self._dismissed_pr_new_status:
            return False
        entry = self._pr_changed_at.get(tag)
        if entry is None:
            return False
        changed_at = entry[1]
        return (time.time() - changed_at) < threshold

    def _pr_fire_tooltip(self, tag: str) -> str:
        """Build tooltip text for the PR fire indicator."""
        entry = self._pr_changed_at.get(tag)
        if not entry:
            return ''
        ago = int(time.time() - entry[1])
        return f'PR status changed {ago}s ago - click to dismiss'

    def _build_tag_cell(self, row: int, tag: str,
                        row_color: Optional[str] = None) -> None:
        """Build the Tag column cell: elided label + palette icon button."""
        alias = self._aliases.get(tag)
        tag_state = (tag, row_color, alias)
        if self._cell_cached(tag, 'tag', tag_state, row, self.COL_TAG):
            return

        tag_container = QWidget()
        tag_layout = QHBoxLayout(tag_container)
        tag_layout.setContentsMargins(0, 0, 0, 0)
        tag_layout.setSpacing(2)

        display_text = alias if alias else tag
        tag_label = ElidedLabel(display_text)
        tag_label.setAlignment(Qt.AlignCenter)
        t_tag = current_theme()
        if alias:
            font = tag_label.font()
            font.setItalic(True)
            tag_label.setFont(font)
        else:
            tag_label.setStyleSheet(f'QLabel {{ font-weight: 600; }}')
        tag_layout.addWidget(tag_label, 1)

        palette_btn = HoverIconButton(_PALETTE_SVG, self._zoomed_btn_w(14))
        palette_btn.setFixedSize(self._zoomed_btn_w(22),palette_btn.sizeHint().height())
        palette_btn.setStyleSheet(menu_btn_style(font_size=self._zoomed_size()))
        palette_btn.setToolTip('Set row color')
        palette_btn.clicked.connect(
            lambda checked, t=tag, btn=palette_btn:
                self._show_color_picker(t, btn)
        )
        tag_layout.addWidget(palette_btn, 0, Qt.AlignVCenter)

        # Right-click context menu for alias
        tag_container.setContextMenuPolicy(Qt.CustomContextMenu)
        tag_container.customContextMenuRequested.connect(
            lambda pos, t=tag, w=tag_container:
                self._show_tag_context_menu(t, w.mapToGlobal(pos))
        )

        # Tooltip: aliased tags always show both alias and tag.
        # Regular tags show on hover only when truncated or tooltips enabled
        # (truncation detection handled by the tooltip app for ElidedLabel).
        if alias:
            tag_label.setToolTip(f'Alias: {alias}\nTag: {tag}')
            tag_label.setProperty('always_tooltip', True)
        else:
            tag_label.setToolTip(tag)

        # Ensure a table item exists with the tooltip
        item = self.table.item(row, self.COL_TAG)
        if not item:
            item = QTableWidgetItem('')
            self.table.setItem(row, self.COL_TAG, item)
        item.setText('')
        item.setToolTip('')
        self._set_cell_widget(row, self.COL_TAG, tag_container)
        self._apply_row_color_to_widget(tag_container, row_color)
        self._cache_cell(tag, 'tag', tag_state, row, self.COL_TAG)

    def _show_tag_context_menu(self, tag: str, global_pos: QPoint) -> None:
        """Show context menu for the tag cell (set/remove alias)."""
        menu = QMenu(self)
        alias = self._aliases.get(tag)
        if alias:
            set_action = menu.addAction(f'Rename alias')
            remove_action = menu.addAction('Remove alias')
        else:
            set_action = menu.addAction('Set alias')
            remove_action = None

        action = menu.exec_(global_pos)
        if action == set_action:
            current = alias or ''
            text, ok = QInputDialog.getText(
                self, 'Set Alias', f'Alias for "{tag}":', text=current)
            if ok and text.strip():
                self._set_alias(tag, text.strip())
        elif remove_action and action == remove_action:
            self._set_alias(tag, None)

    def _set_alias(self, tag: str, alias: Optional[str]) -> None:
        """Set or clear the alias for a tag and persist."""
        if alias:
            self._aliases[tag] = alias
        else:
            self._aliases.pop(tag, None)
        self._prefs['aliases'] = self._aliases
        self._save_prefs()
        # Invalidate tag cell cache so it rebuilds
        self._cell_cache.pop((tag, 'tag'), None)
        self._update_table()

    def _show_color_picker(self, tag: str, anchor: QWidget) -> None:
        """Show the color picker popup anchored below the palette button."""
        current = self._row_colors.get(tag)
        popup = ColorPickerPopup(
            current,
            lambda color, t=tag: self._set_row_color(t, color),
            parent=self,
        )
        # Position below the anchor button
        pos = anchor.mapToGlobal(anchor.rect().bottomLeft())
        popup.move(pos)
        popup.show()

    def _set_row_color(self, tag: str, color: Optional[str]) -> None:
        """Set or clear the row color for a tag and persist."""
        if color:
            self._row_colors[tag] = color
        else:
            self._row_colors.pop(tag, None)
        self._prefs['row_colors'] = self._row_colors
        self._save_prefs()
        # Invalidate all cell caches for this tag so they rebuild with new color
        stale = [k for k in self._cell_cache if k[0] == tag]
        for k in stale:
            self._cell_cache.pop(k, None)
        # Update table property and rebuild
        self.table.setProperty('_row_colors', self._row_colors)
        self._update_table()

    def _apply_row_color_to_widget(self, widget: QWidget,
                                   row_color: Optional[str]) -> None:
        """Adjust child QLabel text and icon button colors for contrast."""
        if not row_color:
            return
        t = current_theme()
        fg = ensure_contrast(t.text_primary, row_color)
        for child in widget.findChildren(QLabel):
            if isinstance(child, (PulsingLabel, IndicatorLabel)):
                continue
            pal = child.palette()
            pal.setColor(QPalette.WindowText, QColor(fg))
            child.setPalette(pal)
        icon_fg = ensure_contrast(t.icon_color, row_color)
        if icon_fg != t.icon_color:
            for btn in widget.findChildren(HoverIconButton):
                btn.set_icon_color(icon_fg)
        # On colored rows, buttons need opaque backgrounds to stand out.
        # Use the theme's button_bg as a solid base.
        btn_bg = t.button_bg or t.window_bg
        btn_hover = t.button_hover_bg or t.border_solid
        btn_border = t.button_border or t.border_solid
        r_radius = t.border_radius
        def _solid_btn(fg: str, border_color: Optional[str] = None) -> str:
            bc = border_color or btn_border
            return (
                f'QPushButton {{ color: {fg};'
                f' background-color: {btn_bg};'
                f' border: 1px solid {bc};'
                f' border-radius: {r_radius}px;'
                f' padding: 0px 8px; }}'
                f'QPushButton:hover {{ background-color: {btn_hover};'
                f' border-color: {fg}; }}'
                f'QPushButton:disabled {{ color: {t.text_muted};'
                f' background-color: {btn_bg};'
                f' border-color: {btn_border}; }}'
            )
        for btn in widget.findChildren(QPushButton):
            if isinstance(btn, HoverIconButton):
                continue
            role = btn.property('_btn_role')
            if role == 'active':
                green_fg = ensure_contrast(t.accent_green, row_color)
                btn.setStyleSheet(_solid_btn(green_fg, green_fg))
            elif role == 'orange':
                orange_fg = ensure_contrast(t.accent_orange, row_color)
                btn.setStyleSheet(_solid_btn(orange_fg, orange_fg))
            elif role == 'menu':
                menu_fg = ensure_contrast(t.icon_color, row_color)
                btn.setStyleSheet(
                    f'QPushButton {{ color: {menu_fg};'
                    f' background-color: {btn_bg};'
                    f' border: 1px solid {btn_border};'
                    f' border-radius: {r_radius}px;'
                    f' padding: 0px 4px; }}'
                    f'QPushButton:hover {{ color: {fg};'
                    f' background-color: {btn_hover}; }}'
                )
            elif role == 'close':
                # Match close_btn_style() exactly (font-size + padding) so
                # the X glyph sits in the same spot whether or not the row
                # is colored. Only the fg color and hover behavior differ
                # from the uncolored default.
                muted_fg = ensure_contrast(t.text_muted, row_color)
                btn.setStyleSheet(
                    f'QPushButton {{ color: {muted_fg};'
                    f' font-size: {self._zoomed_size()}px;'
                    f' background-color: {btn_bg};'
                    f' border: 1px solid {btn_border};'
                    f' border-radius: {r_radius}px;'
                    f' padding: 0px 6px 1px 6px; }}'
                    f'QPushButton:hover {{ color: {t.accent_red};'
                    f' border-color: {t.accent_red}; }}'
                )
            else:
                primary_fg = ensure_contrast(t.text_primary, row_color)
                btn.setStyleSheet(_solid_btn(primary_fg))

    def _build_path_cell(self, row: int, tag: str, path_text: str,
                         row_color: Optional[str] = None) -> None:
        """Build the Path column cell: elided label + 3-dot menu button.

        The 3-dot button and right-click on the label both open the path
        actions menu (Open in Terminal, Open in IDE).  Disabled when
        path_text is 'N/A'.
        """
        path_state = (path_text, row_color)
        if self._cell_cached(tag, 'path', path_state, row, self.COL_PATH):
            return

        has_path = path_text != 'N/A'
        path_container = QWidget()
        path_layout = QHBoxLayout(path_container)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(2)

        path_label = ElidedLabel(path_text)
        path_label.setAlignment(Qt.AlignCenter)
        path_label.setToolTip(path_text)
        mono = QFont('Menlo')
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(max(10, self._zoomed_size(-1)))
        path_label.setFont(mono)
        if has_path:
            path_label.setContextMenuPolicy(Qt.CustomContextMenu)
            path_label.customContextMenuRequested.connect(
                lambda _pos, t=tag: self._show_path_menu(t)
            )
        path_layout.addWidget(path_label, 1)

        path_menu_btn = HoverIconButton(_OPEN_EXTERNAL_SVG, self._zoomed_btn_w(14))
        path_menu_btn.setFixedSize(self._zoomed_btn_w(22),path_menu_btn.sizeHint().height())
        path_menu_btn.setStyleSheet(menu_btn_style(font_size=self._zoomed_size()))
        path_menu_btn.setToolTip('Open in Terminal / IDE' if has_path
                                 else 'No project path available')
        path_menu_btn.setEnabled(has_path)
        if has_path:
            path_menu_btn.clicked.connect(
                lambda checked, t=tag: self._show_path_menu(t))
        path_layout.addWidget(path_menu_btn, 0, Qt.AlignVCenter)

        # Ensure a table item exists with the tooltip so the
        # cell-widget tooltip path can show truncated text.
        item = self.table.item(row, self.COL_PATH)
        if not item:
            item = QTableWidgetItem('')
            self.table.setItem(row, self.COL_PATH, item)
        item.setText('')
        item.setToolTip(path_text)
        self._set_cell_widget(row, self.COL_PATH, path_container)
        self._apply_row_color_to_widget(path_container, row_color)
        self._cache_cell(tag, 'path', path_state, row, self.COL_PATH)

    def _build_branch_cell(self, row: int, tag: str, branch_text: str,
                           row_color: Optional[str] = None) -> None:
        """Build the Server Branch column cell: label + git icon button.

        The git icon button and right-click on the label both open the git
        changes menu.  Disabled when branch_text is 'N/A'.
        """
        branch_state = (branch_text, row_color)
        if self._cell_cached(tag, 'server_branch', branch_state,
                             row, self.COL_SERVER_BRANCH):
            return

        has_git = branch_text != 'N/A' and self._has_git_project(tag)
        branch_container = QWidget()
        branch_layout = QHBoxLayout(branch_container)
        branch_layout.setContentsMargins(0, 0, 0, 0)
        branch_layout.setSpacing(2)

        branch_label = ElidedLabel(branch_text)
        branch_label.setAlignment(Qt.AlignCenter)
        branch_label.setToolTip(branch_text)
        mono = QFont('Menlo')
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(max(10, self._zoomed_size(-1)))
        branch_label.setFont(mono)
        if has_git:
            branch_label.setContextMenuPolicy(Qt.CustomContextMenu)
            branch_label.customContextMenuRequested.connect(
                lambda _pos, t=tag: self._show_git_menu(t)
            )
        branch_layout.addWidget(branch_label, 1)

        git_btn = HoverIconButton(_GIT_BRANCH_SVG, self._zoomed_btn_w(14))
        git_btn.setFixedSize(self._zoomed_btn_w(22),git_btn.sizeHint().height())
        git_btn.setStyleSheet(menu_btn_style(font_size=self._zoomed_size()))
        git_btn.setToolTip('Git Changes' if has_git
                           else 'No git project detected')
        git_btn.setEnabled(has_git)
        if has_git:
            git_btn.clicked.connect(
                lambda checked, t=tag: self._show_git_menu(t))
        branch_layout.addWidget(git_btn, 0, Qt.AlignVCenter)

        # Ensure a table item exists with the tooltip so the
        # cell-widget tooltip path can show truncated text.
        item = self.table.item(row, self.COL_SERVER_BRANCH)
        if not item:
            item = QTableWidgetItem('')
            self.table.setItem(row, self.COL_SERVER_BRANCH, item)
        item.setText('')
        item.setToolTip(branch_text)
        self._set_cell_widget(row, self.COL_SERVER_BRANCH, branch_container)
        self._apply_row_color_to_widget(branch_container, row_color)
        self._cache_cell(tag, 'server_branch', branch_state,
                         row, self.COL_SERVER_BRANCH)

    def _on_search_changed(self, text: str) -> None:
        """Update the search filter and rebuild the table.

        Filter is intentionally stateless across monitor restarts —
        clears every time the app is launched so the user isn't
        surprised by a stale filter hiding sessions they expect to
        see.  The substring match itself happens in
        ``_apply_search_filter`` and is wired into ``_update_table``
        via a try/finally swap so the rest of the table-build code
        path is unchanged.
        """
        self._search_query = text.strip()
        self._update_table()

    def _set_badge_cell(self, row: int, col: int, text: str,
                        row_color: Optional[str]) -> None:
        """Render a cell as the outlined-pill badge used by the CLI/App
        columns, so non-server rows match the styling of normal rows."""
        t = current_theme()
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        fg = t.text_secondary
        border_c = t.border_solid
        if row_color:
            fg = ensure_contrast(t.text_secondary, row_color)
            border_c = ensure_contrast(t.border_solid, row_color)
        label.setFixedHeight(self._zoomed_btn_w(24))
        label.setStyleSheet(
            f'QLabel {{'
            f'  color: {fg};'
            f'  border: 1px solid {border_c};'
            f'  border-radius: 12px;'
            f'  padding: 0px 10px;'
            f'  font-size: {self._zoomed_size()}px;'
            f'}}'
        )
        self._set_cell_widget(row, col, label)

    def _render_tracked_pr_cell(self, row: int, tag: str,
                                row_color: Optional[str], *,
                                on_stop: Callable[[str], None],
                                server_running: bool,
                                wire_send_callbacks: bool,
                                checking_when_no_status: bool = False) -> Optional[PRStatus]:
        """Render/refresh the tracked-PR status cell in COL_PR.

        Builds (or reuses, via the cell cache + per-tag PulsingLabel /
        IndicatorLabel) the X-to-stop + approval + status + fire layout,
        then applies the current PRStatus.  Shared by normal session rows
        and Cursor editor-tab rows; callers vary only the stop callback,
        the server-running flag, and whether the send-to-Leap callbacks
        are wired.  ``checking_when_no_status`` shows "Checking…" while a
        freshly-tracked row's first poll is still in flight (Cursor rows,
        which skip the normal ``_checking_tags`` flow).  Returns the
        applied PRStatus (or None).
        """
        pr_widget = self._pr_widgets.get(tag)
        reused_pr = bool(pr_widget) and not sip.isdeleted(pr_widget)
        if not reused_pr:
            pr_widget = PulsingLabel()
            self._pr_widgets[tag] = pr_widget
        approval_label = self._pr_approval_widgets.get(tag)
        reused_approval = bool(approval_label) and not sip.isdeleted(approval_label)
        if not reused_approval:
            approval_label = IndicatorLabel()
            self._pr_approval_widgets[tag] = approval_label

        pr_state = ('tracked', self._should_show_pr_fire(tag))
        pr_cached = (
            reused_pr and reused_approval
            and self._cell_cached(tag, 'pr', pr_state, row, self.COL_PR)
        )
        if not pr_cached:
            if self.table.columnSpan(row, self.COL_PR) > 1:
                self.table.setSpan(row, self.COL_PR, 1, 1)
            if reused_pr:
                pr_widget.set_preserve_popup(True)
            if reused_approval:
                approval_label.set_preserve_popup(True)

            pr_container = QWidget()
            pr_layout = QHBoxLayout(pr_container)
            pr_layout.setContentsMargins(0, 0, 0, 0)
            pr_layout.setSpacing(2)

            pr_x = QPushButton('×')
            pr_x.setFixedSize(self._zoomed_btn_w(28), pr_x.sizeHint().height())
            pr_x.setStyleSheet(close_btn_style(font_size=self._zoomed_size()))
            pr_x.setProperty('_btn_role', 'close')
            pr_x.setToolTip(f'Stop tracking PR for {tag}')
            pr_x.clicked.connect(lambda checked, t=tag: on_stop(t))
            pr_layout.addWidget(pr_x, 0, Qt.AlignVCenter)

            pr_layout.addStretch()
            pr_layout.addWidget(approval_label)
            pr_layout.addWidget(pr_widget)
            pr_layout.addStretch()

            show_pr_fire = self._should_show_pr_fire(tag)
            pr_fire_label = QLabel('\U0001f525' if show_pr_fire else '')
            pr_fire_label.setObjectName('_prFireLabel')
            pr_fire_px = max(10, self._zoomed_size(-3))
            pr_fire_label.setFixedWidth(int(pr_fire_px * 1.4))
            pr_fire_label.setAlignment(Qt.AlignCenter | Qt.AlignVCenter)
            if show_pr_fire:
                t_pf = current_theme()
                pf_color = (ensure_contrast(t_pf.accent_orange, row_color)
                            if row_color else t_pf.accent_orange)
                pr_fire_label.setStyleSheet(
                    f'color: {pf_color}; font-size: {pr_fire_px}px;')
                pr_fire_label.setToolTip(self._pr_fire_tooltip(tag))

            def _make_pr_dismiss(t: str = tag) -> Callable:
                def _dismiss(event: object) -> None:
                    if t not in self._dismissed_pr_new_status:
                        self._dismissed_pr_new_status.add(t)
                        self._update_table()
                return _dismiss
            pr_fire_label.mousePressEvent = _make_pr_dismiss()
            pr_layout.addWidget(pr_fire_label)

            self._set_cell_widget(row, self.COL_PR, pr_container)
            self._apply_row_color_to_widget(pr_container, row_color)
            self._cache_cell(tag, 'pr', pr_state, row, self.COL_PR)

            if reused_pr:
                pr_widget.set_preserve_popup(False)
            if reused_approval:
                approval_label.set_preserve_popup(False)

        # Always update PR widget properties (change each poll)
        pr_status = self._pr_statuses.get(tag)
        if pr_status is None and checking_when_no_status:
            pr_widget.setText('Checking…')
            pr_widget.setStyleSheet(f'color: {current_theme().text_muted};')
            pr_widget.set_pulsing(False)
            pr_widget.set_pr_url(None)
            pr_widget.set_has_unresponded(False)
        else:
            self._apply_pr_status(pr_widget, approval_label, pr_status)
            pr_widget.set_has_unresponded(
                pr_status is not None
                and pr_status.state == PRState.UNRESPONDED
            )
        pr_widget.set_server_running(server_running)
        if wire_send_callbacks and not reused_pr:
            pr_widget.set_send_to_leap_callback(
                lambda t=tag: self._send_all_threads_to_leap(t)
            )
            pr_widget.set_send_combined_to_leap_callback(
                lambda t=tag: self._send_all_threads_combined_to_leap(t)
            )
            pr_widget.set_send_leap_threads_callback(
                lambda t=tag: self._send_leap_threads_to_leap(t)
            )
            pr_widget.set_send_leap_threads_combined_callback(
                lambda t=tag: self._send_leap_threads_combined_to_leap(t)
            )
        pr_widget.set_auto_fetch_leap(
            self._prefs.get('auto_fetch_leap', False)
        )
        return pr_status

    def _build_cursor_gui_row(self, row: int, session: dict,
                              row_color: Optional[str]) -> None:
        """Render a read-only Cursor editor Agent-tab row.

        These rows have no Leap server: they show the tab's title, a
        best-effort status read from Cursor's on-disk state, and an
        "Open" button that raises the Cursor *window* (tab-level focus
        is impossible - Cursor exposes nothing clickable to
        Accessibility).  Every column is painted explicitly, clearing
        any widget left by a previous occupant of this grid row, so no
        stale cells bleed through.  Not cell-cached (few rows, cheap).
        """
        tag = session['tag']
        t = current_theme()
        # A synthesized "tab closed" row (the tab was closed via the Open-cell
        # X but the PR is still tracked, so the row stays to monitor it).
        tab_closed = bool(session.get('_tab_closed'))

        # Clear any widgets a prior occupant of this row left behind.
        # COL_PR is skipped: its PulsingLabel/IndicatorLabel are reused
        # across rebuilds (preserving pulse + hover popup), and setting a
        # fresh container below replaces the old one without deleting the
        # reparented widgets.
        for col in range(self.table.columnCount()):
            # COL_PR + COL_SERVER_BRANCH + COL_PATH are built by
            # cached/reused helpers below (PulsingLabel reuse;
            # _build_branch_cell / _build_path_cell have their own cache);
            # blanket-removing them would defeat the reuse and churn the
            # button hover state every tick.
            if col in (self.COL_PR, self.COL_SERVER_BRANCH, self.COL_PATH):
                continue
            self.table.removeCellWidget(row, col)

        # ── Delete column: the "full close" X - stops PR tracking AND
        # closes the Agent tab in Cursor (the chat stays in Cursor's
        # history), so the row goes away entirely.  This mirrors a normal
        # row's leftmost X (remove the whole row); the Open-cell X below
        # closes only the tab and keeps a tracked row alive. ──
        close_container = QWidget()
        close_layout = QHBoxLayout(close_container)
        close_layout.setContentsMargins(0, 0, 0, 0)
        close_layout.setSpacing(0)
        close_btn = QPushButton('×')
        close_btn.setFixedSize(self._zoomed_btn_w(28),
                               close_btn.sizeHint().height())
        close_btn.setStyleSheet(close_btn_style(font_size=self._zoomed_size()))
        close_btn.setProperty('_btn_role', 'close')
        close_btn.setToolTip('Stop tracking and close this Agent tab in Cursor'
                             if not tab_closed else 'Stop tracking (remove row)')
        close_btn.clicked.connect(
            lambda checked, fol=session.get('cursor_window_folder') or '',
            cid=session.get('composer_id'), tg=tag, closed=tab_closed,
            lbl=(self._aliases.get(tag)
                 or session.get('display_label') or tag):
                self._close_cursor_tab_and_untrack(fol, cid, lbl, tg, closed))
        close_layout.addWidget(close_btn, 0, Qt.AlignCenter)
        self._set_cell_widget(row, self.COL_DELETE, close_container)
        self._apply_row_color_to_widget(close_container, row_color)

        # ── Tag: Leap alias if set, else the composer-derived label.
        # Right-click offers the same Set/Rename/Remove alias menu as
        # real rows, so a Cursor tab can be named purely Leap-side
        # (persists by composer id, works even for unnamed tabs).
        alias = self._aliases.get(tag)
        display = alias or session.get('display_label') or tag
        tag_container = QWidget()
        tlay = QHBoxLayout(tag_container)
        tlay.setContentsMargins(0, 0, 0, 0)
        tlay.setSpacing(2)
        tag_label = ElidedLabel(display)
        tag_label.setAlignment(Qt.AlignCenter)
        # Italic ONLY when a Leap alias is set - same convention as normal
        # rows (alias = italic).  The default chat name stays upright: the
        # auto-generated Cursor name shouldn't read as a user alias, and the
        # CLI column ("Cursor Editor") already signals this is an editor tab.
        if alias:
            font = tag_label.font()
            font.setItalic(True)
            tag_label.setFont(font)
        tip = ('Cursor Agent tab (read-only)\nChat: '
               + (session.get('composer_name') or 'New Agent'))
        if alias:
            tip = f'Alias: {alias}\n' + tip
        tag_label.setToolTip(tip)
        tag_label.setProperty('always_tooltip', True)
        tlay.addWidget(tag_label, 1)
        # Row-color picker - same as normal rows (color persists by
        # composer id via _row_colors; the SeparatorDelegate fills the row).
        palette_btn = HoverIconButton(_PALETTE_SVG, self._zoomed_btn_w(14))
        palette_btn.setFixedSize(self._zoomed_btn_w(22),
                                 palette_btn.sizeHint().height())
        palette_btn.setStyleSheet(menu_btn_style(font_size=self._zoomed_size()))
        palette_btn.setToolTip('Set row color')
        palette_btn.clicked.connect(
            lambda checked, t=tag, btn=palette_btn:
                self._show_color_picker(t, btn))
        tlay.addWidget(palette_btn, 0, Qt.AlignVCenter)
        tag_container.setContextMenuPolicy(Qt.CustomContextMenu)
        tag_container.customContextMenuRequested.connect(
            lambda pos, _t=tag, w=tag_container:
                self._show_tag_context_menu(_t, w.mapToGlobal(pos)))
        self._set_cell_widget(row, self.COL_TAG, tag_container)
        self._apply_row_color_to_widget(tag_container, row_color)

        # ── CLI / App as outlined-pill badges (match normal rows) ──
        self._set_badge_cell(row, self.COL_CLI, 'Cursor Editor', row_color)
        self._set_badge_cell(row, self.COL_APP, 'Cursor', row_color)
        self._set_cell_text(row, self.COL_PROJECT,
                            session.get('project') or 'N/A', row_color)
        # Last Msg: the most recent user prompt in this Agent tab (read
        # from Cursor's message bubbles on disk; blank if none readable).
        self._set_cell_text(row, self.COL_TASK,
                            session.get('last_msg') or '', row_color)
        # Path: same label + actions button as normal rows (Open in
        # Terminal / Open in IDE - both operate on this row's folder).
        self._build_path_cell(row, tag, session.get('project_path') or 'N/A',
                              row_color)
        # Server Branch: same label + git-changes button as normal rows
        # (the git menu resolves this row's project_path; the button is
        # disabled for a non-git folder, i.e. when branch is 'N/A').
        self._build_branch_cell(row, tag, session.get('branch') or 'N/A',
                                row_color)

        # ── Server column → close-tab "×" + window-level "Open" jump,
        # mirroring a normal running row's [× | Terminal] layout.  The ×
        # closes ONLY the Cursor Agent tab (PR tracking stays, so a tracked
        # row survives as a "tab closed" row).  Hidden once the tab is
        # already closed - there's nothing left to close. ──
        folder = session.get('cursor_window_folder') or ''
        jump_container = QWidget()
        jlay = QHBoxLayout(jump_container)
        jlay.setContentsMargins(0, 0, 0, 0)
        jlay.setSpacing(2)
        if not tab_closed:
            srv_close_btn = QPushButton('×')
            srv_close_btn.setFixedSize(self._zoomed_btn_w(28),
                                       srv_close_btn.sizeHint().height())
            srv_close_btn.setStyleSheet(
                close_btn_style(font_size=self._zoomed_size()))
            srv_close_btn.setProperty('_btn_role', 'close')
            srv_close_btn.setToolTip('Close only this Agent tab in Cursor '
                                     '(keeps PR tracking)')
            srv_close_btn.clicked.connect(
                lambda checked, fol=folder,
                cid=session.get('composer_id'),
                lbl=(self._aliases.get(tag)
                     or session.get('display_label') or tag):
                    self._close_cursor_tab(fol, cid, lbl))
            jlay.addWidget(srv_close_btn, 0, Qt.AlignVCenter)
        jump_btn = QPushButton('Open')
        jump_btn.setStyleSheet(active_btn_style())
        jump_btn.setProperty('_btn_role', 'active')
        proj = session.get('project') or 'this project'
        jump_btn.setToolTip(
            f'Reopen this Agent tab in Cursor ({proj})' if tab_closed
            else f'Open the Cursor window for {proj} and focus this Agent tab')
        jump_btn.clicked.connect(
            lambda checked, fol=folder, cid=session.get('composer_id'):
                self._jump_to_cursor_window(fol, cid))
        jlay.addWidget(jump_btn)
        self._set_cell_widget(row, self.COL_SERVER, jump_container)
        self._apply_row_color_to_widget(jump_container, row_color)

        # ── Status (custom kind → theme-color mapping) ──
        kind = session.get('status_kind', 'idle')
        status_text = session.get('status_text', '')
        color_by_kind = {
            'running': t.status_running,
            'unread': t.status_input,
            'idle': t.status_idle,
        }
        color = color_by_kind.get(kind, t.status_idle)
        if row_color:
            color = ensure_contrast(color, row_color)
        st_container = QWidget()
        slay = QHBoxLayout(st_container)
        slay.setContentsMargins(0, 0, 2, 0)
        slay.setSpacing(0)
        st_label = ElidedLabel(status_text)
        st_label.setAlignment(Qt.AlignCenter)
        pal = st_label.palette()
        pal.setColor(QPalette.WindowText, QColor(color))
        st_label.setPalette(pal)
        sf = st_label.font()
        sf.setBold(True)
        st_label.setFont(sf)
        st_label.setToolTip(status_text)
        slay.addWidget(st_label, 1)
        s_item = self.table.item(row, self.COL_STATUS)
        if not s_item:
            s_item = QTableWidgetItem('')
            self.table.setItem(row, self.COL_STATUS, s_item)
        s_item.setText('')
        self._set_cell_widget(row, self.COL_STATUS, st_container)
        # NOTE: do NOT call _apply_row_color_to_widget here - it overrides
        # every child QLabel's color to the generic text color, which would
        # clobber the status-specific color (running/idle/unread) we just
        # set above (already contrast-adjusted for row_color).  This matches
        # the normal status cell, which bakes the color and skips it too.

        # ── Queue: always N/A (a Cursor tab has no Leap queue behind it).
        # Rendered exactly like the PR Branch column's N/A - dimmed +
        # centered (both via _set_cell_text's N/A handling) + monospace.
        # COL_QUEUE isn't a _MONO_COLS column (normal rows overlay a widget
        # there, so their item font never shows), so apply the same Menlo
        # font _set_cell_text uses for the mono columns here. ──
        self._set_cell_text(row, self.COL_QUEUE, 'N/A', row_color)
        q_item = self.table.item(row, self.COL_QUEUE)
        if q_item is not None:
            mono = QFont('Menlo')
            mono.setStyleHint(QFont.Monospace)
            mono.setPointSize(max(10, self._zoomed_size(-1)))
            q_item.setFont(mono)
        # ── Client / Slack: blank (no Leap server behind a tab) ──
        for col in (self.COL_CLIENT, self.COL_SLACK):
            self._set_cell_text(row, col, '', row_color)

        # ── PR cell: opt-in tracking, exactly like normal rows ──
        # Untracked -> "Track PR" button; tracked -> live status (reusing
        # the per-tag PulsingLabel so pulse/popup survive the 1s rebuilds)
        # plus an X to stop.  Cursor rows are tracked in-memory only
        # (their tag goes in _tracked_tags; they are NEVER pinned).
        pr_status = None
        if tag in self._tracked_tags:
            # Shared tracked-PR cell (same widget reuse / pulse / fire as
            # normal rows).  Cursor differences: stop = _untrack_cursor_pr
            # (no unpin), no Leap server, no send-to-Leap callbacks, and
            # "Checking…" until the first poll returns.
            pr_status = self._render_tracked_pr_cell(
                row, tag, row_color,
                on_stop=self._untrack_cursor_pr,
                server_running=False,
                wire_send_callbacks=False,
                checking_when_no_status=True,
            )
        else:
            # Untracked -> "Track PR" button (opt-in, like normal rows).
            self._pr_widgets.pop(tag, None)
            self._pr_approval_widgets.pop(tag, None)
            track_container = QWidget()
            track_layout = QHBoxLayout(track_container)
            track_layout.setContentsMargins(0, 0, 0, 0)
            track_layout.setSpacing(2)
            track_btn = QPushButton('Track PR')
            track_btn.setStyleSheet(inactive_btn_style())
            track_btn.setToolTip("Track this Agent tab's branch PR")
            track_btn.clicked.connect(
                lambda checked, t=tag: self._track_cursor_pr(t))
            track_layout.addWidget(track_btn)
            self._set_cell_widget(row, self.COL_PR, track_container)
            self._apply_row_color_to_widget(track_container, row_color)

        # ── PR Branch: the PR's source branch when an open PR exists,
        # else N/A (dimmed) - matches normal rows. ──
        has_pr = pr_status is not None and pr_status.state in (
            PRState.UNRESPONDED, PRState.ALL_RESPONDED)
        self._set_cell_text(
            row, self.COL_PR_BRANCH,
            (session.get('branch') or 'N/A') if has_pr else 'N/A', row_color)

    def _jump_to_cursor_window(self, folder: str,
                               composer_id: Optional[str] = None) -> None:
        """Raise the Cursor window for *folder* and focus the Agent tab.

        Window raise is via the System Events bridge; tab-level focus is
        handed off to the Leap Cursor extension (best-effort).  Runs in a
        background thread so the osascript call doesn't block the UI.
        """
        if not folder:
            self._show_status('No project folder recorded for this Cursor tab')
            return
        self._show_status(
            f'Opening Cursor window for {Path(folder).name}...')
        worker = BackgroundCallWorker(
            lambda: focus_cursor_window(folder, composer_id), self)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _close_cursor_tab(self, folder: str, composer_id: Optional[str],
                          label: str) -> None:
        """Close a Cursor Agent tab (with confirmation, background).

        Acts on the Cursor app - distinct from the normal row X (which
        just removes the monitor row) - so it confirms first.
        """
        if not composer_id:
            self._show_status('No composer id recorded for this Cursor tab')
            return
        reply = QMessageBox.question(
            self, 'Close Cursor Agent Tab',
            f"Close the Cursor Agent tab '{label}'?\n\n"
            "This closes the tab in Cursor. The chat stays in Cursor's "
            "history, so you can reopen it there.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._show_status(f"Closing Cursor tab '{label}'...")
        worker = BackgroundCallWorker(
            lambda: close_cursor_composer(folder, composer_id), self)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _close_cursor_tab_and_untrack(self, folder: str,
                                      composer_id: Optional[str], label: str,
                                      tag: str, tab_closed: bool) -> None:
        """Leftmost-X action: stop PR tracking AND close the Agent tab, so
        the row goes away entirely (mirror of a normal row's delete-X).

        For an already-closed tab (a synthesized "tab closed" row that only
        persists because it's tracked) there's no tab to close - it just
        stops tracking, dropping the row.
        """
        if tab_closed:
            self._untrack_cursor_pr(tag)  # nothing to close; just drop it
            return
        if not composer_id:
            self._show_status('No composer id recorded for this Cursor tab')
            return
        tracked = tag in self._tracked_tags
        extra = ' and stop tracking its PR' if tracked else ''
        reply = QMessageBox.question(
            self, 'Close Cursor Agent Tab',
            f"Close the Cursor Agent tab '{label}'{extra}?\n\n"
            "This closes the tab in Cursor (the chat stays in history) and "
            "removes the row from Leap.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        # Stop tracking first (also drops the cache so no "tab closed" row is
        # synthesized once the tab leaves the next scan), then close the tab.
        self._untrack_cursor_pr(tag)
        self._show_status(f"Closing Cursor tab '{label}'...")
        worker = BackgroundCallWorker(
            lambda: close_cursor_composer(folder, composer_id), self)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _track_cursor_pr(self, tag: str) -> None:
        """Start tracking a Cursor row's branch PR (opt-in, in-memory).

        Unlike `_start_tracking` (for real sessions), this never writes to
        pinned_sessions.json - the tag just joins `_tracked_tags`, the SCM
        poll picks it up (see `_start_scm_poll`), and the next poll fills
        in the live status.
        """
        if not self._scm_providers:
            QMessageBox.information(
                self, 'No SCM Connected',
                'Connect to GitLab or GitHub first using the buttons at '
                'the bottom.')
            return
        self._tracked_tags.add(tag)
        self._show_status('Tracking PR for this Cursor tab...')
        self._sync_scm_poll_timer()
        self._start_scm_poll()  # kick an immediate poll
        self._update_table()

    def _untrack_cursor_pr(self, tag: str) -> None:
        """Stop tracking a Cursor row's PR (mirror of `_track_cursor_pr`)."""
        self._tracked_tags.discard(tag)
        self._pr_statuses.pop(tag, None)
        self._pr_widgets.pop(tag, None)
        self._pr_approval_widgets.pop(tag, None)
        self._pr_changed_at.pop(tag, None)
        self._dismissed_pr_new_status.discard(tag)
        # Drop the cached row so a closed-tab tag isn't re-synthesized once
        # it's no longer tracked (for a live tab it's harmlessly re-cached on
        # the next scan).
        self._cursor_row_cache.pop(tag, None)
        # A synthesized "tab closed" row exists ONLY because it was tracked,
        # so drop it from the overlay right now - otherwise the immediate
        # _update_table() below would re-render it (untracked) as a stray
        # "Tab closed" row with a "Track PR" button until the next scan
        # reconciles it away.  A live row (no `_tab_closed`) is left in place
        # so it stays visible with a "Track PR" button while its tab is open.
        self._cursor_gui_rows = [
            r for r in (getattr(self, '_cursor_gui_rows', []) or [])
            if not (r.get('tag') == tag and r.get('_tab_closed'))
        ]
        self._sync_scm_poll_timer()
        self._update_table()

    def _apply_search_filter(
        self, sessions: list[dict],
    ) -> list[dict]:
        """Return *sessions* filtered by ``self._search_query``.

        Mirrors the Resume dialog's filter: rows are bucketed by
        where the query first matches (priority order Tag → Project
        → App → CLI → Path), then each bucket is sorted by the
        position the row already held in *sessions* so the user's
        manual row order survives the filter.  Empty query short-
        circuits to the input list unchanged.

        Match targets per row:

        * Tag — the raw tag, any user-set alias, and (for Cursor GUI
          rows) the ``display_label`` shown in the Tag column, so a
          filter on the chat name the user actually sees matches.
        * Project — ``s['project']`` (already the git project name,
          basename-equivalent — same data the Project column shows).
        * App — ``s['ide']`` (the terminal/IDE the session was last
          launched from).
        * CLI — ``s['cli_provider']`` AND its display name (e.g.
          ``claude`` and ``Claude Code`` both match).
        * Path — ``s['project_path']`` (full git root; kept last
          because it's the longest field and most likely to match
          incidentally on a fragment that means something else).
        """
        q = self._search_query.lower()
        if not q:
            return sessions
        tag_hits: list[tuple[int, dict]] = []
        project_hits: list[tuple[int, dict]] = []
        app_hits: list[tuple[int, dict]] = []
        cli_hits: list[tuple[int, dict]] = []
        path_hits: list[tuple[int, dict]] = []
        aliases = getattr(self, '_aliases', {}) or {}
        for i, s in enumerate(sessions):
            tag = s.get('tag', '') or ''
            alias = aliases.get(tag, '') or ''
            # Cursor GUI rows show their chat name (display_label), not the
            # raw cursor-gui:<id> tag, in the Tag column — match it so the
            # filter finds what the user actually sees.
            label = s.get('display_label', '') or ''
            project = s.get('project', '') or ''
            ide = s.get('ide', '') or ''
            cli = s.get('cli_provider', '') or ''
            try:
                cli_display = get_display_name(cli) if cli else ''
            except Exception:
                cli_display = ''
            project_path = s.get('project_path', '') or ''
            if (q in tag.lower() or (alias and q in alias.lower())
                    or (label and q in label.lower())):
                tag_hits.append((i, s))
            elif q in project.lower():
                project_hits.append((i, s))
            elif q in ide.lower():
                app_hits.append((i, s))
            elif q in cli.lower() or q in cli_display.lower():
                cli_hits.append((i, s))
            elif q in project_path.lower():
                path_hits.append((i, s))
        merged = tag_hits + project_hits + app_hits + cli_hits + path_hits
        return [s for _, s in merged]

    def _update_table(self) -> None:
        """Update table with current sessions.

        Cell widgets for button columns (Delete, Server, Client, PR) are
        cached by ``(tag, column)`` with a state key.  When the state key
        matches and the widget is still at the correct row, the cell is
        left untouched — preserving active tooltips.  When the state
        changes, the cell is rebuilt from scratch and re-cached.

        PR status widgets (PulsingLabel, IndicatorLabel) are additionally
        cached in ``_pr_widgets`` / ``_pr_approval_widgets`` to preserve
        hover popups via ``set_preserve_popup()``.

        While the search box is non-empty, ``self.sessions`` is
        temporarily swapped to the filtered subset so the rest of
        this method's logic (row count, cell building, _row_tags
        property) sees only the visible rows.  The swap is undone in
        the outer ``finally`` so every other code path on the
        monitor — drag-drop, PR tracking, sleep guard — keeps seeing
        the full session list.
        """
        _full_sessions = self.sessions
        # Overlay read-only Cursor editor Agent-tab rows after the real
        # sessions.  They render last (highest row indices), so drag-drop
        # - which maps visible rows to ``self.sessions`` and is restored
        # to the leap-only list in the ``finally`` - never targets them
        # (their index is >= len(self.sessions), and the drag guards bail).
        cursor_rows = getattr(self, '_cursor_gui_rows', []) or []
        self.sessions = self._apply_search_filter(_full_sessions + cursor_rows)
        try:
            self._update_table_body()
        finally:
            self.sessions = _full_sessions

    def _update_table_body(self) -> None:
        """Render the table from the current ``self.sessions`` view.

        Split out from ``_update_table`` so the outer wrapper can swap
        in the filtered session view via try/finally without touching
        the body's existing structure.  All references to
        ``self.sessions`` inside this method are intentional — they
        read whatever the outer wrapper installed (filtered or full).
        """
        # Hide drag-drop indicator during table refresh (safety net)
        if hasattr(self, '_drop_indicator') and self._drop_indicator:
            self._drop_indicator.setVisible(False)
        new_count = len(self.sessions)

        self.table.setUpdatesEnabled(False)
        # Suppress tooltip events during rebuild — destroying cell widgets
        # can trigger nested tooltip dispatches on stale C++ pointers,
        # causing a segfault in QToolTip::showText().
        app = QApplication.instance()
        tooltips_were_enabled = getattr(app, '_suppress_tooltips', False)
        app._suppress_tooltips = True
        try:
            # Track which cached PR widgets are stale (tag no longer in table).
            # Widgets for still-present tracked tags are reused to preserve
            # hover popups across table rebuilds.
            stale_pr_tags = set(self._pr_widgets.keys())

            if not self.sessions:
                # ─── Transition: populated → empty ───────────────
                # Snapshot widths, capture COL_DELETE's resize mode
                # (the only non-Interactive column in this table),
                # switch it to Interactive, and apply the saved
                # widths — ALL before the setRowCount(1) /
                # removeCellWidget calls below.  Doing it up-front
                # means those calls run against a fully-Interactive
                # header, so Qt's auto-resize can't shrink COL_DELETE
                # when the X-button widget is removed.  We guard the
                # setColumnWidth loop with ``_resizing_columns`` so
                # ``_on_section_resized`` doesn't fire after each
                # call and redistribute — without that guard, the
                # handler treats each restored width as a fresh user
                # resize, accumulates overflow, and caps PR_BRANCH
                # down to 30 px (making it visually vanish).
                if getattr(self, '_last_render_was_populated', False):
                    self._last_populated_widths = [
                        self.table.columnWidth(c)
                        for c in range(self.table.columnCount())
                    ]
                    header = self.table.horizontalHeader()
                    self._saved_col_delete_mode = (
                        header.sectionResizeMode(self.COL_DELETE))
                    header.setSectionResizeMode(
                        self.COL_DELETE, QHeaderView.Interactive)
                    prior_resizing = getattr(
                        self, '_resizing_columns', False)
                    self._resizing_columns = True
                    try:
                        for c, w in enumerate(self._last_populated_widths):
                            if 0 <= c < self.table.columnCount() and w > 0:
                                self.table.setColumnWidth(c, w)
                    finally:
                        self._resizing_columns = prior_resizing
                self._last_render_was_populated = False

                # All PR widgets are stale — stop pulsing and clear
                for w in self._pr_widgets.values():
                    try:
                        w.set_pulsing(False)
                    except RuntimeError:
                        pass
                self._pr_widgets.clear()
                self._pr_approval_widgets.clear()
                self._cell_cache.clear()
                self.table.setRowCount(1)
                self.table.setRowHeight(0, 80)
                # Clear row tags so SeparatorDelegate won't paint stale row colors
                self.table.setProperty('_row_tags', [])
                for col in range(self.table.columnCount()):
                    self.table.removeCellWidget(0, col)
                total_cols = self.table.columnCount()
                # Span the entire row so no column separators are visible
                self.table.setSpan(0, 0, 1, total_cols)
                item = self.table.item(0, 0)
                t_empty = current_theme()
                # Distinguish "really no sessions" from "filter hid them
                # all" — without this the user can't tell if their search
                # missed everything or the monitor just hasn't picked up
                # any servers yet.
                if self._search_query:
                    empty_text = 'No matching sessions'
                else:
                    empty_text = 'No active sessions'
                if not item:
                    item = QTableWidgetItem(empty_text)
                    self.table.setItem(0, 0, item)
                elif item.text() != empty_text:
                    item.setText(empty_text)
                item.setTextAlignment(Qt.AlignCenter)
                item.setForeground(QColor(t_empty.text_muted))
                font = item.font()
                font.setPointSize(self._zoomed_size(2))
                item.setFont(font)
                return

            # Reset the full-row span and placeholder text from the empty state.
            # Also restore row 0's height — the empty branch pins it to 80 px
            # via setRowHeight, which survives setRowCount() and isn't touched
            # by setDefaultSectionSize (only affects rows without explicit
            # heights), so without this reset row 0 stays oversized forever.
            if self.table.columnSpan(0, 0) > 1:
                self.table.setSpan(0, 0, 1, 1)
                item = self.table.item(0, 0)
                # Clear any empty-state placeholder unconditionally — the
                # transparent X-button widget that gets layered on top of
                # COL_DELETE doesn't fully cover the cell, so leftover text
                # bleeds through around the button.  We check for both
                # known placeholder strings ("No active sessions" and
                # "No matching sessions") and any future variants by
                # treating ANY pre-existing text on cell (0,0) as stale
                # placeholder copy that must go.
                if item and item.text():
                    item.setText('')
                self.table.setRowHeight(
                    0, self.table.verticalHeader().defaultSectionSize())

            # ─── Transition: empty → populated ───────────────────
            # Restore COL_DELETE to its original resize mode (typically
            # ResizeToContents) so the column auto-fits the X-button
            # widgets we're about to set on each row.  We saved the
            # original mode in the empty branch and switched COL_DELETE
            # to Interactive to keep the saved width stable; switching
            # back here lets the explicit ``resizeColumnToContents``
            # call at the end of this branch and the cell-widget
            # measurements take over.
            saved_mode = getattr(self, '_saved_col_delete_mode', None)
            if saved_mode is not None:
                self.table.horizontalHeader().setSectionResizeMode(
                    self.COL_DELETE, saved_mode)
                self._saved_col_delete_mode = None

            self.table.setRowCount(new_count)

            # Update row_tags property for SeparatorDelegate row coloring
            self.table.setProperty(
                '_row_tags', [s['tag'] for s in self.sessions])

            # Clear starting guard for tags whose server is now running
            if self._starting_tags:
                alive = {s['tag'] for s in self.sessions if s.get('server_pid')}
                self._starting_tags -= alive

            for row, session in enumerate(self.sessions):
                tag = session['tag']
                row_color = self._row_colors.get(tag)
                # Read-only Cursor editor Agent-tab rows are rendered by a
                # dedicated, self-contained helper (paints all columns,
                # no server semantics) and skip the normal cell pipeline.
                if session.get('row_type') == CURSOR_GUI_ROW_TYPE:
                    # This row's PR PulsingLabel/IndicatorLabel are reused
                    # across rebuilds (like normal rows) - keep them out of
                    # the stale-cleanup below, which would otherwise pop
                    # them and stop the pulse every 1s.
                    stale_pr_tags.discard(tag)
                    self._build_cursor_gui_row(row, session, row_color)
                    continue
                server_pid = session.get('server_pid')
                is_dead = server_pid is None
                client_pid = session.get('client_pid')
                has_client = session.get('has_client', False)
                pinned_data = self._pinned_sessions.get(tag, {})
                pinned_branch = pinned_data.get('branch', '')

                # ── Delete button ──────────────────────────────────
                del_state = ()  # never changes for a given tag
                if not self._cell_cached(tag, 'del', del_state,
                                         row, self.COL_DELETE):
                    del_container = QWidget()
                    del_layout = QHBoxLayout(del_container)
                    del_layout.setContentsMargins(0, 0, 0, 0)
                    del_layout.setSpacing(0)
                    del_btn = QPushButton('\u00d7')
                    del_btn.setFixedSize(self._zoomed_btn_w(28),del_btn.sizeHint().height())
                    del_btn.setStyleSheet(close_btn_style(font_size=self._zoomed_size()))
                    del_btn.setProperty('_btn_role', 'close')
                    del_btn.setToolTip(f'Remove row for {tag}')
                    del_btn.clicked.connect(
                        lambda checked, t=tag: self._delete_row(t)
                    )
                    del_layout.addWidget(del_btn, 0, Qt.AlignCenter)
                    self._set_cell_widget(row, self.COL_DELETE, del_container)
                    self._apply_row_color_to_widget(del_container, row_color)
                    self._cache_cell(tag, 'del', del_state,
                                     row, self.COL_DELETE)

                # ── Tag cell (elided label + palette icon) ──────────
                self._build_tag_cell(row, tag, row_color)

                # ── CLI cell ────────────────────────────────────────
                cli_provider = session.get('cli_provider', DEFAULT_PROVIDER)
                cli_display = get_display_name(cli_provider)
                if is_dead:
                    # For dead rows, try metadata fallback
                    pinned_cli = pinned_data.get('cli_provider', '')
                    cli_display = get_display_name(pinned_cli) if pinned_cli else 'N/A'
                # CLI column — show as a subtle outlined badge
                cli_state_key = (cli_display, row_color)
                if not self._cell_cached(tag, 'cli', cli_state_key,
                                         row, self.COL_CLI):
                    t_cli = current_theme()
                    cli_label = QLabel(cli_display)
                    cli_label.setAlignment(Qt.AlignCenter)
                    if cli_display != 'N/A':
                        fg = t_cli.text_secondary
                        border_c = t_cli.border_solid
                        if row_color:
                            fg = ensure_contrast(t_cli.text_secondary, row_color)
                            border_c = ensure_contrast(t_cli.border_solid, row_color)
                        cli_label.setFixedHeight(self._zoomed_btn_w(24))
                        cli_label.setStyleSheet(
                            f'QLabel {{'
                            f'  color: {fg};'
                            f'  border: 1px solid {border_c};'
                            f'  border-radius: 12px;'
                            f'  padding: 0px 10px;'
                            f'  font-size: {self._zoomed_size()}px;'
                            f'}}'
                        )
                    self._set_cell_widget(row, self.COL_CLI, cli_label)
                    self._cache_cell(tag, 'cli', cli_state_key,
                                     row, self.COL_CLI)

                # ── App cell ───────────────────────────────────────
                # The terminal/IDE app the session is running in.  For
                # live rows it comes from ``<tag>.meta`` (populated by
                # ``detect_ide()`` at server start).  For dead rows we
                # don't persist it anywhere so it falls back to N/A.
                app_display = session.get('ide') or 'N/A'
                app_state_key = (app_display, row_color)
                if not self._cell_cached(tag, 'app', app_state_key,
                                         row, self.COL_APP):
                    t_app = current_theme()
                    app_label = QLabel(app_display)
                    app_label.setAlignment(Qt.AlignCenter)
                    if app_display != 'N/A':
                        fg = t_app.text_secondary
                        border_c = t_app.border_solid
                        if row_color:
                            fg = ensure_contrast(t_app.text_secondary, row_color)
                            border_c = ensure_contrast(t_app.border_solid, row_color)
                        app_label.setFixedHeight(self._zoomed_btn_w(24))
                        app_label.setStyleSheet(
                            f'QLabel {{'
                            f'  color: {fg};'
                            f'  border: 1px solid {border_c};'
                            f'  border-radius: 12px;'
                            f'  padding: 0px 10px;'
                            f'  font-size: {self._zoomed_size()}px;'
                            f'}}'
                        )
                    self._set_cell_widget(row, self.COL_APP, app_label)
                    self._cache_cell(tag, 'app', app_state_key,
                                     row, self.COL_APP)

                # Server Branch always shows the live branch
                server_branch = session['branch']

                # PR Branch shows the PR's source branch if tracked
                if pinned_data.get('remote_project_path'):
                    pr_branch = pinned_branch or 'N/A'
                else:
                    pr_branch = 'N/A'

                if is_dead:
                    remote_path = pinned_data.get('remote_project_path', '')
                    dead_project = (remote_path.rsplit('/', 1)[-1]
                                    if remote_path
                                    else 'N/A')
                    self._set_cell_text(row, self.COL_PROJECT, dead_project,
                                        row_color)
                    self._build_path_cell(row, tag, 'N/A', row_color)
                    self._build_branch_cell(row, tag, 'N/A', row_color)
                    # Remove the live status cell widget (coloured
                    # indicator + label) before switching to plain text,
                    # otherwise the old widget renders on top of "N/A".
                    self.table.removeCellWidget(row, self.COL_STATUS)
                    self._cell_cache.pop((tag, 'status'), None)
                    self._set_cell_text(row, self.COL_STATUS, 'N/A',
                                        row_color)
                    status_item = self.table.item(row, self.COL_STATUS)
                    if status_item and not row_color:
                        status_item.setForeground(QColor(current_theme().text_primary))

                    self._set_cell_text(row, self.COL_TASK, 'N/A',
                                        row_color)

                    # Queue N/A with menu button
                    dead_q_state = ('dead', session.get('auto_send_mode', AutoSendMode.PAUSE),
                                    row_color)
                    if not self._cell_cached(tag, 'queue', dead_q_state,
                                             row, self.COL_QUEUE):
                        dq_container = QWidget()
                        dq_layout = QHBoxLayout(dq_container)
                        dq_layout.setContentsMargins(0, 0, 0, 0)
                        dq_layout.setSpacing(2)

                        dq_menu_btn = HoverIconButton(_THREE_DOT_SVG, self._zoomed_btn_w(14))
                        dq_menu_btn.setFixedSize(
                            self._zoomed_btn_w(24),
                            dq_menu_btn.sizeHint().height())
                        dq_menu_btn.setStyleSheet(menu_btn_style(font_size=self._zoomed_size()))
                        dq_menu_btn.setToolTip('Queue options')
                        dq_menu_btn.clicked.connect(
                            lambda checked, btn=dq_menu_btn, t=tag:
                                self._show_queue_context_menu(
                                    btn, btn.rect().bottomLeft(), t)
                        )
                        dq_layout.addWidget(
                            dq_menu_btn, 0, Qt.AlignVCenter)

                        dq_label = QLabel('N/A')
                        dq_label.setAlignment(Qt.AlignCenter)
                        dq_layout.addWidget(dq_label, 1)

                        dq_action_btn = HoverIconButton(_SEND_SVG, self._zoomed_btn_w(14))
                        dq_action_btn.setFixedSize(
                            self._zoomed_btn_w(24),
                            dq_action_btn.sizeHint().height())
                        dq_action_btn.setStyleSheet(menu_btn_style(font_size=self._zoomed_size()))
                        dq_action_btn.setEnabled(False)
                        dq_action_btn.setToolTip('Send options (server offline)')
                        dq_layout.addWidget(
                            dq_action_btn, 0, Qt.AlignVCenter)

                        item = self.table.item(row, self.COL_QUEUE)
                        if item:
                            item.setText('')
                        self._set_cell_widget(
                            row, self.COL_QUEUE, dq_container)
                        self._apply_row_color_to_widget(
                            dq_container, row_color)
                        self._cache_cell(tag, 'queue', dead_q_state,
                                         row, self.COL_QUEUE)
                else:
                    self._set_cell_text(row, self.COL_PROJECT,
                                        session['project'], row_color)
                    live_path = session.get('project_path', '') or ''
                    self._build_path_cell(row, tag, live_path or 'N/A',
                                         row_color)
                    self._build_branch_cell(row, tag, server_branch,
                                            row_color)

                    cli_state = session.get('cli_state', CLIState.IDLE)
                    t = current_theme()
                    state_display = {
                        CLIState.IDLE: ('\u25cb  Idle', QColor(t.status_idle)),
                        CLIState.RUNNING: ('\u25cf  Running', QColor(t.status_running)),
                        CLIState.NEEDS_PERMISSION: ('\u25b2  Permission', QColor(t.status_permission)),
                        CLIState.NEEDS_INPUT: ('\u25c6  Question', QColor(t.status_input)),
                        CLIState.INTERRUPTED: ('\u25c7  Interrupted', QColor(t.status_interrupted)),
                    }
                    text, color = state_display.get(cli_state, (cli_state, QColor(t.status_idle)))

                    # Track state changes and show fire indicator for recent ones
                    prev = self._state_changed_at.get(tag)
                    now = time.time()
                    if prev is None:
                        # First time seeing this tag — seed with epoch 0
                        # so the fire indicator doesn't flash on startup.
                        self._state_changed_at[tag] = (cli_state, 0)
                    elif prev[0] != cli_state:
                        self._state_changed_at[tag] = (cli_state, now)
                        # Reset dismissal when state changes again
                        self._dismissed_new_status.discard(tag)
                    show_fire = False
                    threshold = self._prefs.get('new_status_seconds', 60)
                    if (
                        threshold > 0
                        and cli_state not in (CLIState.RUNNING, CLIState.INTERRUPTED)
                        and tag not in self._dismissed_new_status
                    ):
                        changed_at = self._state_changed_at[tag][1]
                        if (now - changed_at) < threshold:
                            show_fire = True

                    state_explanations = {
                        CLIState.IDLE: 'Waiting for input - will accept next queued message',
                        CLIState.RUNNING: 'Actively processing a request',
                        CLIState.NEEDS_PERMISSION: 'Needs your permission to use a tool',
                        CLIState.NEEDS_INPUT: 'Asking you a question',
                        CLIState.INTERRUPTED: 'Was interrupted - will accept next queued message',
                    }

                    # Adjust status color for row background contrast
                    if row_color:
                        adjusted = ensure_contrast(color.name(), row_color)
                        color = QColor(adjusted)
                    color_key = color.name()
                    status_state = (text, show_fire, color_key, row_color)
                    if not self._cell_cached(tag, 'status', status_state,
                                             row, self.COL_STATUS):
                        container = QWidget()
                        c_layout = QHBoxLayout(container)
                        c_layout.setContentsMargins(0, 0, 2, 0)
                        c_layout.setSpacing(0)

                        # Left spacer balances the indicator dot width
                        spacer = QWidget()
                        spacer.setFixedWidth(int(max(10, self._zoomed_size(-3)) * 1.4))
                        c_layout.addWidget(spacer)

                        # Status text — colored, bold, centered
                        status_fg = color.name()
                        if row_color:
                            status_fg = ensure_contrast(
                                color.name(), row_color)
                        status_label = ElidedLabel(text)
                        status_label.setAlignment(Qt.AlignCenter)
                        pal = status_label.palette()
                        pal.setColor(QPalette.WindowText, QColor(status_fg))
                        status_label.setPalette(pal)
                        font = status_label.font()
                        font.setBold(True)
                        status_label.setFont(font)
                        status_label.setToolTip(text)
                        c_layout.addWidget(status_label, 1)

                        # Right-aligned change indicator dot
                        fire_label = QLabel(
                            '\U0001f525' if show_fire else '')
                        fire_label.setObjectName('_fireLabel')
                        fire_px = max(10, self._zoomed_size(-3))
                        fire_label.setFixedWidth(int(fire_px * 1.4))
                        fire_label.setAlignment(
                            Qt.AlignCenter | Qt.AlignVCenter)
                        if show_fire:
                            fire_color = t.accent_orange
                            if row_color:
                                fire_color = ensure_contrast(
                                    t.accent_orange, row_color)
                            fire_label.setStyleSheet(
                                f'color: {fire_color}; font-size: {fire_px}px;')
                        c_layout.addWidget(fire_label)

                        # Left-click: dismiss fire indicator.
                        def _make_click(
                            t: str = tag,
                            w: QWidget = container,
                        ) -> Callable:
                            def _on_click(event: object) -> None:
                                if event.button() != Qt.LeftButton:
                                    QWidget.mousePressEvent(w, event)
                                    return
                                if t not in self._dismissed_new_status:
                                    self._dismissed_new_status.add(t)
                                    self._update_table()
                            return _on_click
                        container.mousePressEvent = _make_click()

                        # Right-click context menu for actionable states.
                        # Set CustomContextMenu on each child widget
                        # directly (not on the container) because
                        # ContextMenu event propagation from child to
                        # parent is unreliable on macOS PyQt5.
                        if cli_state in (
                            CLIState.INTERRUPTED, CLIState.RUNNING,
                            CLIState.NEEDS_PERMISSION, CLIState.NEEDS_INPUT,
                        ):
                            if cli_state == CLIState.INTERRUPTED:
                                _handler = self._show_status_action_menu
                            elif cli_state == CLIState.RUNNING:
                                _handler = self._show_running_status_menu
                            else:
                                _handler = self._show_permission_menu
                            for child in (spacer, status_label,
                                          fire_label, container):
                                child.setContextMenuPolicy(
                                    Qt.CustomContextMenu)
                                child.customContextMenuRequested.connect(
                                    lambda _pos, _w=container, _t=tag,
                                    _h=_handler: _h(_w, _t))

                        # Ensure a table item exists so the
                        # cell-widget tooltip path can find it.
                        s_item = self.table.item(row, self.COL_STATUS)
                        if not s_item:
                            s_item = QTableWidgetItem('')
                            self.table.setItem(
                                row, self.COL_STATUS, s_item)
                        s_item.setText('')

                        self._set_cell_widget(
                            row, self.COL_STATUS, container)
                        self._cache_cell(tag, 'status', status_state,
                                         row, self.COL_STATUS)

                    # Update tooltips every refresh (explanation and
                    # fire-ago text can change).
                    # Item tooltip = value only (for truncation path).
                    # _extra_tooltip on cell widget = explanation
                    #   (combined by tooltip handler when truncated).
                    # ElidedLabel tooltip = explanation (shown by
                    #   widget tooltip path when not truncated).
                    s_item = self.table.item(row, self.COL_STATUS)
                    w = self.table.cellWidget(row, self.COL_STATUS)
                    explanation = ''
                    if self._prefs.get('show_tooltips', True):
                        explanation = state_explanations.get(
                            cli_state, '')
                        if show_fire and explanation:
                            ago = int(
                                now - self._state_changed_at[tag][1])
                            explanation += (
                                f' (changed {ago}s ago'
                                ' - click to dismiss)')
                    if s_item:
                        s_item.setToolTip('')
                    if w:
                        w.setProperty('_extra_tooltip',
                                      explanation or None)
                        label = w.findChild(ElidedLabel)
                        if label:
                            # Keep status text for truncation tooltip;
                            # append explanation when hover tooltips are on
                            if explanation:
                                label.setToolTip(
                                    f'{text}\n{explanation}')
                            else:
                                label.setToolTip(text)

                    # Task column — last message sent to the CLI
                    current_task = session.get('current_task', '')
                    # Show first line only; tooltip has the full message
                    task_display = (current_task.split('\n', 1)[0]
                                    if current_task else 'N/A')
                    self._set_cell_text(row, self.COL_TASK,
                                        task_display, row_color)
                    task_item = self.table.item(row, self.COL_TASK)
                    if task_item and current_task:
                        # Wrap in HTML so Qt word-wraps long tooltips
                        escaped = (current_task
                                   .replace('&', '&amp;')
                                   .replace('<', '&lt;')
                                   .replace('>', '&gt;')
                                   .replace('\n', '<br>'))
                        task_item.setToolTip(
                            f'<div style="max-width:600px">{escaped}</div>'
                        )

                    # Queue column with menu button on the left
                    auto_send_mode = session.get('auto_send_mode', AutoSendMode.PAUSE)
                    queue_size = session['queue_size']
                    q_state = (queue_size, auto_send_mode, row_color)
                    if not self._cell_cached(tag, 'queue', q_state,
                                             row, self.COL_QUEUE):
                        q_container = QWidget()
                        q_layout = QHBoxLayout(q_container)
                        q_layout.setContentsMargins(0, 0, 0, 0)
                        q_layout.setSpacing(2)

                        q_menu_btn = HoverIconButton(_THREE_DOT_SVG, self._zoomed_btn_w(14))
                        q_menu_btn.setFixedSize(
                            self._zoomed_btn_w(24),
                            q_menu_btn.sizeHint().height())
                        q_menu_btn.setStyleSheet(menu_btn_style(font_size=self._zoomed_size()))
                        q_menu_btn.setToolTip('Queue options')
                        q_menu_btn.clicked.connect(
                            lambda checked, btn=q_menu_btn, t=tag:
                                self._show_queue_context_menu(
                                    btn, btn.rect().bottomLeft(), t)
                        )
                        q_layout.addWidget(
                            q_menu_btn, 0, Qt.AlignVCenter)

                        q_label = QLabel(str(queue_size))
                        q_label.setAlignment(Qt.AlignCenter)
                        if queue_size > 0:
                            t_q = current_theme()
                            q_label.setStyleSheet(
                                f'QLabel {{'
                                f'  color: {t_q.accent_blue};'
                                f'  font-size: {self._zoomed_size(-2)}px;'
                                f'  font-weight: bold;'
                                f'}}'
                            )
                        q_layout.addWidget(q_label, 1, Qt.AlignCenter)

                        q_action_btn = HoverIconButton(_SEND_SVG, self._zoomed_btn_w(14))
                        q_action_btn.setFixedSize(
                            self._zoomed_btn_w(24),
                            q_action_btn.sizeHint().height())
                        q_action_btn.setStyleSheet(menu_btn_style(font_size=self._zoomed_size()))
                        q_action_btn.setToolTip('Send options')
                        q_action_btn.clicked.connect(
                            lambda checked, btn=q_action_btn, t=tag:
                                self._show_queue_action_menu(
                                    btn, btn.rect().bottomLeft(), t)
                        )
                        q_layout.addWidget(
                            q_action_btn, 0, Qt.AlignVCenter)

                        # Clear underlying item text
                        item = self.table.item(row, self.COL_QUEUE)
                        if item:
                            item.setText('')
                        self._set_cell_widget(
                            row, self.COL_QUEUE, q_container)
                        self._apply_row_color_to_widget(
                            q_container, row_color)
                        self._cache_cell(tag, 'queue', q_state,
                                         row, self.COL_QUEUE)

                # ── Server button + close button ───────────────────
                # Show "Starting..." for dead rows that are mid-transition
                # — either freshly launched (``_starting_tags``) or being
                # moved from one terminal to another (``_moving_tags``,
                # the Move-to-IDE close→relaunch flow).
                starting = is_dead and (
                    tag in self._starting_tags or tag in self._moving_tags
                )
                branch_mismatch = bool(
                    not is_dead
                    and pinned_data.get('remote_project_path')
                    and pinned_branch
                    and pinned_branch != 'N/A'
                    and session.get('branch')
                    and session['branch'] != pinned_branch
                )
                srv_state = (is_dead, starting, branch_mismatch,
                             pinned_branch, session.get('branch', ''),
                             server_pid)

                if not self._cell_cached(tag, 'server', srv_state,
                                         row, self.COL_SERVER):
                    server_container = QWidget()
                    server_layout = QHBoxLayout(server_container)
                    server_layout.setContentsMargins(0, 0, 0, 0)
                    server_layout.setSpacing(2)

                    if not is_dead:
                        server_x = QPushButton('\u00d7')
                        server_x.setFixedSize(self._zoomed_btn_w(28),server_x.sizeHint().height())
                        server_x.setStyleSheet(close_btn_style(font_size=self._zoomed_size()))
                        server_x.setProperty('_btn_role', 'close')
                        server_x.setToolTip(f'Close server {tag}')
                        server_x.clicked.connect(
                            lambda checked, t=tag, spid=server_pid:
                                self._close_server(t, spid)
                        )
                        server_layout.addWidget(server_x, 0, Qt.AlignVCenter)

                    if is_dead:
                        server_btn = QPushButton(
                            'Starting...' if starting else '\u25cb  Terminal')
                        server_btn.setStyleSheet(inactive_btn_style())
                        server_btn.setToolTip(
                            f'Server is starting for {tag}...' if starting
                            else f'Start server for {tag}'
                        )
                        if starting:
                            server_btn.setEnabled(False)
                        server_btn.clicked.connect(
                            lambda checked, t=tag: self._start_server(t)
                        )
                    else:
                        if branch_mismatch:
                            server_btn = QPushButton('\u26a0  Terminal')
                            # Reuse the shared active-button geometry (same
                            # padding/border/radius/height as the green
                            # Terminal) tinted orange. A bare `color:` override
                            # left padding/border at the stylesheet defaults,
                            # which rendered the button wider than the green
                            # one (verified on macOS: 175px vs 169px).
                            server_btn.setStyleSheet(
                                active_btn_style(
                                    fg_override=current_theme().accent_orange))
                            server_btn.setToolTip(
                                f"Branch mismatch: expected '{pinned_branch}', "
                                f"got '{session['branch']}'"
                            )
                            server_btn.setProperty('always_tooltip', True)
                            server_btn.setProperty('_btn_role', 'orange')
                        else:
                            server_btn = QPushButton('Terminal')
                            server_btn.setStyleSheet(active_btn_style())
                            server_btn.setProperty('_btn_role', 'active')
                            server_btn.setToolTip(
                                f'Jump to server terminal for {tag}')
                        server_btn.clicked.connect(
                            lambda checked, t=tag:
                                self._focus_session(t, 'server')
                        )
                    server_layout.addWidget(server_btn)

                    self._set_cell_widget(row, self.COL_SERVER,
                                          server_container)
                    self._apply_row_color_to_widget(
                        server_container, row_color)
                    self._cache_cell(tag, 'server', srv_state,
                                     row, self.COL_SERVER)

                # ── Client button + close button ───────────────────
                cli_state = (is_dead, has_client, client_pid)

                if not self._cell_cached(tag, 'client', cli_state,
                                         row, self.COL_CLIENT):
                    client_container = QWidget()
                    client_layout = QHBoxLayout(client_container)
                    client_layout.setContentsMargins(0, 0, 0, 0)
                    client_layout.setSpacing(2)

                    if has_client:
                        client_x = QPushButton('\u00d7')
                        client_x.setFixedSize(self._zoomed_btn_w(28),client_x.sizeHint().height())
                        client_x.setStyleSheet(close_btn_style(font_size=self._zoomed_size()))
                        client_x.setProperty('_btn_role', 'close')
                        client_x.setToolTip(f'Close client {tag}')
                        client_x.clicked.connect(
                            lambda checked, t=tag, pid=client_pid:
                                self._close_client(t, pid)
                        )
                        client_layout.addWidget(client_x, 0, Qt.AlignVCenter)

                    if is_dead and not has_client:
                        client_btn = QPushButton('Terminal')
                        client_btn.setStyleSheet(inactive_btn_style())
                        client_btn.setEnabled(False)
                        client_btn.setToolTip('No client connected')
                    elif has_client:
                        client_btn = QPushButton('Terminal')
                        client_btn.setStyleSheet(active_btn_style())
                        client_btn.setProperty('_btn_role', 'active')
                        client_btn.setToolTip(
                            f'Jump to client terminal for {tag}')
                    else:
                        client_btn = QPushButton('Terminal')
                        client_btn.setStyleSheet(inactive_btn_style())
                        client_btn.setToolTip(
                            f'Open new client for {tag}')
                    if not (is_dead and not has_client):
                        client_btn.clicked.connect(
                            lambda checked, t=tag:
                                self._focus_session(t, 'client')
                        )
                    client_layout.addWidget(client_btn)
                    self._set_cell_widget(row, self.COL_CLIENT,
                                          client_container)
                    self._apply_row_color_to_widget(
                        client_container, row_color)
                    self._cache_cell(tag, 'client', cli_state,
                                     row, self.COL_CLIENT)

                # ── Slack column ──────────────────────────────────
                slack_enabled = session.get('slack_enabled', False)
                slack_installed = self._is_slack_installed()
                bot_running = self._is_slack_bot_running()
                slack_state = (is_dead, slack_installed, bot_running,
                               slack_enabled)
                if not self._cell_cached(tag, 'slack', slack_state,
                                         row, self.COL_SLACK):
                    if not slack_installed:
                        slack_btn = QPushButton('Slack')
                        slack_btn.setStyleSheet(inactive_btn_style())
                        slack_btn.setEnabled(False)
                        slack_btn.setToolTip(
                            'Install Slack app first (make install-slack-app)')
                        self._set_cell_widget(row, self.COL_SLACK, slack_btn)
                    elif is_dead:
                        slack_btn = QPushButton('Slack')
                        slack_btn.setStyleSheet(inactive_btn_style())
                        slack_btn.setEnabled(False)
                        slack_btn.setToolTip('Start server first')
                        self._set_cell_widget(row, self.COL_SLACK, slack_btn)
                    elif not bot_running:
                        slack_btn = QPushButton('Slack')
                        slack_btn.setStyleSheet(inactive_btn_style())
                        tip = ('Slack bot is not running - will reconnect '
                               'when started' if slack_enabled
                               else 'Start the Slack bot first')
                        slack_btn.setToolTip(tip)
                        slack_btn.clicked.connect(
                            lambda checked:
                                self._show_slack_bot_not_running()
                        )
                        self._set_cell_widget(row, self.COL_SLACK, slack_btn)
                    elif slack_enabled:
                        slack_container = QWidget()
                        slack_layout = QHBoxLayout(slack_container)
                        slack_layout.setContentsMargins(0, 0, 0, 0)
                        slack_layout.setSpacing(2)

                        slack_x = QPushButton('\u00d7')
                        slack_x.setFixedSize(self._zoomed_btn_w(28),slack_x.sizeHint().height())
                        slack_x.setStyleSheet(close_btn_style(font_size=self._zoomed_size()))
                        slack_x.setProperty('_btn_role', 'close')
                        slack_x.setToolTip(f'Disconnect Slack for {tag}')
                        slack_x.clicked.connect(
                            lambda checked, t=tag:
                                self._toggle_slack(t, False)
                        )
                        slack_layout.addWidget(slack_x, 0, Qt.AlignVCenter)

                        slack_btn = QPushButton('Slack')
                        slack_btn.setStyleSheet(active_btn_style())
                        slack_btn.setProperty('_btn_role', 'active')
                        slack_btn.setToolTip(
                            f'Open Slack thread for {tag}')
                        slack_btn.clicked.connect(
                            lambda checked, t=tag:
                                self._open_slack_thread(t)
                        )
                        slack_layout.addWidget(slack_btn)
                        self._set_cell_widget(
                            row, self.COL_SLACK, slack_container)
                        self._apply_row_color_to_widget(
                            slack_container, row_color)
                    else:
                        slack_btn = QPushButton('Slack')
                        slack_btn.setStyleSheet(inactive_btn_style())
                        slack_btn.setToolTip(
                            f'Enable Slack integration for {tag}')
                        slack_btn.clicked.connect(
                            lambda checked, t=tag:
                                self._toggle_slack(t, True)
                        )
                        self._set_cell_widget(row, self.COL_SLACK, slack_btn)
                    self._cache_cell(tag, 'slack', slack_state,
                                     row, self.COL_SLACK)

                # ── PR column: "Track PR" → "Checking..." → tracked
                if tag in self._checking_tags:
                    pr_state = ('checking',)
                    if not self._cell_cached(tag, 'pr', pr_state,
                                             row, self.COL_PR):
                        if self.table.columnSpan(row, self.COL_PR) > 1:
                            self.table.setSpan(row, self.COL_PR, 1, 1)
                        checking_label = PulsingLabel()
                        checking_label.setText('Checking...')
                        checking_label.setStyleSheet(
                            f'color: {current_theme().text_muted}; font-style: italic;')
                        self._set_cell_widget(row, self.COL_PR,
                                              checking_label)
                        self._cache_cell(tag, 'pr', pr_state,
                                         row, self.COL_PR)
                    self.table.removeCellWidget(row, self.COL_PR_BRANCH)
                    self._set_cell_text(row, self.COL_PR_BRANCH, pr_branch,
                                        row_color)

                elif tag in self._tracked_tags:
                    stale_pr_tags.discard(tag)
                    self._render_tracked_pr_cell(
                        row, tag, row_color,
                        on_stop=self._stop_tracking,
                        server_running=not is_dead,
                        wire_send_callbacks=True,
                    )
                    self.table.removeCellWidget(row, self.COL_PR_BRANCH)
                    self._set_cell_text(row, self.COL_PR_BRANCH, pr_branch,
                                        row_color)

                else:
                    # Not tracked — "Track PR" button
                    pr_state = ('untracked', is_dead,
                                    bool(pinned_data.get('remote_project_path')))
                    if not self._cell_cached(tag, 'pr', pr_state,
                                             row, self.COL_PR):
                        if self.table.columnSpan(row, self.COL_PR) > 1:
                            self.table.setSpan(row, self.COL_PR, 1, 1)
                        is_pr_pinned_row = bool(
                            pinned_data.get('remote_project_path'))
                        track_btn = QPushButton('Track PR')
                        if is_dead and not is_pr_pinned_row:
                            track_btn.setToolTip(
                                'Start a server first to discover PR from branch')
                            track_btn.setEnabled(False)
                        else:
                            track_btn.setToolTip(
                                f'Start tracking PR for {tag}')
                        track_btn.setStyleSheet(inactive_btn_style())
                        track_btn.clicked.connect(
                            lambda checked, t=tag: self._start_tracking(t)
                        )
                        self._set_cell_widget(row, self.COL_PR, track_btn)
                        self._cache_cell(tag, 'pr', pr_state,
                                         row, self.COL_PR)

                    # PR Branch: show stored branch + X button if PR-pinned
                    is_pr_pinned = (
                        pinned_data.get('remote_project_path')
                        and pr_branch != 'N/A'
                    )
                    if is_pr_pinned:
                        pr_br_state = ('untracked_pinned', pr_branch,
                                       row_color)
                        if not self._cell_cached(tag, 'pr_branch', pr_br_state,
                                                 row, self.COL_PR_BRANCH):
                            pr_br_container = QWidget()
                            pr_br_layout = QHBoxLayout(pr_br_container)
                            pr_br_layout.setContentsMargins(0, 0, 0, 0)
                            pr_br_layout.setSpacing(4)

                            pr_br_x = QPushButton('\u00d7')
                            pr_br_x.setFixedSize(self._zoomed_btn_w(28),pr_br_x.sizeHint().height())
                            pr_br_x.setStyleSheet(close_btn_style(font_size=self._zoomed_size()))
                            pr_br_x.setProperty('_btn_role', 'close')
                            pr_br_x.setToolTip(
                                f'Clear pinned PR data for {tag}')
                            pr_br_x.clicked.connect(
                                lambda checked, t=tag:
                                    self._clear_pinned_pr_data(t)
                            )
                            pr_br_layout.addWidget(
                                pr_br_x, 0, Qt.AlignVCenter)

                            pr_br_label = ElidedLabel(pr_branch)
                            pr_br_label.setAlignment(Qt.AlignCenter)
                            pr_br_label.setToolTip(pr_branch)
                            pr_br_layout.addWidget(pr_br_label, 1)

                            # Right-side spacer matching the X button width so the centered label sits in the column's centre, not offset by the X.
                            pr_br_spacer = QWidget()
                            pr_br_spacer.setFixedWidth(self._zoomed_btn_w(28))
                            pr_br_layout.addWidget(
                                pr_br_spacer, 0, Qt.AlignVCenter)

                            # Ensure a table item exists with the tooltip
                            # so the cell-widget tooltip path can find it.
                            # Clear display text so it doesn't render
                            # through behind the widget.
                            item = self.table.item(row, self.COL_PR_BRANCH)
                            if not item:
                                item = QTableWidgetItem('')
                                self.table.setItem(
                                    row, self.COL_PR_BRANCH, item)
                            item.setText('')
                            item.setToolTip(pr_branch)
                            self._set_cell_widget(
                                row, self.COL_PR_BRANCH, pr_br_container)
                            self._apply_row_color_to_widget(
                                pr_br_container, row_color)
                            self._cache_cell(
                                tag, 'pr_branch', pr_br_state,
                                row, self.COL_PR_BRANCH)
                    else:
                        self.table.removeCellWidget(row, self.COL_PR_BRANCH)
                        self._set_cell_text(row, self.COL_PR_BRANCH, 'N/A',
                                            row_color)

            # Clean up stale PR widgets for tags no longer shown
            for stale_tag in stale_pr_tags:
                w = self._pr_widgets.pop(stale_tag, None)
                if w:
                    try:
                        w.set_pulsing(False)
                    except RuntimeError:
                        pass
                self._pr_approval_widgets.pop(stale_tag, None)

            # Clean up stale cell cache entries for tags no longer shown
            current_tags = {s['tag'] for s in self.sessions}
            stale_keys = [k for k in self._cell_cache
                          if k[0] not in current_tags]
            for k in stale_keys:
                self._cell_cache.pop(k, None)

            # Force COL_DELETE to re-measure against the current X-button
            # widget.  ResizeToContents grows the section when a wider widget
            # arrives via setCellWidget, but setCellWidget doesn't emit a
            # signal that triggers a downward re-measure — so after a zoom-up
            # → zoom-down round-trip the column stays inflated.  An explicit
            # call here keeps it honest on every rebuild.
            self.table.resizeColumnToContents(self.COL_DELETE)
            # Mark this render as populated so the next empty-state
            # transition knows it can snapshot the current widths for
            # later restoration.  See the matching snapshot block in
            # the empty branch above.
            self._last_render_was_populated = True
        finally:
            app._suppress_tooltips = tooltips_were_enabled
            self.table.setUpdatesEnabled(True)
            # Re-apply row hover highlight (widgets were replaced during rebuild)
            if getattr(self, '_hovered_row', -1) >= 0:
                self._apply_hover_to_row(self._hovered_row, True)

    def _refresh_data(self) -> None:
        """Refresh session data and update table (non-blocking).

        Launches a SessionRefreshWorker to query sockets in the background.
        Falls back to synchronous refresh on first call (before timer starts).
        """
        if self._refresh_worker and self._refresh_worker.isRunning():
            return  # skip this cycle

        # Ensure the previous worker's thread has fully stopped before we
        # orphan the reference.  isRunning() can return False while the
        # underlying OS thread is still winding down; wait() blocks until
        # the thread is truly finished, preventing QThread::~QThread() from
        # aborting on a still-running thread (SIGABRT after sleep/wake).
        if self._refresh_worker is not None:
            self._refresh_worker.wait(500)  # ms – should be near-instant

        self._refresh_worker = SessionRefreshWorker(
            self,
            scan_cursor_gui=bool(self._prefs.get('show_cursor_gui_agents', True)),
        )
        self._refresh_worker.sessions_ready.connect(self._on_sessions_refreshed)
        self._refresh_worker.finished.connect(self._on_refresh_worker_finished)
        self._refresh_worker.start()

    def _on_refresh_worker_finished(self) -> None:
        """Clean up the refresh worker reference after it completes.

        Uses sender() to identify the actual worker that emitted ``finished``,
        avoiding a race where a *new* worker has already replaced
        ``self._refresh_worker`` (e.g. after sleep/wake timer bursts).
        """
        worker = self.sender()
        if worker is not None:
            worker.wait()  # ensure OS thread is fully stopped before deletion
            worker.deleteLater()
        if self._refresh_worker is worker:
            self._refresh_worker = None

    def _reconcile_cursor_gui_rows(self, cursor_gui_rows: list) -> None:
        """Set ``self._cursor_gui_rows`` from a fresh scan, synthesizing a
        "tab closed" row for each tracked Cursor tag whose tab is no longer
        open, and pruning per-tag state / caches for tags we no longer show.

        This is what makes the two close buttons differ: the Open-cell X
        closes only the tab (tracking stays → the tag is synthesized here →
        the row survives as "tab closed", like a dead-but-tracked regular
        row), while the leftmost X also stops tracking (no synthesis → the
        row drops).  Pure state transform, factored out for unit testing.
        """
        live_cursor_tags = {r['tag'] for r in cursor_gui_rows}
        # Cache each live Cursor row (a shallow copy, so a later in-place
        # mutation of the live row dict can't corrupt the cached snapshot)
        # so a PR-tracked tab that's later closed can still be rendered as a
        # "tab closed" row.
        for r in cursor_gui_rows:
            self._cursor_row_cache[r['tag']] = dict(r)
        # Synthesize a "tab closed" row for each tracked Cursor tag whose tab
        # is no longer open.
        synthesized: list[dict] = []
        for t in self._tracked_tags:
            if (t.startswith(CURSOR_GUI_TAG_PREFIX) and t not in live_cursor_tags
                    and t in self._cursor_row_cache):
                closed = dict(self._cursor_row_cache[t])
                closed['_tab_closed'] = True
                closed['status_kind'] = 'idle'
                closed['status_text'] = '○  Tab closed'
                synthesized.append(closed)
        self._cursor_gui_rows = list(cursor_gui_rows) + synthesized
        # Tags we keep showing (live tabs + synthesized closed-but-tracked)
        # must survive the prune below; everything else cursor-ish that's
        # neither shown nor tracked is stale and gets cleaned up.
        kept_cursor_tags = live_cursor_tags | {r['tag'] for r in synthesized}
        stale_cursor_tags = {
            t for t in (set(self._pr_statuses) | set(self._tracked_tags)
                        | set(self._pr_widgets))
            if t.startswith(CURSOR_GUI_TAG_PREFIX) and t not in kept_cursor_tags
        }
        for t in stale_cursor_tags:
            self._tracked_tags.discard(t)
            self._pr_statuses.pop(t, None)
            self._pr_widgets.pop(t, None)
            self._pr_approval_widgets.pop(t, None)
            self._pr_changed_at.pop(t, None)
            self._dismissed_pr_new_status.discard(t)
            # Drop cached cell widgets for gone Cursor tabs so the
            # cache can't grow unbounded.
            for key in [k for k in self._cell_cache if k[0] == t]:
                self._cell_cache.pop(key, None)
        # Prune the row cache to tags we still care about (live or kept).
        for t in [t for t in self._cursor_row_cache
                  if t not in kept_cursor_tags]:
            self._cursor_row_cache.pop(t, None)

    def _on_sessions_refreshed(self, sessions: list,
                               cursor_gui_rows: list) -> None:
        """Handle background session refresh result.

        ``cursor_gui_rows`` are read-only Cursor editor Agent-tab rows.
        They are stashed separately (not merged into ``self.sessions``)
        so the pinned-session / PR / sleep-guard machinery never sees
        them; ``_update_table`` overlays them at render time only.
        """
        # Respect the CURRENT toggle, not the value captured when this scan's
        # worker was launched: if the feature was turned off mid-scan, an
        # in-flight worker (built with scan_cursor_gui=True) would otherwise
        # briefly resurrect the rows here for one refresh tick.
        if not self._prefs.get('show_cursor_gui_agents', True):
            cursor_gui_rows = []
        self._reconcile_cursor_gui_rows(cursor_gui_rows)
        # A tracked Cursor row keeps the SCM poll timer running - keep it
        # in sync (also stops it when the last tracked thing goes away).
        self._sync_scm_poll_timer()
        self.sessions = self._merge_sessions(sessions)
        # Dynamically show/hide Slack column if install state changed
        slack_now = self._is_slack_installed()
        if slack_now != self._slack_available:
            self._slack_available = slack_now
            if not slack_now:
                self.table.setColumnHidden(self.COL_SLACK, True)
            else:
                # Only un-hide if user hasn't explicitly hidden it
                hidden = self._prefs.get('hidden_columns', [])
                if 'Slack' not in hidden:
                    self.table.setColumnHidden(self.COL_SLACK, False)
        self._update_table()
        self._update_slack_bot_button()
        self._check_slack_bot_transition()
        self._evaluate_sleep_guard()
        dock_enabled = get_dock_enabled(self._prefs)
        events = self._dock_badge.update_sessions(
            sessions, self.isActiveWindow(), dock_enabled,
        )
        self._send_banner_notifications(events)

    def _open_settings(self) -> None:
        """Open the settings dialog."""
        server_settings = load_settings()
        dialog = SettingsDialog(
            current_terminal=self._prefs.get('default_terminal', 'Terminal.app'),
            current_repos_dir=self._prefs.get('repos_dir', DEFAULT_REPOS_DIR),
            active_paths_fn=self._get_active_project_paths,
            log_fn=self._show_status,
            show_tooltips=self._prefs.get('show_tooltips', True),
            show_cursor_gui_agents=self._prefs.get('show_cursor_gui_agents', True),
            notification_prefs=get_notification_prefs(self._prefs),
            current_auto_send_mode=server_settings.get('auto_send_mode', AutoSendMode.PAUSE),
            current_diff_tool=self._prefs.get('default_diff_tool', ''),
            new_status_seconds=self._prefs.get('new_status_seconds', 60),
            current_global_shortcut=self._prefs.get('global_shortcut', ''),
            current_notes_shortcut_focused=self._prefs.get('notes_shortcut_focused', ''),
            current_notes_shortcut_global=self._prefs.get('notes_shortcut_global', ''),
            current_theme_name=self._prefs.get('theme', 'Nord'),
            on_theme_change=self._apply_theme,
            parent=self,
        )
        if dialog.exec_():
            self._prefs['default_terminal'] = dialog.selected_terminal()
            self._prefs['repos_dir'] = dialog.selected_repos_dir()
            self._prefs['show_tooltips'] = dialog.show_tooltips()
            cursor_gui_now = dialog.show_cursor_gui_agents()
            if not cursor_gui_now:
                # Turning the feature off: drop any overlay rows now so they
                # vanish immediately rather than lingering until the next
                # refresh tick (which would no longer repopulate them).  Also
                # tear down any in-memory Cursor PR tracking right away (those
                # tags are never pinned, so nothing else would clean them up)
                # and re-sync the SCM poll timer so it stops if nothing else
                # is tracked.
                for t in [t for t in self._tracked_tags
                          if t.startswith(CURSOR_GUI_TAG_PREFIX)]:
                    self._untrack_cursor_pr(t)
                self._cursor_gui_rows = []
                self._cursor_row_cache.clear()
                self._sync_scm_poll_timer()
            self._prefs['show_cursor_gui_agents'] = cursor_gui_now
            self._prefs['notifications'] = dialog.notification_prefs()
            self._prefs['default_diff_tool'] = dialog.selected_diff_tool()
            self._prefs['new_status_seconds'] = dialog.new_status_seconds()
            old_shortcut = self._prefs.get('global_shortcut', '')
            new_shortcut = dialog.selected_global_shortcut()
            self._prefs['global_shortcut'] = new_shortcut
            old_notes_f = self._prefs.get('notes_shortcut_focused', '')
            old_notes_g = self._prefs.get('notes_shortcut_global', '')
            new_notes_f = dialog.selected_notes_shortcut_focused()
            new_notes_g = dialog.selected_notes_shortcut_global()
            self._prefs['notes_shortcut_focused'] = new_notes_f
            self._prefs['notes_shortcut_global'] = new_notes_g
            self._prefs['theme'] = dialog.selected_theme()
            self._save_prefs()
            # Save auto-send mode to server settings (read by new servers)
            server_settings['auto_send_mode'] = dialog.selected_auto_send_mode()
            save_settings(server_settings)
            self._apply_tooltips_setting()
            if new_shortcut != old_shortcut:
                self._register_global_shortcut()
            if new_notes_f != old_notes_f or new_notes_g != old_notes_g:
                self._register_notes_shortcut()
            self._show_status('Settings saved')

    def _open_notes(self) -> None:
        """Toggle the notes dialog — open if closed, close if open."""
        existing = getattr(self, '_notes_dialog', None)
        if existing is not None:
            existing.close()
            return
        dialog = NotesDialog(parent=self)
        self._notes_dialog = dialog
        dialog.finished.connect(self._on_notes_closed)
        dialog.show()

    def _on_notes_closed(self) -> None:
        """Clean up after the notes dialog closes."""
        dlg = self._notes_dialog
        self._notes_dialog = None
        if dlg is not None:
            dlg.deleteLater()

    def _show_queue_context_menu(
        self, label: QLabel, pos: 'QPoint', tag: str,
    ) -> None:
        """Show context menu on the Queue column left button."""
        current_mode = AutoSendMode.PAUSE
        for s in self.sessions:
            if s['tag'] == tag:
                current_mode = s.get('auto_send_mode', AutoSendMode.PAUSE)
                break
        default_mode = load_settings().get('auto_send_mode', AutoSendMode.PAUSE)

        menu = QMenu(self)
        if self._prefs.get('show_tooltips', True):
            menu.setToolTipsVisible(True)

        # Determine current queue size for enabling/disabling actions
        queue_size = 0
        for s in self.sessions:
            if s['tag'] == tag:
                queue_size = s.get('queue_size', 0)
                break

        edit_action = menu.addAction('Edit queue messages')
        edit_action.setToolTip('Open a dialog to view and edit queued messages')
        edit_action.setEnabled(queue_size > 0)
        edit_action.triggered.connect(
            lambda _checked, t=tag: self._edit_queue_messages(t)
        )

        clear_action = menu.addAction('Clear queue')
        clear_action.setToolTip('Delete all queued messages without sending them')
        clear_action.setEnabled(queue_size > 0)
        clear_action.triggered.connect(
            lambda _checked, t=tag: self._clear_queue(t)
        )

        menu.addSeparator()

        pause_label = 'Pause on input'
        if default_mode == AutoSendMode.PAUSE:
            pause_label += ' (default)'
        pause_action = menu.addAction(pause_label)
        pause_action.setCheckable(True)
        pause_action.setChecked(current_mode == AutoSendMode.PAUSE)
        pause_action.setToolTip(
            'Auto-send queued messages only when the CLI is idle.\n'
            '\n'
            '\u25cb Idle - sends next queued message\n'
            '\u25cf Running - waits until finished\n'
            '\u25b2 Permission - queue waits for your answer\n'
            '\u25c6 Question - queue waits for your answer\n'
            '\u25c7 Interrupted - waits (needs manual resume)')
        pause_action.triggered.connect(
            lambda _checked, t=tag: self._set_auto_send_mode(t, AutoSendMode.PAUSE)
        )

        always_label = 'Always send'
        if default_mode == AutoSendMode.ALWAYS:
            always_label += ' (default)'
        always_action = menu.addAction(always_label)
        always_action.setCheckable(True)
        always_action.setChecked(current_mode == AutoSendMode.ALWAYS)
        always_action.setToolTip(
            'Auto-approve permission prompts and send queued\n'
            'messages when idle.\n'
            '\n'
            '\u25cb Idle - sends next queued message\n'
            '\u25cf Running - waits until finished\n'
            '\u25b2 Permission - auto-approves "Yes"\n'
            '\u25c6 Question - queue waits for your answer\n'
            '\u25c7 Interrupted - waits (needs manual resume)')
        always_action.triggered.connect(
            lambda _checked, t=tag: self._set_auto_send_mode(t, AutoSendMode.ALWAYS)
        )

        menu.exec_(label.mapToGlobal(pos))
        # Clear stuck hover state after menu closes
        if not sip.isdeleted(label):
            label.setAttribute(Qt.WA_UnderMouse, False)
            label.update()

    def _show_queue_action_menu(
        self, btn: QPushButton, pos: 'QPoint', tag: str,
    ) -> None:
        """Show send-options menu on the Queue column right button."""
        queue_size = 0
        for s in self.sessions:
            if s['tag'] == tag:
                queue_size = s.get('queue_size', 0)
                break

        menu = QMenu(self)
        if self._prefs.get('show_tooltips', True):
            menu.setToolTipsVisible(True)

        force_action = menu.addAction('Force-send next queued message')
        force_action.setEnabled(queue_size > 0)
        force_action.setToolTip(
            'Send the next queued message immediately,\n'
            'even if the CLI is still running')
        force_action.triggered.connect(
            lambda _checked, t=tag: self._force_send_next(t)
        )

        menu.addSeparator()

        msg_action = menu.addAction('Send message')
        msg_action.setToolTip(
            'Type a message and choose whether to queue it\n'
            'at the front or end of the queue')
        msg_action.triggered.connect(
            lambda _checked, t=tag: self._send_immediate_message(t)
        )

        preset_action = menu.addAction('Send preset')
        preset_action.setToolTip(
            'Pick a saved message-bundle preset and choose whether\n'
            'to queue it at the front or end of the queue')
        preset_action.triggered.connect(
            lambda _checked, t=tag: self._send_preset_message(t)
        )

        menu.exec_(btn.mapToGlobal(pos))

    def _set_auto_send_mode(self, tag: str, mode: str) -> None:
        """Send set_auto_send_mode to the Leap server."""
        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'set_auto_send_mode', 'mode': mode},
        )
        # Update local session data immediately so the next menu
        # open (before the background refresh) shows the new mode.
        for s in self.sessions:
            if s['tag'] == tag:
                s['auto_send_mode'] = mode
                break
        # Persist in pinned sessions so dead rows survive refresh cycles.
        # Use the targeted ``update_pinned_session_field`` instead of
        # ``save_pinned_sessions(self._pinned_sessions)`` — the latter
        # writes the monitor's WHOLE in-memory ``_pinned_sessions`` to
        # disk and would silently overwrite OTHER tags' recent
        # server-side ``auto_send_mode`` writes (e.g., another session
        # toggled via client between our last ``_merge_sessions``
        # refresh and this toggle).  Per-session toggles must stay
        # per-session — the cross-session leak the original fix closed
        # would otherwise re-open through this back door.
        if tag in self._pinned_sessions:
            self._pinned_sessions[tag]['auto_send_mode'] = mode
            update_pinned_session_field(tag, 'auto_send_mode', mode)
        # Invalidate cache so next refresh rebuilds with new mode
        self._cell_cache.pop((tag, 'queue'), None)
        if response and response.get('status') == 'ok':
            self._show_status(f'Auto-send mode: {mode}')
        else:
            self._show_status(f'Auto-send mode: {mode} (server offline)')

    def _edit_queue_messages(self, tag: str) -> None:
        """Open the queue edit dialog for a session."""
        # Race condition guard: queue may have drained since menu was opened
        queue_size = 0
        for s in self.sessions:
            if s['tag'] == tag:
                queue_size = s.get('queue_size', 0)
                break
        if queue_size == 0:
            QMessageBox.warning(
                self, 'Queue Empty',
                f'The queue for "{tag}" is now empty.',
            )
            return

        socket_path = SOCKET_DIR / f"{tag}.sock"
        dialog = QueueEditDialog(tag, socket_path, parent=self)
        dialog.exec_()
        # Invalidate cache so next refresh reflects any edits
        self._cell_cache.pop((tag, 'queue'), None)

    def _clear_queue(self, tag: str) -> None:
        """Clear all queued messages for a session without sending them."""
        # Race condition guard: queue may have drained since menu was opened
        queue_size = 0
        for s in self.sessions:
            if s['tag'] == tag:
                queue_size = s.get('queue_size', 0)
                break
        if queue_size == 0:
            QMessageBox.warning(
                self, 'Queue Empty',
                f'The queue for "{tag}" is now empty.',
            )
            return

        reply = QMessageBox.question(
            self, 'Clear Queue',
            f'Delete all queued messages for "{tag}"?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'clear_queue'},
        )
        self._cell_cache.pop((tag, 'queue'), None)
        if response and response.get('status') == 'ok':
            self._show_status('Queue cleared')
        else:
            self._show_status('Failed to clear queue (server offline)')

    def _show_running_status_menu(
        self, widget: QWidget, tag: str,
    ) -> None:
        """Show action menu when clicking a 'running' status cell."""
        menu = QMenu(self)
        if self._prefs.get('show_tooltips', True):
            menu.setToolTipsVisible(True)

        interrupt_action = menu.addAction('Interrupt')
        interrupt_action.setToolTip('Send Ctrl+C to stop the CLI')
        interrupt_action.triggered.connect(
            lambda _checked, t=tag: self._send_interrupt(t)
        )

        menu.exec_(widget.mapToGlobal(widget.rect().center()))

    def _send_interrupt(self, tag: str) -> None:
        """Send interrupt (Ctrl+C) to a running Leap session."""
        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'interrupt'},
        )
        if response and response.get('status') == 'sent':
            self._show_status(f'Interrupted {tag}')
            self._refresh_data()
        else:
            self._show_status(f'Failed to interrupt {tag}')

    def _show_permission_menu(
        self, widget: QWidget, tag: str,
    ) -> None:
        """Show permission options menu for needs_permission/needs_input."""
        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'get_prompt'},
        )
        if not response or response.get('status') != 'ok':
            self._show_status(f'Failed to get prompt for {tag}')
            return

        prompt_output = response.get('prompt_output', '')
        if not prompt_output:
            self._show_status(f'No prompt output for {tag}')
            return

        # Parse the actual menu options (last 1..n sequence),
        # ignoring numbered plan/content lines above the menu.
        options = extract_menu_options(prompt_output)

        if not options:
            self._show_status(f'No options found in prompt for {tag}')
            return

        menu = QMenu(self)
        if self._prefs.get('show_tooltips', True):
            menu.setToolTipsVisible(True)

        for option_num, label in options:
            if label.startswith('Type something'):
                action = menu.addAction(f'{option_num}. {label}')
                action.setToolTip('Open a text input dialog')
                action.triggered.connect(
                    lambda _checked, t=tag:
                        self._show_custom_answer_dialog(t)
                )
            else:
                action = menu.addAction(f'{option_num}. {label}')
                action.triggered.connect(
                    lambda _checked, t=tag, n=option_num:
                        self._select_permission_option(t, n)
                )

        menu.exec_(widget.mapToGlobal(widget.rect().center()))

    def _select_permission_option(self, tag: str, option_num: int) -> None:
        """Send a numbered option selection to a Leap session."""
        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'select_option', 'message': str(option_num)},
        )
        if response and response.get('status') == 'sent':
            self._show_status(f'Selected option {option_num} for {tag}')
            self._refresh_data()
        else:
            error = (response or {}).get('error', 'unknown error')
            self._show_status(f'Failed: {error}')

    def _show_custom_answer_dialog(self, tag: str) -> None:
        """Show text input dialog for the 'Type something' permission option."""
        text, ok = QInputDialog.getMultiLineText(
            self, 'Custom Answer', f'Type your answer ({tag}):', '')
        if not ok or not text.strip():
            return

        stripped = text.strip()
        socket_path = SOCKET_DIR / f"{tag}.sock"
        # custom_answer types char-by-char (20ms/char) + 0.5s setup;
        # scale timeout so long messages don't hit the default 5s limit.
        timeout = max(5.0, 1.0 + len(stripped) * 0.025)
        result_holder: list[Optional[dict]] = [None]

        def _send() -> None:
            result_holder[0] = send_socket_request(
                socket_path, {'type': 'custom_answer', 'message': stripped},
                timeout=timeout,
            )

        def _on_done() -> None:
            response = result_holder[0]
            if response and response.get('status') == 'sent':
                self._show_status(f'Sent custom answer for {tag}')
                self._refresh_data()
            else:
                error = (response or {}).get('error', 'unknown error')
                self._show_status(f'Failed: {error}')

        worker = BackgroundCallWorker(_send, self)
        worker.finished.connect(_on_done)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _show_status_action_menu(
        self, widget: QWidget, tag: str,
    ) -> None:
        """Show action menu when clicking an 'interrupted' status cell."""
        queue_size = 0
        for s in self.sessions:
            if s['tag'] == tag:
                queue_size = s.get('queue_size', 0)
                break

        menu = QMenu(self)
        if self._prefs.get('show_tooltips', True):
            menu.setToolTipsVisible(True)

        continue_action = menu.addAction("Force-send 'continue' message")
        continue_action.setToolTip(
            "Send 'continue' directly to the CLI,\n"
            'bypassing the queue')
        continue_action.triggered.connect(
            lambda _checked, t=tag: self._send_continue(t)
        )

        menu.addSeparator()

        force_action = menu.addAction('Force-send next queued message')
        force_action.setEnabled(queue_size > 0)
        force_action.setToolTip(
            'Send the next queued message immediately,\n'
            'even if the CLI is still running')
        force_action.triggered.connect(
            lambda _checked, t=tag: self._force_send_next(t)
        )

        menu.exec_(widget.mapToGlobal(widget.rect().center()))

    def _send_continue(self, tag: str) -> None:
        """Send 'continue' directly to the Leap server (bypasses queue)."""
        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'direct', 'message': 'continue'},
        )
        if response and response.get('status') in ('ok', 'sent'):
            self._show_status(f'Sent "continue" to {tag}')
            self._refresh_data()
        else:
            self._show_status(f'Failed to send "continue" to {tag}')

    def _force_send_next(self, tag: str) -> None:
        """Force-send the next queued message to the Leap server."""
        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'force_send'},
        )
        if response and response.get('status') == 'sent':
            self._show_status(f'Force-sent queued message for {tag}')
            self._refresh_data()
        elif response and response.get('status') == 'empty':
            self._show_status(f'No queued messages for {tag}')
        else:
            self._show_status(f'Failed to force-send for {tag}')

    def _send_immediate_message(self, tag: str) -> None:
        """Open a dialog to type and queue a message for the session.

        The Next/To-End toggle in ``SendMessageDialog`` determines whether
        the message is prepended to the front of the queue or appended to
        the end.  The toggle's last value is persisted across dialogs.
        """
        text, at_end, accepted = SendMessageDialog.get_message(
            self, 'Send message', f'Message for "{tag}":')
        if not accepted or not text.strip():
            return
        if at_end:
            sent = send_to_leap_session_raw(tag, text.strip())
        else:
            sent = prepend_to_leap_queue(tag, [text.strip()])
        pos = 'end' if at_end else 'next'
        if sent:
            self._show_status(f'Message queued ({pos}) for {tag}')
            self._refresh_data()
        else:
            self._show_status(f'Failed to queue message for {tag}')

    def _send_preset_message(self, tag: str) -> None:
        """Open the preset picker dialog and queue the chosen preset.

        The Next/To-End toggle decides whether the bundle is prepended or
        appended.  The picker's combo lets the user pick any saved preset
        on the fly.
        """
        preset_name, at_end, accepted = SendPresetDialog.choose(self, tag)
        if not accepted or not preset_name:
            return
        messages = [
            m for m in load_saved_presets().get(preset_name, []) if m.strip()
        ]
        if not messages:
            self._show_status(f'Preset "{preset_name}" is empty')
            return
        if at_end:
            sent = all(send_to_leap_session_raw(tag, m) for m in messages)
        else:
            sent = prepend_to_leap_queue(tag, messages)
        pos = 'end' if at_end else 'next'
        if sent:
            self._show_status(f'Preset queued ({pos}) for {tag}')
            self._refresh_data()
        else:
            self._show_status(f'Preset send failed for {tag}')

    def _open_preset_editor(self) -> None:
        """Open the preset editor dialog.

        The editor only creates/edits/saves presets in ``leap_presets.json``.
        Which preset is active is decided separately inside
        ``SendPresetDialog`` and ``SendCommentsDialog``, both of which read
        ``leap_presets.json`` fresh on open, so no refresh is needed when
        this dialog closes.
        """
        dialog = PresetEditorDialog(self)
        dialog.exec_()

    def _on_cell_clicked(self, row: int, col: int) -> None:
        """Handle cell click — dismiss fire indicator on Status column."""
        if col != self.COL_STATUS:
            return
        if row < 0 or row >= len(self.sessions):
            return
        tag = self.sessions[row]['tag']
        if tag not in self._state_changed_at or tag in self._dismissed_new_status:
            return
        threshold = self._prefs.get('new_status_seconds', 60)
        cli_state = self.sessions[row].get('cli_state', CLIState.IDLE)
        changed_at = self._state_changed_at[tag][1]
        if (threshold > 0
                and cli_state not in (CLIState.RUNNING, CLIState.INTERRUPTED)
                and (time.time() - changed_at) < threshold):
            self._dismissed_new_status.add(tag)
            self._update_table()

    def _extract_cell_text(self, row: int, col: int) -> str:
        """Extract the displayed text from a cell (item or widget)."""
        # Try QTableWidgetItem text first
        item = self.table.item(row, col)
        if item and item.text():
            return item.text()
        # For widget-based cells, extract text from child labels
        widget = self.table.cellWidget(row, col)
        if not widget:
            return ''
        # Check for ElidedLabel (has _full_text with the non-elided value)
        elided = widget.findChild(ElidedLabel)
        if elided and elided._full_text:
            return elided._full_text
        # Check for PulsingLabel / QLabel children (skip fire icons)
        for label in widget.findChildren(QLabel):
            label_text = label.text()
            if label_text and label.objectName() not in (
                '_fireLabel', '_prFireLabel',
            ):
                return label_text
        if isinstance(widget, QLabel) and widget.text():
            return widget.text()
        return ''

    def _copy_cell_to_clipboard(self, row: int, col: int) -> bool:
        """Copy cell text to clipboard. Return True if something was copied."""
        text = self._extract_cell_text(row, col)
        if not text:
            return False
        QApplication.clipboard().setText(text)
        self._show_status(f'Copied: {text}')
        return True

    def _apply_header_tooltips(self) -> None:
        """Set or clear column header tooltips based on show_tooltips preference."""
        enabled = self._prefs.get('show_tooltips', True)
        right_click_hint = 'Right-click header to show/hide columns'
        for col, desc in self._col_tooltip_descriptions.items():
            item = self.table.horizontalHeaderItem(col)
            if not item:
                continue
            if enabled:
                all_lines = desc.split('\n') + [right_click_hint]
                max_len = max(len(line) for line in all_lines)
                separator = '\u2500' * max_len
                item.setToolTip(f'{desc}\n{separator}\n{right_click_hint}')
            else:
                item.setToolTip('')

    def _apply_tooltips_setting(self) -> None:
        """Sync the tooltip app with the current preference."""
        if hasattr(self, '_tooltip_app'):
            self._tooltip_app.tooltips_enabled = self._prefs.get('show_tooltips', True)
        self._apply_header_tooltips()

    def _is_slack_installed(self) -> bool:
        """Check if the Slack app config file exists."""
        return is_slack_installed()

    def _show_slack_bot_not_running(self) -> None:
        """Show an informational popup when the Slack bot is not running."""
        QMessageBox.information(
            self, 'No Slack Bot Running',
            'Start the Slack bot using the Slack Bot button in the toolbar,\n'
            'or run  leap --slack  in a terminal.',
        )

    def _check_slack_bot_transition(self) -> None:
        """Detect Slack bot start/stop transitions and show status messages."""
        bot_running = self._is_slack_bot_running()
        was_running = self._slack_bot_was_running
        if bot_running == was_running:
            return
        self._slack_bot_was_running = bot_running

        slack_sessions = [
            s for s in self.sessions
            if s.get('slack_enabled') and not s.get('is_dead', True)
        ]
        count = len(slack_sessions)

        if not bot_running and count:
            self._show_status(
                f'Slack bot stopped - {count} session(s) disconnected')
        elif bot_running and count:
            self._show_status(
                f'Slack bot reconnected - {count} session(s) restored')

    def _toggle_slack(self, tag: str, enabled: bool) -> None:
        """Send set_slack to the Leap server to enable/disable Slack."""
        socket_path = SOCKET_DIR / f"{tag}.sock"
        response = send_socket_request(
            socket_path, {'type': 'set_slack', 'enabled': enabled},
        )
        if response and response.get('status') == 'ok':
            # Invalidate cache so next refresh rebuilds
            self._cell_cache.pop((tag, 'slack'), None)
            action = 'enabled' if enabled else 'disabled'
            self._show_status(f'Slack {action} for {tag}')
        else:
            self._show_status(f'Failed to toggle Slack for {tag}')

    def _open_slack_thread(self, tag: str) -> None:
        """Open the Slack thread for a session in the Slack app or browser.

        Prefers the native Slack app via ``slack://channel`` deep link.
        Falls back to the web client URL when the app is not installed.
        """
        config = load_slack_config()
        channel_id = config.get('dm_channel_id', '')

        if not channel_id:
            self._show_status('Slack not configured (missing dm_channel_id)')
            return

        team_id = resolve_team_id()
        sessions = load_slack_sessions()
        thread_ts = sessions.get(tag, {}).get('thread_ts', '')

        # Try native Slack app first
        slack_app_installed = any(
            p.is_dir() for p in (
                Path('/Applications/Slack.app'),
                Path.home() / 'Applications' / 'Slack.app',
            )
        )

        if slack_app_installed and team_id:
            deep = f'slack://channel?team={team_id}&id={channel_id}'
            if thread_ts:
                # Thread-level: use message permalink format
                ts_no_dot = thread_ts.replace('.', '')
                deep = (f'slack://channel?team={team_id}'
                        f'&id={channel_id}&message={ts_no_dot}')
            subprocess.Popen(
                ['open', deep],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return

        # Fallback: open in browser
        if team_id:
            url = f'https://app.slack.com/client/{team_id}/{channel_id}'
            if thread_ts:
                url += f'/thread/{channel_id}-{thread_ts}'
        else:
            url = f'https://app.slack.com/client/{channel_id}'

        webbrowser.open(url)

    def _check_row_hover(self) -> None:
        """Poll cursor position to track which table row is hovered."""
        # Keep hover locked while a context menu is open
        if QApplication.activePopupWidget():
            return

        viewport = self.table.viewport()
        local_pos = viewport.mapFromGlobal(QCursor.pos())

        if viewport.rect().contains(local_pos):
            index = self.table.indexAt(local_pos)
            row = index.row() if index.isValid() else -1
        else:
            row = -1

        if row != self._hovered_row:
            old = self._hovered_row
            self._hovered_row = row
            self.table.setProperty('_hovered_row', row)
            self._apply_hover_to_row(old, False)
            self._apply_hover_to_row(row, True)
            viewport.update()
