"""Bitbucket provider for PR tracking.

Supports both Bitbucket flavors behind one class:

- **Bitbucket Cloud** (bitbucket.org) via the v2.0 REST API
  (``https://api.bitbucket.org/2.0``).
- **Bitbucket Server / Data Center** (self-hosted) via the v1.0 REST API
  (``https://<host>/rest/api/1.0``).

The two products share no API surface, so every endpoint helper branches on
``self._is_cloud`` (decided once from the configured URL).  All public
``SCMProvider`` methods normalize both backends into the same internal
"thread" shape so the unresponded / /leap logic is written once.

Capability differences (inherent to the platforms, not omissions):

- Draft PRs: Cloud always exposes ``draft``; Data Center added it in 9.3
  (older Servers omit the key, which reads as False).
- Merge-conflict detection exists only on Server (the ``/merge`` resource);
  ``has_conflicts`` is always False for Cloud.  Cloud's ``mergeable``
  field was evaluated and deliberately NOT used: it means "passes all
  merge checks" (approvals, builds, ...), so mapping it to conflicts
  would light the conflict marker on any PR that merely lacks approvals.
- Comment emoji reactions have no public API on either flavor, so the
  "user reacted to the last comment" responded-heuristic the GitLab/GitHub
  providers use is skipped - a reply or thread resolution is required.
- User notifications are Server-only (the dashboard reviewer queue);
  Cloud exposes no notification/inbox API, so
  ``supports_notifications()`` is False there.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import quote

import requests

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

_REQUEST_TIMEOUT = 15
# Hard cap on paginated fetches (100 items per page) so a pathological PR
# with thousands of comments can't stall a poll cycle.
_PAGE_CAP = 10
# Lazy Cloud self-identity fetch: don't re-attempt a failed /2.0/user
# lookup more often than this (it's consulted per-comment during scans).
_SELF_ID_RETRY_SECONDS = 60.0


class BitbucketAuthError(Exception):
    """Auth failure (401/403) from a Bitbucket notifications fetch.

    Carries ``status = 403`` because the SCM poll worker detects
    notification auth errors via ``getattr(exc, 'status', None)``
    (the PyGithub convention) and auto-disables notifications.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.status = 403


def is_bitbucket_cloud_url(url: str) -> bool:
    """True if *url* points at Bitbucket Cloud (bitbucket.org)."""
    host = url.lower()
    if '://' in host:
        host = host.split('://', 1)[1]
    host = host.split('/', 1)[0].rsplit('@', 1)[-1].split(':', 1)[0]
    return host == 'bitbucket.org' or host.endswith('.bitbucket.org')


