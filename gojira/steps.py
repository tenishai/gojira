"""Provision steps, organized by scope, plus multi-env spec assembly.

Steps are thin: naming and derivation live here, the cloud operations live in
the clients. Each step reads only the part of the ProvisionContext its scope
sets — `ctx.project` for per-project identity, `ctx.environment` for per-env
cluster work, `ctx.environments` for per-app generation.

Promotion model (revised from the design doc): the CI workflow builds and
deploys the first env (dev). Each later env builds from scratch too, EXCEPT
the last (prod), which crane-copies the exact image its predecessor (qa) built
and tested — prod runs the bytes qa validated, no rebuild. The last env is the
gated production target.
"""

import logging
from dataclasses import dataclass

from gojira.gcp import GCPClient
from gojira.generator import (
    EnvSpec,
    GeneratorSpec,
    preserve_patches,
    read_overrides,
    render,
)
from gojira.github import SETUP_BRANCH, GitHubClient
from gojira.k8s import KubectlClient
from gojira.models import App, Environment
from gojira.provisioner import ProvisionContext, Step

log = logging.getLogger(__name__)

MANAGED_LABEL = {"gojira.dev/managed": "true"}

WIF_POOL = "gojira"
WIF_PROVIDER = "github"

REQUIRED_APIS = (
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "artifactregistry.googleapis.com",
    "container.googleapis.com",
    "cloudresourcemanager.googleapis.com",
)

DEPLOY_ROLES = (
    "roles/artifactregistry.writer",
    "roles/container.developer",
)

# Role a copy-target env's deploy GSA needs on the SOURCE project's registry
# so its promote job can read the image it crane-copies (cross-project only).
PROMOTE_READER_ROLE = "roles/artifactregistry.reader"


@dataclass(frozen=True)
class Names:
    """Concrete resource identifiers for an app within one GCP project."""

    project: str
    gsa_account_id: str
    gsa_email: str
    repo_owner: str
    repo_full_name: str
    ar_location: str
    ar_repo: str

    @classmethod
    def for_project(cls, app: App, project: str, ar_repo: str) -> "Names":
        account_id = f"{app.name}-deploy"
        owner, _, _ = app.repo_full_name.partition("/")
        # ar_repo is "<location>-docker.pkg.dev/<project>/<repo>"
        host, _, path = ar_repo.partition("/")
        ar_location = host.removesuffix("-docker.pkg.dev")
        return cls(
            project=project,
            gsa_account_id=account_id,
            gsa_email=f"{account_id}@{project}.iam.gserviceaccount.com",
            repo_owner=owner,
            repo_full_name=app.repo_full_name,
            ar_location=ar_location,
            ar_repo=path.rsplit("/", 1)[-1],
        )

    @classmethod
    def derive(cls, ctx: ProvisionContext) -> "Names":
        """Derive from the per-project scope target set on the context."""
        if ctx.project is None:
            raise ValueError("Names.derive needs ctx.project (per-project scope)")
        return cls.for_project(ctx.app, ctx.project.project, ctx.project.ar_repo)

    def principal_set(self, project_number: str) -> str:
        return (
            f"principalSet://iam.googleapis.com/projects/{project_number}"
            f"/locations/global/workloadIdentityPools/{WIF_POOL}"
            f"/attribute.repository/{self.repo_full_name}"
        )

    def wif_provider_name(self, project_number: str) -> str:
        """Full resource name the GH Actions auth step references."""
        return (
            f"projects/{project_number}/locations/global"
            f"/workloadIdentityPools/{WIF_POOL}/providers/{WIF_PROVIDER}"
        )

    @property
    def gsa_member(self) -> str:
        return f"serviceAccount:{self.gsa_email}"


def _gcp(ctx: ProvisionContext) -> GCPClient:
    return ctx.clients["gcp"]


def _kubectl(ctx: ProvisionContext) -> KubectlClient:
    return ctx.clients["kubectl"]


def _github(ctx: ProvisionContext) -> GitHubClient:
    return ctx.clients["github"]


# --- per-project scope: identity (SA, WIF, IAM, Artifact Registry) -----------


class EnsureApisEnabled:
    name = "ensure_apis_enabled"

    def check(self, ctx: ProvisionContext) -> str | None:
        gcp, n = _gcp(ctx), Names.derive(ctx)
        if all(gcp.api_enabled(n.project, api) for api in REQUIRED_APIS):
            return ",".join(REQUIRED_APIS)
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        gcp, n = _gcp(ctx), Names.derive(ctx)
        for api in REQUIRED_APIS:
            if not gcp.api_enabled(n.project, api):
                gcp.enable_api(n.project, api)
        return ",".join(REQUIRED_APIS)


