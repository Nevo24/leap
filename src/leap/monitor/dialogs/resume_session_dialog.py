"""Dialog for picking a CLI session to resume.

GUI counterpart of ``leap --resume`` — but only the *picking* step.
Shows one row per recorded ``(cli, tag)`` pair, newest-first; tags
with more than one session open a modal sub-picker.  Selection
returns ``(cli, tag, SessionRecord)``; the caller spawns
``leap --resume --cli=… --tag=… --session=…`` in a new terminal so
the user finishes the flow (cwd choice for cwd-bound CLIs, tag
rename, provider hand-off) interactively from there.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from leap.cli_providers.registry import get_display_name
from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.pr_tracking.config import (
    load_dialog_geometry, load_resume_hidden_columns,
    load_resume_open_in_last_app, save_dialog_geometry,
    save_resume_hidden_columns, save_resume_open_in_last_app,
)
from leap.monitor.themes import current_theme
from leap.monitor.ui.table_helpers import (
    SubtleColumnSeparatorDelegate, SubtleColumnSeparatorHeaderView,
    build_column_visibility_menu,
)
from leap.utils.resume_store import SessionRecord, TagRow, load_tag_rows


def _format_age(ts: float) -> str:
    """Human-readable "Xs/m/h/d ago" — mirrors leap-resume.py output."""
    if ts <= 0:
        return "unknown"
    delta = max(0.0, time.time() - ts)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def _shorten_cwd(cwd: str) -> str:
    """Replace the user's home prefix with ``~`` (mirrors leap-resume.py)."""
    if not cwd:
        return ""
    home = str(Path.home())
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return "~" + cwd[len(home):]
    return cwd


