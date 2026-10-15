"""Step tests: naming derivation, per-scope idempotency, multi-env assembly,
and the full orchestrator across per-project / per-env / per-app scopes.

Steps run against in-memory fakes, so the whole check/apply/skip/resume flow
is exercised without touching gcloud, kubectl, or git.
"""

import json

import pytest
import yaml
from fakes import FakeGCPClient, FakeGitHubClient, FakeKubectlClient, KubectlFactory
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gojira.config import Config
from gojira.models import App, AppStatus, Base, Cluster, Environment, ProvisionStep, StepStatus
from gojira.provisioner import (
    ProjectScope,
    ProvisionContext,
    ProvisionEngine,
    provision_app,
)
from gojira.steps import (
    DEPLOY_ROLES,
    MANAGED_LABEL,
    PROMOTE_READER_ROLE,
    REQUIRED_APIS,
    WIF_POOL,
    WIF_PROVIDER,
    BindWifRepo,
    EnsureNamespace,
    GrantIamRoles,
    GrantPromoteSourceReader,
    MergeSetupPr,
    Names,
    build_spec,
    per_project_steps,
    promotion_plan,
)


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def _cluster(session, name, project, ns_loc="us-central1"):
    c = Cluster(
        name=name,
        gcp_project=project,
        location=ns_loc,
        gke_cluster=f"{name}-gke",
        ar_repo=f"{ns_loc}-docker.pkg.dev/{project}/apps",
    )
    session.add(c)
    session.commit()
    return c


def _app(session, repo="acme/clips-api"):
    a = App(name="clips-api", repo_full_name=repo, installation_id=1, workload_class="web")
    session.add(a)
    session.commit()
    return a


def _env(session, app, cluster, name, namespace="clips"):
    e = Environment(app_id=app.id, name=name, cluster_id=cluster.id, namespace=namespace)
    session.add(e)
    session.commit()
    return e


@pytest.fixture
def single_env(session):
    """An app with one dev environment in project acme-499303."""
    cluster = _cluster(session, "dev", "acme-499303")
    app = _app(session)
    env = _env(session, app, cluster, "dev")
    return app, cluster, env


@pytest.fixture
def project_ctx(single_env):
    """Build a per-project-scope context for the dev cluster's project."""
    app, cluster, env = single_env

    def _make(gcp):
        return ProvisionContext(
            app=app,
            config=Config(),
            clients={"gcp": gcp},
            project=ProjectScope(cluster.gcp_project, cluster.ar_repo),
            environments=[env],
        )

    return _make


# --- Names derivation --------------------------------------------------------


def test_names_derivation(project_ctx):
    n = Names.derive(project_ctx(FakeGCPClient()))
    assert n.gsa_account_id == "clips-api-deploy"
    assert n.gsa_email == "clips-api-deploy@acme-499303.iam.gserviceaccount.com"
    assert n.repo_owner == "acme"
    assert n.ar_location == "us-central1"
    assert n.ar_repo == "apps"
    assert n.gsa_member == "serviceAccount:clips-api-deploy@acme-499303.iam.gserviceaccount.com"


def test_names_requires_project_scope(single_env):
    app, _, env = single_env
    ctx = ProvisionContext(app=app, config=Config(), environments=[env])
    with pytest.raises(ValueError, match="ctx.project"):
        Names.derive(ctx)


def test_principal_set_format(project_ctx):
    n = Names.derive(project_ctx(FakeGCPClient()))
    ps = n.principal_set("773958892580")
    assert ps == (
        "principalSet://iam.googleapis.com/projects/773958892580"
        "/locations/global/workloadIdentityPools/gojira"
        "/attribute.repository/acme/clips-api"
    )


# --- per-project identity scope ----------------------------------------------


def test_per_project_clean(session, single_env, project_ctx):
    gcp = FakeGCPClient()
    result = ProvisionEngine(per_project_steps()).run(session, project_ctx(gcp), "acme-499303")

    assert result.completed
    assert gcp.enabled_apis == set(REQUIRED_APIS)
    assert gcp.service_accounts == {"clips-api-deploy@acme-499303.iam.gserviceaccount.com"}
    assert gcp.wif_pools == {WIF_POOL}
    assert (WIF_POOL, WIF_PROVIDER) in gcp.wif_providers
    assert ("acme-499303", "us-central1", "apps") in gcp.artifact_repos
    assert len([b for b in gcp.project_bindings if b[2] in DEPLOY_ROLES]) == len(DEPLOY_ROLES)

    # ledger rows are scoped to the project
    rows = session.scalars(select(ProvisionStep)).all()
    assert {r.step_name for r in rows} == {f"{s.name}@acme-499303" for s in per_project_steps()}
    assert all(r.status == StepStatus.DONE for r in rows)


def test_provider_restricted_to_repo_owner(session, project_ctx):
    gcp = FakeGCPClient()
    ProvisionEngine(per_project_steps()).run(session, project_ctx(gcp), "acme-499303")
    assert "create_provider:gojira/github:acme" in gcp.calls


def test_second_app_skips_shared_resources(session, project_ctx):
    """Pool, provider, APIs exist from a prior app — only per-app work runs."""
    gcp = FakeGCPClient(
        existing={
            "apis": list(REQUIRED_APIS),
            "wif_pools": [WIF_POOL],
            "wif_providers": [(WIF_POOL, WIF_PROVIDER)],
        }
    )
    result = ProvisionEngine(per_project_steps()).run(session, project_ctx(gcp), "acme-499303")

    assert result.completed
    assert "ensure_apis_enabled@acme-499303" in result.steps_skipped
    assert "ensure_wif_pool@acme-499303" in result.steps_skipped
    assert "ensure_service_account@acme-499303" in result.steps_run
    assert "create_pool:gojira" not in gcp.calls


