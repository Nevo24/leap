"""GitHub provider for PR tracking."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import requests
from github import Github

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
        # Caches keyed by (project_path, branch, pr_iid) so two PRs that
        # share the same branch (different base targets, or internal+fork
        # with same head ref) don't share cache slots.
        self._approval_cache: dict[
            tuple[str, str, Optional[int]],
            # (approved, approved_by, self_approved, changes_requested)
            tuple[bool, list[str], bool, bool],
        ] = {}
        self._status_cache: dict[tuple[str, str, Optional[int]], PRStatus] = {}
        # comment_id -> True iff we know this comment has a reaction by the
        # current user.  Used as a stale-on-failure fallback (matches the
        # GitLab provider's emoji_cache semantics).  Bounded growth ignored
        # for the moment — same trade-off as GitLab.
        self._reaction_cache: dict[int, bool] = {}
        # GraphQL endpoint for resolved-thread queries.  github.com uses
        # /graphql at the API root; GHE uses /api/graphql at the web host
        # (NOT /api/v3/graphql despite the v3 REST namespace).
        if base_url:
            stripped_base = base_url.rstrip('/')
            if stripped_base.endswith('/api/v3'):
                self._graphql_url = stripped_base[:-len('/api/v3')] + '/api/graphql'
            else:
                self._graphql_url = stripped_base + '/graphql'
        else:
            self._graphql_url = 'https://api.github.com/graphql'

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

    def _find_open_prs(self, repo, project_path: str, branch: str,
                       pr_iid: Optional[int]) -> list:
        """Find open PR(s) for the given branch — IID-first, head filter as fallback.

        - When *pr_iid* is provided (PR-pinned rows), fetch the PR directly
          via ``repo.get_pull(iid)``.  This works for **fork PRs** because
          it doesn't go through the ``head`` filter (which on GitHub
          requires the head owner — unknown for forks).
        - Otherwise, list open PRs filtered by ``<base_owner>:<branch>``.
          This is exact and cheap for internal PRs.  Fork PRs auto-tracked
          (without going through the +button URL flow) won't match — but
          that's an explicit limitation we accept rather than scan every
          open PR each poll cycle.
        """
        if pr_iid is not None:
            try:
                pr = repo.get_pull(pr_iid)
                # Confirm it's still open (PRs by IID return any state)
                if pr.state == 'open':
                    return [pr]
                return []
            except Exception:
                logger.debug("Failed to fetch PR #%s in %s by IID",
                             pr_iid, project_path)
                return []

        owner = project_path.split('/')[0]
        try:
            quick = repo.get_pulls(state='open', head=f'{owner}:{branch}')
            if quick.totalCount > 0:
                return list(quick)
            return []
        except Exception:
            logger.debug("Failed to list PRs for %s branch %s",
                         project_path, branch)
            return []

    def find_latest_closed_pr(self, project_path: str,
                              branch: str) -> Optional[ClosedPRInfo]:
        repo = self._get_repo(project_path)
        if not repo:
            return None
        # head filter uses the BASE repo owner, so a merged/closed *fork* PR
        # won't match — same accepted limitation as ``_find_open_prs``.  The
        # caller degrades gracefully to the plain "no open PR" alert.
        owner = project_path.split('/')[0]
        try:
            pulls = repo.get_pulls(
                state='closed', head=f'{owner}:{branch}',
                sort='updated', direction='desc',
            )
            for pr in pulls:
                # ``merged_at`` is present in the list response, so this
                # avoids the extra full-PR GET that reading ``pr.merged``
                # would lazily trigger.
                return ClosedPRInfo(
                    pr_iid=pr.number,
                    pr_title=pr.title or '',
                    pr_url=pr.html_url,
                    merged=pr.merged_at is not None,
                )
            return None
        except Exception:
            logger.debug("Failed to look up closed PRs for %s branch %s",
                         project_path, branch)
            return None

    def get_pr_details(self, project_path: str, pr_iid: int) -> Optional[PRDetails]:
        repo = self._get_repo(project_path)
        if not repo:
            return None
        try:
            pr = repo.get_pull(pr_iid)
            # For fork PRs the source branch lives in pr.head.repo (the
            # fork), not in repo (the base).  Querying the wrong repo
            # would always 404 and falsely flag the branch as deleted.
            # When pr.head.repo is None, the fork itself has been deleted;
            # falling back to base repo is a no-op (the branch isn't there
            # either) and correctly results in branch_deleted=True.
            head_repo = pr.head.repo or repo
            branch_deleted = False
            try:
                head_repo.get_branch(pr.head.ref)
            except Exception:
                branch_deleted = True
            return PRDetails(
                source_branch=pr.head.ref,
                pr_title=pr.title,
                pr_url=pr.html_url,
                source_branch_deleted=branch_deleted,
            )
        except Exception:
            logger.debug("Failed to get PR #%s in %s", pr_iid, project_path)
            return None

    def _github_checks_failed(self, repo: Any, pr: Any) -> bool:
        """Whether the PR's head commit has a *failed* check (not just pending).

        Called only when ``mergeable_state`` is 'unstable'/'blocked' (so clean
        PRs incur no extra API calls).  Reads the head commit's check-runs
        (GitHub Actions) and, as a fallback, the legacy combined commit status.
        Pending/running checks are NOT treated as failures — distinguishing
        those from real failures is the whole reason ``mergeable_state`` alone
        isn't enough.  Best-effort: any read failure returns False.
        """
        try:
            sha = pr.head.sha
        except Exception:
            return False
        try:
            commit = repo.get_commit(sha)
        except Exception:
            logger.debug("Failed to fetch head commit %s for checks", sha, exc_info=True)
            return False
        # 'failure'/'timed_out'/'action_required' are conclusive failures;
        # 'success'/'neutral'/'skipped'/'stale'/'cancelled' and a None
        # conclusion (still running) are not.
        failing = {'failure', 'timed_out', 'action_required'}
        try:
            for run in commit.get_check_runs():
                if getattr(run, 'conclusion', None) in failing:
                    return True
        except Exception:
            logger.debug("Failed to read check-runs for %s", sha, exc_info=True)
        try:
            combined = commit.get_combined_status()
            if getattr(combined, 'state', None) in ('failure', 'error'):
                return True
        except Exception:
            logger.debug("Failed to read combined status for %s", sha, exc_info=True)
        return False

    def get_pr_status(self, project_path: str, branch: str,
                      pr_iid: Optional[int] = None) -> PRStatus:
        # Cache key includes pr_iid so two PRs that share the same branch
        # (e.g. one targeting main and one targeting develop, or an
        # internal + fork PR with the same source branch name) don't
        # clobber each other's cached status on transient API failure.
        cache_key = (project_path, branch, pr_iid)
        repo = self._get_repo(project_path)
        if not repo:
            return PRStatus(state=PRState.NO_PR)

        pulls = self._find_open_prs(repo, project_path, branch, pr_iid)
        if not pulls:
            return PRStatus(state=PRState.NO_PR)
        pr = pulls[0]
        pr_number = pr.number
        pr_url = pr.html_url
        pr_title = pr.title

        # Draft + merge-conflict + failing-checks state.  ``draft`` is on the
        # list response.  ``mergeable_state`` may be lazy-fetched by PyGithub
        # (one extra GET) — 'dirty' = conflicts; 'None'/'unknown' = still
        # computing (treated as not-known).
        #
        # ``mergeable_state`` alone can't tell "CI failed" from "CI pending"
        # ('unstable' / 'blocked' cover both), so when it signals a possible
        # problem we confirm with a head-commit check-runs lookup.  That
        # lookup is GATED on 'unstable'/'blocked' so clean PRs (the majority)
        # cost no extra calls.
        draft = False
        has_conflicts = False
        checks_failed = False
        try:
            draft = bool(getattr(pr, 'draft', False))
        except Exception:
            logger.debug("Failed to read draft for PR #%s", pr_number, exc_info=True)
        try:
            mergeable_state = getattr(pr, 'mergeable_state', None)
            has_conflicts = (mergeable_state == 'dirty')
            if mergeable_state in ('unstable', 'blocked'):
                checks_failed = self._github_checks_failed(repo, pr)
        except Exception:
            logger.debug("Failed to read mergeable_state for PR #%s",
                         pr_number, exc_info=True)

        # Check approval status
        approval_failed = False
        approved = False
        approved_by: list[str] = []
        self_approved = False
        changes_requested = False
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
                    if reviewer == self._username:
                        self_approved = True
            # A reviewer whose *latest* review is CHANGES_REQUESTED is still
            # blocking (a later APPROVED by the same reviewer overrides it,
            # since latest_reviews keeps only the last state per reviewer).
            changes_requested = any(
                s == 'CHANGES_REQUESTED' for s in latest_reviews.values())
            self._approval_cache[cache_key] = (
                approved, list(approved_by), self_approved, changes_requested)
        except Exception:
            logger.debug("Failed to fetch review status for PR #%s", pr_number)
            approval_failed = True
            cached_approval = self._approval_cache.get(cache_key)
            if cached_approval is not None:
                approved, approved_by = cached_approval[0], list(cached_approval[1])
                self_approved = cached_approval[2]
                if len(cached_approval) > 3:
                    changes_requested = cached_approval[3]

        # Count unresponded review comment threads (+ PR conversation)
        try:
            unresponded, first_comment_id, first_origin = (
                self._count_unresponded_threads(repo, pr)
            )
        except Exception:
            logger.debug("Failed to count unresponded threads for PR #%s", pr_number)
            cached = self._status_cache.get(cache_key)
            if cached is not None:
                if not approval_failed:
                    return PRStatus(
                        state=cached.state,
                        unresponded_count=cached.unresponded_count,
                        pr_url=pr_url, pr_title=pr_title, pr_iid=pr_number,
                        first_unresponded_note_id=cached.first_unresponded_note_id,
                        first_unresponded_url=cached.first_unresponded_url,
                        approved=approved, approved_by=approved_by or None, self_approved=self_approved,
                        draft=draft, has_conflicts=has_conflicts,
                        changes_requested=changes_requested, checks_failed=checks_failed,
                    )
                return cached
            return PRStatus(
                state=PRState.ALL_RESPONDED,
                pr_url=pr_url, pr_title=pr_title, pr_iid=pr_number,
                approved=approved, approved_by=approved_by or None, self_approved=self_approved,
                draft=draft, has_conflicts=has_conflicts,
                changes_requested=changes_requested, checks_failed=checks_failed,
            )

        if unresponded > 0:
            first_url = (
                self.build_first_unresponded_url(
                    pr_url, first_comment_id, origin=first_origin or 'r')
                if first_comment_id is not None else None
            )
            result = PRStatus(
                state=PRState.UNRESPONDED,
                unresponded_count=unresponded,
                pr_url=pr_url, pr_title=pr_title, pr_iid=pr_number,
                first_unresponded_note_id=first_comment_id,
                first_unresponded_url=first_url,
                approved=approved, approved_by=approved_by or None, self_approved=self_approved,
                draft=draft, has_conflicts=has_conflicts,
                changes_requested=changes_requested, checks_failed=checks_failed,
            )
        else:
            result = PRStatus(
                state=PRState.ALL_RESPONDED,
                pr_url=pr_url, pr_title=pr_title, pr_iid=pr_number,
                approved=approved, approved_by=approved_by or None, self_approved=self_approved,
                draft=draft, has_conflicts=has_conflicts,
                changes_requested=changes_requested, checks_failed=checks_failed,
            )

        self._status_cache[cache_key] = result
        return result

    def _count_unresponded_threads(
        self, repo, pr,
    ) -> tuple[int, Optional[int], Optional[str]]:
        """Count unresponded threads — review comments + PR conversation.

        GitHub splits "thread state" between two endpoints that GitLab unifies
        in /discussions:
          - ``pr.get_review_comments()`` — file-line annotations, threaded
            via ``in_reply_to_id``
          - ``pr.get_issue_comments()`` — top-level PR conversation (flat,
            no threading)
        We treat the whole issue-comment stream as one virtual thread so
        users posting blocking review feedback in the conversation tab
        aren't silently missed.

        Returns ``(count, first_comment_id, first_origin)`` where
        ``first_origin`` is ``'r'`` (review) or ``'i'`` (issue) — used to
        select the right URL anchor and ack endpoint downstream.
        """
        unresponded = 0
        first_comment_id: Optional[int] = None
        first_origin: Optional[str] = None

        # 1. Review-comment threads.  Fetch resolution status (GraphQL) so
        # threads that someone already clicked "Resolve conversation" on
        # don't keep pulsing — REST v3 doesn't expose that flag.
        try:
            review_comments = list(pr.get_review_comments())
        except Exception:
            logger.debug("Failed to fetch review comments for PR #%s", pr.number)
            review_comments = []
        resolved_root_ids: set[int] = set()
        if review_comments:
            project_path = repo.full_name
            fetched = self._fetch_resolved_thread_root_ids(project_path, pr.number)
            if fetched is not None:
                resolved_root_ids = fetched
            threads = self._group_into_threads(review_comments)
            for root_id, thread_comments in threads.items():
                if not thread_comments or root_id in resolved_root_ids:
                    continue
                if self._is_unresponded_thread(thread_comments):
                    unresponded += 1
                    if first_comment_id is None:
                        first_comment_id = thread_comments[0].id
                        first_origin = 'r'

        # 2. PR conversation as a single virtual thread
        try:
            issue_comments = list(pr.get_issue_comments())
        except Exception:
            logger.debug("Failed to fetch issue comments for PR #%s", pr.number)
            issue_comments = []
        if issue_comments and self._is_unresponded_thread(issue_comments):
            unresponded += 1
            if first_comment_id is None:
                # Anchor on the most recent non-user comment so the user
                # lands on the message that needs a response.
                human = self._filter_human_comments(issue_comments)
                for c in human:
                    if c.user and c.user.login != self._username:
                        first_comment_id = c.id
                        first_origin = 'i'

        return unresponded, first_comment_id, first_origin

    def _filter_human_comments(self, comments: list) -> list:
        """Apply the bot filter consistently across review + issue comments."""
        return [
            c for c in comments
            if not c.user or (not self._filter_bots or c.user.type != 'Bot')
        ]

    # Cap how many GraphQL pages we'll fetch per PR per call.  At 100
    # threads/page this covers PRs with up to 300 review threads; the
    # very long-tail case where a PR has more than that is rare and we
    # accept that some resolved threads may slip through (they'd just
    # appear as unresponded — no functional harm).
    _GRAPHQL_THREAD_PAGE_CAP = 3

    def _fetch_resolved_thread_root_ids(
        self, project_path: str, pr_number: int,
    ) -> Optional[set[int]]:
        """Return the set of *root* review-comment ``databaseId`` values for
        review threads currently marked **resolved** on GitHub.

        Resolution state is GraphQL-only on the GitHub REST v3 API, so we
        fall back to a direct HTTP call rather than depend on a typed
        client.  Returns ``None`` on failure so callers can distinguish
        "no resolved threads" from "couldn't determine" and avoid hiding
        unresponded threads on transient errors.

        Paginates up to ``_GRAPHQL_THREAD_PAGE_CAP`` pages of 100 threads
        — enough headroom for typical PRs without unbounded fetches on
        pathologically large ones.
        """
        try:
            owner, name = project_path.split('/', 1)
        except ValueError:
            return None

        query = (
            'query($owner:String!,$name:String!,$number:Int!,$cursor:String){'
            ' repository(owner:$owner,name:$name){'
            '  pullRequest(number:$number){'
            '   reviewThreads(first:100,after:$cursor){'
            '    pageInfo{hasNextPage endCursor}'
            '    nodes{isResolved comments(first:1){nodes{databaseId}}}'
            '   }'
            '  }'
            ' }'
            '}'
        )
        resolved: set[int] = set()
        cursor: Optional[str] = None
        for _ in range(self._GRAPHQL_THREAD_PAGE_CAP):
            try:
                resp = requests.post(
                    self._graphql_url,
                    headers={'Authorization': f'bearer {self._token}'},
                    json={'query': query,
                          'variables': {'owner': owner, 'name': name,
                                        'number': pr_number,
                                        'cursor': cursor}},
                    timeout=10,
                )
                if resp.status_code != 200:
                    return None
                payload = resp.json()
            except Exception:
                logger.debug("GraphQL reviewThreads query failed for %s#%s",
                             project_path, pr_number, exc_info=True)
                return None

            if 'errors' in payload:
                logger.debug("GraphQL errors for %s#%s: %s",
                             project_path, pr_number, payload['errors'])
                return None
            try:
                review_threads = (
                    payload['data']['repository']['pullRequest']['reviewThreads']
                )
                threads = review_threads['nodes']
                page_info = review_threads['pageInfo']
            except (KeyError, TypeError):
                return None

            for thread in threads or []:
                if not thread.get('isResolved'):
                    continue
                comment_nodes = (thread.get('comments', {}) or {}).get('nodes', [])
                if comment_nodes:
                    root_id = comment_nodes[0].get('databaseId')
                    if isinstance(root_id, int):
                        resolved.add(root_id)

            if not page_info.get('hasNextPage'):
                break
            cursor = page_info.get('endCursor')
            if not cursor:
                break

        return resolved

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
        """Check if a thread of review/issue comments is unresponded.

        A thread is "unresponded" if:
        - It has comments from someone other than the user
        - The last human comment is from someone else, AND
        - The user hasn't replied after it AND hasn't reacted to it
          with an emoji (matching GitLab's award-emoji ack semantics).
        """
        human_comments = self._filter_human_comments(comments)
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
        # (/leap commands don't count as a real reply)
        for comment in human_comments[last_other_idx + 1:]:
            if comment.user and comment.user.login == self._username \
                    and (comment.body or '').strip() != '/leap':
                return False

        # Emoji reaction on the last other-user comment counts as ack —
        # mirrors GitLab's award-emoji semantics so cross-platform users
        # get the same UX.
        if self._user_reacted_to_comment(human_comments[last_other_idx]):
            return False

        return True

    def _user_reacted_to_comment(self, comment) -> bool:
        """Whether the configured user has any reaction on *comment*.

        Caches per-comment results.  On transient API failure, returns the
        cached value if present (and ``False`` if not) — same flapping
        protection the GitLab provider uses for its award-emoji check.
        """
        comment_id = getattr(comment, 'id', None)
        if comment_id is None:
            return False
        try:
            for reaction in comment.get_reactions():
                user = getattr(reaction, 'user', None)
                if user is not None and user.login == self._username:
                    self._reaction_cache[comment_id] = True
                    return True
            self._reaction_cache[comment_id] = False
            return False
        except Exception:
            return self._reaction_cache.get(comment_id, False)

    def scan_leap_commands(self, project_path: str, branch: str,
                           pr_iid: Optional[int] = None) -> list[CqCommand]:
        """Scan open PRs for /leap commands — review threads + PR conversation."""
        repo = self._get_repo(project_path)
        if not repo:
            return []

        pulls = self._find_open_prs(repo, project_path, branch, pr_iid)

        commands: list[CqCommand] = []
        for pr in pulls:
            # Review-comment threads (file-line annotations)
            try:
                review_comments = list(pr.get_review_comments())
            except Exception:
                logger.debug("Failed to fetch review comments for PR #%s", pr.number)
                review_comments = []

            # Skip threads someone already resolved on the PR — clicking
            # "Resolve conversation" on a /leap thread should dismiss it.
            resolved_root_ids: set[int] = set()
            if review_comments:
                fetched = self._fetch_resolved_thread_root_ids(project_path, pr.number)
                if fetched is not None:
                    resolved_root_ids = fetched

            threads = self._group_into_threads(review_comments)
            for root_id, thread_comments in threads.items():
                if root_id in resolved_root_ids:
                    continue
                cmd = self._check_thread_for_leap(
                    repo, project_path, pr, root_id, thread_comments, branch
                )
                if cmd:
                    commands.append(cmd)

            # PR conversation — single virtual thread, may contain its own /leap
            try:
                issue_comments = list(pr.get_issue_comments())
            except Exception:
                logger.debug("Failed to fetch issue comments for PR #%s", pr.number)
                issue_comments = []
            if issue_comments:
                cmd = self._check_issue_comments_for_leap(
                    project_path, pr, issue_comments
                )
                if cmd:
                    commands.append(cmd)

        return commands

    def _check_thread_for_leap(
        self, repo, project_path: str, pr, root_id: int,
        thread_comments: list, branch: str
    ) -> Optional[CqCommand]:
        """Check a single review comment thread for a /leap trigger."""
        if not thread_comments:
            return None

        # Find the last /leap trigger and last bot acknowledgment.
        # An ack only covers /leap commands that appear before it.
        last_leap_index = -1
        last_ack_index = -1
        for i, comment in enumerate(thread_comments):
            body = (comment.body or '').strip()
            author = comment.user.login if comment.user else ''
            if body == '/leap' and author == self._username:
                last_leap_index = i
            if LEAP_ACK_MESSAGE in (comment.body or ''):
                last_ack_index = i

        if last_leap_index < 0 or last_leap_index < last_ack_index:
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
            pr_iid=pr.number,
            pr_title=pr.title,
            pr_url=pr.html_url,
            # 'r:' prefix marks this as a review-comment thread so
            # acknowledge_leap_command routes to create_review_comment_reply.
            discussion_id=f'r:{root_id}',
            thread_notes=thread_notes,
            file_path=file_path,
            old_line=old_line,
            new_line=new_line,
            code_snippet=code_snippet,
            scm_type='github',
        )

    def _check_issue_comments_for_leap(
        self, project_path: str, pr, issue_comments: list,
    ) -> Optional[CqCommand]:
        """Check the PR's conversation tab for an unacked ``/leap`` from us.

        GitHub issue comments are flat (no in_reply_to threading), so the
        whole conversation is treated as a single virtual thread.  Behaviour
        mirrors the review-thread path: a ``/leap`` only counts if it has
        no ``[Leap bot] on it!`` ack posted *after* it.
        """
        if not issue_comments:
            return None

        last_leap_index = -1
        last_ack_index = -1
        for i, comment in enumerate(issue_comments):
            body = (comment.body or '').strip()
            author = comment.user.login if comment.user else ''
            if body == '/leap' and author == self._username:
                last_leap_index = i
            if LEAP_ACK_MESSAGE in (comment.body or ''):
                last_ack_index = i

        if last_leap_index < 0 or last_leap_index < last_ack_index:
            return None

        thread_notes = []
        for comment in issue_comments:
            author = comment.user.login if comment.user else ''
            thread_notes.append({
                'author': author,
                'body': comment.body or '',
                'created_at': str(comment.created_at) if comment.created_at else '',
            })

        return CqCommand(
            project_path=project_path,
            pr_iid=pr.number,
            pr_title=pr.title,
            pr_url=pr.html_url,
            # 'i:' prefix → issue-comment ack uses pr.create_issue_comment().
            # Anchor the (single) command to the most recent issue comment id.
            discussion_id=f'i:{issue_comments[-1].id}',
            thread_notes=thread_notes,
            scm_type='github',
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

    @staticmethod
    def _split_discussion_id(discussion_id: str) -> tuple[str, int]:
        """Parse a CqCommand.discussion_id into (origin, comment_id).

        - ``'r:<int>'``  → review-comment thread root id
        - ``'i:<int>'``  → issue-comment id (anchor only — issue comments
                           don't form threads)
        - bare ``'<int>'`` → backward-compat: treat as review thread root.
        """
        if discussion_id.startswith('r:'):
            return 'r', int(discussion_id[2:])
        if discussion_id.startswith('i:'):
            return 'i', int(discussion_id[2:])
        return 'r', int(discussion_id)

    def _post_leap_reply(self, project_path: str, pr_iid: int,
                        discussion_id: str, body: str) -> bool:
        """Shared writer for ack + no-session messages.

        Routes to ``create_review_comment_reply`` for review threads or
        ``create_issue_comment`` for PR conversation comments.
        """
        repo = self._get_repo(project_path)
        if not repo:
            return False
        try:
            origin, comment_id = self._split_discussion_id(discussion_id)
        except (ValueError, AttributeError):
            logger.debug("Bad discussion_id %r on PR #%s", discussion_id, pr_iid)
            return False
        try:
            pr = repo.get_pull(pr_iid)
            if origin == 'i':
                # Issue comments are flat — post a new top-level comment.
                # We could try to "reply" by quoting the original, but plain
                # post matches GitLab's behaviour for general discussions.
                pr.create_issue_comment(body)
            else:
                root_comment = pr.get_review_comment(comment_id)
                pr.create_review_comment_reply(root_comment.id, body)
            return True
        except Exception:
            logger.debug("Failed to post %r on PR #%s discussion %s",
                         body, pr_iid, discussion_id, exc_info=True)
            return False

    def acknowledge_leap_command(self, project_path: str, pr_iid: int, discussion_id: str) -> bool:
        """Post '[Leap bot] on it!' reply to the review thread or PR conversation."""
        return self._post_leap_reply(project_path, pr_iid, discussion_id, LEAP_ACK_MESSAGE)

    def report_no_session(self, project_path: str, pr_iid: int, discussion_id: str) -> bool:
        """Post error reply when no matching Leap session is found."""
        return self._post_leap_reply(project_path, pr_iid, discussion_id, LEAP_NO_SESSION_MESSAGE)

    def supports_notifications(self) -> bool:
        return True

    def build_first_unresponded_url(self, pr_url: str, comment_id: int,
                                    origin: str = 'r') -> str:
        """Anchor format depends on which endpoint the comment came from.

        - Review comments (file-line annotations): ``#discussion_r<id>``
        - Issue/conversation comments: ``#issuecomment-<id>``

        ``comment_id`` is the comment's database id (``comment.id`` on the
        PyGithub object) for both kinds.
        """
        if origin == 'i':
            return f'{pr_url}#issuecomment-{comment_id}'
        return f'{pr_url}#discussion_r{comment_id}'

    def get_user_notifications(self) -> list[UserNotification]:
        """Fetch pending GitHub notifications as user notifications."""
        try:
            raw = self._gh.get_user().get_notifications(all=False)
            # Slice to at most 50
            items = []
            for i, n in enumerate(raw):
                if i >= 50:
                    break
                items.append(n)
        except Exception as exc:
            # Let 403 propagate so the poll worker can detect auth errors
            status_code = getattr(exc, 'status', None)
            if status_code == 403:
                raise
            logger.debug("Failed to fetch GitHub notifications", exc_info=True)
            return []

        notifications: list[UserNotification] = []
        for n in items:
            reason = self._normalize_github_reason(n.reason or '')
            subject = n.subject
            title = (subject.title or '') if subject else ''
            target_url = self._resolve_notification_url(n)
            repo = n.repository
            # GitHub's /notifications endpoint does not expose the actor on
            # the notification object — actor lives only on the underlying
            # issue/PR/comment resource, fetching which would cost an extra
            # API call per notification (≤50 per poll).  We accept the gap
            # rather than burn rate-limit budget; callers must treat
            # author='' as "unknown" (matching the dataclass default).
            notifications.append(UserNotification(
                id=str(n.id),
                scm_type='github',
                reason=reason,
                title=title,
                target_url=target_url,
                project_name=repo.full_name if repo else '',
                author='',
                created_at=str(n.updated_at) if n.updated_at else '',
            ))
        return notifications

    @staticmethod
    def _normalize_github_reason(reason: str) -> str:
        """Normalize a GitHub notification reason to a standard reason."""
        reason = reason.lower()
        if reason == 'review_requested':
            return 'review_requested'
        elif reason == 'assign':
            return 'assigned'
        elif reason in ('mention', 'team_mention'):
            return 'mentioned'
        return 'other'

    @staticmethod
    def _resolve_notification_url(notification) -> str:
        """Convert a GitHub notification's API URL to an HTML URL.

        Handles the path segments that differ between API and web URLs:
        ``/pulls/`` → ``/pull/``, ``/commits/<sha>`` → ``/commit/<sha>``.
        Releases use IDs in the API but tag names in the web URL; we can't
        translate that without an extra fetch, so we fall the user to the
        repo's releases page instead of leaving them on a 404.

        For subject types we don't recognise (workflow runs, discussions,
        check suites, …), we fall back to the repository's HTML URL so the
        user lands somewhere navigable.
        """
        repo_html = ''
        repo = getattr(notification, 'repository', None)
        if repo is not None:
            repo_html = getattr(repo, 'html_url', '') or ''

        subject = notification.subject
        api_url = (subject.url or '') if subject else ''
        if not api_url:
            return repo_html

        try:
            # github.com → github.com web host
            url = api_url.replace('https://api.github.com/repos/',
                                  'https://github.com/')
            # GHE Enterprise: /api/v3/repos/ → /
            if '/api/v3/repos/' in url:
                url = url.replace('/api/v3/repos/', '/')
            # Path segments that differ between API and web
            url = url.replace('/pulls/', '/pull/')
            url = url.replace('/commits/', '/commit/')
            # Releases: API uses numeric ID, web uses tag — fall back to /releases
            url = re.sub(r'/releases/\d+(?:/.*)?$', '/releases', url)

            # If the URL still looks API-shaped, the subject type isn't one
            # we know how to translate.  Fall back to the repo URL.
            if 'api.github.com' in url or '/api/v3/' in url:
                return repo_html
            return url
        except Exception:
            return repo_html

    def collect_unresponded_threads(self, project_path: str, branch: str,
                                    pr_iid: Optional[int] = None) -> list[CqCommand]:
        """Collect all unresponded threads — review threads + PR conversation."""
        repo = self._get_repo(project_path)
        if not repo:
            return []

        pulls = self._find_open_prs(repo, project_path, branch, pr_iid)
        if not pulls:
            return []

        pr = pulls[0]

        commands: list[CqCommand] = []

        # Review-comment threads
        try:
            review_comments = list(pr.get_review_comments())
        except Exception:
            logger.debug("Failed to fetch review comments for PR #%s", pr.number)
            review_comments = []
        resolved_root_ids: set[int] = set()
        if review_comments:
            fetched = self._fetch_resolved_thread_root_ids(project_path, pr.number)
            if fetched is not None:
                resolved_root_ids = fetched
        threads = self._group_into_threads(review_comments)
        for root_id, thread_comments in threads.items():
            if root_id in resolved_root_ids:
                continue
            if not self._is_unresponded_thread(thread_comments):
                continue
            cmd = self._build_leap_command_from_thread(
                repo, project_path, pr, root_id, thread_comments, branch
            )
            if cmd:
                commands.append(cmd)

        # PR conversation as a single virtual thread
        try:
            issue_comments = list(pr.get_issue_comments())
        except Exception:
            logger.debug("Failed to fetch issue comments for PR #%s", pr.number)
            issue_comments = []
        if issue_comments and self._is_unresponded_thread(issue_comments):
            cmd = self._build_leap_command_from_issue_comments(
                project_path, pr, issue_comments
            )
            if cmd:
                commands.append(cmd)

        return commands

    def _build_leap_command_from_thread(
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
            pr_iid=pr.number,
            pr_title=pr.title,
            pr_url=pr.html_url,
            discussion_id=f'r:{root_id}',
            thread_notes=thread_notes,
            file_path=file_path,
            old_line=old_line,
            new_line=new_line,
            code_snippet=code_snippet,
            scm_type='github',
        )

    def _build_leap_command_from_issue_comments(
        self, project_path: str, pr, issue_comments: list,
    ) -> Optional[CqCommand]:
        """Build a CqCommand from the PR's issue-comment conversation."""
        if not issue_comments:
            return None

        thread_notes = []
        for comment in issue_comments:
            author = comment.user.login if comment.user else ''
            thread_notes.append({
                'author': author,
                'body': comment.body or '',
                'created_at': str(comment.created_at) if comment.created_at else '',
            })

        return CqCommand(
            project_path=project_path,
            pr_iid=pr.number,
            pr_title=pr.title,
            pr_url=pr.html_url,
            discussion_id=f'i:{issue_comments[-1].id}',
            thread_notes=thread_notes,
            scm_type='github',
        )
