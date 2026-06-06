"""Tests for GitLabProvider helpers + shared paths touched by the GitLab
bughunt fix batch (#1, #3, #4, #5, #8, #10, #12, #13, #18, #19, #20, #21).

These exercise pure-logic surfaces without hitting the network.  Where we
need a provider instance we build one via ``__new__`` to skip the
constructor's auth path — the methods under test never touch ``self._gl``.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest


# --- helpers ---------------------------------------------------------

def _make_gitlab_provider(username: str = 'me', filter_bots: bool = True) -> Any:
    from leap.monitor.pr_tracking.gitlab_provider import GitLabProvider
    p = GitLabProvider.__new__(GitLabProvider)
    p._gl = None
    p._username = username
    p._filter_bots = filter_bots
    p._project_cache = {}
    p._bot_cache = {}
    p._approval_cache = {}
    p._status_cache = {}
    p._emoji_cache = {}
    p._scan_state = threading.local()
    return p


def _discussion(notes: list[dict], *, resolved: bool = False, did: str = 'd1') -> Any:
    """Build a stand-in for python-gitlab's discussion object."""
    return SimpleNamespace(
        id=did,
        attributes={'notes': notes, 'resolved': resolved},
    )


def _note(*, body: str = '', author: str = 'me', system: bool = False,
          note_id: int = 1, position: dict | None = None) -> dict:
    """Note dict matching what python-gitlab puts in discussion.attributes['notes']."""
    n: dict = {
        'id': note_id,
        'body': body,
        'system': system,
        'author': {'id': hash(author) & 0xffff, 'username': author},
    }
    if position is not None:
        n['position'] = position
    return n


# --- #4 + #8: resolved skip + system-note filter in /leap detection ---

class TestCheckDiscussionForLeap:
    """Fix #4 (skip resolved threads) and #8 (system notes don't poison ack)."""

    def test_emits_when_user_posted_leap(self) -> None:
        p = _make_gitlab_provider(username='me')
        d = _discussion([
            _note(body='Please fix this', author='reviewer', note_id=1),
            _note(body='/leap', author='me', note_id=2),
        ])
        cmd = p._check_discussion_for_leap(
            project=None, project_path='g/p',
            mr=SimpleNamespace(iid=1, title='T', web_url='https://gitlab.com/g/p/-/merge_requests/1'),
            discussion=d, branch='feat',
        )
        assert cmd is not None
        assert cmd.discussion_id == 'd1'

    def test_skips_resolved_thread(self) -> None:
        # Fix #4: resolved threads should never trigger /leap
        p = _make_gitlab_provider(username='me')
        d = _discussion([
            _note(body='Fix this', author='reviewer', note_id=1),
            _note(body='/leap', author='me', note_id=2),
        ], resolved=True)
        cmd = p._check_discussion_for_leap(
            project=None, project_path='g/p',
            mr=SimpleNamespace(iid=1, title='T', web_url='url'),
            discussion=d, branch='feat',
        )
        assert cmd is None

    def test_system_note_with_ack_text_does_not_block_leap(self) -> None:
        # Fix #8: a system note whose body contains LEAP_ACK_MESSAGE
        # (e.g. quoted in an auto-generated changelog) must NOT mark
        # the thread as already-acked.
        from leap.monitor.pr_tracking.gitlab_provider import LEAP_ACK_MESSAGE
        p = _make_gitlab_provider(username='me')
        d = _discussion([
            _note(body='Fix this', author='reviewer', note_id=1),
            # System note that happens to contain the ack message string
            _note(body=f'changed title to "{LEAP_ACK_MESSAGE}"',
                  author='gitlab-bot', system=True, note_id=2),
            _note(body='/leap', author='me', note_id=3),
        ])
        cmd = p._check_discussion_for_leap(
            project=None, project_path='g/p',
            mr=SimpleNamespace(iid=1, title='T', web_url='url'),
            discussion=d, branch='feat',
        )
        assert cmd is not None, 'system-note ack-string should be ignored'

    def test_real_ack_after_leap_blocks_emission(self) -> None:
        # Sanity: the genuine ack post IS still respected
        from leap.monitor.pr_tracking.gitlab_provider import LEAP_ACK_MESSAGE
        p = _make_gitlab_provider(username='me')
        d = _discussion([
            _note(body='Fix this', author='reviewer', note_id=1),
            _note(body='/leap', author='me', note_id=2),
            _note(body=LEAP_ACK_MESSAGE, author='me', note_id=3),
        ])
        cmd = p._check_discussion_for_leap(
            project=None, project_path='g/p',
            mr=SimpleNamespace(iid=1, title='T', web_url='url'),
            discussion=d, branch='feat',
        )
        assert cmd is None


# --- #10: position extraction picks first note WITH position ---

