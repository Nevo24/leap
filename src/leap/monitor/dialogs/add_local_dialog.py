"""Dialog for adding a session from a local path."""

from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QRadioButton, QVBoxLayout,
)

from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.pr_tracking.config import load_dialog_geometry, save_dialog_geometry


class AddLocalDialog(ZoomMixin, QDialog):
    """Simple dialog to select a local directory and choose clone vs open mode."""

    _DEFAULT_SIZE = (500, 200)

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Add from Local Path')
        self.resize(*self._DEFAULT_SIZE)
        saved = load_dialog_geometry('add_local')
        if saved:
            self.resize(saved[0], saved[1])

        layout = QVBoxLayout()
        self.setLayout(layout)

        # Path input row
        path_layout = QHBoxLayout()
        path_layout.addWidget(QLabel('Path:'))
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText('/path/to/project')
        path_layout.addWidget(self._path_edit)
        browse_btn = QPushButton('Browse...')
        browse_btn.setToolTip('Pick a directory with the file chooser')
        browse_btn.clicked.connect(self._browse)
        path_layout.addWidget(browse_btn)
        layout.addLayout(path_layout)

        # Mode radio buttons
        self._clone_radio = QRadioButton('Clone to repos dir (clone from remote)')
        self._clone_radio.setToolTip(
            'Read the git remote of the chosen directory and clone a fresh '
            'copy into the repos directory configured in Settings. Keeps '
            'your original workspace untouched.'
        )
        self._open_radio = QRadioButton('Open directly (use this directory as-is)')
        self._open_radio.setToolTip(
            'Run the session directly inside the chosen directory — no '
            'clone. Changes will apply to your existing workspace.'
        )
        self._clone_radio.setChecked(True)
        layout.addWidget(self._clone_radio)
        layout.addWidget(self._open_radio)

        # OK / Cancel
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        self._init_zoom('add_local_font_size')

    def _browse(self) -> None:
        """Open a directory chooser."""
        path = QFileDialog.getExistingDirectory(self, 'Select Project Directory')
        if path:
            self._path_edit.setText(path)

    def selected_path(self) -> str:
        """Return the entered path."""
        return self._path_edit.text().strip()

    def done(self, result: int) -> None:
        """Save dialog size on close."""
        save_dialog_geometry('add_local', self.width(), self.height())
        super().done(result)

    def is_clone_mode(self) -> bool:
        """Return True if the user chose clone mode."""
        return self._clone_radio.isChecked()
