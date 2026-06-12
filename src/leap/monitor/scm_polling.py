"""Background workers for Leap Monitor."""

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from typing import Any, Callable, Optional

from PyQt5.QtWidgets import QWidget
from PyQt5.QtCore import QThread, pyqtSignal

from leap.utils.constants import SCM_MAX_CONCURRENT_POLLS
from leap.monitor.pr_tracking.base import (
    ConnectionTestResult, PRState, PRStatus, SCMProvider, UserNotification,
)
from leap.monitor.pr_tracking.config import load_auto_fetch_leap_preset
from leap.monitor.pr_tracking.leap_command import format_leap_message
from leap.monitor.pr_tracking.git_utils import (
    get_git_remote_info, refine_scm_type,
)
from leap.monitor.leap_sender import send_to_leap_session
from leap.monitor.session_manager import get_active_sessions
from leap.monitor.cursor_gui_scan import scan_open_cursor_agents
from leap.monitor.sleep_guard import have_internet

# Maximum time to wait for all poll futures to complete
_POLL_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


class SCMOneShotWorker(QThread):
    """Background worker for a single PR status check (non-blocking Track PR)."""

    result_ready = pyqtSignal(str, object)  # (tag, PRStatus)
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
            status = self._provider.get_pr_status(self._project_path, self._branch)
            self.result_ready.emit(self._tag, status)
        except Exception:
            logger.debug("One-shot PR check failed for %s", self._tag, exc_info=True)
            self.error.emit(self._tag, 'Failed to query SCM provider.')