class EnsureServiceAccount:
    name = "ensure_service_account"

    def check(self, ctx: ProvisionContext) -> str | None:
        n = Names.derive(ctx)
        return n.gsa_email if _gcp(ctx).service_account_exists(n.project, n.gsa_email) else None

    def apply(self, ctx: ProvisionContext) -> str:
        n = Names.derive(ctx)
        return _gcp(ctx).create_service_account(
            n.project, n.gsa_account_id, f"Gojira deploy SA for {ctx.app.name}"
        )


class EnsureWifPool:
    name = "ensure_wif_pool"

    def check(self, ctx: ProvisionContext) -> str | None:
        n = Names.derive(ctx)
        return WIF_POOL if _gcp(ctx).wif_pool_exists(n.project, WIF_POOL) else None

    def apply(self, ctx: ProvisionContext) -> str:
        n = Names.derive(ctx)
        _gcp(ctx).create_wif_pool(n.project, WIF_POOL, "Gojira")
        return WIF_POOL


class EnsureWifProvider:
    name = "ensure_wif_provider"

    def check(self, ctx: ProvisionContext) -> str | None:
        n = Names.derive(ctx)
        return (
            WIF_PROVIDER
            if _gcp(ctx).wif_provider_exists(n.project, WIF_POOL, WIF_PROVIDER)
            else None
        )

    def apply(self, ctx: ProvisionContext) -> str:
        n = Names.derive(ctx)
        _gcp(ctx).create_wif_provider(n.project, WIF_POOL, WIF_PROVIDER, n.repo_owner)
        return WIF_PROVIDER


class BindWifRepo:
    """Grant the repo's principalSet permission to impersonate the deploy GSA."""

    name = "bind_wif_repo"
    role = "roles/iam.workloadIdentityUser"

    def check(self, ctx: ProvisionContext) -> str | None:
        n, gcp = Names.derive(ctx), _gcp(ctx)
        member = n.principal_set(gcp.project_number(n.project))
        return member if gcp.sa_has_binding(n.project, n.gsa_email, member, self.role) else None

    def apply(self, ctx: ProvisionContext) -> str:
        n, gcp = Names.derive(ctx), _gcp(ctx)
        member = n.principal_set(gcp.project_number(n.project))
        gcp.add_sa_binding(n.project, n.gsa_email, member, self.role)
        return member


class GrantIamRoles:
    """Grant the deploy GSA the project roles it needs to push images and deploy."""

    name = "grant_iam_roles"

    def check(self, ctx: ProvisionContext) -> str | None:
        n, gcp = Names.derive(ctx), _gcp(ctx)
        if all(gcp.project_has_binding(n.project, n.gsa_member, r) for r in DEPLOY_ROLES):
            return ",".join(DEPLOY_ROLES)
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        n, gcp = Names.derive(ctx), _gcp(ctx)
        for role in DEPLOY_ROLES:
            if not gcp.project_has_binding(n.project, n.gsa_member, role):
                gcp.add_project_binding(n.project, n.gsa_member, role)
        return ",".join(DEPLOY_ROLES)


class EnsureArtifactRepo:
    name = "ensure_artifact_repo"

    def check(self, ctx: ProvisionContext) -> str | None:
        n = Names.derive(ctx)
        return (
            n.ar_repo
            if _gcp(ctx).artifact_repo_exists(n.project, n.ar_location, n.ar_repo)
            else None
        )

    def apply(self, ctx: ProvisionContext) -> str:
        n = Names.derive(ctx)
        _gcp(ctx).create_artifact_repo(n.project, n.ar_location, n.ar_repo)
        return n.ar_repo


# --- per-environment scope: cluster setup ------------------------------------


class EnsureNamespace:
    """Create the env's namespace on its cluster, labeled as managed.

    The kubectl client is bound to this env's cluster by the orchestrator.
    """

    name = "ensure_namespace"

    def check(self, ctx: ProvisionContext) -> str | None:
        ns = _env(ctx).namespace
        return ns if _kubectl(ctx).namespace_exists(ns) else None

    def apply(self, ctx: ProvisionContext) -> str:
        ns = _env(ctx).namespace
        _kubectl(ctx).ensure_namespace(ns, MANAGED_LABEL)
        return ns


