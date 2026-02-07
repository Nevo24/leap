"""GitLab provider for MR tracking."""

from __future__ import annotations

import logging
from typing import Optional

import gitlab

from claudeq.monitor.mr_tracking.base import MRState, MRStatus, SCMProvider

logger = logging.getLogger(__name__)


class GitLabProvider(SCMProvider):
    """GitLab provider with thread-level comment tracking."""

    def __init__(self, gitlab_url: str, private_token: str, username: str,
                 filter_bots: bool = True) -> None:
        self._gl = gitlab.Gitlab(gitlab_url, private_token=private_token)
        self._username = username
        self._filter_bots = filter_bots
        self._project_cache: dict[str, gitlab.v4.objects.Project] = {}
        self._bot_cache: dict[int, bool] = {}  # user_id -> is_bot

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

    def get_mr_status(self, project_path: str, branch: str) -> MRStatus:
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

        # Fetch discussions to count unresponded threads
        try:
            mr_full = project.mergerequests.get(mr_iid)
            discussions = mr_full.discussions.list(get_all=True)
        except Exception:
            logger.debug("Failed to fetch discussions for MR !%s", mr_iid)
            return MRStatus(
                state=MRState.ALL_RESPONDED,
                mr_url=mr_url, mr_title=mr_title, mr_iid=mr_iid,
            )

        unresponded = 0
        for discussion in discussions:
            if self._is_unresponded_thread(discussion, project, mr_iid):
                unresponded += 1

        if unresponded > 0:
            return MRStatus(
                state=MRState.UNRESPONDED,
                unresponded_count=unresponded,
                mr_url=mr_url, mr_title=mr_title, mr_iid=mr_iid,
            )

        return MRStatus(
            state=MRState.ALL_RESPONDED,
            mr_url=mr_url, mr_title=mr_title, mr_iid=mr_iid,
        )

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

        # If resolved, treat as acknowledged
        if discussion.attributes.get('resolved', False):
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
        for note in human_notes[last_other_idx + 1:]:
            if self._note_author(note) == self._username:
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
            return any(
                e.get('user', {}).get('username') == self._username
                for e in emojis
            )
        except Exception:
            return False

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
    def _note_author(note) -> str:
        """Extract username from a note (dict format from discussion attributes)."""
        if isinstance(note, dict):
            return note.get('author', {}).get('username', '')
        # Fallback for object format
        author = getattr(note, 'author', None)
        if isinstance(author, dict):
            return author.get('username', '')
        return getattr(author, 'username', '')