def test_rerun_is_fully_idempotent(session, project_ctx):
    gcp = FakeGCPClient()
    engine = ProvisionEngine(per_project_steps())
    engine.run(session, project_ctx(gcp), "acme-499303")
    assert gcp.calls  # first run did real work

    gcp.calls.clear()
    result = engine.run(session, project_ctx(gcp), "acme-499303")
    assert result.completed
    assert result.steps_run == []
    assert gcp.calls == []


def test_partial_existing_grants_only_missing_role(session, project_ctx):
    gcp = FakeGCPClient()
    n = Names.derive(project_ctx(gcp))
    gcp.project_bindings.add(("acme-499303", n.gsa_member, DEPLOY_ROLES[0]))

    step = GrantIamRoles()
    assert step.check(project_ctx(gcp)) is None
    step.apply(project_ctx(gcp))
    assert gcp.calls.count(f"proj_bind:acme-499303:{DEPLOY_ROLES[1]}") == 1
    assert f"proj_bind:acme-499303:{DEPLOY_ROLES[0]}" not in gcp.calls


def test_bind_wif_uses_project_number_in_principal_set(session, project_ctx):
    gcp = FakeGCPClient(project_num="999000111")
    BindWifRepo().apply(project_ctx(gcp))
    binding = next(iter(gcp.sa_bindings))
    assert "/projects/999000111/" in binding[1]


# --- per-environment scope ---------------------------------------------------


def test_ensure_namespace_creates_with_managed_label(single_env):
    app, cluster, env = single_env
    kube = FakeKubectlClient()
    ctx = ProvisionContext(
        app=app, config=Config(), clients={"kubectl": kube}, environment=env, environments=[env]
    )
    step = EnsureNamespace()
    assert step.check(ctx) is None
    assert step.apply(ctx) == "clips"
    assert kube.namespaces["clips"] == MANAGED_LABEL


def test_ensure_namespace_skips_when_present_and_labeled(single_env):
    # "exists" alone is not "done" anymore (a tenant's finding): the namespace
    # must also carry the managed + configured labels to be skipped.
    app, cluster, env = single_env
    kube = FakeKubectlClient(existing={"clips": {"gojira.dev/managed": "true"}})
    ctx = ProvisionContext(
        app=app, config=Config(), clients={"kubectl": kube}, environment=env, environments=[env]
    )
    assert EnsureNamespace().check(ctx) == "clips"
    assert not any(c.startswith("ensure_ns") for c in kube.calls)


def test_promote_reader_grant_noop_for_single_env(single_env):
    app, cluster, env = single_env
    gcp = FakeGCPClient()
    ctx = ProvisionContext(
        app=app, config=Config(), clients={"gcp": gcp}, environment=env, environments=[env]
    )
    # dev is the CI env, copy_from is None -> nothing to grant
    assert GrantPromoteSourceReader().check(ctx) == "n/a"


def test_promote_reader_grant_crosses_project(session):
    """prod (project B) crane-copies from qa (project A) -> grant reader on A."""
    app = _app(session)
    dev = _cluster(session, "dev", "proj-a")
    qa = _cluster(session, "qa", "proj-a")
    prod = _cluster(session, "prod", "proj-b")
    e_dev = _env(session, app, dev, "dev")
    e_qa = _env(session, app, qa, "qa")
    e_prod = _env(session, app, prod, "prod")
    envs = [e_dev, e_qa, e_prod]

    gcp = FakeGCPClient()
    prod_ctx = ProvisionContext(
        app=app, config=Config(), clients={"gcp": gcp}, environment=e_prod, environments=envs
    )
    step = GrantPromoteSourceReader()
    assert step.check(prod_ctx) is None  # not yet granted
    ref = step.apply(prod_ctx)
    assert ref == f"proj-a:{PROMOTE_READER_ROLE}"
    member = "serviceAccount:clips-api-deploy@proj-b.iam.gserviceaccount.com"
    assert ("proj-a", member, PROMOTE_READER_ROLE) in gcp.project_bindings

    # qa is a same-project build env -> no grant
    qa_ctx = ProvisionContext(
        app=app, config=Config(), clients={"gcp": gcp}, environment=e_qa, environments=envs
    )
    assert step.check(qa_ctx) == "n/a"


# --- auto-merge the setup PR --------------------------------------------------


def test_merge_setup_pr_skips_when_not_requested(single_env):
    app, cluster, env = single_env
    app.auto_merge_setup_pr = False
    app.setup_pr_url = "https://github.com/acme/agent-clio/pull/1"
    ctx = ProvisionContext(
        app=app, config=Config(), clients={"github": FakeGitHubClient()}, environments=[env]
    )
    assert MergeSetupPr().check(ctx) == "auto-merge not requested"


def test_merge_setup_pr_skips_when_no_pr(single_env):
    app, cluster, env = single_env
    app.auto_merge_setup_pr = True
    app.setup_pr_url = None
    ctx = ProvisionContext(
        app=app, config=Config(), clients={"github": FakeGitHubClient()}, environments=[env]
    )
    assert MergeSetupPr().check(ctx) == "no setup PR to merge"


def test_merge_setup_pr_merges_and_activates(single_env):
    app, cluster, env = single_env
    app.auto_merge_setup_pr = True
    app.setup_pr_url = "https://github.com/acme/agent-clio/pull/1"
    github = FakeGitHubClient()
    ctx = ProvisionContext(
        app=app, config=Config(), clients={"github": github}, environments=[env]
    )
    result = MergeSetupPr().apply(ctx)
    assert github.merged == ["https://github.com/acme/agent-clio/pull/1"]
    assert ctx.app.status == AppStatus.ACTIVE
    assert "merged" in result


