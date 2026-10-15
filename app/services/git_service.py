"""Fetch exactly the commit a deployment is pinned to.

App repositories are public and read anonymously (.github#7 B): no token is
sent anywhere or written into the clone, and only hosts in
``APP_GIT_ALLOWED_HOSTS`` are fetched from (``app.git_urls``, the same check
the API applies). The commit is the one the API pinned when the deployment
was created (.github#7 G); a tag is never resolved here, so a tag moved after
the review cannot change what runs.
"""

import logging
import shutil
from pathlib import Path

import git

from ..config import settings
from ..git_urls import checked_https_url, is_commit_sha, parse_allowed_hosts

logger = logging.getLogger(__name__)

FETCH_TIMEOUT_SECONDS = 300
# Never ask for or look up credentials: a private or missing repository
# fails instead of hanging on a prompt.
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}


class GitService:
    """Shallow, anonymous fetches of one commit into a per-deployment directory."""

    def __init__(self) -> None:
        self.base_path = Path(settings.TEMP_REPO_BASE_PATH)

    def clone_commit(self, git_url: str, tag: str, commit_sha: str, deployment_id: str) -> str:
        """Fetch ``commit_sha`` of ``git_url`` and check it out detached.

        The commit itself is fetched (GitHub, GitLab and Forgejo serve any
        reachable commit); a server that refuses gets ``tag`` fetched
        instead, which must then still point at ``commit_sha``. Raises with
        a readable message on a missing pin, a host outside the allowlist,
        an unreadable repository or a moved tag; the partial clone is
        removed.
        """
        if not is_commit_sha(commit_sha):
            raise Exception("the deployment has no pinned commit; refusing to clone by tag name")
        url = checked_https_url(git_url, parse_allowed_hosts(settings.APP_GIT_ALLOWED_HOSTS))
        repo_path = self.base_path / f"deploy_{deployment_id}"
        if repo_path.exists():
            logger.info("Removing existing repo at %s", repo_path)
            shutil.rmtree(repo_path)
        try:
            logger.info("Fetching %s at %s (%s)", url, commit_sha[:8], tag)
            repo = git.Repo.init(repo_path)
            repo.git.update_environment(**_GIT_ENV)
            with repo.config_writer() as cfg:
                cfg.set_value("credential", "helper", "")
            origin = repo.create_remote("origin", url)
            try:
                origin.fetch(commit_sha, depth=1, kill_after_timeout=FETCH_TIMEOUT_SECONDS)
            except git.GitCommandError:
                logger.info("fetching the commit was refused; fetching tag %s instead", tag)
                origin.fetch(
                    refspec=f"refs/tags/{tag}:refs/tags/{tag}", depth=1,
                    kill_after_timeout=FETCH_TIMEOUT_SECONDS,
                )
            try:
                repo.git.checkout("--detach", commit_sha, force=True)
            except git.GitCommandError as e:
                raise Exception(
                    f"commit {commit_sha[:8]} is not available (tag {tag} was moved and the "
                    "server does not serve the commit directly)"
                ) from e
            head = repo.head.commit.hexsha
            if head != commit_sha:
                raise Exception(f"fetched {head[:8]}, expected the pinned commit {commit_sha[:8]}")
            logger.info("Checked out %s", head)
            return str(repo_path)
        except git.GitCommandError as e:
            shutil.rmtree(repo_path, ignore_errors=True)
            raise Exception(f"Git command failed: {(e.stderr or '').strip() or e}") from e
        except BaseException:
            shutil.rmtree(repo_path, ignore_errors=True)
            raise

    def cleanup_repository(self, repo_path: str) -> None:
        """Delete the cloned repository."""
        path = Path(repo_path)
        if path.exists():
            logger.info("Cleaning up %s", repo_path)
            shutil.rmtree(path, ignore_errors=True)


# Singleton
git_service = GitService()