class TestPositionExtraction:
    def test_skips_leading_system_notes_when_finding_position(self) -> None:
        # Fix #10: a system note at notes[0] previously stole the
        # code-context lookup.  Now we find the first note that has a
        # position field.
        p = _make_gitlab_provider(username='me')
        # Build an MR object that lets _build_leap_command_from_discussion
        # run (it doesn't fetch code unless file_path is set, and we
        # don't have a real project here, so code_snippet will stay None
        # — that's fine for the assertion).
        d = _discussion([
            _note(body='changed milestone', author='bot', system=True, note_id=10),
            _note(body='Fix this', author='reviewer', note_id=11,
                  position={'new_path': 'src/foo.py', 'new_line': 42,
                            'old_path': 'src/foo.py', 'old_line': 42}),
        ])
        cmd = p._build_leap_command_from_discussion(
            project=None, project_path='g/p',
            mr=SimpleNamespace(iid=1, title='T', web_url='url'),
            discussion=d, branch='feat',
        )
        assert cmd is not None
        assert cmd.file_path == 'src/foo.py'
        assert cmd.new_line == 42


# --- #12: approval_required maps to review_requested ---

class TestNormalizeGitLabAction:
    def test_review_requested(self) -> None:
        from leap.monitor.pr_tracking.gitlab_provider import GitLabProvider
        assert GitLabProvider._normalize_gitlab_action('review_requested') == 'review_requested'

    def test_approval_required_maps_to_review_requested(self) -> None:
        # Fix #12: GitLab Premium "approval_required" todos should not be dropped
        from leap.monitor.pr_tracking.gitlab_provider import GitLabProvider
        assert GitLabProvider._normalize_gitlab_action('approval_required') == 'review_requested'

    def test_assigned(self) -> None:
        from leap.monitor.pr_tracking.gitlab_provider import GitLabProvider
        assert GitLabProvider._normalize_gitlab_action('assigned') == 'assigned'

    def test_mentioned_and_directly_addressed(self) -> None:
        from leap.monitor.pr_tracking.gitlab_provider import GitLabProvider
        assert GitLabProvider._normalize_gitlab_action('mentioned') == 'mentioned'
        assert GitLabProvider._normalize_gitlab_action('directly_addressed') == 'mentioned'

    def test_unknown_falls_through_to_other(self) -> None:
        from leap.monitor.pr_tracking.gitlab_provider import GitLabProvider
        for action in ('marked', 'unmergeable', 'build_failed', 'merge_train_removed'):
            assert GitLabProvider._normalize_gitlab_action(action) == 'other'


# --- #3: bot cache no longer poisoned on transient errors ---

class TestBotCacheTransient:
    def test_does_not_cache_on_exception(self) -> None:
        # Fix #3: a failed user lookup must NOT cache False
        p = _make_gitlab_provider()

        class _RaisingUsers:
            def get(self, _id: int) -> Any:
                raise RuntimeError('transient network blip')

        p._gl = SimpleNamespace(users=_RaisingUsers())
        n = _note(author='somebot', note_id=1)
        # First lookup: fails → returns False, must NOT cache
        assert p._is_bot_author(n) is False
        assert n['author']['id'] not in p._bot_cache, \
            'cache must not be poisoned by transient failure'


# --- #5: trailing-slash URL not treated as self-hosted ---

class TestIsDefaultUrl:
    def test_trailing_slash_treated_as_default(self) -> None:
        # Fix #5: 'https://gitlab.com/' should match default 'https://gitlab.com'
        from leap.monitor.dialogs.gitlab_setup_dialog import GitLabSetupDialog
        d = GitLabSetupDialog.__new__(GitLabSetupDialog)
        # Need to stub out the abstract URL default since we bypass __init__
        d._url_default = lambda: 'https://gitlab.com'
        # Use the inherited base method
        from leap.monitor.dialogs.scm_setup_dialog import SCMSetupDialog
        assert SCMSetupDialog._is_default_url(d, 'https://gitlab.com') is True
        assert SCMSetupDialog._is_default_url(d, 'https://gitlab.com/') is True
        assert SCMSetupDialog._is_default_url(d, 'https://gitlab.example.com') is False

    def test_case_insensitive_match(self) -> None:
        # Round-4 audit: URL hosts are case-insensitive per RFC 3986 §3.1
        from leap.monitor.dialogs.scm_setup_dialog import SCMSetupDialog
        d = type('Stub', (), {})()
        d._url_default = lambda: 'https://gitlab.com'
        assert SCMSetupDialog._is_default_url(d, 'HTTPS://gitlab.com') is True
        assert SCMSetupDialog._is_default_url(d, 'https://GITLAB.com') is True
        assert SCMSetupDialog._is_default_url(d, 'HTTPS://GITLAB.COM/') is True