def test_merge_setup_pr_retry_skips_already_merged_pr(single_env):
    """A retry after a partial failure (merge landed server-side, client raised
    before setup_pr_url cleared) must not re-merge — that would error on an
    already-merged PR and wedge the ephemeral step at FAILED forever."""
    app, cluster, env = single_env
    app.auto_merge_setup_pr = True
    url = "https://github.com/acme/agent-clio/pull/1"
    app.setup_pr_url = url
    github = FakeGitHubClient(merged_urls={url})
    ctx = ProvisionContext(
        app=app, config=Config(), clients={"github": github}, environments=[env]
    )
    result = MergeSetupPr().apply(ctx)
    assert result == "already merged"
    assert github.merged == []  # merge_pr was NOT called
    assert ctx.app.setup_pr_url is None
    assert ctx.app.status == AppStatus.ACTIVE


def test_merge_setup_pr_merges_fresh_pr_despite_older_pr_merged(single_env):
    """The guard is PR-specific, not repo-history: re-provisioning after a
    generator upgrade opens a FRESH setup PR under the same branch (finding
    #13's OpenSetupPr refresh). A stale PR #1 having been merged in some
    earlier cycle must not short-circuit "already merged" for the new PR #2 —
    that would erase setup_pr_url and silently orphan the fresh PR."""
    app, cluster, env = single_env
    app.auto_merge_setup_pr = True
    old_url = "https://github.com/acme/agent-clio/pull/1"
    new_url = "https://github.com/acme/agent-clio/pull/2"
    app.setup_pr_url = new_url
    github = FakeGitHubClient(merged_urls={old_url})
    ctx = ProvisionContext(
        app=app, config=Config(), clients={"github": github}, environments=[env]
    )
    result = MergeSetupPr().apply(ctx)
    assert github.merged == [new_url]  # merge_pr WAS called, on the fresh PR
    assert ctx.app.setup_pr_url is None
    assert ctx.app.status == AppStatus.ACTIVE
    assert "merged" in result


# --- promotion plan ----------------------------------------------------------


def test_promotion_plan_dev_qa_prod(session):
    app = _app(session)
    dev = _env(session, app, _cluster(session, "dev", "p"), "dev")
    qa = _env(session, app, _cluster(session, "qa", "p"), "qa")
    prod = _env(session, app, _cluster(session, "prod", "p"), "prod")
    plan = promotion_plan([prod, dev, qa])  # order-insensitive input

    assert plan[dev.id].is_ci and plan[dev.id].copy_from is None and not plan[dev.id].gated
    assert not plan[qa.id].is_ci and plan[qa.id].copy_from is None and not plan[qa.id].gated
    assert plan[prod.id].copy_from is qa and plan[prod.id].gated and not plan[prod.id].is_ci


# --- multi-env spec assembly -------------------------------------------------


def test_build_spec_single_env(single_env):
    app, cluster, env = single_env
    gcp = FakeGCPClient(project_num="773958892580")
    ctx = ProvisionContext(app=app, config=Config(), clients={"gcp": gcp}, environments=[env])
    spec = build_spec(ctx, gcp)

    assert spec.app == "clips-api"
    assert [e.name for e in spec.environments] == ["dev"]
    dev = spec.ci_env
    assert dev.gsa_email == "clips-api-deploy@acme-499303.iam.gserviceaccount.com"
    assert dev.wif_provider == (
        "projects/773958892580/locations/global"
        "/workloadIdentityPools/gojira/providers/github"
    )
    assert spec.image(dev) == "us-central1-docker.pkg.dev/acme-499303/apps/clips-api"
    assert spec.promote_envs == []


def test_build_spec_carries_the_platform_arch(single_env):
    """defaults.arch / defaults.node_selector reach the spec as the platform's
    values, leaving the contract's own arch unsaid (None) so GenerateArtifacts
    lets gojira.yaml override them."""
    app, cluster, env = single_env
    gcp = FakeGCPClient(project_num="773958892580")
    cfg = Config.model_validate({"defaults": {
        "arch": "arm64", "node_selector": "{cloud.google.com/compute-class: Scale-Out}",
    }})
    ctx = ProvisionContext(app=app, config=cfg, clients={"gcp": gcp}, environments=[env])
    spec = build_spec(ctx, gcp)
    assert spec.arch is None and spec.effective_arch == "arm64"
    assert spec.effective_node_selector == {"cloud.google.com/compute-class": "Scale-Out"}
    plain = build_spec(ProvisionContext(app=app, config=Config(), clients={"gcp": gcp}, environments=[env]), gcp)
    assert plain.effective_arch == "amd64" and plain.effective_node_selector == {}


def test_build_spec_multi_project(session):
    app = _app(session)
    e_dev = _env(session, app, _cluster(session, "dev", "proj-a"), "dev")
    e_qa = _env(session, app, _cluster(session, "qa", "proj-a"), "qa")
    e_prod = _env(session, app, _cluster(session, "prod", "proj-b"), "prod")
    gcp = FakeGCPClient()
    ctx = ProvisionContext(
        app=app, config=Config(), clients={"gcp": gcp}, environments=[e_dev, e_qa, e_prod]
    )
    spec = build_spec(ctx, gcp)

    names = [e.name for e in spec.environments]
    assert names == ["dev", "qa", "prod"]
    prod = spec.environments[2]
    qa = spec.environments[1]
    assert prod.copy_from == "qa" and prod.gated
    assert qa.copy_from is None and not qa.gated
    # identity is project-scoped: prod's GSA lives in proj-b
    assert prod.gsa_email == "clips-api-deploy@proj-b.iam.gserviceaccount.com"
    assert qa.gsa_email == "clips-api-deploy@proj-a.iam.gserviceaccount.com"
    # prod's registry differs from qa's -> the promote workflow will crane-copy
    assert prod.ar_repo != qa.ar_repo


# --- full orchestrator end-to-end --------------------------------------------


