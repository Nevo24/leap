"""GitLab provider for PR tracking."""

from __future__ import annotations

import base64
import logging
import threading
from typing import Any, Optional

import gitlab

from leap.monitor.pr_tracking.base import (
    ClosedPRInfo,
    PRDetails,
    PRState,
    PRStatus,
    SCMProvider,
    UserNotification,
)
from leap.monitor.pr_tracking.leap_command import CqCommand

logger = logging.getLogger(__name__)

LEAP_BOT_PREFIX = "[Leap bot]"
LEAP_ACK_MESSAGE = f"{LEAP_BOT_PREFIX} on it!"
LEAP_NO_SESSION_MESSAGE = f"{LEAP_BOT_PREFIX} No matching Leap session found for this project."


class GitLabProvider(SCMProvider):
    """GitLab provider with thread-level comment tracking."""

    def __init__(self, gitlab_url: str, private_token: str, username: str,
                 filter_bots: bool = True) -> None:
        self._gl = gitlab.Gitlab(gitlab_url, private_token=private_token, timeout=15)
        self._username = username
        self._filter_bots = filter_bots
        self._project_cache: dict[str, gitlab.v4.objects.Project] = {}
        self._bot_cache: dict[int, bool] = {}  # user_id -> is_bot
        # Caches to avoid phantom state changes on transient API failures
        self._approval_cache: dict[tuple[str, str], tuple[bool, list[str], bool]] = {}
        self._status_cache: dict[tuple[str, str], PRStatus] = {}
        self._emoji_cache: dict[int, bool] = {}  # note_id -> user_reacted
        # Per-thread reliability flag for the unresponded-thread scan: set
        # False when a per-thread sub-lookup (emoji reaction / bot author)
        # transiently fails, so the count is carried forward instead of being
        # flapped upward into a phantom "N unresponded comments" alert.
        # Thread-local because the SCM poll worker and the collect-threads
        # worker call this same provider instance concurrently — a shared bool
        # could let one thread's failure flip the other's mid-scan.
        self._scan_state = threading.local()

    def test_connection(self) -> tuple[bool, str]:
        try:
            self._gl.auth()
            return True, self._gl.user.username
        except Exception as e:
            return False, str(e)

    def get_username(self) -> Optional[str]:
        return self._username

    def _get_project(self, project_path: str) -> Optional[gitlab.v4.objects.Project]:
        if project_path in self._project_cache:
            return self._project_cache[project_path]
        try:
            project = self._gl.projects.get(project_path)
            self._project_cache[project_path] = project
            return project
        except Exception:
            logger.debug("Failed to get project: %s", project_path)
            return None

    def get_pr_details(self, project_path: str, pr_iid: int) -> Optional[PRDetails]:
        project = self._get_project(project_path)
        if not project:
            return None
        try:
            mr = project.mergerequests.get(pr_iid)
            # For fork MRs the source branch lives in the *source* project
            # (the fork), not in the base project we got from project_path.
            # Querying the base project would always 404 and falsely flag
            # the branch as deleted.  Fall back to the base project when
            # the source project is unset (fork deleted) or unreachable
            # (token can't see private fork) — same outcome as before for
            # those cases.
            branch_repo = project
            source_project_id = getattr(mr, 'source_project_id', None)
            if source_project_id and source_project_id != project.id:
                try:
                    branch_repo = self._gl.projects.get(source_project_id)
                except Exception:
                    branch_repo = project
            branch_deleted = False
            try:
                branch_repo.branches.get(mr.source_branch)
            except Exception:
                branch_deleted = True
            return PRDetails(
                source_branch=mr.source_branch,
                pr_title=mr.title,
                pr_url=mr.web_url,
                source_branch_deleted=branch_deleted,
            )
        except Exception:
            logger.debug("Failed to get PR !%s in %s", pr_iid, project_path)
            return None

    @staticmethod
    def _mr_pipeline_failed(mr: Any) -> bool:
        """Whether the MR's head pipeline reports a failed status."""
        hp = getattr(mr, 'head_pipeline', None)
        if isinstance(hp, dict):
            return hp.get('status') == 'failed'
        if hp is not None:
            return getattr(hp, 'status', None) == 'failed'
        return False

    @staticmethod
    def _mr_changes_requested(mr: Any) -> bool:
        """Whether a reviewer requested changes (best-effort).

        Recent GitLab servers expose this as
        ``detailed_merge_status == 'requested_changes'``; older versions don't
        surface per-reviewer review state over REST, so this returns False
        there (the field defaults off and simply isn't shown).
        """
        return getattr(mr, 'detailed_merge_status', None) == 'requested_changes'

    @staticmethod
    def _mr_has_conflicts(mr: Any, default: Optional[bool] = False) -> Optional[bool]:
        """Whether an MR object reports merge conflicts.

        Prefers the canonical ``has_conflicts`` boolean; falls back to the
        historical ``merge_status`` / ``detailed_merge_status`` strings when
        that attribute is absent.  Returns ``default`` when the object
        carries none of those fields (so callers can distinguish "no conflict
        signal present" from a definite False).
        """
        has_conflicts = getattr(mr, 'has_conflicts', None)
        if has_conflicts is not None:
            return bool(has_conflicts)
        merge_status = getattr(mr, 'merge_status', None)
        detailed = getattr(mr, 'detailed_merge_status', None)
        if merge_status or detailed:
            return (merge_status == 'cannot_be_merged'
                    or detailed == 'conflict')
        return default

    def get_pr_status(self, project_path: str, branch: str,
                      pr_iid: Optional[int] = None) -> PRStatus:
        # pr_iid is accepted for SCMProvider symmetry but ignored here:
        # GitLab's mergerequests.list(source_branch=...) on the base project
        # already returns fork-originated MRs, since GitLab merges them
        # into the same MR list as internal MRs.
        del pr_iid
        cache_key = (project_path, branch)
        project = self._get_project(project_path)
        if not project:
            return PRStatus(state=PRState.NO_PR)

        try:
            mrs = project.mergerequests.list(
                state='opened',
                source_branch=branch,
                get_all=False,
            )
        except Exception:
            logger.debug("Failed to list PRs for %s branch %s", project_path, branch)
            # Transient list failure — keep the last known status rather than
            # flapping to NO_PR (which on recovery reads as a brand-new
            # approval and fires a phantom alert).
            cached = self._status_cache.get(cache_key)
            return cached if cached is not None else PRStatus(state=PRState.NO_PR)

        if not mrs:
            # No open MR for this branch (merged / closed / never existed).
            # Drop any cached open status + approval so a later *transient*
            # list failure can't resurrect it — for a merged/closed pinned row
            # a resurrected open status would trigger a spurious "PR is open
            # again" reopen.
            self._status_cache.pop(cache_key, None)
            self._approval_cache.pop(cache_key, None)
            return PRStatus(state=PRState.NO_PR)

        mr = mrs[0]
        pr_iid = mr.iid
        pr_url = mr.web_url
        pr_title = mr.title

        # Draft + merge-conflict state, read from the list response first so
        # we still have correct values if the full-fetch below fails.
        # ``draft`` is the modern field; ``work_in_progress`` is the legacy
        # alias older servers still return — accept either.  ``has_conflicts``
        # is canonical; ``merge_status == 'cannot_be_merged'`` /
        # ``detailed_merge_status == 'conflict'`` are the historical fallbacks.
        draft = bool(getattr(mr, 'draft', False)
                     or getattr(mr, 'work_in_progress', False))
        has_conflicts = self._mr_has_conflicts(mr)
        # Reviewer "request changes" + pipeline state are reliable only on the
        # full MR object, so seed them False and fill in after the fetch below.
        changes_requested = False
        checks_failed = False

        # Fetch the full PR object once (used for approvals + discussions)
        try:
            pr_full = project.mergerequests.get(pr_iid)
        except Exception:
            logger.debug("Failed to fetch full PR !%s", pr_iid, exc_info=True)
            cached = self._status_cache.get(cache_key)
            if cached is not None:
                return cached
            # Approvals were never fetched on this path — mark them unknown so a
            # later successful poll isn't read as a brand-new approval.
            return PRStatus(
                state=PRState.ALL_RESPONDED,
                pr_url=pr_url, pr_title=pr_title, pr_iid=pr_iid,
                draft=draft, has_conflicts=has_conflicts,
                approval_known=False,
            )

        # Prefer the full-fetch values when present (the list response can be
        # stale).  Only override when the full object actually carries the
        # field, so a missing attribute doesn't reset the list-response read.
        if hasattr(pr_full, 'draft') or hasattr(pr_full, 'work_in_progress'):
            draft = bool(getattr(pr_full, 'draft', False)
                         or getattr(pr_full, 'work_in_progress', False))
        full_conflicts = self._mr_has_conflicts(pr_full, default=None)
        if full_conflicts is not None:
            has_conflicts = full_conflicts
        changes_requested = self._mr_changes_requested(pr_full)
        checks_failed = self._mr_pipeline_failed(pr_full)

        # Check approval status
        approval_failed = False
        approval_known = True
        approved = False
        approved_by: list[str] = []
        self_approved = False
        try:
            approvals = pr_full.approvals.get()
            for entry in getattr(approvals, 'approved_by', []):
                name = self._extract_user_name(entry)
                if name:
                    approved_by.append(name)
                uname = self._extract_user_username(entry)
                if uname and uname == self._username:
                    self_approved = True
            # Fallback 1: check PR object's approved_by attribute
            if not approved_by:
                for entry in getattr(pr_full, 'approved_by', []):
                    name = self._extract_user_name(entry)
                    if name and name not in approved_by:
                        approved_by.append(name)
                    uname = self._extract_user_username(entry)
                    if uname and uname == self._username:
                        self_approved = True
            # Fallback 2: check approval_state for approver names
            if not approved_by:
                try:
                    state = pr_full.approval_state.get()
                    for rule in getattr(state, 'rules', []):
                        rule_data = rule if isinstance(rule, dict) else vars(rule)
                        for user in rule_data.get('approved_by', []):
                            name = self._extract_user_name(user)
                            if name and name not in approved_by:
                                approved_by.append(name)
                            uname = self._extract_user_username(user)
                            if uname and uname == self._username:
                                self_approved = True
                except Exception:
                    pass
            # Only mark as approved if someone actually approved — GitLab
            # returns approved=True when zero approvals are required, which
            # is misleading in the UI.
            approved = len(approved_by) > 0
            logger.debug("Approvals for PR !%s: approved=%s approved_by=%s self_approved=%s",
                         pr_iid, approved, approved_by, self_approved)
            self._approval_cache[cache_key] = (approved, list(approved_by), self_approved)
        except Exception:
            logger.debug("Failed to fetch approval status for PR !%s", pr_iid, exc_info=True)
            approval_failed = True
            cached_approval = self._approval_cache.get(cache_key)
            if cached_approval is not None:
                approved, approved_by = cached_approval[0], list(cached_approval[1])
                self_approved = cached_approval[2]
            else:
                # No approval-specific cache yet.  Carry the approval fields
                # from the last full status if we have a known one; otherwise
                # mark approval state as unknown so the notification diff won't
                # read the next successful fetch as a brand-new approval.
                prior = self._status_cache.get(cache_key)
                if prior is not None and prior.approval_known:
                    approved = prior.approved
                    approved_by = list(prior.approved_by or [])
                    self_approved = prior.self_approved
                else:
                    approval_known = False

        # Fetch discussions to count unresponded threads
        try:
            discussions = pr_full.discussions.list(get_all=True)
        except Exception:
            logger.debug("Failed to fetch discussions for PR !%s", pr_iid)
            cached = self._status_cache.get(cache_key)
            if cached is not None:
                # Use cached status but update approval fields if they were fresh
                if not approval_failed:
                    return PRStatus(
                        state=cached.state,
                        unresponded_count=cached.unresponded_count,
                        pr_url=pr_url, pr_title=pr_title, pr_iid=pr_iid,
                        first_unresponded_note_id=cached.first_unresponded_note_id,
                        first_unresponded_url=cached.first_unresponded_url,
                        approved=approved, approved_by=approved_by or None, self_approved=self_approved,
                        approval_known=approval_known,
                        draft=draft, has_conflicts=has_conflicts,
                        changes_requested=changes_requested, checks_failed=checks_failed,
                    )
                return cached
            return PRStatus(
                state=PRState.ALL_RESPONDED,
                pr_url=pr_url, pr_title=pr_title, pr_iid=pr_iid,
                approved=approved, approved_by=approved_by or None, self_approved=self_approved,
                approval_known=approval_known,
                draft=draft, has_conflicts=has_conflicts,
                changes_requested=changes_requested, checks_failed=checks_failed,
            )

        # Recompute unresponded threads from scratch.  Reset the scan-reliable
        # flag first; the per-thread helpers flip it False on a transient
        # sub-lookup failure (see _user_reacted_to_note / _is_bot_author).
        self._scan_state.reliable = True
        unresponded = 0
        first_note_id: Optional[int] = None
        try:
            for discussion in discussions:
                if self._is_unresponded_thread(discussion, project, pr_iid):
                    unresponded += 1
                    if first_note_id is None:
                        notes = discussion.attributes.get('notes', [])
                        if notes:
                            first_note_id = notes[0].get('id')
        except Exception:
            # Malformed discussion data must not escape get_pr_status — that
            # would bubble to the poll worker, which falls back to NO_PR and
            # re-fires a phantom approval on recovery.  Treat it as an
            # unreliable scan so the cached count is carried forward instead.
            logger.debug("Error scanning discussions for PR !%s", pr_iid, exc_info=True)
            self._scan_state.reliable = False

        # An unreliable scan (a thread lookup failed transiently) could have
        # mis-counted in either direction.  Reuse the last known-good count so a
        # blip can't fire a phantom "N unresponded comments" alert.
        if not getattr(self._scan_state, 'reliable', True):
            prior = self._status_cache.get(cache_key)
            if prior is not None:
                unresponded = prior.unresponded_count
                first_note_id = prior.first_unresponded_note_id

        if unresponded > 0:
            first_url = (self.build_first_unresponded_url(pr_url, first_note_id)
                         if first_note_id is not None else None)
            result = PRStatus(
                state=PRState.UNRESPONDED,
                unresponded_count=unresponded,
                pr_url=pr_url, pr_title=pr_title, pr_iid=pr_iid,
                first_unresponded_note_id=first_note_id,
                first_unresponded_url=first_url,
                approved=approved, approved_by=approved_by or None, self_approved=self_approved,
                approval_known=approval_known,
                draft=draft, has_conflicts=has_conflicts,
                changes_requested=changes_requested, checks_failed=checks_failed,
            )
        else:
            result = PRStatus(
                state=PRState.ALL_RESPONDED,
                pr_url=pr_url, pr_title=pr_title, pr_iid=pr_iid,
                approved=approved, approved_by=approved_by or None, self_approved=self_approved,
                approval_known=approval_known,
                draft=draft, has_conflicts=has_conflicts,
                changes_requested=changes_requested, checks_failed=checks_failed,
            )

        self._status_cache[cache_key] = result
        return result

    def find_latest_closed_pr(self, project_path: str,
                              branch: str) -> Optional[ClosedPRInfo]:
        project = self._get_project(project_path)
        if not project:
            return None
        # Prefer merged over closed-without-merge; within each state pick the
        # most recently updated.  GitLab's MR list defaults to
        # order_by=created_at, so request updated_at explicitly to match the
        # GitHub provider's "most recently updated" semantics.
        for state, merged in (('merged', True), ('closed', False)):
            try:
                mrs = project.mergerequests.list(
                    state=state,
                    source_branch=branch,
                    order_by='updated_at',
                    sort='desc',
                    get_all=False,
                    per_page=1,
                )
            except Exception:
                logger.debug("Failed to list %s MRs for %s branch %s",
                             state, project_path, branch)
                continue
            if mrs:
                mr = mrs[0]
                return ClosedPRInfo(
                    pr_iid=mr.iid,
                    pr_title=getattr(mr, 'title', '') or '',
                    pr_url=getattr(mr, 'web_url', '') or '',
                    merged=merged,
                )
        return None

    def _is_unresponded_thread(self, discussion, project, pr_iid: int) -> bool:
        """Check if a discussion thread has unresponded comments from others.

        A thread is "unresponded" if:
        - It has notes from someone other than the user
        - The last non-system note by someone else is not followed by a reply from the user
        - The user hasn't reacted with an emoji on that note
        - The thread is not resolved
        """
        # Notes are dicts in discussion.attributes['notes'], NOT the NoteManager
        notes = discussion.attributes.get('notes', [])
        if not notes:
            return False

        # Filter out system notes (and bot users if enabled)
        human_notes = [
            n for n in notes
            if not n.get('system', False)
            and (not self._filter_bots or not self._is_bot_author(n))
        ]
        if not human_notes:
            return False

        # If resolved, treat as acknowledged.  Check both the discussion-
        # level flag and individual note flags — discussions.list() may
        # omit the discussion-level resolved field.
        if discussion.attributes.get('resolved', False):
            return False
        resolvable_notes = [n for n in notes if n.get('resolvable', False)]
        if resolvable_notes and all(n.get('resolved', False) for n in resolvable_notes):
            return False

        # Check if only the user commented (skip own-only threads)
        other_authors = [
            n for n in human_notes
            if self._note_author(n) != self._username
        ]
        if not other_authors:
            return False

        # Find the last note by someone other than the user
        last_other_idx = -1
        for i, note in enumerate(human_notes):
            if self._note_author(note) != self._username:
                last_other_idx = i

        # Check if user replied after the last other person's note
        # (/leap commands don't count as a real reply)
        for note in human_notes[last_other_idx + 1:]:
            if self._note_author(note) == self._username and note.get('body', '').strip() != '/leap':
                return False

        # Check if user reacted with an emoji on the last note by someone else
        last_other_note = human_notes[last_other_idx]
        if self._user_reacted_to_note(project, pr_iid, last_other_note):
            return False

        return True

    def _user_reacted_to_note(self, project, pr_iid: int, note: dict) -> bool:
        """Check if the user has an emoji reaction on a note."""
        note_id = note.get('id')
        if not note_id:
            return False
        try:
            emojis = self._gl.http_list(
                f'/projects/{project.id}/merge_requests/{pr_iid}/notes/{note_id}/award_emoji',
                as_list=True,
            )
            result = any(
                e.get('user', {}).get('username') == self._username
                for e in emojis
            )
            self._emoji_cache[note_id] = result
            return result
        except Exception:
            # Unknown reaction state for an uncached note: flag the scan as
            # unreliable so the count is carried forward rather than letting a
            # missed emoji-ack flap a thread into the unresponded tally.
            if note_id not in self._emoji_cache:
                self._scan_state.reliable = False
            return self._emoji_cache.get(note_id, False)

    def _is_bot_author(self, note: dict) -> bool:
        """Check if a note's author is a bot, with caching via GitLab user API.

        Only caches the result on a *successful* user-fetch.  Transient
        failures (network blip, rate limit, 5xx) return False without
        poisoning the cache — otherwise a single bad lookup early in the
        session would permanently classify a real bot as human.
        """
        author = note.get('author', {})
        user_id = author.get('id')
        if user_id is None:
            return False

        if user_id in self._bot_cache:
            return self._bot_cache[user_id]

        try:
            user = self._gl.users.get(user_id)
            is_bot = getattr(user, 'bot', False)
        except Exception:
            # Don't cache on failure — retry on next call.  Flag the scan as
            # unreliable so an unknown author can't push the unresponded count
            # up (a real bot momentarily counted as a human commenter).
            self._scan_state.reliable = False
            return False

        self._bot_cache[user_id] = is_bot
        return is_bot

    @staticmethod
    def _extract_user_name(entry: object) -> str:
        """Extract a display name from a GitLab user or approval entry.

        Handles both dict and RESTObject formats, with nested 'user' key or direct.
        """
        # Unwrap nested 'user' key if present
        if isinstance(entry, dict):
            user = entry.get('user', entry)
        else:
            user = getattr(entry, 'user', entry)
        # Extract name or username
        if isinstance(user, dict):
            return user.get('name', '') or user.get('username', '')
        return getattr(user, 'name', '') or getattr(user, 'username', '')

    @staticmethod
    def _extract_user_username(entry: object) -> str:
        """Extract the login username from a GitLab user or approval entry.

        Same unwrap logic as _extract_user_name but returns username (login).
        """
        if isinstance(entry, dict):
            user = entry.get('user', entry)
        else:
            user = getattr(entry, 'user', entry)
        if isinstance(user, dict):
            return user.get('username', '')
        return getattr(user, 'username', '')

    @staticmethod
    def _note_author(note) -> str:
        """Extract username from a note (dict format from discussion attributes)."""
        if isinstance(note, dict):
            return note.get('author', {}).get('username', '')
        # Fallback for object format
        author = getattr(note, 'author', None)
        if isinstance(author, dict):
            return author.get('username', '')
        return getattr(author, 'username', '')

    def scan_leap_commands(self, project_path: str, branch: str,
                           pr_iid: Optional[int] = None) -> list[CqCommand]:
        """Scan open PRs for /leap commands from the configured user."""
        del pr_iid  # see get_pr_status — GitLab listing already covers forks
        project = self._get_project(project_path)
        if not project:
            return []

        try:
            mrs = project.mergerequests.list(
                state='opened',
                source_branch=branch,
                get_all=False,
            )
        except Exception:
            logger.debug("Failed to list PRs for /leap scan: %s branch %s", project_path, branch)
            return []

        commands = []
        for mr in mrs:
            try:
                pr_full = project.mergerequests.get(mr.iid)
                discussions = pr_full.discussions.list(get_all=True)
            except Exception:
                logger.debug("Failed to fetch discussions for PR !%s", mr.iid)
                continue

            for discussion in discussions:
                cmd = self._check_discussion_for_leap(
                    project, project_path, mr, discussion, branch
                )
                if cmd:
                    commands.append(cmd)

        return commands

    def _check_discussion_for_leap(
        self, project, project_path: str, mr, discussion, branch: str
    ) -> Optional[CqCommand]:
        """Check a single discussion for a /leap trigger."""
        # Skip resolved threads — clicking "Resolve thread" should
        # dismiss any pending /leap (matches our GitHub fix #5).
        if discussion.attributes.get('resolved', False):
            return None

        notes = discussion.attributes.get('notes', [])
        if not notes:
            return None

        # Find the last /leap trigger and last bot acknowledgment.
        # An ack only covers /leap commands that appear before it.
        # System notes are excluded so an auto-generated body that
        # happens to contain LEAP_ACK_MESSAGE can't accidentally mark
        # the thread as acked (#8).
        last_leap_index = -1
        last_ack_index = -1
        for i, note in enumerate(notes):
            if note.get('system', False):
                continue
            body = note.get('body', '').strip()
            author = self._note_author(note)
            if body == '/leap' and author == self._username:
                last_leap_index = i
            if LEAP_ACK_MESSAGE in note.get('body', ''):
                last_ack_index = i

        if last_leap_index < 0 or last_leap_index < last_ack_index:
            return None

        # Extract thread notes (excluding system notes)
        thread_notes = []
        for note in notes:
            if note.get('system', False):
                continue
            thread_notes.append({
                'author': self._note_author(note),
                'body': note.get('body', ''),
                'created_at': note.get('created_at', ''),
            })

        # Extract code context from the first note that has a position.
        # System notes (and any other note without position) are skipped
        # so a leading system note doesn't strip the file:line context
        # from the message we send to the CLI.
        file_path = None
        old_line = None
        new_line = None
        code_snippet = None

        first_with_position = next(
            (n for n in notes if n.get('position')), None,
        )
        if first_with_position:
            position = first_with_position['position']
            file_path = position.get('new_path') or position.get('old_path')
            new_line = position.get('new_line')
            old_line = position.get('old_line')

            if file_path:
                code_snippet = self._fetch_code_snippet(
                    project, file_path, branch, new_line or old_line
                )

        return CqCommand(
            project_path=project_path,
            pr_iid=mr.iid,
            pr_title=mr.title,
            pr_url=mr.web_url,
            discussion_id=discussion.id,
            thread_notes=thread_notes,
            file_path=file_path,
            old_line=old_line,
            new_line=new_line,
            code_snippet=code_snippet,
        )

    def _fetch_code_snippet(
        self, project, file_path: str, branch: str, target_line: Optional[int]
    ) -> Optional[str]:
        """Fetch a code snippet around the target line from GitLab."""
        if not target_line:
            return None
        try:
            f = project.files.get(file_path=file_path, ref=branch)
            content = base64.b64decode(f.content).decode('utf-8')
            lines = content.splitlines()

            # Extract ~5 lines around target (2 before, target, 2 after)
            start = max(0, target_line - 3)
            end = min(len(lines), target_line + 2)
            return "\n".join(lines[start:end])
        except Exception:
            logger.debug("Failed to fetch code snippet for %s:%s", file_path, target_line)
            return None

    def acknowledge_leap_command(self, project_path: str, pr_iid: int, discussion_id: str) -> bool:
        """Post '[Leap bot] on it!' reply to the discussion thread."""
        project = self._get_project(project_path)
        if not project:
            return False
        try:
            mr = project.mergerequests.get(pr_iid)
            discussion = mr.discussions.get(discussion_id)
            discussion.notes.create({"body": LEAP_ACK_MESSAGE})
            return True
        except Exception:
            logger.debug("Failed to acknowledge /leap on PR !%s discussion %s",
                         pr_iid, discussion_id, exc_info=True)
            return False

    def collect_unresponded_threads(self, project_path: str, branch: str,
                                    pr_iid: Optional[int] = None) -> list[CqCommand]:
        """Collect all unresponded discussion threads from a PR as CqCommand objects."""
        del pr_iid  # see get_pr_status — GitLab listing already covers forks
        project = self._get_project(project_path)
        if not project:
            return []

        try:
            mrs = project.mergerequests.list(
                state='opened',
                source_branch=branch,
                get_all=False,
            )
        except Exception:
            logger.debug("Failed to list PRs for collect_unresponded: %s branch %s",
                         project_path, branch)
            return []

        if not mrs:
            return []

        mr = mrs[0]
        try:
            pr_full = project.mergerequests.get(mr.iid)
            discussions = pr_full.discussions.list(get_all=True)
        except Exception:
            logger.debug("Failed to fetch discussions for PR !%s", mr.iid)
            return []

        commands = []
        for discussion in discussions:
            if not self._is_unresponded_thread(discussion, project, mr.iid):
                continue

            cmd = self._build_leap_command_from_discussion(
                project, project_path, mr, discussion, branch
            )
            if cmd:
                commands.append(cmd)

        return commands

    def _build_leap_command_from_discussion(
        self, project, project_path: str, mr, discussion, branch: str
    ) -> Optional[CqCommand]:
        """Build a CqCommand from an unresponded discussion thread."""
        notes = discussion.attributes.get('notes', [])
        if not notes:
            return None

        # Extract thread notes (excluding system notes)
        thread_notes = []
        for note in notes:
            if note.get('system', False):
                continue
            thread_notes.append({
                'author': self._note_author(note),
                'body': note.get('body', ''),
                'created_at': note.get('created_at', ''),
            })

        # Extract code context from the first note that has a position.
        # System notes (and any other note without position) are skipped
        # so a leading system note doesn't strip the file:line context
        # from the message we send to the CLI.
        file_path = None
        old_line = None
        new_line = None
        code_snippet = None

        first_with_position = next(
            (n for n in notes if n.get('position')), None,
        )
        if first_with_position:
            position = first_with_position['position']
            file_path = position.get('new_path') or position.get('old_path')
            new_line = position.get('new_line')
            old_line = position.get('old_line')

            if file_path:
                code_snippet = self._fetch_code_snippet(
                    project, file_path, branch, new_line or old_line
                )

        return CqCommand(
            project_path=project_path,
            pr_iid=mr.iid,
            pr_title=mr.title,
            pr_url=mr.web_url,
            discussion_id=discussion.id,
            thread_notes=thread_notes,
            file_path=file_path,
            old_line=old_line,
            new_line=new_line,
            code_snippet=code_snippet,
        )

    def report_no_session(self, project_path: str, pr_iid: int, discussion_id: str) -> bool:
        """Post error reply when no matching Leap session is found."""
        project = self._get_project(project_path)
        if not project:
            return False
        try:
            mr = project.mergerequests.get(pr_iid)
            discussion = mr.discussions.get(discussion_id)
            discussion.notes.create({"body": LEAP_NO_SESSION_MESSAGE})
            return True
        except Exception:
            logger.debug("Failed to post no-session reply on PR !%s discussion %s",
                         pr_iid, discussion_id, exc_info=True)
            return False

    def supports_notifications(self) -> bool:
        return True

    def build_first_unresponded_url(self, pr_url: str, comment_id: int,
                                    origin: str = 'r') -> str:
        """GitLab MR notes are anchored with ``#note_<note_id>`` regardless
        of whether they're code-anchored or top-level (the GitLab API
        returns them in the same /discussions endpoint, so we never need
        the *origin* discriminator GitHub does)."""
        del origin
        return f'{pr_url}#note_{comment_id}'

    def get_user_notifications(self) -> list[UserNotification]:
        """Fetch pending GitLab Todos as user notifications."""
        try:
            todos = self._gl.todos.list(state='pending', get_all=False, per_page=50)
        except Exception as exc:
            # Let 403 propagate so the poll worker can detect auth errors
            status_code = getattr(exc, 'response_code', None)
            if status_code == 403:
                raise
            logger.debug("Failed to fetch GitLab todos", exc_info=True)
            return []

        notifications: list[UserNotification] = []
        for todo in todos:
            reason = self._normalize_gitlab_action(getattr(todo, 'action_name', ''))
            target = getattr(todo, 'target', {}) or {}
            title = target.get('title', '') or getattr(todo, 'body', '')
            target_url = getattr(todo, 'target_url', '')
            project = getattr(todo, 'project', {}) or {}
            author = getattr(todo, 'author', {}) or {}
            author_name = author.get('username', '')

            # Skip self-actions (e.g. assigning yourself to a PR)
            if author_name and author_name == self._username:
                continue

            notifications.append(UserNotification(
                id=str(todo.id),
                scm_type='gitlab',
                reason=reason,
                title=title,
                target_url=target_url,
                project_name=project.get('path_with_namespace', ''),
                author=author_name,
                created_at=getattr(todo, 'created_at', ''),
            ))
        return notifications

    @staticmethod
    def _normalize_gitlab_action(action: str) -> str:
        """Normalize a GitLab todo action_name to a standard reason.

        ``approval_required`` (GitLab Premium) is treated like a review
        request: same UX, same notification category — the user is being
        asked to look at and act on the MR.  Free/CE never emits it, so
        the extra mapping is dead code there.
        """
        action = action.lower()
        if action in ('review_requested', 'approval_required'):
            return 'review_requested'
        elif action == 'assigned':
            return 'assigned'
        elif action in ('mentioned', 'directly_addressed'):
            return 'mentioned'
        return 'other'