def _format_size(n: int) -> str:
    """Render a transcript size as ``Xb/KB/MB/GB``."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)}{unit}"
        n //= 1024
    return f"{int(n)}TB"


class ResumeSessionDialog(ZoomMixin, QDialog):
    """Tag-level picker over recorded CLI sessions.

    One row per ``(cli, tag)`` pair.  Tags with multiple recorded
    sessions show ``N sessions`` in the Session column and route
    through :class:`_TagSessionPicker` after the user picks the row;
    single-session tags accept directly.
    """

    _DEFAULT_SIZE = (820, 460)

    # Column indices — order mirrors the main monitor table:
    # Tag → CLI → App → Project → Path, then the Resume-specific
    # Age + Session metadata.  The first five also have visibility
    # controlled by the main table's ``hidden_columns`` pref so a
    # column the user hid in the monitor disappears here too.
    _COL_TAG = 0
    _COL_CLI = 1
    _COL_APP = 2
    _COL_PROJECT = 3
    _COL_PATH = 4
    _COL_AGE = 5
    _COL_SESSION = 6

    # Labels in column order; used by ``setHorizontalHeaderLabels`` and
    # by the per-column visibility menu.  Tag is the only mandatory
    # column (a Resume picker without it is useless); every other
    # column is toggleable via the header's right-click menu and the
    # choice persists per dialog in ``resume_session_hidden_columns``.
    _COL_LABELS = ['Tag', 'CLI', 'App', 'Project', 'Path', 'Age', 'Session']
    _MANDATORY_LABEL = 'Tag'

    def __init__(
        self,
        storage_dir: Path,
        parent: object = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle('Resume CLI Session')
        self.resize(*self._DEFAULT_SIZE)
        saved = load_dialog_geometry('resume_session')
        if saved:
            self.resize(saved[0], saved[1])

        # One TagRow per (cli, tag).  load_tag_rows already drops stale
        # transcripts and dedups across tags, so the rows here are
        # exactly what the user can resume.  Sort newest-first by the
        # tag's most recent session's last_seen — same key the filter
        # uses, so unfiltered + filtered displays stay consistent.
        self._rows: list[TagRow] = sorted(
            load_tag_rows(storage_dir),
            key=self._row_freshness,
            reverse=True,
        )
        # Result populated after sub-picker (or directly for single-session tags).
        self._chosen: Optional[tuple[str, str, SessionRecord]] = None

        layout = QVBoxLayout()
        self.setLayout(layout)

        layout.addWidget(QLabel('Pick a recorded CLI session to resume:'))

        # Search filter
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(
            'Filter by tag, project, app, or CLI…')
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self._search_edit)

        # Table
        self._table = QTableWidget(0, len(self._COL_LABELS), self)
        # Install BEFORE setHorizontalHeaderLabels so the header is
        # the custom view from the start (avoids a flash of the
        # default header on first paint).  Delegate handles cell
        # separators; header view handles the header-row separators
        # — together they look like one continuous vertical line.
        self._table.setHorizontalHeader(
            SubtleColumnSeparatorHeaderView(Qt.Horizontal, self._table))
        self._table.setItemDelegate(
            SubtleColumnSeparatorDelegate(self._table))
        self._table.setHorizontalHeaderLabels(self._COL_LABELS)
        # ``setStretchLastSection`` defaults to True — combined with
        # our explicit ``Stretch`` mode on ``_COL_PATH`` it produces a
        # small visual artefact (a bump / partial-section on the last
        # column's right edge) because Qt runs both stretches and
        # they fight each other for the leftover pixels.  Disable so
        # only Path stretches and the rightmost section ends cleanly.
        self._table.horizontalHeader().setStretchLastSection(False)
        # Corner button (intersection of vertical + horizontal headers)
        # — invisible when the vertical header is hidden, but disabling
        # it is the documented way to avoid stray paint events on the
        # top-right corner.
        self._table.setCornerButtonEnabled(False)
        # Mirror the thin themed scrollbar from MonitorWindow's app-level
        # QSS (which doesn't cascade into this dialog).  The default
        # Qt scrollbar on macOS is the chunky pill-shaped one that the
        # user spotted as "a bump" in the top-right of the table.  Use
        # the same theme colours + thin (8px) layout the main table
        # uses so the scrollbar looks consistent across surfaces.
        t = current_theme()
        sb_bg = t.scrollbar_bg or t.window_bg
        sb_handle = t.scrollbar_handle or t.border_solid
        sb_hover = t.scrollbar_handle_hover or t.text_muted
        self._table.setStyleSheet(
            (self._table.styleSheet() or '')
            + f'''
QScrollBar:vertical {{
    background: {sb_bg}; width: 8px; margin: 0; border: none;
}}
QScrollBar::handle:vertical {{
    background: {sb_handle}; min-height: 30px; border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{ background: {sb_hover}; }}
QScrollBar:horizontal {{
    background: {sb_bg}; height: 8px; margin: 0; border: none;
}}
QScrollBar::handle:horizontal {{
    background: {sb_handle}; min-width: 30px; border-radius: 4px;
}}
QScrollBar::handle:horizontal:hover {{ background: {sb_hover}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0; border: none; background: transparent;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0; border: none; background: transparent;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QTableCornerButton::section {{ background: transparent; border: none; }}
'''
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        # Double-click on a row should accept (same as picking + OK).
        self._table.itemDoubleClicked.connect(lambda _i: self._accept_if_selected())

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(self._COL_TAG, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_CLI, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_APP, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_PROJECT, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_PATH, QHeaderView.Stretch)
        header.setSectionResizeMode(self._COL_AGE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_SESSION, QHeaderView.ResizeToContents)

        # Restore the user's per-column visibility choices for THIS
        # dialog (independent from the main monitor table — Resume
        # serves a different purpose, so the user toggles each set
        # separately).  Tag is non-toggleable so it's force-shown
        # even if the persisted list (e.g. hand-edited prefs) names it.
        hidden_columns = set(load_resume_hidden_columns())
        for col_idx, label in enumerate(self._COL_LABELS):
            if label == self._MANDATORY_LABEL:
                continue
            if label in hidden_columns:
                self._table.setColumnHidden(col_idx, True)

        # Right-click the header to toggle column visibility.  Mirrors
        # the main monitor table's UX so the gesture is familiar.
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self._show_column_menu)

        layout.addWidget(self._table, 1)

        # "Open in the app the session was last run in" toggle — when
        # checked, the caller passes ``record.terminal_app`` as
        # ``preferred_ide`` to ``open_resume_in_terminal``, so the resume
        # relaunches in iTerm2 (or wherever the session was last seen)
        # instead of the user's global ``default_terminal`` pref.  When
        # off, or when the record has no recorded app (pre-feature
        # entries), the global default is used.
        self._open_in_last_app_check = QCheckBox(
            'Open in the app the session was last run in')
        self._open_in_last_app_check.setChecked(load_resume_open_in_last_app())
        layout.addWidget(self._open_in_last_app_check)

        # The dialog does only the *picking* step — after Accept, the
        # caller spawns ``leap --resume --cli=… --tag=… --session=…`` in
        # a new terminal so the user finishes the flow (cwd choice for
        # cwd-bound CLIs, server hand-off) interactively.
        # Cancel bottom-left, OK bottom-right.
        btn_row = QHBoxLayout()
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch()
        ok_btn = QPushButton('OK')
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._accept_if_selected)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        self._populate(self._rows)
        # Focus the search box so the user can start typing immediately —
        # arrow keys are forwarded to the table from there (see eventFilter)
        # so navigation works without first clicking on the table.
        self._search_edit.installEventFilter(self)
        self._search_edit.setFocus()

        self._init_zoom(
            pref_key='resume_session_font_size',
            content_pref_key='resume_session_text_font_size',
            content_widgets=[self._table],
        )

    # ── Key forwarding ────────────────────────────────────────────────

    _NAV_KEYS = frozenset({
        Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown,
    })

    def eventFilter(self, obj, event):
        """Forward Up/Down/PgUp/PgDn from the search box to the table.

        Lets the user navigate immediately after the dialog opens
        without having to click the table first.  Home/End/Left/Right
        stay in the QLineEdit (cursor movement) so search editing
        keeps working normally.  Defers everything else to ``ZoomMixin``
        via ``super().eventFilter`` so Cmd+wheel/± still zooms.
        """
        if (obj is self._search_edit
                and event.type() == QEvent.KeyPress
                and event.key() in self._NAV_KEYS):
            QApplication.sendEvent(self._table, event)
            return True
        return super().eventFilter(obj, event)

    # ── Population / filtering ────────────────────────────────────────

    def _populate(self, rows: list[TagRow]) -> None:
        """Replace the table contents with *rows* (one entry per tag).

        All cells are centered horizontally + vertically per design
        request — keeps the picker visually balanced even when the
        Working-directory column stretches across the row.

        When *rows* is empty AND the search box has text, render a
        single spanned "No matches" placeholder row so the user has a
        positive signal that their filter just didn't hit anything (vs
        wondering if the dialog is broken).  No filter + no rows
        leaves the table empty — the caller is expected to skip the
        dialog entirely in that case via ``has_resumable_sessions``.
        """
        header = self._table.horizontalHeader()

        going_empty = (
            (not rows) and getattr(self, '_was_populated', False)
        )
        going_populated_from_empty = (
            bool(rows) and getattr(self, '_saved_col_modes', None) is not None
        )

        # ─── Transition: populated → empty ───────────────────────
        # Capture widths AND modes, switch every column to Interactive,
        # and immediately reapply the captured widths — all BEFORE any
        # setRowCount call below.  Doing all three up-front means the
        # upcoming setRowCount(0) (and subsequent setRowCount(1) in the
        # placeholder branch) runs against a fully Interactive header,
        # so Stretch/ResizeToContents can't fire and reflow the columns.
        # The original empty-state code did the mode switch *after*
        # setRowCount(0), by which point Stretch had already inflated
        # PATH and shrunk the ResizeToContents columns — freezing the
        # wrong widths regardless of subsequent setColumnWidth calls.
        if going_empty:
            self._saved_col_widths = [
                self._table.columnWidth(c)
                for c in range(self._table.columnCount())
            ]
            self._saved_col_modes = [
                header.sectionResizeMode(c)
                for c in range(self._table.columnCount())
            ]
            for c in range(self._table.columnCount()):
                header.setSectionResizeMode(c, QHeaderView.Interactive)
            for c, w in enumerate(self._saved_col_widths):
                if 0 <= c < self._table.columnCount() and w > 0:
                    self._table.setColumnWidth(c, w)

        # Always drop any prior placeholder span/text BEFORE resizing
        # the row count.  Cells in this dialog are pure ``QTableWidgetItem``
        # (no ``setCellWidget`` overlays) so we don't have the
        # bleed-through risk the main monitor table had, but stale text
        # could still flash on transitions if we left it intact.
        if self._table.columnSpan(0, 0) > 1:
            self._table.setSpan(0, 0, 1, 1)
            prior_item = self._table.item(0, 0)
            if prior_item and prior_item.text():
                prior_item.setText('')

        self._table.setRowCount(0)

        if not rows:
            self._was_populated = False
            if self._search_edit.text().strip():
                # All width preservation already happened in the
                # ``going_empty`` block above (or on the previous empty
                # tick when ``_saved_col_widths`` was first captured).
                # Subsequent empty ticks with the same query just
                # rebuild the placeholder row; columns are already
                # Interactive at the saved widths from the first
                # transition.
                self._table.setRowCount(1)
                total_cols = self._table.columnCount()
                self._table.setSpan(0, 0, 1, total_cols)
                placeholder = 'No matches'
                item = self._table.item(0, 0)
                if not item:
                    item = QTableWidgetItem(placeholder)
                    self._table.setItem(0, 0, item)
                else:
                    item.setText(placeholder)
                item.setTextAlignment(Qt.AlignCenter)
                # Strip selectable + enabled flags so the placeholder
                # doesn't take focus, can't be selected, and won't
                # respond to Enter / double-click.  ``_accept_if_selected``
                # already guards against an out-of-range index, but
                # disabling the row is cleaner UX — no hover highlight,
                # no false sense that this is a pickable entry.
                item.setFlags(
                    item.flags() & ~Qt.ItemIsSelectable & ~Qt.ItemIsEnabled)
                item.setForeground(QColor(current_theme().text_muted))
            return

        self._was_populated = True
        self._table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            newest = row.sessions[0]
            cli_item = QTableWidgetItem(get_display_name(row.cli))
            cli_item.setToolTip(row.cli)
            tag_item = QTableWidgetItem(row.tag)
            age_item = QTableWidgetItem(_format_age(newest.last_seen))
            nsess = len(row.sessions)
            if nsess > 1:
                sess_label = f"{nsess} sessions"
                sess_tip = "\n".join(
                    f"{s.session_id[:8]} · {_format_age(s.last_seen)}"
                    for s in row.sessions
                )
            else:
                sess_label = newest.session_id[:8]
                sess_tip = newest.session_id
            sess_item = QTableWidgetItem(sess_label)
            sess_item.setToolTip(sess_tip)
            cwd_short = _shorten_cwd(newest.cwd)
            cwd_item = QTableWidgetItem(cwd_short)
            cwd_item.setToolTip(newest.cwd)
            # ``terminal_app`` lands here from the hook (read from <tag>.meta's
            # ``ide`` field at record time).  Empty for pre-feature records.
            app_item = QTableWidgetItem(newest.terminal_app or '')
            if newest.terminal_app:
                app_item.setToolTip(newest.terminal_app)
            # ``project_path`` — display just the basename (project folder
            # name) so the column stays compact; full path lives in the
            # tooltip and in the Working directory column.  Empty for
            # pre-feature records that don't have project_path stored.
            project_basename = (
                os.path.basename(newest.project_path.rstrip(os.sep))
                if newest.project_path else ''
            )
            project_item = QTableWidgetItem(project_basename)
            if newest.project_path:
                project_item.setToolTip(newest.project_path)
            for col, item in (
                (self._COL_TAG, tag_item),
                (self._COL_CLI, cli_item),
                (self._COL_APP, app_item),
                (self._COL_PROJECT, project_item),
                (self._COL_PATH, cwd_item),
                (self._COL_AGE, age_item),
                (self._COL_SESSION, sess_item),
            ):
                item.setTextAlignment(Qt.AlignCenter)
                self._table.setItem(i, col, item)

        # ─── Transition: empty → populated ───────────────────────
        # Restore the captured resize modes now that the cells are in
        # place.  Switching back to Stretch/ResizeToContents triggers
        # Qt to remeasure based on the freshly-populated content, so
        # PATH stretches into remaining space and the other columns
        # auto-fit their data — naturally undoing the Interactive
        # freeze we used to preserve widths during empty state.
        if going_populated_from_empty:
            for c, mode in enumerate(self._saved_col_modes):
                if 0 <= c < self._table.columnCount():
                    header.setSectionResizeMode(c, mode)
            self._saved_col_modes = None

        if rows:
            self._table.selectRow(0)

    def _apply_filter(self, text: str) -> None:
        """Filter rows by substring match on tag, project, app, or CLI."""
        self._populate(self._filtered_rows(text))

    @staticmethod
    def _row_freshness(r: TagRow) -> float:
        """Sort key: freshest session's ``last_seen`` (0 for empty rows).

        Used everywhere we need newest-first ordering — both the
        unfiltered display and each bucket of the filter so the
        contract is locally guaranteed regardless of input order.
        """
        return r.sessions[0].last_seen if r.sessions else 0.0

    def _filtered_rows(self, text: str) -> list[TagRow]:
        """Return the subset of ``self._rows`` matching *text*, sorted
        newest-first.

        Each row is bucketed by where the query first matches, then
        each bucket is sorted by freshness so the most recently used
        session rises to the top of its bucket.  Buckets, in order:

        1. Tag (most specific identifier — typing a tag fragment
           should never be drowned out by a broader match on a
           different row)
        2. Project (basename of ``project_path`` — usually how users
           remember which session was which: "the leap one")
        3. App (e.g. ``iTerm2``, ``PyCharm`` — terminal app the
           session was last seen in)
        4. CLI (e.g. ``claude``, ``Claude Code`` — least discriminating
           field for most users, kept above Path so a CLI-name hit
           still wins over an incidental path substring)
        5. Path (full cwd — kept last because it's the longest field
           and most likely to incidentally substring-match)

        Each row appears in exactly one bucket (its highest-priority
        match) so a tag match isn't duplicated by a coincidental path
        match further down.
        """
        q = text.strip().lower()
        if not q:
            return sorted(self._rows, key=self._row_freshness, reverse=True)
        tag_hits: list[TagRow] = []
        project_hits: list[TagRow] = []
        app_hits: list[TagRow] = []
        cli_hits: list[TagRow] = []
        path_hits: list[TagRow] = []
        for r in self._rows:
            newest = r.sessions[0] if r.sessions else None
            newest_cwd = _shorten_cwd(newest.cwd) if newest else ""
            app = (newest.terminal_app or '') if newest else ''
            project_basename = (
                os.path.basename(newest.project_path.rstrip(os.sep))
                if newest and newest.project_path else ''
            )
            if q in r.tag.lower():
                tag_hits.append(r)
            elif q in project_basename.lower():
                project_hits.append(r)
            elif q in app.lower():
                app_hits.append(r)
            elif (q in r.cli.lower()
                    or q in get_display_name(r.cli).lower()):
                cli_hits.append(r)
            elif q in newest_cwd.lower():
                path_hits.append(r)
        for bucket in (tag_hits, project_hits, app_hits, cli_hits, path_hits):
            bucket.sort(key=self._row_freshness, reverse=True)
        return tag_hits + project_hits + app_hits + cli_hits + path_hits

    # ── Selection ─────────────────────────────────────────────────────

    def _selected_index(self) -> int:
        """Return the currently-selected visible row index, or -1."""
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return -1
        return rows[0].row()

    def _visible_rows(self) -> list[TagRow]:
        """Reconstruct the row list currently shown in the table."""
        return self._filtered_rows(self._search_edit.text())

    # ── Column visibility ────────────────────────────────────────────

    def _show_column_menu(self, pos: QPoint) -> None:
        """Header right-click — toggle column visibility per column.

        Tag is omitted from the menu since it's mandatory.
        """
        menu = build_column_visibility_menu(
            self,
            self._table,
            self._COL_LABELS,
            on_toggle=self._toggle_column,
            skip=lambda _i, label: label == self._MANDATORY_LABEL,
        )
        header = self._table.horizontalHeader()
        menu.exec_(header.mapToGlobal(pos))

    def _toggle_column(self, col_idx: int, label: str, visible: bool) -> None:
        """Toggle column ``col_idx`` and persist the choice.

        ``visible`` is the new state requested by the menu's toggle.
        We read+write the full hidden-columns list each call (cheap;
        list is at most ~7 entries) to avoid coupling to any in-memory
        cache the dialog might hold across opens.
        """
        self._table.setColumnHidden(col_idx, not visible)
        hidden = set(load_resume_hidden_columns())
        # Tag is never toggleable, but guard anyway so a hand-coded
        # call can't accidentally persist a forbidden hide.
        if label == self._MANDATORY_LABEL:
            hidden.discard(label)
        elif visible:
            hidden.discard(label)
        else:
            hidden.add(label)
        save_resume_hidden_columns(sorted(hidden))

    def _accept_if_selected(self) -> None:
        """Accept the dialog when a row is selected.

        Tags with more than one recorded session route through a modal
        sub-picker so the user can choose which session to resume; the
        outer dialog only accepts after the sub-picker returns a pick.
        """
        idx = self._selected_index()
        if idx < 0:
            return
        rows = self._visible_rows()
        if not 0 <= idx < len(rows):
            return
        row = rows[idx]
        if len(row.sessions) == 1:
            self._chosen = (row.cli, row.tag, row.sessions[0])
            self.accept()
            return
        sub = _TagSessionPicker(row, self)
        if sub.exec_() != QDialog.Accepted:
            return  # Cancel in sub-picker — stay in the tag picker
        sess = sub.selected_session()
        if sess is None:
            return
        self._chosen = (row.cli, row.tag, sess)
        self.accept()

    def selected_session(self) -> Optional[tuple[str, str, SessionRecord]]:
        """Return ``(cli, tag, SessionRecord)`` for the picked row."""
        return self._chosen

    @property
    def open_in_last_app(self) -> bool:
        """Whether the user wants the resume to open in the recorded app.

        Caller reads this after ``exec_()`` returns ``Accepted`` and
        decides whether to thread ``record.terminal_app`` through to
        ``open_resume_in_terminal`` or fall back to the global default.
        """
        return self._open_in_last_app_check.isChecked()

    @staticmethod
    def has_resumable_sessions(storage_dir: Path) -> bool:
        """Quick check the caller can use to short-circuit to a message box."""
        for row in load_tag_rows(storage_dir):
            if row.sessions:
                return True
        return False

    # ── Persistence ───────────────────────────────────────────────────

    def done(self, result: int) -> None:
        """Save dialog size + the "open in last app" toggle on close.

        Persist the checkbox state on every close (Accept *and* Cancel)
        so the user's preference sticks even when they back out of the
        dialog.
        """
        save_dialog_geometry('resume_session', self.width(), self.height())
        save_resume_open_in_last_app(self._open_in_last_app_check.isChecked())
        super().done(result)


class _TagSessionPicker(ZoomMixin, QDialog):
    """Sub-picker shown when a tag has more than one recorded session.

    Tag picker → (this dialog) → caller resumes the picked session.
    Cancelling here returns the user to the tag picker without
    closing the outer dialog.  Geometry persisted under its own key
    so it doesn't fight the parent dialog's size.
    """

    _DEFAULT_SIZE = (640, 360)
    _COL_AGE = 0
    _COL_SESSION = 1
    _COL_SIZE = 2
    _COL_CWD = 3

    def __init__(self, tag_row: TagRow, parent: object = None) -> None:
        super().__init__(parent)
        self._tag_row = tag_row
        self._chosen: Optional[SessionRecord] = None
        cli_name = get_display_name(tag_row.cli)
        self.setWindowTitle(f"Sessions for [{cli_name}] {tag_row.tag}")
        self.resize(*self._DEFAULT_SIZE)
        saved = load_dialog_geometry('resume_tag_sessions')
        if saved:
            self.resize(saved[0], saved[1])

        layout = QVBoxLayout()
        self.setLayout(layout)
        layout.addWidget(QLabel(
            f"Tag '{tag_row.tag}' has {len(tag_row.sessions)} recorded "
            f"sessions - pick one to resume:"
        ))

        self._table = QTableWidget(len(tag_row.sessions), 4, self)
        self._table.setHorizontalHeaderLabels(
            ['Age', 'Session', 'Size', 'Working directory'])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.itemDoubleClicked.connect(lambda _i: self._accept_if_selected())

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(self._COL_AGE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_SESSION, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_SIZE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_CWD, QHeaderView.Stretch)

        # Defensively re-sort by last_seen DESC so newest-first is
        # guaranteed regardless of how the JSON file is ordered on
        # disk (load_tag_rows reverses file order, but a future writer
        # could change that — sorting here keeps the UI promise).
        self._sessions: list[SessionRecord] = sorted(
            tag_row.sessions, key=lambda s: s.last_seen, reverse=True,
        )
        for i, sess in enumerate(self._sessions):
            age_item = QTableWidgetItem(_format_age(sess.last_seen))
            sess_item = QTableWidgetItem(sess.session_id[:8])
            sess_item.setToolTip(sess.session_id)
            size_item = QTableWidgetItem(_format_size(sess.size))
            cwd_item = QTableWidgetItem(_shorten_cwd(sess.cwd))
            cwd_item.setToolTip(sess.cwd)
            for col, item in (
                (self._COL_AGE, age_item),
                (self._COL_SESSION, sess_item),
                (self._COL_SIZE, size_item),
                (self._COL_CWD, cwd_item),
            ):
                item.setTextAlignment(Qt.AlignCenter)
                self._table.setItem(i, col, item)
        if self._sessions:
            self._table.selectRow(0)
        layout.addWidget(self._table, 1)

        # Cancel bottom-left, OK bottom-right.
        btn_row = QHBoxLayout()
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch()
        ok_btn = QPushButton('OK')
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._accept_if_selected)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        self._init_zoom(
            pref_key='resume_tag_sessions_font_size',
            content_pref_key='resume_tag_sessions_text_font_size',
            content_widgets=[self._table],
        )

    def _accept_if_selected(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        if not 0 <= idx < len(self._sessions):
            return
        self._chosen = self._sessions[idx]
        self.accept()

    def selected_session(self) -> Optional[SessionRecord]:
        return self._chosen

    def done(self, result: int) -> None:
        save_dialog_geometry(
            'resume_tag_sessions', self.width(), self.height())
        super().done(result)