def test_provision_app_single_env(session, single_env, tmp_path):
    app = single_env[0]
    gcp, gh = FakeGCPClient(), FakeGitHubClient()
    kube = KubectlFactory()
    result = provision_app(
        session, app, gcp=gcp, github=gh, kubectl_for=kube,
        config=Config(), workdir=tmp_path,
    )

    assert result.completed
    assert app.status == AppStatus.AWAITING_PR
    assert gcp.service_accounts
    assert kube.by_cluster["dev"].namespaces["clips"] == MANAGED_LABEL
    assert (tmp_path / "k8s/base/service.yaml").exists()
    assert (tmp_path / "gojira.yaml").exists()
    assert app.setup_pr_url == "https://github.com/acme/clips-api/pull/1"
    # only the per-app step is unscoped; identity is scoped to the project
    names = {r.step_name for r in session.scalars(select(ProvisionStep))}
    assert "ensure_service_account@acme-499303" in names
    assert "ensure_namespace@dev" in names
    assert "open_setup_pr" in names


def test_provision_app_three_envs_two_projects(session, tmp_path):
    app = _app(session)
    _env(session, app, _cluster(session, "dev", "proj-a"), "dev", namespace="clips-dev")
    _env(session, app, _cluster(session, "qa", "proj-a"), "qa", namespace="clips-qa")
    _env(session, app, _cluster(session, "prod", "proj-b"), "prod", namespace="clips-prod")
    gcp, gh = FakeGCPClient(), FakeGitHubClient()
    kube = KubectlFactory()

    result = provision_app(
        session, app, gcp=gcp, github=gh, kubectl_for=kube,
        config=Config(), workdir=tmp_path,
    )

    assert result.completed
    assert app.status == AppStatus.AWAITING_PR
    # identity provisioned once per distinct project
    assert "clips-api-deploy@proj-a.iam.gserviceaccount.com" in gcp.service_accounts
    assert "clips-api-deploy@proj-b.iam.gserviceaccount.com" in gcp.service_accounts
    names = {r.step_name for r in session.scalars(select(ProvisionStep))}
    assert "ensure_service_account@proj-a" in names
    assert "ensure_service_account@proj-b" in names
    # namespace created per env on the right cluster
    assert kube.by_cluster["dev"].namespaces["clips-dev"] == MANAGED_LABEL
    assert kube.by_cluster["prod"].namespaces["clips-prod"] == MANAGED_LABEL
    # prod's GSA got cross-project reader on proj-a so promote can crane-copy
    prod_member = "serviceAccount:clips-api-deploy@proj-b.iam.gserviceaccount.com"
    assert ("proj-a", prod_member, PROMOTE_READER_ROLE) in gcp.project_bindings
    # the generated promote workflow exists and crane-copies into prod
    promote = (tmp_path / ".github/workflows/gojira-promote.yaml").read_text()
    assert "crane copy" in promote


def test_provision_app_resumes_after_failure(session, single_env, tmp_path, monkeypatch):
    """A failure in the per-app scope leaves earlier scopes done; re-run resumes."""
    app = single_env[0]
    gcp, kube = FakeGCPClient(), KubectlFactory()

    boom = FakeGitHubClient()
    original_clone = boom.clone

    def explode(repo, workdir):
        raise RuntimeError("clone failed")

    boom.clone = explode
    first = provision_app(
        session, app, gcp=gcp, github=boom, kubectl_for=kube,
        config=Config(), workdir=tmp_path,
    )
    assert not first.completed
    assert app.status == AppStatus.FAILED
    # identity + namespace already done before the per-app failure
    done = {
        r.step_name for r in session.scalars(select(ProvisionStep))
        if r.status == StepStatus.DONE
    }
    assert "ensure_service_account@acme-499303" in done
    assert "ensure_namespace@dev" in done

    # fix the issue and re-run: GCP/namespace skipped, per-app completes
    boom.clone = original_clone
    gcp.calls.clear()
    second = provision_app(
        session, app, gcp=gcp, github=boom, kubectl_for=kube,
        config=Config(), workdir=tmp_path,
    )
    assert second.completed
    assert app.status == AppStatus.AWAITING_PR
    assert gcp.calls == []  # nothing re-provisioned in GCP


def test_dns_access_enabled_once_then_skipped(session_and_app=None):
    """Finding #14: per-env step turns on the cluster's DNS-based endpoint —
    generated CI's only path to a private control plane. Idempotent: second
    provision run skips via check()."""
    from gojira.steps import EnsureClusterDnsAccess
    from tests.fakes import FakeGCPClient

    class Env:
        class cluster:
            gcp_project, location, gke_cluster = "p", "us-east4", "g"

    class Ctx:
        environment = Env()
        clients = {"gcp": FakeGCPClient()}

    step = EnsureClusterDnsAccess()
    ctx = Ctx()
    assert step.check(ctx) is None                       # not enabled yet
    assert "dns access enabled" in step.apply(ctx)
    assert ctx.clients["gcp"].dns_access == {("p", "us-east4", "g")}
    assert step.check(ctx) is not None                   # now skips


def test_namespace_labels_stamped_and_converged():
    """A tenant's finding: secrets infra gates on namespace labels; the provisioner
    stamps defaults.namespace_labels and re-asserts them on existing
    namespaces (a label-less namespace is not 'done')."""
    from gojira.config import Config
    from gojira.steps import EnsureNamespace, MANAGED_LABEL
    from tests.fakes import FakeKubectlClient

    cfg = Config()
    cfg.defaults.namespace_labels = {"example.com/team": "sre"}

    class Env:
        namespace = "scout"

    class Ctx:
        environment = Env()
        config = cfg
        clients = {"kubectl": FakeKubectlClient(existing={"scout": {"app": "scout"}})}

    step = EnsureNamespace()
    ctx = Ctx()
    assert step.check(ctx) is None            # exists but unlabeled → not done
    step.apply(ctx)
    kubectl = ctx.clients["kubectl"]
    assert kubectl.namespaces["scout"]["example.com/team"] == "sre"
    assert kubectl.namespaces["scout"][next(iter(MANAGED_LABEL))] == "true"
    assert step.check(ctx) == "scout"          # converged → done


