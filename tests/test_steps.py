"""GCP step tests: naming derivation, engine integration, idempotency.

Steps run against a FakeGCPClient that models GCP state in memory, so the
full check/apply/skip/resume behavior is exercised without touching gcloud.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gojira.config import Config
from gojira.gcp import GCPClient
from gojira.models import App, AppStatus, Base, Cluster, ProvisionStep, StepStatus
from gojira.provisioner import ProvisionContext, ProvisionEngine
from gojira.steps import (
    DEPLOY_ROLES,
    REQUIRED_APIS,
    WIF_POOL,
    WIF_PROVIDER,
    BindWifRepo,
    GrantIamRoles,
    Names,
    default_gcp_steps,
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

    def _ctx(gcp):
        return ProvisionContext(app=app, cluster=cluster, config=Config(), clients={"gcp": gcp})

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