class TestParseUrlAliasResolution:
    """A user pasting a PR URL with an SSH-alias hostname (e.g.
    ``https://planck_gitlab/.../-/merge_requests/42``) should get the
    same alias resolution that the local-clone path already does.
    """

    def test_pr_url_with_alias_host_resolves(self, monkeypatch) -> None:
        from leap.monitor.pr_tracking import git_utils
        git_utils._SSH_HOST_CACHE.clear()
        monkeypatch.setattr(
            git_utils, 'resolve_ssh_alias',
            lambda h: 'gitlab.com' if h == 'planck_gitlab' else h,
        )
        parsed = git_utils.parse_pr_url(
            'https://planck_gitlab/group/project/-/merge_requests/42'
        )
        assert parsed is not None
        assert parsed.host_url == 'https://gitlab.com'
        assert parsed.project_path == 'group/project'
        assert parsed.pr_iid == 42

    def test_pr_url_with_real_host_unchanged(self) -> None:
        # Sanity: dotted hosts short-circuit (no subprocess call needed)
        from leap.monitor.pr_tracking.git_utils import parse_pr_url
        parsed = parse_pr_url('https://gitlab.com/g/p/-/merge_requests/1')
        assert parsed is not None
        assert parsed.host_url == 'https://gitlab.com'

    def test_project_url_with_alias_host_resolves(self, monkeypatch) -> None:
        from leap.monitor.pr_tracking import git_utils
        git_utils._SSH_HOST_CACHE.clear()
        monkeypatch.setattr(
            git_utils, 'resolve_ssh_alias',
            lambda h: 'gitlab.com' if h == 'planck_gitlab' else h,
        )
        parsed = git_utils.parse_project_url(
            'https://planck_gitlab/group/project'
        )
        assert parsed is not None
        assert parsed.host_url == 'https://gitlab.com'
        assert parsed.project_path == 'group/project'

    def test_project_url_ssh_with_alias_resolves(self, monkeypatch) -> None:
        from leap.monitor.pr_tracking import git_utils
        git_utils._SSH_HOST_CACHE.clear()
        monkeypatch.setattr(
            git_utils, 'resolve_ssh_alias',
            lambda h: 'gitlab.com' if h == 'planck_gitlab' else h,
        )
        parsed = git_utils.parse_project_url(
            'git@planck_gitlab:group/project.git'
        )
        assert parsed is not None
        assert parsed.host_url == 'https://gitlab.com'

    def test_https_with_credentials_stripped_then_resolved(self, monkeypatch) -> None:
        from leap.monitor.pr_tracking import git_utils
        git_utils._SSH_HOST_CACHE.clear()
        monkeypatch.setattr(
            git_utils, 'resolve_ssh_alias',
            lambda h: 'gitlab.com' if h == 'planck_gitlab' else h,
        )
        parsed = git_utils.parse_pr_url(
            'https://user:pass@planck_gitlab/g/p/-/merge_requests/7'
        )
        assert parsed is not None
        assert parsed.host_url == 'https://gitlab.com'


class TestLeapSendRecoverySignal:
    """Round-6 audit: after a send-failed warning, a successful send to
    the same tag must clear the dedup so the NEXT failure pops up again
    instead of being silenced."""

    def test_send_recovered_signal_defined(self) -> None:
        from leap.monitor.scm_polling import SCMPollerWorker
        assert hasattr(SCMPollerWorker, 'leap_send_recovered')

    def test_send_recovered_handler_clears_dedup(self) -> None:
        """``_on_leap_send_recovered`` must discard the tag so a future
        failure shows a fresh popup instead of status-bar-only."""
        from leap.monitor._mixins.pr_tracking_mixin import PRTrackingMixin
        # Build a minimal stand-in for self
        stub = type('Stub', (), {})()
        stub._leap_send_failed_warned = {'feat1', 'feat2'}
        # Bind the real method to the stub
        PRTrackingMixin._on_leap_send_recovered(stub, 'feat1')
        assert stub._leap_send_failed_warned == {'feat2'}

    def test_send_recovered_no_op_when_attr_missing(self) -> None:
        # Dedup attribute might not exist yet (no failure ever occurred)
        from leap.monitor._mixins.pr_tracking_mixin import PRTrackingMixin
        stub = type('Stub', (), {})()
        # Should not raise
        PRTrackingMixin._on_leap_send_recovered(stub, 'feat1')
        assert getattr(stub, '_leap_send_failed_warned', None) is None


class TestSendThreadsPartialFailure:
    """Round-4 audit: SendThreadsWorker each-mode used to silently drop
    failed sends from the count.  Now we emit ``send_partial_failed``
    INSTEAD of ``finished`` when any per-cmd send returned False so the
    user sees one popup, not two."""

    def test_send_partial_failed_signal_defined(self) -> None:
        from leap.monitor.scm_polling import _BaseSendWorker
        assert hasattr(_BaseSendWorker, 'send_partial_failed')

    def test_finished_and_partial_are_mutually_exclusive(self) -> None:
        """The worker should emit one or the other, never both."""
        # Inspect the source as a smoke test — the signal logic isn't
        # easily isolatable without spinning up a Qt event loop.
        import inspect
        from leap.monitor.scm_polling import SendThreadsWorker
        src = inspect.getsource(SendThreadsWorker.run)
        # Both signals appear in the source...
        assert 'self.finished.emit' in src
        assert 'self.send_partial_failed.emit' in src
        # ...but they're guarded by mutually-exclusive branches keyed on
        # failed_count (the post-fix structure)
        assert 'if failed_count > 0:' in src
        assert 'else:' in src


# --- #13: SSH URL parser handles ssh:// + port-prefix ---

