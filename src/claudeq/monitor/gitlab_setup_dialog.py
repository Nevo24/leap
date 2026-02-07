"""GitLab connection setup dialog for ClaudeQ Monitor."""

import webbrowser

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QMessageBox, QSpinBox,
)
from PyQt5.QtCore import Qt

from claudeq.monitor.mr_tracking.config import load_gitlab_config, save_gitlab_config


class GitLabSetupDialog(QDialog):
    """Dialog for configuring GitLab connection."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle('Connect GitLab')
        self.setMinimumWidth(450)
        self._verified_username: str | None = None
        self._init_ui()
        self._load_existing()

    def _init_ui(self) -> None:
        layout = QVBoxLayout()
        self.setLayout(layout)

        # GitLab URL
        layout.addWidget(QLabel('GitLab URL:'))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText('https://gitlab.com')
        layout.addWidget(self.url_input)

        # Token
        layout.addWidget(QLabel('Personal Access Token (api scope):'))
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.Password)
        self.token_input.setPlaceholderText('glpat-...')
        layout.addWidget(self.token_input)

        # Poll interval
        poll_layout = QHBoxLayout()
        poll_layout.addWidget(QLabel('Poll interval (seconds):'))
        self.poll_input = QSpinBox()
        self.poll_input.setRange(5, 300)
        self.poll_input.setValue(30)
        poll_layout.addWidget(self.poll_input)
        poll_note = QLabel('(min: 5s)')
        poll_note.setStyleSheet('color: grey; font-size: 11px;')
        poll_layout.addWidget(poll_note)
        poll_layout.addStretch()
        layout.addLayout(poll_layout)

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

    def _load_existing(self) -> None:
        config = load_gitlab_config()
        if not config:
            return
        self.url_input.setText(config.get('gitlab_url', ''))
        self.token_input.setText(config.get('private_token', ''))
        self.poll_input.setValue(config.get('poll_interval', 30))
        # If config exists, username is already verified - enable save
        if config.get('username'):
            self._verified_username = config['username']
            self.save_btn.setEnabled(True)
            self.status_label.setText(f'Connected as: {self._verified_username}')
            self.status_label.setStyleSheet('color: green;')

    def _open_token_page(self) -> None:
        url = self.url_input.text().strip() or 'https://gitlab.com'
        webbrowser.open(f'{url}/-/user_settings/personal_access_tokens')

    def _test_connection(self) -> None:
        url = self.url_input.text().strip() or 'https://gitlab.com'
        token = self.token_input.text().strip()
        if not token:
            self.status_label.setText('Please enter a token.')
            self.status_label.setStyleSheet('color: red;')
            return

        self.status_label.setText('Testing...')
        self.status_label.setStyleSheet('color: grey;')
        # Force UI update
        from PyQt5.QtWidgets import QApplication
        QApplication.processEvents()

        try:
            import gitlab
            gl = gitlab.Gitlab(url, private_token=token)
            gl.auth()
            username = gl.user.username
            self._verified_username = username
            self.status_label.setText(f'Connected as: {username}')
            self.status_label.setStyleSheet('color: green;')
            self.save_btn.setEnabled(True)
        except Exception as e:
            self._verified_username = None
            self.status_label.setText(f'Failed: {e}')
            self.status_label.setStyleSheet('color: red;')
            self.save_btn.setEnabled(False)

    def _save(self) -> None:
        url = self.url_input.text().strip() or 'https://gitlab.com'
        token = self.token_input.text().strip()
        if not self._verified_username:
            QMessageBox.warning(self, 'Error', 'Test the connection first.')
            return

        config = {
            'gitlab_url': url,
            'private_token': token,
            'username': self._verified_username,
            'poll_interval': self.poll_input.value(),
        }
        save_gitlab_config(config)
        self.accept()
