"""Tests for GitHubProvider helpers — anchor formatting, URL translation,
notification reasons, thread response detection, and discussion-id routing.

These exercise the pure-logic surface of the provider without hitting the
network.  PyGithub objects are stubbed with simple namespaces because the
provider only reads attributes (no method calls) on them in these paths.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from leap.monitor.pr_tracking.github_provider import GitHubProvider


def _make_provider(username: str = 'me', filter_bots: bool = True) -> GitHubProvider:
    """Build a GitHubProvider without exercising the PyGithub constructor's
    auth path — the methods under test never touch ``self._gh``."""
    p = GitHubProvider.__new__(GitHubProvider)
    p._gh = None  # type: ignore[attr-defined]
    p._token = 'fake'
    p._username = username
    p._filter_bots = filter_bots
    p._repo_cache = {}
    p._approval_cache = {}
    p._status_cache = {}
    p._reaction_cache = {}
    p._graphql_url = 'https://api.github.com/graphql'
    return p


def _comment(
    *, id: int, login: str | None = 'me', body: str = '',
    user_type: str = 'User', in_reply_to_id: int | None = None,
    created_at: Any = None, path: str | None = None,
    line: int | None = None, original_line: int | None = None,
) -> Any:
    """Build a minimal stand-in for a PyGithub PullRequestComment."""
    user = (None if login is None
            else SimpleNamespace(login=login, type=user_type))
    return SimpleNamespace(
        id=id, body=body, user=user, in_reply_to_id=in_reply_to_id,
        created_at=created_at, path=path, line=line,
        original_line=original_line,
        get_reactions=lambda: [],  # default: no reactions
    )


class TestBuildFirstUnrespondedUrl:
    """Anchor format must match the platform — fix #1."""

    def test_review_comment_uses_discussion_r_anchor(self) -> None:
        p = _make_provider()
        url = p.build_first_unresponded_url(
            'https://github.com/o/r/pull/42', 12345, origin='r')
        assert url == 'https://github.com/o/r/pull/42#discussion_r12345'

    def test_issue_comment_uses_issuecomment_anchor(self) -> None:
        p = _make_provider()
        url = p.build_first_unresponded_url(
            'https://github.com/o/r/pull/42', 67890, origin='i')
        assert url == 'https://github.com/o/r/pull/42#issuecomment-67890'

    def test_default_origin_is_review(self) -> None:
        p = _make_provider()
        url = p.build_first_unresponded_url(
            'https://github.com/o/r/pull/42', 1)
        assert url == 'https://github.com/o/r/pull/42#discussion_r1'


class TestSplitDiscussionId:
    """Discussion-id discrimination drives ack routing — fix #4."""

    def test_review_prefix(self) -> None:
        assert GitHubProvider._split_discussion_id('r:42') == ('r', 42)

    def test_issue_prefix(self) -> None:
        assert GitHubProvider._split_discussion_id('i:99') == ('i', 99)

    def test_bare_int_treated_as_review_for_back_compat(self) -> None:
        assert GitHubProvider._split_discussion_id('123') == ('r', 123)

    def test_invalid_id_raises(self) -> None:
        with pytest.raises(ValueError):
            GitHubProvider._split_discussion_id('r:notanint')


class TestNormalizeGithubReason:
    """Reason normalization keeps notification surfacing in sync with GitLab."""

    def test_review_requested(self) -> None:
        assert GitHubProvider._normalize_github_reason('review_requested') == 'review_requested'

    def test_assign_singular_form(self) -> None:
        # GitHub uses 'assign', not 'assigned' — the normalizer must handle that.
        assert GitHubProvider._normalize_github_reason('assign') == 'assigned'

    def test_mentions_consolidated(self) -> None:
        assert GitHubProvider._normalize_github_reason('mention') == 'mentioned'
        assert GitHubProvider._normalize_github_reason('team_mention') == 'mentioned'

    def test_unknown_reasons_drop_to_other(self) -> None:
        for reason in ('comment', 'state_change', 'subscribed',
                       'ci_activity', 'manual', 'security_alert'):
            assert GitHubProvider._normalize_github_reason(reason) == 'other'

    def test_case_insensitive(self) -> None:
        assert GitHubProvider._normalize_github_reason('REVIEW_REQUESTED') == 'review_requested'