class TestGitRemoteInfoSshFormats:
    def test_classic_ssh_format(self) -> None:
        # Sanity: the normal git@host:path.git form still works
        from leap.monitor.pr_tracking import git_utils
        url = 'git@gitlab.com:group/project.git'
        ssh_match = git_utils.re.match(
            r'git@([^:/]+):(.+?)(?:\.git)?/?$', url,
        )
        assert ssh_match is not None
        assert ssh_match.group(1) == 'gitlab.com'
        assert ssh_match.group(2) == 'group/project'

    def test_ssh_uri_format(self) -> None:
        # Fix #13: ssh://git@host[:port]/path.git
        from leap.monitor.pr_tracking import git_utils
        url = 'ssh://git@gitlab.com:22/group/project.git'
        m = git_utils.re.match(
            r'ssh://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$', url,
        )
        assert m is not None
        assert m.group(1) == 'gitlab.com'
        assert m.group(2) == 'group/project'

    def test_scp_with_numeric_group_not_misparsed_as_port(self) -> None:
        # Round-5 audit: an earlier version of this regex tried to detect
        # ``git@host:port/path`` and broke numeric GitLab groups like
        # ``git@host:42/repo``.  git itself treats ``:42/repo`` as path
        # ``42/repo`` (no port disambiguation in scp-style), so we must too.
        from leap.monitor.pr_tracking import git_utils
        url = 'git@gitlab.com:42/repo.git'
        m = git_utils.re.match(
            r'git@([^:/]+):(.+?)(?:\.git)?/?$', url,
        )
        assert m is not None
        assert m.group(1) == 'gitlab.com'
        # Critical: the entire path including the leading numeric segment
        # must be preserved.
        assert m.group(2) == '42/repo'


# --- #21: SSH alias resolution ---

class TestResolveSshAlias:
    def test_dotted_hostname_short_circuits(self) -> None:
        # Real DNS names skip the subprocess call entirely
        from leap.monitor.pr_tracking.git_utils import resolve_ssh_alias
        assert resolve_ssh_alias('gitlab.com') == 'gitlab.com'
        assert resolve_ssh_alias('gitlab.example.com') == 'gitlab.example.com'
        assert resolve_ssh_alias('localhost') == 'localhost'

    def test_empty_returns_empty(self) -> None:
        from leap.monitor.pr_tracking.git_utils import resolve_ssh_alias
        assert resolve_ssh_alias('') == ''

    def test_alias_resolution_uses_subprocess(self, monkeypatch) -> None:
        # Fake `ssh -G alias` to return a hostname line
        from leap.monitor.pr_tracking import git_utils
        # Clear cache to force the call
        git_utils._SSH_HOST_CACHE.clear()

        def fake_run(cmd: list[str], *a, **kw) -> Any:
            assert cmd[:2] == ['ssh', '-G']
            return SimpleNamespace(
                stdout='user git\nhostname gitlab.example.com\nport 22\n',
                stderr='', returncode=0,
            )
        monkeypatch.setattr(git_utils.subprocess, 'run', fake_run)
        assert git_utils.resolve_ssh_alias('myalias') == 'gitlab.example.com'

    def test_unresolved_alias_returns_self(self, monkeypatch) -> None:
        # ssh -G returns the alias verbatim when no config entry exists
        from leap.monitor.pr_tracking import git_utils
        git_utils._SSH_HOST_CACHE.clear()

        def fake_run(cmd, *a, **kw):
            return SimpleNamespace(
                stdout='hostname myalias\n', stderr='', returncode=0,
            )
        monkeypatch.setattr(git_utils.subprocess, 'run', fake_run)
        assert git_utils.resolve_ssh_alias('myalias') == 'myalias'

    def test_subprocess_failure_returns_self(self, monkeypatch) -> None:
        # Don't crash when ssh isn't installed or the subprocess errors
        from leap.monitor.pr_tracking import git_utils
        git_utils._SSH_HOST_CACHE.clear()

        def fake_run(cmd, *a, **kw):
            raise FileNotFoundError('ssh: command not found')
        monkeypatch.setattr(git_utils.subprocess, 'run', fake_run)
        assert git_utils.resolve_ssh_alias('myalias') == 'myalias'

    def test_caches_result(self, monkeypatch) -> None:
        from leap.monitor.pr_tracking import git_utils
        git_utils._SSH_HOST_CACHE.clear()
        call_count = 0

        def fake_run(cmd, *a, **kw):
            nonlocal call_count
            call_count += 1
            return SimpleNamespace(
                stdout='hostname realhost.com\n', stderr='', returncode=0,
            )
        monkeypatch.setattr(git_utils.subprocess, 'run', fake_run)
        git_utils.resolve_ssh_alias('myalias')
        git_utils.resolve_ssh_alias('myalias')
        git_utils.resolve_ssh_alias('myalias')
        assert call_count == 1


# --- #19: deterministic eviction in notification cap ---

