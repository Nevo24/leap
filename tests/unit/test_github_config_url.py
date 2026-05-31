"""Tests for GitHub Enterprise base-URL normalization.

GitHub Enterprise Server exposes its REST API under ``/api/v3`` (and GraphQL
under ``/api/graphql``).  ``GitHubProvider`` already assumes the stored base
URL carries that suffix, so a user who types just the host must have it added
for them — otherwise both the REST client and resolved-thread queries break.
These exercise the normalization helper, the load/save self-heal, and the SCM
host-comparison strip, without touching the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leap.monitor.pr_tracking import config as cfg
from leap.monitor.pr_tracking import git_utils as gu


class TestNormalizeGithubApiUrl:
    @pytest.mark.parametrize('inp,exp', [
        ('', ''),
        ('https://github.com', ''),
        ('http://github.com', ''),
        ('github.com', ''),
        ('https://github.com/', ''),
        ('https://api.github.com', 'https://api.github.com'),
        ('https://api.github.com/', 'https://api.github.com'),
        ('https://github.acme.com', 'https://github.acme.com/api/v3'),
        ('https://github.acme.com/', 'https://github.acme.com/api/v3'),
        ('https://github.acme.com/api/v3', 'https://github.acme.com/api/v3'),
        ('https://github.acme.com/api/v3/', 'https://github.acme.com/api/v3'),
    ])
    def test_cases(self, inp: str, exp: str) -> None:
        assert cfg.normalize_github_api_url(inp) == exp

    def test_idempotent(self) -> None:
        once = cfg.normalize_github_api_url('https://github.acme.com')
        assert cfg.normalize_github_api_url(once) == once

    def test_host_ending_in_api_github_com_is_not_treated_as_canonical(self) -> None:
        # A real GHE host that merely *ends* with 'api.github.com' must still
        # get the /api/v3 suffix — only the exact canonical host is special.
        assert cfg.normalize_github_api_url(
            'https://myapi.github.com') == 'https://myapi.github.com/api/v3'

    def test_preserves_original_casing_of_host(self) -> None:
        # Only the trailing slash is stripped; we don't lowercase the host
        # (PyGithub is fine with the user's casing).
        assert cfg.normalize_github_api_url(
            'https://GitHub.ACME.com') == 'https://GitHub.ACME.com/api/v3'


class TestLoadSaveGithubConfig:
    @pytest.fixture(autouse=True)
    def _tmp_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cfg, 'GITHUB_CONFIG_FILE', tmp_path / 'github_config.json')

    def test_save_normalizes_enterprise_url(self) -> None:
        cfg.save_github_config(
            {'github_url': 'https://github.acme.com', 'token': 'x', 'username': 'me'})
        on_disk = json.loads(cfg.GITHUB_CONFIG_FILE.read_text())
        assert on_disk['github_url'] == 'https://github.acme.com/api/v3'

    def test_save_canonical_github_com_becomes_empty(self) -> None:
        cfg.save_github_config(
            {'github_url': 'https://github.com', 'token': 'x', 'username': 'me'})
        on_disk = json.loads(cfg.GITHUB_CONFIG_FILE.read_text())
        assert on_disk['github_url'] == ''

    def test_load_normalizes_legacy_url_in_memory(self) -> None:
        # A config saved before this fix has the bare host.
        cfg.GITHUB_CONFIG_FILE.write_text(json.dumps(
            {'github_url': 'https://github.acme.com', 'token': 'x', 'username': 'me'}))
        loaded = cfg.load_github_config()
        assert loaded is not None
        assert loaded['github_url'] == 'https://github.acme.com/api/v3'

    def test_load_does_not_write_back_to_disk(self) -> None:
        # load() must be side-effect-free: it runs on background poll threads,
        # so a write-back could clobber a concurrent save on the main thread.
        cfg.GITHUB_CONFIG_FILE.write_text(json.dumps(
            {'github_url': 'https://github.acme.com', 'token': 'x', 'username': 'me'}))
        before = cfg.GITHUB_CONFIG_FILE.read_text()
        cfg.load_github_config()
        assert cfg.GITHUB_CONFIG_FILE.read_text() == before  # unchanged on disk

    def test_load_no_rewrite_when_already_canonical(self) -> None:
        cfg.GITHUB_CONFIG_FILE.write_text(json.dumps(
            {'github_url': 'https://github.acme.com/api/v3', 'token': 'x', 'username': 'me'}))
        before = cfg.GITHUB_CONFIG_FILE.read_text()
        cfg.load_github_config()
        assert cfg.GITHUB_CONFIG_FILE.read_text() == before

    def test_load_missing_file_returns_none(self) -> None:
        assert cfg.load_github_config() is None

    def test_load_non_dict_returns_none(self) -> None:
        cfg.GITHUB_CONFIG_FILE.write_text('["not", "a", "dict"]')
        assert cfg.load_github_config() is None

    def test_load_corrupt_json_returns_none(self) -> None:
        cfg.GITHUB_CONFIG_FILE.write_text('{not valid json')
        assert cfg.load_github_config() is None

    def test_round_trip_stays_canonical(self) -> None:
        cfg.save_github_config(
            {'github_url': 'https://github.acme.com', 'token': 'x', 'username': 'me'})
        loaded = cfg.load_github_config()
        assert loaded is not None and loaded['github_url'] == 'https://github.acme.com/api/v3'


class TestDetectScmTypeApiV3Strip:
    def test_strips_api_v3_before_host_compare(self) -> None:
        # git remote gives the bare host; saved config now carries /api/v3.
        github_config = {'github_url': 'https://github.acme.com/api/v3'}
        assert gu.detect_scm_type(
            'https://github.acme.com', github_config=github_config) == gu.SCMType.GITHUB

    def test_bare_saved_url_still_matches(self) -> None:
        github_config = {'github_url': 'https://github.acme.com'}
        assert gu.detect_scm_type(
            'https://github.acme.com', github_config=github_config) == gu.SCMType.GITHUB

    def test_none_urls_do_not_crash(self) -> None:
        assert gu.detect_scm_type(
            'https://x.com',
            github_config={'github_url': None},
            gitlab_config={'gitlab_url': None}) == gu.SCMType.UNKNOWN
