"""Background SCM polling worker for ClaudeQ Monitor."""

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from typing import Any, Optional

from PyQt5.QtWidgets import QWidget
from PyQt5.QtCore import QThread, pyqtSignal

from claudeq.utils.constants import SCM_MAX_CONCURRENT_POLLS
from claudeq.monitor.mr_tracking.base import MRState, MRStatus, SCMProvider
from claudeq.monitor.mr_tracking.git_utils import SCMType, get_git_remote_info

# Maximum time to wait for all poll futures to complete
_POLL_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


class SCMOneShotWorker(QThread):
    """Background worker for a single MR status check (non-blocking Track MR)."""

    result_ready = pyqtSignal(str, object)  # (tag, MRStatus)
    error = pyqtSignal(str, str)  # (tag, error_message)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._provider: Optional[SCMProvider] = None
        self._tag: str = ''
        self._project_path: str = ''
        self._branch: str = ''

    def configure(self, provider: SCMProvider, tag: str, project_path: str, branch: str) -> None:
        self._provider = provider
        self._tag = tag
        self._project_path = project_path
        self._branch = branch

    def run(self) -> None:
        if not self._provider:
            return
        try:
            status = self._provider.get_mr_status(self._project_path, self._branch)
            self.result_ready.emit(self._tag, status)
        except Exception:
            logger.debug("One-shot MR check failed for %s", self._tag, exc_info=True)
            self.error.emit(self._tag, 'Failed to query SCM provider.')


class SCMPollerWorker(QThread):
    """Background worker that polls SCM providers for MR statuses."""

    results_ready = pyqtSignal(dict)
    cq_commands_ready = pyqtSignal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._providers: dict[str, SCMProvider] = {}
        self._sessions: list[dict[str, Any]] = []

    def configure(self, providers: dict[str, SCMProvider], sessions: list[dict[str, Any]]) -> None:
        """Configure the poller with available providers and sessions to poll.

        Args:
            providers: Dict mapping SCMType value ("gitlab", "github") to provider instance.
            sessions: List of session dicts to poll.
        """
        self._providers = dict(providers)
        self._sessions = list(sessions)

    def run(self) -> None:
        if not self._providers:
            return

        results: dict[str, MRStatus] = {}
        all_cq_commands: list[Any] = []

        # Poll sessions in parallel — each session's API calls are independent.
        with ThreadPoolExecutor(max_workers=SCM_MAX_CONCURRENT_POLLS) as pool:
            futures = {
                pool.submit(self._poll_session, session): session['tag']
                for session in self._sessions
            }
            try:
                for future in as_completed(futures, timeout=_POLL_TIMEOUT_SECONDS):
                    tag = futures[future]
                    try:
                        status, cq_commands = future.result()
                        results[tag] = status
                        all_cq_commands.extend(cq_commands)
                    except Exception:
                        logger.debug("Error polling session %s", tag, exc_info=True)
                        results[tag] = MRStatus(state=MRState.NO_MR)
            except TimeoutError:
                logger.warning("SCM poll timed out after %ds, returning partial results",
                               _POLL_TIMEOUT_SECONDS)

        self.results_ready.emit(results)
        if all_cq_commands:
            self.cq_commands_ready.emit(all_cq_commands)

    def _poll_session(self, session: dict[str, Any]) -> tuple[MRStatus, list[Any]]:
        """Poll a single session for MR status and /cq commands."""
        project_path = session.get('project_path')
        if not project_path:
            logger.debug("Poll skip: no project_path for tag %s", session.get('tag'))
            return MRStatus(state=MRState.NO_MR), []

        remote_info = get_git_remote_info(project_path)
        if not remote_info:
            logger.debug("Poll skip: no remote info for tag %s (path=%s)",
                         session.get('tag'), project_path)
            return MRStatus(state=MRState.NO_MR), []

        # Select the right provider based on SCM type
        provider = self._providers.get(remote_info.scm_type.value)
        if not provider:
            logger.debug("Poll skip: no provider for scm_type %s (tag %s)",
                         remote_info.scm_type.value, session.get('tag'))
            return MRStatus(state=MRState.NO_MR), []

        logger.debug("Polling MR for tag %s: project=%s branch=%s scm=%s",
                      session.get('tag'), remote_info.project_path, remote_info.branch,
                      remote_info.scm_type.value)
        try:
            status = provider.get_mr_status(
                remote_info.project_path, remote_info.branch
            )
        except Exception:
            logger.debug("Error polling MR for tag %s", session['tag'], exc_info=True)
            status = MRStatus(state=MRState.NO_MR)

        cq_commands: list[Any] = []
        try:
            cq_commands = provider.scan_cq_commands(
                remote_info.project_path, remote_info.branch
            )
        except Exception:
            logger.debug("Error scanning /cq for tag %s", session['tag'], exc_info=True)

        return status, cq_commands
