"""Background SCM polling worker for ClaudeQ Monitor."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from PyQt5.QtWidgets import QWidget
from PyQt5.QtCore import QThread, pyqtSignal

from claudeq.utils.constants import GITLAB_MAX_CONCURRENT_POLLS
from claudeq.monitor.mr_tracking.base import MRState, MRStatus, SCMProvider
from claudeq.monitor.mr_tracking.git_utils import get_git_remote_info

logger = logging.getLogger(__name__)


class SCMPollerWorker(QThread):
    """Background worker that polls an SCM provider for MR statuses."""

    results_ready = pyqtSignal(dict)
    cq_commands_ready = pyqtSignal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._provider: Optional[SCMProvider] = None
        self._sessions: list[dict[str, Any]] = []

    def configure(self, provider: SCMProvider, sessions: list[dict[str, Any]]) -> None:
        self._provider = provider
        self._sessions = list(sessions)

    def run(self) -> None:
        if not self._provider:
            return

        results: dict[str, MRStatus] = {}
        all_cq_commands: list[Any] = []

        # Poll sessions in parallel — each session's API calls are independent.
        with ThreadPoolExecutor(max_workers=GITLAB_MAX_CONCURRENT_POLLS) as pool:
            futures = {
                pool.submit(self._poll_session, session): session['tag']
                for session in self._sessions
            }
            for future in as_completed(futures):
                tag = futures[future]
                try:
                    status, cq_commands = future.result()
                    results[tag] = status
                    all_cq_commands.extend(cq_commands)
                except Exception:
                    logger.debug("Error polling session %s", tag, exc_info=True)
                    results[tag] = MRStatus(state=MRState.NO_MR)

        self.results_ready.emit(results)
        if all_cq_commands:
            self.cq_commands_ready.emit(all_cq_commands)

    def _poll_session(self, session: dict[str, Any]) -> tuple[MRStatus, list[Any]]:
        """Poll a single session for MR status and /cq commands."""
        project_path = session.get('project_path')
        if not project_path:
            return MRStatus(state=MRState.NO_MR), []

        remote_info = get_git_remote_info(project_path)
        if not remote_info:
            return MRStatus(state=MRState.NO_MR), []

        try:
            status = self._provider.get_mr_status(
                remote_info.project_path, remote_info.branch
            )
        except Exception:
            logger.debug("Error polling MR for tag %s", session['tag'], exc_info=True)
            status = MRStatus(state=MRState.NO_MR)

        cq_commands: list[Any] = []
        try:
            cq_commands = self._provider.scan_cq_commands(
                remote_info.project_path, remote_info.branch
            )
        except Exception:
            logger.debug("Error scanning /cq for tag %s", session['tag'], exc_info=True)

        return status, cq_commands