class SCMPollerWorker(QThread):
    """Background worker that polls SCM providers for PR statuses.

    Also handles /leap commands entirely in the background — matching sessions,
    sending to Leap, and acknowledging on the SCM provider.
    """

    results_ready = pyqtSignal(dict)
    notifications_ready = pyqtSignal(list)  # list[UserNotification]
    notification_auth_error = pyqtSignal(str)  # scm_type with 403/auth failure
    leap_ack_failed = pyqtSignal()  # /leap ack post failed (likely token scope issue)
    leap_send_failed = pyqtSignal(str)  # tag — couldn't deliver /leap to the Leap session
    leap_send_recovered = pyqtSignal(str)  # tag — delivery succeeded (clear dedup)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._providers: dict[str, SCMProvider] = {}
        self._sessions: list[dict[str, Any]] = []
        self._auto_fetch_leap: bool = False
        self._notif_scm_types: set[str] = set()

    def configure(
        self,
        providers: dict[str, SCMProvider],
        sessions: list[dict[str, Any]],
        auto_fetch_leap: bool = False,
        notif_scm_types: Optional[set[str]] = None,
    ) -> None:
        """Configure the poller with available providers and sessions to poll.

        Args:
            providers: Dict mapping SCMType value ("gitlab", "github",
                "bitbucket") to provider instance.
            sessions: List of session dicts to poll.
            auto_fetch_leap: Whether to scan and handle /leap commands.
            notif_scm_types: Set of SCM type strings to fetch notifications for.
                Only providers in this set will be polled for notifications.
        """
        self._providers = dict(providers)
        self._sessions = list(sessions)
        self._auto_fetch_leap = auto_fetch_leap
        self._notif_scm_types = notif_scm_types or set()

    def run(self) -> None:
        if not self._providers:
            return

        results: dict[str, PRStatus] = {}
        all_leap_commands: list[tuple[Any, SCMProvider]] = []
        all_notifications: list[UserNotification] = []

        # Poll sessions in parallel — each session's API calls are independent.
        with ThreadPoolExecutor(max_workers=SCM_MAX_CONCURRENT_POLLS) as pool:
            # Submit session poll futures
            session_futures = {
                pool.submit(self._poll_session, session): ('session', session['tag'])
                for session in self._sessions
            }

            # Submit notification futures alongside session futures
            notif_futures: dict = {}
            if self._notif_scm_types:
                for scm_type, provider in self._providers.items():
                    if scm_type in self._notif_scm_types and provider.supports_notifications():
                        notif_futures[pool.submit(provider.get_user_notifications)] = (
                            'notif', scm_type
                        )

            all_futures = {**session_futures, **notif_futures}
            try:
                for future in as_completed(all_futures, timeout=_POLL_TIMEOUT_SECONDS):
                    kind, key = all_futures[future]
                    try:
                        if kind == 'session':
                            status, leap_commands = future.result()
                            results[key] = status
                            all_leap_commands.extend(leap_commands)
                        elif kind == 'notif':
                            notifs = future.result()
                            all_notifications.extend(notifs)
                    except Exception as exc:
                        if kind == 'session':
                            logger.debug("Error polling session %s", key, exc_info=True)
                            results[key] = PRStatus(state=PRState.NO_PR)
                        else:
                            logger.debug("Error fetching notifications for %s", key,
                                         exc_info=True)
                            # Detect 403/auth errors (PyGithub .status, python-gitlab .response_code)
                            status_code = getattr(exc, 'status', None) or getattr(exc, 'response_code', None)
                            if status_code == 403:
                                self.notification_auth_error.emit(key)
            except TimeoutError:
                logger.debug("SCM poll timed out after %ds, returning partial results",
                               _POLL_TIMEOUT_SECONDS)

        self.results_ready.emit(results)
        if self._notif_scm_types:
            self.notifications_ready.emit(all_notifications)

        # Handle /leap commands in this background thread
        if all_leap_commands:
            self._handle_leap_commands(all_leap_commands)

    def _poll_session(
        self, session: dict[str, Any]
    ) -> tuple[PRStatus, list[tuple[Any, SCMProvider]]]:
        """Poll a single session for PR status and /leap commands."""
        # Resolve SCM project path, branch, and provider.
        # PR-pinned rows have remote_project_path/scm_type stored directly;
        # active sessions resolve from the local git remote.
        remote_project = session.get('remote_project_path')
        scm_type_str = session.get('scm_type')
        # Prefer the pinned PR branch over the live branch so polling
        # keeps tracking the correct PR even when the user switches
        # branches locally.
        branch = session.get('pr_branch') or session.get('branch')
        # PR-pinned rows added via the +button know the IID up front.
        # Providers that need it (GitHub, for fork-PR support) use it to
        # bypass branch-based listing.  Auto-tracked rows pass None.
        pr_iid = session.get('pr_iid')

        if remote_project and scm_type_str and branch and branch != 'N/A':
            # Use pinned PR data directly
            scm_project_path = remote_project
            scm_branch = branch
            provider = self._providers.get(scm_type_str)
        else:
            # Resolve from local git remote
            project_path = session.get('project_path')
            if not project_path:
                logger.debug("Poll skip: no project_path for tag %s", session.get('tag'))
                return PRStatus(state=PRState.NO_PR), []

            remote_info = get_git_remote_info(project_path)
            if not remote_info:
                logger.debug("Poll skip: no remote info for tag %s (path=%s)",
                             session.get('tag'), project_path)
                return PRStatus(state=PRState.NO_PR), []

            scm_project_path = remote_info.project_path
            scm_branch = remote_info.branch
            scm_type = refine_scm_type(remote_info.host_url, remote_info.scm_type)
            provider = self._providers.get(scm_type.value)

        if not provider:
            logger.debug("Poll skip: no provider for tag %s", session.get('tag'))
            return PRStatus(state=PRState.NO_PR), []

        logger.debug("Polling PR for tag %s: project=%s branch=%s iid=%s",
                      session.get('tag'), scm_project_path, scm_branch, pr_iid)
        try:
            status = provider.get_pr_status(scm_project_path, scm_branch, pr_iid=pr_iid)
        except Exception:
            logger.debug("Error polling PR for tag %s", session['tag'], exc_info=True)
            status = PRStatus(state=PRState.NO_PR)

        leap_commands: list[tuple[Any, SCMProvider]] = []
        # ``_pr_only`` rows (Cursor editor Agent tabs) get PR *status* but
        # never participate in /leap handling - they have no Leap session
        # to receive a message, and scanning here would also duplicate the
        # scan a real session for the same project already does.
        if self._auto_fetch_leap and not session.get('_pr_only'):
            try:
                raw_commands = provider.scan_leap_commands(
                    scm_project_path, scm_branch, pr_iid=pr_iid
                )
                leap_commands = [(cmd, provider) for cmd in raw_commands]
            except Exception:
                logger.debug("Error scanning /leap for tag %s", session['tag'], exc_info=True)

        return status, leap_commands

    def _handle_leap_commands(self, commands: list[tuple[Any, SCMProvider]]) -> None:
        """Process /leap commands entirely in the background thread.

        Auto-fetched /leap comments use their own preset (selected via the
        combobox next to the "Auto '/leap' fetch" checkbox), which is
        independent of the preset used by manual sends in
        SendCommentsDialog. Resolved once per poll cycle.
        """
        auto_preset = load_auto_fetch_leap_preset()
        for cmd, provider in commands:
            try:
                # Match sessions by SCM project path
                matching_tags: list[str] = []
                for session in self._sessions:
                    # Never deliver /leap to a PR-only row (Cursor GUI tab) -
                    # it isn't a Leap session.
                    if session.get('_pr_only'):
                        continue
                    # Check pinned remote_project_path first
                    rpp = session.get('remote_project_path')
                    if rpp and rpp == cmd.project_path:
                        matching_tags.append(session['tag'])
                        continue
                    # Fall back to resolving from local git remote
                    sp = session.get('project_path')
                    if not sp:
                        continue
                    ri = get_git_remote_info(sp)
                    if ri and ri.project_path == cmd.project_path:
                        matching_tags.append(session['tag'])

                if matching_tags:
                    tag = matching_tags[0]
                    message = format_leap_message(cmd)
                    sent = send_to_leap_session(tag, message, preset=auto_preset)
                    if not sent:
                        # Don't ack — next poll re-attempts delivery so the
                        # /leap isn't silently consumed.  Surface a popup so
                        # the user knows something went wrong (otherwise
                        # the failure is logged at debug only).
                        logger.debug("Failed to send /leap message to session '%s' "
                                     "- skipping ack so next poll retries", tag)
                        self.leap_send_failed.emit(tag)
                        continue
                    logger.debug("/leap from PR !%s sent to session '%s'",
                                 cmd.pr_iid, tag)
                    # Tell the main thread to clear any stale "warned"
                    # dedup entry for this tag — if the session previously
                    # failed and now recovered, the next failure should
                    # show a fresh popup instead of being silenced.
                    self.leap_send_recovered.emit(tag)
                    # Acknowledge to prevent re-processing now that the
                    # session has the message.
                    acked = provider.acknowledge_leap_command(
                        cmd.project_path, cmd.pr_iid, cmd.discussion_id
                    )
                    if not acked:
                        logger.debug("Failed to acknowledge /leap on PR !%s", cmd.pr_iid)
                        self.leap_ack_failed.emit()
                        return  # Stop processing — ack will fail for all
                else:
                    provider.report_no_session(
                        cmd.project_path, cmd.pr_iid, cmd.discussion_id
                    )
                    logger.debug("No session match for /leap from PR !%s (%s)",
                                cmd.pr_iid, cmd.project_path)
            except Exception:
                logger.debug("Error handling /leap command for PR !%s",
                             cmd.pr_iid, exc_info=True)


