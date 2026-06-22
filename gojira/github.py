"""GitHub + git operations behind an interface.

Steps depend on the GitHubClient ABC. Two backends implement it:

- GhCLI: shells out to `git` and `gh` using the operator's `gh auth` session.
  The honest single-tenant story — runs anywhere the operator is logged in.
- GitHubAppClient: authenticates as a GitHub App installation (RS256 JWT →
  installation access token), talks REST over httpx, and pushes over a
  token-authed remote. The hosted, webhook-driven path. Selected when the App
  is configured (see service._build_github_client).

Both share the auth-free local git mechanics in _LocalGit.
"""

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

import httpx
import jwt

from gojira.installations import InstallationResolver
from gojira.shell import CommandRunner

log = logging.getLogger(__name__)

SETUP_BRANCH = "gojira/setup"
COMMIT_AUTHOR_NAME = "Gojira"
COMMIT_AUTHOR_EMAIL = "noreply@gojira.dev"

GITHUB_API = "https://api.github.com"
GITHUB_HOST = "github.com"
_ACCEPT = "application/vnd.github+json"


class GitHubError(RuntimeError):
    """A git, gh, or GitHub API call failed."""


class GitHubClient(ABC):
    """The GitHub/git operations the provisioner needs."""

    @abstractmethod
    def list_repos(self, limit: int = 200) -> list[str]:
        """owner/repo names the operator can deploy, most-recently-pushed first."""
        ...

    @abstractmethod
    def installation_id_for(self, repo: str) -> int:
        """The GitHub App installation id covering repo, or 0 if not App-backed."""
        ...

    @abstractmethod
    def file_exists(self, repo: str, path: str) -> bool:
        """Whether path exists at the repo's default branch."""
        ...

    @abstractmethod
    def setup_pr_merged(self, repo: str) -> bool:
        """Whether the gojira/setup PR has been merged."""
        ...

    @abstractmethod
    def head_sha(self, repo: str) -> str:
        """The commit SHA at the tip of the repo's default branch."""
        ...

    @abstractmethod
    def create_tag(self, repo: str, tag: str, sha: str) -> None:
        """Create a lightweight tag ref pointing at sha."""
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


