"""Base SCM connection setup dialog for ClaudeQ Monitor."""

from abc import abstractmethod
from typing import Any, Optional

from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QMessageBox, QSpinBox, QWidget,
)

from claudeq.utils.constants import SCM_POLL_INTERVAL
from claudeq.monitor.scm_polling import TestConnectionWorker


class SCMSetupDialog(QDialog):
    """Base dialog for configuring SCM provider connections.

    Subclasses must implement:
        - _window_title() -> str
        - _url_label() -> str
        - _url_placeholder() -> str
        - _token_label() -> str
        - _token_placeholder() -> str
        - _do_test_connection(url, token) -> tuple[bool, str]
        - _load_config() -> Optional[dict]
        - _save_config(config) -> None
        - _config_url_key() -> str
        - _config_token_key() -> str
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self._window_title())
        self.setMinimumWidth(450)
        self._verified_username: Optional[str] = None
        self._test_worker: Optional[TestConnectionWorker] = None
        self._init_ui()
        self._load_existing()

    @abstractmethod
    def _window_title(self) -> str:
        """Return the dialog window title."""

    @abstractmethod
    def _url_label(self) -> str:
        """Return the label for the URL input field."""

    @abstractmethod
    def _url_placeholder(self) -> str:
        """Return the placeholder text for the URL input."""

    @abstractmethod
    def _url_default(self) -> str:
        """Return the default URL when input is empty."""

    @abstractmethod
    def _token_label(self) -> str:
        """Return the label for the token input field."""

    @abstractmethod
    def _token_placeholder(self) -> str:
        """Return the placeholder text for the token input."""

    @abstractmethod
    def _do_test_connection(self, url: str, token: str) -> tuple[bool, str]:
        """Test the connection and return (success, username_or_error)."""

    @abstractmethod
    def _load_config(self) -> Optional[dict[str, Any]]:
        """Load the existing config for this provider."""

    @abstractmethod
    def _save_config(self, config: dict[str, Any]) -> None:
        """Save the config for this provider."""

    @abstractmethod
    def _config_url_key(self) -> str:
        """Return the config dict key for the URL field."""

    @abstractmethod
    def _config_token_key(self) -> str:
        """Return the config dict key for the token field."""

    def _notif_tooltip(self) -> str:
        """Return tooltip text for the notification tracking checkbox."""
        return 'Poll for personal notifications each cycle'

    def _init_ui(self) -> None:
        layout = QVBoxLayout()
        self.setLayout(layout)

        # URL — hidden by default behind "Self-hosted" toggle
        self._url_check = QCheckBox('Self-hosted (custom URL)')
        self._url_check.toggled.connect(self._toggle_url_visible)
        layout.addWidget(self._url_check)

        self._url_label_widget = QLabel(self._url_label())
        self._url_label_widget.setVisible(False)
        layout.addWidget(self._url_label_widget)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(self._url_placeholder())
        self.url_input.setVisible(False)
        layout.addWidget(self.url_input)

        # Token
        layout.addWidget(QLabel(self._token_label()))
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.Password)
        self.token_input.setPlaceholderText(self._token_placeholder())
        layout.addWidget(self.token_input)

        # Poll interval
        poll_layout = QHBoxLayout()
        poll_layout.addWidget(QLabel('Poll interval (seconds):'))
        self.poll_input = QSpinBox()
        self.poll_input.setRange(5, 300)
        self.poll_input.setValue(SCM_POLL_INTERVAL)
        poll_layout.addWidget(self.poll_input)
        poll_note = QLabel('(min: 5s)')
        poll_note.setStyleSheet('color: grey; font-size: 11px;')
        poll_layout.addWidget(poll_note)
        poll_layout.addStretch()
        layout.addLayout(poll_layout)

        # Notification tracking checkbox
        self.notif_check = QCheckBox('Enable notification tracking')
        self.notif_check.setToolTip(self._notif_tooltip())
        layout.addWidget(self.notif_check)

        # Status label
        self.status_label = QLabel('')
        layout.addWidget(self.status_label)

        # Buttons
        btn_layout = QHBoxLayout()

        self.test_btn = QPushButton('Test Connection')
        self.test_btn.clicked.connect(self._test_connection)
        btn_layout.addWidget(self.test_btn)

        btn_layout.addStretch()

        self.save_btn = QPushButton('Save')
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        btn_layout.addWidget(self.save_btn)

        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

    def _toggle_url_visible(self, checked: bool) -> None:
        self._url_label_widget.setVisible(checked)
        self.url_input.setVisible(checked)
        if not checked:
            self.url_input.clear()

    def _load_existing(self) -> None:
        config = self._load_config()
        if not config:
            return
        saved_url = config.get(self._config_url_key(), '')
        default_url = self._url_default()
        # Auto-expand URL field if a non-default URL is saved
        if saved_url and saved_url != default_url:
            self._url_check.setChecked(True)
            self.url_input.setText(saved_url)
        self.token_input.setText(config.get(self._config_token_key(), ''))
        self.poll_input.setValue(config.get('poll_interval', SCM_POLL_INTERVAL))
        self.notif_check.setChecked(config.get('enable_notifications', False))
        if config.get('username'):
            self._verified_username = config['username']
            self.save_btn.setEnabled(True)
            self.status_label.setText(f'Connected as: {self._verified_username}')
            self.status_label.setStyleSheet('color: green;')

    def _test_connection(self) -> None:
        url = self.url_input.text().strip() or self._url_default()
        token = self.token_input.text().strip()
        if not token:
            self.status_label.setText('Please enter a token.')
            self.status_label.setStyleSheet('color: red;')
            return

        self.status_label.setText('Testing...')
        self.status_label.setStyleSheet('color: grey;')
        self.test_btn.setEnabled(False)

        self._test_worker = TestConnectionWorker(self)
        self._test_worker.configure(self._do_test_connection, url, token)
        self._test_worker.result_ready.connect(self._on_test_result)
        self._test_worker.finished.connect(self._test_worker.deleteLater)
        self._test_worker.start()

    def _on_test_result(self, success: bool, result: str) -> None:
        """Handle background connection test result."""
        self.test_btn.setEnabled(True)
        if success:
            self._verified_username = result
            self.status_label.setText(f'Connected as: {result}')
            self.status_label.setStyleSheet('color: green;')
            self.save_btn.setEnabled(True)
        else:
            self._verified_username = None
            self.status_label.setText(f'Failed: {result}')
            self.status_label.setStyleSheet('color: red;')
            self.save_btn.setEnabled(False)

    def _save(self) -> None:
        url = self.url_input.text().strip() or self._url_default()
        token = self.token_input.text().strip()
        if not self._verified_username:
            QMessageBox.warning(self, 'Error', 'Test the connection first.')
            return

        config = {
            self._config_url_key(): url,
            self._config_token_key(): token,
            'username': self._verified_username,
            'poll_interval': self.poll_input.value(),
            'enable_notifications': self.notif_check.isChecked(),
        }
        self._save_config(config)
        self.accept()
