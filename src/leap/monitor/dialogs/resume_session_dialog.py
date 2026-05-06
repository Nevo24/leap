"""Dialog for picking a CLI session to resume.

GUI counterpart of ``leap --resume``: shows every recorded CLI session
in a flat list (one row per session, newest-first), with a search
filter. Selection returns ``(cli, tag, SessionRecord)``; the caller
handles tag-conflict resolution and spawning the server with
``LEAP_RESUME_*`` env vars.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QHeaderView,
    QLabel, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from leap.cli_providers.registry import get_display_name
from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.pr_tracking.config import (
    load_dialog_geometry, save_dialog_geometry,
)
from leap.utils.resume_store import SessionRecord, load_tag_rows


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


class ResumeSessionDialog(ZoomMixin, QDialog):
    """Flat-list picker over every recorded CLI session."""

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

        # Flatten (TagRow → one entry per SessionRecord). load_tag_rows
        # already drops stale transcripts and dedups across tags, so
        # what's left here is exactly what the user can resume.
        self._rows: list[tuple[str, str, SessionRecord]] = []
        for row in load_tag_rows(storage_dir):
            for s in row.sessions:
                self._rows.append((row.cli, row.tag, s))
        self._rows.sort(key=lambda r: r[2].last_seen, reverse=True)

        layout = QVBoxLayout()
        self.setLayout(layout)

        layout.addWidget(QLabel('Pick a recorded CLI session to resume:'))

        # Search filter
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(
            'Filter by CLI, tag, session id, or cwd…')
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

        # OK / Cancel
        self._btn_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._btn_box.accepted.connect(self._accept_if_selected)
        self._btn_box.rejected.connect(self.reject)
        layout.addWidget(self._btn_box)

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

    def _populate(self, rows: list[tuple[str, str, SessionRecord]]) -> None:
        """Replace the table contents with *rows*."""
        self._table.setRowCount(0)
        self._table.setRowCount(len(rows))
        for i, (cli, tag, sess) in enumerate(rows):
            cli_item = QTableWidgetItem(get_display_name(cli))
            cli_item.setToolTip(cli)
            tag_item = QTableWidgetItem(tag)
            age_item = QTableWidgetItem(_format_age(sess.last_seen))
            sess_item = QTableWidgetItem(sess.session_id[:8])
            sess_item.setToolTip(sess.session_id)
            cwd_short = _shorten_cwd(sess.cwd)
            cwd_item = QTableWidgetItem(cwd_short)
            cwd_item.setToolTip(sess.cwd)
            for col, item in (
                (self._COL_CLI, cli_item),
                (self._COL_TAG, tag_item),
                (self._COL_AGE, age_item),
                (self._COL_SESSION, sess_item),
                (self._COL_CWD, cwd_item),
            ):
                self._table.setItem(i, col, item)
        if rows:
            self._table.selectRow(0)

    def _apply_filter(self, text: str) -> None:
        """Filter rows by substring match on cli/tag/session/cwd."""
        q = text.strip().lower()
        if not q:
            filtered = self._rows
        else:
            filtered = [
                r for r in self._rows
                if q in get_display_name(r[0]).lower()
                or q in r[0].lower()
                or q in r[1].lower()
                or q in r[2].session_id.lower()
                or q in _shorten_cwd(r[2].cwd).lower()
            ]
        self._populate(filtered)

    # ── Selection ─────────────────────────────────────────────────────

    def _selected_index(self) -> int:
        """Return the currently-selected visible row index, or -1."""
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return -1
        return rows[0].row()

    def _visible_rows(self) -> list[tuple[str, str, SessionRecord]]:
        """Reconstruct the row list currently shown in the table.

        Cheap to recompute (same filter as :meth:`_apply_filter`) and
        avoids storing a parallel list that could drift out of sync.
        """
        q = self._search_edit.text().strip().lower()
        if not q:
            return self._rows
        return [
            r for r in self._rows
            if q in get_display_name(r[0]).lower()
            or q in r[0].lower()
            or q in r[1].lower()
            or q in r[2].session_id.lower()
            or q in _shorten_cwd(r[2].cwd).lower()
        ]

    def _accept_if_selected(self) -> None:
        """Accept the dialog only when a row is actually selected."""
        if self._selected_index() < 0:
            return
        self.accept()

    def selected_session(self) -> Optional[tuple[str, str, SessionRecord]]:
        """Return ``(cli, tag, SessionRecord)`` for the picked row."""
        idx = self._selected_index()
        if idx < 0:
            return None
        rows = self._visible_rows()
        if not 0 <= idx < len(rows):
            return None
        return rows[idx]

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
