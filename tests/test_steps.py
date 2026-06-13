"""GCP step tests: naming derivation, engine integration, idempotency.

Steps run against a FakeGCPClient that models GCP state in memory, so the
full check/apply/skip/resume behavior is exercised without touching gcloud.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gojira.config import Config
from gojira.gcp import GCPClient
from gojira.github import SETUP_BRANCH, GitHubClient
from gojira.k8s import KubectlClient
from gojira.models import App, AppStatus, Base, Cluster, ProvisionStep, StepStatus
from gojira.provisioner import ProvisionContext, ProvisionEngine
from gojira.steps import (
    DEPLOY_ROLES,
    MANAGED_LABEL,
    REQUIRED_APIS,
    WIF_POOL,
    WIF_PROVIDER,
    BindWifRepo,
    EnsureNamespace,
    GenerateArtifacts,
    GrantIamRoles,
    Names,
    OpenSetupPr,
    PrepareWorkdir,
    build_spec,
    default_gcp_steps,
    default_steps,
)


class FakeGCPClient(GCPClient):
    """In-memory GCP. Records every mutation; preset existing state via kwargs."""

    def __init__(self, *, project_num="773958892580", existing=None):
        self._num = project_num
        e = existing or {}
        self.enabled_apis = set(e.get("apis", []))
        self.service_accounts = set(e.get("service_accounts", []))
        self.wif_pools = set(e.get("wif_pools", []))
        self.wif_providers = set(e.get("wif_providers", []))  # (pool, provider)
        self.sa_bindings = set(e.get("sa_bindings", []))      # (sa, member, role)
        self.project_bindings = set(e.get("project_bindings", []))  # (member, role)
        self.artifact_repos = set(e.get("artifact_repos", []))     # (location, repo)
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
        return (member, role) in self.project_bindings

    def add_project_binding(self, project, member, role):
        self.calls.append(f"proj_bind:{role}")
        self.project_bindings.add((member, role))

    def artifact_repo_exists(self, project, location, repo):
        return (location, repo) in self.artifact_repos

    def create_artifact_repo(self, project, location, repo):
        self.calls.append(f"create_ar:{location}/{repo}")
        self.artifact_repos.add((location, repo))

    def get_gke_credentials(self, project, location, cluster):
        self.calls.append(f"get_creds:{cluster}")
        return f"gke_{project}_{location}_{cluster}"


class FakeKubectlClient(KubectlClient):
    """In-memory cluster namespaces."""

    def __init__(self, existing=None):
        self.namespaces = dict(existing or {})  # name -> labels
        self.calls: list[str] = []

    def namespace_exists(self, namespace):
        return namespace in self.namespaces

    def ensure_namespace(self, namespace, labels):
        self.calls.append(f"ensure_ns:{namespace}")
        self.namespaces[namespace] = labels


class FakeGitHubClient(GitHubClient):
    """In-memory git/GitHub. Tracks the repo flow; preset an existing PR."""

    def __init__(self, *, default_branch="main", existing_pr=None):
        self._default_branch = default_branch
        self.prs = {}  # head -> url
        if existing_pr:
            self.prs[SETUP_BRANCH] = existing_pr
        self.cloned = None
        self.branch = None
        self.committed = False
        self.pushed = None
        self.calls: list[str] = []

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


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def ctx(session):
    cluster = Cluster(
        name="dev",
        gcp_project="shaz-499303",
        location="us-central1",
        gke_cluster="dev-gke",
        ar_repo="us-central1-docker.pkg.dev/shaz-499303/apps",
    )
    session.add(cluster)
    session.commit()
    app = App(
        name="clips-api",
        repo_full_name="shaz/clips-api",
        installation_id=1,
        cluster_id=cluster.id,
        namespace="clips",
        workload_class="web",
    )
    session.add(app)
    session.commit()

    def _ctx(gcp, kubectl=None, github=None, workdir=None):
        clients = {"gcp": gcp}
        if kubectl is not None:
            clients["kubectl"] = kubectl
        if github is not None:
            clients["github"] = github
        return ProvisionContext(
            app=app, cluster=cluster, config=Config(), clients=clients, workdir=workdir
        )

    return _ctx


# --- Names derivation --------------------------------------------------------


def test_names_derivation(ctx):
    gcp = FakeGCPClient()
    n = Names.derive(ctx(gcp))
    assert n.gsa_account_id == "clips-api-deploy"
    assert n.gsa_email == "clips-api-deploy@shaz-499303.iam.gserviceaccount.com"
    assert n.repo_owner == "shaz"
    assert n.ar_location == "us-central1"
    assert n.ar_repo == "apps"
    assert n.gsa_member == "serviceAccount:clips-api-deploy@shaz-499303.iam.gserviceaccount.com"


def test_principal_set_format(ctx):
    n = Names.derive(ctx(FakeGCPClient()))
    ps = n.principal_set("773958892580")
    assert ps == (
        "principalSet://iam.googleapis.com/projects/773958892580"
        "/locations/global/workloadIdentityPools/gojira"
        "/attribute.repository/shaz/clips-api"
    )


# --- full sequence on a clean project ---------------------------------------


def test_full_provision_clean_project(session, ctx):
    gcp = FakeGCPClient()
    c = ctx(gcp)
    result = ProvisionEngine(default_gcp_steps()).run(session, c)

    assert result.completed
    assert c.app.status == AppStatus.AWAITING_PR
    # everything created
    assert gcp.enabled_apis == set(REQUIRED_APIS)
    assert gcp.service_accounts == {"clips-api-deploy@shaz-499303.iam.gserviceaccount.com"}
    assert gcp.wif_pools == {WIF_POOL}
    assert (WIF_POOL, WIF_PROVIDER) in gcp.wif_providers
    assert ("us-central1", "apps") in gcp.artifact_repos
    assert len(gcp.project_bindings) == len(DEPLOY_ROLES)

    # ledger: all DONE, each with a resource_ref
    rows = session.scalars(select(ProvisionStep)).all()
    assert {r.step_name for r in rows} == {s.name for s in default_gcp_steps()}
    assert all(r.status == StepStatus.DONE for r in rows)
    assert all(r.resource_ref for r in rows)


def test_provider_restricted_to_repo_owner(session, ctx):
    gcp = FakeGCPClient()
    ProvisionEngine(default_gcp_steps()).run(session, ctx(gcp))
    assert "create_provider:gojira/github:shaz" in gcp.calls


# --- idempotency: shared resources already exist -----------------------------


def test_second_app_skips_shared_resources(session, ctx):
    """Pool, provider, APIs exist from a prior app — only per-app work runs."""
    gcp = FakeGCPClient(
        existing={
            "apis": list(REQUIRED_APIS),
            "wif_pools": [WIF_POOL],
            "wif_providers": [(WIF_POOL, WIF_PROVIDER)],
        }
    )
    c = ctx(gcp)
    result = ProvisionEngine(default_gcp_steps()).run(session, c)

    assert result.completed
    assert "ensure_apis_enabled" in result.steps_skipped
    assert "ensure_wif_pool" in result.steps_skipped
    assert "ensure_wif_provider" in result.steps_skipped
    # per-app resources still created
    assert "ensure_service_account" in result.steps_run
    assert "bind_wif_repo" in result.steps_run
    assert "ensure_artifact_repo" in result.steps_run
    assert "create_pool:gojira" not in gcp.calls


def test_rerun_is_fully_idempotent(session, ctx):
    gcp = FakeGCPClient()
    engine = ProvisionEngine(default_gcp_steps())
    engine.run(session, ctx(gcp))
    calls_after_first = len(gcp.calls)

    # ledger marks everything done; a second run touches nothing
    gcp.calls.clear()
    result = engine.run(session, ctx(gcp))
    assert result.completed
    assert result.steps_run == []
    assert gcp.calls == []
    assert calls_after_first > 0  # sanity: the first run did real work


def test_partial_existing_grants_only_missing_role(session, ctx):
    gcp = FakeGCPClient()
    n = Names.derive(ctx(gcp))
    gcp.project_bindings.add((n.gsa_member, DEPLOY_ROLES[0]))  # one role pre-granted

    step = GrantIamRoles()
    assert step.check(ctx(gcp)) is None  # not all roles present yet
    step.apply(ctx(gcp))
    assert gcp.calls.count(f"proj_bind:{DEPLOY_ROLES[1]}") == 1  # missing role granted
    assert f"proj_bind:{DEPLOY_ROLES[0]}" not in gcp.calls       # existing one untouched


def test_bind_wif_uses_project_number_in_principal_set(session, ctx):
    gcp = FakeGCPClient(project_num="999000111")
    step = BindWifRepo()
    step.apply(ctx(gcp))
    binding = next(iter(gcp.sa_bindings))
    assert "/projects/999000111/" in binding[1]


# --- spec assembly + artifact generation ------------------------------------


def test_build_spec_wires_provisioned_identity(ctx):
    gcp = FakeGCPClient(project_num="773958892580")
    spec = build_spec(ctx(gcp), gcp)
    assert spec.app == "clips-api"
    assert spec.gsa_email == "clips-api-deploy@shaz-499303.iam.gserviceaccount.com"
    assert spec.wif_provider == (
        "projects/773958892580/locations/global"
        "/workloadIdentityPools/gojira/providers/github"
    )
    assert spec.environments == ["dev", "qa", "prod"]
    assert spec.image == "us-central1-docker.pkg.dev/shaz-499303/apps/clips-api"


def test_generate_artifacts_writes_working_tree(session, ctx, tmp_path):
    gcp = FakeGCPClient()
    c = ctx(gcp)
    c.workdir = tmp_path
    ref = GenerateArtifacts().apply(c)

    assert (tmp_path / ".github/workflows/gojira-ci.yaml").exists()
    assert (tmp_path / "k8s/base/deployment.yaml").exists()
    assert (tmp_path / "k8s/overlays/prod/kustomization.yaml").exists()
    assert "files" in ref
    # the generated workflow references the real provisioned provider
    ci = (tmp_path / ".github/workflows/gojira-ci.yaml").read_text()
    assert "workloadIdentityPools/gojira/providers/github" in ci


def test_generate_artifacts_requires_workdir(session, ctx):
    with pytest.raises(ValueError, match="workdir"):
        GenerateArtifacts().apply(ctx(FakeGCPClient()))


def test_ensure_namespace_creates_with_managed_label(ctx):
    kube = FakeKubectlClient()
    step = EnsureNamespace()
    assert step.check(ctx(FakeGCPClient(), kubectl=kube)) is None
    ref = step.apply(ctx(FakeGCPClient(), kubectl=kube))
    assert ref == "clips"
    assert kube.namespaces["clips"] == MANAGED_LABEL


def test_ensure_namespace_skips_when_present(ctx):
    kube = FakeKubectlClient(existing={"clips": {"existing": "label"}})
    step = EnsureNamespace()
    assert step.check(ctx(FakeGCPClient(), kubectl=kube)) == "clips"
    assert kube.calls == []  # check short-circuits; apply never runs


def test_prepare_workdir_clones_and_branches(ctx, tmp_path):
    gh = FakeGitHubClient()
    c = ctx(FakeGCPClient(), github=gh, workdir=tmp_path)
    step = PrepareWorkdir()
    assert step.check(c) is None  # no .git yet
    step.apply(c)
    assert gh.cloned == "shaz/clips-api"
    assert gh.branch == SETUP_BRANCH
    assert step.check(c) == str(tmp_path)  # now .git exists -> would skip


def test_open_setup_pr_commits_pushes_and_records_url(ctx, tmp_path):
    gh = FakeGitHubClient()
    c = ctx(FakeGCPClient(), github=gh, workdir=tmp_path)
    step = OpenSetupPr()
    assert step.check(c) is None  # no PR yet
    url = step.apply(c)
    assert gh.committed and gh.pushed == SETUP_BRANCH
    assert "open_pr:gojira/setup->main" in gh.calls
    assert url == "https://github.com/shaz/clips-api/pull/1"
    assert c.app.setup_pr_url == url


def test_open_setup_pr_skips_when_pr_exists(ctx, tmp_path):
    existing = "https://github.com/shaz/clips-api/pull/42"
    gh = FakeGitHubClient(existing_pr=existing)
    c = ctx(FakeGCPClient(), github=gh, workdir=tmp_path)
    step = OpenSetupPr()
    assert step.check(c) == existing  # skip — no duplicate PR
    assert c.app.setup_pr_url == existing
    assert "open_pr" not in " ".join(gh.calls)


def test_full_default_sequence_end_to_end(session, ctx, tmp_path):
    gcp, kube, gh = FakeGCPClient(), FakeKubectlClient(), FakeGitHubClient()
    c = ctx(gcp, kubectl=kube, github=gh, workdir=tmp_path)
    result = ProvisionEngine(default_steps()).run(session, c)

    assert result.completed
    assert c.app.status == AppStatus.AWAITING_PR
    # every layer touched: GCP identity, namespace, generated files, PR opened
    assert gcp.service_accounts
    assert kube.namespaces["clips"] == MANAGED_LABEL
    assert (tmp_path / "k8s/base/service.yaml").exists()
    assert (tmp_path / "gojira.yaml").exists()
    assert c.app.setup_pr_url == "https://github.com/shaz/clips-api/pull/1"
    # ledger captures the PR url as the final step's resource_ref
    pr_row = next(r for r in session.scalars(select(ProvisionStep)) if r.step_name == "open_setup_pr")
    assert pr_row.resource_ref == c.app.setup_pr_url