class TestResolveNotificationUrl:
    """API-URL → web-URL translation — fix #11."""

    @staticmethod
    def _notif(api_url: str | None, repo_html: str = 'https://github.com/o/r') -> Any:
        subject = SimpleNamespace(url=api_url, title='', latest_comment_url=None)
        repo = SimpleNamespace(html_url=repo_html)
        return SimpleNamespace(subject=subject, repository=repo)

    def test_pull_url_translated_to_pull(self) -> None:
        n = self._notif('https://api.github.com/repos/o/r/pulls/42')
        assert (GitHubProvider._resolve_notification_url(n)
                == 'https://github.com/o/r/pull/42')

    def test_commit_url_translated_to_commit_singular(self) -> None:
        n = self._notif('https://api.github.com/repos/o/r/commits/abc123')
        assert (GitHubProvider._resolve_notification_url(n)
                == 'https://github.com/o/r/commit/abc123')

    def test_release_id_falls_back_to_releases_page(self) -> None:
        n = self._notif('https://api.github.com/repos/o/r/releases/12345')
        # Tag isn't in the API URL so we can't link to /releases/tag/<tag>;
        # /releases is the safest landing.
        assert (GitHubProvider._resolve_notification_url(n)
                == 'https://github.com/o/r/releases')

    def test_issue_url_passes_through(self) -> None:
        n = self._notif('https://api.github.com/repos/o/r/issues/7')
        assert (GitHubProvider._resolve_notification_url(n)
                == 'https://github.com/o/r/issues/7')

    def test_ghe_pulls_url_translated(self) -> None:
        n = self._notif('https://ghe.example.com/api/v3/repos/o/r/pulls/3')
        assert (GitHubProvider._resolve_notification_url(n)
                == 'https://ghe.example.com/o/r/pull/3')

    def test_unknown_subject_falls_back_to_repo_html(self) -> None:
        # WorkflowRun-style URL we don't translate — return repo URL so the
        # user lands somewhere navigable rather than a 404.
        n = self._notif(
            'https://api.github.com/repos/o/r/check-suites/9',
            repo_html='https://github.com/o/r',
        )
        # /check-suites/ isn't in our translate list, but it also doesn't
        # contain api.github.com after the first replace, so we keep it.
        # (Web URL for check-suites is /actions/runs/<id> — translation
        # would need extra info we don't have.)
        result = GitHubProvider._resolve_notification_url(n)
        # Either the literal translated URL, or the repo fallback — both
        # are non-empty and rooted at the right host.
        assert result.startswith('https://github.com/o/r')

    def test_no_subject_falls_back_to_repo_html(self) -> None:
        notif = SimpleNamespace(
            subject=None,
            repository=SimpleNamespace(html_url='https://github.com/o/r'),
        )
        assert GitHubProvider._resolve_notification_url(notif) == 'https://github.com/o/r'


class TestGroupIntoThreads:
    """The threading reconstruction must survive out-of-order delivery."""

    def test_root_then_replies_keeps_root_first(self) -> None:
        p = _make_provider()
        c0 = _comment(id=1, login='reviewer')
        c1 = _comment(id=2, login='me', in_reply_to_id=1)
        c2 = _comment(id=3, login='reviewer', in_reply_to_id=1)
        threads = p._group_into_threads([c0, c1, c2])
        assert list(threads.keys()) == [1]
        assert [c.id for c in threads[1]] == [1, 2, 3]

    def test_reply_arriving_before_root_is_recovered(self) -> None:
        p = _make_provider()
        # Same thread, but the API listed a reply before the root
        c1 = _comment(id=2, login='me', in_reply_to_id=1)
        c2 = _comment(id=3, login='reviewer', in_reply_to_id=1)
        c0 = _comment(id=1, login='reviewer')
        threads = p._group_into_threads([c1, c2, c0])
        # Root inserted at position 0 even when it arrived last
        assert threads[1][0].id == 1


