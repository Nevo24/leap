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

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from leap.cli_providers.registry import get_display_name
from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.pr_tracking.config import (
    load_dialog_geometry, save_dialog_geometry,
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

    # Column indices
    _COL_CLI = 0
    _COL_TAG = 1
    _COL_AGE = 2
    _COL_SESSION = 3
    _COL_CWD = 4

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
            'Filter by tag or CLI…')
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self._search_edit)

        # Table
        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(
            ['CLI', 'Tag', 'Age', 'Session', 'Working directory'])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        # Double-click on a row should accept (same as picking + OK).
        self._table.itemDoubleClicked.connect(lambda _i: self._accept_if_selected())

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(self._COL_CLI, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_TAG, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_AGE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_SESSION, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(self._COL_CWD, QHeaderView.Stretch)

        layout.addWidget(self._table, 1)

        # The dialog now does only the *picking* step — after Accept,
        # the caller spawns ``leap --resume --cli=… --tag=… --session=…``
        # in a new terminal so the user finishes the flow (cwd choice
        # for cwd-bound CLIs, server hand-off) interactively.  No
        # "Use ~/" toggle here; that decision belongs in the terminal.
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
        """
        self._table.setRowCount(0)
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
            for col, item in (
                (self._COL_CLI, cli_item),
                (self._COL_TAG, tag_item),
                (self._COL_AGE, age_item),
                (self._COL_SESSION, sess_item),
                (self._COL_CWD, cwd_item),
            ):
                item.setTextAlignment(Qt.AlignCenter)
                self._table.setItem(i, col, item)
        if rows:
            self._table.selectRow(0)

    def _apply_filter(self, text: str) -> None:
        """Filter rows by substring match on tag or CLI."""
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

        Tag/CLI matches come first, cwd-only matches after — typing a
        tag fragment shouldn't be drowned out by a working-directory
        hit on a different row.  Each priority bucket is then sorted
        by freshness so the most recently used session always rises
        to the top of its bucket.
        """
        q = text.strip().lower()
        if not q:
            return sorted(self._rows, key=self._row_freshness, reverse=True)
        tag_hits: list[TagRow] = []
        cwd_hits: list[TagRow] = []
        for r in self._rows:
            newest_cwd = _shorten_cwd(r.sessions[0].cwd) if r.sessions else ""
            if (q in r.tag.lower()
                    or q in r.cli.lower()
                    or q in get_display_name(r.cli).lower()):
                tag_hits.append(r)
            elif q in newest_cwd.lower():
                cwd_hits.append(r)
        tag_hits.sort(key=self._row_freshness, reverse=True)
        cwd_hits.sort(key=self._row_freshness, reverse=True)
        return tag_hits + cwd_hits

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

    @staticmethod
    def has_resumable_sessions(storage_dir: Path) -> bool:
        """Quick check the caller can use to short-circuit to a message box."""
        for row in load_tag_rows(storage_dir):
            if row.sessions:
                return True
        return False

    # ── Persistence ───────────────────────────────────────────────────

    def done(self, result: int) -> None:
        """Save dialog size on close."""
        save_dialog_geometry('resume_session', self.width(), self.height())
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
            f"sessions — pick one to resume:"
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
