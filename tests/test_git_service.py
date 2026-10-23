"""Tests for the Git service: anonymous fetch of exactly the pinned commit.

App repositories are public (.github#7 B): no token is sent or written into
the clone, only allowlisted hosts are fetched from, and the commit is the
one the API pinned (.github#7 G). GitPython is mocked.
"""

import importlib
from unittest.mock import MagicMock, patch

import git
import pytest

from app.git_urls import GitHostNotAllowed
from app.services.git_service import GitService

# The module, not the ``git_service`` singleton that ``app.services`` re-exports.
git_module = importlib.import_module("app.services.git_service")

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(git_module.settings, "TEMP_REPO_BASE_PATH", str(tmp_path))
    monkeypatch.setattr(git_module.settings, "APP_GIT_ALLOWED_HOSTS", "github.com")
    return GitService()


def _repo(head: str = SHA) -> MagicMock:
    repo = MagicMock(name="Repo")
    repo.create_remote.return_value = MagicMock(name="Origin")
    repo.head.commit.hexsha = head
    return repo


def test_no_token_anywhere():
    assert not hasattr(git_module.settings, "GIT_ACCESS_TOKEN")
    assert not hasattr(GitService(), "token")


@patch("app.services.git_service.git.Repo")
def test_fetches_the_commit_anonymously_and_checks_it_out(mock_repo_cls, service, tmp_path):
    repo = _repo()
    mock_repo_cls.init.return_value = repo

    path = service.clone_commit("git@github.com:acme/app.git", "v1", SHA, "dep-1")

    assert path == str(tmp_path / "deploy_dep-1")
    repo.create_remote.assert_called_once_with("origin", "https://github.com/acme/app.git")
    origin = repo.create_remote.return_value
    assert origin.fetch.call_args.args[0] == SHA
    assert origin.fetch.call_args.kwargs["depth"] == 1
    repo.git.checkout.assert_called_once_with("--detach", SHA, force=True)
    assert repo.git.update_environment.call_args.kwargs["GIT_TERMINAL_PROMPT"] == "0"


@patch("app.services.git_service.git.Repo")
def test_falls_back_to_the_tag_when_the_server_refuses_a_sha(mock_repo_cls, service):
    repo = _repo()
    origin = repo.create_remote.return_value
    origin.fetch.side_effect = [git.GitCommandError(["git", "fetch"], 128), None]
    mock_repo_cls.init.return_value = repo

    service.clone_commit("https://github.com/acme/app", "v1", SHA, "dep-1")

    assert origin.fetch.call_args.kwargs["refspec"] == "refs/tags/v1:refs/tags/v1"


@patch("app.services.git_service.git.Repo")
def test_a_moved_tag_fails_and_removes_the_clone(mock_repo_cls, service, tmp_path):
    mock_repo_cls.init.side_effect = lambda path: (path.mkdir(parents=True, exist_ok=True), _repo("f" * 40))[1]

    with pytest.raises(Exception, match="expected the pinned commit"):
        service.clone_commit("https://github.com/acme/app", "v1", SHA, "dep-1")
    assert not (tmp_path / "deploy_dep-1").exists()


@pytest.mark.parametrize("commit", [None, "", "v1.0.0", SHA[:8]])
def test_refuses_to_clone_without_a_pinned_commit(service, commit):
    with pytest.raises(Exception, match="no pinned commit"):
        service.clone_commit("https://github.com/acme/app", "v1", commit, "dep-1")


@pytest.mark.parametrize(
    "url", ["https://evil.example/acme/app", "https://github.com:444/acme/app", "file:///etc/passwd"]
)
def test_refuses_hosts_outside_the_allowlist(service, url):
    with pytest.raises(GitHostNotAllowed):
        service.clone_commit(url, "v1", SHA, "dep-1")


@patch("app.services.git_service.git.Repo")
def test_git_errors_become_readable_and_leave_nothing_behind(mock_repo_cls, service, tmp_path):
    def init(path):
        path.mkdir(parents=True, exist_ok=True)
        repo = _repo()
        repo.create_remote.return_value.fetch.side_effect = git.GitCommandError(
            ["git", "fetch"], 128, stderr="fatal: repository not found"
        )
        return repo

    mock_repo_cls.init.side_effect = init
    with pytest.raises(Exception, match="repository not found"):
        service.clone_commit("https://github.com/acme/app", "v1", SHA, "dep-1")
    assert not (tmp_path / "deploy_dep-1").exists()


def test_cleanup_repository(service, tmp_path):
    target = tmp_path / "deploy_x"
    target.mkdir()
    service.cleanup_repository(str(target))
    assert not target.exists()
    service.cleanup_repository(str(target))  # gone already: no error
