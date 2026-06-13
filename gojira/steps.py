"""The GCP-side provision steps and the default sequence.

Each step derives its concrete parameters from the ProvisionContext
(app + cluster) and calls the GCPClient. Steps are thin: naming and
derivation live here, the actual cloud operations live in GCPClient.
"""

import logging
from dataclasses import dataclass

from gojira.gcp import GCPClient
from gojira.generator import GeneratorSpec, render
from gojira.provisioner import ProvisionContext, Step

log = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class Names:
    """Concrete resource identifiers derived from an app + cluster."""

    project: str
    gsa_account_id: str
    gsa_email: str
    repo_owner: str
    repo_full_name: str
    ar_location: str
    ar_repo: str

    @classmethod
    def derive(cls, ctx: ProvisionContext) -> "Names":
        project = ctx.cluster.gcp_project
        account_id = f"{ctx.app.name}-deploy"
        owner, _, _ = ctx.app.repo_full_name.partition("/")
        # cluster.ar_repo is "<location>-docker.pkg.dev/<project>/<repo>"
        host, _, path = ctx.cluster.ar_repo.partition("/")
        ar_location = host.removesuffix("-docker.pkg.dev")
        ar_repo = path.rsplit("/", 1)[-1]
        return cls(
            project=project,
            gsa_account_id=account_id,
            gsa_email=f"{account_id}@{project}.iam.gserviceaccount.com",
            repo_owner=owner,
            repo_full_name=ctx.app.repo_full_name,
            ar_location=ar_location,
            ar_repo=ar_repo,
        )

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


class EnsureApisEnabled:
    name = "ensure_apis_enabled"

    def check(self, ctx: ProvisionContext) -> str | None:
        gcp = _gcp(ctx)
        project = ctx.cluster.gcp_project
        if all(gcp.api_enabled(project, api) for api in REQUIRED_APIS):
            return ",".join(REQUIRED_APIS)
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        gcp = _gcp(ctx)
        project = ctx.cluster.gcp_project
        for api in REQUIRED_APIS:
            if not gcp.api_enabled(project, api):
                gcp.enable_api(project, api)
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
        n = Names.derive(ctx)
        gcp = _gcp(ctx)
        member = n.principal_set(gcp.project_number(n.project))
        return member if gcp.sa_has_binding(n.project, n.gsa_email, member, self.role) else None

    def apply(self, ctx: ProvisionContext) -> str:
        n = Names.derive(ctx)
        gcp = _gcp(ctx)
        member = n.principal_set(gcp.project_number(n.project))
        gcp.add_sa_binding(n.project, n.gsa_email, member, self.role)
        return member


class GrantIamRoles:
    """Grant the deploy GSA the project roles it needs to push images and deploy."""

    name = "grant_iam_roles"

    def check(self, ctx: ProvisionContext) -> str | None:
        n = Names.derive(ctx)
        gcp = _gcp(ctx)
        if all(gcp.project_has_binding(n.project, n.gsa_member, r) for r in DEPLOY_ROLES):
            return ",".join(DEPLOY_ROLES)
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        n = Names.derive(ctx)
        gcp = _gcp(ctx)
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


def build_spec(ctx: ProvisionContext, gcp: GCPClient) -> GeneratorSpec:
    """Assemble the GeneratorSpec from app + cluster + provisioned identity.

    Called after the GCP steps run, so the GSA and WIF provider the
    generated workflows reference actually exist.
    """
    n = Names.derive(ctx)
    num = gcp.project_number(n.project)
    return GeneratorSpec(
        app=ctx.app.name,
        repo_full_name=ctx.app.repo_full_name,
        namespace=ctx.app.namespace,
        workload_class=ctx.app.workload_class,
        cluster_name=ctx.cluster.name,
        gcp_project=ctx.cluster.gcp_project,
        location=ctx.cluster.location,
        gke_cluster=ctx.cluster.gke_cluster,
        ar_repo=ctx.cluster.ar_repo,
        wif_provider=n.wif_provider_name(num),
        gsa_email=n.gsa_email,
        environments=ctx.config.defaults.environments,
    )


class GenerateArtifacts:
    """Render workflows + manifests into the repo working tree.

    Idempotent by overwrite: re-rendering the same spec produces identical
    files, so there's nothing to 'check' — apply() always regenerates.
    """

    name = "generate_artifacts"

    def check(self, ctx: ProvisionContext) -> str | None:
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        if ctx.workdir is None:
            raise ValueError("generate_artifacts requires ctx.workdir (the repo working tree)")
        files = render(build_spec(ctx, _gcp(ctx)))
        for relpath, content in files.items():
            target = ctx.workdir / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        log.info("generated %d artifacts into %s", len(files), ctx.workdir)
        return f"{len(files)} files"


def default_gcp_steps() -> list[Step]:
    """The GCP-side provision sequence. GitHub + kubectl steps append later."""
    return [
        EnsureApisEnabled(),
        EnsureServiceAccount(),
        EnsureWifPool(),
        EnsureWifProvider(),
        BindWifRepo(),
        GrantIamRoles(),
        EnsureArtifactRepo(),
    ]


def default_steps() -> list[Step]:
    """The full phase-1 provision sequence built so far (GCP + artifact generation)."""
    return [*default_gcp_steps(), GenerateArtifacts()]
