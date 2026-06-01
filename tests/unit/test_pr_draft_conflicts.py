"""Tests for the Draft / merge-conflict signals surfaced on open-PR cells.

Covers both providers' ``get_pr_status`` threading the new ``draft`` /
``has_conflicts`` fields through to ``PRStatus``, GitLab's centralized
``_mr_has_conflicts`` derivation, and the new ``pr_merged_color`` theme field.
All pure-logic: providers are built via ``__new__`` with the network surface
(``_get_repo`` / ``_get_project`` / thread counting) stubbed out.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from leap.monitor.pr_tracking.base import PRState
from leap.monitor.pr_tracking.github_provider import GitHubProvider
from leap.monitor.pr_tracking.gitlab_provider import GitLabProvider
from leap.monitor.themes import THEMES


# ---------------------------------------------------------------------------
#  GitHub
# ---------------------------------------------------------------------------

def _github_provider() -> GitHubProvider:
    p = GitHubProvider.__new__(GitHubProvider)
    p._gh = None  # type: ignore[attr-defined]
    p._token = 'fake'
    p._username = 'me'
    p._filter_bots = True
    p._repo_cache = {}
    p._approval_cache = {}
    p._status_cache = {}
    p._reaction_cache = {}
    p._graphql_url = 'https://api.github.com/graphql'
    return p


def _github_with_open_pr(pr: Any, unresponded: int = 0) -> GitHubProvider:
    """Wire a provider so ``get_pr_status`` resolves to *pr* with the given
    unresponded count, bypassing the network/threading paths."""
    p = _github_provider()
    p._get_repo = lambda project_path: object()          # type: ignore[method-assign]
    p._find_open_prs = lambda repo, pp, br, iid: [pr]      # type: ignore[method-assign]
    first = (555, 'r') if unresponded else (None, None)
    p._count_unresponded_threads = (                       # type: ignore[method-assign]
        lambda repo, _pr: (unresponded, first[0], first[1]))
    return p


def _review(login: str, state: str) -> Any:
    return SimpleNamespace(user=SimpleNamespace(login=login), state=state)


def _gh_pr(*, draft: bool = False, mergeable_state: Any = None,
           reviews: Any = None) -> Any:
    return SimpleNamespace(
        number=42, html_url='https://github.com/o/r/pull/42',
        title='T', draft=draft, mergeable_state=mergeable_state,
        get_reviews=lambda: (reviews or []),
    )


class TestGithubDraftConflicts:
    def test_clean_ready_pr_has_neither(self) -> None:
        p = _github_with_open_pr(_gh_pr(draft=False, mergeable_state='clean'))
        st = p.get_pr_status('o/r', 'b')
        assert st.state == PRState.ALL_RESPONDED
        assert st.draft is False and st.has_conflicts is False

    def test_draft_flag_threaded(self) -> None:
        p = _github_with_open_pr(_gh_pr(draft=True, mergeable_state='clean'))
        assert p.get_pr_status('o/r', 'b').draft is True

    def test_dirty_mergeable_state_is_conflict(self) -> None:
        p = _github_with_open_pr(_gh_pr(mergeable_state='dirty'))
        assert p.get_pr_status('o/r', 'b').has_conflicts is True

    def test_unknown_mergeable_state_not_conflict(self) -> None:
        # GitHub returns None/'unknown' while still computing — must not
        # be reported as a conflict.
        for ms in (None, 'unknown', 'blocked', 'behind'):
            p = _github_with_open_pr(_gh_pr(mergeable_state=ms))
            assert p.get_pr_status('o/r', 'b').has_conflicts is False

    def test_draft_and_conflict_on_unresponded(self) -> None:
        p = _github_with_open_pr(
            _gh_pr(draft=True, mergeable_state='dirty'), unresponded=3)
        st = p.get_pr_status('o/r', 'b')
        assert st.state == PRState.UNRESPONDED
        assert st.unresponded_count == 3
        assert st.draft is True and st.has_conflicts is True


class TestGithubChangesRequestedAndChecks:
    def test_changes_requested_from_review(self) -> None:
        p = _github_with_open_pr(
            _gh_pr(reviews=[_review('alice', 'CHANGES_REQUESTED')]))
        assert p.get_pr_status('o/r', 'b').changes_requested is True

    def test_later_approval_overrides_changes_requested(self) -> None:
        # Same reviewer flips to APPROVED later -> no longer blocking.
        p = _github_with_open_pr(_gh_pr(reviews=[
            _review('alice', 'CHANGES_REQUESTED'),
            _review('alice', 'APPROVED'),
        ]))
        st = p.get_pr_status('o/r', 'b')
        assert st.changes_requested is False
        assert st.approved is True

    def test_disagreeing_reviewers_block(self) -> None:
        # One approves, another requests changes -> still blocking.
        p = _github_with_open_pr(_gh_pr(reviews=[
            _review('alice', 'APPROVED'),
            _review('bob', 'CHANGES_REQUESTED'),
        ]))
        st = p.get_pr_status('o/r', 'b')
        assert st.changes_requested is True
        assert st.approved is True

    def test_checks_lookup_gated_on_mergeable_state(self) -> None:
        # The (extra-API-call) check-runs lookup runs ONLY when mergeable_state
        # signals a possible problem, so clean PRs cost nothing.  When it runs
        # and reports a failure, checks_failed is True.
        cases = {'clean': False, 'behind': False, 'dirty': False,
                 None: False, 'unstable': True, 'blocked': True}
        for ms, expect in cases.items():
            p = _github_with_open_pr(_gh_pr(mergeable_state=ms))
            calls: list = []
            p._github_checks_failed = (  # type: ignore[method-assign]
                lambda repo, pr: (calls.append(1) or True))
            st = p.get_pr_status('o/r', 'b')
            assert bool(calls) is expect, ms        # lookup gated correctly
            assert st.checks_failed is expect, ms    # result threaded through


class TestGithubChecksFailedLookup:
    """The head-commit check-runs probe that confirms a real CI failure."""

    def _run(self, *, conclusions: tuple = (), combined: Any = None,
             has_head: bool = True) -> bool:
        p = _github_provider()
        commit = SimpleNamespace(
            get_check_runs=lambda: [SimpleNamespace(conclusion=c)
                                    for c in conclusions],
            get_combined_status=lambda: SimpleNamespace(state=combined),
        )
        repo = SimpleNamespace(get_commit=lambda sha: commit)
        pr = (SimpleNamespace(head=SimpleNamespace(sha='abc'))
              if has_head else SimpleNamespace())
        return p._github_checks_failed(repo, pr)

    def test_failure_conclusion(self) -> None:
        assert self._run(conclusions=('success', 'failure')) is True

    def test_all_success(self) -> None:
        assert self._run(conclusions=('success', 'neutral')) is False

    def test_pending_run_not_failed(self) -> None:
        # conclusion None == still running -> not a failure (the key case).
        assert self._run(conclusions=(None, None)) is False

    def test_timed_out_is_failure(self) -> None:
        assert self._run(conclusions=('timed_out',)) is True

    def test_legacy_combined_failure(self) -> None:
        assert self._run(conclusions=(), combined='failure') is True

    def test_legacy_combined_error(self) -> None:
        assert self._run(combined='error') is True

    def test_legacy_combined_success(self) -> None:
        assert self._run(conclusions=('success',), combined='success') is False

    def test_nothing_reported(self) -> None:
        assert self._run(conclusions=(), combined=None) is False


# ---------------------------------------------------------------------------
#  GitLab — _mr_has_conflicts (centralized derivation)
# ---------------------------------------------------------------------------

class TestMrHasConflicts:
    def test_canonical_true(self) -> None:
        mr = SimpleNamespace(has_conflicts=True)
        assert GitLabProvider._mr_has_conflicts(mr) is True

    def test_canonical_false_wins_over_merge_status(self) -> None:
        # has_conflicts present (even if False) is authoritative.
        mr = SimpleNamespace(has_conflicts=False,
                             merge_status='cannot_be_merged')
        assert GitLabProvider._mr_has_conflicts(mr) is False

    def test_merge_status_fallback(self) -> None:
        mr = SimpleNamespace(merge_status='cannot_be_merged')
        assert GitLabProvider._mr_has_conflicts(mr) is True

    def test_detailed_merge_status_conflict_fallback(self) -> None:
        mr = SimpleNamespace(detailed_merge_status='conflict')
        assert GitLabProvider._mr_has_conflicts(mr) is True

    def test_mergeable_status_is_false(self) -> None:
        mr = SimpleNamespace(merge_status='can_be_merged')
        assert GitLabProvider._mr_has_conflicts(mr) is False

    def test_no_fields_returns_default(self) -> None:
        mr = SimpleNamespace()
        assert GitLabProvider._mr_has_conflicts(mr) is False
        assert GitLabProvider._mr_has_conflicts(mr, default=None) is None


# ---------------------------------------------------------------------------
#  GitLab — get_pr_status threading
# ---------------------------------------------------------------------------

def _gitlab_provider() -> GitLabProvider:
    p = GitLabProvider.__new__(GitLabProvider)
    p._gl = None  # type: ignore[attr-defined]
    p._username = 'me'
    p._filter_bots = True
    p._project_cache = {}
    p._bot_cache = {}
    p._approval_cache = {}
    p._status_cache = {}
    p._emoji_cache = {}
    return p


def _gl_pr_full(*, head_pipeline: Any = None,
                detailed_merge_status: Any = None) -> Any:
    """A full MR object with no draft/conflict attributes and no comments —
    so the list-response read is what determines draft/has_conflicts.
    Optionally carries head_pipeline / detailed_merge_status for the
    CI-failed / changes-requested derivations."""
    ns = SimpleNamespace(
        approvals=SimpleNamespace(get=lambda: SimpleNamespace(approved_by=[])),
        approved_by=[],
        approval_state=SimpleNamespace(get=lambda: SimpleNamespace(rules=[])),
        discussions=SimpleNamespace(list=lambda get_all=True: []),
    )
    if head_pipeline is not None:
        ns.head_pipeline = head_pipeline
    if detailed_merge_status is not None:
        ns.detailed_merge_status = detailed_merge_status
    return ns


def _gitlab_with_open_mr(mr: Any, pr_full: Any) -> GitLabProvider:
    p = _gitlab_provider()
    project = SimpleNamespace(
        mergerequests=SimpleNamespace(
            list=lambda **kw: [mr],
            get=lambda iid: pr_full,
        )
    )
    p._get_project = lambda project_path: project  # type: ignore[method-assign]
    return p


class TestGitlabDraftConflicts:
    def _mr(self, **kw: Any) -> Any:
        base = dict(iid=7, web_url='https://gitlab.com/g/p/-/merge_requests/7',
                    title='T')
        base.update(kw)
        return SimpleNamespace(**base)

    def test_clean_pr_has_neither(self) -> None:
        p = _gitlab_with_open_mr(
            self._mr(draft=False, has_conflicts=False), _gl_pr_full())
        st = p.get_pr_status('g/p', 'b')
        assert st.state == PRState.ALL_RESPONDED
        assert st.draft is False and st.has_conflicts is False

    def test_draft_from_list_response(self) -> None:
        p = _gitlab_with_open_mr(
            self._mr(draft=True, has_conflicts=False), _gl_pr_full())
        assert p.get_pr_status('g/p', 'b').draft is True

    def test_legacy_work_in_progress_alias(self) -> None:
        p = _gitlab_with_open_mr(
            self._mr(work_in_progress=True), _gl_pr_full())
        assert p.get_pr_status('g/p', 'b').draft is True

    def test_conflicts_from_has_conflicts(self) -> None:
        p = _gitlab_with_open_mr(
            self._mr(has_conflicts=True), _gl_pr_full())
        assert p.get_pr_status('g/p', 'b').has_conflicts is True

    def test_conflicts_from_merge_status_fallback(self) -> None:
        p = _gitlab_with_open_mr(
            self._mr(merge_status='cannot_be_merged'), _gl_pr_full())
        assert p.get_pr_status('g/p', 'b').has_conflicts is True

    def test_full_fetch_overrides_stale_list_draft(self) -> None:
        # List said draft, but the freshly-fetched full object says ready.
        mr = self._mr(draft=True)
        pr_full = _gl_pr_full()
        pr_full.draft = False
        p = _gitlab_with_open_mr(mr, pr_full)
        assert p.get_pr_status('g/p', 'b').draft is False

    def test_pipeline_failed_is_checks_failed(self) -> None:
        p = _gitlab_with_open_mr(
            self._mr(), _gl_pr_full(head_pipeline={'status': 'failed'}))
        assert p.get_pr_status('g/p', 'b').checks_failed is True

    def test_pipeline_success_not_checks_failed(self) -> None:
        p = _gitlab_with_open_mr(
            self._mr(), _gl_pr_full(head_pipeline={'status': 'success'}))
        assert p.get_pr_status('g/p', 'b').checks_failed is False

    def test_running_pipeline_not_checks_failed(self) -> None:
        # Only a definite 'failed' counts — a running pipeline isn't a failure.
        p = _gitlab_with_open_mr(
            self._mr(), _gl_pr_full(head_pipeline={'status': 'running'}))
        assert p.get_pr_status('g/p', 'b').checks_failed is False

    def test_changes_requested_from_detailed_merge_status(self) -> None:
        p = _gitlab_with_open_mr(
            self._mr(),
            _gl_pr_full(detailed_merge_status='requested_changes'))
        assert p.get_pr_status('g/p', 'b').changes_requested is True

    def test_mergeable_not_changes_requested(self) -> None:
        p = _gitlab_with_open_mr(
            self._mr(), _gl_pr_full(detailed_merge_status='mergeable'))
        assert p.get_pr_status('g/p', 'b').changes_requested is False


class TestGitlabStaticHelpers:
    def test_pipeline_failed_dict(self) -> None:
        assert GitLabProvider._mr_pipeline_failed(
            SimpleNamespace(head_pipeline={'status': 'failed'})) is True

    def test_pipeline_failed_object(self) -> None:
        hp = SimpleNamespace(status='failed')
        assert GitLabProvider._mr_pipeline_failed(
            SimpleNamespace(head_pipeline=hp)) is True

    def test_pipeline_none(self) -> None:
        assert GitLabProvider._mr_pipeline_failed(SimpleNamespace()) is False

    def test_changes_requested_helper(self) -> None:
        assert GitLabProvider._mr_changes_requested(
            SimpleNamespace(detailed_merge_status='requested_changes')) is True
        assert GitLabProvider._mr_changes_requested(
            SimpleNamespace(detailed_merge_status='mergeable')) is False
        assert GitLabProvider._mr_changes_requested(
            SimpleNamespace()) is False


# ---------------------------------------------------------------------------
#  Theme field
# ---------------------------------------------------------------------------

class TestPrMergedColorTheme:
    def test_every_theme_defines_a_merged_color(self) -> None:
        for name, theme in THEMES.items():
            assert isinstance(theme.pr_merged_color, str), name
            assert theme.pr_merged_color.startswith('#'), name
            # 6-digit hex so active_btn_style's int(h[..],16) slicing works.
            assert len(theme.pr_merged_color.lstrip('#')) == 6, name

    def test_light_themes_override_to_darker_violet(self) -> None:
        # The dark default (#a371f7) washes out on light backgrounds — the
        # two light themes must override it.
        for name in ('Dawn', 'Leap'):
            assert THEMES[name].is_dark is False
            assert THEMES[name].pr_merged_color == '#7c3aed'

    def test_dark_themes_keep_the_default_violet(self) -> None:
        assert THEMES['Midnight'].pr_merged_color == '#a371f7'
