"""Git changes dialog — local diff, commit diff, diff vs main."""

import logging
import subprocess
from typing import Callable, Optional

from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)
from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QTextDocument

from claudeq.monitor.mr_tracking.config import load_dialog_geometry, save_dialog_geometry
from claudeq.monitor.mr_tracking.git_utils import detect_default_branch

logger = logging.getLogger(__name__)

_COMMIT_ITEM_STYLE = """
QWidget#commit_item {
    border: 1px solid #555;
    border-radius: 6px;
    padding: 8px;
    margin: 2px;
    background: #2d2d2d;
}
QWidget#commit_item:hover {
    background: #3a3a3a;
    border-color: #88f;
}
"""


class _CommitItemWidget(QWidget):
    """Custom widget displaying a single commit's details (glog-style)."""

    def __init__(
        self,
        sha: str,
        full_sha: str,
        subject: str,
        author_name: str,
        author_email: str,
        date_abs: str,
        date_rel: str,
        refs: str,
        files: list,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName('commit_item')
        self.setStyleSheet(_COMMIT_ITEM_STYLE)
        self.setCursor(Qt.PointingHandCursor)

        mono = 'Menlo, Monaco, Courier'
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(1)

        # Line 1: "commit <full_sha>" + (refs)
        commit_html = (
            f'<span style="color: #e5c07b; font-family: {mono};">commit {full_sha}</span>'
        )
        if refs:
            ref_parts = []
            for r in refs.split(', '):
                r = r.strip()
                if '->' in r:
                    parts = r.split('->')
                    ref_parts.append(
                        f'<span style="color: #56b6c2;">{parts[0].strip()}</span>'
                        f' \u2192 '
                        f'<span style="color: #98c379;">{parts[1].strip()}</span>'
                    )
                elif r.startswith('origin/'):
                    ref_parts.append(f'<span style="color: #e06c75;">{r}</span>')
                elif r.startswith('tag:'):
                    ref_parts.append(f'<span style="color: #c678dd;">{r}</span>')
                else:
                    ref_parts.append(f'<span style="color: #98c379;">{r}</span>')
            commit_html += f' <span style="font-size: 11px;">({", ".join(ref_parts)})</span>'
        commit_label = QLabel(commit_html)
        commit_label.setWordWrap(False)
        layout.addWidget(commit_label)

        # Line 2: Author
        author_label = QLabel(
            f'<span style="color: #aaa; font-family: {mono}; font-size: 12px;">'
            f'Author: {author_name} &lt;{author_email}&gt;</span>'
        )
        layout.addWidget(author_label)

        # Line 3: Date (absolute + relative)
        date_label = QLabel(
            f'<span style="color: #aaa; font-family: {mono}; font-size: 12px;">'
            f'Date:   {date_abs}'
            f'  <span style="color: #98c379;">({date_rel})</span></span>'
        )
        layout.addWidget(date_label)

        # Line 4: Subject (indented, bold)
        subj_label = QLabel(subject)
        subj_label.setStyleSheet('color: #ffffff; font-weight: bold; padding-left: 16px;')
        subj_label.setWordWrap(True)
        layout.addWidget(subj_label)

        # Line 5+: Changed files
        if files:
            files_html = '<br>'.join(
                f'<span style="color: #61afef; font-family: {mono}; '
                f'font-size: 11px;">{f}</span>'
                for f in files
            )
            files_label = QLabel(files_html)
            files_label.setWordWrap(False)
            files_label.setContentsMargins(16, 2, 0, 0)
            layout.addWidget(files_label)

        # Compute true width from richtext labels for proper horizontal scroll
        margins = layout.contentsMargins()
        pad = margins.left() + margins.right() + 20  # extra for frame border/padding
        max_w = 0
        for i in range(layout.count()):
            w = layout.itemAt(i).widget()
            if isinstance(w, QLabel) and not w.wordWrap():
                doc = QTextDocument()
                doc.setHtml(w.text())
                doc.setDefaultFont(w.font())
                max_w = max(max_w, int(doc.idealWidth()))
        self._ideal_width = max_w + pad

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(max(hint.width(), self._ideal_width), hint.height())


class CommitListDialog(QDialog):
    """Dialog showing recent commits for selection."""

    _PAGE_SIZE = 50

    def __init__(
        self,
        project_path: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle('Select Commit')
        self.resize(780, 500)
        saved = load_dialog_geometry('commit_list')
        if saved:
            self.resize(saved[0], saved[1])
        self._project_path = project_path
        self._selected_commit: Optional[str] = None
        self._commits: list[str] = []  # SHA list parallel to list items
        self._loaded: int = 0  # number of commits loaded so far
        self._has_more: bool = True
        self._load_more_item: Optional[QListWidgetItem] = None

        layout = QVBoxLayout(self)

        self._list = QListWidget()
        self._list.setSpacing(6)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._list.setResizeMode(QListWidget.Fixed)
        self._list.setStyleSheet(
            'QListWidget { background: #1e1e1e; border: none; }'
            'QListWidget::item { border: none; padding: 2px; }'
            'QListWidget::item:selected { background: rgba(80, 120, 255, 40); }'
        )
        self._list.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._list)

        # Bottom row: manual entry + OK/Cancel
        bottom = QHBoxLayout()

        manual_btn = QPushButton('Enter commit SHA manually')
        manual_btn.setToolTip('Type a commit hash instead of selecting from the list')
        manual_btn.clicked.connect(self._enter_manual)
        bottom.addWidget(manual_btn)

        bottom.addStretch()

        cancel_btn = QPushButton('Cancel')
        cancel_btn.clicked.connect(self.reject)
        bottom.addWidget(cancel_btn)

        ok_btn = QPushButton('OK')
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._on_accept)
        bottom.addWidget(ok_btn)

        layout.addLayout(bottom)

        self._load_page()

    def _load_page(self) -> None:
        """Load the next page of commits."""
        # Remove the existing "Load more" item before appending new commits
        if self._load_more_item is not None:
            row = self._list.row(self._load_more_item)
            if row >= 0:
                self._list.takeItem(row)
            self._load_more_item = None

        count = 0
        try:
            _sep = '\x1e'  # ASCII record separator
            result = subprocess.run(
                [
                    'git', 'log',
                    f'--format={_sep}%h%x00%H%x00%an%x00%ae%x00%ad%x00%ar%x00%s%x00%D',
                    '--date=format:%a %b %d %H:%M:%S %Y %z',
                    '--name-only',
                    f'--skip={self._loaded}',
                    f'-{self._PAGE_SIZE}',
                ],
                cwd=self._project_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                self._has_more = False
                return

            chunks = result.stdout.split(_sep)
            for chunk in chunks:
                chunk = chunk.strip()
                if not chunk:
                    continue
                lines = chunk.split('\n')
                header = lines[0]
                parts = header.split('\x00')
                if len(parts) < 7:
                    continue
                sha = parts[0]
                full_sha = parts[1]
                author_name = parts[2]
                author_email = parts[3]
                date_abs = parts[4]
                date_rel = parts[5]
                subject = parts[6]
                refs = parts[7] if len(parts) > 7 else ''
                files = [f for f in lines[1:] if f.strip()]

                widget = _CommitItemWidget(
                    sha, full_sha, subject, author_name,
                    author_email, date_abs, date_rel, refs, files,
                )
                item = QListWidgetItem(self._list)
                item.setSizeHint(widget.sizeHint() + QSize(0, 6))
                self._list.setItemWidget(item, widget)
                self._commits.append(sha)
                count += 1
        except Exception:
            logger.debug("Failed to load git log", exc_info=True)

        self._loaded += count
        self._has_more = count >= self._PAGE_SIZE
        if self._has_more:
            self._add_load_more_item()

    def _add_load_more_item(self) -> None:
        """Append a 'Load more commits...' button at the bottom of the list."""
        btn = QPushButton(f'Load more commits... ({self._loaded} loaded)')
        btn.setStyleSheet(
            'QPushButton { color: #5B9BD5; border: 1px solid #444; '
            'border-radius: 6px; padding: 10px; background: #252525; }'
            'QPushButton:hover { background: #333; border-color: #88f; }'
        )
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self._load_page)
        self._load_more_item = QListWidgetItem(self._list)
        self._load_more_item.setFlags(Qt.NoItemFlags)  # not selectable
        self._load_more_item.setSizeHint(QSize(0, btn.sizeHint().height() + 12))
        self._list.setItemWidget(self._load_more_item, btn)

    def _on_double_click(self, item: QListWidgetItem) -> None:
        """Handle double-click on a commit (ignore the Load More item)."""
        if item is not self._load_more_item:
            self._on_accept()

    def _on_accept(self) -> None:
        """Set selected commit and accept."""
        row = self._list.currentRow()
        if 0 <= row < len(self._commits):
            self._selected_commit = self._commits[row]
            self.accept()

    def _enter_manual(self) -> None:
        """Prompt user to enter a commit SHA manually."""
        text, ok = QInputDialog.getText(
            self, 'Enter Commit', 'Commit SHA or ref:',
        )
        if ok and text.strip():
            self._selected_commit = text.strip()
            self.accept()

    def done(self, result: int) -> None:
        """Save dialog size on close."""
        save_dialog_geometry('commit_list', self.width(), self.height())
        super().done(result)

    def selected_commit(self) -> Optional[str]:
        """Return the selected commit SHA."""
        return self._selected_commit


