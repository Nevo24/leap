"""Send-comments-to-session picker dialog.

Replaces the four "Send … thread" menu actions with a single popup that
lets the user pick independently:

* **Which comments** — all unresponded comments, or only comments with
  an unacknowledged ``/leap`` tag.
* **How to send** — one queue message per comment, or all comments
  combined into a single message.
* **PR context preset** — a single-message preset that gets prepended
  to every comment sent to Leap (persisted in
  ``.storage/leap_selected_preset``, the same file ``leap_sender`` reads
  to prepend context on outgoing messages).

All three picks are persisted in ``monitor_prefs.json`` / the preset
selection files so the dialog opens next time on the user's last
choice.
"""

from typing import Optional

from PyQt5.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton,
    QRadioButton, QVBoxLayout, QWidget,
)

from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.pr_tracking.config import (
    load_dialog_geometry, load_saved_presets, load_selected_preset_name,
    load_send_comments_prefs, save_dialog_geometry,
    save_selected_preset_name, save_send_comments_prefs,
)
from leap.monitor.themes import current_theme


_PR_CONTEXT_NONE = '(None)'


class SendCommentsDialog(ZoomMixin, QDialog):
    """Binary-choice picker for sending PR comments to a Leap session."""

    _DEFAULT_SIZE = (460, 380)

    def __init__(
        self,
        parent: Optional[QWidget],
        *,
        auto_fetch_leap: bool,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle('Send comments to session')
        self.resize(*self._DEFAULT_SIZE)
        saved_geom = load_dialog_geometry('send_comments')
        if saved_geom:
            self.resize(saved_geom[0], saved_geom[1])

        saved = load_send_comments_prefs()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(6)

        # -- Which comments ---------------------------------------------
        # When auto-fetch for '/leap' tags is ON, those comments are
        # already queued automatically, so there's no meaningful filter
        # choice left. Hide the whole section; the effective filter is
        # always 'all'.
        self._filter_all = QRadioButton('All unresponded comments')
        self._filter_all.setToolTip(
            'Every comment the SCM reports as unresponded.')
        self._filter_leap = QRadioButton("Only comments with a '/leap' tag")
        self._filter_leap.setToolTip(
            "Only comments that contain an unacknowledged '/leap' tag."
        )

        filter_group = QButtonGroup(self)
        filter_group.addButton(self._filter_all)
        filter_group.addButton(self._filter_leap)
        if not auto_fetch_leap and saved['filter'] == 'leap':
            self._filter_leap.setChecked(True)
        else:
            self._filter_all.setChecked(True)

        if not auto_fetch_leap:
            which_label = QLabel('Which comments to send:')
            which_label.setStyleSheet('font-weight: bold;')
            layout.addWidget(which_label)
            layout.addWidget(self._filter_all)
            layout.addWidget(self._filter_leap)

        # -- How to send ------------------------------------------------
        if not auto_fetch_leap:
            layout.addSpacing(10)
        how_label = QLabel('How to send:')
        how_label.setStyleSheet('font-weight: bold;')
        layout.addWidget(how_label)

        self._mode_each = QRadioButton('Separate messages (one per comment)')
        self._mode_each.setToolTip(
            'Queue each comment as its own message so the CLI\n'
            'handles them one at a time.')
        self._mode_combined = QRadioButton('One combined message')
        self._mode_combined.setToolTip(
            'Concatenate every selected comment into a single queued\n'
            'message so the CLI sees them all at once.')

        mode_group = QButtonGroup(self)
        mode_group.addButton(self._mode_each)
        mode_group.addButton(self._mode_combined)
        if saved['mode'] == 'combined':
            self._mode_combined.setChecked(True)
        else:
            self._mode_each.setChecked(True)

        layout.addWidget(self._mode_each)
        layout.addWidget(self._mode_combined)

        # -- Context preset ---------------------------------------------
        layout.addSpacing(12)
        ctx_label = QLabel('Context preset (single-message):')
        ctx_label.setStyleSheet('font-weight: bold;')
        layout.addWidget(ctx_label)

        self._ctx_combo = QComboBox()
        self._ctx_combo.setToolTip(
            'A single-message preset prepended to every comment sent\n'
            'to the session. Only single-message presets appear here.')
        self._populate_ctx_combo()
        layout.addWidget(self._ctx_combo)

        ctx_help = QLabel(
            'Prepended to every comment sent to the session.')
        ctx_help.setStyleSheet(f'color: {current_theme().text_muted};')
        ctx_help.setWordWrap(True)
        layout.addWidget(ctx_help)

        layout.addStretch()

        hint = QLabel(
            'Each comment is sent with its full thread (all replies) '
            'and the related code context.')
        hint.setStyleSheet(f'color: {current_theme().text_muted};')
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # -- Buttons ----------------------------------------------------
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        send_btn = QPushButton('Send')
        send_btn.setDefault(True)
        send_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(send_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        # Persist on every flip so the choice survives Cancel/close too.
        # Signals connected after initial setChecked above so opening
        # doesn't rewrite the file with the saved value.
        self._filter_all.toggled.connect(self._persist_filter_mode)
        self._filter_leap.toggled.connect(self._persist_filter_mode)
        self._mode_each.toggled.connect(self._persist_filter_mode)
        self._mode_combined.toggled.connect(self._persist_filter_mode)
        self._ctx_combo.currentIndexChanged.connect(self._persist_ctx)

        self._init_zoom(pref_key='send_comments_font_size')

    def _populate_ctx_combo(self) -> None:
        """Fill the PR-context combo with single-message presets.

        PR context requires single-message presets (multi-message presets
        belong in the message-bundle combo), so we filter by
        ``len(messages) <= 1``.

        Self-heal: if the saved selection points at a preset that no
        longer exists *or* has grown to multi-message, clear the saved
        slot so the dialog's "(None)" state stays consistent with what
        ``leap_sender.load_leap_preset()`` will actually prepend.
        """
        self._ctx_combo.blockSignals(True)
        self._ctx_combo.clear()
        self._ctx_combo.addItem(_PR_CONTEXT_NONE)
        names: list[str] = []
        for name, messages in sorted(load_saved_presets().items()):
            if len(messages) <= 1:
                names.append(name)
                self._ctx_combo.addItem(name)
        self._ctx_names: list[str] = names

        selected = load_selected_preset_name()
        if selected and selected in names:
            self._ctx_combo.setCurrentIndex(names.index(selected) + 1)
        else:
            self._ctx_combo.setCurrentIndex(0)
            if selected:
                # Stored selection is stale — clear so send-path and UI agree.
                save_selected_preset_name('')
        self._ctx_combo.blockSignals(False)

    def done(self, result: int) -> None:
        """Persist dialog size on close (accept / reject / X button)."""
        save_dialog_geometry('send_comments', self.width(), self.height())
        super().done(result)

    def _persist_filter_mode(self, _checked: bool) -> None:
        save_send_comments_prefs(self.selected_filter(), self.selected_mode())

    def _persist_ctx(self, _idx: int) -> None:
        text = self._ctx_combo.currentText()
        save_selected_preset_name('' if text == _PR_CONTEXT_NONE else text)

    def selected_filter(self) -> str:
        """Return ``'all'`` or ``'leap'``."""
        return 'leap' if self._filter_leap.isChecked() else 'all'

    def selected_mode(self) -> str:
        """Return ``'each'`` or ``'combined'``."""
        return 'combined' if self._mode_combined.isChecked() else 'each'

    @staticmethod
    def choose(
        parent: Optional[QWidget], *, auto_fetch_leap: bool,
    ) -> tuple[str, str, bool]:
        """Show the dialog and return ``(filter, mode, accepted)``."""
        dlg = SendCommentsDialog(parent, auto_fetch_leap=auto_fetch_leap)
        accepted = dlg.exec_() == QDialog.Accepted
        return dlg.selected_filter(), dlg.selected_mode(), accepted