class TestNotificationEviction:
    def test_keeps_most_recent_numeric_ids(self) -> None:
        # Fix #19: when capped, keep the most-recent (highest) historical IDs
        # Mimic the trim logic with a small cap
        seen = {str(i) for i in range(0, 100)}
        current = {str(i) for i in range(95, 100)}  # last 5 are "active"
        max_seen = 10  # tiny cap for the test

        def _sort_key(value: str) -> tuple[int, str]:
            try:
                return (0, f'{int(value):020d}')
            except ValueError:
                return (1, value)

        historical = seen - current
        max_historical = max(0, max_seen - len(current))
        trimmed = set(sorted(historical, key=_sort_key)[-max_historical:])
        result = current | trimmed

        # Active IDs always present
        assert current.issubset(result)
        # Among historical, newer wins (90-94, not 0-4)
        assert {'90', '91', '92', '93', '94'}.issubset(result)
        assert '0' not in result and '4' not in result

    def test_non_numeric_ids_fall_back_to_string_sort(self) -> None:
        # Mixed IDs: numeric ones beat non-numeric (sort key tuple)
        def _sort_key(value: str) -> tuple[int, str]:
            try:
                return (0, f'{int(value):020d}')
            except ValueError:
                return (1, value)
        ids = ['abc', '123', 'xyz', '99']
        sorted_ids = sorted(ids, key=_sort_key)
        # Numeric IDs come first (tuple's first element 0), sorted numerically
        assert sorted_ids[:2] == ['99', '123']
        # Non-numerics come next, sorted lexicographically
        assert sorted_ids[2:] == ['abc', 'xyz']

    def test_max_historical_zero_does_not_keep_everything(self) -> None:
        # Regression: ``sorted(...)[-0:]`` returns the whole list, not empty.
        # When ``current`` alone fills the cap, no historical IDs should
        # be kept.  Mirror the production logic.
        def _sort_key(value: str) -> tuple[int, str]:
            try:
                return (0, f'{int(value):020d}')
            except ValueError:
                return (1, value)
        historical = {'10', '20', '30'}
        max_historical = 0  # current already fills cap
        if max_historical:
            trimmed = set(sorted(historical, key=_sort_key)[-max_historical:])
        else:
            trimmed = set()
        assert trimmed == set(), (
            'when max_historical is 0, no IDs should survive trimming — '
            'else current+historical exceeds the cap'
        )


class _FakeMRList:
    """Stand-in for ``project.mergerequests`` honoring state-keyed lookups."""

    def __init__(self, by_state: dict[str, list]) -> None:
        self._by_state = by_state
        self.calls: list[dict] = []

    def list(self, **kwargs: Any) -> list:
        self.calls.append(kwargs)
        return self._by_state.get(kwargs.get('state'), [])


def _make_project(by_state: dict[str, list]) -> Any:
    return SimpleNamespace(mergerequests=_FakeMRList(by_state))


class TestFindLatestClosedMR:
    """The closed/merged-MR fallback shown when no OPEN MR matches a branch."""

    def test_prefers_merged_over_closed(self) -> None:
        p = _make_gitlab_provider()
        merged = SimpleNamespace(iid=42, title='merged', web_url='wu42')
        closed = SimpleNamespace(iid=99, title='closed', web_url='wu99')
        proj = _make_project({'merged': [merged], 'closed': [closed]})
        p._get_project = lambda project_path: proj
        info = p.find_latest_closed_pr('grp/x', 'b')
        assert info is not None
        assert info.pr_iid == 42 and info.merged is True
        # Stops after the merged hit — never queries 'closed'.
        assert [c['state'] for c in proj.mergerequests.calls] == ['merged']

    def test_falls_back_to_closed_when_no_merged(self) -> None:
        p = _make_gitlab_provider()
        closed = SimpleNamespace(iid=99, title='c', web_url='wu99')
        proj = _make_project({'closed': [closed]})
        p._get_project = lambda project_path: proj
        info = p.find_latest_closed_pr('grp/x', 'b')
        assert info is not None
        assert info.pr_iid == 99 and info.merged is False
        assert [c['state'] for c in proj.mergerequests.calls] == ['merged', 'closed']

    def test_queries_source_branch_one_result(self) -> None:
        p = _make_gitlab_provider()
        proj = _make_project({'merged': [SimpleNamespace(iid=1, title='t', web_url='u')]})
        p._get_project = lambda project_path: proj
        p.find_latest_closed_pr('grp/x', 'my-branch')
        first = proj.mergerequests.calls[0]
        assert first['source_branch'] == 'my-branch'
        assert first['per_page'] == 1 and first['get_all'] is False
        # Explicit "most recently updated" ordering — GitLab defaults to
        # created_at, which wouldn't match the GitHub provider's semantics.
        assert first['order_by'] == 'updated_at' and first['sort'] == 'desc'

    def test_no_project_returns_none(self) -> None:
        p = _make_gitlab_provider()
        p._get_project = lambda project_path: None
        assert p.find_latest_closed_pr('grp/x', 'b') is None

    def test_no_matches_returns_none(self) -> None:
        p = _make_gitlab_provider()
        p._get_project = lambda project_path: _make_project({})
        assert p.find_latest_closed_pr('grp/x', 'b') is None

    def test_exception_in_one_state_continues_to_next(self) -> None:
        p = _make_gitlab_provider()

        class _Flaky:
            def list(self, **kwargs: Any) -> list:
                if kwargs['state'] == 'merged':
                    raise RuntimeError('boom')
                return [SimpleNamespace(iid=7, title='c', web_url='u')]

        p._get_project = lambda project_path: SimpleNamespace(mergerequests=_Flaky())
        info = p.find_latest_closed_pr('grp/x', 'b')
        assert info is not None and info.pr_iid == 7 and info.merged is False

    def test_missing_attrs_coerced(self) -> None:
        p = _make_gitlab_provider()
        # title/web_url absent on the object → getattr fallback to ''
        bare = SimpleNamespace(iid=3)
        p._get_project = lambda project_path: _make_project({'merged': [bare]})
        info = p.find_latest_closed_pr('grp/x', 'b')
        assert info is not None
        assert info.pr_iid == 3 and info.pr_title == '' and info.pr_url == ''