class GrantPromoteSourceReader:
    """Let a copy-target env's GSA read the source env's registry (cross-project).

    Only the last (prod) env crane-copies, and only when its project differs
    from the source (qa) env's project does it need a grant. Same-project — the
    common case and the v1 validation milestone — is a clean no-op.
    """

    name = "grant_promote_source_reader"

    def _source(self, ctx: ProvisionContext) -> Environment | None:
        plan = promotion_plan(ctx.environments)
        return plan[_env(ctx).id].copy_from

    def check(self, ctx: ProvisionContext) -> str | None:
        env = _env(ctx)
        source = self._source(ctx)
        if source is None or source.cluster.gcp_project == env.cluster.gcp_project:
            return "n/a"  # not a cross-project copy target — nothing to grant
        n = Names.for_project(ctx.app, env.cluster.gcp_project, env.cluster.ar_repo)
        return (
            f"{source.cluster.gcp_project}:{PROMOTE_READER_ROLE}"
            if _gcp(ctx).project_has_binding(
                source.cluster.gcp_project, n.gsa_member, PROMOTE_READER_ROLE
            )
            else None
        )

    def apply(self, ctx: ProvisionContext) -> str:
        env = _env(ctx)
        source = self._source(ctx)
        if source is None or source.cluster.gcp_project == env.cluster.gcp_project:
            return "n/a"  # self-safe: nothing to grant if not a cross-project copy
        n = Names.for_project(ctx.app, env.cluster.gcp_project, env.cluster.ar_repo)
        _gcp(ctx).add_project_binding(
            source.cluster.gcp_project, n.gsa_member, PROMOTE_READER_ROLE
        )
        return f"{source.cluster.gcp_project}:{PROMOTE_READER_ROLE}"


# --- per-app scope: repo working tree + setup PR -----------------------------


class PrepareWorkdir:
    """Clone the repo into the working tree and switch to the setup branch."""

    name = "prepare_workdir"
    ephemeral = True  # the workdir dies with the run; a past DONE means nothing

    def check(self, ctx: ProvisionContext) -> str | None:
        if ctx.workdir is None:
            raise ValueError("prepare_workdir requires ctx.workdir")
        return str(ctx.workdir) if (ctx.workdir / ".git").exists() else None

    def apply(self, ctx: ProvisionContext) -> str:
        if ctx.workdir is None:
            raise ValueError("prepare_workdir requires ctx.workdir")
        gh = _github(ctx)
        gh.clone(ctx.app.repo_full_name, ctx.workdir)
        gh.checkout_branch(ctx.workdir, SETUP_BRANCH)
        return str(ctx.workdir)


class GenerateArtifacts:
    """Render workflows + manifests into the repo working tree (idempotent overwrite)."""

    name = "generate_artifacts"
    ephemeral = True  # renders into the per-run workdir

    def check(self, ctx: ProvisionContext) -> str | None:
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        if ctx.workdir is None:
            raise ValueError("generate_artifacts requires ctx.workdir (the repo working tree)")
        spec = build_spec(ctx, _gcp(ctx))
        # Honor the repo's justified overrides: regeneration must produce the
        # sized manifests, not revert them to class defaults.
        existing_spec = ctx.workdir / "gojira.yaml"
        if existing_spec.exists():
            spec = spec.model_copy(
                update={"overrides": read_overrides(existing_spec.read_text())}
            )
        files = render(spec)
        for relpath, content in files.items():
            target = ctx.workdir / relpath
            # Re-onboard case: an existing overlay kustomization may carry
            # operator-owned patches (drift capture). Preserve them — a blind
            # overwrite silently reverts captured cluster state on next deploy.
            if target.name == "kustomization.yaml" and target.exists():
                content = preserve_patches(target.read_text(), content)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        log.info("generated %d artifacts into %s", len(files), ctx.workdir)
        return f"{len(files)} files"


class OpenSetupPr:
    """Commit the generated tree, push the setup branch, open the setup PR."""

    name = "open_setup_pr"

    def check(self, ctx: ProvisionContext) -> str | None:
        url = _github(ctx).existing_pr(ctx.app.repo_full_name, SETUP_BRANCH)
        if url:
            ctx.app.setup_pr_url = url
        return url

    def apply(self, ctx: ProvisionContext) -> str:
        if ctx.workdir is None:
            raise ValueError("open_setup_pr requires ctx.workdir")
        gh = _github(ctx)
        repo = ctx.app.repo_full_name
        committed = gh.commit_all(ctx.workdir, "Add Gojira deploy config")
        if not committed:
            # Re-onboard of an already-configured repo: the render matches what
            # is on the default branch, so there is no setup to propose. Clear
            # setup_pr_url (any previous setup PR is merged history) — the
            # orchestrator reads no-URL as "already active" rather than
            # "awaiting a PR that will never exist".
            log.info("%s already carries the generated config — no setup PR needed", repo)
            ctx.app.setup_pr_url = None
            return "up-to-date (no setup PR needed)"
        gh.push(ctx.workdir, SETUP_BRANCH)
        base = gh.default_branch(repo)
        envs = ", ".join(
            f"`{e.name}`→`{e.cluster.name}`" for e in order_by_promotion(ctx.environments)
        )
        url = gh.open_pr(
            repo,
            head=SETUP_BRANCH,
            base=base,
            title="Add Gojira deploy config",
            body=(
                f"Gojira provisioned deploy config for **{ctx.app.name}** "
                f"(`{ctx.app.workload_class}`).\n\n"
                f"Environments: {envs}.\n\n"
                "Merge to enable push-to-deploy: merge to main → dev, "
                "tag → qa, manual approval → prod."
            ),
        )
        ctx.app.setup_pr_url = url
        return url