class GitChangesDialog(QDialog):
    """Dialog with three options for viewing git changes.

    The ``on_run_git`` callback receives ``(diff_args, project_path)`` where
    ``diff_args`` is a list of git-diff ref arguments (e.g. ``[]`` for local,
    ``['origin/main']``, or ``['sha~1', 'sha']``).  The caller is responsible
    for building the full difftool command and checking for empty diffs.
    """

    def __init__(
        self,
        project_path: str,
        on_run_git: Callable[[list, str], None],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle('See Git Changes')
        self.resize(350, 150)
        saved = load_dialog_geometry('git_changes')
        if saved:
            self.resize(saved[0], saved[1])
        self._project_path = project_path
        self._on_run_git = on_run_git

        layout = QVBoxLayout(self)

        local_btn = QPushButton('See local changes')
        local_btn.setAutoDefault(False)
        local_btn.setToolTip('Show uncommitted changes using difftool')
        local_btn.clicked.connect(self._see_local_changes)
        layout.addWidget(local_btn)

        main_btn = QPushButton('Compare to origin/main (or master) branch')
        main_btn.setAutoDefault(False)
        main_btn.setToolTip('Show diff between HEAD and the default remote branch')
        main_btn.clicked.connect(self._compare_to_main)
        layout.addWidget(main_btn)

        commits_btn = QPushButton('See changes compared to previous commits')
        commits_btn.setAutoDefault(False)
        commits_btn.setToolTip('Pick a commit and show its diff using difftool')
        commits_btn.clicked.connect(self._see_commit_changes)
        layout.addWidget(commits_btn)

        close_btn = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn.rejected.connect(self.reject)
        layout.addWidget(close_btn)

    def _see_local_changes(self) -> None:
        """Open difftool for uncommitted changes."""
        self._on_run_git([], self._project_path)
        self.accept()

    def _see_commit_changes(self) -> None:
        """Open commit list, then difftool for selected commit."""
        dialog = CommitListDialog(self._project_path, parent=self)
        if dialog.exec_():
            sha = dialog.selected_commit()
            if sha:
                self._on_run_git([f'{sha}~1', sha], self._project_path)
                self.accept()

    def _compare_to_main(self) -> None:
        """Open difftool comparing HEAD to origin/main."""
        main_branch = self._detect_main_branch()
        self._on_run_git([f'origin/{main_branch}'], self._project_path)
        self.accept()

    def done(self, result: int) -> None:
        """Save dialog size on close."""
        save_dialog_geometry('git_changes', self.width(), self.height())
        super().done(result)

    def _detect_main_branch(self) -> str:
        """Detect the default branch name (main or master)."""
        return detect_default_branch(self._project_path)