# --- the provider admits this repo's owner, not just "exists" (2026-09-30) ----


def test_second_owner_is_added_to_the_provider_condition(session, project_ctx):
    """The dogfood: a provider made by a personal-account connect said
    repository_owner == 'shaz'; the org's repos then failed their first token
    exchange in CI. Provisioning had skipped the provider as existing."""
    gcp = FakeGCPClient(
        existing={
            "apis": list(REQUIRED_APIS),
            "wif_pools": [WIF_POOL],
            "wif_providers": [(WIF_POOL, WIF_PROVIDER)],
            "provider_condition": "assertion.repository_owner == 'shaz'",
        }
    )
    result = ProvisionEngine(per_project_steps()).run(session, project_ctx(gcp), "acme-499303")
    assert result.completed
    assert "update_provider:gojira/github:acme,shaz" in gcp.calls
    assert not [c for c in gcp.calls if c.startswith("create_provider")]
    assert gcp.provider_condition == "assertion.repository_owner in ['acme', 'shaz']"


def test_an_admitted_owner_leaves_the_provider_alone(session, project_ctx):
    gcp = FakeGCPClient(
        existing={
            "apis": list(REQUIRED_APIS),
            "wif_pools": [WIF_POOL],
            "wif_providers": [(WIF_POOL, WIF_PROVIDER)],
            "provider_condition": "assertion.repository_owner in ['acme', 'shaz']",
        }
    )
    result = ProvisionEngine(per_project_steps()).run(session, project_ctx(gcp), "acme-499303")
    assert "ensure_wif_provider@acme-499303" in result.steps_skipped
    assert not [c for c in gcp.calls if "provider" in c]


def test_a_hand_written_condition_is_kept_with_a_warning(session, project_ctx, caplog):
    """A condition Gojira did not write is a person's decision; widening it
    silently would be worse than the failure it prevents."""
    gcp = FakeGCPClient(
        existing={
            "apis": list(REQUIRED_APIS),
            "wif_pools": [WIF_POOL],
            "wif_providers": [(WIF_POOL, WIF_PROVIDER)],
            "provider_condition": "assertion.repository == 'acme/one' || assertion.ref == 'refs/heads/main'",
        }
    )
    with caplog.at_level("WARNING"):
        result = ProvisionEngine(per_project_steps()).run(session, project_ctx(gcp), "acme-499303")
    assert result.completed
    assert not [c for c in gcp.calls if "provider" in c]
    assert "hand-written condition" in caplog.text and "'acme'" in caplog.text


def test_owner_conditions_round_trip():
    from gojira.gcp import owner_condition, owners_in_condition

    assert owner_condition(["shaz"]) == "assertion.repository_owner == 'shaz'"
    assert owners_in_condition(owner_condition(["tenishai", "shaz"])) == ["shaz", "tenishai"]
    assert owners_in_condition("assertion.repository_owner == 'x'") == ["x"]
    assert owners_in_condition("assertion.repository == 'a/b'") is None
    assert owners_in_condition("") is None


# --- the install's facts on the repo, as Actions variables (issue #87, 2026-10-06) --
# Before this, CI_RUNNER was the only variable (2026-09-30); the registry,
# deploy identity and cluster were literals in the generated workflow.


def _app_ctx(single_env, github, runner=""):
    app, cluster, env = single_env
    cfg = Config()
    cfg.ci.runner = runner
    gcp = FakeGCPClient(project_num="773958892580")
    return ProvisionContext(app=app, config=cfg, clients={"github": github, "gcp": gcp}, environments=[env])


EXPECTED_DEV_VARS = {
    "GOJIRA_DEV_AR_REPO": "us-central1-docker.pkg.dev/acme-499303/apps",
    "GOJIRA_DEV_WIF_PROVIDER": "projects/773958892580/locations/global/workloadIdentityPools/gojira/providers/github",
    "GOJIRA_DEV_DEPLOY_SA": "clips-api-deploy@acme-499303.iam.gserviceaccount.com",
    "GOJIRA_DEV_PROJECT": "acme-499303",
}


def test_platform_facts_are_set_on_the_repo_as_variables(single_env):
    from gojira.steps import SetRepoVariables

    gh = FakeGitHubClient()
    ctx = _app_ctx(single_env, gh)
    assert SetRepoVariables().check(ctx) is None
    out = SetRepoVariables().apply(ctx)
    assert out.startswith("set GOJIRA_DEV_AR_REPO")
    got = {k: v for (repo, k), v in gh.variables.items() if repo == "acme/clips-api"}
    for name, value in EXPECTED_DEV_VARS.items():
        assert got[name] == value
    assert {"GOJIRA_DEV_LOCATION", "GOJIRA_DEV_GKE_CLUSTER"} <= set(got)
    assert "CI_RUNNER" not in got  # hosted runners: nothing to say
    assert SetRepoVariables().check(ctx) == "6 variables current"  # idempotent


def test_ci_runner_rides_along_when_the_install_names_one(single_env):
    from gojira.steps import SetRepoVariables

    gh = FakeGitHubClient()
    ctx = _app_ctx(single_env, gh, runner="self-hosted-0")
    SetRepoVariables().apply(ctx)
    assert gh.variables[("acme/clips-api", "CI_RUNNER")] == "self-hosted-0"
    assert SetRepoVariables().check(ctx) == "7 variables current"


