"""GitHub provider for PR tracking."""

from __future__ import annotations

import logging
from typing import Optional

from github import Github, GithubException

from claudeq.monitor.mr_tracking.base import MRState, MRStatus, SCMProvider
from claudeq.monitor.mr_tracking.cq_command import CqCommand

logger = logging.getLogger(__name__)

CQ_BOT_PREFIX = "[ClaudeQ bot]"
CQ_ACK_MESSAGE = f"{CQ_BOT_PREFIX} on it!"
CQ_NO_SESSION_MESSAGE = f"{CQ_BOT_PREFIX} No matching ClaudeQ session found for this project."


class GitHubProvider(SCMProvider):
    """GitHub provider with review thread tracking."""

    def __init__(self, token: str, username: str, github_url: Optional[str] = None,
                 filter_bots: bool = True) -> None:
        # Normalize: treat github.com URLs as default (PyGithub needs api.github.com)
        base_url = github_url or ''
        if base_url:
            stripped = base_url.lower().rstrip('/')
            if stripped in ('https://github.com', 'http://github.com', 'github.com'):
                base_url = ''
        if base_url:
            self._gh = Github(login_or_token=token, base_url=base_url, timeout=15)
        else:
            self._gh = Github(login_or_token=token, timeout=15)
        self._token = token
        self._username = username
        self._filter_bots = filter_bots
        self._repo_cache: dict[str, object] = {}

    def test_connection(self) -> tuple[bool, str]:
        try:
            user = self._gh.get_user()
            return True, user.login
        except Exception as e:
            return False, str(e)

    def get_username(self) -> Optional[str]:
        return self._username

    def _get_repo(self, project_path: str):
        """Get a GitHub repo object, with caching."""
        if project_path in self._repo_cache:
            return self._repo_cache[project_path]
        try:
            repo = self._gh.get_repo(project_path)
            self._repo_cache[project_path] = repo
            return repo
        except Exception:
            logger.debug("Failed to get repo: %s", project_path)
            return None

    def get_mr_status(self, project_path: str, branch: str) -> MRStatus:
        repo = self._get_repo(project_path)
        if not repo:
            return MRStatus(state=MRState.NO_MR)

        try:
            pulls = list(repo.get_pulls(state='open', head=f'{project_path.split("/")[0]}:{branch}'))
        except Exception:
            logger.debug("Failed to list PRs for %s branch %s", project_path, branch)
            return MRStatus(state=MRState.NO_MR)

        if not pulls:
            return MRStatus(state=MRState.NO_MR)

        pr = pulls[0]
        pr_number = pr.number
        pr_url = pr.html_url
        pr_title = pr.title

        # Check approval status
        approved = False
        approved_by: list[str] = []
        try:
            reviews = list(pr.get_reviews())
            # Track latest review state per reviewer
            latest_reviews: dict[str, str] = {}
            for review in reviews:
                if review.user and review.state in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
                    latest_reviews[review.user.login] = review.state
            for reviewer, state in latest_reviews.items():
                if state == 'APPROVED':
                    approved = True
                    approved_by.append(reviewer)
        except Exception:
            logger.debug("Failed to fetch review status for PR #%s", pr_number)

        # Count unresponded review comment threads
        try:
            unresponded, first_comment_id = self._count_unresponded_threads(repo, pr)
        except Exception:
            logger.debug("Failed to count unresponded threads for PR #%s", pr_number)
            return MRStatus(
                state=MRState.ALL_RESPONDED,
                mr_url=pr_url, mr_title=pr_title, mr_iid=pr_number,
                approved=approved, approved_by=approved_by or None,
            )

        if unresponded > 0:
            return MRStatus(
                state=MRState.UNRESPONDED,
                unresponded_count=unresponded,
                mr_url=pr_url, mr_title=pr_title, mr_iid=pr_number,
                first_unresponded_note_id=first_comment_id,
                approved=approved, approved_by=approved_by or None,
            )

        return MRStatus(
            state=MRState.ALL_RESPONDED,
            mr_url=pr_url, mr_title=pr_title, mr_iid=pr_number,
            approved=approved, approved_by=approved_by or None,
        )

    def _count_unresponded_threads(self, repo, pr) -> tuple[int, Optional[int]]:
        """Count unresponded review comment threads on a PR.

        Returns (count, first_unresponded_comment_id).
        """
        comments = list(pr.get_review_comments())
        if not comments:
            return 0, None

        # Group comments into threads by in_reply_to_id
        threads = self._group_into_threads(comments)

        unresponded = 0
        first_comment_id: Optional[int] = None
        for thread_comments in threads.values():
            if self._is_unresponded_thread(thread_comments):
                unresponded += 1
                if first_comment_id is None:
                    first_comment_id = thread_comments[0].id

        return unresponded, first_comment_id

    def _group_into_threads(self, comments: list) -> dict[int, list]:
        """Group review comments into threads.

        Each thread is keyed by the root comment ID.
        """
        threads: dict[int, list] = {}
        for comment in comments:
            reply_to = comment.in_reply_to_id
            if reply_to:
                # This is a reply — add to existing thread
                if reply_to in threads:
                    threads[reply_to].append(comment)
                else:
                    # Root comment might not be in threads yet; create thread
                    threads[reply_to] = [comment]
            else:
                # Root comment
                if comment.id not in threads:
                    threads[comment.id] = [comment]
                else:
                    threads[comment.id].insert(0, comment)

        return threads

    def _is_unresponded_thread(self, comments: list) -> bool:
        """Check if a thread of review comments is unresponded.

        A thread is "unresponded" if:
        - It has comments from someone other than the user
        - The last human comment is from someone else (user hasn't replied after it)
        """
        # Filter out bot comments if enabled
        human_comments = [
            c for c in comments
            if not c.user or (not self._filter_bots or c.user.type != 'Bot')
        ]
        if not human_comments:
            return False

        # Check if only the user commented
        other_comments = [
            c for c in human_comments
            if c.user and c.user.login != self._username
        ]
        if not other_comments:
            return False

        # Find the last comment by someone other than the user
        last_other_idx = -1
        for i, comment in enumerate(human_comments):
            if comment.user and comment.user.login != self._username:
                last_other_idx = i

        # Check if user replied after the last other person's comment
        for comment in human_comments[last_other_idx + 1:]:
            if comment.user and comment.user.login == self._username:
                return False

        return True

    def scan_cq_commands(self, project_path: str, branch: str) -> list[CqCommand]:
        """Scan open PRs for /cq commands from the configured user."""
        repo = self._get_repo(project_path)
        if not repo:
            return []

        try:
            pulls = list(repo.get_pulls(state='open', head=f'{project_path.split("/")[0]}:{branch}'))
        except Exception:
            logger.debug("Failed to list PRs for /cq scan: %s branch %s", project_path, branch)
            return []

        commands: list[CqCommand] = []
        for pr in pulls:
            try:
                comments = list(pr.get_review_comments())
            except Exception:
                logger.debug("Failed to fetch review comments for PR #%s", pr.number)
                continue

            threads = self._group_into_threads(comments)
            for root_id, thread_comments in threads.items():
                cmd = self._check_thread_for_cq(
                    repo, project_path, pr, root_id, thread_comments, branch
                )
                if cmd:
                    commands.append(cmd)

        return commands

    def _check_thread_for_cq(
        self, repo, project_path: str, pr, root_id: int,
        thread_comments: list, branch: str
    ) -> Optional[CqCommand]:
        """Check a single review comment thread for a /cq trigger."""
        if not thread_comments:
            return None

        # Check if any comment is /cq from the configured user
        has_cq_trigger = False
        for comment in thread_comments:
            body = (comment.body or '').strip()
            author = comment.user.login if comment.user else ''
            if body == '/cq' and author == self._username:
                has_cq_trigger = True
                break

        if not has_cq_trigger:
            return None

        # Check if already acknowledged
        for comment in thread_comments:
            if CQ_ACK_MESSAGE in (comment.body or ''):
                return None

        # Extract thread notes (excluding bot comments)
        thread_notes = []
        for comment in thread_comments:
            author = comment.user.login if comment.user else ''
            thread_notes.append({
                'author': author,
                'body': comment.body or '',
                'created_at': str(comment.created_at) if comment.created_at else '',
            })

        # Extract code context from the first comment's position
        file_path = None
        old_line = None
        new_line = None
        code_snippet = None

        first_comment = thread_comments[0]
        file_path = first_comment.path
        new_line = first_comment.line or first_comment.original_line
        old_line = first_comment.original_line

        if file_path:
            code_snippet = self._fetch_code_snippet(
                repo, file_path, branch, new_line or old_line
            )

        return CqCommand(
            project_path=project_path,
            mr_iid=pr.number,
            mr_title=pr.title,
            mr_url=pr.html_url,
            discussion_id=str(root_id),
            thread_notes=thread_notes,
            file_path=file_path,
            old_line=old_line,
            new_line=new_line,
            code_snippet=code_snippet,
        )

    def _fetch_code_snippet(
        self, repo, file_path: str, branch: str, target_line: Optional[int]
    ) -> Optional[str]:
        """Fetch a code snippet around the target line from GitHub."""
        if not target_line:
            return None
        try:
            content_file = repo.get_contents(file_path, ref=branch)
            content = content_file.decoded_content.decode('utf-8')
            lines = content.splitlines()

            # Extract ~5 lines around target (2 before, target, 2 after)
            start = max(0, target_line - 3)
            end = min(len(lines), target_line + 2)
            return "\n".join(lines[start:end])
        except Exception:
            logger.debug("Failed to fetch code snippet for %s:%s", file_path, target_line)
            return None

    def acknowledge_cq_command(self, project_path: str, mr_iid: int, discussion_id: str) -> bool:
        """Post '[ClaudeQ bot] on it!' reply to the review comment thread."""
        repo = self._get_repo(project_path)
        if not repo:
            return False
        try:
            pr = repo.get_pull(mr_iid)
            # Reply to the root comment of the thread
            root_comment = pr.get_review_comment(int(discussion_id))
            pr.create_review_comment_reply(root_comment.id, CQ_ACK_MESSAGE)
            return True
        except Exception:
            logger.error("Failed to acknowledge /cq on PR #%s thread %s",
                         mr_iid, discussion_id, exc_info=True)
            return False

    def report_no_session(self, project_path: str, mr_iid: int, discussion_id: str) -> bool:
        """Post error reply when no matching CQ session is found."""
        repo = self._get_repo(project_path)
        if not repo:
            return False
        try:
            pr = repo.get_pull(mr_iid)
            root_comment = pr.get_review_comment(int(discussion_id))
            pr.create_review_comment_reply(root_comment.id, CQ_NO_SESSION_MESSAGE)
            return True
        except Exception:
            logger.error("Failed to post no-session reply on PR #%s thread %s",
                         mr_iid, discussion_id, exc_info=True)
            return False

    def collect_unresponded_threads(self, project_path: str, branch: str) -> list[CqCommand]:
        """Collect all unresponded review comment threads from a PR as CqCommand objects."""
        repo = self._get_repo(project_path)
        if not repo:
            return []

        try:
            pulls = list(repo.get_pulls(state='open', head=f'{project_path.split("/")[0]}:{branch}'))
        except Exception:
            logger.debug("Failed to list PRs for collect_unresponded: %s branch %s",
                         project_path, branch)
            return []

        if not pulls:
            return []

        pr = pulls[0]
        try:
            comments = list(pr.get_review_comments())
        except Exception:
            logger.debug("Failed to fetch review comments for PR #%s", pr.number)
            return []

        threads = self._group_into_threads(comments)

        commands: list[CqCommand] = []
        for root_id, thread_comments in threads.items():
            if not self._is_unresponded_thread(thread_comments):
                continue

            cmd = self._build_cq_command_from_thread(
                repo, project_path, pr, root_id, thread_comments, branch
            )
            if cmd:
                commands.append(cmd)

        return commands

    def _build_cq_command_from_thread(
        self, repo, project_path: str, pr, root_id: int,
        thread_comments: list, branch: str
    ) -> Optional[CqCommand]:
        """Build a CqCommand from an unresponded review comment thread."""
        if not thread_comments:
            return None

        thread_notes = []
        for comment in thread_comments:
            author = comment.user.login if comment.user else ''
            thread_notes.append({
                'author': author,
                'body': comment.body or '',
                'created_at': str(comment.created_at) if comment.created_at else '',
            })

        # Extract code context from the first comment
        file_path = None
        old_line = None
        new_line = None
        code_snippet = None

        first_comment = thread_comments[0]
        file_path = first_comment.path
        new_line = first_comment.line or first_comment.original_line
        old_line = first_comment.original_line

        if file_path:
            code_snippet = self._fetch_code_snippet(
                repo, file_path, branch, new_line or old_line
            )

        return CqCommand(
            project_path=project_path,
            mr_iid=pr.number,
            mr_title=pr.title,
            mr_url=pr.html_url,
            discussion_id=str(root_id),
            thread_notes=thread_notes,
            file_path=file_path,
            old_line=old_line,
            new_line=new_line,
            code_snippet=code_snippet,
        )
