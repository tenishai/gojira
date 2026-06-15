"""GitHub + git operations behind an interface.

Same pattern as the gcloud/kubectl clients: steps depend on the
GitHubClient ABC; the v0.1 backend shells out to `git` and `gh` using the
operator's existing `gh auth` session. A later GitHub App backend can
implement the same interface for the hosted, webhook-driven flow.
"""

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from gojira.shell import CommandRunner

log = logging.getLogger(__name__)

SETUP_BRANCH = "gojira/setup"
COMMIT_AUTHOR_NAME = "Gojira"
COMMIT_AUTHOR_EMAIL = "noreply@gojira.dev"


class GitHubError(RuntimeError):
    """A git or gh command failed."""


class GitHubClient(ABC):
    """The GitHub/git operations the provisioner needs."""

    @abstractmethod
    def list_repos(self, limit: int = 200) -> list[str]:
        """owner/repo names the operator can deploy, most-recently-pushed first."""
        ...

    @abstractmethod
    def default_branch(self, repo: str) -> str: ...

    @abstractmethod
    def clone(self, repo: str, workdir: Path) -> None: ...

    @abstractmethod
    def checkout_branch(self, workdir: Path, branch: str) -> None: ...

    @abstractmethod
    def commit_all(self, workdir: Path, message: str) -> bool:
        """Stage and commit everything. Returns False if there was nothing to commit."""
        ...

    @abstractmethod
    def push(self, workdir: Path, branch: str) -> None: ...

    @abstractmethod
    def existing_pr(self, repo: str, head: str) -> str | None:
        """URL of an open PR for head, or None."""
        ...

    @abstractmethod
    def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> str:
        """Open a PR; return its URL."""
        ...


class GhCLI(GitHubClient):
    """git + gh backed client using the operator's gh auth session."""

    def __init__(self, runner: CommandRunner | None = None, git="git", gh="gh"):
        self._runner = runner or CommandRunner()
        self._git = git
        self._gh = gh

    def _exec(self, args: list[str], stdin: str | None = None) -> str:
        result = self._runner.run(args, stdin=stdin)
        if result.returncode != 0:
            raise GitHubError(f"{' '.join(args)} failed: {result.stderr or result.stdout}")
        return result.stdout

    def _git_in(self, workdir: Path, *args: str) -> str:
        return self._exec([self._git, "-C", str(workdir), *args])

    def list_repos(self, limit: int = 200) -> list[str]:
        out = self._exec(
            [self._gh, "repo", "list", "--no-archived", "--limit", str(limit),
             "--json", "nameWithOwner", "--jq", ".[].nameWithOwner"]
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    def default_branch(self, repo: str) -> str:
        return self._exec(
            [self._gh, "repo", "view", repo, "--json", "defaultBranchRef",
             "--jq", ".defaultBranchRef.name"]
        )

    def clone(self, repo: str, workdir: Path) -> None:
        self._exec([self._gh, "repo", "clone", repo, str(workdir)])

    def checkout_branch(self, workdir: Path, branch: str) -> None:
        self._git_in(workdir, "checkout", "-B", branch)

    def commit_all(self, workdir: Path, message: str) -> bool:
        self._git_in(workdir, "add", "-A")
        status = self._git_in(workdir, "status", "--porcelain")
        if not status:
            log.info("nothing to commit in %s", workdir)
            return False
        self._git_in(
            workdir,
            "-c", f"user.name={COMMIT_AUTHOR_NAME}",
            "-c", f"user.email={COMMIT_AUTHOR_EMAIL}",
            "commit", "-m", message,
        )
        return True

    def push(self, workdir: Path, branch: str) -> None:
        self._git_in(workdir, "push", "-u", "origin", branch, "--force-with-lease")

    def existing_pr(self, repo: str, head: str) -> str | None:
        out = self._exec(
            [self._gh, "pr", "list", "--repo", repo, "--head", head,
             "--state", "open", "--json", "url", "--jq", ".[0].url // empty"]
        )
        return out or None

    def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> str:
        return self._exec(
            [self._gh, "pr", "create", "--repo", repo, "--head", head,
             "--base", base, "--title", title, "--body", body]
        )
