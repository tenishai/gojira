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

import json
import logging
import re
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
CI_WORKFLOW = "gojira-ci.yaml"  # the generated deploy workflow's file name
COMMIT_AUTHOR_NAME = "Gojira"
COMMIT_AUTHOR_EMAIL = "noreply@gojira.dev"

GITHUB_API = "https://api.github.com"
GITHUB_HOST = "github.com"
_ACCEPT = "application/vnd.github+json"


class GitHubError(RuntimeError):
    """A git, gh, or GitHub API call failed.

    `status` is the HTTP status when the call was a REST request (the App
    client); None for a gh or git subprocess, whose stderr is the message."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# What GitHub says when it declines a merge for a reason a person settles: a
# branch rule wants a review or a check, the head moved, the base changed. The
# App client sees the status; gh prints the same sentences on stderr.
_MERGE_REFUSAL = re.compile(
    r"rule violations|not mergeable|review (is )?required|required status check"
    r"|protected branch|base branch was modified|cannot be cleanly created",
    re.IGNORECASE,
)


def merge_refused(exc: GitHubError) -> bool:
    """GitHub refused THIS merge (405 not mergeable or rules, 409 head changed,
    422 validation; or gh's text saying one of those), as opposed to the call
    failing (a 5xx, a dead network). The first is a person's merge now; the
    second is a step that should fail and be retried (#144)."""
    if exc.status is not None:
        return exc.status in (405, 409, 422)
    return bool(_MERGE_REFUSAL.search(str(exc)))


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
    def read_file(self, repo: str, path: str) -> str | None:
        """The text of path at the repo's default branch, None when absent."""
        ...

    @abstractmethod
    def setup_pr_merged(self, repo: str) -> bool:
        """Whether the gojira/setup PR has been merged."""
        ...

    @abstractmethod
    def pr_merged(self, repo: str, pr_url: str) -> bool:
        """Whether this specific PR has been merged."""
        ...

    @abstractmethod
    def head_sha(self, repo: str) -> str:
        """The commit SHA at the tip of the repo's default branch."""
        ...

    @abstractmethod
    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> None:
        """Trigger a workflow_dispatch run of workflow (file name) on ref."""

    @abstractmethod
    def create_tag(self, repo: str, tag: str, sha: str) -> None:
        """Create a lightweight tag ref pointing at sha."""
        ...

    @abstractmethod
    def repo_variable(self, repo: str, name: str) -> str | None:
        """An Actions repository variable's value, or None when unset."""
        ...

    @abstractmethod
    def set_repo_variable(self, repo: str, name: str, value: str) -> None:
        """Create or update an Actions repository variable."""
        ...

    @abstractmethod
    def delete_repo_variable(self, repo: str, name: str) -> None:
        """Remove an Actions repository variable; one that is not there is fine."""
        ...

    @abstractmethod
    def repo_is_public(self, repo: str) -> bool:
        """Whether anyone can read (and fork) the repository."""
        ...

    @abstractmethod
    def default_branch(self, repo: str) -> str: ...

    @abstractmethod
    def clone(self, repo: str, workdir: Path) -> None: ...

    @abstractmethod
    def checkout_branch(self, workdir: Path, branch: str) -> None: ...

    @abstractmethod
    def checkout_sha(self, workdir: Path, sha: str) -> None:
        """Detached checkout of an exact commit — rollback's time travel."""
        ...

    @abstractmethod
    def deploy_history(self, repo: str, workflow: str, branch: str, limit: int = 10) -> list[dict]:
        """Successful completed runs of `workflow` on `branch`, newest first:
        {"sha", "title", "finished_at", "run_url"} each. The receipts the
        rollback picker offers — read from GitHub, not our webhook Events,
        so the list has no gaps from webhook downtime and predates the
        feature."""
        ...

    @abstractmethod
    def restore_tree(self, workdir: Path, sha: str) -> None:
        """Make the working tree exactly match sha's tree (adds, edits, AND
        deletions), staged, without moving the current branch — the revert
        PR's one honest git operation."""
        ...

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

    @abstractmethod
    def merge_pr(self, repo: str, pr_url: str) -> None:
        """Squash-merge the PR and best-effort delete its branch."""
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

    def checkout_sha(self, workdir: Path, sha: str) -> None:
        self._git_in(workdir, "checkout", "--detach", sha)

    def restore_tree(self, workdir: Path, sha: str) -> None:
        # read-tree -u --reset makes index AND worktree exactly match sha's
        # tree — including deleting files added since. `checkout <sha> -- .`
        # would silently keep those, reverting only half the history.
        self._git_in(workdir, "read-tree", "-u", "--reset", sha)

    def commit_all(self, workdir: Path, message: str) -> bool:
        self._git_in(workdir, "add", "-A")
        if not self._git_in(workdir, "status", "--porcelain"):
            log.info("nothing to commit in %s", workdir)
            return False
        # -s: a Signed-off-by trailer in the author's own name. Repos that
        # enforce the Developer Certificate of Origin reject a commit without
        # one, and the setup PR is a commit Gojira makes in someone else's repo
        # (kobo, cabal and liminal all turned the check on, 2026-10-06). The
        # same convention dependabot follows for the commits it authors.
        self._git_in(
            workdir,
            "-c", f"user.name={COMMIT_AUTHOR_NAME}",
            "-c", f"user.email={COMMIT_AUTHOR_EMAIL}",
            "commit", "-s", "-m", message,
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

    def read_file(self, repo: str, path: str) -> str | None:
        # The raw media type returns the file body itself, not the JSON+base64 envelope.
        result = self._runner.run(
            [self._gh, "api", "-H", "Accept: application/vnd.github.raw+json",
             f"repos/{repo}/contents/{path}"]
        )
        return result.stdout if result.returncode == 0 else None

    def setup_pr_merged(self, repo: str) -> bool:
        out = self._exec(
            [self._gh, "pr", "list", "--repo", repo, "--head", SETUP_BRANCH,
             "--state", "merged", "--json", "number", "--jq", "length"]
        )
        return out.strip() not in ("", "0")

    def pr_merged(self, repo: str, pr_url: str) -> bool:
        # gh's pr-view JSON has no "merged" field; state is OPEN/CLOSED/MERGED.
        out = self._exec(
            [self._gh, "pr", "view", pr_url, "--repo", repo,
             "--json", "state", "--jq", '.state == "MERGED"']
        )
        return out.strip() == "true"

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

    def repo_variable(self, repo: str, name: str) -> str | None:
        out = self._exec([self._gh, "variable", "list", "--repo", repo, "--json", "name,value"])
        for v in json.loads(out) if out.strip() else []:
            if v.get("name") == name:
                return str(v.get("value", ""))
        return None

    def set_repo_variable(self, repo: str, name: str, value: str) -> None:
        self._exec([self._gh, "variable", "set", name, "--repo", repo, "--body", value])

    def delete_repo_variable(self, repo: str, name: str) -> None:
        if self.repo_variable(repo, name) is None:  # `gh variable delete` fails on a missing one
            return
        self._exec([self._gh, "variable", "delete", name, "--repo", repo])

    def repo_is_public(self, repo: str) -> bool:
        out = self._exec([self._gh, "repo", "view", repo, "--json", "visibility", "--jq", ".visibility"])
        return out.strip().lower() == "public"

    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> None:
        self._exec(
            [self._gh, "workflow", "run", workflow, "--repo", repo, "--ref", ref]
        )

    def deploy_history(self, repo: str, workflow: str, branch: str, limit: int = 10) -> list[dict]:
        out = self._exec(
            [self._gh, "run", "list", "--repo", repo, "--workflow", workflow,
             "--branch", branch, "--status", "success", "--limit", str(limit),
             "--json", "headSha,displayTitle,updatedAt,url"]
        )
        return [
            {"sha": r.get("headSha", ""), "title": r.get("displayTitle", ""),
             "finished_at": r.get("updatedAt", ""), "run_url": r.get("url", "")}
            for r in (json.loads(out) if out.strip() else [])
        ]

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

    def merge_pr(self, repo: str, pr_url: str) -> None:
        self._exec([self._gh, "pr", "merge", pr_url, "--repo", repo,
                    "--squash", "--delete-branch"])


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
            raise GitHubError(f"{what} failed: {r.status_code} {r.text}", status=r.status_code)

    def _request(self, method: str, path: str, repo: str, **kwargs) -> httpx.Response:
        """An installation-scoped REST call for repo."""
        token = self._token(self.installation_id_for(repo))
        headers = {"Accept": _ACCEPT, **kwargs.pop("headers", {}), "Authorization": f"Bearer {token}"}
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

    def read_file(self, repo: str, path: str) -> str | None:
        r = self._request(
            "GET", f"/repos/{repo}/contents/{path}", repo,
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if r.status_code == 404:
            return None
        self._check(r, f"read {path} in {repo}")
        return r.text

    def setup_pr_merged(self, repo: str) -> bool:
        owner = repo.split("/")[0]
        r = self._request(
            "GET", f"/repos/{repo}/pulls", repo,
            params={"state": "closed", "head": f"{owner}:{SETUP_BRANCH}"},
        )
        self._check(r, f"list setup PRs for {repo}")
        return any(pr.get("merged_at") for pr in r.json())

    def pr_merged(self, repo: str, pr_url: str) -> bool:
        number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        r = self._request("GET", f"/repos/{repo}/pulls/{number}", repo)
        self._check(r, f"check merged state of PR {pr_url}")
        return bool(r.json()["merged"])

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

    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> None:
        # Needs the App's "Actions: write" permission. A 422 here usually means
        # the repo's workflow predates the workflow_dispatch trigger — a setup
        # PR refresh (re-provision) brings it in.
        r = self._request(
            "POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches", repo,
            json={"ref": ref},
        )
        self._check(r, f"dispatch {workflow} in {repo}")

    def repo_variable(self, repo: str, name: str) -> str | None:
        r = self._request("GET", f"/repos/{repo}/actions/variables/{name}", repo)
        if r.status_code == 404:
            return None
        self._check(r, f"read variable {name} in {repo}")
        return str(r.json().get("value", ""))

    def set_repo_variable(self, repo: str, name: str, value: str) -> None:
        # Needs the App's "Actions: write". PATCH updates; a 404 means it
        # does not exist yet and POST creates it.
        body = {"name": name, "value": value}
        r = self._request("PATCH", f"/repos/{repo}/actions/variables/{name}", repo, json=body)
        if r.status_code == 404:
            r = self._request("POST", f"/repos/{repo}/actions/variables", repo, json=body)
        self._check(r, f"set variable {name} in {repo}")

    def delete_repo_variable(self, repo: str, name: str) -> None:
        r = self._request("DELETE", f"/repos/{repo}/actions/variables/{name}", repo)
        if r.status_code == 404:
            return
        self._check(r, f"delete variable {name} in {repo}")

    def repo_is_public(self, repo: str) -> bool:
        r = self._request("GET", f"/repos/{repo}", repo)
        self._check(r, f"read {repo}")
        return not bool(r.json().get("private", True))

    def deploy_history(self, repo: str, workflow: str, branch: str, limit: int = 10) -> list[dict]:
        r = self._request(
            "GET", f"/repos/{repo}/actions/workflows/{workflow}/runs", repo,
            params={"status": "success", "branch": branch, "per_page": limit},
        )
        self._check(r, f"deploy history for {repo}")
        return [
            {"sha": run.get("head_sha", ""), "title": run.get("display_title", ""),
             "finished_at": run.get("updated_at", ""), "run_url": run.get("html_url", "")}
            for run in (r.json().get("workflow_runs") or [])
        ]

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

    def merge_pr(self, repo: str, pr_url: str) -> None:
        number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        r = self._request(
            "PUT", f"/repos/{repo}/pulls/{number}/merge", repo,
            json={"merge_method": "squash"},
        )
        self._check(r, f"merge PR {pr_url}")
        # Branch cleanup (fetching the head ref, then deleting it) is cosmetic —
        # a failure in either call must never fail the step after a successful
        # merge, so both live inside this one best-effort try/except.
        try:
            pr = self._request("GET", f"/repos/{repo}/pulls/{number}", repo)
            self._check(pr, f"fetch PR {pr_url}")
            head_ref = pr.json()["head"]["ref"]
            d = self._request("DELETE", f"/repos/{repo}/git/refs/heads/{head_ref}", repo)
            self._check(d, f"delete branch {head_ref} in {repo}")
        except Exception as exc:
            log.warning("could not delete setup branch in %s after merge: %s", repo, exc)

    def clone(self, repo: str, workdir: Path) -> None:
        token = self._token(self.installation_id_for(repo))
        # Token is embedded in the ephemeral working tree's origin URL; the tree
        # is a tempdir removed after the run, and push reuses this same origin.
        url = f"https://x-access-token:{token}@{self._git_host}/{repo}.git"
        result = self._runner.run([self._git, "clone", url, str(workdir)])
        if result.returncode != 0:
            raise GitHubError(f"clone {repo} failed: {result.stderr or result.stdout}")