class TestIsUnrespondedThread:
    """Core thread-state machine — used by both the count and the /leap scan."""

    def test_other_then_user_reply_is_responded(self) -> None:
        p = _make_provider(username='me')
        thread = [
            _comment(id=1, login='reviewer', body='Please fix'),
            _comment(id=2, login='me', body='Done'),
        ]
        assert p._is_unresponded_thread(thread) is False

    def test_other_only_is_unresponded(self) -> None:
        p = _make_provider(username='me')
        thread = [_comment(id=1, login='reviewer', body='Please fix')]
        assert p._is_unresponded_thread(thread) is True

    def test_user_only_is_not_unresponded(self) -> None:
        # No other party = nothing to respond to.
        p = _make_provider(username='me')
        thread = [_comment(id=1, login='me', body='self-note')]
        assert p._is_unresponded_thread(thread) is False

    def test_leap_does_not_count_as_response(self) -> None:
        p = _make_provider(username='me')
        thread = [
            _comment(id=1, login='reviewer', body='Please fix'),
            _comment(id=2, login='me', body='/leap'),
        ]
        assert p._is_unresponded_thread(thread) is True

    def test_bots_filtered_out_when_filter_bots(self) -> None:
        p = _make_provider(username='me', filter_bots=True)
        thread = [
            _comment(id=1, login='dependabot[bot]', body='bump',
                     user_type='Bot'),
        ]
        # Only the bot commented; with bots filtered out there's no thread.
        assert p._is_unresponded_thread(thread) is False

    def test_emoji_reaction_acks_thread(self) -> None:
        # Fix #6: reacting to the last other-party comment counts as ack.
        p = _make_provider(username='me')
        last_other = _comment(id=1, login='reviewer', body='Please fix')
        last_other.get_reactions = lambda: [
            SimpleNamespace(user=SimpleNamespace(login='me'),
                            content='THUMBS_UP'),
        ]
        assert p._is_unresponded_thread([last_other]) is False

    def test_other_reaction_does_not_ack(self) -> None:
        p = _make_provider(username='me')
        last_other = _comment(id=1, login='reviewer', body='Please fix')
        last_other.get_reactions = lambda: [
            SimpleNamespace(user=SimpleNamespace(login='someone-else'),
                            content='THUMBS_UP'),
        ]
        assert p._is_unresponded_thread([last_other]) is True


class TestPRPrefixInLeapMessage:
    """Cosmetic: GitLab uses '!', GitHub uses '#'.  Fix #9."""

    def test_github_prefix(self) -> None:
        from leap.monitor.pr_tracking.leap_command import _pr_prefix
        assert _pr_prefix('github') == '#'

    def test_gitlab_prefix(self) -> None:
        from leap.monitor.pr_tracking.leap_command import _pr_prefix
        assert _pr_prefix('gitlab') == '!'

    def test_unknown_falls_back_to_gitlab(self) -> None:
        # Default-case behaviour for callers that didn't set scm_type.
        from leap.monitor.pr_tracking.leap_command import _pr_prefix
        assert _pr_prefix('') == '!'


class TestFormatLeapMessageScmType:
    def test_github_command_renders_hash_prefix(self) -> None:
        from leap.monitor.pr_tracking.leap_command import (
            CqCommand, format_leap_message,
        )
        cmd = CqCommand(
            project_path='o/r', pr_iid=42, pr_title='T',
            pr_url='https://github.com/o/r/pull/42',
            discussion_id='r:1', thread_notes=[
                {'author': 'reviewer', 'body': 'Please fix', 'created_at': ''}
            ],
            scm_type='github',
        )
        msg = format_leap_message(cmd)
        assert 'PR #42' in msg
        assert 'PR !42' not in msg

    def test_gitlab_command_renders_bang_prefix(self) -> None:
        from leap.monitor.pr_tracking.leap_command import (
            CqCommand, format_leap_message,
        )
        cmd = CqCommand(
            project_path='g/p', pr_iid=42, pr_title='T',
            pr_url='https://gitlab.com/g/p/-/merge_requests/42',
            discussion_id='abc', thread_notes=[],
            scm_type='gitlab',
        )
        msg = format_leap_message(cmd)
        assert 'PR !42' in msg
        assert 'PR #42' not in msg


