"""Shared in-memory fakes for the GCP / kubectl / GitHub clients.

Imported by the step, service, and web tests so the full check/apply/skip/resume
flow is exercised without touching gcloud, kubectl, or git.
"""

from gojira.gcp import GCPClient
from gojira.github import SETUP_BRANCH, GitHubClient, GitHubError
from gojira.installations import InstallationResolver
from gojira.k8s import KubectlClient
from gojira.shell import CommandResult


class FakeGCPClient(GCPClient):
    """In-memory GCP. Records every mutation; preset existing state via kwargs."""

    def __init__(self, *, project_num="773958892580", existing=None):
        self._num = project_num
        e = existing or {}
        self.enabled_apis = set(e.get("apis", []))
        self.service_accounts = set(e.get("service_accounts", []))
        self.wif_pools = set(e.get("wif_pools", []))
        self.wif_providers = set(e.get("wif_providers", []))
        self.sa_bindings = set(e.get("sa_bindings", []))
        self.project_bindings = set(e.get("project_bindings", []))  # (project, member, role)
        self.artifact_repos = set(e.get("artifact_repos", []))
        self.dns_access = set(e.get("dns_access", []))  # (project, location, cluster)
        self.calls: list[str] = []

    def project_number(self, project):
        return self._num

    def api_enabled(self, project, api):
        return api in self.enabled_apis

    def enable_api(self, project, api):
        self.calls.append(f"enable_api:{api}")
        self.enabled_apis.add(api)

    def service_account_exists(self, project, email):
        return email in self.service_accounts

    def create_service_account(self, project, account_id, display_name):
        email = f"{account_id}@{project}.iam.gserviceaccount.com"
        self.calls.append(f"create_sa:{email}")
        self.service_accounts.add(email)
        return email

    def wif_pool_exists(self, project, pool):
        return pool in self.wif_pools

    def create_wif_pool(self, project, pool, display_name):
        self.calls.append(f"create_pool:{pool}")
        self.wif_pools.add(pool)

    def wif_provider_exists(self, project, pool, provider):
        return (pool, provider) in self.wif_providers

    def create_wif_provider(self, project, pool, provider, allowed_owner):
        self.calls.append(f"create_provider:{pool}/{provider}:{allowed_owner}")
        self.wif_providers.add((pool, provider))

    def sa_has_binding(self, project, sa_email, member, role):
        return (sa_email, member, role) in self.sa_bindings

    def add_sa_binding(self, project, sa_email, member, role):
        self.calls.append(f"sa_bind:{sa_email}:{role}")
        self.sa_bindings.add((sa_email, member, role))

    def project_has_binding(self, project, member, role):
        return (project, member, role) in self.project_bindings

    def add_project_binding(self, project, member, role):
        self.calls.append(f"proj_bind:{project}:{role}")
        self.project_bindings.add((project, member, role))

    def artifact_repo_exists(self, project, location, repo):
        return (project, location, repo) in self.artifact_repos

    def create_artifact_repo(self, project, location, repo):
        self.calls.append(f"create_ar:{project}/{location}/{repo}")
        self.artifact_repos.add((project, location, repo))

    def get_gke_credentials(self, project, location, cluster):
        self.calls.append(f"get_creds:{cluster}")
        return f"gke_{project}_{location}_{cluster}"

    def dns_access_enabled(self, project, location, cluster):
        return (project, location, cluster) in self.dns_access

    def enable_dns_access(self, project, location, cluster):
        self.calls.append(f"enable_dns:{cluster}")
        self.dns_access.add((project, location, cluster))


class FakeKubectlClient(KubectlClient):
    """In-memory cluster namespaces (one instance per cluster)."""

    def __init__(self, existing=None, resources=None):
        self.namespaces = dict(existing or {})
        # live objects keyed by (namespace, kind.lower(), name)
        self.resources = dict(resources or {})
        self.calls: list[str] = []
        self.applied: list[str] = []  # raw manifests handed to apply()

    def namespace_exists(self, namespace):
        return namespace in self.namespaces

    def ensure_namespace(self, namespace, labels):
        self.calls.append(f"ensure_ns:{namespace}")
        self.namespaces[namespace] = labels

    def delete_namespace(self, namespace):
        self.calls.append(f"delete_ns:{namespace}")
        self.namespaces.pop(namespace, None)

    def get_resource(self, namespace, kind, name):
        self.calls.append(f"get:{kind}/{name}")
        if kind.lower() == "namespace" and name in self.namespaces:
            return {"metadata": {"labels": dict(self.namespaces[name])}}
        return self.resources.get((namespace, kind.lower(), name))

    def apply(self, manifest):
        self.calls.append("apply")
        self.applied.append(manifest)

    def create_job_from_cronjob(self, namespace, name, cronjob):
        self.calls.append(f"job:{namespace}/{name}<-{cronjob}")