def test_only_drifted_variables_are_rewritten(single_env):
    from gojira.steps import SetRepoVariables

    gh = FakeGitHubClient()
    ctx = _app_ctx(single_env, gh, runner="self-hosted-0")
    SetRepoVariables().apply(ctx)
    gh.variables[("acme/clips-api", "CI_RUNNER")] = "old"          # someone changed it
    gh.variables[("acme/clips-api", "GOJIRA_DEV_PROJECT")] = "x"    # or that
    assert SetRepoVariables().check(ctx) is None
    assert SetRepoVariables().apply(ctx) == "set GOJIRA_DEV_PROJECT, CI_RUNNER"
    assert gh.variables[("acme/clips-api", "CI_RUNNER")] == "self-hosted-0"
    assert gh.variables[("acme/clips-api", "GOJIRA_DEV_PROJECT")] == "acme-499303"


def test_variables_step_names_the_missing_app_permission_on_403(single_env):
    from gojira.steps import SetRepoVariables

    class Forbidden(FakeGitHubClient):
        def repo_variable(self, repo, name):
            raise RuntimeError('read variable GOJIRA_DEV_AR_REPO in acme/clips-api failed: 403 {"message":"Resource not accessible by integration"}')

    with pytest.raises(RuntimeError, match="Variables: Read & write"):
        SetRepoVariables().check(_app_ctx(single_env, Forbidden()))


# --- identity: the pod's GCP identity, made and granted by the platform (2026-10-06) --
# docs/grants-and-platform-facts-design.md §1. kobo-room and litellm were made
# by hand (GSA, role, WI binding, annotation) because the contract had no
# sentence for "let this pod call Vertex".


_identity_apps = 0


def _identity_ctx(session, roles=("roles/aiplatform.user",), gsa="", grantable=("roles/aiplatform.user",), multiproject=False):
    global _identity_apps
    from gojira.generator import Identity

    _identity_apps += 1  # several contexts per session: distinct repos/clusters
    app = _app(session, repo=f"acme/clips-api-{_identity_apps}")
    envs = [_env(session, app, _cluster(session, f"dev{_identity_apps}", "proj-a"), "dev")]
    if multiproject:
        envs.append(_env(session, app, _cluster(session, f"prod{_identity_apps}", "proj-b"), "prod"))
    gcp = FakeGCPClient(project_num="773958892580", existing={"service_accounts": [gsa]} if gsa else None)
    cfg = Config.model_validate({"iam": {"grantable_roles": list(grantable)}})
    ctx = ProvisionContext(app=app, config=cfg, clients={"gcp": gcp, "github": FakeGitHubClient()}, environments=envs)
    ctx.spec = build_spec(ctx, gcp).model_copy(update={
        "identity": Identity(gcp_roles=list(roles), gsa=gsa), "service_account": "",
    })
    return ctx, gcp


def test_identity_makes_one_run_gsa_per_project(session):
    from gojira.steps import EnsureRunIdentity

    ctx, gcp = _identity_ctx(session, multiproject=True)
    assert EnsureRunIdentity().check(ctx) is None
    out = EnsureRunIdentity().apply(ctx)
    assert out == "clips-api-run@proj-a.iam.gserviceaccount.com, clips-api-run@proj-b.iam.gserviceaccount.com"
    assert "create_sa:clips-api-run@proj-a.iam.gserviceaccount.com" in gcp.calls
    assert EnsureRunIdentity().check(ctx) is not None  # idempotent
    rec = json.loads(ctx.app.run_identity_json)
    assert rec["gsa_by_project"] == {
        "proj-a": "clips-api-run@proj-a.iam.gserviceaccount.com",
        "proj-b": "clips-api-run@proj-b.iam.gserviceaccount.com",
    }
    assert rec["adopted"] is False


def test_identity_adopts_an_existing_gsa_and_refuses_a_missing_one(session):
    from gojira.steps import EnsureRunIdentity, IdentityRefused

    ctx, gcp = _identity_ctx(session, gsa="kobo-room@proj-a.iam.gserviceaccount.com")
    assert EnsureRunIdentity().check(ctx) == "kobo-room@proj-a.iam.gserviceaccount.com"
    assert not [c for c in gcp.calls if c.startswith("create_sa")]
    assert json.loads(ctx.app.run_identity_json)["adopted"] is True

    ctx2, _ = _identity_ctx(session, gsa="ghost-sa@proj-a.iam.gserviceaccount.com")
    ctx2.clients["gcp"].service_accounts.clear()
    with pytest.raises(IdentityRefused, match="does not exist"):
        EnsureRunIdentity().check(ctx2)


def test_roles_off_the_allowlist_are_refused_by_name(session):
    from gojira.steps import GrantRunRoles, IdentityRefused

    ctx, gcp = _identity_ctx(session, roles=("roles/aiplatform.user", "roles/owner"))
    with pytest.raises(IdentityRefused, match=r"roles/owner.*GOJIRA_GRANTABLE_ROLES"):
        GrantRunRoles().check(ctx)
    assert not [c for c in gcp.calls if c.startswith("proj_bind")]

    empty, _ = _identity_ctx(session, grantable=())
    with pytest.raises(IdentityRefused, match="allows nothing"):
        GrantRunRoles().check(empty)


def test_allowed_roles_are_granted_to_the_run_gsa_on_each_project(session):
    from gojira.steps import GrantRunRoles

    ctx, gcp = _identity_ctx(session, multiproject=True)
    assert GrantRunRoles().check(ctx) is None
    assert GrantRunRoles().apply(ctx) == "roles/aiplatform.user"
    for project in ("proj-a", "proj-b"):
        assert (project, f"serviceAccount:clips-api-run@{project}.iam.gserviceaccount.com", "roles/aiplatform.user") in gcp.project_bindings
    assert GrantRunRoles().check(ctx) == "roles/aiplatform.user"
    assert json.loads(ctx.app.run_identity_json)["roles"] == ["roles/aiplatform.user"]