class CollectThreadsWorker(QThread):
    """Phase 1: Resolve provider, collect unresponded threads, find matching sessions."""

    collected = pyqtSignal(list, list)  # (commands, matching_tags)
    error = pyqtSignal(str)  # error_message

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._project_path: str = ''
        self._scm_providers: dict[str, SCMProvider] = {}
        self._sessions: list[dict[str, Any]] = []
        self._target_tag: Optional[str] = None
        self._pr_iid: Optional[int] = None
        self.provider: Optional[SCMProvider] = None  # set during run()

    def configure(
        self,
        project_path: str,
        scm_providers: dict[str, SCMProvider],
        sessions: list[dict[str, Any]],
        leap_only: bool = False,
        target_tag: Optional[str] = None,
        pr_iid: Optional[int] = None,
    ) -> None:
        """Configure the worker.

        Args:
            project_path: Filesystem path to the project.
            scm_providers: Dict mapping SCMType value to provider instance.
            sessions: List of session dicts (need 'project_path' and 'tag' keys).
            leap_only: If True, collect only threads with unacknowledged /leap commands.
            target_tag: If set, skip session matching and use this tag directly.
            pr_iid: Optional PR number — passed through to provider methods so
                GitHub can bypass branch listing (required for fork PRs).
        """
        self._project_path = project_path
        self._scm_providers = dict(scm_providers)
        self._sessions = list(sessions)
        self._leap_only = leap_only
        self._target_tag = target_tag
        self._pr_iid = pr_iid

    def run(self) -> None:
        try:
            # Resolve remote info and provider
            remote_info = get_git_remote_info(self._project_path)
            if not remote_info:
                self.collected.emit([], [])
                return

            scm_type = refine_scm_type(remote_info.host_url, remote_info.scm_type)

            self.provider = self._scm_providers.get(scm_type.value)
            if not self.provider:
                self.collected.emit([], [])
                return

            # Collect threads (heavy HTTP calls)
            if self._leap_only:
                commands = self.provider.scan_leap_commands(
                    remote_info.project_path, remote_info.branch,
                    pr_iid=self._pr_iid,
                )
            else:
                commands = self.provider.collect_unresponded_threads(
                    remote_info.project_path, remote_info.branch,
                    pr_iid=self._pr_iid,
                )

            if self._target_tag:
                matching_tags = [self._target_tag]
            else:
                # Find matching sessions by project path (subprocess per session)
                matching_tags: list[str] = []
                for session in self._sessions:
                    sp = session.get('project_path')
                    if not sp:
                        continue
                    ri = get_git_remote_info(sp)
                    if ri and ri.project_path == remote_info.project_path:
                        matching_tags.append(session['tag'])

            self.collected.emit(commands, matching_tags)
        except Exception:
            logger.exception("Error in CollectThreadsWorker")
            self.error.emit("Failed to collect comments.")