class BitbucketProvider(SCMProvider):
    """Bitbucket Cloud / Server provider with thread-level comment tracking."""

    def __init__(self, bitbucket_url: str, token: str, username: str,
                 auth_user: str = '', filter_bots: bool = True) -> None:
        base = (bitbucket_url or 'https://bitbucket.org').rstrip('/')
        self._is_cloud = is_bitbucket_cloud_url(base)
        if self._is_cloud:
            self._api_root = 'https://api.bitbucket.org/2.0'
            self._build_status_root = ''  # Cloud statuses live under the PR resource
        else:
            self._api_root = f'{base}/rest/api/1.0'
            self._build_status_root = f'{base}/rest/build-status/1.0'
        self._base_url = base
        self._token = token
        self._auth_user = auth_user
        self._username = username
        self._filter_bots = filter_bots

        self._session = requests.Session()
        if auth_user:
            # Cloud: email + API token, or username + app password.
            # Server: username + token also works as HTTP Basic.
            self._session.auth = (auth_user, token)
        else:
            # Access tokens (Cloud workspace/repo tokens, Server PATs)
            # authenticate as Bearer.
            self._session.headers['Authorization'] = f'Bearer {token}'

        # Keyed by (project_path, branch, pr_iid) - the pr_iid component
        # keeps two tracked PRs that share a source branch from sharing a
        # cache slot (same rationale as the GitHub provider).
        self._status_cache: dict[tuple[str, str, Optional[int]], PRStatus] = {}
        # Cloud self-identity (uuid, account_id) resolved lazily - comment
        # author objects on Cloud carry no username, so "is this me" has to
        # match on uuid/account_id.  None until first successful fetch.
        self._self_ids: Optional[tuple[str, str]] = None
        self._self_ids_lock = threading.Lock()
        # -inf, not 0.0: time.monotonic() starts near zero at boot on
        # macOS/Linux, so a 0.0 sentinel would wrongly suppress the first
        # identity fetch for a monitor launched within the retry window
        # of boot (login-item startup).
        self._self_ids_attempted_at = float('-inf')
        # Like the GitLab/GitHub providers: False when a sub-lookup failed
        # mid-scan, so the unresponded count is carried forward instead of
        # flapping into a phantom alert.  Thread-local because the SCM poll
        # worker and the collect-threads worker share this instance.
        self._scan_state = threading.local()

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _request(self, method: str, url: str, params: Optional[dict[str, Any]] = None,
                 json_body: Optional[dict[str, Any]] = None) -> requests.Response:
        return self._session.request(
            method, url, params=params, json=json_body, timeout=_REQUEST_TIMEOUT)

    def _get_json(self, url: str, params: Optional[dict[str, Any]] = None) -> Any:
        resp = self._request('GET', url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _iter_pages(self, url: str, params: Optional[dict[str, Any]] = None,
                    page_cap: int = _PAGE_CAP) -> list[dict[str, Any]]:
        """Collect ``values`` across paginated responses (both API styles).

        Raises on HTTP/network failure - callers decide whether that means
        "carry the cached value" or "skip this PR".
        """
        values: list[dict[str, Any]] = []
        if self._is_cloud:
            page_params: Optional[dict[str, Any]] = dict(params or {})
            # 50, not 100: Cloud caps pagelen per-endpoint and the
            # pullrequests list rejects values above 50 with HTTP 400 -
            # which would read as a permanent "transient failure" here.
            # 50 is within every endpoint's cap.
            page_params.setdefault('pagelen', 50)
            next_url = url
            for _ in range(page_cap):
                data = self._get_json(next_url, params=page_params)
                values.extend(data.get('values', []))
                next_url = data.get('next')
                page_params = None  # the 'next' URL embeds the query string
                if not next_url:
                    break
        else:
            page_params = dict(params or {})
            page_params.setdefault('limit', 100)
            start = 0
            for _ in range(page_cap):
                page_params['start'] = start
                data = self._get_json(url, params=page_params)
                values.extend(data.get('values', []))
                if data.get('isLastPage', True):
                    break
                start = data.get('nextPageStart')
                if start is None:
                    break
        return values

    # ------------------------------------------------------------------
    # Project / PR resource helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _split_project(project_path: str) -> Optional[tuple[str, str]]:
        """Normalize a project path into (workspace-or-key, repo-slug).

        Accepts every form Leap encounters:
        - Cloud / canonical Server: ``workspace/slug`` or ``KEY/slug``
        - Server HTTPS clone remotes: ``scm/KEY/slug``
        - Server web URLs: ``projects/KEY/repos/slug``

        The Server forms are matched suffix-anchored so installs under a
        context path survive (a remote like
        ``https://host/stash/scm/key/slug.git`` parses to project path
        ``stash/scm/key/slug``).  The ``projects/…/repos/…`` form is
        checked first - it is the more specific shape, and a project key
        literally named ``scm`` must not trip the clone-path rule.
        """
        parts = [p for p in project_path.strip('/').split('/') if p]
        if (len(parts) >= 4 and parts[-4].lower() == 'projects'
                and parts[-2].lower() == 'repos'):
            return parts[-3], parts[-1]
        if len(parts) >= 3 and parts[-3].lower() == 'scm':
            return parts[-2], parts[-1]
        if len(parts) == 2:
            return parts[0], parts[1]
        return None

    @staticmethod
    def server_git_path(project_path: str) -> Optional[str]:
        """Git-over-HTTPS path for a Server repo, context segments preserved.

        Bitbucket Server clones at ``[context/]scm/KEY/slug.git``.
        Clone-form inputs (``[context/]scm/KEY/slug``) pass through
        verbatim, web-form inputs swap ``projects/K/repos/S`` for
        ``scm/K/S`` in place, and a bare ``K/S`` gains the ``scm/``
        prefix.  Without the prefix preservation, a pin derived from a
        context-path install's local remote would rebuild a clone URL
        missing the context and 404.
        """
        parts = [p for p in project_path.strip('/').split('/') if p]
        if (len(parts) >= 4 and parts[-4].lower() == 'projects'
                and parts[-2].lower() == 'repos'):
            return '/'.join(parts[:-4] + ['scm', parts[-3], parts[-1]])
        if len(parts) >= 3 and parts[-3].lower() == 'scm':
            return '/'.join(parts)
        if len(parts) == 2:
            return f'scm/{parts[0]}/{parts[1]}'
        return None

    def _repo_api(self, parts: tuple[str, str]) -> str:
        left, slug = parts
        if self._is_cloud:
            return f'{self._api_root}/repositories/{left}/{slug}'
        return f'{self._api_root}/projects/{left}/repos/{slug}'

    def _pr_resource(self, parts: tuple[str, str], pr_id: int) -> str:
        noun = 'pullrequests' if self._is_cloud else 'pull-requests'
        return f'{self._repo_api(parts)}/{noun}/{pr_id}'

    def _pr_list_url(self, parts: tuple[str, str]) -> str:
        noun = 'pullrequests' if self._is_cloud else 'pull-requests'
        return f'{self._repo_api(parts)}/{noun}'

    def _pr_web_url(self, pr: dict[str, Any]) -> str:
        links = pr.get('links') or {}
        if self._is_cloud:
            html = links.get('html') or {}
            return html.get('href', '') or ''
        self_links = links.get('self') or []
        if self_links and isinstance(self_links[0], dict):
            return self_links[0].get('href', '') or ''
        return ''

    # ------------------------------------------------------------------
    # Identity / bot helpers
    # ------------------------------------------------------------------

    def _ensure_self_ids(self) -> Optional[tuple[str, str]]:
        """Lazily resolve the Cloud (uuid, account_id) of the token's user.

        Cached after the first success; failed attempts retry at most once
        per ``_SELF_ID_RETRY_SECONDS`` so a scan over many comments doesn't
        hammer a failing endpoint.
        """
        if self._self_ids is not None:
            return self._self_ids
        with self._self_ids_lock:
            if self._self_ids is not None:
                return self._self_ids
            now = time.monotonic()
            if now - self._self_ids_attempted_at < _SELF_ID_RETRY_SECONDS:
                return None
            self._self_ids_attempted_at = now
            try:
                me = self._get_json(f'{self._api_root}/user')
                ids = (me.get('uuid', '') or '',
                       me.get('account_id', '') or '')
                # Only cache a usable identity: ('', '') would make _is_me
                # classify every author (including the user) as "not me"
                # while skipping the name fallback.
                if ids[0] or ids[1]:
                    self._self_ids = ids
                else:
                    return None
            except Exception:
                logger.debug("Failed to resolve Bitbucket Cloud identity",
                             exc_info=True)
                return None
        return self._self_ids

    def _is_me(self, user: Optional[dict[str, Any]]) -> bool:
        """Whether *user* (a comment/participant user object) is the token's user."""
        if not user:
            return False
        if not self._is_cloud:
            return user.get('name', '') == self._username
        ids = self._ensure_self_ids()
        if ids:
            my_uuid, my_account = ids
            if my_uuid and user.get('uuid') == my_uuid:
                return True
            if my_account and user.get('account_id') == my_account:
                return True
            return False
        # Identity unknown (lookup failing) - fall back to name matching and
        # flag the scan unreliable so a wrong guess can't flap the count.
        self._scan_state.reliable = False
        return (user.get('nickname') == self._username
                or user.get('username') == self._username
                or user.get('display_name') == self._username)

    def _is_bot(self, user: Optional[dict[str, Any]]) -> bool:
        """Best-effort bot check (no extra API call on either flavor)."""
        if not user:
            return False
        if self._is_cloud:
            # Real humans are type 'user'; integrations/apps report a
            # different type (e.g. 'app_user').
            return user.get('type', 'user') not in ('user', '')
        # Server service accounts are type SERVICE (humans are NORMAL).
        return user.get('type', 'NORMAL') == 'SERVICE'

    @staticmethod
    def _user_display(user: Optional[dict[str, Any]]) -> str:
        """Readable author handle for thread notes / approval lists."""
        if not user:
            return ''
        return (user.get('nickname') or user.get('name')
                or user.get('display_name') or user.get('displayName') or '')

    @staticmethod
    def _user_full_name(user: Optional[dict[str, Any]]) -> str:
        """Full display name (used for the approved-by tooltip list)."""
        if not user:
            return ''
        return (user.get('display_name') or user.get('displayName')
                or user.get('nickname') or user.get('name') or '')

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        try:
            if self._is_cloud:
                resp = self._request('GET', f'{self._api_root}/user')
                if resp.status_code != 200:
                    hint = ''
                    if resp.status_code in (401, 403):
                        hint = (' If you are using an API token or app '
                                'password, fill in the "Email / username" '
                                'field; only workspace/repo access tokens '
                                'work without it.')
                    return False, f'HTTP {resp.status_code} from Bitbucket Cloud.{hint}'
                me = resp.json()
                username = (me.get('username') or me.get('nickname')
                            or me.get('display_name') or '')
                if not username or not me.get('uuid'):
                    return False, ('Server response does not match the '
                                   'Bitbucket Cloud API. Check your URL.')
                self._self_ids = (me.get('uuid', '') or '',
                                  me.get('account_id', '') or '')
                return True, username
            # Server / Data Center: confirm it actually speaks the Server
            # API before trusting the whoami answer.
            props = self._request(
                'GET', f'{self._api_root}/application-properties')
            if props.status_code in (401, 403):
                return False, (f'HTTP {props.status_code} from the server - '
                               'check the token (an HTTP access token with '
                               'at least repository read permission).')
            ok_props = False
            if props.status_code == 200:
                try:
                    ok_props = 'version' in props.json()
                except ValueError:
                    ok_props = False
            if not ok_props:
                return False, ('Server does not appear to be Bitbucket '
                               'Server / Data Center. Check your URL.')
            whoami = self._request(
                'GET', f'{self._base_url}/plugins/servlet/applinks/whoami')
            username = (whoami.text or '').strip() if whoami.status_code == 200 else ''
            if not username:
                return False, ('Authenticated request did not return a '
                               'username. Check the token (an HTTP access '
                               'token with at least repository read '
                               'permission).')
            return True, username
        except Exception as e:
            return False, str(e)

    def get_username(self) -> Optional[str]:
        return self._username

    # ------------------------------------------------------------------
    # PR lookup
    # ------------------------------------------------------------------

    def _find_open_prs(self, parts: tuple[str, str], branch: str,
                       pr_iid: Optional[int]) -> Optional[list[dict[str, Any]]]:
        """Open PRs for *branch*, newest-listed first.

        Mirrors the GitHub provider's contract: ``None`` = transient
        failure (caller keeps cached state), ``[]`` = definite no-PR.
        When *pr_iid* is known the PR is fetched directly and the branch
        listing is never consulted - cheaper, pins the row to THAT PR,
        and the Cloud direct GET returns the full object (the list
        endpoint trims ``participants``).
        """
        if pr_iid is not None:
            # Mirror the GitHub provider's pr_iid contract exactly: open
            # returns the PR, closed returns [] (feeding the merged/closed
            # badge flow), transient failure returns None (caller keeps
            # cached state).  No fall-through to branch listing - a pinned
            # row tracks THIS PR, not whatever currently rides the branch.
            try:
                pr = self._get_json(self._pr_resource(parts, pr_iid))
                # Cloud's state enum includes DRAFT and QUEUED alongside
                # OPEN - all three are "still open" for tracking purposes.
                if pr.get('state') in ('OPEN', 'DRAFT', 'QUEUED'):
                    return [pr]
                return []
            except requests.HTTPError as exc:
                status = getattr(getattr(exc, 'response', None),
                                 'status_code', None)
                if status == 404:
                    return []  # PR deleted - definitively gone
                logger.debug("Failed to fetch Bitbucket PR #%s for %s/%s",
                             pr_iid, parts[0], parts[1])
                return None
            except Exception:
                logger.debug("Failed to fetch Bitbucket PR #%s for %s/%s",
                             pr_iid, parts[0], parts[1])
                return None
        try:
            if self._is_cloud:
                escaped = branch.replace('\\', '\\\\').replace('"', '\\"')
                # Exclude the closed states rather than matching
                # state = "OPEN": draft/queued PRs (state DRAFT/QUEUED on
                # newer Cloud) must stay tracked, and every literal compared
                # here is a documented enum member so the query can't 400.
                values = self._iter_pages(
                    self._pr_list_url(parts),
                    params={'q': f'source.branch.name = "{escaped}" '
                                 f'AND state != "MERGED" '
                                 f'AND state != "DECLINED" '
                                 f'AND state != "SUPERSEDED"'},
                    page_cap=1,
                )
            else:
                values = self._iter_pages(
                    self._pr_list_url(parts),
                    params={'state': 'OPEN', 'direction': 'OUTGOING',
                            'at': f'refs/heads/{branch}'},
                    page_cap=1,
                )
            return list(values)
        except Exception:
            logger.debug("Failed to list Bitbucket PRs for %s/%s branch %s",
                         parts[0], parts[1], branch)
            return None

    def _get_full_pr(self, parts: tuple[str, str], pr: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Return a PR object that includes participants.

        Server list responses are already complete; Cloud list responses
        are trimmed and need a direct GET.  ``None`` = fetch failed.
        """
        if not self._is_cloud or 'participants' in pr:
            return pr
        try:
            return self._get_json(self._pr_resource(parts, pr['id']))
        except Exception:
            logger.debug("Failed to fetch full Bitbucket PR #%s", pr.get('id'))
            return None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_pr_status(self, project_path: str, branch: str,
                      pr_iid: Optional[int] = None) -> PRStatus:
        cache_key = (project_path, branch, pr_iid)
        parts = self._split_project(project_path)
        if not parts:
            return PRStatus(state=PRState.NO_PR)

        prs = self._find_open_prs(parts, branch, pr_iid)
        if prs is None:
            # Transient list failure - keep the last known status rather than
            # flapping to NO_PR (which on recovery reads as a brand-new
            # approval and fires a phantom alert).
            cached = self._status_cache.get(cache_key)
            return cached if cached is not None else PRStatus(state=PRState.NO_PR)
        if not prs:
            # No open PR (merged / declined / never existed).  Drop the cached
            # open status so a later transient failure can't resurrect it.
            self._status_cache.pop(cache_key, None)
            return PRStatus(state=PRState.NO_PR)

        pr = self._get_full_pr(parts, prs[0])
        if pr is None:
            cached = self._status_cache.get(cache_key)
            if cached is not None:
                return cached
            listed = prs[0]
            return PRStatus(
                state=PRState.ALL_RESPONDED,
                pr_url=self._pr_web_url(listed),
                pr_title=listed.get('title', '') or '',
                pr_iid=listed.get('id'),
                approval_known=False,
            )

        pr_id = pr['id']
        pr_url = self._pr_web_url(pr)
        pr_title = pr.get('title', '') or ''
        prior = self._status_cache.get(cache_key)

        # Cloud always carries the draft flag; Data Center added it in 9.3
        # (older Servers simply omit the key, which reads as False here).
        draft = bool(pr.get('draft', False))

        # Merge conflicts: Server exposes them via the /merge resource;
        # Cloud has no API signal, so the flag stays False there.
        has_conflicts = False
        if not self._is_cloud:
            conflicted = self._server_pr_conflicted(parts, pr_id)
            if conflicted is None:
                has_conflicts = prior.has_conflicts if prior is not None else False
            else:
                has_conflicts = conflicted

        # Approvals + changes-requested come straight off the full PR
        # object on both flavors - no separate call that could fail.
        approved_by: list[str] = []
        self_approved = False
        changes_requested = False
        if self._is_cloud:
            entries = pr.get('participants') or []
        else:
            entries = (pr.get('reviewers') or []) + (pr.get('participants') or [])
        for entry in entries:
            user = entry.get('user') or {}
            if entry.get('approved'):
                name = self._user_full_name(user)
                if name and name not in approved_by:
                    approved_by.append(name)
                if self._is_me(user):
                    self_approved = True
            if self._is_cloud:
                if entry.get('state') == 'changes_requested':
                    changes_requested = True
            elif entry.get('status') == 'NEEDS_WORK':
                changes_requested = True
        approved = len(approved_by) > 0

        checks = self._checks_failed(parts, pr)
        if checks is None:
            checks_failed = prior.checks_failed if prior is not None else False
        else:
            checks_failed = checks

        # Fetch comment threads to count unresponded ones
        try:
            threads = self._fetch_threads(parts, pr_id)
        except Exception:
            logger.debug("Failed to fetch comments for Bitbucket PR #%s",
                         pr_id, exc_info=True)
            if prior is not None:
                return PRStatus(
                    state=prior.state,
                    unresponded_count=prior.unresponded_count,
                    pr_url=pr_url, pr_title=pr_title, pr_iid=pr_id,
                    first_unresponded_note_id=prior.first_unresponded_note_id,
                    first_unresponded_url=prior.first_unresponded_url,
                    approved=approved, approved_by=approved_by or None,
                    self_approved=self_approved,
                    draft=draft, has_conflicts=has_conflicts,
                    changes_requested=changes_requested,
                    checks_failed=checks_failed,
                )
            return PRStatus(
                state=PRState.ALL_RESPONDED,
                pr_url=pr_url, pr_title=pr_title, pr_iid=pr_id,
                approved=approved, approved_by=approved_by or None,
                self_approved=self_approved,
                draft=draft, has_conflicts=has_conflicts,
                changes_requested=changes_requested, checks_failed=checks_failed,
            )

        # Recompute unresponded threads from scratch.  Reset the
        # scan-reliable flag first; sub-lookups flip it False on transient
        # failure (see _is_me's identity fallback).
        self._scan_state.reliable = True
        unresponded = 0
        first_note_id: Optional[int] = None
        try:
            for thread in threads:
                if self._is_unresponded_thread(thread):
                    unresponded += 1
                    if first_note_id is None:
                        first_note_id = thread['root_id']
        except Exception:
            logger.debug("Error scanning Bitbucket PR #%s threads",
                         pr_id, exc_info=True)
            self._scan_state.reliable = False

        # An unreliable scan could have mis-counted in either direction -
        # reuse the last known-good count so a blip can't fire a phantom
        # "N unresponded comments" alert.
        if not getattr(self._scan_state, 'reliable', True) and prior is not None:
            unresponded = prior.unresponded_count
            first_note_id = prior.first_unresponded_note_id

        if unresponded > 0:
            first_url = (self.build_first_unresponded_url(pr_url, first_note_id)
                         if first_note_id is not None else None)
            result = PRStatus(
                state=PRState.UNRESPONDED,
                unresponded_count=unresponded,
                pr_url=pr_url, pr_title=pr_title, pr_iid=pr_id,
                first_unresponded_note_id=first_note_id,
                first_unresponded_url=first_url,
                approved=approved, approved_by=approved_by or None,
                self_approved=self_approved,
                draft=draft, has_conflicts=has_conflicts,
                changes_requested=changes_requested, checks_failed=checks_failed,
            )
        else:
            result = PRStatus(
                state=PRState.ALL_RESPONDED,
                pr_url=pr_url, pr_title=pr_title, pr_iid=pr_id,
                approved=approved, approved_by=approved_by or None,
                self_approved=self_approved,
                draft=draft, has_conflicts=has_conflicts,
                changes_requested=changes_requested, checks_failed=checks_failed,
            )

        self._status_cache[cache_key] = result
        return result

    def _server_pr_conflicted(self, parts: tuple[str, str],
                              pr_id: int) -> Optional[bool]:
        """Server-only: whether the PR has merge conflicts (None = unknown)."""
        try:
            data = self._get_json(f'{self._pr_resource(parts, pr_id)}/merge')
            return bool(data.get('conflicted', False))
        except Exception:
            logger.debug("Failed to fetch merge state for Bitbucket PR #%s",
                         pr_id)
            return None

    def _checks_failed(self, parts: tuple[str, str],
                       pr: dict[str, Any]) -> Optional[bool]:
        """Whether any CI build on the PR head reports FAILED (None = unknown).

        Mirrors the GitLab provider's failed-only semantics: in-progress or
        stopped builds don't light the marker.
        """
        try:
            if self._is_cloud:
                statuses = self._iter_pages(
                    f'{self._pr_resource(parts, pr["id"])}/statuses',
                    page_cap=2,
                )
                return any(s.get('state') == 'FAILED' for s in statuses)
            sha = (pr.get('fromRef') or {}).get('latestCommit')
            if not sha:
                return None
            data = self._get_json(f'{self._build_status_root}/commits/{sha}',
                                  params={'limit': 100})
            return any(v.get('state') == 'FAILED'
                       for v in data.get('values', []))
        except Exception:
            logger.debug("Failed to fetch CI status for Bitbucket PR #%s",
                         pr.get('id'))
            return None

    # ------------------------------------------------------------------
    # Comment threads (normalized shape shared by Cloud and Server)
    # ------------------------------------------------------------------

    def _fetch_threads(self, parts: tuple[str, str],
                       pr_id: int) -> list[dict[str, Any]]:
        """Fetch comment threads as normalized dicts (raises on failure).

        Each thread:
        ``{'root_id': int, 'resolved': bool, 'notes': [note...],
        'file_path': Optional[str], 'old_line': Optional[int],
        'new_line': Optional[int]}``
        where each note is ``{'id', 'author_user', 'author', 'body',
        'created_at'}`` ordered root-first.
        """
        if self._is_cloud:
            return self._fetch_threads_cloud(parts, pr_id)
        return self._fetch_threads_server(parts, pr_id)

    def _fetch_threads_cloud(self, parts: tuple[str, str],
                             pr_id: int) -> list[dict[str, Any]]:
        comments = self._iter_pages(
            f'{self._pr_resource(parts, pr_id)}/comments')
        # Drop deleted comments and unpublished review drafts.
        live = [c for c in comments
                if not c.get('deleted') and not c.get('pending')]
        by_id = {c['id']: c for c in live if 'id' in c}

        def _root_of(comment: dict[str, Any]) -> dict[str, Any]:
            seen: set[int] = set()
            while True:
                parent = comment.get('parent') or {}
                parent_id = parent.get('id')
                if parent_id is None or parent_id not in by_id:
                    return comment
                if comment.get('id') in seen:
                    return comment  # defensive: malformed parent cycle
                seen.add(comment.get('id'))
                comment = by_id[parent_id]

        grouped: dict[int, list[dict[str, Any]]] = {}
        for c in live:
            if 'id' not in c:
                continue
            grouped.setdefault(_root_of(c)['id'], []).append(c)

        threads: list[dict[str, Any]] = []
        for root_id, thread_comments in grouped.items():
            thread_comments.sort(key=lambda c: c.get('created_on') or '')
            root = by_id[root_id]
            inline = root.get('inline') or {}
            notes = [{
                'id': c.get('id'),
                'author_user': c.get('user') or {},
                'author': self._user_display(c.get('user')),
                'body': (c.get('content') or {}).get('raw', '') or '',
                'created_at': c.get('created_on', '') or '',
            } for c in thread_comments]
            threads.append({
                'root_id': root_id,
                'resolved': bool(root.get('resolution')),
                'notes': notes,
                'file_path': inline.get('path'),
                'old_line': inline.get('from'),
                'new_line': inline.get('to'),
            })
        threads.sort(key=lambda t: t['notes'][0]['created_at'] if t['notes'] else '')
        return threads

    def _fetch_threads_server(self, parts: tuple[str, str],
                              pr_id: int) -> list[dict[str, Any]]:
        activities = self._iter_pages(
            f'{self._pr_resource(parts, pr_id)}/activities')
        # One COMMENTED activity per root comment carries the whole reply
        # tree.  Re-edits can surface the same root in several activities -
        # key by comment id so each thread appears once.
        roots: dict[int, dict[str, Any]] = {}
        anchors: dict[int, dict[str, Any]] = {}
        for activity in activities:
            if activity.get('action') != 'COMMENTED':
                continue
            comment = activity.get('comment')
            if not isinstance(comment, dict) or 'id' not in comment:
                continue
            # Only true thread roots become threads.  Some Server versions
            # emit a COMMENTED activity per *reply* (commentAction REPLIED)
            # whose embedded comment carries a parent - treating those as
            # roots would double-count the thread (the reply already
            # appears inside its root's embedded tree).
            if comment.get('parent'):
                continue
            roots[comment['id']] = comment
            anchor = activity.get('commentAnchor')
            if isinstance(anchor, dict):
                anchors[comment['id']] = anchor

        def _flatten(comment: dict[str, Any],
                     out: list[dict[str, Any]]) -> None:
            out.append(comment)
            for child in comment.get('comments') or []:
                if isinstance(child, dict):
                    _flatten(child, out)

        threads: list[dict[str, Any]] = []
        for root_id, root in roots.items():
            flat: list[dict[str, Any]] = []
            _flatten(root, flat)
            # Depth-first tree order is NOT chronological when sub-replies
            # interleave (a reply to an early comment can postdate a later
            # top-level reply).  The unresponded rule reasons about "the
            # last comment by someone else", so re-sort by creation time;
            # the sort is stable and the root (earliest) stays first.
            flat.sort(key=lambda c: c.get('createdDate') or 0)
            # The diff anchor normally rides on the activity entry; newer
            # Servers also expose it on the comment itself - use that as
            # the fallback.
            anchor = anchors.get(root_id) or root.get('anchor') or {}
            line = anchor.get('line')
            removed = anchor.get('lineType') == 'REMOVED'
            notes = [{
                'id': c.get('id'),
                'author_user': c.get('author') or {},
                'author': self._user_display(c.get('author')),
                'body': c.get('text', '') or '',
                'created_at': str(c.get('createdDate', '') or ''),
            } for c in flat]
            threads.append({
                'root_id': root_id,
                # Two distinct resolution mechanisms: "Resolve thread" sets
                # threadResolved on the root; tasks/blocker comments resolve
                # with state RESOLVED.  Either one dismisses the thread.
                'resolved': (bool(root.get('threadResolved'))
                             or root.get('state') == 'RESOLVED'),
                'notes': notes,
                'file_path': anchor.get('path'),
                'old_line': line if removed else None,
                'new_line': None if removed else line,
            })
        threads.sort(key=lambda t: t['notes'][0]['created_at'] if t['notes'] else '')
        return threads

    def _is_unresponded_thread(self, thread: dict[str, Any]) -> bool:
        """Check if a thread has unresponded comments from others.

        Same rules as the GitLab provider, minus the emoji-reaction check
        (no public reactions API on either Bitbucket flavor):
        - the thread is not resolved
        - it has comments from someone other than the user
        - the last such comment is not followed by a reply from the user
          (a bare ``/leap`` does not count as a real reply)
        """
        if thread.get('resolved'):
            return False
        notes = thread.get('notes') or []
        human_notes = [
            n for n in notes
            if not self._filter_bots or not self._is_bot(n.get('author_user'))
        ]
        if not human_notes:
            return False

        last_other_idx = -1
        for i, note in enumerate(human_notes):
            if not self._is_me(note.get('author_user')):
                last_other_idx = i
        if last_other_idx < 0:
            return False  # only the user commented

        for note in human_notes[last_other_idx + 1:]:
            if (self._is_me(note.get('author_user'))
                    and note.get('body', '').strip() != '/leap'):
                return False
        return True

    # ------------------------------------------------------------------
    # /leap commands
    # ------------------------------------------------------------------

    def scan_leap_commands(self, project_path: str, branch: str,
                           pr_iid: Optional[int] = None) -> list[CqCommand]:
        """Scan open PRs for /leap commands from the configured user."""
        parts = self._split_project(project_path)
        if not parts:
            return []
        prs = self._find_open_prs(parts, branch, pr_iid)
        if not prs:
            return []

        commands: list[CqCommand] = []
        for pr in prs:
            try:
                threads = self._fetch_threads(parts, pr['id'])
            except Exception:
                logger.debug("Failed to fetch threads for Bitbucket PR #%s",
                             pr.get('id'))
                continue
            for thread in threads:
                cmd = self._check_thread_for_leap(project_path, parts, pr,
                                                  thread, branch)
                if cmd:
                    commands.append(cmd)
        return commands

    def _check_thread_for_leap(self, project_path: str,
                               parts: tuple[str, str], pr: dict[str, Any],
                               thread: dict[str, Any],
                               branch: str) -> Optional[CqCommand]:
        """Check a single thread for an un-acknowledged /leap trigger."""
        # Resolving a thread dismisses any pending /leap (same rule as the
        # GitLab/GitHub providers).
        if thread.get('resolved'):
            return None
        notes = thread.get('notes') or []
        if not notes:
            return None

        # An ack only covers /leap commands that appear before it.
        last_leap_index = -1
        last_ack_index = -1
        for i, note in enumerate(notes):
            body = note.get('body', '').strip()
            if body == '/leap' and self._is_me(note.get('author_user')):
                last_leap_index = i
            if LEAP_ACK_MESSAGE in note.get('body', ''):
                last_ack_index = i
        if last_leap_index < 0 or last_leap_index < last_ack_index:
            return None

        return self._build_leap_command_from_thread(project_path, parts, pr,
                                                    thread, branch)

    def _build_leap_command_from_thread(self, project_path: str,
                                        parts: tuple[str, str],
                                        pr: dict[str, Any],
                                        thread: dict[str, Any],
                                        branch: str) -> Optional[CqCommand]:
        notes = thread.get('notes') or []
        if not notes:
            return None
        thread_notes = [{
            'author': n.get('author', ''),
            'body': n.get('body', ''),
            'created_at': n.get('created_at', ''),
        } for n in notes]

        file_path = thread.get('file_path')
        old_line = thread.get('old_line')
        new_line = thread.get('new_line')
        code_snippet = None
        if file_path:
            code_snippet = self._fetch_code_snippet(
                parts, file_path, branch, new_line or old_line)

        return CqCommand(
            # MUST be the caller's verbatim project_path (not the normalized
            # KEY/slug form): _handle_leap_commands matches commands to
            # sessions by comparing this against the session's own path
            # (e.g. 'scm/proj/repo' from a Server HTTPS clone remote) - a
            # normalized value would fail to match the very session whose
            # scan produced the command.
            project_path=project_path,
            pr_iid=pr['id'],
            pr_title=pr.get('title', '') or '',
            pr_url=self._pr_web_url(pr),
            discussion_id=str(thread['root_id']),
            thread_notes=thread_notes,
            file_path=file_path,
            old_line=old_line,
            new_line=new_line,
            code_snippet=code_snippet,
            scm_type='bitbucket',
        )

    def _fetch_code_snippet(self, parts: tuple[str, str], file_path: str,
                            branch: str,
                            target_line: Optional[int]) -> Optional[str]:
        """Fetch ~5 lines of code around the target line."""
        if not target_line:
            return None
        try:
            encoded_path = quote(file_path, safe='/')
            if self._is_cloud:
                resp = self._request(
                    'GET',
                    f'{self._repo_api(parts)}/src/'
                    f'{quote(branch, safe="")}/{encoded_path}')
            else:
                resp = self._request(
                    'GET', f'{self._repo_api(parts)}/raw/{encoded_path}',
                    params={'at': f'refs/heads/{branch}'})
            resp.raise_for_status()
            lines = resp.text.splitlines()
            # Extract ~5 lines around target (2 before, target, 2 after)
            start = max(0, target_line - 3)
            end = min(len(lines), target_line + 2)
            return "\n".join(lines[start:end])
        except Exception:
            logger.debug("Failed to fetch code snippet for %s:%s",
                         file_path, target_line)
            return None

    # ------------------------------------------------------------------
    # Replies
    # ------------------------------------------------------------------

    def _post_reply(self, project_path: str, pr_iid: int,
                    discussion_id: str, message: str) -> bool:
        parts = self._split_project(project_path)
        if not parts:
            return False
        try:
            parent_id = int(discussion_id)
        except (TypeError, ValueError):
            logger.debug("Invalid Bitbucket discussion id: %r", discussion_id)
            return False
        try:
            if self._is_cloud:
                body: dict[str, Any] = {'content': {'raw': message},
                                        'parent': {'id': parent_id}}
            else:
                body = {'text': message, 'parent': {'id': parent_id}}
            resp = self._request(
                'POST', f'{self._pr_resource(parts, pr_iid)}/comments',
                json_body=body)
            resp.raise_for_status()
            return True
        except Exception:
            logger.debug("Failed to reply on Bitbucket PR #%s comment %s",
                         pr_iid, discussion_id, exc_info=True)
            return False

    def acknowledge_leap_command(self, project_path: str, pr_iid: int,
                                 discussion_id: str) -> bool:
        """Post '[Leap bot] on it!' reply to the comment thread."""
        return self._post_reply(project_path, pr_iid, discussion_id,
                                LEAP_ACK_MESSAGE)

    def report_no_session(self, project_path: str, pr_iid: int,
                          discussion_id: str) -> bool:
        """Post error reply when no matching Leap session is found."""
        return self._post_reply(project_path, pr_iid, discussion_id,
                                LEAP_NO_SESSION_MESSAGE)

    # ------------------------------------------------------------------
    # Collect unresponded threads
    # ------------------------------------------------------------------

    def collect_unresponded_threads(self, project_path: str, branch: str,
                                    pr_iid: Optional[int] = None) -> list[CqCommand]:
        """Collect all unresponded threads from a PR as CqCommand objects."""
        parts = self._split_project(project_path)
        if not parts:
            return []
        prs = self._find_open_prs(parts, branch, pr_iid)
        if not prs:
            return []
        pr = prs[0]
        try:
            threads = self._fetch_threads(parts, pr['id'])
        except Exception:
            logger.debug("Failed to fetch threads for Bitbucket PR #%s",
                         pr.get('id'))
            return []

        commands: list[CqCommand] = []
        for thread in threads:
            if not self._is_unresponded_thread(thread):
                continue
            cmd = self._build_leap_command_from_thread(project_path, parts,
                                                       pr, thread, branch)
            if cmd:
                commands.append(cmd)
        return commands

    # ------------------------------------------------------------------
    # Closed-PR lookup / details
    # ------------------------------------------------------------------

    def find_latest_closed_pr(self, project_path: str,
                              branch: str) -> Optional[ClosedPRInfo]:
        parts = self._split_project(project_path)
        if not parts:
            return None
        # Prefer merged over declined; within each state pick the most
        # recently updated (matches the GitLab provider's semantics).
        for state, merged in (('MERGED', True), ('DECLINED', False)):
            try:
                if self._is_cloud:
                    escaped = branch.replace('\\', '\\\\').replace('"', '\\"')
                    data = self._get_json(
                        self._pr_list_url(parts),
                        params={'q': f'source.branch.name = "{escaped}" '
                                     f'AND state = "{state}"',
                                'sort': '-updated_on', 'pagelen': 1})
                else:
                    data = self._get_json(
                        self._pr_list_url(parts),
                        params={'state': state, 'direction': 'OUTGOING',
                                'at': f'refs/heads/{branch}',
                                'order': 'NEWEST', 'limit': 1})
                values = data.get('values', [])
            except Exception:
                logger.debug("Failed to list %s Bitbucket PRs for %s branch %s",
                             state, project_path, branch)
                continue
            if values:
                pr = values[0]
                return ClosedPRInfo(
                    pr_iid=pr['id'],
                    pr_title=pr.get('title', '') or '',
                    pr_url=self._pr_web_url(pr),
                    merged=merged,
                )
        return None

    def get_pr_details(self, project_path: str, pr_iid: int) -> Optional[PRDetails]:
        parts = self._split_project(project_path)
        if not parts:
            return None
        try:
            pr = self._get_json(self._pr_resource(parts, pr_iid))
        except Exception:
            logger.debug("Failed to fetch Bitbucket PR #%s in %s",
                         pr_iid, project_path)
            return None
        try:
            if self._is_cloud:
                source = pr.get('source') or {}
                source_branch = (source.get('branch') or {}).get('name', '') or ''
                src_repo = (source.get('repository') or {}).get('full_name')
            else:
                from_ref = pr.get('fromRef') or {}
                source_branch = from_ref.get('displayId', '') or ''
                repo = from_ref.get('repository') or {}
                key = (repo.get('project') or {}).get('key', '')
                slug = repo.get('slug', '')
                src_repo = f'{key}/{slug}' if key and slug else None
            branch_deleted = self._source_branch_deleted(src_repo, source_branch)
            return PRDetails(
                source_branch=source_branch,
                pr_title=pr.get('title', '') or '',
                pr_url=self._pr_web_url(pr),
                source_branch_deleted=branch_deleted,
            )
        except Exception:
            logger.debug("Failed to parse Bitbucket PR #%s details", pr_iid,
                         exc_info=True)
            return None

    def _source_branch_deleted(self, src_repo: Optional[str],
                               branch: str) -> bool:
        """Whether the PR's source branch no longer exists (best-effort)."""
        if not src_repo or not branch:
            # Cloud reports a null source repository when a fork was
            # deleted - the branch is definitely gone then.
            return bool(branch) and not src_repo
        parts = self._split_project(src_repo)
        if not parts:
            return False
        try:
            if self._is_cloud:
                resp = self._request(
                    'GET',
                    f'{self._repo_api(parts)}/refs/branches/'
                    f'{quote(branch, safe="")}')
                if resp.status_code == 404:
                    return True
                return False
            data = self._get_json(f'{self._repo_api(parts)}/branches',
                                  params={'filterText': branch, 'limit': 100})
            return not any(v.get('displayId') == branch
                           for v in data.get('values', []))
        except Exception:
            logger.debug("Failed to check Bitbucket branch existence: %s",
                         branch)
            return False

    # ------------------------------------------------------------------
    # Notifications (Server-only)
    # ------------------------------------------------------------------

    def supports_notifications(self) -> bool:
        # Bitbucket Cloud has no notifications/inbox API; Server exposes
        # the reviewer dashboard.
        return not self._is_cloud

    def get_user_notifications(self) -> list[UserNotification]:
        """Server-only: open PRs awaiting the user's review."""
        if self._is_cloud:
            return []
        try:
            # participantStatus filters by MY review status server-side, so
            # already-approved PRs never enter the payload (the loop below
            # re-checks as a backstop).
            resp = self._request(
                'GET', f'{self._api_root}/dashboard/pull-requests',
                params={'role': 'REVIEWER', 'state': 'OPEN',
                        'participantStatus': 'UNAPPROVED,NEEDS_WORK',
                        'limit': 50})
        except Exception:
            logger.debug("Failed to fetch Bitbucket reviewer dashboard",
                         exc_info=True)
            return []
        if resp.status_code == 403:
            # Propagate as an auth error so the poll worker can detect it
            # and auto-disable notification tracking.  Deliberately 403-only
            # (matching the GitLab provider): a 401 can be transient (token
            # mid-rotation) and must not permanently flip
            # enable_notifications off on disk.
            raise BitbucketAuthError('HTTP 403 from Bitbucket dashboard')
        if resp.status_code != 200:
            logger.debug("Bitbucket dashboard returned HTTP %s",
                         resp.status_code)
            return []
        try:
            values = resp.json().get('values', [])
        except ValueError:
            return []

        notifications: list[UserNotification] = []
        for pr in values:
            author = (pr.get('author') or {}).get('user') or {}
            author_name = author.get('name', '') or ''
            # Skip self-authored PRs (mirrors the GitLab todos behavior)
            if author_name and author_name == self._username:
                continue
            # Skip PRs the user already approved - their review is done.
            already_approved = False
            for reviewer in pr.get('reviewers') or []:
                if (self._is_me(reviewer.get('user') or {})
                        and reviewer.get('approved')):
                    already_approved = True
                    break
            if already_approved:
                continue
            repo = (pr.get('toRef') or {}).get('repository') or {}
            key = (repo.get('project') or {}).get('key', '')
            slug = repo.get('slug', '')
            project_name = f'{key}/{slug}' if key and slug else ''
            notifications.append(UserNotification(
                id=f'{project_name}#{pr.get("id")}',
                scm_type='bitbucket',
                reason='review_requested',
                title=pr.get('title', '') or '',
                target_url=self._pr_web_url(pr),
                project_name=project_name,
                author=author_name,
                created_at=str(pr.get('createdDate', '') or ''),
            ))
        return notifications

    # ------------------------------------------------------------------
    # Deep links
    # ------------------------------------------------------------------

    def build_first_unresponded_url(self, pr_url: str, comment_id: int,
                                    origin: str = 'r') -> str:
        """Cloud anchors comments with ``#comment-<id>``; Server uses the
        ``?commentId=<id>`` query on the overview page."""
        del origin
        if self._is_cloud:
            return f'{pr_url}#comment-{comment_id}'
        base = pr_url.rstrip('/')
        if re.search(r'/overview$', base):
            return f'{base}?commentId={comment_id}'
        return f'{base}/overview?commentId={comment_id}'
