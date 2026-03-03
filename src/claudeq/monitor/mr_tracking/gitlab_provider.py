"""GitLab provider for MR tracking."""

from __future__ import annotations

import base64
import logging
from typing import Optional

import gitlab

from claudeq.monitor.mr_tracking.base import MRDetails, MRState, MRStatus, SCMProvider, UserNotification
from claudeq.monitor.mr_tracking.cq_command import CqCommand

logger = logging.getLogger(__name__)

CQ_BOT_PREFIX = "[ClaudeQ bot]"
CQ_ACK_MESSAGE = f"{CQ_BOT_PREFIX} on it!"
CQ_NO_SESSION_MESSAGE = f"{CQ_BOT_PREFIX} No matching ClaudeQ session found for this project."


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
        self._approval_cache: dict[tuple[str, str], tuple[bool, list[str]]] = {}
        self._status_cache: dict[tuple[str, str], MRStatus] = {}
        self._emoji_cache: dict[int, bool] = {}  # note_id -> user_reacted

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

    def get_mr_details(self, project_path: str, mr_iid: int) -> Optional[MRDetails]:
        project = self._get_project(project_path)
        if not project:
            return None
        try:
            mr = project.mergerequests.get(mr_iid)
            branch_deleted = False
            try:
                project.branches.get(mr.source_branch)
            except Exception:
                branch_deleted = True
            return MRDetails(
                source_branch=mr.source_branch,
                mr_title=mr.title,
                mr_url=mr.web_url,
                source_branch_deleted=branch_deleted,
            )
        except Exception:
            logger.debug("Failed to get MR !%s in %s", mr_iid, project_path)
            return None

    def get_mr_status(self, project_path: str, branch: str) -> MRStatus:
        cache_key = (project_path, branch)
        project = self._get_project(project_path)
        if not project:
            return MRStatus(state=MRState.NO_MR)

        try:
            mrs = project.mergerequests.list(
                state='opened',
                source_branch=branch,
                get_all=False,
            )
        except Exception:
            logger.debug("Failed to list MRs for %s branch %s", project_path, branch)
            return MRStatus(state=MRState.NO_MR)

        if not mrs:
            return MRStatus(state=MRState.NO_MR)

        mr = mrs[0]
        mr_iid = mr.iid
        mr_url = mr.web_url
        mr_title = mr.title

        # Fetch the full MR object once (used for approvals + discussions)
        try:
            mr_full = project.mergerequests.get(mr_iid)
        except Exception:
            logger.debug("Failed to fetch full MR !%s", mr_iid, exc_info=True)
            cached = self._status_cache.get(cache_key)
            if cached is not None:
                return cached
            return MRStatus(
                state=MRState.ALL_RESPONDED,
                mr_url=mr_url, mr_title=mr_title, mr_iid=mr_iid,
            )

        # Check approval status
        approval_failed = False
        approved = False
        approved_by: list[str] = []
        try:
            approvals = mr_full.approvals.get()
            for entry in getattr(approvals, 'approved_by', []):
                name = self._extract_user_name(entry)
                if name:
                    approved_by.append(name)
            # Fallback 1: check MR object's approved_by attribute
            if not approved_by:
                for entry in getattr(mr_full, 'approved_by', []):
                    name = self._extract_user_name(entry)
                    if name and name not in approved_by:
                        approved_by.append(name)
            # Fallback 2: check approval_state for approver names
            if not approved_by:
                try:
                    state = mr_full.approval_state.get()
                    for rule in getattr(state, 'rules', []):
                        rule_data = rule if isinstance(rule, dict) else vars(rule)
                        for user in rule_data.get('approved_by', []):
                            name = self._extract_user_name(user)
                            if name and name not in approved_by:
                                approved_by.append(name)
                except Exception:
                    pass
            # Only mark as approved if someone actually approved — GitLab
            # returns approved=True when zero approvals are required, which
            # is misleading in the UI.
            approved = len(approved_by) > 0
            logger.debug("Approvals for MR !%s: approved=%s approved_by=%s",
                         mr_iid, approved, approved_by)
            self._approval_cache[cache_key] = (approved, list(approved_by))
        except Exception:
            logger.debug("Failed to fetch approval status for MR !%s", mr_iid, exc_info=True)
            approval_failed = True
            cached_approval = self._approval_cache.get(cache_key)
            if cached_approval is not None:
                approved, approved_by = cached_approval[0], list(cached_approval[1])

        # Fetch discussions to count unresponded threads
        try:
            discussions = mr_full.discussions.list(get_all=True)
        except Exception:
            logger.debug("Failed to fetch discussions for MR !%s", mr_iid)
            cached = self._status_cache.get(cache_key)
            if cached is not None:
                # Use cached status but update approval fields if they were fresh
                if not approval_failed:
                    return MRStatus(
                        state=cached.state,
                        unresponded_count=cached.unresponded_count,
                        mr_url=mr_url, mr_title=mr_title, mr_iid=mr_iid,
                        first_unresponded_note_id=cached.first_unresponded_note_id,
                        approved=approved, approved_by=approved_by or None,
                    )
                return cached
            return MRStatus(
                state=MRState.ALL_RESPONDED,
                mr_url=mr_url, mr_title=mr_title, mr_iid=mr_iid,
                approved=approved, approved_by=approved_by or None,
            )

        unresponded = 0
        first_note_id: Optional[int] = None
        for discussion in discussions:
            if self._is_unresponded_thread(discussion, project, mr_iid):
                unresponded += 1
                if first_note_id is None:
                    notes = discussion.attributes.get('notes', [])
                    if notes:
                        first_note_id = notes[0].get('id')

        if unresponded > 0:
            result = MRStatus(
                state=MRState.UNRESPONDED,
                unresponded_count=unresponded,
                mr_url=mr_url, mr_title=mr_title, mr_iid=mr_iid,
                first_unresponded_note_id=first_note_id,
                approved=approved, approved_by=approved_by or None,
            )
        else:
            result = MRStatus(
                state=MRState.ALL_RESPONDED,
                mr_url=mr_url, mr_title=mr_title, mr_iid=mr_iid,
                approved=approved, approved_by=approved_by or None,
            )

        self._status_cache[cache_key] = result
        return result

    def _is_unresponded_thread(self, discussion, project, mr_iid: int) -> bool:
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
        # (/cq commands don't count as a real reply)
        for note in human_notes[last_other_idx + 1:]:
            if self._note_author(note) == self._username and note.get('body', '').strip() != '/cq':
                return False

        # Check if user reacted with an emoji on the last note by someone else
        last_other_note = human_notes[last_other_idx]
        if self._user_reacted_to_note(project, mr_iid, last_other_note):
            return False

        return True

    def _user_reacted_to_note(self, project, mr_iid: int, note: dict) -> bool:
        """Check if the user has an emoji reaction on a note."""
        note_id = note.get('id')
        if not note_id:
            return False
        try:
            emojis = self._gl.http_list(
                f'/projects/{project.id}/merge_requests/{mr_iid}/notes/{note_id}/award_emoji',
                as_list=True,
            )
            result = any(
                e.get('user', {}).get('username') == self._username
                for e in emojis
            )
            self._emoji_cache[note_id] = result
            return result
        except Exception:
            return self._emoji_cache.get(note_id, False)

    def _is_bot_author(self, note: dict) -> bool:
        """Check if a note's author is a bot, with caching via GitLab user API."""
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
            is_bot = False

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
    def _note_author(note) -> str:
        """Extract username from a note (dict format from discussion attributes)."""
        if isinstance(note, dict):
            return note.get('author', {}).get('username', '')
        # Fallback for object format
        author = getattr(note, 'author', None)
        if isinstance(author, dict):
            return author.get('username', '')
        return getattr(author, 'username', '')

    def scan_cq_commands(self, project_path: str, branch: str) -> list[CqCommand]:
        """Scan open MRs for /cq commands from the configured user."""
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
            logger.debug("Failed to list MRs for /cq scan: %s branch %s", project_path, branch)
            return []

        commands = []
        for mr in mrs:
            try:
                mr_full = project.mergerequests.get(mr.iid)
                discussions = mr_full.discussions.list(get_all=True)
            except Exception:
                logger.debug("Failed to fetch discussions for MR !%s", mr.iid)
                continue

            for discussion in discussions:
                cmd = self._check_discussion_for_cq(
                    project, project_path, mr, discussion, branch
                )
                if cmd:
                    commands.append(cmd)

        return commands

    def _check_discussion_for_cq(
        self, project, project_path: str, mr, discussion, branch: str
    ) -> Optional[CqCommand]:
        """Check a single discussion for a /cq trigger."""
        notes = discussion.attributes.get('notes', [])
        if not notes:
            return None

        # Find the last /cq trigger and last bot acknowledgment.
        # An ack only covers /cq commands that appear before it.
        last_cq_index = -1
        last_ack_index = -1
        for i, note in enumerate(notes):
            body = note.get('body', '').strip()
            author = self._note_author(note)
            if body == '/cq' and author == self._username:
                last_cq_index = i
            if CQ_ACK_MESSAGE in note.get('body', ''):
                last_ack_index = i

        if last_cq_index < 0 or last_cq_index < last_ack_index:
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

        # Extract code context from the first note's position
        file_path = None
        old_line = None
        new_line = None
        code_snippet = None

        first_note = notes[0]
        position = first_note.get('position')
        if position:
            file_path = position.get('new_path') or position.get('old_path')
            new_line = position.get('new_line')
            old_line = position.get('old_line')

            if file_path:
                code_snippet = self._fetch_code_snippet(
                    project, file_path, branch, new_line or old_line
                )

        return CqCommand(
            project_path=project_path,
            mr_iid=mr.iid,
            mr_title=mr.title,
            mr_url=mr.web_url,
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

    def acknowledge_cq_command(self, project_path: str, mr_iid: int, discussion_id: str) -> bool:
        """Post '[ClaudeQ bot] on it!' reply to the discussion thread."""
        project = self._get_project(project_path)
        if not project:
            return False
        try:
            mr = project.mergerequests.get(mr_iid)
            discussion = mr.discussions.get(discussion_id)
            discussion.notes.create({"body": CQ_ACK_MESSAGE})
            return True
        except Exception:
            logger.debug("Failed to acknowledge /cq on MR !%s discussion %s",
                         mr_iid, discussion_id, exc_info=True)
            return False

    def collect_unresponded_threads(self, project_path: str, branch: str) -> list[CqCommand]:
        """Collect all unresponded discussion threads from an MR as CqCommand objects."""
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
            logger.debug("Failed to list MRs for collect_unresponded: %s branch %s",
                         project_path, branch)
            return []

        if not mrs:
            return []

        mr = mrs[0]
        try:
            mr_full = project.mergerequests.get(mr.iid)
            discussions = mr_full.discussions.list(get_all=True)
        except Exception:
            logger.debug("Failed to fetch discussions for MR !%s", mr.iid)
            return []

        commands = []
        for discussion in discussions:
            if not self._is_unresponded_thread(discussion, project, mr.iid):
                continue

            cmd = self._build_cq_command_from_discussion(
                project, project_path, mr, discussion, branch
            )
            if cmd:
                commands.append(cmd)

        return commands

    def _build_cq_command_from_discussion(
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

        # Extract code context from the first note's position
        file_path = None
        old_line = None
        new_line = None
        code_snippet = None

        first_note = notes[0]
        position = first_note.get('position')
        if position:
            file_path = position.get('new_path') or position.get('old_path')
            new_line = position.get('new_line')
            old_line = position.get('old_line')

            if file_path:
                code_snippet = self._fetch_code_snippet(
                    project, file_path, branch, new_line or old_line
                )

        return CqCommand(
            project_path=project_path,
            mr_iid=mr.iid,
            mr_title=mr.title,
            mr_url=mr.web_url,
            discussion_id=discussion.id,
            thread_notes=thread_notes,
            file_path=file_path,
            old_line=old_line,
            new_line=new_line,
            code_snippet=code_snippet,
        )

    def report_no_session(self, project_path: str, mr_iid: int, discussion_id: str) -> bool:
        """Post error reply when no matching CQ session is found."""
        project = self._get_project(project_path)
        if not project:
            return False
        try:
            mr = project.mergerequests.get(mr_iid)
            discussion = mr.discussions.get(discussion_id)
            discussion.notes.create({"body": CQ_NO_SESSION_MESSAGE})
            return True
        except Exception:
            logger.debug("Failed to post no-session reply on MR !%s discussion %s",
                         mr_iid, discussion_id, exc_info=True)
            return False

    def supports_notifications(self) -> bool:
        return True

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

            # Skip self-actions (e.g. assigning yourself to an MR)
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
        """Normalize a GitLab todo action_name to a standard reason."""
        action = action.lower()
        if action == 'review_requested':
            return 'review_requested'
        elif action == 'assigned':
            return 'assigned'
        elif action in ('mentioned', 'directly_addressed'):
            return 'mentioned'
        return 'other'