# --- promotion topology + spec assembly --------------------------------------


@dataclass(frozen=True)
class EnvPlan:
    """An env's role in the promotion path."""

    copy_from: Environment | None  # crane-copy source, or None to build from scratch
    gated: bool                    # behind the production environment gate
    is_ci: bool                    # the env CI builds + deploys (the first)


def order_by_promotion(envs: list[Environment]) -> list[Environment]:
    """Stable promotion order without needing config: dev, qa, prod, then rest."""
    rank = {"dev": 0, "qa": 1, "prod": 2}
    return sorted(envs, key=lambda e: (rank.get(e.name, 99), e.id))


def promotion_plan(envs: list[Environment]) -> dict[int, EnvPlan]:
    """Map each env id to its promotion role.

    First env is the CI target (builds + deploys on push). Middle envs build
    from scratch on promote. The last env crane-copies from its predecessor
    and is the gated production target. A single-env app has only the CI target.
    """
    ordered = order_by_promotion(envs)
    plan: dict[int, EnvPlan] = {}
    for i, env in enumerate(ordered):
        is_ci = i == 0
        is_last = i == len(ordered) - 1
        copy_from = ordered[i - 1] if (is_last and not is_ci) else None
        plan[env.id] = EnvPlan(copy_from=copy_from, gated=is_last and not is_ci, is_ci=is_ci)
    return plan


def build_spec(ctx: ProvisionContext, gcp: GCPClient) -> GeneratorSpec:
    """Assemble the multi-env GeneratorSpec from app + environments + identity.

    Called after the GCP steps run, so each env's GSA and WIF provider exist.
    One EnvSpec per environment, each carrying its own project-scoped identity.
    """
    ordered = order_by_promotion(ctx.environments)
    plan = promotion_plan(ordered)
    env_specs: list[EnvSpec] = []
    for env in ordered:
        cluster = env.cluster
        n = Names.for_project(ctx.app, cluster.gcp_project, cluster.ar_repo)
        p = plan[env.id]
        env_specs.append(
            EnvSpec(
                name=env.name,
                namespace=env.namespace,
                project=cluster.gcp_project,
                cluster_name=cluster.name,
                location=cluster.location,
                gke_cluster=cluster.gke_cluster,
                ar_repo=cluster.ar_repo,
                wif_provider=n.wif_provider_name(gcp.project_number(cluster.gcp_project)),
                gsa_email=n.gsa_email,
                copy_from=p.copy_from.name if p.copy_from else None,
                gated=p.gated,
            )
        )
    return GeneratorSpec(
        app=ctx.app.name,
        repo_full_name=ctx.app.repo_full_name,
        workload_class=ctx.app.workload_class,
        environments=env_specs,
    )


def _env(ctx: ProvisionContext) -> Environment:
    if ctx.environment is None:
        raise ValueError("step requires ctx.environment (per-env scope)")
    return ctx.environment


# --- scope sequences ---------------------------------------------------------


def per_project_steps() -> list[Step]:
    """Identity: SA, WIF pool/provider, repo binding, IAM roles, Artifact Registry."""
    return [
        EnsureApisEnabled(),
        EnsureServiceAccount(),
        EnsureWifPool(),
        EnsureWifProvider(),
        BindWifRepo(),
        GrantIamRoles(),
        EnsureArtifactRepo(),
    ]


def per_env_steps() -> list[Step]:
    """Per-environment cluster setup: namespace + any cross-project promote grant."""
    return [EnsureNamespace(), GrantPromoteSourceReader()]


def per_app_steps() -> list[Step]:
    """The repo flow: clone the working tree, render artifacts, open the setup PR."""
    return [PrepareWorkdir(), GenerateArtifacts(), OpenSetupPr()]