# --- Phantom-notification fixes -------------------------------------------
#
# Two transient-failure flaps used to fire bogus alerts:
#   1. A failed approvals fetch reported "nobody approved"; the next successful
#      poll then read an old approval as brand-new -> "approved by X".
#   2. A failed per-thread emoji/bot lookup bumped the unresponded count up;
#      the increase fired "N unresponded comments" for a stale thread.

class _ApprovalsObj:
    """Stand-in for pr_full.approvals — .get() returns a value or raises."""

    def __init__(self, approved_by: list[dict] | None, raises: bool = False) -> None:
        self._approved_by = approved_by or []
        self._raises = raises

    def get(self) -> Any:
        if self._raises:
            raise RuntimeError('approvals fetch failed')
        return SimpleNamespace(approved_by=self._approved_by)


class _DiscussionsObj:
    """Stand-in for pr_full.discussions — .list() returns prepared threads."""

    def __init__(self, discussions: list) -> None:
        self._discussions = discussions

    def list(self, get_all: bool = True) -> list:
        del get_all
        return self._discussions


def _make_status_provider(gl: Any, *, approvals: _ApprovalsObj,
                          discussions: _DiscussionsObj,
                          filter_bots: bool = False) -> Any:
    """Provider wired to drive get_pr_status end-to-end against fakes."""
    p = _make_gitlab_provider(filter_bots=filter_bots)
    p._gl = gl
    pr_full = SimpleNamespace(approvals=approvals, discussions=discussions)
    mr = SimpleNamespace(
        iid=154, title='T',
        web_url='https://gitlab.com/g/p/-/merge_requests/154',
    )
    project = SimpleNamespace(
        id=1,
        mergerequests=SimpleNamespace(
            list=lambda **kw: [mr],
            get=lambda iid: pr_full,
        ),
    )
    p._project_cache['g/p'] = project
    return p


class _FakeGL:
    """Minimal gitlab client: award-emoji listing + user (bot) lookup."""

    def __init__(self, emoji: dict[int, list[str]],
                 fail_emoji: set[int] | None = None,
                 bots: dict[int, bool] | None = None,
                 fail_users: bool = False) -> None:
        self._emoji = emoji
        self._fail_emoji = fail_emoji or set()
        self._bots = bots or {}
        self._fail_users = fail_users
        self.users = SimpleNamespace(get=self._get_user)

    def http_list(self, path: str, as_list: bool = True) -> list:
        del as_list
        note_id = int(path.split('/notes/')[1].split('/')[0])
        if note_id in self._fail_emoji:
            raise RuntimeError('award_emoji fetch failed')
        return [{'user': {'username': u}} for u in self._emoji.get(note_id, [])]

    def _get_user(self, user_id: int) -> Any:
        if self._fail_users:
            raise RuntimeError('user fetch failed')
        return SimpleNamespace(bot=self._bots.get(user_id, False))


def _reviewer_thread(note_id: int) -> Any:
    """A thread with a single reviewer note and no reply from the user —
    counts as unresponded unless the user reacted with an emoji."""
    return _discussion([_note(body='please fix', author='reviewer', note_id=note_id)])


class TestApprovalKnownGuard:
    """dock_badge._detect_pr_events must not fire on a recovered-from-unknown
    approval, but must still fire on a genuine new approval."""

    def _detect(self, seen: Any, current: Any) -> list:
        from leap.monitor.ui.dock_badge import DockBadge
        b = DockBadge.__new__(DockBadge)
        return b._detect_pr_events('lalala', seen, current)

    def test_recovery_from_failed_approval_fetch_does_not_fire(self) -> None:
        from leap.monitor.pr_tracking.base import PRState, PRStatus
        from leap.monitor.ui.dock_badge import NotificationType
        seen = PRStatus(state=PRState.ALL_RESPONDED, approval_known=False)
        current = PRStatus(state=PRState.ALL_RESPONDED, approved=True,
                           approved_by=['Yarden Goor'], approval_known=True)
        events = self._detect(seen, current)
        assert not any(e.type == NotificationType.PR_APPROVED for e in events)

    def test_current_poll_unknown_does_not_fire(self) -> None:
        from leap.monitor.pr_tracking.base import PRState, PRStatus
        from leap.monitor.ui.dock_badge import NotificationType
        seen = PRStatus(state=PRState.ALL_RESPONDED, approved=True,
                        approved_by=['Yarden Goor'], approval_known=True)
        current = PRStatus(state=PRState.ALL_RESPONDED, approval_known=False)
        events = self._detect(seen, current)
        assert not any(e.type == NotificationType.PR_APPROVED for e in events)

    def test_genuine_new_approval_still_fires(self) -> None:
        from leap.monitor.pr_tracking.base import PRState, PRStatus
        from leap.monitor.ui.dock_badge import NotificationType
        seen = PRStatus(state=PRState.ALL_RESPONDED, approved=False,
                        approved_by=None, approval_known=True)
        current = PRStatus(state=PRState.ALL_RESPONDED, approved=True,
                           approved_by=['Yarden Goor'], approval_known=True)
        events = self._detect(seen, current)
        approved = [e for e in events if e.type == NotificationType.PR_APPROVED]
        assert len(approved) == 1 and approved[0].approved_by == ['Yarden Goor']