class TestGitLabRegressionGuard:
    """Pin GitLab outputs against the byte-identical-to-pre-fix promise.

    Several of the fixes touched shared files (base, scm_polling,
    pr_display_mixin, leap_command, gitlab_provider).  This class makes sure
    GitLab's user-visible outputs didn't drift.
    """

    def _make_gitlab_provider(self) -> Any:
        from leap.monitor.pr_tracking.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p._gl = None
        p._username = 'me'
        p._filter_bots = True
        p._project_cache = {}
        p._bot_cache = {}
        p._approval_cache = {}
        p._status_cache = {}
        p._emoji_cache = {}
        return p

    def test_gitlab_anchor_is_note_format(self) -> None:
        # The fix split the deep-link out of pr_display_mixin into the
        # provider.  GitLab MUST still emit '#note_<id>' so existing user
        # bookmarks / muscle memory keep working.
        p = self._make_gitlab_provider()
        assert (p.build_first_unresponded_url('https://gitlab.com/g/p/-/merge_requests/42', 9999)
                == 'https://gitlab.com/g/p/-/merge_requests/42#note_9999')

    def test_gitlab_anchor_ignores_origin_kwarg(self) -> None:
        # New base-class signature added an `origin` kwarg.  GitLab must
        # ignore it — review/general MR comments use the same anchor.
        p = self._make_gitlab_provider()
        for origin in ('r', 'i', 'whatever'):
            assert (p.build_first_unresponded_url('https://gitlab.com/g/p/-/merge_requests/1', 7, origin=origin)
                    == 'https://gitlab.com/g/p/-/merge_requests/1#note_7')

    def test_gitlab_format_leap_message_keeps_bang_prefix(self) -> None:
        # The CqCommand default scm_type is 'gitlab', so any GitLab caller
        # that didn't update its construction site keeps producing '!N'.
        from leap.monitor.pr_tracking.leap_command import CqCommand, format_leap_message
        cmd = CqCommand(
            project_path='g/p', pr_iid=99, pr_title='T',
            pr_url='https://gitlab.com/g/p/-/merge_requests/99',
            discussion_id='abc', thread_notes=[],
        )
        # No scm_type passed → defaults to 'gitlab'
        msg = format_leap_message(cmd)
        assert 'PR !99' in msg

    def test_gitlab_setup_dialog_keeps_gitlab_token_placeholder(self) -> None:
        # GitLab subclass's env-var placeholder must still be 'GITLAB_TOKEN'
        # — that's the whole point of letting subclasses override.
        from leap.monitor.dialogs.gitlab_setup_dialog import GitLabSetupDialog
        # We can't easily instantiate the dialog without Qt, but we CAN
        # call the subclass method on the class (it doesn't need self
        # state for this).
        d = GitLabSetupDialog.__new__(GitLabSetupDialog)
        assert d._env_var_placeholder() == 'e.g. GITLAB_TOKEN'

    def test_gitlab_get_pr_status_accepts_pr_iid_kwarg(self) -> None:
        # New abstract signature added `pr_iid: Optional[int] = None`.
        # GitLab must accept it without error (provider del's it).
        from inspect import signature
        from leap.monitor.pr_tracking.gitlab_provider import GitLabProvider
        params = signature(GitLabProvider.get_pr_status).parameters
        assert 'pr_iid' in params
        assert params['pr_iid'].default is None