def test_workload_identity_binds_each_environments_ksa(session):
    from gojira.steps import BindRunWorkloadIdentity

    ctx, gcp = _identity_ctx(session, multiproject=True)
    assert BindRunWorkloadIdentity().check(ctx) is None
    out = BindRunWorkloadIdentity().apply(ctx)
    assert "serviceAccount:proj-a.svc.id.goog[clips/clips-api]" in out
    assert ("clips-api-run@proj-b.iam.gserviceaccount.com",
            "serviceAccount:proj-b.svc.id.goog[clips/clips-api]",
            "roles/iam.workloadIdentityUser") in gcp.sa_bindings
    assert BindRunWorkloadIdentity().check(ctx) is not None
    rec = json.loads(ctx.app.run_identity_json)
    assert ["proj-a", "clips-api-run@proj-a.iam.gserviceaccount.com", "serviceAccount:proj-a.svc.id.goog[clips/clips-api]"] in rec["bindings"]


def test_adopted_gsa_is_bound_on_its_own_project(session):
    """A GSA in another project than the cluster's: the binding lives on the
    GSA, so its project is the GSA's, while the member names the cluster's."""
    from gojira.steps import BindRunWorkloadIdentity

    ctx, gcp = _identity_ctx(session, gsa="shared-run@proj-x.iam.gserviceaccount.com")
    BindRunWorkloadIdentity().apply(ctx)
    assert ("shared-run@proj-x.iam.gserviceaccount.com",
            "serviceAccount:proj-a.svc.id.goog[clips/clips-api]",
            "roles/iam.workloadIdentityUser") in gcp.sa_bindings


def test_identity_steps_are_quiet_without_an_identity(session):
    from gojira.steps import BindRunWorkloadIdentity, EnsureRunIdentity, GrantRunRoles

    ctx, gcp = _identity_ctx(session)
    ctx.spec = ctx.spec.model_copy(update={"identity": None})
    for step in (EnsureRunIdentity(), GrantRunRoles(), BindRunWorkloadIdentity()):
        assert step.check(ctx) == "no identity declared"
    assert gcp.calls == []
    assert ctx.app.run_identity_json is None


def test_run_sa_reaches_the_repo_as_a_variable(session):
    from gojira.steps import SetRepoVariables

    ctx, _ = _identity_ctx(session)
    SetRepoVariables().apply(ctx)
    assert ctx.clients["github"].variables[(ctx.app.repo_full_name, "GOJIRA_DEV_RUN_SA")] == "clips-api-run@proj-a.iam.gserviceaccount.com"
    assert SetRepoVariables().check(ctx) == "7 variables current"


# --- cluster_roles: the operator's ClusterRoles, Gojira's bindings (2026-10-07, §2) --


def _cluster_ctx(session, roles=("kobo-room",), grantable=("kobo-room",), on_cluster=("kobo-room",), multiproject=False):
    ctx, gcp = _identity_ctx(session, multiproject=multiproject)
    ctx.spec = ctx.spec.model_copy(update={"identity": None, "cluster_roles": list(roles)})
    ctx.config = Config.model_validate({"iam": {"grantable_cluster_roles": list(grantable)}})
    kube = KubectlFactory()
    for env in ctx.environments:
        kube(env.cluster).cluster_roles.update(on_cluster)
    ctx.clients["kubectl_for"] = kube
    return ctx, kube


def test_cluster_roles_off_the_allowlist_are_refused_by_name(session):
    from gojira.steps import BindClusterRoles, IdentityRefused

    ctx, _ = _cluster_ctx(session, roles=("kobo-room", "cluster-admin"))
    with pytest.raises(IdentityRefused, match=r"cluster-admin.*GOJIRA_GRANTABLE_CLUSTER_ROLES"):
        BindClusterRoles().check(ctx)


def test_a_cluster_role_the_operator_has_not_applied_is_refused(session):
    from gojira.steps import BindClusterRoles, IdentityRefused

    ctx, _ = _cluster_ctx(session, on_cluster=())
    with pytest.raises(IdentityRefused, match="apply it first"):
        BindClusterRoles().check(ctx)


def test_cluster_roles_are_bound_on_every_environments_cluster(session):
    from gojira.steps import BindClusterRoles

    ctx, kube = _cluster_ctx(session, multiproject=True)
    assert BindClusterRoles().check(ctx) is None
    out = BindClusterRoles().apply(ctx)
    assert out == "dev:gojira-clips-api-kobo-room, prod:gojira-clips-api-kobo-room"
    for env in ctx.environments:
        k = kube(env.cluster)
        assert "gojira-clips-api-kobo-room" in k.cluster_role_bindings
        crb = yaml.safe_load(k.applied[-1])
        assert crb["roleRef"]["name"] == "kobo-room"
        assert crb["subjects"] == [{"kind": "ServiceAccount", "name": "clips-api", "namespace": "clips"}]
    assert BindClusterRoles().check(ctx) == "kobo-room"  # idempotent
    rec = json.loads(ctx.app.run_identity_json)
    assert rec["cluster_bindings"] == [["dev", "gojira-clips-api-kobo-room"], ["prod", "gojira-clips-api-kobo-room"]]


def test_cluster_roles_step_is_quiet_without_any(session):
    from gojira.steps import BindClusterRoles

    ctx, kube = _cluster_ctx(session, roles=(), grantable=())
    assert BindClusterRoles().check(ctx) == "no cluster roles asked for"
    assert ctx.app.run_identity_json is None


