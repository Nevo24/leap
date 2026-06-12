"""Tests for BitbucketProvider (Cloud + Server) and its integration seams.

Pure-logic tests - no network.  The provider's constructor only builds a
``requests.Session`` (no auth call), so instances are constructed normally
and the HTTP layer is stubbed per test by monkeypatching the private
fetch helpers.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import requests

from leap.monitor.pr_tracking.base import PRState
from leap.monitor.pr_tracking.bitbucket_provider import (
    BitbucketAuthError,
    BitbucketProvider,
    LEAP_ACK_MESSAGE,
    is_bitbucket_cloud_url,
)
from leap.monitor.pr_tracking.leap_command import _pr_prefix, format_leap_message


# --- helpers ---------------------------------------------------------

def _cloud(username: str = 'me', auth_user: str = '',
           filter_bots: bool = True) -> BitbucketProvider:
    p = BitbucketProvider('https://bitbucket.org', 'tok', username,
                          auth_user=auth_user, filter_bots=filter_bots)
    # Pre-resolve the lazy Cloud identity so _is_me never hits the network
    p._self_ids = ('{uuid-me}', 'acct-me')
    return p


def _server(username: str = 'me',
            filter_bots: bool = True) -> BitbucketProvider:
    return BitbucketProvider('https://bitbucket.corp.com', 'tok', username,
                             filter_bots=filter_bots)


def _cloud_user(name: str, *, bot: bool = False) -> dict:
    return {
        'type': 'app_user' if bot else 'user',
        'uuid': '{uuid-me}' if name == 'me' else f'{{uuid-{name}}}',
        'account_id': 'acct-me' if name == 'me' else f'acct-{name}',
        'nickname': name,
        'display_name': name.title(),
    }


def _server_user(name: str, *, bot: bool = False) -> dict:
    return {
        'name': name,
        'displayName': name.title(),
        'type': 'SERVICE' if bot else 'NORMAL',
    }


def _note(author_user: dict, body: str, note_id: int = 1,
          created: str = '2026-01-01') -> dict:
    return {
        'id': note_id,
        'author_user': author_user,
        'author': author_user.get('nickname') or author_user.get('name', ''),
        'body': body,
        'created_at': created,
    }


def _thread(notes: list[dict], *, resolved: bool = False,
            root_id: int = 100, file_path: Optional[str] = None,
            old_line: Optional[int] = None,
            new_line: Optional[int] = None) -> dict:
    return {
        'root_id': root_id,
        'resolved': resolved,
        'notes': notes,
        'file_path': file_path,
        'old_line': old_line,
        'new_line': new_line,
    }


# --- flavor detection ------------------------------------------------

class TestFlavorDetection:
    def test_cloud_urls(self) -> None:
        assert is_bitbucket_cloud_url('https://bitbucket.org')
        assert is_bitbucket_cloud_url('https://api.bitbucket.org')
        assert is_bitbucket_cloud_url('http://bitbucket.org/')
        assert is_bitbucket_cloud_url('bitbucket.org')

    def test_server_urls(self) -> None:
        assert not is_bitbucket_cloud_url('https://bitbucket.corp.com')
        assert not is_bitbucket_cloud_url('https://stash.corp.com')
        # Lookalike host must not be treated as Cloud
        assert not is_bitbucket_cloud_url('https://notbitbucket.org.evil.com')

    def test_detect_heuristics_are_strictly_additive(self) -> None:
        # Regression: a host containing both 'gitlab' and 'stash' must keep
        # resolving to GITLAB exactly as it did before Bitbucket support.
        from leap.monitor.pr_tracking.git_utils import SCMType, detect_scm_type
        assert detect_scm_type('https://gitlab.stash-team.com') == SCMType.GITLAB
        assert detect_scm_type('https://stash.corp.com') == SCMType.BITBUCKET
        assert detect_scm_type('https://bitbucket.corp.com') == SCMType.BITBUCKET

    def test_detect_context_path_config_by_hostname(self) -> None:
        # A saved context-path URL ('https://git.corp.com/stash') is never a
        # substring of the bare host from a git remote - hostname equality
        # must still classify it.
        from leap.monitor.pr_tracking.git_utils import SCMType, detect_scm_type
        cfg = {'bitbucket_url': 'https://git.corp.com/stash'}
        assert detect_scm_type('https://git.corp.com',
                               bitbucket_config=cfg) == SCMType.BITBUCKET

    def test_parse_urls_with_context_path(self) -> None:
        from leap.monitor.pr_tracking.git_utils import (
            SCMType, parse_pr_url, parse_project_url,
        )
        p = parse_pr_url(
            'https://git.corp.com/stash/projects/PROJ/repos/r/pull-requests/7')
        assert p is not None
        assert p.scm_type == SCMType.BITBUCKET
        assert p.host_url == 'https://git.corp.com/stash'
        assert p.project_path == 'PROJ/r'
        assert p.pr_iid == 7
        proj = parse_project_url('https://git.corp.com/stash/scm/proj/r.git')
        assert proj is not None
        assert proj.host_url == 'https://git.corp.com/stash'
        assert proj.project_path == 'proj/r'
        web = parse_project_url(
            'https://git.corp.com/stash/projects/PROJ/repos/r/browse')
        assert web is not None and web.project_path == 'PROJ/r'
        assert web.host_url == 'https://git.corp.com/stash'

    def test_provider_flavor_and_api_roots(self) -> None:
        p = _cloud()
        assert p._is_cloud
        assert p._api_root == 'https://api.bitbucket.org/2.0'
        s = _server()
        assert not s._is_cloud
        assert s._api_root == 'https://bitbucket.corp.com/rest/api/1.0'
        assert s._build_status_root == 'https://bitbucket.corp.com/rest/build-status/1.0'

    def test_auth_modes(self) -> None:
        basic = BitbucketProvider('https://bitbucket.org', 'tok', 'me',
                                  auth_user='me@corp.com')
        assert basic._session.auth == ('me@corp.com', 'tok')
        bearer = BitbucketProvider('https://bitbucket.org', 'tok', 'me')
        assert bearer._session.auth is None
        assert bearer._session.headers['Authorization'] == 'Bearer tok'


# --- project path normalization --------------------------------------

class TestSplitProject:
    def test_canonical(self) -> None:
        assert BitbucketProvider._split_project('ws/repo') == ('ws', 'repo')

    def test_server_clone_path(self) -> None:
        assert BitbucketProvider._split_project('scm/PROJ/repo') == ('PROJ', 'repo')

    def test_server_web_path(self) -> None:
        assert BitbucketProvider._split_project(
            'projects/PROJ/repos/repo') == ('PROJ', 'repo')

    def test_invalid(self) -> None:
        assert BitbucketProvider._split_project('just-a-name') is None
        assert BitbucketProvider._split_project('a/b/c') is None

    def test_context_path_forms(self) -> None:
        # Server installs under a context path (e.g. /stash) produce
        # remotes whose project path carries leading context segments.
        assert BitbucketProvider._split_project(
            'stash/scm/proj/repo') == ('proj', 'repo')
        assert BitbucketProvider._split_project(
            'stash/projects/PROJ/repos/repo') == ('PROJ', 'repo')
        # A project key literally named SCM must hit the web-path rule
        assert BitbucketProvider._split_project(
            'projects/SCM/repos/repo') == ('SCM', 'repo')

    def test_server_git_path_preserves_context(self) -> None:
        # The git clone path must keep context segments a local remote
        # baked into the stored project path - stripping them 404s the
        # clone on context-path installs.
        f = BitbucketProvider.server_git_path
        assert f('stash/scm/key/slug') == 'stash/scm/key/slug'
        assert f('stash/projects/KEY/repos/slug') == 'stash/scm/KEY/slug'
        assert f('projects/KEY/repos/slug') == 'scm/KEY/slug'
        assert f('KEY/slug') == 'scm/KEY/slug'
        assert f('projects/SCM/repos/slug') == 'scm/SCM/slug'
        assert f('not-a-path') is None

    def test_repo_api_urls(self) -> None:
        p = _cloud()
        assert p._repo_api(('ws', 'r')) == \
            'https://api.bitbucket.org/2.0/repositories/ws/r'
        s = _server()
        assert s._repo_api(('PROJ', 'r')) == \
            'https://bitbucket.corp.com/rest/api/1.0/projects/PROJ/repos/r'

    def test_cloud_pagelen_stays_within_endpoint_cap(self, monkeypatch) -> None:
        # Regression: the Cloud pullrequests list rejects pagelen > 50 with
        # HTTP 400, which get_pr_status would read as a permanent
        # "transient failure".
        p = _cloud()
        captured: list[dict] = []

        def fake_get_json(url: str, params: Any = None) -> dict:
            captured.append(params or {})
            return {'values': []}

        monkeypatch.setattr(p, '_get_json', fake_get_json)
        p._iter_pages('https://api.bitbucket.org/2.0/x')
        assert captured[0].get('pagelen') == 50


# --- identity / bot detection -----------------------------------------

class TestIdentity:
    def test_is_me_cloud_by_uuid(self) -> None:
        p = _cloud()
        assert p._is_me(_cloud_user('me'))
        assert not p._is_me(_cloud_user('reviewer'))

    def test_is_me_server_by_name(self) -> None:
        s = _server()
        assert s._is_me(_server_user('me'))
        assert not s._is_me(_server_user('reviewer'))

    def test_is_me_none_user(self) -> None:
        assert not _cloud()._is_me(None)
        assert not _server()._is_me({})

    def test_self_id_fetch_allowed_right_after_boot(self, monkeypatch) -> None:
        # time.monotonic() starts near zero at boot - the never-attempted
        # sentinel must not suppress the first identity fetch.
        import leap.monitor.pr_tracking.bitbucket_provider as bp
        p = BitbucketProvider('https://bitbucket.org', 'tok', 'me')
        monkeypatch.setattr(bp.time, 'monotonic', lambda: 1.0)
        monkeypatch.setattr(
            p, '_get_json',
            lambda *a, **k: {'uuid': '{u}', 'account_id': 'a'})
        assert p._ensure_self_ids() == ('{u}', 'a')

    def test_poll_interval_includes_bitbucket(self, monkeypatch) -> None:
        # The poll-interval minimum must consider the Bitbucket config -
        # a Bitbucket-only user's chosen interval was ignored otherwise.
        import leap.monitor._mixins.scm_config_mixin as scm
        monkeypatch.setattr(scm, 'load_gitlab_config', lambda: None)
        monkeypatch.setattr(scm, 'load_github_config', lambda: None)
        monkeypatch.setattr(scm, 'load_bitbucket_config',
                            lambda: {'poll_interval': 7})
        assert scm.SCMConfigMixin._get_poll_interval(SimpleNamespace()) == 7

    def test_bot_detection(self) -> None:
        p = _cloud()
        assert p._is_bot(_cloud_user('ci', bot=True))
        assert not p._is_bot(_cloud_user('human'))
        s = _server()
        assert s._is_bot(_server_user('ci', bot=True))
        assert not s._is_bot(_server_user('human'))


# --- unresponded-thread rules -----------------------------------------

class TestIsUnrespondedThread:
    def test_unresponded_when_other_commented_last(self) -> None:
        p = _cloud()
        t = _thread([_note(_cloud_user('reviewer'), 'fix this', 1)])
        assert p._is_unresponded_thread(t)

    def test_responded_when_user_replied(self) -> None:
        p = _cloud()
        t = _thread([
            _note(_cloud_user('reviewer'), 'fix this', 1),
            _note(_cloud_user('me'), 'done', 2),
        ])
        assert not p._is_unresponded_thread(t)

    def test_leap_reply_does_not_count_as_response(self) -> None:
        p = _cloud()
        t = _thread([
            _note(_cloud_user('reviewer'), 'fix this', 1),
            _note(_cloud_user('me'), '/leap', 2),
        ])
        assert p._is_unresponded_thread(t)

    def test_resolved_thread_is_responded(self) -> None:
        p = _cloud()
        t = _thread([_note(_cloud_user('reviewer'), 'fix this', 1)],
                    resolved=True)
        assert not p._is_unresponded_thread(t)

    def test_own_only_thread_is_responded(self) -> None:
        p = _cloud()
        t = _thread([_note(_cloud_user('me'), 'note to self', 1)])
        assert not p._is_unresponded_thread(t)

    def test_bot_comments_filtered(self) -> None:
        p = _cloud(filter_bots=True)
        t = _thread([_note(_cloud_user('ci', bot=True), 'build failed', 1)])
        assert not p._is_unresponded_thread(t)

    def test_bot_comments_kept_when_filter_off(self) -> None:
        p = _cloud(filter_bots=False)
        t = _thread([_note(_cloud_user('ci', bot=True), 'build failed', 1)])
        assert p._is_unresponded_thread(t)

    def test_server_flavor_users(self) -> None:
        s = _server()
        t = _thread([
            _note(_server_user('reviewer'), 'fix this', 1),
            _note(_server_user('me'), 'done', 2),
        ])
        assert not s._is_unresponded_thread(t)
        t2 = _thread([_note(_server_user('reviewer'), 'fix this', 1)])
        assert s._is_unresponded_thread(t2)


# --- /leap detection ---------------------------------------------------

class TestCheckThreadForLeap:
    _PR = {'id': 7, 'title': 'T',
           'links': {'html': {'href': 'https://bitbucket.org/ws/r/pull-requests/7'}}}

    def test_emits_when_user_posted_leap(self) -> None:
        p = _cloud()
        p._fetch_code_snippet = lambda *a, **k: None
        t = _thread([
            _note(_cloud_user('reviewer'), 'Please fix', 1),
            _note(_cloud_user('me'), '/leap', 2),
        ])
        cmd = p._check_thread_for_leap('ws/r', ('ws', 'r'), self._PR, t, 'feat')
        assert cmd is not None
        assert cmd.discussion_id == '100'
        assert cmd.scm_type == 'bitbucket'
        assert cmd.project_path == 'ws/r'

    def test_skips_resolved_thread(self) -> None:
        p = _cloud()
        t = _thread([
            _note(_cloud_user('reviewer'), 'Fix', 1),
            _note(_cloud_user('me'), '/leap', 2),
        ], resolved=True)
        assert p._check_thread_for_leap('ws/r', ('ws', 'r'),
                                        self._PR, t, 'feat') is None

    def test_ack_after_leap_blocks_emission(self) -> None:
        p = _cloud()
        t = _thread([
            _note(_cloud_user('reviewer'), 'Fix', 1),
            _note(_cloud_user('me'), '/leap', 2),
            _note(_cloud_user('me'), LEAP_ACK_MESSAGE, 3),
        ])
        assert p._check_thread_for_leap('ws/r', ('ws', 'r'),
                                        self._PR, t, 'feat') is None

    def test_new_leap_after_ack_re_emits(self) -> None:
        p = _cloud()
        p._fetch_code_snippet = lambda *a, **k: None
        t = _thread([
            _note(_cloud_user('reviewer'), 'Fix', 1),
            _note(_cloud_user('me'), '/leap', 2),
            _note(_cloud_user('me'), LEAP_ACK_MESSAGE, 3),
            _note(_cloud_user('reviewer'), 'Still broken', 4),
            _note(_cloud_user('me'), '/leap', 5),
        ])
        cmd = p._check_thread_for_leap('ws/r', ('ws', 'r'), self._PR, t, 'feat')
        assert cmd is not None

    def test_leap_from_other_user_ignored(self) -> None:
        p = _cloud()
        t = _thread([_note(_cloud_user('reviewer'), '/leap', 1)])
        assert p._check_thread_for_leap('ws/r', ('ws', 'r'),
                                        self._PR, t, 'feat') is None

    def test_message_uses_hash_prefix(self) -> None:
        p = _cloud()
        p._fetch_code_snippet = lambda *a, **k: None
        t = _thread([
            _note(_cloud_user('reviewer'), 'Please fix', 1),
            _note(_cloud_user('me'), '/leap', 2),
        ])
        cmd = p._check_thread_for_leap('ws/r', ('ws', 'r'), self._PR, t, 'feat')
        assert _pr_prefix('bitbucket') == '#'
        assert 'PR #7' in format_leap_message(cmd)

    def test_project_path_passed_through_verbatim(self, monkeypatch) -> None:
        # Regression: _handle_leap_commands matches commands to sessions by
        # comparing cmd.project_path against the session's own (raw) path -
        # e.g. 'scm/proj/repo' from a Server HTTPS clone remote.  The
        # provider must echo the input verbatim, NOT the normalized
        # KEY/slug form, or the emitting session itself fails to match and
        # a "No matching Leap session" reply gets posted to the PR.
        s = _server()
        pr = {'id': 3, 'title': 'T', 'state': 'OPEN',
              'links': {'self': [{'href': 'u'}]}}
        thread = _thread([
            _note(_server_user('reviewer'), 'Fix', 1),
            _note(_server_user('me'), '/leap', 2),
        ])
        monkeypatch.setattr(s, '_find_open_prs', lambda *a, **k: [pr])
        monkeypatch.setattr(s, '_fetch_threads', lambda *a, **k: [thread])
        monkeypatch.setattr(s, '_fetch_code_snippet', lambda *a, **k: None)
        cmds = s.scan_leap_commands('scm/proj/repo', 'feat')
        assert len(cmds) == 1
        assert cmds[0].project_path == 'scm/proj/repo'
        collected = s.collect_unresponded_threads('projects/PROJ/repos/repo',
                                                  'feat')
        assert collected and collected[0].project_path == \
            'projects/PROJ/repos/repo'


# --- thread normalization (Cloud) --------------------------------------

class TestFetchThreadsCloud:
    def _comment(self, cid: int, author: str, raw: str, *,
                 parent: Optional[int] = None, deleted: bool = False,
                 pending: bool = False, resolution: Optional[dict] = None,
                 inline: Optional[dict] = None,
                 created: str = '2026-01-01') -> dict:
        c: dict[str, Any] = {
            'id': cid,
            'user': _cloud_user(author),
            'content': {'raw': raw},
            'created_on': created,
            'deleted': deleted,
            'pending': pending,
        }
        if parent is not None:
            c['parent'] = {'id': parent}
        if resolution is not None:
            c['resolution'] = resolution
        if inline is not None:
            c['inline'] = inline
        return c

    def test_groups_replies_into_threads(self, monkeypatch) -> None:
        p = _cloud()
        comments = [
            self._comment(1, 'reviewer', 'fix this', created='2026-01-01'),
            self._comment(2, 'me', 'done', parent=1, created='2026-01-02'),
            self._comment(3, 'other', 'separate thread', created='2026-01-03'),
        ]
        monkeypatch.setattr(p, '_iter_pages', lambda *a, **k: comments)
        threads = p._fetch_threads(('ws', 'r'), 7)
        assert len(threads) == 2
        first = threads[0]
        assert first['root_id'] == 1
        assert [n['body'] for n in first['notes']] == ['fix this', 'done']

    def test_deleted_and_pending_skipped(self, monkeypatch) -> None:
        p = _cloud()
        comments = [
            self._comment(1, 'reviewer', 'live'),
            self._comment(2, 'reviewer', 'gone', deleted=True),
            self._comment(3, 'me', 'draft', pending=True),
        ]
        monkeypatch.setattr(p, '_iter_pages', lambda *a, **k: comments)
        threads = p._fetch_threads(('ws', 'r'), 7)
        assert len(threads) == 1
        assert threads[0]['notes'][0]['body'] == 'live'

    def test_resolution_marks_thread_resolved(self, monkeypatch) -> None:
        p = _cloud()
        comments = [self._comment(1, 'reviewer', 'fix',
                                  resolution={'user': {}})]
        monkeypatch.setattr(p, '_iter_pages', lambda *a, **k: comments)
        threads = p._fetch_threads(('ws', 'r'), 7)
        assert threads[0]['resolved'] is True

    def test_inline_anchor_extracted(self, monkeypatch) -> None:
        p = _cloud()
        comments = [self._comment(1, 'reviewer', 'fix',
                                  inline={'path': 'src/x.py', 'from': None,
                                          'to': 12})]
        monkeypatch.setattr(p, '_iter_pages', lambda *a, **k: comments)
        t = p._fetch_threads(('ws', 'r'), 7)[0]
        assert t['file_path'] == 'src/x.py'
        assert t['new_line'] == 12
        assert t['old_line'] is None

    def test_orphaned_reply_becomes_own_root(self, monkeypatch) -> None:
        # Parent deleted: child must not crash the walk
        p = _cloud()
        comments = [self._comment(2, 'me', 'reply to ghost', parent=99)]
        monkeypatch.setattr(p, '_iter_pages', lambda *a, **k: comments)
        threads = p._fetch_threads(('ws', 'r'), 7)
        assert len(threads) == 1
        assert threads[0]['root_id'] == 2


# --- thread normalization (Server) --------------------------------------

class TestFetchThreadsServer:
    def _activity(self, cid: int, author: str, text: str, *,
                  replies: Optional[list[dict]] = None,
                  state: Optional[str] = None,
                  anchor: Optional[dict] = None,
                  created: int = 1700000000000) -> dict:
        comment: dict[str, Any] = {
            'id': cid,
            'author': _server_user(author),
            'text': text,
            'createdDate': created,
            'comments': replies or [],
        }
        if state:
            comment['state'] = state
        activity: dict[str, Any] = {'action': 'COMMENTED', 'comment': comment}
        if anchor:
            activity['commentAnchor'] = anchor
        return activity

    def test_flattens_reply_tree(self, monkeypatch) -> None:
        s = _server()
        reply = {'id': 2, 'author': _server_user('me'), 'text': 'done',
                 'createdDate': 1700000001000, 'comments': []}
        acts = [self._activity(1, 'reviewer', 'fix this', replies=[reply])]
        monkeypatch.setattr(s, '_iter_pages', lambda *a, **k: acts)
        threads = s._fetch_threads(('PROJ', 'r'), 7)
        assert len(threads) == 1
        assert [n['body'] for n in threads[0]['notes']] == ['fix this', 'done']

    def test_non_comment_activities_ignored(self, monkeypatch) -> None:
        s = _server()
        acts = [{'action': 'APPROVED', 'user': _server_user('reviewer')},
                self._activity(1, 'reviewer', 'fix')]
        monkeypatch.setattr(s, '_iter_pages', lambda *a, **k: acts)
        assert len(s._fetch_threads(('PROJ', 'r'), 7)) == 1

    def test_reply_activities_do_not_become_roots(self, monkeypatch) -> None:
        # Some Server versions emit a COMMENTED activity per reply whose
        # embedded comment carries a parent - those must not double-count
        # as separate threads.
        s = _server()
        reply = {'id': 2, 'author': _server_user('me'), 'text': 'done',
                 'createdDate': 1700000001000, 'comments': [],
                 'parent': {'id': 1}}
        acts = [
            self._activity(1, 'reviewer', 'fix this',
                           replies=[{k: v for k, v in reply.items()
                                     if k != 'parent'}]),
            {'action': 'COMMENTED', 'commentAction': 'REPLIED',
             'comment': reply},
        ]
        monkeypatch.setattr(s, '_iter_pages', lambda *a, **k: acts)
        threads = s._fetch_threads(('PROJ', 'r'), 7)
        assert len(threads) == 1
        assert threads[0]['root_id'] == 1

    def test_duplicate_root_deduped(self, monkeypatch) -> None:
        s = _server()
        acts = [self._activity(1, 'reviewer', 'fix'),
                self._activity(1, 'reviewer', 'fix (edited)')]
        monkeypatch.setattr(s, '_iter_pages', lambda *a, **k: acts)
        threads = s._fetch_threads(('PROJ', 'r'), 7)
        assert len(threads) == 1
        assert threads[0]['notes'][0]['body'] == 'fix (edited)'

    def test_resolved_state(self, monkeypatch) -> None:
        s = _server()
        acts = [self._activity(1, 'reviewer', 'task', state='RESOLVED')]
        monkeypatch.setattr(s, '_iter_pages', lambda *a, **k: acts)
        assert s._fetch_threads(('PROJ', 'r'), 7)[0]['resolved'] is True

    def test_thread_resolved_flag(self, monkeypatch) -> None:
        # Regression: "Resolve thread" on a normal comment sets
        # threadResolved on the root (state RESOLVED is only for tasks).
        s = _server()
        act = self._activity(1, 'reviewer', 'looks off')
        act['comment']['threadResolved'] = True
        monkeypatch.setattr(s, '_iter_pages', lambda *a, **k: [act])
        thread = s._fetch_threads(('PROJ', 'r'), 7)[0]
        assert thread['resolved'] is True
        assert not s._is_unresponded_thread(thread)

    def test_anchor_fallback_from_comment(self, monkeypatch) -> None:
        # Newer Servers expose the diff anchor on the comment itself; use
        # it when the activity entry lacks commentAnchor.
        s = _server()
        act = self._activity(1, 'reviewer', 'inline note')
        act['comment']['anchor'] = {'path': 'x.py', 'line': 3,
                                    'lineType': 'ADDED'}
        monkeypatch.setattr(s, '_iter_pages', lambda *a, **k: [act])
        thread = s._fetch_threads(('PROJ', 'r'), 7)[0]
        assert thread['file_path'] == 'x.py'
        assert thread['new_line'] == 3

    def test_anchor_line_mapping(self, monkeypatch) -> None:
        s = _server()
        acts = [
            self._activity(1, 'reviewer', 'added line',
                           anchor={'path': 'a.py', 'line': 5,
                                   'lineType': 'ADDED'}),
            self._activity(2, 'reviewer', 'removed line',
                           anchor={'path': 'b.py', 'line': 9,
                                   'lineType': 'REMOVED'}),
        ]
        monkeypatch.setattr(s, '_iter_pages', lambda *a, **k: acts)
        threads = {t['root_id']: t for t in s._fetch_threads(('PROJ', 'r'), 7)}
        assert threads[1]['new_line'] == 5 and threads[1]['old_line'] is None
        assert threads[2]['old_line'] == 9 and threads[2]['new_line'] is None

    def test_interleaved_subreplies_sorted_chronologically(self, monkeypatch) -> None:
        # Regression: depth-first tree order is not chronological.  Reviewer
        # replies to an early comment (12:00) AFTER my later top-level reply
        # (11:00) - in tree order my reply lands last and the thread would
        # wrongly read as responded.
        s = _server()
        sub_reply = {'id': 3, 'author': _server_user('reviewer'),
                     'text': 'still wrong here', 'createdDate': 1700001200000,
                     'comments': []}
        early = {'id': 2, 'author': _server_user('reviewer'),
                 'text': 'see this line', 'createdDate': 1700001000000,
                 'comments': [sub_reply]}
        mine = {'id': 4, 'author': _server_user('me'), 'text': 'fixed',
                'createdDate': 1700001100000, 'comments': []}
        root = {'id': 1, 'author': _server_user('reviewer'), 'text': 'review',
                'createdDate': 1700000900000, 'comments': [early, mine]}
        acts = [{'action': 'COMMENTED', 'comment': root}]
        monkeypatch.setattr(s, '_iter_pages', lambda *a, **k: acts)
        thread = s._fetch_threads(('PROJ', 'r'), 7)[0]
        assert [n['id'] for n in thread['notes']] == [1, 2, 4, 3]
        # The reviewer's 12:00 sub-reply is now last -> unresponded
        assert s._is_unresponded_thread(thread)


# --- get_pr_status flows -------------------------------------------------

def _cloud_pr(pr_id: int = 7, *, draft: bool = False,
              participants: Optional[list[dict]] = None) -> dict:
    return {
        'id': pr_id,
        'title': 'My PR',
        'state': 'OPEN',
        'draft': draft,
        'participants': participants if participants is not None else [],
        'links': {'html': {'href': f'https://bitbucket.org/ws/r/pull-requests/{pr_id}'}},
    }


class TestGetPRStatus:
    def _wire(self, p: BitbucketProvider, *, prs: Any,
              threads: Any = (), checks: Optional[bool] = False,
              monkeypatch: Any) -> None:
        monkeypatch.setattr(p, '_find_open_prs', lambda *a, **k: prs)
        if isinstance(threads, Exception):
            def _boom(*a: Any, **k: Any) -> Any:
                raise threads
            monkeypatch.setattr(p, '_fetch_threads', _boom)
        else:
            monkeypatch.setattr(p, '_fetch_threads',
                                lambda *a, **k: list(threads))
        monkeypatch.setattr(p, '_checks_failed', lambda *a, **k: checks)

    def test_no_pr(self, monkeypatch) -> None:
        p = _cloud()
        self._wire(p, prs=[], monkeypatch=monkeypatch)
        assert p.get_pr_status('ws/r', 'feat').state == PRState.NO_PR

    def test_open_pr_all_responded(self, monkeypatch) -> None:
        p = _cloud()
        self._wire(p, prs=[_cloud_pr()], monkeypatch=monkeypatch)
        st = p.get_pr_status('ws/r', 'feat')
        assert st.state == PRState.ALL_RESPONDED
        assert st.pr_iid == 7
        assert st.pr_url == 'https://bitbucket.org/ws/r/pull-requests/7'
        assert st.approval_known

    def test_unresponded_counts_and_first_url(self, monkeypatch) -> None:
        p = _cloud()
        threads = [
            _thread([_note(_cloud_user('reviewer'), 'fix', 1)], root_id=11),
            _thread([_note(_cloud_user('reviewer'), 'and this', 2)], root_id=22),
            _thread([_note(_cloud_user('me'), 'mine', 3)], root_id=33),
        ]
        self._wire(p, prs=[_cloud_pr()], threads=threads,
                   monkeypatch=monkeypatch)
        st = p.get_pr_status('ws/r', 'feat')
        assert st.state == PRState.UNRESPONDED
        assert st.unresponded_count == 2
        assert st.first_unresponded_note_id == 11
        assert st.first_unresponded_url == \
            'https://bitbucket.org/ws/r/pull-requests/7#comment-11'

    def test_approvals_and_changes_requested_cloud(self, monkeypatch) -> None:
        p = _cloud()
        participants = [
            {'user': _cloud_user('alice'), 'approved': True,
             'state': 'approved'},
            {'user': _cloud_user('bob'), 'approved': False,
             'state': 'changes_requested'},
        ]
        self._wire(p, prs=[_cloud_pr(participants=participants)],
                   monkeypatch=monkeypatch)
        st = p.get_pr_status('ws/r', 'feat')
        assert st.approved
        assert st.approved_by == ['Alice']
        assert st.changes_requested
        assert not st.self_approved

    def test_self_approved_cloud(self, monkeypatch) -> None:
        p = _cloud()
        participants = [{'user': _cloud_user('me'), 'approved': True,
                         'state': 'approved'}]
        self._wire(p, prs=[_cloud_pr(participants=participants)],
                   monkeypatch=monkeypatch)
        assert p.get_pr_status('ws/r', 'feat').self_approved

    def test_approvals_server_reviewers_and_needs_work(self, monkeypatch) -> None:
        s = _server()
        pr = {
            'id': 3, 'title': 'T', 'state': 'OPEN',
            'reviewers': [
                {'user': _server_user('alice'), 'approved': True,
                 'status': 'APPROVED'},
                {'user': _server_user('bob'), 'approved': False,
                 'status': 'NEEDS_WORK'},
            ],
            'participants': [],
            'fromRef': {'latestCommit': 'abc'},
            'links': {'self': [{'href': 'https://bitbucket.corp.com/projects/P/repos/r/pull-requests/3'}]},
        }
        monkeypatch.setattr(s, '_server_pr_conflicted', lambda *a, **k: True)
        self._wire(s, prs=[pr], monkeypatch=monkeypatch)
        st = s.get_pr_status('P/r', 'feat')
        assert st.approved and st.approved_by == ['Alice']
        assert st.changes_requested
        assert st.has_conflicts
        assert not st.draft  # no draft key on this (pre-9.3) Server payload

    def test_server_draft_flag_dc93(self, monkeypatch) -> None:
        # Data Center 9.3+ added draft PRs - the flag must be read on
        # Server too, not just Cloud.
        s = _server()
        pr = {'id': 4, 'title': 'T', 'state': 'OPEN', 'draft': True,
              'reviewers': [], 'participants': [],
              'links': {'self': [{'href': 'u'}]}}
        monkeypatch.setattr(s, '_server_pr_conflicted', lambda *a, **k: False)
        self._wire(s, prs=[pr], monkeypatch=monkeypatch)
        assert s.get_pr_status('P/r', 'feat').draft

    def test_transient_list_failure_keeps_cache(self, monkeypatch) -> None:
        p = _cloud()
        self._wire(p, prs=[_cloud_pr()], monkeypatch=monkeypatch)
        first = p.get_pr_status('ws/r', 'feat')
        assert first.state == PRState.ALL_RESPONDED
        monkeypatch.setattr(p, '_find_open_prs', lambda *a, **k: None)
        again = p.get_pr_status('ws/r', 'feat')
        assert again.state == PRState.ALL_RESPONDED
        assert again.pr_iid == 7

    def test_no_pr_drops_cache(self, monkeypatch) -> None:
        p = _cloud()
        self._wire(p, prs=[_cloud_pr()], monkeypatch=monkeypatch)
        p.get_pr_status('ws/r', 'feat')
        monkeypatch.setattr(p, '_find_open_prs', lambda *a, **k: [])
        assert p.get_pr_status('ws/r', 'feat').state == PRState.NO_PR
        # Cache dropped: a later transient failure must NOT resurrect OPEN
        monkeypatch.setattr(p, '_find_open_prs', lambda *a, **k: None)
        assert p.get_pr_status('ws/r', 'feat').state == PRState.NO_PR

    def test_comment_fetch_failure_carries_prior_count(self, monkeypatch) -> None:
        p = _cloud()
        threads = [_thread([_note(_cloud_user('reviewer'), 'fix', 1)],
                           root_id=11)]
        self._wire(p, prs=[_cloud_pr()], threads=threads,
                   monkeypatch=monkeypatch)
        assert p.get_pr_status('ws/r', 'feat').unresponded_count == 1
        self._wire(p, prs=[_cloud_pr()], threads=RuntimeError('boom'),
                   monkeypatch=monkeypatch)
        st = p.get_pr_status('ws/r', 'feat')
        assert st.state == PRState.UNRESPONDED
        assert st.unresponded_count == 1

    def test_full_fetch_failure_marks_approval_unknown(self, monkeypatch) -> None:
        p = _cloud()
        listed = {'id': 7, 'title': 'T', 'state': 'OPEN',
                  'links': {'html': {'href': 'https://bitbucket.org/ws/r/pull-requests/7'}}}
        monkeypatch.setattr(p, '_find_open_prs', lambda *a, **k: [listed])
        monkeypatch.setattr(p, '_get_full_pr', lambda *a, **k: None)
        st = p.get_pr_status('ws/r', 'feat')
        assert st.state == PRState.ALL_RESPONDED
        assert not st.approval_known

    def test_draft_flag_cloud(self, monkeypatch) -> None:
        p = _cloud()
        self._wire(p, prs=[_cloud_pr(draft=True)], monkeypatch=monkeypatch)
        assert p.get_pr_status('ws/r', 'feat').draft

    def test_cloud_draft_state_counts_as_open(self, monkeypatch) -> None:
        # Cloud's state enum includes DRAFT and QUEUED alongside OPEN; a
        # tracked draft PR must not read as "No PR".
        p = _cloud()
        pr = _cloud_pr(draft=True)
        pr['state'] = 'DRAFT'
        monkeypatch.setattr(p, '_get_json', lambda *a, **k: pr)
        found = p._find_open_prs(('ws', 'r'), 'feat', pr_iid=7)
        assert found == [pr]

    def test_direct_fetch_contract_mirrors_github(self, monkeypatch) -> None:
        # pr_iid given: closed -> [] (merged-badge flow), 404 -> [] (PR
        # deleted), other failure -> None (transient, keep cached state).
        # Never falls through to branch listing.
        p = _cloud()
        closed = _cloud_pr()
        closed['state'] = 'MERGED'
        monkeypatch.setattr(p, '_get_json', lambda *a, **k: closed)
        assert p._find_open_prs(('ws', 'r'), 'feat', pr_iid=7) == []

        def _raise_404(*a: Any, **k: Any) -> None:
            err = requests.HTTPError()
            err.response = SimpleNamespace(status_code=404)
            raise err

        monkeypatch.setattr(p, '_get_json', _raise_404)
        assert p._find_open_prs(('ws', 'r'), 'feat', pr_iid=7) == []

        def _raise_boom(*a: Any, **k: Any) -> None:
            raise RuntimeError('network down')

        monkeypatch.setattr(p, '_get_json', _raise_boom)
        assert p._find_open_prs(('ws', 'r'), 'feat', pr_iid=7) is None

    def test_status_cache_keyed_by_pr_iid(self, monkeypatch) -> None:
        # Two PRs sharing a source branch must not share a cache slot - a
        # transient failure polling PR 9 must not resurrect PR 7's status.
        p = _cloud()
        self._wire(p, prs=[_cloud_pr(pr_id=7)], monkeypatch=monkeypatch)
        assert p.get_pr_status('ws/r', 'feat', pr_iid=7).pr_iid == 7
        monkeypatch.setattr(p, '_find_open_prs', lambda *a, **k: None)
        st = p.get_pr_status('ws/r', 'feat', pr_iid=9)
        assert st.state == PRState.NO_PR
        # PR 7's own cached status is still served for PR 7
        assert p.get_pr_status('ws/r', 'feat', pr_iid=7).pr_iid == 7

    def test_cloud_branch_query_excludes_closed_states(self, monkeypatch) -> None:
        # The listing query must exclude closed states (rather than match
        # state = "OPEN") so DRAFT/QUEUED PRs stay tracked.
        p = _cloud()
        captured: list[dict] = []

        def fake_iter(url: str, params: Any = None, page_cap: int = 0) -> list:
            captured.append(params or {})
            return []

        monkeypatch.setattr(p, '_iter_pages', fake_iter)
        p._find_open_prs(('ws', 'r'), 'feat', pr_iid=None)
        q = captured[0]['q']
        assert 'state != "MERGED"' in q
        assert 'state != "DECLINED"' in q
        assert 'state != "SUPERSEDED"' in q
        assert 'source.branch.name = "feat"' in q


# --- deep links ----------------------------------------------------------

class TestFirstUnrespondedUrl:
    def test_cloud_anchor(self) -> None:
        p = _cloud()
        assert p.build_first_unresponded_url(
            'https://bitbucket.org/ws/r/pull-requests/7', 42) == \
            'https://bitbucket.org/ws/r/pull-requests/7#comment-42'

    def test_server_overview_query(self) -> None:
        s = _server()
        assert s.build_first_unresponded_url(
            'https://bitbucket.corp.com/projects/P/repos/r/pull-requests/3',
            42) == ('https://bitbucket.corp.com/projects/P/repos/r/'
                    'pull-requests/3/overview?commentId=42')

    def test_server_url_already_on_overview(self) -> None:
        s = _server()
        assert s.build_first_unresponded_url(
            'https://bitbucket.corp.com/projects/P/repos/r/pull-requests/3/overview',
            42) == ('https://bitbucket.corp.com/projects/P/repos/r/'
                    'pull-requests/3/overview?commentId=42')


# --- notifications --------------------------------------------------------

class TestNotifications:
    def test_cloud_does_not_support(self) -> None:
        p = _cloud()
        assert not p.supports_notifications()
        assert p.get_user_notifications() == []

    def test_server_supports(self) -> None:
        assert _server().supports_notifications()

    def test_server_dashboard_mapping(self, monkeypatch) -> None:
        s = _server()
        prs = [
            {  # normal review request
                'id': 1, 'title': 'Review me', 'createdDate': 1700000000000,
                'author': {'user': _server_user('alice')},
                'reviewers': [{'user': _server_user('me'), 'approved': False}],
                'toRef': {'repository': {'slug': 'r1',
                                         'project': {'key': 'PROJ'}}},
                'links': {'self': [{'href': 'https://bitbucket.corp.com/projects/PROJ/repos/r1/pull-requests/1'}]},
            },
            {  # self-authored: skipped
                'id': 2, 'title': 'Mine', 'createdDate': 1700000000000,
                'author': {'user': _server_user('me')},
                'reviewers': [],
                'toRef': {'repository': {'slug': 'r1',
                                         'project': {'key': 'PROJ'}}},
                'links': {'self': [{'href': 'x'}]},
            },
            {  # already approved by me: skipped
                'id': 3, 'title': 'Done', 'createdDate': 1700000000000,
                'author': {'user': _server_user('bob')},
                'reviewers': [{'user': _server_user('me'), 'approved': True}],
                'toRef': {'repository': {'slug': 'r1',
                                         'project': {'key': 'PROJ'}}},
                'links': {'self': [{'href': 'x'}]},
            },
        ]
        resp = SimpleNamespace(status_code=200,
                               json=lambda: {'values': prs})
        monkeypatch.setattr(s, '_request', lambda *a, **k: resp)
        notifs = s.get_user_notifications()
        assert len(notifs) == 1
        n = notifs[0]
        assert n.id == 'PROJ/r1#1'
        assert n.scm_type == 'bitbucket'
        assert n.reason == 'review_requested'
        assert n.author == 'alice'
        assert n.project_name == 'PROJ/r1'

    def test_server_403_raises_auth_error_with_status(self, monkeypatch) -> None:
        s = _server()
        resp = SimpleNamespace(status_code=403, json=lambda: {})
        monkeypatch.setattr(s, '_request', lambda *a, **k: resp)
        with pytest.raises(BitbucketAuthError) as exc_info:
            s.get_user_notifications()
        # The SCM poll worker detects auth errors via getattr(exc, 'status')
        assert getattr(exc_info.value, 'status', None) == 403

    def test_server_401_is_transient_not_auth_disable(self, monkeypatch) -> None:
        # Regression: only 403 may auto-disable notification tracking
        # (matching GitLab) - a 401 can be a token mid-rotation and must
        # not flip enable_notifications off on disk.
        s = _server()
        resp = SimpleNamespace(status_code=401, json=lambda: {})
        monkeypatch.setattr(s, '_request', lambda *a, **k: resp)
        assert s.get_user_notifications() == []


# --- find_latest_closed_pr -------------------------------------------------

class TestFindLatestClosedPR:
    def test_prefers_merged_over_declined(self, monkeypatch) -> None:
        p = _cloud()
        calls: list[str] = []

        def fake_get_json(url: str, params: Any = None) -> dict:
            q = (params or {}).get('q', '')
            state = 'MERGED' if 'MERGED' in q else 'DECLINED'
            calls.append(state)
            return {'values': [{'id': 5, 'title': f'{state} pr',
                                'links': {'html': {'href': 'u'}}}]}

        monkeypatch.setattr(p, '_get_json', fake_get_json)
        info = p.find_latest_closed_pr('ws/r', 'feat')
        assert info is not None and info.merged
        assert calls == ['MERGED']

    def test_falls_back_to_declined(self, monkeypatch) -> None:
        s = _server()

        def fake_get_json(url: str, params: Any = None) -> dict:
            if (params or {}).get('state') == 'MERGED':
                return {'values': []}
            return {'values': [{'id': 9, 'title': 'nope',
                                'links': {'self': [{'href': 'u'}]}}]}

        monkeypatch.setattr(s, '_get_json', fake_get_json)
        info = s.find_latest_closed_pr('PROJ/r', 'feat')
        assert info is not None and not info.merged and info.pr_iid == 9

    def test_none_when_nothing_found(self, monkeypatch) -> None:
        p = _cloud()
        monkeypatch.setattr(p, '_get_json',
                            lambda *a, **k: {'values': []})
        assert p.find_latest_closed_pr('ws/r', 'feat') is None


# --- replies ---------------------------------------------------------------

class TestPostReply:
    def test_cloud_payload_shape(self, monkeypatch) -> None:
        p = _cloud()
        captured: dict[str, Any] = {}

        def fake_request(method: str, url: str, params: Any = None,
                         json_body: Any = None) -> Any:
            captured.update(method=method, url=url, body=json_body)
            return SimpleNamespace(status_code=201,
                                   raise_for_status=lambda: None)

        monkeypatch.setattr(p, '_request', fake_request)
        assert p.acknowledge_leap_command('ws/r', 7, '100')
        assert captured['method'] == 'POST'
        assert captured['url'].endswith('/repositories/ws/r/pullrequests/7/comments')
        assert captured['body'] == {'content': {'raw': LEAP_ACK_MESSAGE},
                                    'parent': {'id': 100}}

    def test_server_payload_shape(self, monkeypatch) -> None:
        s = _server()
        captured: dict[str, Any] = {}

        def fake_request(method: str, url: str, params: Any = None,
                         json_body: Any = None) -> Any:
            captured.update(method=method, url=url, body=json_body)
            return SimpleNamespace(status_code=201,
                                   raise_for_status=lambda: None)

        monkeypatch.setattr(s, '_request', fake_request)
        assert s.report_no_session('PROJ/r', 3, '55')
        assert captured['url'].endswith(
            '/projects/PROJ/repos/r/pull-requests/3/comments')
        assert captured['body']['parent'] == {'id': 55}
        assert 'No matching Leap session' in captured['body']['text']

    def test_invalid_discussion_id(self) -> None:
        assert not _cloud().acknowledge_leap_command('ws/r', 7, 'not-an-int')


# --- clone/fetch URL integration seams --------------------------------------

class TestValidationAuthUrls:
    def _write_cfg(self, storage: Path, cfg: dict) -> None:
        (storage / 'bitbucket_config.json').write_text(json.dumps(cfg))

    def test_cloud_with_auth_user(self, tmp_path: Path) -> None:
        from leap.server.validation import build_auth_fetch_url
        self._write_cfg(tmp_path, {'token': 'tok',
                                   'auth_user': 'me@corp.com',
                                   'username': 'me'})
        pinned = {'host_url': 'https://bitbucket.org',
                  'remote_project_path': 'ws/repo', 'scm_type': 'bitbucket'}
        assert build_auth_fetch_url(pinned, tmp_path) == \
            'https://me%40corp.com:tok@bitbucket.org/ws/repo.git'

    def test_cloud_bearer_token_uses_x_token_auth(self, tmp_path: Path) -> None:
        from leap.server.validation import build_auth_fetch_url
        self._write_cfg(tmp_path, {'token': 'tok', 'auth_user': '',
                                   'username': 'me'})
        pinned = {'host_url': 'https://bitbucket.org',
                  'remote_project_path': 'ws/repo', 'scm_type': 'bitbucket'}
        assert build_auth_fetch_url(pinned, tmp_path) == \
            'https://x-token-auth:tok@bitbucket.org/ws/repo.git'

    def test_server_uses_scm_path_and_username(self, tmp_path: Path) -> None:
        from leap.server.validation import build_auth_fetch_url
        self._write_cfg(tmp_path, {'token': 'tok', 'auth_user': '',
                                   'username': 'me'})
        pinned = {'host_url': 'https://bitbucket.corp.com',
                  'remote_project_path': 'PROJ/repo',
                  'scm_type': 'bitbucket'}
        assert build_auth_fetch_url(pinned, tmp_path) == \
            'https://me:tok@bitbucket.corp.com/scm/PROJ/repo.git'

    def test_server_web_path_normalized(self, tmp_path: Path) -> None:
        from leap.server.validation import build_auth_fetch_url
        self._write_cfg(tmp_path, {'token': 'tok', 'auth_user': '',
                                   'username': 'me'})
        pinned = {'host_url': 'https://bitbucket.corp.com',
                  'remote_project_path': 'projects/PROJ/repos/repo',
                  'scm_type': 'bitbucket'}
        assert build_auth_fetch_url(pinned, tmp_path) == \
            'https://me:tok@bitbucket.corp.com/scm/PROJ/repo.git'

    def test_server_fetch_keeps_context_in_path(self, tmp_path: Path) -> None:
        from leap.server.validation import build_auth_fetch_url
        self._write_cfg(tmp_path, {'token': 'tok', 'auth_user': '',
                                   'username': 'me'})
        pinned = {'host_url': 'https://bitbucket.corp.com',
                  'remote_project_path': 'stash/scm/key/slug',
                  'scm_type': 'bitbucket'}
        assert build_auth_fetch_url(pinned, tmp_path) == \
            'https://me:tok@bitbucket.corp.com/stash/scm/key/slug.git'

    def test_project_key_comparison(self) -> None:
        from leap.server.validation import _bitbucket_project_key
        assert _bitbucket_project_key('scm/proj/repo') == \
            _bitbucket_project_key('PROJ/repo')
        assert _bitbucket_project_key('projects/PROJ/repos/repo') == \
            'proj/repo'
        # Context-path clone remotes carry leading segments
        assert _bitbucket_project_key('stash/scm/proj/repo') == 'proj/repo'


class TestServerLauncherCloneUrl:
    def _launcher(self, provider: Any) -> Any:
        from leap.monitor.server_launcher import ServerLauncher
        launcher = ServerLauncher.__new__(ServerLauncher)
        launcher._w = SimpleNamespace(
            _scm_providers={'bitbucket': provider} if provider else {})
        return launcher

    def test_cloud_clone_with_auth_user(self) -> None:
        p = _cloud(auth_user='me@corp.com')
        launcher = self._launcher(p)
        url = launcher._build_clone_url(
            'https://bitbucket.org', 'ws/repo', 'bitbucket')
        assert url == 'https://me%40corp.com:tok@bitbucket.org/ws/repo.git'

    def test_server_clone_inserts_scm_prefix(self) -> None:
        s = _server(username='me')
        launcher = self._launcher(s)
        url = launcher._build_clone_url(
            'https://bitbucket.corp.com', 'PROJ/repo', 'bitbucket')
        assert url == 'https://me:tok@bitbucket.corp.com/scm/PROJ/repo.git'

    def test_server_clone_normalizes_web_path(self) -> None:
        s = _server(username='me')
        launcher = self._launcher(s)
        url = launcher._build_clone_url(
            'https://bitbucket.corp.com', 'projects/PROJ/repos/repo',
            'bitbucket')
        assert url == 'https://me:tok@bitbucket.corp.com/scm/PROJ/repo.git'

    def test_server_clone_keeps_context_in_path(self) -> None:
        # A pin derived from a context-path install's local remote stores
        # 'stash/scm/key/slug' with a bare host_url - the context must
        # survive into the clone URL.
        s = _server(username='me')
        launcher = self._launcher(s)
        url = launcher._build_clone_url(
            'https://bitbucket.corp.com', 'stash/scm/key/slug', 'bitbucket')
        assert url == 'https://me:tok@bitbucket.corp.com/stash/scm/key/slug.git'

    def test_no_provider_falls_back_unauthenticated(self) -> None:
        launcher = self._launcher(None)
        url = launcher._build_clone_url(
            'https://bitbucket.corp.com', 'PROJ/repo', 'bitbucket')
        assert url == 'https://bitbucket.corp.com/scm/PROJ/repo.git'
