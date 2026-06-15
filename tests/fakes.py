"""Shared in-memory fakes for the GCP / kubectl / GitHub clients.

Imported by the step, service, and web tests so the full check/apply/skip/resume
flow is exercised without touching gcloud, kubectl, or git.
"""

from gojira.gcp import GCPClient
from gojira.github import SETUP_BRANCH, GitHubClient
from gojira.k8s import KubectlClient


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


class FakeKubectlClient(KubectlClient):
    """In-memory cluster namespaces (one instance per cluster)."""

    def __init__(self, existing=None):
        self.namespaces = dict(existing or {})
        self.calls: list[str] = []

    def namespace_exists(self, namespace):
        return namespace in self.namespaces

    def ensure_namespace(self, namespace, labels):
        self.calls.append(f"ensure_ns:{namespace}")
        self.namespaces[namespace] = labels


class KubectlFactory:
    """A kubectl_for(cluster) factory yielding one fake per cluster name."""

    def __init__(self):
        self.by_cluster: dict[str, FakeKubectlClient] = {}

    def __call__(self, cluster) -> FakeKubectlClient:
        return self.by_cluster.setdefault(cluster.name, FakeKubectlClient())


class FakeGitHubClient(GitHubClient):
    """In-memory git/GitHub. Tracks the repo flow; preset an existing PR."""

    def __init__(self, *, default_branch="main", existing_pr=None, repos=None):
        self._default_branch = default_branch
        self.repos = repos or ["shaz/clips-api", "shaz/gojira-demo", "acme/web"]
        self.prs = {}
        if existing_pr:
            self.prs[SETUP_BRANCH] = existing_pr
        self.cloned = None
        self.branch = None
        self.committed = False
        self.pushed = None
        self.calls: list[str] = []

    def list_repos(self, limit=200):
        return self.repos[:limit]

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