class _BaseSendWorker(QThread):
    """Base class for Phase 2 workers that send commands to Leap and ack on SCM."""

    finished = pyqtSignal(int, str)  # (sent_count, matched_tag)
    error = pyqtSignal(str)  # error_message
    ack_failed = pyqtSignal()  # /leap ack post failed
    send_partial_failed = pyqtSignal(int, int, str)  # (sent, failed, matched_tag)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._provider: Optional[SCMProvider] = None
        self._commands: list[Any] = []
        self._matched_tag: str = ''

    def configure(
        self,
        provider: SCMProvider,
        commands: list[Any],
        matched_tag: str,
    ) -> None:
        self._provider = provider
        self._commands = list(commands)
        self._matched_tag = matched_tag

    def _ack_commands(self, commands: list[Any]) -> bool:
        """Acknowledge commands on the SCM provider. Returns True if all succeeded."""
        ok = True
        for cmd in commands:
            if not self._provider.acknowledge_leap_command(
                cmd.project_path, cmd.pr_iid, cmd.discussion_id
            ):
                ok = False
        return ok


class SendThreadsWorker(_BaseSendWorker):
    """Phase 2: Send pre-collected commands to Leap one-by-one and acknowledge on SCM."""

    def run(self) -> None:
        if not self._provider:
            return
        try:
            sent_count = 0
            failed_count = 0
            ack_ok = True
            for cmd in self._commands:
                message = format_leap_message(cmd)
                sent = send_to_leap_session(self._matched_tag, message)
                if sent:
                    if not self._provider.acknowledge_leap_command(
                        cmd.project_path, cmd.pr_iid, cmd.discussion_id
                    ):
                        ack_ok = False
                    sent_count += 1
                else:
                    # Don't ack — next attempt will re-detect this thread as
                    # unresponded.  Surface the partial failure so the user
                    # knows the success count doesn't tell the whole story.
                    failed_count += 1
                    logger.debug("Failed to send thread to session '%s'", self._matched_tag)

            # Emit the appropriate completion signal so the receiver shows
            # one popup, not two:
            #   - all good           → finished(sent_count, tag)
            #   - partial failure    → send_partial_failed(sent, failed, tag)
            # (The receiver for finished still handles the all-zero case
            # too; that path doesn't fire send_partial_failed.)
            if failed_count > 0:
                self.send_partial_failed.emit(
                    sent_count, failed_count, self._matched_tag,
                )
            else:
                self.finished.emit(sent_count, self._matched_tag)
            if not ack_ok:
                self.ack_failed.emit()
        except Exception:
            logger.exception("Error in SendThreadsWorker")
            self.error.emit("Failed to send comments.")


