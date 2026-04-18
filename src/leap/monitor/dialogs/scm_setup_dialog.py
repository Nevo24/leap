"""Base SCM connection setup dialog for Leap Monitor."""

import os
from abc import abstractmethod
from typing import Any, Optional

from PyQt5.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QMessageBox, QRadioButton, QSpinBox, QWidget,
)

from leap.utils.constants import SCM_POLL_INTERVAL
from leap.monitor.pr_tracking.base import ConnectionTestResult
from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.pr_tracking.config import load_dialog_geometry, save_dialog_geometry
from leap.monitor.scm_polling import TestConnectionWorker
from leap.monitor.themes import current_theme


class SCMSetupDialog(ZoomMixin, QDialog):
    """Base dialog for configuring SCM provider connections.

    Three distinct actions:

    - **Save**: writes URL, token, token_mode, poll interval and
      notifications to disk. Does not touch ``username`` — i.e. Save
      never changes the connected/disconnected state. Its purpose is
      "remember what I typed across dialog opens".
    - **Connect / Disconnect** (same button, label depends on state):
      - Connect (when disconnected): validates the token with the
        current form values; on success writes everything including
        ``username``. Next open, this button shows Disconnect.
      - Disconnect (when connected): clears ``username`` only. Keeps
        URL, token and other fields on disk so reconnecting is a
        one-click affair.
    - **Cancel**: closes the dialog without writing anything.

    Subclasses must implement:
        - _window_title() -> str
        - _url_label() -> str
        - _url_placeholder() -> str
        - _url_default() -> str
        - _token_label() -> str
        - _token_placeholder() -> str
        - _do_test_connection(url, token) -> ConnectionTestResult
        - _load_config() -> Optional[dict]
        - _save_config(config) -> None
        - _config_url_key() -> str
        - _config_token_key() -> str
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self._window_title())
        self.setMinimumWidth(450)
        self._geometry_key = self._window_title().lower().replace(' ', '_')
        saved = load_dialog_geometry(self._geometry_key)
        if saved:
            self.resize(saved[0], saved[1])
        self._verified_username: Optional[str] = None
        self._test_worker: Optional[TestConnectionWorker] = None
        self._pending_values: dict[str, Any] = {}
        self._was_connected = bool(
            (self._load_config() or {}).get('username')
        )
        # Outcome flags read by the parent to pick a status-bar message.
        # At most one of these is True after accept(); both False means
        # plain Save (fields persisted, no connection state change).
        self.disconnected = False
        self.connected = False
        self._init_ui()
        self._load_existing()
        self._init_zoom(f'{self._geometry_key}_font_size')

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
    def _do_test_connection(self, url: str, token: str) -> ConnectionTestResult:
        """Test the connection and return a ConnectionTestResult."""

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

    def _provider_display_name(self) -> str:
        """Human-readable provider name for confirmation text (e.g. 'GitLab')."""
        title = self._window_title()
        return title.replace('Connect', '').strip() or title

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

        # Token mode: direct value vs environment variable
        mode_layout = QHBoxLayout()
        self._token_direct_radio = QRadioButton('Token')
        self._token_direct_radio.setToolTip('Paste the token value directly (stored in .storage/)')
        self._token_envvar_radio = QRadioButton('Environment variable')
        self._token_envvar_radio.setToolTip(
            'Enter the name of an environment variable that holds the token\n'
            '(e.g. GITLAB_TOKEN). The token is resolved at runtime and never stored.'
        )
        self._token_mode_group = QButtonGroup(self)
        self._token_mode_group.addButton(self._token_direct_radio)
        self._token_mode_group.addButton(self._token_envvar_radio)
        self._token_direct_radio.setChecked(True)
        self._token_direct_radio.toggled.connect(self._on_token_mode_changed)
        mode_layout.addWidget(self._token_direct_radio)
        mode_layout.addWidget(self._token_envvar_radio)
        mode_layout.addStretch()
        layout.addLayout(mode_layout)

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
        poll_note.setStyleSheet(f'color: {current_theme().text_muted};')
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

        # Buttons: [Connect|Disconnect]  [stretch]  [Save] [Cancel]
        # Left slot is a single button whose label and style flip based on
        # the saved ``username`` state. Save sits with Cancel on the right
        # so the user reads it as "commit my edits" alongside "discard".
        btn_layout = QHBoxLayout()

        self.toggle_btn = QPushButton()
        self.toggle_btn.clicked.connect(self._on_toggle_clicked)
        btn_layout.addWidget(self.toggle_btn)

        btn_layout.addStretch()

        self.save_btn = QPushButton('Save')
        self.save_btn.setToolTip(
            'Save the current field values to disk. Does not test the '
            'connection and does not change the connected state.'
        )
        self.save_btn.clicked.connect(self._save_fields)
        btn_layout.addWidget(self.save_btn)

        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addLayout(btn_layout)

        self._apply_toggle_btn_state()

    @staticmethod
    def _disconnect_btn_style() -> str:
        """Red-bordered style for the Disconnect state. Mirrors the
        'connected' button style geometry (padding, min-height) for
        consistent vertical alignment on macOS Qt."""
        t = current_theme()
        btn_bg = t.button_bg or t.window_bg
        return (
            f'QPushButton {{ color: {t.accent_red};'
            f' background-color: {btn_bg};'
            f' border: 1px solid {t.accent_red};'
            f' padding: 5px 16px;'
            f' min-height: 18px; }}'
            f'QPushButton:hover {{ background-color: {t.accent_red};'
            f' color: {t.window_bg};'
            f' border-color: {t.accent_red}; }}'
            f'QPushButton:disabled {{ color: {t.text_muted};'
            f' border-color: {t.text_muted}; }}'
        )

    def _apply_toggle_btn_state(self) -> None:
        """Update the left button's label/style/tooltip for the current state."""
        name = self._provider_display_name()
        if self._was_connected:
            self.toggle_btn.setText('Disconnect')
            self.toggle_btn.setStyleSheet(self._disconnect_btn_style())
            self.toggle_btn.setToolTip(
                f'Clear the saved {name} login. The token and other '
                'fields stay on disk.'
            )
        else:
            self.toggle_btn.setText('Connect')
            self.toggle_btn.setStyleSheet('')
            self.toggle_btn.setToolTip(
                f'Validate the token with {name} and save everything on '
                'success. Use Save on its own if you just want to remember '
                'the field values without logging in.'
            )

    def _on_toggle_clicked(self) -> None:
        """Route clicks to Connect or Disconnect based on current state."""
        if self._was_connected:
            self._disconnect()
        else:
            self._connect()

    def _disconnect(self) -> None:
        """Clear the saved username. Keeps token/URL/prefs intact."""
        name = self._provider_display_name()
        reply = QMessageBox.question(
            self,
            f'Disconnect {name}?',
            f'Stop using your {name} credentials?\n\n'
            f'The token, URL and other settings stay saved — only the '
            f'connection is cleared. Click Connect later to log in again '
            f'with the same values, or edit the fields first.',
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        config = self._load_config() or {}
        config.pop('username', None)
        self._save_config(config)
        self.disconnected = True
        self.accept()

    def _on_token_mode_changed(self, direct_checked: bool) -> None:
        """Toggle token input between direct and env var mode."""
        self.token_input.clear()
        if direct_checked:
            self.token_input.setEchoMode(QLineEdit.Password)
            self.token_input.setPlaceholderText(self._token_placeholder())
        else:
            self.token_input.setEchoMode(QLineEdit.Normal)
            self.token_input.setPlaceholderText('e.g. GITLAB_TOKEN')

    @staticmethod
    def _is_valid_env_var_name(name: str) -> bool:
        """Check if a string looks like a valid environment variable name."""
        return bool(name) and all(
            c.isalnum() or c == '_' for c in name
        ) and not name[0].isdigit()

    def _validate_token_mode(self) -> bool:
        """Validate token input matches the selected mode.

        Returns True if valid, False if a warning was shown.
        """
        value = self.token_input.text().strip()
        if not value:
            return True
        if self._token_envvar_radio.isChecked() and not self._is_valid_env_var_name(value):
            QMessageBox.warning(
                self, 'Invalid environment variable',
                f'"{value}" is not a valid environment variable name.\n\n'
                'If you want to use a token directly, select the "Token" '
                'radio button instead.',
            )
            return False
        return True

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
        if config.get('token_mode') == 'env_var':
            self._token_envvar_radio.setChecked(True)
            # Explicitly set Normal echo mode — radio signal may not fire
            # during construction, and plaintext makes it clear no token is stored
            self.token_input.setEchoMode(QLineEdit.Normal)
            self.token_input.setPlaceholderText('e.g. GITLAB_TOKEN')
        self.token_input.setText(config.get(self._config_token_key(), ''))
        self.poll_input.setValue(config.get('poll_interval', SCM_POLL_INTERVAL))
        self.notif_check.setChecked(config.get('enable_notifications', False))
        if config.get('username'):
            self._verified_username = config['username']
            self.status_label.setText(f'Connected as: {self._verified_username}')
            self.status_label.setStyleSheet(f'color: {current_theme().accent_green};')

    def _form_values(self) -> Optional[dict[str, Any]]:
        """Collect validated form values into a partial config dict.

        Returns ``None`` (and shows an error on the status label) if the
        token field is empty or token-mode validation fails. The returned
        dict contains every field *except* ``username`` — callers decide
        whether to add it (Connect) or preserve the on-disk value (Save).
        """
        raw_token = self.token_input.text().strip()
        if not raw_token:
            if self._token_envvar_radio.isChecked():
                msg = 'Please enter an environment variable name.'
            else:
                msg = 'Please enter a token.'
            self.status_label.setText(msg)
            self.status_label.setStyleSheet(f'color: {current_theme().accent_red};')
            return None
        if not self._validate_token_mode():
            return None
        url = self.url_input.text().strip() or self._url_default()
        token_mode = 'env_var' if self._token_envvar_radio.isChecked() else 'direct'
        return {
            self._config_url_key(): url,
            self._config_token_key(): raw_token,
            'token_mode': token_mode,
            'poll_interval': self.poll_input.value(),
            'enable_notifications': self.notif_check.isChecked(),
        }

    def _save_fields(self) -> None:
        """Persist the current form values without touching connection state.

        Preserves the existing ``username`` on disk so Save never flips
        the connected/disconnected state. Closes the dialog on success.
        """
        values = self._form_values()
        if values is None:
            return
        existing = self._load_config() or {}
        if existing.get('username'):
            values['username'] = existing['username']
        self._save_config(values)
        self.accept()

    def _connect(self) -> None:
        """Validate the token in the background, then save + log in on success."""
        values = self._form_values()
        if values is None:
            return

        # Resolve env var if in env var mode — we need the actual token
        # value for the test call, but the form value we store is the
        # env-var name (for env_var mode) or the token itself (direct).
        raw_token = values[self._config_token_key()]
        if values['token_mode'] == 'env_var':
            token = os.environ.get(raw_token)
            if not token:
                self.status_label.setText(
                    f'Environment variable ${raw_token} is not set.')
                self.status_label.setStyleSheet(f'color: {current_theme().accent_red};')
                return
        else:
            token = raw_token

        self.status_label.setText('Connecting...')
        self.status_label.setStyleSheet(f'color: {current_theme().text_muted};')
        self.toggle_btn.setEnabled(False)
        self.save_btn.setEnabled(False)

        self._pending_values = values
        self._test_worker = TestConnectionWorker(self)
        self._test_worker.configure(
            self._do_test_connection, values[self._config_url_key()], token)
        self._test_worker.result_ready.connect(self._on_connect_result)
        self._test_worker.finished.connect(self._test_worker.deleteLater)
        self._test_worker.start()

    def _on_connect_result(self, result: ConnectionTestResult) -> None:
        """Handle background connection test result from Connect."""
        self.toggle_btn.setEnabled(True)
        self.save_btn.setEnabled(True)

        if not result.success:
            self._verified_username = None
            self.status_label.setText(f'Failed: {result.username}')
            self.status_label.setStyleSheet(f'color: {current_theme().accent_red};')
            return

        self._verified_username = result.username
        values = self._pending_values
        values['username'] = self._verified_username
        self._save_config(values)
        self.connected = True

        # Surface scope/permission warnings before closing — the dialog is
        # about to disappear, so an inline warning would be invisible.
        if result.warnings:
            QMessageBox.warning(
                self,
                'Connected with warnings',
                f'Connected as: {result.username}\n\n' + '\n'.join(result.warnings),
            )
        self.accept()

    def done(self, result: int) -> None:
        """Save dialog size on close."""
        save_dialog_geometry(self._geometry_key, self.width(), self.height())
        super().done(result)