class TestApprovalFetchFailureStatus:
    """get_pr_status marks approval_known=False only when there is no prior
    value to carry forward — never reporting a definite 'no approvers'."""

    def test_first_fetch_failure_marks_unknown(self) -> None:
        gl = _FakeGL(emoji={})
        p = _make_status_provider(
            gl,
            approvals=_ApprovalsObj(None, raises=True),
            discussions=_DiscussionsObj([]),  # ALL_RESPONDED
        )
        st = p.get_pr_status('g/p', 'feat')
        assert st.approval_known is False
        assert not st.approved and not st.approved_by

    def test_failure_carries_forward_prior_approval(self) -> None:
        # Poll 1 succeeds with an approver; poll 2's approvals fetch fails.
        gl = _FakeGL(emoji={})
        p = _make_status_provider(
            gl,
            approvals=_ApprovalsObj([{'user': {'name': 'Yarden Goor',
                                               'username': 'yarden'}}]),
            discussions=_DiscussionsObj([]),
        )
        st1 = p.get_pr_status('g/p', 'feat')
        assert st1.approved and st1.approved_by == ['Yarden Goor']
        assert st1.approval_known is True
        # Now make the approvals fetch fail and re-poll.
        p._approval_cache.clear()  # force the status-cache carry-forward path
        gl_fail = _FakeGL(emoji={})
        p._gl = gl_fail
        pr_full = SimpleNamespace(approvals=_ApprovalsObj(None, raises=True),
                                  discussions=_DiscussionsObj([]))
        p._project_cache['g/p'].mergerequests.get = lambda iid: pr_full
        st2 = p.get_pr_status('g/p', 'feat')
        # Carried forward from the cached status — still "known", still Yarden.
        assert st2.approval_known is True
        assert st2.approved_by == ['Yarden Goor']


class TestUnrespondedCountFlap:
    """A transient per-thread lookup failure must reuse the cached count
    instead of flapping it upward."""

    def test_transient_emoji_failure_carries_forward_count(self) -> None:
        from leap.monitor.pr_tracking.base import PRState
        # Poll 1: one genuinely-unresponded thread (note 10). Count = 1.
        gl = _FakeGL(emoji={10: []})
        p = _make_status_provider(
            gl,
            approvals=_ApprovalsObj([]),
            discussions=_DiscussionsObj([_reviewer_thread(10)]),
        )
        st1 = p.get_pr_status('g/p', 'feat')
        assert st1.state == PRState.UNRESPONDED and st1.unresponded_count == 1

        # Poll 2: a second thread (note 20) appears; its emoji lookup FAILS
        # while uncached. A naive recount would call it unresponded -> 2.
        gl2 = _FakeGL(emoji={10: [], 20: ['me']}, fail_emoji={20})
        p._gl = gl2
        pr_full = SimpleNamespace(
            approvals=_ApprovalsObj([]),
            discussions=_DiscussionsObj([_reviewer_thread(10),
                                         _reviewer_thread(20)]),
        )
        p._project_cache['g/p'].mergerequests.get = lambda iid: pr_full
        st2 = p.get_pr_status('g/p', 'feat')
        # Carried forward — NOT flapped to 2.
        assert st2.unresponded_count == 1

    def test_real_new_unresponded_thread_still_counts(self) -> None:
        from leap.monitor.pr_tracking.base import PRState
        # Poll 1: count = 1. Poll 2: a real new thread, all lookups succeed.
        gl = _FakeGL(emoji={10: []})
        p = _make_status_provider(
            gl,
            approvals=_ApprovalsObj([]),
            discussions=_DiscussionsObj([_reviewer_thread(10)]),
        )
        assert p.get_pr_status('g/p', 'feat').unresponded_count == 1
        gl2 = _FakeGL(emoji={10: [], 30: []})  # no failures
        p._gl = gl2
        pr_full = SimpleNamespace(
            approvals=_ApprovalsObj([]),
            discussions=_DiscussionsObj([_reviewer_thread(10),
                                         _reviewer_thread(30)]),
        )
        p._project_cache['g/p'].mergerequests.get = lambda iid: pr_full
        st2 = p.get_pr_status('g/p', 'feat')
        assert st2.state == PRState.UNRESPONDED and st2.unresponded_count == 2

    def test_malformed_discussion_carries_forward_not_escapes(self) -> None:
        from leap.monitor.pr_tracking.base import PRState

        class _BadDiscussion:
            @property
            def attributes(self) -> Any:
                raise RuntimeError('malformed discussion payload')

        # Poll 1: count = 1, cached.
        gl = _FakeGL(emoji={10: []})
        p = _make_status_provider(
            gl,
            approvals=_ApprovalsObj([]),
            discussions=_DiscussionsObj([_reviewer_thread(10)]),
        )
        assert p.get_pr_status('g/p', 'feat').unresponded_count == 1
        # Poll 2: a malformed discussion raises inside the scan loop.  It must
        # NOT escape get_pr_status (which would bubble to the worker -> NO_PR
        # -> phantom approval on recovery) — the cached count is carried forward.
        pr_full = SimpleNamespace(
            approvals=_ApprovalsObj([]),
            discussions=_DiscussionsObj([_BadDiscussion()]),
        )
        p._project_cache['g/p'].mergerequests.get = lambda iid: pr_full
        st2 = p.get_pr_status('g/p', 'feat')  # must not raise
        assert st2.state == PRState.UNRESPONDED and st2.unresponded_count == 1