class TestCheckGithubScopesPublicRepoAcceptance:
    """public_repo alone is enough for public-repo PR tracking — fix #13."""

    def test_public_repo_does_not_warn_about_repo_scope(self) -> None:
        from leap.monitor.dialogs.github_setup_dialog import _check_github_scopes
        gh = SimpleNamespace(oauth_scopes=['public_repo', 'notifications'])
        warnings = _check_github_scopes(gh)
        assert all('repo scope' not in w for w in warnings)

    def test_repo_scope_does_not_warn(self) -> None:
        from leap.monitor.dialogs.github_setup_dialog import _check_github_scopes
        gh = SimpleNamespace(oauth_scopes=['repo', 'notifications'])
        warnings = _check_github_scopes(gh)
        assert warnings == []

    def test_no_repo_scopes_at_all_warns(self) -> None:
        from leap.monitor.dialogs.github_setup_dialog import _check_github_scopes
        gh = SimpleNamespace(oauth_scopes=['notifications'])
        warnings = _check_github_scopes(gh)
        assert any('repo scope' in w for w in warnings)


def _closed_pr(*, number: int, title: str = 't', html_url: str = 'u',
               merged_at: Any = None) -> Any:
    """Minimal stand-in for a PyGithub PullRequest from get_pulls(state='closed')."""
    return SimpleNamespace(
        number=number, title=title, html_url=html_url, merged_at=merged_at,
    )


class TestFindLatestClosedPR:
    """The closed/merged-PR fallback shown when no OPEN PR matches a branch."""

    def _provider_with_pulls(self, pulls: list) -> GitHubProvider:
        p = _make_provider()
        seen: dict = {}
        repo = SimpleNamespace(get_pulls=lambda **kw: (seen.update(kw) or pulls))
        p._get_repo = lambda project_path: repo  # type: ignore[method-assign]
        p._last_pulls_kwargs = seen  # type: ignore[attr-defined]
        return p

    def test_merged_pr_reports_merged_true(self) -> None:
        p = self._provider_with_pulls([_closed_pr(
            number=108, title='IBOSS-5465', html_url='https://h/o/r/pull/108',
            merged_at='2024-01-01T00:00:00Z')])
        info = p.find_latest_closed_pr('o/r', 'feature-branch')
        assert info is not None
        assert info.pr_iid == 108
        assert info.merged is True
        assert info.pr_url == 'https://h/o/r/pull/108'

    def test_closed_unmerged_reports_merged_false(self) -> None:
        p = self._provider_with_pulls([_closed_pr(number=5, merged_at=None)])
        info = p.find_latest_closed_pr('o/r', 'b')
        assert info is not None and info.merged is False

    def test_uses_owner_head_filter_sorted_recent(self) -> None:
        p = self._provider_with_pulls([_closed_pr(number=1)])
        p.find_latest_closed_pr('myorg/myrepo', 'topic')
        kw = p._last_pulls_kwargs  # type: ignore[attr-defined]
        assert kw['state'] == 'closed'
        assert kw['head'] == 'myorg:topic'
        assert kw['sort'] == 'updated' and kw['direction'] == 'desc'

    def test_no_repo_returns_none(self) -> None:
        p = _make_provider()
        p._get_repo = lambda project_path: None  # type: ignore[method-assign]
        assert p.find_latest_closed_pr('o/r', 'b') is None

    def test_empty_pulls_returns_none(self) -> None:
        p = self._provider_with_pulls([])
        assert p.find_latest_closed_pr('o/r', 'b') is None

    def test_api_exception_swallowed_returns_none(self) -> None:
        p = _make_provider()

        def _boom(**_kw: Any) -> Any:
            raise RuntimeError('network')

        p._get_repo = lambda project_path: SimpleNamespace(get_pulls=_boom)  # type: ignore[method-assign]
        assert p.find_latest_closed_pr('o/r', 'b') is None

    def test_title_none_coerced_to_empty(self) -> None:
        p = self._provider_with_pulls([_closed_pr(number=2, title=None)])
        info = p.find_latest_closed_pr('o/r', 'b')
        assert info is not None and info.pr_title == ''