def test_a_base_with_a_cluster_scoped_object_is_refused_before_the_setup_pr(tmp_path, session):
    from gojira.steps import IdentityRefused, _refuse_cluster_scoped_base

    base = tmp_path / "k8s" / "base"
    base.mkdir(parents=True)
    (base / "rbac.yaml").write_text("apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata: {name: kobo-room}\nrules: []\n")
    files = {"k8s/base/kustomization.yaml": "resources:\n  - deployment.yaml\n  - rbac.yaml\n",
             "k8s/base/deployment.yaml": "kind: Deployment\nmetadata: {name: x}\n"}
    with pytest.raises(IdentityRefused, match=r"k8s/base/rbac.yaml holds a ClusterRole.*cluster_roles"):
        _refuse_cluster_scoped_base(base, files)
    files["k8s/base/kustomization.yaml"] = "resources:\n  - deployment.yaml\n"
    _refuse_cluster_scoped_base(base, files)  # an unlisted file is nobody's problem


# --- finding #83: build every overlay before anything is committed ---------


def test_generate_refuses_an_overlay_that_does_not_build(session, single_env, tmp_path, monkeypatch):
    """A preserved patch that kustomize rejects fails generate_artifacts with
    kustomize's message, so the app row says why instead of a green setup PR
    carrying an overlay the generated CI can never apply (tenishai/kobo#92)."""
    from gojira import steps as steps_mod
    from gojira.k8s import K8sError
    built = []

    def broken(overlay):
        built.append(overlay.name)
        raise K8sError(
            "kubectl kustomize k8s/overlays/dev failed: no resource matches strategic "
            "merge patch \"ServiceAccount.v1.[noGrp]/kobo.kobo\""
        )

    monkeypatch.setattr(steps_mod, "kustomize_build", broken)
    app = single_env[0]
    result = provision_app(
        session, app,
        gcp=FakeGCPClient(), github=FakeGitHubClient(),
        kubectl_for=KubectlFactory(), config=Config(), workdir=tmp_path,
    )
    assert not result.completed
    assert result.failed_step == "generate_artifacts"
    assert "k8s/overlays/dev does not build" in result.error
    assert "no resource matches" in result.error        # kustomize's own reason survives
    assert built == ["dev"]
    assert app.status == AppStatus.FAILED
    assert app.setup_pr_url is None                     # nothing was opened


def test_generate_builds_the_real_overlays(session, single_env, tmp_path):
    """With a real kubectl the render builds, so the step still completes."""
    import shutil
    if not shutil.which("kubectl"):
        pytest.skip("kubectl not on PATH")
    app = single_env[0]
    result = provision_app(
        session, app,
        gcp=FakeGCPClient(), github=FakeGitHubClient(),
        kubectl_for=KubectlFactory(), config=Config(), workdir=tmp_path,
    )
    assert result.completed, result.error
    assert (tmp_path / "k8s" / "overlays" / "dev" / "kustomization.yaml").exists()


# --- issue #102: install-named variables ride along; unknown refs refused --


def test_install_repo_variables_are_set_next_to_the_platform_facts(single_env):
    from gojira.steps import SetRepoVariables

    gh = FakeGitHubClient()
    ctx = _app_ctx(single_env, gh)
    ctx.config.ci.repo_variables = {"GOJIRA_PYTHON_INDEX": "https://idx/simple/"}
    SetRepoVariables().apply(ctx)
    assert gh.variables[("acme/clips-api", "GOJIRA_PYTHON_INDEX")] == "https://idx/simple/"
    assert SetRepoVariables().check(ctx) == "7 variables current"


def test_generate_refuses_a_reference_this_install_does_not_set(session, single_env, tmp_path):
    from gojira.provisioner import provision_app

    app = single_env[0]
    (tmp_path / "gojira.yaml").write_text(
        "app: clips-api\nclass: web\nbuild_args:\n  EXTRA_INDEX_URL: ${GOJIRA_PYTHON_INDEX}\n"
    )
    result = provision_app(
        session, app, gcp=FakeGCPClient(), github=FakeGitHubClient(),
        kubectl_for=KubectlFactory(), config=Config(), workdir=tmp_path,
    )
    assert not result.completed
    assert result.failed_step == "generate_artifacts"
    assert "GOJIRA_PYTHON_INDEX" in result.error and "GOJIRA_REPO_VARIABLES" in result.error


def test_generate_refuses_an_image_with_build_args(session, single_env, tmp_path):
    """image: is copied, not built (issue #108); build_args next to it would be
    silently ignored, so the provision says so instead."""
    from gojira.provisioner import provision_app

    app = single_env[0]
    (tmp_path / "gojira.yaml").write_text(
        "app: clips-api\nclass: web\nimage: ghcr.io/acme/clips:1\nbuild_args:\n  X: y\n"
    )
    result = provision_app(
        session, app, gcp=FakeGCPClient(), github=FakeGitHubClient(),
        kubectl_for=KubectlFactory(), config=Config(), workdir=tmp_path,
    )
    assert not result.completed
    assert result.failed_step == "generate_artifacts"
    assert "image:" in result.error and "build_args" in result.error


def test_generate_renders_an_image_contract_without_a_dockerfile(session, single_env, tmp_path):
    from gojira.provisioner import provision_app

    app = single_env[0]
    (tmp_path / "gojira.yaml").write_text("app: clips-api\nclass: web\nimage: ghcr.io/acme/clips:1\n")
    result = provision_app(
        session, app, gcp=FakeGCPClient(), github=FakeGitHubClient(dockerfile=False),
        kubectl_for=KubectlFactory(), config=Config(), workdir=tmp_path,
    )
    assert result.completed, result.error
    ci = (tmp_path / ".github" / "workflows" / "gojira-ci.yaml").read_text()
    assert "crane copy" in ci and "docker build" not in ci
    assert "image: ghcr.io/acme/clips:1" in (tmp_path / "gojira.yaml").read_text()