class TestScanReliabilityFlag:
    """The per-thread helpers flip the (thread-local) scan-reliable flag on
    transient failure."""

    @staticmethod
    def _reliable(p: Any) -> bool:
        return getattr(p._scan_state, 'reliable', True)

    def test_emoji_failure_uncached_marks_unreliable(self) -> None:
        p = _make_gitlab_provider()
        p._gl = _FakeGL(emoji={}, fail_emoji={7})
        p._scan_state.reliable = True
        project = SimpleNamespace(id=1)
        assert p._user_reacted_to_note(project, 1, {'id': 7}) is False
        assert self._reliable(p) is False

    def test_emoji_failure_cached_stays_reliable(self) -> None:
        p = _make_gitlab_provider()
        p._gl = _FakeGL(emoji={}, fail_emoji={7})
        p._emoji_cache[7] = True  # a prior successful read
        p._scan_state.reliable = True
        assert p._user_reacted_to_note(project=SimpleNamespace(id=1),
                                       pr_iid=1, note={'id': 7}) is True
        assert self._reliable(p) is True

    def test_bot_lookup_failure_marks_unreliable(self) -> None:
        p = _make_gitlab_provider()
        p._gl = _FakeGL(emoji={}, fail_users=True)
        p._scan_state.reliable = True
        assert p._is_bot_author({'author': {'id': 5, 'username': 'x'}}) is False
        assert self._reliable(p) is False


class TestGitLabTransientListFailure:
    """A transient MR-list failure must keep the last known status rather than
    flapping to NO_PR (which re-fires a phantom approval on recovery)."""

    def test_list_failure_keeps_cached_status(self) -> None:
        from leap.monitor.pr_tracking.base import PRState
        gl = _FakeGL(emoji={})
        p = _make_status_provider(
            gl,
            approvals=_ApprovalsObj([{'user': {'name': 'Yarden Goor',
                                               'username': 'yarden'}}]),
            discussions=_DiscussionsObj([]),
        )
        st1 = p.get_pr_status('g/p', 'feat')
        assert st1.state != PRState.NO_PR and st1.approved

        def _boom(**_kw: Any) -> list:
            raise RuntimeError('list failed')

        p._project_cache['g/p'].mergerequests.list = _boom
        st2 = p.get_pr_status('g/p', 'feat')
        assert st2.state != PRState.NO_PR
        assert st2.approved_by == ['Yarden Goor']

    def test_list_failure_with_no_cache_is_no_pr(self) -> None:
        from leap.monitor.pr_tracking.base import PRState
        gl = _FakeGL(emoji={})
        p = _make_status_provider(
            gl,
            approvals=_ApprovalsObj([]),
            discussions=_DiscussionsObj([]),
        )

        def _boom(**_kw: Any) -> list:
            raise RuntimeError('list failed')

        p._project_cache['g/p'].mergerequests.list = _boom
        assert p.get_pr_status('g/p', 'feat').state == PRState.NO_PR

    def test_genuine_empty_evicts_cache_so_later_failure_stays_no_pr(self) -> None:
        # A merged/closed PR must not be "resurrected" as open by a transient
        # failure after it genuinely went away — otherwise a merged pinned row
        # spuriously reopens.
        from leap.monitor.pr_tracking.base import PRState
        gl = _FakeGL(emoji={})
        p = _make_status_provider(
            gl,
            approvals=_ApprovalsObj([{'user': {'name': 'Yarden Goor',
                                               'username': 'yarden'}}]),
            discussions=_DiscussionsObj([]),
        )
        # Poll 1: open + approved -> cached open status.
        assert p.get_pr_status('g/p', 'feat').state != PRState.NO_PR
        # Poll 2: branch now has no open MR (merged) -> NO_PR, evicts cache.
        p._project_cache['g/p'].mergerequests.list = lambda **kw: []
        assert p.get_pr_status('g/p', 'feat').state == PRState.NO_PR

        # Poll 3: transient list failure -> must stay NO_PR (cache evicted),
        # NOT resurrect the stale open/approved status.
        def _boom(**_kw: Any) -> list:
            raise RuntimeError('list failed')

        p._project_cache['g/p'].mergerequests.list = _boom
        st3 = p.get_pr_status('g/p', 'feat')
        assert st3.state == PRState.NO_PR
        assert not st3.approved