class SendThreadsCombinedWorker(_BaseSendWorker):
    """Send all collected threads as a single concatenated message to Leap."""

    def run(self) -> None:
        if not self._provider:
            return
        try:
            # Format all threads and concatenate
            parts: list[str] = []
            for i, cmd in enumerate(self._commands):
                if i > 0:
                    parts.append("\n---\n")
                parts.append(format_leap_message(cmd))

            combined = "".join(parts)
            sent = send_to_leap_session(self._matched_tag, combined)
            if sent:
                ack_ok = self._ack_commands(self._commands)
                self.finished.emit(len(self._commands), self._matched_tag)
                if not ack_ok:
                    self.ack_failed.emit()
            else:
                logger.debug(
                    "Failed to send combined threads to session '%s'",
                    self._matched_tag,
                )
                self.error.emit("Failed to send combined message.")
        except Exception:
            logger.exception("Error in SendThreadsCombinedWorker")
            self.error.emit("Failed to send combined message.")


class SessionRefreshWorker(QThread):
    """Background worker for refreshing active sessions (avoids blocking on socket I/O).

    Emits two lists: the real Leap sessions, and (when ``scan_cursor_gui``
    is enabled) read-only rows for Cursor editor Agent tabs scanned from
    disk.  The two are kept separate so Cursor-GUI rows never enter the
    monitor's ``self.sessions`` / pinned-session machinery - they are a
    pure display overlay added at render time.
    """

    sessions_ready = pyqtSignal(list, list)  # (leap_sessions, cursor_gui_rows)

    def __init__(self, parent: Optional[QWidget] = None,
                 scan_cursor_gui: bool = False) -> None:
        super().__init__(parent)
        self._scan_cursor_gui = scan_cursor_gui

    def run(self) -> None:
        try:
            sessions = get_active_sessions()
        except Exception:
            # A transient leap-session fetch failure must NOT take the
            # Cursor scan down with it: emitting ([], []) here would make
            # _on_sessions_refreshed see zero live Cursor tags and prune
            # every cursor-gui PR the user opted into (cursor tracking is
            # in-memory only, so there's no auto-reconnect).  Fall through
            # with an empty leap list but still scan Cursor below.
            logger.debug("Error refreshing sessions", exc_info=True)
            sessions = []
        cursor_rows: list = []
        if self._scan_cursor_gui:
            try:
                cursor_rows = scan_open_cursor_agents()
            except Exception:
                logger.debug("Error scanning Cursor agent tabs", exc_info=True)
                cursor_rows = []
        self.sessions_ready.emit(sessions, cursor_rows)


class ConnectivityProbeWorker(QThread):
    """Background worker that probes internet connectivity off the UI thread.

    ``have_internet()`` can block for a couple of seconds on a hard
    outage, which would stutter the 1s refresh loop if run inline, so the
    sleep-guard evaluator kicks this instead and reads the result via a
    signal.
    """

    result_ready = pyqtSignal(bool)  # True iff a TCP handshake succeeded

    def run(self) -> None:
        try:
            ok = have_internet()
        except Exception:
            # Fail open: a bug in the probe must never sleep the Mac and
            # pause running sessions.  Treat an errored probe as online.
            logger.debug("Error probing connectivity", exc_info=True)
            ok = True
        self.result_ready.emit(ok)


class TestConnectionWorker(QThread):
    """Background worker for testing SCM connection (avoids blocking setup dialog)."""

    result_ready = pyqtSignal(object)  # ConnectionTestResult

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._test_func: Optional[Callable] = None
        self._url: str = ''
        self._token: str = ''

    def configure(
        self,
        test_func: Callable[[str, str], ConnectionTestResult],
        url: str,
        token: str,
    ) -> None:
        self._test_func = test_func
        self._url = url
        self._token = token

    def run(self) -> None:
        if not self._test_func:
            return
        try:
            result = self._test_func(self._url, self._token)
            self.result_ready.emit(result)
        except Exception as e:
            logger.debug("Error testing connection", exc_info=True)
            self.result_ready.emit(ConnectionTestResult(
                success=False, username=str(e), warnings=[],
            ))


class BackgroundCallWorker(QThread):
    """Generic worker that runs a callable in the background."""

    def __init__(self, func: Callable, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._func = func

    def run(self) -> None:
        try:
            self._func()
        except Exception:
            logger.debug("Error in BackgroundCallWorker", exc_info=True)
