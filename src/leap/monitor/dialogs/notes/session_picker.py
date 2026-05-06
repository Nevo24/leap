"""Modal dialog for the Notes-dialog "Run in Session" action.

Shown when the user picks a session to send a note's content to —
returns the chosen tag, queue position (next vs end), and whether to
include completed checklist items.
"""

from typing import Optional

from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QHeaderView, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.pr_tracking.config import (
    load_monitor_prefs, load_send_position, save_monitor_prefs,
)
from leap.monitor.session_manager import get_active_sessions
from leap.monitor.ui.image_text_edit import _build_send_position_toggle


class _SessionPickerDialog(ZoomMixin, QDialog):
    """Modal dialog to choose a running Leap session and send mode."""

    _DEFAULT_SIZE = (480, 300)

    def __init__(self, sessions: list[dict], aliases: dict,
                 is_checklist: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Select Session')
        self.resize(*self._DEFAULT_SIZE)
        self._result: Optional[tuple[str, bool]] = None
        self._is_checklist = is_checklist

        layout = QVBoxLayout(self)

        self._table = QTableWidget(len(sessions), 3)
        self._table.setHorizontalHeaderLabels(['Tag', 'Project', 'State'])
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents)

        self._tags: list[str] = []
        for row, session in enumerate(sessions):
            tag = session['tag']
            self._tags.append(tag)
            display_tag = aliases.get(tag, tag)
            self._table.setItem(row, 0, QTableWidgetItem(display_tag))
            self._table.setItem(
                row, 1, QTableWidgetItem(session.get('project', 'N/A')))
            state = session.get('cli_state', '')
            if hasattr(state, 'value'):
                state = state.value
            self._table.setItem(row, 2, QTableWidgetItem(str(state)))
        self._table.doubleClicked.connect(self._on_send_clicked)
        layout.addWidget(self._table)

        if sessions:
            self._table.selectRow(0)

        # "Include completed" — only shown for checklist notes
        self._include_completed = QCheckBox('Include completed checkboxes')
        self._include_completed.setToolTip(
            'Include checked items when sending to session')
        if is_checklist:
            prefs = load_monitor_prefs()
            self._include_completed.setChecked(
                prefs.get('run_session_include_completed', False))
            layout.addWidget(self._include_completed)

        toggle_row, _group, _next_radio, _end_radio = (
            _build_send_position_toggle(self))
        layout.addLayout(toggle_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        send_btn = QPushButton('Send')
        send_btn.setDefault(True)
        send_btn.clicked.connect(self._on_send_clicked)
        btn_row.addWidget(send_btn)
        layout.addLayout(btn_row)

        self._init_zoom(
            pref_key='session_picker_font_size',
            content_pref_key='session_picker_text_font_size',
            content_widgets=[self._table],
        )

    @property
    def include_completed(self) -> bool:
        """Whether the 'Include completed' checkbox is checked."""
        return self._include_completed.isChecked()

    def _accept(self, at_end: bool) -> None:
        row = self._table.currentRow()
        if row < 0 or row >= len(self._tags):
            return
        self._result = (self._tags[row], at_end)
        self.accept()

    def _on_send_clicked(self) -> None:
        """Read the persisted toggle and dispatch to _accept."""
        at_end = load_send_position() != 'next'
        self._accept(at_end)

    @staticmethod
    def pick_session(
        parent: Optional[QWidget] = None,
        is_checklist: bool = False,
    ) -> Optional[tuple[str, bool, bool]]:
        """Show the picker and return (tag, at_end, include_completed) or None."""
        sessions = get_active_sessions()
        if not sessions:
            QMessageBox.information(
                parent, 'Run in Session', 'No active sessions found.')
            return None
        prefs = load_monitor_prefs()
        aliases = prefs.get('aliases', {})
        # Match the main-window order (drag-and-drop reorder is persisted
        # as ``row_order`` in monitor prefs).  Unknown tags go to the end.
        row_order = prefs.get('row_order', [])
        order_map = {tag: i for i, tag in enumerate(row_order)}
        sessions.sort(key=lambda s: order_map.get(s['tag'], float('inf')))
        dlg = _SessionPickerDialog(sessions, aliases, is_checklist, parent)
        accepted = dlg.exec_() == QDialog.Accepted
        # Persist checkbox state on any close (OK, Cancel, or X)
        if is_checklist:
            prefs = load_monitor_prefs()
            prefs['run_session_include_completed'] = dlg.include_completed
            save_monitor_prefs(prefs)
        if accepted and dlg._result is not None:
            tag, at_end = dlg._result
            return (tag, at_end, dlg.include_completed)
        return None
