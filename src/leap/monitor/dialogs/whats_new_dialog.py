"""'What's new' dialog — list commits in HEAD..origin/main (read-only)."""

import html
import logging
import subprocess
from typing import Optional

from PyQt5.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)
from PyQt5.QtCore import QSize, Qt

from leap.monitor.dialogs.git_changes_dialog import _CommitItemWidget
from leap.monitor.dialogs.zoom_mixin import ZoomMixin
from leap.monitor.pr_tracking.config import load_dialog_geometry, save_dialog_geometry
from leap.monitor.themes import current_theme

logger = logging.getLogger(__name__)


class WhatsNewDialog(ZoomMixin, QDialog):
    """Read-only list of commits the user would pull in on next update."""

    _DEFAULT_SIZE = (780, 500)

    def __init__(self, repo_path: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("What's new")
        self.resize(*self._DEFAULT_SIZE)
        saved = load_dialog_geometry('whats_new')
        if saved:
            self.resize(saved[0], saved[1])
        self._repo_path = repo_path

        layout = QVBoxLayout(self)

        self._list = QListWidget()
        self._list.setSpacing(6)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._list.setResizeMode(QListWidget.Fixed)
        self._list.setSelectionMode(QListWidget.NoSelection)
        t = current_theme()
        self._list.setStyleSheet(
            f'QListWidget {{ background: {t.window_bg}; border: none; }}'
            f'QListWidget::item {{ border: none; padding: 2px; }}'
        )
        layout.addWidget(self._list)

        bottom = QHBoxLayout()
        bottom.addStretch()
        close_btn = QPushButton('Close')
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)

        self._load_commits()
        self._init_zoom(
            pref_key='whats_new_font_size',
            content_pref_key='whats_new_text_font_size',
            content_widgets=[self._list],
        )

    def _apply_zoom_content_font_size(self) -> None:  # type: ignore[override]
        super()._apply_zoom_content_font_size()
        for i in range(self._list.count()):
            it = self._list.item(i)
            w = self._list.itemWidget(it)
            if w is not None:
                it.setSizeHint(w.sizeHint())

    def _load_commits(self) -> None:
        """Run `git log HEAD..origin/main` and populate the list (newest first)."""
        _sep = '\x1e'
        try:
            result = subprocess.run(
                [
                    'git', 'log',
                    'HEAD..origin/main',
                    f'--format={_sep}%h%x00%H%x00%an%x00%ae%x00%ad%x00%ar%x00%s%x00%D%x00%b',
                    '--date=format:%a %b %d %H:%M:%S %Y %z',
                ],
                cwd=self._repo_path,
                capture_output=True, text=True,
                encoding='utf-8', errors='replace',
                timeout=10,
            )
            if result.returncode != 0:
                self._show_error((result.stderr or 'git log failed').strip())
                return

            chunks = result.stdout.split(_sep)
            any_added = False
            for chunk in chunks:
                chunk = chunk.strip()
                if not chunk:
                    continue
                parts = chunk.split('\x00')
                if len(parts) < 7:
                    continue
                sha, full_sha, author_name, author_email, date_abs, date_rel, subject = parts[:7]
                refs = parts[7] if len(parts) > 7 else ''
                body = parts[8].strip() if len(parts) > 8 else ''
                if body:
                    t = current_theme()
                    body_html = html.escape(body).replace('\n', '<br>')
                    subject = (
                        f'{html.escape(subject)}<br><br>'
                        f'<span style="font-weight: normal; color: {t.text_secondary};">'
                        f'{body_html}</span>'
                    )
                widget = _CommitItemWidget(
                    sha, full_sha, subject, '', '',
                    date_abs, date_rel, refs, [],
                )
                item = QListWidgetItem(self._list)
                item.setSizeHint(widget.sizeHint() + QSize(0, 6))
                self._list.setItemWidget(item, widget)
                any_added = True

            if not any_added:
                self._show_empty()
        except Exception as exc:
            logger.debug("Failed to load whats-new commits", exc_info=True)
            self._show_error(str(exc))

    def _show_error(self, message: str) -> None:
        t = current_theme()
        label = QLabel(
            f'<span style="color: {t.accent_red};">Failed to load commits</span>'
            f'<br><span style="color: {t.text_secondary};">{message}</span>'
        )
        label.setWordWrap(True)
        label.setContentsMargins(12, 12, 12, 12)
        item = QListWidgetItem(self._list)
        item.setFlags(Qt.NoItemFlags)
        item.setSizeHint(label.sizeHint() + QSize(24, 24))
        self._list.setItemWidget(item, label)

    def _show_empty(self) -> None:
        t = current_theme()
        label = QLabel(
            f'<span style="color: {t.text_secondary};">'
            f"You're up to date — no new commits on origin/main."
            f'</span>'
        )
        label.setWordWrap(True)
        label.setContentsMargins(12, 12, 12, 12)
        item = QListWidgetItem(self._list)
        item.setFlags(Qt.NoItemFlags)
        item.setSizeHint(label.sizeHint() + QSize(24, 24))
        self._list.setItemWidget(item, label)

    def done(self, result: int) -> None:
        save_dialog_geometry('whats_new', self.width(), self.height())
        super().done(result)
