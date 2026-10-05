"""Step tests: naming derivation, per-scope idempotency, multi-env assembly,
and the full orchestrator across per-project / per-env / per-app scopes.

Steps run against in-memory fakes, so the whole check/apply/skip/resume flow
is exercised without touching gcloud, kubectl, or git.
"""

import pytest
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


# --- CI_RUNNER on the repo, from the install's knob (2026-09-30) ------------------


def _app_ctx(single_env, github, runner=""):
    app, cluster, env = single_env
    cfg = Config()
    cfg.ci.runner = runner
    return ProvisionContext(app=app, config=cfg, clients={"github": github}, environments=[env])


def test_ci_runner_is_set_on_the_repo_when_the_install_names_one(single_env):
    from gojira.steps import SetCiRunner

    gh = FakeGitHubClient()
    ctx = _app_ctx(single_env, gh, runner="self-hosted-0")
    assert SetCiRunner().check(ctx) is None
    assert SetCiRunner().apply(ctx) == "CI_RUNNER=self-hosted-0"
    assert gh.variables[("acme/clips-api", "CI_RUNNER")] == "self-hosted-0"
    assert SetCiRunner().check(ctx) == "CI_RUNNER=self-hosted-0"  # idempotent: skipped next time


def test_ci_runner_is_left_alone_for_hosted_runners(single_env):
    from gojira.steps import SetCiRunner

    gh = FakeGitHubClient()
    assert SetCiRunner().check(_app_ctx(single_env, gh)) == "hosted runners"
    assert gh.variables == {}


def test_ci_runner_change_is_applied(single_env):
    from gojira.steps import SetCiRunner

    gh = FakeGitHubClient()
    gh.variables[("acme/clips-api", "CI_RUNNER")] = "old"
    ctx = _app_ctx(single_env, gh, runner="self-hosted-0")
    assert SetCiRunner().check(ctx) is None
    SetCiRunner().apply(ctx)
    assert gh.variables[("acme/clips-api", "CI_RUNNER")] == "self-hosted-0"


def test_ci_runner_names_the_missing_app_permission_on_403(single_env):
    from gojira.steps import SetCiRunner

    class Forbidden(FakeGitHubClient):
        def repo_variable(self, repo, name):
            raise RuntimeError('read variable CI_RUNNER in acme/clips-api failed: 403 {"message":"Resource not accessible by integration"}')

    with pytest.raises(RuntimeError, match="Variables: Read & write"):
        SetCiRunner().check(_app_ctx(single_env, Forbidden(), runner="self-hosted-0"))