class _LocalGit:
    """Auth-free local git operations shared by both backends.

    These run inside an already-cloned working tree, so they need no
    credentials — only the local `git` binary and a CommandRunner.
    """

    def __init__(self, runner: CommandRunner | None = None, git: str = "git"):
        self._runner = runner or CommandRunner()
        self._git = git

    def _git_in(self, workdir: Path, *args: str) -> str:
        result = self._runner.run([self._git, "-C", str(workdir), *args])
        if result.returncode != 0:
            raise GitHubError(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")
        return result.stdout

    def checkout_branch(self, workdir: Path, branch: str) -> None:
        self._git_in(workdir, "checkout", "-B", branch)

    def commit_all(self, workdir: Path, message: str) -> bool:
        self._git_in(workdir, "add", "-A")
        if not self._git_in(workdir, "status", "--porcelain"):
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
        # origin already carries credentials from clone (gh helper or token URL).
        self._git_in(workdir, "push", "-u", "origin", branch, "--force-with-lease")


class GhCLI(_LocalGit, GitHubClient):
    """git + gh backed client using the operator's gh auth session."""

    def __init__(self, runner: CommandRunner | None = None, git: str = "git", gh: str = "gh"):
        super().__init__(runner, git)
        self._gh = gh

    def _exec(self, args: list[str], stdin: str | None = None) -> str:
        result = self._runner.run(args, stdin=stdin)
        if result.returncode != 0:
            raise GitHubError(f"{' '.join(args)} failed: {result.stderr or result.stdout}")
        return result.stdout

    def list_repos(self, limit: int = 200) -> list[str]:
        out = self._exec(
            [self._gh, "repo", "list", "--no-archived", "--limit", str(limit),
             "--json", "nameWithOwner", "--jq", ".[].nameWithOwner"]
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    def installation_id_for(self, repo: str) -> int:
        return 0  # the gh-CLI backend has no GitHub App installation concept

    def file_exists(self, repo: str, path: str) -> bool:
        # gh api returns 0 on 200 (exists), non-zero on 404; don't raise on 404.
        result = self._runner.run([self._gh, "api", f"repos/{repo}/contents/{path}", "--silent"])
        return result.returncode == 0

    def setup_pr_merged(self, repo: str) -> bool:
        out = self._exec(
            [self._gh, "pr", "list", "--repo", repo, "--head", SETUP_BRANCH,
             "--state", "merged", "--json", "number", "--jq", "length"]
        )
        return out.strip() not in ("", "0")

    def default_branch(self, repo: str) -> str:
        return self._exec(
            [self._gh, "repo", "view", repo, "--json", "defaultBranchRef",
             "--jq", ".defaultBranchRef.name"]
        )

    def head_sha(self, repo: str) -> str:
        branch = self.default_branch(repo)
        return self._exec(
            [self._gh, "api", f"repos/{repo}/commits/{branch}", "--jq", ".sha"]
        ).strip()

    def create_tag(self, repo: str, tag: str, sha: str) -> None:
        self._exec(
            [self._gh, "api", "-X", "POST", f"repos/{repo}/git/refs",
             "-f", f"ref=refs/tags/{tag}", "-f", f"sha={sha}"]
        )

    def clone(self, repo: str, workdir: Path) -> None:
        self._exec([self._gh, "repo", "clone", repo, str(workdir)])

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


class GitHubAppClient(_LocalGit, GitHubClient):
    """Authenticates as a GitHub App installation.

    Mints a short-lived RS256 JWT from the App's private key, exchanges it for
    a per-installation access token (cached until shortly before expiry), and
    uses that token for REST (httpx) and for git push over a token-authed
    remote URL. Repo → installation resolution goes through the resolver, with
    the GitHub API as the source of truth on a cache miss.
    """

    def __init__(
        self,
        app_id: str,
        private_key: str,
        resolver: InstallationResolver,
        *,
        http: httpx.Client | None = None,
        runner: CommandRunner | None = None,
        git: str = "git",
        now=None,
        git_host: str = GITHUB_HOST,
    ):
        super().__init__(runner, git)
        self._app_id = app_id
        self._key = private_key
        self._resolver = resolver
        self._http = http or httpx.Client(base_url=GITHUB_API, timeout=30)
        self._now = now or time.time  # epoch-seconds callable (test seam)
        self._git_host = git_host
        self._tokens: dict[int, tuple[str, float]] = {}  # installation_id → (token, exp_epoch)

    # --- auth ----------------------------------------------------------------

    def _app_jwt(self) -> str:
        now = int(self._now())
        # iat back-dated 60s to tolerate clock skew; exp well within the 10-min max.
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode(payload, self._key, algorithm="RS256")

    def _app_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._app_jwt()}", "Accept": _ACCEPT}

    def _token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached[1] - self._now() > 60:
            return cached[0]
        r = self._http.post(
            f"/app/installations/{installation_id}/access_tokens", headers=self._app_headers()
        )
        self._check(r, f"mint token for installation {installation_id}")
        data = r.json()
        exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        self._tokens[installation_id] = (data["token"], exp)
        return data["token"]

    def installation_id_for(self, repo: str) -> int:
        cached = self._resolver.resolve(repo)
        if cached:
            return cached
        r = self._http.get(f"/repos/{repo}/installation", headers=self._app_headers())
        self._check(r, f"resolve installation for {repo}")
        iid = int(r.json()["id"])
        self._resolver.remember(repo, iid)
        return iid

    # --- REST helpers --------------------------------------------------------

    @staticmethod
    def _check(r: httpx.Response, what: str) -> None:
        if r.status_code >= 300:
            raise GitHubError(f"{what} failed: {r.status_code} {r.text}")

    def _request(self, method: str, path: str, repo: str, **kwargs) -> httpx.Response:
        """An installation-scoped REST call for repo."""
        token = self._token(self.installation_id_for(repo))
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {token}", "Accept": _ACCEPT}
        return self._http.request(method, path, headers=headers, **kwargs)

    # --- GitHubClient surface ------------------------------------------------

    def list_repos(self, limit: int = 200) -> list[str]:
        """Every repo the App is installed on, across installations, newest push
        first. Seeds the resolver cache as a side effect."""
        installs = self._http.get("/app/installations", headers=self._app_headers())
        self._check(installs, "list installations")
        collected: list[tuple[str, str]] = []  # (full_name, pushed_at)
        for inst in installs.json():
            iid = int(inst["id"])
            token = self._token(iid)
            headers = {"Authorization": f"Bearer {token}", "Accept": _ACCEPT}
            page = 1
            while True:
                r = self._http.get(
                    "/installation/repositories",
                    headers=headers,
                    params={"per_page": 100, "page": page},
                )
                self._check(r, f"list repos for installation {iid}")
                repos = r.json().get("repositories", [])
                for repo in repos:
                    collected.append((repo["full_name"], repo.get("pushed_at") or ""))
                    self._resolver.remember(repo["full_name"], iid)
                if len(repos) < 100:
                    break
                page += 1
        collected.sort(key=lambda t: t[1], reverse=True)
        return [name for name, _ in collected[:limit]]

    def file_exists(self, repo: str, path: str) -> bool:
        r = self._request("GET", f"/repos/{repo}/contents/{path}", repo)
        if r.status_code == 404:
            return False
        self._check(r, f"check {path} in {repo}")
        return True

    def setup_pr_merged(self, repo: str) -> bool:
        owner = repo.split("/")[0]
        r = self._request(
            "GET", f"/repos/{repo}/pulls", repo,
            params={"state": "closed", "head": f"{owner}:{SETUP_BRANCH}"},
        )
        self._check(r, f"list setup PRs for {repo}")
        return any(pr.get("merged_at") for pr in r.json())

    def default_branch(self, repo: str) -> str:
        r = self._request("GET", f"/repos/{repo}", repo)
        self._check(r, f"view {repo}")
        return r.json()["default_branch"]

    def head_sha(self, repo: str) -> str:
        branch = self.default_branch(repo)
        r = self._request("GET", f"/repos/{repo}/commits/{branch}", repo)
        self._check(r, f"head sha for {repo}")
        return r.json()["sha"]

    def create_tag(self, repo: str, tag: str, sha: str) -> None:
        r = self._request(
            "POST", f"/repos/{repo}/git/refs", repo,
            json={"ref": f"refs/tags/{tag}", "sha": sha},
        )
        self._check(r, f"tag {tag} in {repo}")

    def existing_pr(self, repo: str, head: str) -> str | None:
        owner = repo.split("/")[0]
        r = self._request(
            "GET", f"/repos/{repo}/pulls", repo,
            params={"state": "open", "head": f"{owner}:{head}"},
        )
        self._check(r, f"list open PRs for {repo}")
        prs = r.json()
        return prs[0]["html_url"] if prs else None

    def open_pr(self, repo: str, head: str, base: str, title: str, body: str) -> str:
        r = self._request(
            "POST", f"/repos/{repo}/pulls", repo,
            json={"title": title, "head": head, "base": base, "body": body},
        )
        self._check(r, f"open PR in {repo}")
        return r.json()["html_url"]

    def clone(self, repo: str, workdir: Path) -> None:
        token = self._token(self.installation_id_for(repo))
        # Token is embedded in the ephemeral working tree's origin URL; the tree
        # is a tempdir removed after the run, and push reuses this same origin.
        url = f"https://x-access-token:{token}@{self._git_host}/{repo}.git"
        result = self._runner.run([self._git, "clone", url, str(workdir)])
        if result.returncode != 0:
            raise GitHubError(f"clone {repo} failed: {result.stderr or result.stdout}")