class KubectlFactory:
    """A kubectl_for(cluster) factory yielding one fake per cluster name."""

    def __init__(self):
        self.by_cluster: dict[str, FakeKubectlClient] = {}

    def __call__(self, cluster) -> FakeKubectlClient:
        return self.by_cluster.setdefault(cluster.name, FakeKubectlClient())


class FakeGitHubClient(GitHubClient):
    """In-memory git/GitHub. Tracks the repo flow; preset an existing PR."""

    def __init__(self, *, default_branch="main", existing_pr=None, repos=None,
                 dockerfile=True, pr_merged=False, installation_id=0):
        self._default_branch = default_branch
        self.dockerfile = dockerfile
        self.pr_merged = pr_merged
        self.installation_id = installation_id
        self.repos = repos or ["shaz/clips-api", "shaz/gojira-demo", "acme/web"]
        self.prs = {}
        if existing_pr:
            self.prs[SETUP_BRANCH] = existing_pr
        self.cloned = None
        self.branch = None
        self.committed = False
        self.pushed = None
        self.tags: list[tuple[str, str, str]] = []  # (repo, tag, sha)
        self.dispatched: list[tuple[str, str, str]] = []  # (repo, workflow, ref)
        self.dispatch_error: str | None = None
        self.merged: list[str] = []
        self.calls: list[str] = []

    def list_repos(self, limit=200):
        return self.repos[:limit]

    def installation_id_for(self, repo):
        return self.installation_id

    def file_exists(self, repo, path):
        return self.dockerfile if path == "Dockerfile" else True

    def setup_pr_merged(self, repo):
        return self.pr_merged

    def head_sha(self, repo):
        return "abc123def456"

    def create_tag(self, repo, tag, sha):
        self.calls.append(f"tag:{tag}")
        self.tags.append((repo, tag, sha))

    def dispatch_workflow(self, repo, workflow, ref):
        if self.dispatch_error:
            raise GitHubError(self.dispatch_error)
        self.calls.append(f"dispatch:{workflow}@{ref}")
        self.dispatched.append((repo, workflow, ref))

    def default_branch(self, repo):
        return self._default_branch

    def clone(self, repo, workdir):
        self.calls.append(f"clone:{repo}")
        (workdir / ".git").mkdir(parents=True, exist_ok=True)
        self.cloned = repo

    def checkout_branch(self, workdir, branch):
        self.calls.append(f"checkout:{branch}")
        self.branch = branch

    def commit_all(self, workdir, message):
        self.calls.append("commit")
        self.committed = True
        return True

    def push(self, workdir, branch):
        self.calls.append(f"push:{branch}")
        self.pushed = branch

    def existing_pr(self, repo, head):
        return self.prs.get(head)

    def open_pr(self, repo, head, base, title, body):
        self.calls.append(f"open_pr:{head}->{base}")
        url = f"https://github.com/{repo}/pull/1"
        self.prs[head] = url
        return url

    def merge_pr(self, repo, pr_url):
        self.calls.append(f"merge_pr:{pr_url}")
        self.merged.append(pr_url)
        self.pr_merged = True


class MemoryInstallationResolver(InstallationResolver):
    """Dict-backed resolver for tests — records what gets cached."""

    def __init__(self, seed=None):
        self.map: dict[str, int] = dict(seed or {})

    def resolve(self, repo):
        return self.map.get(repo)

    def remember(self, repo, installation_id):
        self.map[repo] = installation_id

    def forget(self, repo):
        self.map.pop(repo, None)


class RecordingRunner:
    """A CommandRunner that records argv lists and returns canned results.

    Match a result by a substring of the joined command; the first match wins,
    else a 0/empty result. Lets the App-backend git tests assert what `git` was
    invoked with (including the token-authed clone URL) without running it.
    """

    def __init__(self, results=None):
        self.results = results or {}  # substring → CommandResult
        self.calls: list[list[str]] = []

    def run(self, args, stdin=None):
        self.calls.append(args)
        joined = " ".join(args)
        for needle, result in self.results.items():
            if needle in joined:
                return result
        return CommandResult(0, "", "")
