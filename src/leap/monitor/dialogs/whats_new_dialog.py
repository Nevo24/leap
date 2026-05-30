"""'What's new' dialog — list commits in HEAD..origin/main (read-only)."""

import html
import json
import logging
import os
import re
import subprocess
import time
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

# Marker written by leap-update.sh before its `git pull` and removed by
# `.update-after-pull` on success. While present, the dialog shows the
# commits being installed (range starts at the marker's pre_pull_sha)
# instead of HEAD..origin/main — otherwise the dialog would be empty
# once the pull advances HEAD past origin/main.
_MARKER_REL_PATH = os.path.join('.storage', 'update_in_progress')
_MARKER_STALE_SECONDS = 30 * 60
_SHA_RE = re.compile(r'^[0-9a-f]{7,40}$')


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

        # Banner shown only while a `leap --update` is in progress.
        # Stays hidden in the normal HEAD..origin/main case.
        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setContentsMargins(12, 8, 12, 0)
        self._banner.setVisible(False)
        layout.addWidget(self._banner)

        self._list = QListWidget()
        self._list.setSpacing(6)
        self._list.setViewportMargins(0, 12, 0, 0)
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

    def _compute_range(self) -> tuple[str, Optional[str]]:
        """Pick the git log range and an optional banner.

        Default: ``HEAD..origin/main`` (no banner).
        When a ``leap --update`` is in progress, the marker file lets us
        switch to ``<pre_pull_sha>..origin/main`` so the user keeps
        seeing the commits being installed even after the pull has
        advanced HEAD past origin/main. Stale-fallback (>30 min) and
        any parse failure silently revert to the default — the dialog
        must never break because of a malformed marker.
        """
        default = ('HEAD..origin/main', None)
        marker_path = os.path.join(self._repo_path, _MARKER_REL_PATH)
        try:
            with open(marker_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return default
        # Defensive: json.load can return any JSON type. ``data.get(...)``
        # would raise AttributeError on a list/scalar root, which our
        # except clause above doesn't catch.
        if not isinstance(data, dict):
            return default
        started_at = data.get('started_at')
        # bool is a subclass of int — reject it explicitly so a stray
        # ``"started_at": true`` doesn't read as the epoch second 1.
        if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
            return default
        elapsed = time.time() - float(started_at)
        # Negative elapsed = started_at in the future (clock skew / corruption).
        # Don't trust it; fall back to default so we don't show the banner
        # with a nonsense timestamp. Boundary is ``>=`` to match the
        # worker's ``elapsed < _MARKER_STALE_SECONDS`` check — at exactly
        # 30 min, both treat the marker as stale.
        if elapsed < 0 or elapsed >= _MARKER_STALE_SECONDS:
            return default
        sha = data.get('pre_pull_sha')
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            return default
        banner = 'Update in progress - showing commits being installed.'
        return (f'{sha}..origin/main', banner)

    def _load_commits(self) -> None:
        """Run ``git log <range>`` and populate the list (newest first)."""
        range_spec, banner_text = self._compute_range()
        if banner_text:
            t = current_theme()
            self._banner.setText(
                f'<span style="color: {t.accent_orange}; font-style: italic;">'
                f'{html.escape(banner_text)}</span>'
            )
            self._banner.setVisible(True)
        _sep = '\x1e'
        try:
            result = subprocess.run(
                [
                    'git', 'log',
                    range_spec,
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
            f"You're up to date - no new commits on origin/main."
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
