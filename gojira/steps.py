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

import json
import logging
import shutil
import time
from dataclasses import dataclass

import yaml
from pathlib import Path

from gojira.gcp import GCPClient, owners_in_condition
from gojira.generator import (
    CLUSTER_SCOPED_KINDS,
    EnvSpec,
    GeneratorSpec,
    add_resources,
    contract_fingerprint,
    infer_app_needs,
    preserve_generators,
    preserve_patches,
    preserve_resources,
    read_app_class,
    read_app_needs,
    read_overrides,
    render,
    seed_needs,
)
from gojira.github import SETUP_BRANCH, GitHubClient
from gojira.k8s import K8sError, KubectlClient, kustomize_build
from gojira.models import App, AppStatus, Environment
from gojira.opinions import WORKLOAD_CLASSES
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
    """The pool's GitHub provider, admitting this repo's owner.

    "Exists" was the whole check until 2026-09-30: the provider a first
    owner's connect created said `repository_owner == 'shaz'`, and every repo
    a second owner (the tenishai org) connected passed provisioning and then
    failed its first token exchange in CI. Now the condition is read, and a
    missing owner is added to it. A condition Gojira did not write (any other
    shape) is a person's, and is kept with a warning rather than rewritten.
    """

    name = "ensure_wif_provider"

    def check(self, ctx: ProvisionContext) -> str | None:
        n = Names.derive(ctx)
        gcp = _gcp(ctx)
        if not gcp.wif_provider_exists(n.project, WIF_POOL, WIF_PROVIDER):
            return None
        owners = owners_in_condition(gcp.wif_provider_condition(n.project, WIF_POOL, WIF_PROVIDER))
        if owners is None:
            log.warning(
                "wif provider %s/%s has a hand-written condition; not touching it — "
                "make sure it admits repository_owner %r", WIF_POOL, WIF_PROVIDER, n.repo_owner,
            )
            return WIF_PROVIDER
        return WIF_PROVIDER if n.repo_owner in owners else None

    def apply(self, ctx: ProvisionContext) -> str:
        n = Names.derive(ctx)
        gcp = _gcp(ctx)
        if gcp.wif_provider_exists(n.project, WIF_POOL, WIF_PROVIDER):
            owners = owners_in_condition(gcp.wif_provider_condition(n.project, WIF_POOL, WIF_PROVIDER)) or []
            gcp.set_wif_provider_owners(n.project, WIF_POOL, WIF_PROVIDER, [*owners, n.repo_owner])
        else:
            gcp.create_wif_provider(n.project, WIF_POOL, WIF_PROVIDER, n.repo_owner)
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


class EnsureClusterDnsAccess:
    """Generated CI reaches this env's control plane through GKE's DNS-based
    endpoint — the only path that works for private clusters from GitHub
    runners, and IAM-authenticated everywhere (finding #14). Ensure it's on."""

    name = "ensure_cluster_dns_access"

    def check(self, ctx: ProvisionContext) -> str | None:
        env = _env(ctx)
        c = env.cluster
        if _gcp(ctx).dns_access_enabled(c.gcp_project, c.location, c.gke_cluster):
            return f"{c.gke_cluster}: dns access on"
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        env = _env(ctx)
        c = env.cluster
        _gcp(ctx).enable_dns_access(c.gcp_project, c.location, c.gke_cluster)
        return f"{c.gke_cluster}: dns access enabled"


class EnsureNamespace:
    """Create the env's namespace on its cluster, labeled as managed plus the
    operator's configured labels (defaults.namespace_labels — secrets
    infrastructure often gates on these; missing labels mean ExternalSecrets
    silently never sync).

    The kubectl client is bound to this env's cluster by the orchestrator.
    An existing namespace with missing labels is NOT skipped: apply re-asserts
    labels so already-provisioned namespaces converge on re-onboard.
    """

    name = "ensure_namespace"

    def _labels(self, ctx: ProvisionContext) -> dict[str, str]:
        return {**MANAGED_LABEL, **ctx.config.defaults.namespace_labels}

    def check(self, ctx: ProvisionContext) -> str | None:
        ns = _env(ctx).namespace
        kubectl = _kubectl(ctx)
        if not kubectl.namespace_exists(ns):
            return None
        live = kubectl.get_resource(ns, "namespace", ns) or {}
        have = live.get("metadata", {}).get("labels") or {}
        want = self._labels(ctx)
        return ns if all(have.get(k) == v for k, v in want.items()) else None

    def apply(self, ctx: ProvisionContext) -> str:
        ns = _env(ctx).namespace
        _kubectl(ctx).ensure_namespace(ns, self._labels(ctx))
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
        seed: dict = {}
        # Honor the repo's contract: regeneration must produce the sized
        # manifests (overrides) and the app's declared needs (finding #12),
        # not revert them to bare class defaults.
        existing_spec = ctx.workdir / "gojira.yaml"
        if not existing_spec.exists():
            # Adopt-mode (zero-touch onboarding): no contract yet, but the
            # repo's existing deployment declares its needs — read them and
            # seed the contract the setup PR proposes for review.
            old_deployment = ctx.workdir / "k8s" / "base" / "deployment.yaml"
            if old_deployment.exists():
                ctx.adopted = True  # human content: MergeSetupPr refuses to auto-merge
                inferred = infer_app_needs(old_deployment.read_text())
                if inferred:
                    spec = spec.model_copy(update=inferred)
                    log.info("adopt: inferred needs from existing deployment: %s",
                             sorted(inferred))
            else:
                # A repo with nothing of its own to read. Only here may the
                # caller that connected it shape the first contract (its
                # `seed`): once a person's contract or manifests exist, they
                # are what is read, and a seed is never laid over them.
                seed = _setup_seed(ctx.app)
                if seed.get("needs"):
                    spec = spec.model_copy(update=seed_needs(seed["needs"]))
                    ctx.seed_needs = sorted(seed["needs"])
        if existing_spec.exists():
            text = existing_spec.read_text()
            spec = spec.model_copy(
                update={"overrides": read_overrides(text), **read_app_needs(text)}
            )
            # Finding #16: class belongs to the contract too. An unknown class
            # is left to the DB (fail open with a log, not a dead provision).
            repo_class = read_app_class(text)
            if repo_class and repo_class != spec.workload_class:
                if repo_class in WORKLOAD_CLASSES:
                    spec = spec.model_copy(update={"workload_class": repo_class})
                    ctx.app.workload_class = repo_class  # DB converges
                    log.info("class from contract: %s (db updated)", repo_class)
                else:
                    log.warning("ignoring unknown class %r in gojira.yaml", repo_class)
        ctx.spec = spec  # the contract-applied spec, for the steps after this one
        _refuse_unknown_variable_refs(spec, ctx.config)
        _refuse_image_with_build_args(spec)
        files = render(spec)
        if existing_spec.exists():
            files["gojira.yaml"] = _keep_equal_contract(
                existing_spec.read_text(), files["gojira.yaml"]
            )
        rendered_targets = _rendered_targets(files)

        # Finding #12: app-authored resources (ExternalSecrets, ServiceAccounts,
        # PVCs) survive regeneration. Their (kind, name) also count as rendered
        # for the orphaned-patch check — a patch targeting a preserved resource
        # is not orphaned.
        base_dir = ctx.workdir / "k8s" / "base"
        base_kust = base_dir / "kustomization.yaml"
        base_key = "k8s/base/kustomization.yaml"
        if base_kust.exists() and base_key in files:
            files[base_key] = preserve_resources(
                base_kust.read_text(),
                files[base_key],
                file_exists=lambda e: (base_dir / e).is_file(),
            )
            rendered_targets |= _preserved_targets(base_dir, files[base_key])

        # The seed's files, beside the render's. Never over one: a name the
        # render produces, or the repo already has, stays whose it is.
        for name, text in (seed.get("base_files") or {}).items():
            relpath = f"k8s/base/{name}"
            if relpath in files or (base_dir / name).exists():
                continue
            files[relpath] = text
            ctx.seed_files.append(name)
        if ctx.seed_files and base_key in files:
            # Listed exactly as preserve_resources will list them once the PR
            # is merged, so the next render of the merged repo changes nothing
            # and asks its owner for no second pull request.
            files[base_key] = add_resources(files[base_key], ctx.seed_files)
            rendered_targets |= _rendered_targets(files)

        _refuse_cluster_scoped_base(ctx.workdir / "k8s" / "base", files)

        for relpath, content in files.items():
            target = ctx.workdir / relpath
            # Re-onboard case: an existing overlay kustomization may carry
            # operator-owned patches (drift capture). Preserve them — a blind
            # overwrite silently reverts captured cluster state on next deploy.
            # But drop patches whose target the render no longer produces (an
            # app rename orphans them and kustomize hard-fails the build).
            if target.name == "kustomization.yaml" and target.exists():
                content = preserve_generators(target.read_text(), content)
                content = preserve_patches(
                    target.read_text(),
                    content,
                    keep_path=lambda p, d=target.parent: _patch_targets_rendered(
                        d / p, rendered_targets
                    ),
                    keep_target=lambda k, n: _inline_target_rendered(k, n, rendered_targets),
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        ctx.stale_kept = prune_stale_generated(ctx.workdir, files)
        build_overlays(ctx.workdir, ctx.environments)
        log.info("generated %d artifacts into %s", len(files), ctx.workdir)
        return f"{len(files)} files"


def _setup_seed(app: App) -> dict:
    """The seed the app was connected with (`App.setup_seed_json`), or `{}`.
    Validated when it was stored; a row that does not parse is no seed."""
    try:
        seed = json.loads(app.setup_seed_json or "{}")
    except ValueError:
        return {}
    return seed if isinstance(seed, dict) else {}


def build_overlays(workdir, environments) -> None:
    """Build every environment overlay in the working tree, before anything is
    committed (finding #83).

    The render is correct by construction, but the preserved parts are not:
    an app-authored patch can name a `namespace:` (kustomize matches patches
    before the overlay's namespace transformer runs, so the target is never
    found), or target a field the base no longer has. Until now nothing built
    what the step wrote, so the setup PR carried a broken overlay under a green
    check and the generated CI failed twenty apply retries later with the
    reason buried. A render that cannot build now fails this step with
    kustomize's own message, and the app row says why.
    """
    for env in environments:
        overlay = workdir / "k8s" / "overlays" / env.name
        try:
            kustomize_build(overlay)
        except K8sError as exc:
            raise ValueError(
                f"k8s/overlays/{env.name} does not build, so nothing was committed; "
                f"fix the overlay (usually an app-authored patch) and provision again: {exc}"
            ) from exc


def _keep_equal_contract(existing: str, rendered: str) -> str:
    """The author's gojira.yaml, byte-for-byte, when it means what the render
    means (finding #84).

    Regeneration used to rewrite the file from the template even when every
    need, the class and the overrides came back identical, so the setup PR
    showed a contract change that was not one and the author's comments were
    replaced with boilerplate. Equality is the contract fingerprint, the same
    sorted-JSON meaning the generated workflow is stamped with, so keeping the
    old text changes nothing CI checks. A file that does not parse is simply
    replaced, as before.
    """
    try:
        same = contract_fingerprint(existing) == contract_fingerprint(rendered)
    except yaml.YAMLError:
        return rendered
    return existing if same else rendered


GENERATED_MARKER = "# Generated by Gojira"


def prune_stale_generated(workdir, files: dict[str, str]) -> list[str]:
    """Remove what Gojira wrote for environments the app no longer has
    (finding #96); report what it found there but did not write.

    Gojira owns two places in the tree: `k8s/overlays/*` and
    `.github/workflows/gojira-*.yaml`. A render only ever wrote the current
    environments' files, so an overlay or a promote workflow from a removed
    environment stayed behind, stale, naming a project this install may not
    even have — and GitHub still parses a leftover workflow. A file in those
    places that starts with Gojira's own header and is not in this render is
    deleted. A file that does not carry the header was not Gojira's to write,
    so it is kept and returned for the setup PR to list. An app-authored patch
    inside a stale overlay goes with the overlay: it patched an environment
    that no longer exists, which is worth a warning, not a reason to keep
    deploying nothing.
    """
    rendered_overlays = {
        Path(rel).parts[2] for rel in files if rel.startswith("k8s/overlays/")
    }
    kept: list[str] = []
    overlays = workdir / "k8s" / "overlays"
    if overlays.is_dir():
        for env_dir in sorted(p for p in overlays.iterdir() if p.is_dir()):
            if env_dir.name in rendered_overlays:
                continue
            kust = env_dir / "kustomization.yaml"
            if not (kust.is_file() and _gojira_wrote(kust)):
                kept.extend(sorted(str(p.relative_to(workdir)) for p in env_dir.rglob("*") if p.is_file()))
                continue
            extras = sorted(
                str(p.relative_to(workdir)) for p in env_dir.rglob("*") if p.is_file() and p != kust
            )
            if extras:
                log.warning(
                    "overlay %s is for an environment the app no longer has; removing it "
                    "with the app-authored files inside: %s", env_dir.name, ", ".join(extras),
                )
            shutil.rmtree(env_dir)
            log.info("removed stale overlay k8s/overlays/%s", env_dir.name)
    workflows = workdir / ".github" / "workflows"
    if workflows.is_dir():
        for wf in sorted(workflows.glob("gojira-*.yaml")):
            rel = str(wf.relative_to(workdir))
            if rel in files:
                continue
            if _gojira_wrote(wf):
                wf.unlink()
                log.info("removed stale workflow %s", rel)
            else:
                kept.append(rel)
    return kept


def _gojira_wrote(path) -> bool:
    try:
        with path.open() as fh:
            return fh.readline().startswith(GENERATED_MARKER)
    except OSError:
        return False


def _refuse_unknown_variable_refs(spec, config) -> None:
    """A `${NAME}` in build_args must name a variable this install sets
    (issue #102); otherwise the generated workflow would fail at build time
    with "NAME is not set", and the app row should say so now instead."""
    unknown = [r for r in spec.build_arg_refs() if r not in config.ci.repo_variables]
    if unknown:
        raise ValueError(
            "gojira.yaml build_args reference repository variables this install does "
            f"not set: {', '.join(unknown)}. Name them in GOJIRA_REPO_VARIABLES "
            "(gojira.env) and `make install`, or write a literal value."
        )


def _refuse_image_with_build_args(spec) -> None:
    """`image:` deploys a published image, so there is no build to pass
    arguments to (issue #108). Both keys at once means the author expects a
    build that will not happen; say so now rather than ship the image with
    the arguments silently ignored."""
    if spec.published_image and spec.build_args:
        raise ValueError(
            "gojira.yaml names both image: and build_args:, but an image is copied, "
            "not built, so the build args would be ignored. Drop build_args (and the "
            "Dockerfile, if any), or drop image: to build this repo."
        )


def _refuse_cluster_scoped_base(base_dir, files: dict[str, str]) -> None:
    """A base holding a cluster-scoped object cannot deploy: the generated
    CI applies as the app's identity, which cannot create ClusterRoles and
    must not (issue #86: twenty failed apply attempts on kobo's first
    deploy). Say so here, naming the file, before a setup PR is opened —
    and say what to write instead."""
    base_key = "k8s/base/kustomization.yaml"
    if base_key not in files:
        return
    try:
        entries = (yaml.safe_load(files[base_key]) or {}).get("resources") or []
    except yaml.YAMLError:
        return
    for entry in entries:
        relpath = f"k8s/base/{entry}"
        text = files.get(relpath)
        if text is None:
            path = base_dir / entry
            if not path.is_file():
                continue
            text = path.read_text()
        try:
            docs = [d for d in yaml.safe_load_all(text) if d]
        except yaml.YAMLError:
            continue
        for doc in docs:
            if doc.get("kind") in CLUSTER_SCOPED_KINDS:
                raise IdentityRefused(
                    f"{relpath} holds a {doc['kind']}, which the app's deploy identity "
                    "cannot create (and must not): keep k8s/base to namespaced objects. "
                    "For cluster access, have the operator apply the ClusterRole and name "
                    "it in gojira.yaml `cluster_roles:`; Gojira makes the binding."
                )


def _rendered_targets(files: dict[str, str]) -> set[tuple[str, str]]:
    """(kind, name) of every object the render produces under k8s/base."""
    targets = set()
    for relpath, content in files.items():
        if not relpath.startswith("k8s/base/") or relpath.endswith("kustomization.yaml"):
            continue
        try:
            for doc in yaml.safe_load_all(content):
                if doc and doc.get("kind") and doc.get("metadata", {}).get("name"):
                    targets.add((doc["kind"], doc["metadata"]["name"]))
        except yaml.YAMLError:
            continue
    return targets


def _preserved_targets(base_dir, kustomization_text: str) -> set[tuple[str, str]]:
    """(kind, name) of objects in the preserved (non-generated) resource files
    listed by the final base kustomization. Unreadable files contribute nothing."""
    targets = set()
    try:
        entries = (yaml.safe_load(kustomization_text) or {}).get("resources") or []
    except yaml.YAMLError:
        return targets
    for entry in entries:
        path = base_dir / str(entry)
        try:
            for doc in yaml.safe_load_all(path.read_text()):
                if doc and doc.get("kind") and doc.get("metadata", {}).get("name"):
                    targets.add((doc["kind"], doc["metadata"]["name"]))
        except (OSError, yaml.YAMLError):
            continue
    return targets


def _inline_target_rendered(kind: str, name: str, rendered: set[tuple[str, str]]) -> bool:
    """The inline-patch twin of `_patch_targets_rendered`: the target is named
    in the entry itself, so there is no file to read and nothing to fail open on."""
    keep = (kind, name) in rendered
    if not keep:
        log.info("dropping orphaned inline patch (targets %s/%s, no longer rendered)", kind, name)
    return keep


def _patch_targets_rendered(patch_path, rendered: set[tuple[str, str]]) -> bool:
    """Does this overlay patch target an object the render still produces?
    Unreadable/unparseable patches are kept (fail open — a human wired them)."""
    try:
        doc = yaml.safe_load(patch_path.read_text())
    except (OSError, yaml.YAMLError):
        return True
    kind = (doc or {}).get("kind")
    name = (doc or {}).get("metadata", {}).get("name")
    if not kind or not name:
        return True
    keep = (kind, name) in rendered
    if not keep:
        log.info("dropping orphaned patch %s (targets %s/%s, no longer rendered)",
                 patch_path.name, kind, name)
    return keep


class OpenSetupPr:
    """Commit the generated tree, push the setup branch, open the setup PR."""

    name = "open_setup_pr"
    # Finding #13, the real root: a DONE ledger row must never skip this step.
    # Its "resource" is the setup branch/PR, whose freshness the ledger cannot
    # know — after a generator upgrade the same PR needs new content. Like the
    # workdir steps (finding #6), check()/apply() re-decide against the real
    # world each run; apply() is idempotent (up-to-date concludes, changed
    # content force-pushes under the same PR).
    ephemeral = True

    def check(self, ctx: ProvisionContext) -> str | None:
        # An open setup PR is NOT proof the branch matches the render — after a
        # generator upgrade, Retry must refresh it in place (finding #13: the
        # old check treated PR-exists as done, so re-provision regenerated the
        # workdir and then threw it away). apply() is idempotent: it pushes
        # only when the render actually changed something.
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        if ctx.workdir is None:
            raise ValueError("open_setup_pr requires ctx.workdir")
        gh = _github(ctx)
        repo = ctx.app.repo_full_name
        existing = gh.existing_pr(repo, SETUP_BRANCH)
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
        if existing:
            # Same PR, fresh content — the branch was force-pushed under it.
            ctx.app.setup_pr_url = existing
            return f"refreshed {existing}"
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
                + _identity_summary(ctx)
                + _seed_summary(ctx)
                + _stale_summary(ctx)
                + "Merge to enable push-to-deploy: merge to main → dev, "
                "tag → qa, manual approval → prod."
            ),
        )
        ctx.app.setup_pr_url = url
        return url


def _seed_summary(ctx: ProvisionContext) -> str:
    """What the caller that connected the app asked this pull request to
    carry beside Gojira's own render, and its own words for why: the reviewer
    is agreeing to that too."""
    if not ctx.seed_needs and not ctx.seed_files:
        return ""
    lines = ["Asked for by the service that connected this app, beside what Gojira renders:"]
    if ctx.seed_needs:
        lines.append("- in `gojira.yaml`: " + ", ".join(f"`{k}`" for k in ctx.seed_needs))
    lines += [f"- `k8s/base/{name}`" for name in ctx.seed_files]
    note = (_setup_seed(ctx.app).get("note") or "").strip()
    return "\n".join(lines) + "\n\n" + (note + "\n\n" if note else "")


def _stale_summary(ctx: ProvisionContext) -> str:
    """Files in Gojira's directories that it did not write and so did not
    remove (finding #96): a human decides."""
    if not ctx.stale_kept:
        return ""
    return (
        "Not mine, not removed — found in the directories Gojira owns "
        "(`k8s/overlays/`, `.github/workflows/gojira-*.yaml`) without its header, "
        "for no current environment:\n"
        + "".join(f"- `{f}`\n" for f in ctx.stale_kept)
        + "\n"
    )


def _identity_summary(ctx: ProvisionContext) -> str:
    """What the reviewer of this contract is agreeing to grant (design §1,
    resolved question 3): the pod's GSA per project and its roles."""
    spec = ctx.spec
    if spec is None or (spec.identity is None and not spec.cluster_roles):
        return ""
    lines = []
    if spec.identity is not None:
        lines.append("Identity (gojira.yaml `identity:`): the pod runs as")
        for project, gsa in sorted({(e.project, spec.run_gsa(e)) for e in spec.environments}):
            kind = "adopted" if spec.identity.gsa else "made by Gojira"
            lines.append(f"- `{gsa}` on `{project}` ({kind})")
        if spec.identity.gcp_roles:
            lines.append("with " + ", ".join(f"`{r}`" for r in spec.identity.gcp_roles) + " on each project.")
    if spec.cluster_roles:
        lines.append(
            "Cluster access (gojira.yaml `cluster_roles:`): the pod's ServiceAccount is bound to "
            + ", ".join(f"`{r}`" for r in spec.cluster_roles) + " on each environment's cluster."
        )
    return "\n".join(lines) + "\n\n"


class MergeSetupPr:
    """Auto-merge the setup PR when the app opted in (caller-created repos).

    The one-click contract: a programmatic caller that authored the repo has
    no human content to protect, so the review gate is theirs to waive. Like
    OpenSetupPr, re-decides against the real world each run.
    """

    name = "merge_setup_pr"
    ephemeral = True

    def check(self, ctx: ProvisionContext) -> str | None:
        if not ctx.app.auto_merge_setup_pr:
            return "auto-merge not requested"
        if not ctx.app.setup_pr_url:
            return "no setup PR to merge"
        if ctx.adopted:
            # Finding #22: a programmatic caller asked for auto-merge on a
            # repo it did NOT author — hand-built manifests, no contract. The
            # one-click contract above only waives review for repos with no
            # human content; this one regenerates a human's k8s/ (and can
            # rename their objects out from under other overlays). The PR
            # stays open for a person; the app waits in awaiting_pr.
            log.warning("auto-merge refused for %s: adopted a repo with hand-built "
                        "manifests; the setup PR %s needs a human review",
                        ctx.app.name, ctx.app.setup_pr_url)
            return "auto-merge refused: adopted hand-built manifests, review the setup PR"
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        gh = _github(ctx)
        repo = ctx.app.repo_full_name
        url = ctx.app.setup_pr_url
        if gh.pr_merged(repo, url):
            # Retry after a partial failure: a prior attempt's merge_pr landed
            # server-side but the client raised before setup_pr_url cleared,
            # so a naive retry would re-merge an already-merged PR and wedge
            # the step at FAILED forever. Re-decide against the real world
            # (OpenSetupPr's idiom) instead of blindly re-calling merge_pr.
            #
            # This guard is PR-specific (checks THIS url, not repo history):
            # setup_pr_merged() answers "was any closed setup PR ever merged",
            # which stays true forever after the first merge. Re-provisioning
            # an auto-merge app opens a fresh PR under the same setup branch
            # (OpenSetupPr refreshes/reopens it) — a repo-history guard would
            # short-circuit here, erase setup_pr_url, and silently orphan the
            # fresh PR every time.
            ctx.app.status = AppStatus.ACTIVE  # load-bearing for direct apply() callers
            ctx.app.setup_pr_url = None
            return "already merged"
        gh.merge_pr(repo, url)
        # Load-bearing for direct apply() callers; provision_app's recompute
        # derives the same status from the cleared setup_pr_url below.
        ctx.app.status = AppStatus.ACTIVE
        # Mirrors activate_app's convention (service.py): the URL is merged
        # history now. provision_app recomputes app.status from setup_pr_url
        # right after per_app_steps runs (AWAITING_PR if still set, else
        # ACTIVE) — leaving it set here would drag the app straight back to
        # AWAITING_PR before anyone observes the ACTIVE we just set.
        ctx.app.setup_pr_url = None
        return f"merged {url}"


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
        # The platform's architecture: what the runners build and the
        # clusters run. The contract's own `arch:`/`node_selector:` replace
        # these when GenerateArtifacts reads the repo's gojira.yaml.
        platform_arch=ctx.config.defaults.arch or "amd64",
        platform_node_selector=dict(ctx.config.defaults.node_selector),
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


OBSERVE_SUMMARY_PREFIX = "ARGUS_SUMMARY "


class ObserveApp:
    """Born observed: hand the app's contract to an in-cluster `noto observe`
    Job and record its one-line summary in the ledger (design agreed with
    noto — then named argus — 2026-09-17/18 collab thread).

    Ephemeral, so Provision-again re-observes after contract edits — noto's
    observe is idempotent and its Job deliberately runs without a persistent
    ledger, re-checking the live world every run. Failure is loud but
    isolated: this step runs in its own non-halting pass (an unobserved app
    beats an undeployed one), and the summary line — success or failure —
    is the ledger row, so a failed observe explains itself in the UI.
    """

    name = "observe"
    ephemeral = True

    def check(self, ctx: ProvisionContext) -> str | None:
        cfg = ctx.config.observe
        cluster_name = ctx.environment.cluster.name
        if not cfg.job_image or cluster_name not in cfg.clusters:
            return f"noto not configured for cluster {cluster_name}"
        if ctx.workdir is None or not (ctx.workdir / "gojira.yaml").is_file():
            return "no contract in the working tree"
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        cfg = ctx.config.observe
        ocfg = cfg.clusters[ctx.environment.cluster.name]
        contract = (ctx.workdir / "gojira.yaml").read_text()
        # Same canonicalization the generated CI stamps (#42); noto refuses
        # to apply anything when its own recomputation disagrees.
        sha = contract_fingerprint(contract)
        job_name = _observe_job_name(ctx.app.name, ctx.environment.name, int(time.time()))
        kubectl = _kubectl(ctx)
        kubectl.apply(yaml.safe_dump(
            _observe_job(job_name, ocfg, cfg, contract, sha, ctx.environment.name),
            sort_keys=False,
        ))
        failure = kubectl.wait_job(ocfg.namespace, job_name, cfg.active_deadline_seconds + 60)
        logs = kubectl.job_logs(ocfg.namespace, job_name)
        summary = ""
        for line in reversed(logs.splitlines()):
            if line.startswith(OBSERVE_SUMMARY_PREFIX):
                summary = line[len(OBSERVE_SUMMARY_PREFIX):].strip()
                break
        if failure:
            # noto's own line when it got that far; else the Job's failure
            # reason, so a row for a pull failure or deadline still explains itself.
            raise RuntimeError(summary or f"observe job {job_name} did not complete: {failure}")
        return summary or f"{job_name} complete (no summary line)"


# noto's Dockerfile: `useradd --uid 10001 noto` + `USER 10001`.
NOTO_IMAGE_UID = 10001


def _observe_job_name(app: str, env: str, epoch: int) -> str:
    """Unique per run — a lingering completed Job (ttl not yet elapsed) would
    reject an apply that changes immutable fields. The epoch leads and the
    variable-length app name trails, so the 63-char DNS-label cut trims the
    app name, never the uniqueness (review of #49)."""
    return f"noto-observe-{epoch}-{env}-{app}"[:63].rstrip("-")


def _observe_job(name, ocfg, cfg, contract_yaml, sha, env_name) -> dict:
    """The Job spec, mirroring noto's README manifest ("Running observe as a
    Job") — SA and security context are noto's own prescription; env vars are
    the agreed I/O contract. The wire names (`ARGUS_ENV`, the `ARGUS_SUMMARY `
    line) kept their birth name through the rename: noto's job mode still
    reads and prints them."""
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": ocfg.namespace,
            "labels": {"app.kubernetes.io/managed-by": "gojira"},
        },
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": cfg.ttl_seconds,
            "activeDeadlineSeconds": cfg.active_deadline_seconds,
            "template": {"spec": {
                "serviceAccountName": ocfg.service_account,
                "automountServiceAccountToken": True,
                "restartPolicy": "Never",
                "securityContext": {
                    "runAsNonRoot": True,
                    # Pinned like every other token-reading workload on the estate. (The
                    # first theory — that without a pod-level uid the projected token was
                    # unreadable, causing the first live run's "platform record
                    # unreadable" — was falsified; see this pin's commit message. The pin
                    # stayed as hardening.)
                    "runAsUser": NOTO_IMAGE_UID,
                    "runAsGroup": NOTO_IMAGE_UID,
                    "fsGroup": NOTO_IMAGE_UID,
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "volumes": [{"name": "tmp", "emptyDir": {}}],
                "containers": [{
                    "name": "observe",
                    "image": cfg.job_image,
                    "args": ["observe"],
                    "env": [
                        {"name": "GOJIRA_CONTRACT_YAML", "value": contract_yaml},
                        {"name": "GOJIRA_CONTRACT_SHA256", "value": sha},
                        {"name": "ARGUS_ENV", "value": env_name},
                    ],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"memory": "512Mi"},
                    },
                }],
            }},
        },
    }


def observe_steps() -> list[Step]:
    """The non-halting observe pass — after the repo flow, per environment."""
    return [ObserveApp()]


def per_env_steps() -> list[Step]:
    """Per-environment cluster setup: DNS access for CI, namespace, promote grant."""
    return [EnsureClusterDnsAccess(), EnsureNamespace(), GrantPromoteSourceReader()]


def _app_spec(ctx: ProvisionContext) -> GeneratorSpec:
    """The app's spec with its contract applied — set by GenerateArtifacts,
    which always runs (ephemeral); built bare for a step run on its own."""
    return ctx.spec if ctx.spec is not None else build_spec(ctx, _gcp(ctx))


class IdentityRefused(RuntimeError):
    """The contract asked for an identity this install does not grant."""


def _run_identity(ctx: ProvisionContext):
    """(spec, identity) or (spec, None) when the contract declares none."""
    spec = _app_spec(ctx)
    return spec, spec.identity


def _ksa_member(env, ksa: str) -> str:
    return f"serviceAccount:{env.project}.svc.id.goog[{env.namespace}/{ksa}]"


def _record_identity(ctx: ProvisionContext, **fields) -> None:
    current = json.loads(ctx.app.run_identity_json or "{}")
    current.update(fields)
    ctx.app.run_identity_json = json.dumps(current, sort_keys=True)


class EnsureRunIdentity:
    """The pod's Google service account, per project: `<app>-run@<project>`
    made by Gojira, or the one `identity.gsa` adopts (verified, never made).
    Design: docs/grants-and-platform-facts-design.md §1."""

    name = "ensure_run_identity"
    rechecks = True  # the contract may gain or change identity: on a live app

    def check(self, ctx: ProvisionContext) -> str | None:
        spec, ident = _run_identity(ctx)
        if ident is None:
            return "no identity declared"
        gcp = _gcp(ctx)
        emails = sorted({spec.run_gsa(e) for e in spec.environments})
        missing = [g for g in emails if not gcp.service_account_exists(g.split("@")[1].split(".")[0], g)]
        if missing and ident.gsa:
            raise IdentityRefused(
                f"identity.gsa names {ident.gsa}, which does not exist; Gojira adopts a "
                "service account but never makes the one you name"
            )
        if missing:
            return None
        self._record(ctx, spec, ident)
        return ", ".join(emails)

    def apply(self, ctx: ProvisionContext) -> str:
        spec, ident = _run_identity(ctx)
        gcp = _gcp(ctx)
        made = []
        for env in spec.environments:
            gsa = spec.run_gsa(env)
            if gsa in made or gcp.service_account_exists(env.project, gsa):
                continue
            gcp.create_service_account(env.project, gsa.split("@")[0], f"{ctx.app.name} (Gojira run identity)")
            made.append(gsa)
        self._record(ctx, spec, ident)
        return ", ".join(made)

    @staticmethod
    def _record(ctx, spec, ident) -> None:
        _record_identity(
            ctx,
            gsa_by_project={e.project: spec.run_gsa(e) for e in spec.environments},
            adopted=bool(ident.gsa),
        )


class GrantRunRoles:
    """identity.gcp_roles on each environment's project, for the pod's GSA.
    Every role must be on the install's allowlist (iam.grantable_roles) —
    a security boundary, so a role off the list fails the provision."""

    name = "grant_run_roles"
    rechecks = True

    def _plan(self, ctx: ProvisionContext):
        spec, ident = _run_identity(ctx)
        if ident is None or not ident.gcp_roles:
            return spec, ident, []
        allowed = set(ctx.config.iam.grantable_roles)
        refused = [r for r in ident.gcp_roles if r not in allowed]
        if refused:
            raise IdentityRefused(
                f"this install does not grant {', '.join(refused)}: iam.grantable_roles "
                f"(GOJIRA_GRANTABLE_ROLES) allows {', '.join(sorted(allowed)) or 'nothing'}"
            )
        grants = sorted({(e.project, spec.run_gsa(e), r) for e in spec.environments for r in ident.gcp_roles})
        return spec, ident, grants

    def check(self, ctx: ProvisionContext) -> str | None:
        spec, ident, grants = self._plan(ctx)
        if ident is None:
            return "no identity declared"
        if not grants:
            return "no roles asked for"
        gcp = _gcp(ctx)
        if all(gcp.project_has_binding(p, f"serviceAccount:{g}", r) for p, g, r in grants):
            _record_identity(ctx, roles=sorted(ident.gcp_roles))
            return ", ".join(sorted({r for _, _, r in grants}))
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        spec, ident, grants = self._plan(ctx)
        gcp = _gcp(ctx)
        for project, gsa, role in grants:
            if not gcp.project_has_binding(project, f"serviceAccount:{gsa}", role):
                gcp.add_project_binding(project, f"serviceAccount:{gsa}", role)
        _record_identity(ctx, roles=sorted(ident.gcp_roles))
        return ", ".join(sorted({r for _, _, r in grants}))


class BindRunWorkloadIdentity:
    """Each environment's KSA may impersonate the pod's GSA: the
    workloadIdentityUser binding on the GSA for `<project>.svc.id.goog[ns/ksa]`.
    CI applies the matching annotation from GOJIRA_<ENV>_RUN_SA."""

    name = "bind_run_workload_identity"
    rechecks = True
    role = "roles/iam.workloadIdentityUser"

    def _bindings(self, ctx):
        spec, ident = _run_identity(ctx)
        if ident is None:
            return spec, ident, []
        return spec, ident, [
            (ident.project or e.project, spec.run_gsa(e), _ksa_member(e, spec.ksa_name))
            for e in spec.environments
        ]

    def check(self, ctx: ProvisionContext) -> str | None:
        spec, ident, bindings = self._bindings(ctx)
        if ident is None:
            return "no identity declared"
        gcp = _gcp(ctx)
        if all(gcp.sa_has_binding(p, g, m, self.role) for p, g, m in bindings):
            _record_identity(ctx, bindings=[list(b) for b in bindings])
            return ", ".join(m for _, _, m in bindings)
        return None

    def apply(self, ctx: ProvisionContext) -> str:
        spec, ident, bindings = self._bindings(ctx)
        gcp = _gcp(ctx)
        for project, gsa, member in bindings:
            if not gcp.sa_has_binding(project, gsa, member, self.role):
                gcp.add_sa_binding(project, gsa, member, self.role)
        _record_identity(ctx, bindings=[list(b) for b in bindings])
        return ", ".join(m for _, _, m in bindings)


class BindClusterRoles:
    """`cluster_roles:` — the operator's ClusterRoles, Gojira's bindings.

    A ClusterRoleBinding `gojira-<app>-<role>` from the pod's KSA in each
    environment's namespace to a ClusterRole the operator applied and
    allowlisted (iam.grantable_cluster_roles), made with Gojira's own
    cluster identity (container.admin since finding #11). Design:
    docs/grants-and-platform-facts-design.md §2."""

    name = "bind_cluster_roles"
    rechecks = True  # cluster_roles: may grow on a live app

    def _plan(self, ctx: ProvisionContext):
        spec = _app_spec(ctx)
        roles = list(spec.cluster_roles)
        if not roles:
            return spec, []
        allowed = set(ctx.config.iam.grantable_cluster_roles)
        refused = [r for r in roles if r not in allowed]
        if refused:
            raise IdentityRefused(
                f"this install does not bind {', '.join(refused)}: iam.grantable_cluster_roles "
                f"(GOJIRA_GRANTABLE_CLUSTER_ROLES) allows {', '.join(sorted(allowed)) or 'nothing'}"
            )
        return spec, roles

    @staticmethod
    def _binding_name(app: str, role: str) -> str:
        return f"gojira-{app}-{role}"

    def check(self, ctx: ProvisionContext) -> str | None:
        spec, roles = self._plan(ctx)
        if not roles:
            return "no cluster roles asked for"
        for env in ctx.environments:
            kubectl = ctx.clients["kubectl_for"](env.cluster)
            for role in roles:
                if not kubectl.cluster_role_exists(role):
                    raise IdentityRefused(
                        f"ClusterRole {role} is not on cluster {env.cluster.name}: apply it "
                        "first (it is the operator's, Gojira only binds to it)"
                    )
                if not kubectl.cluster_role_binding_exists(self._binding_name(spec.app, role)):
                    return None
        self._record(ctx, spec, roles)
        return ", ".join(roles)

    def apply(self, ctx: ProvisionContext) -> str:
        spec, roles = self._plan(ctx)
        made = []
        for env in ctx.environments:
            kubectl = ctx.clients["kubectl_for"](env.cluster)
            for role in roles:
                name = self._binding_name(spec.app, role)
                kubectl.apply(self._manifest(name, role, env.namespace, spec.ksa_name))
                made.append(f"{env.name}:{name}")
        self._record(ctx, spec, roles)
        return ", ".join(made)

    @staticmethod
    def _manifest(name: str, role: str, namespace: str, ksa: str) -> str:
        return yaml.safe_dump({
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": name, "labels": {"app.kubernetes.io/managed-by": "gojira"}},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
            "subjects": [{"kind": "ServiceAccount", "name": ksa, "namespace": namespace}],
        }, sort_keys=False)

    def _record(self, ctx, spec, roles) -> None:
        _record_identity(
            ctx,
            cluster_bindings=sorted(
                {(env.name, self._binding_name(spec.app, r)) for env in ctx.environments for r in roles}
            ),
        )


class SetRepoVariables:
    """The install's facts on the repository, as Actions variables (issue #87).

    The generated workflows read the registry, the deploy identity and the
    cluster coordinates of every environment from `vars.GOJIRA_<ENV>_*`,
    and the runner label from `vars.CI_RUNNER` when the install names one.
    Setting them here, before the setup PR is opened, is what lets the
    workflow in the repo be identical under any Gojira: nothing of this
    install is written into the repo, and a second install that connects
    the same repo sets its own values instead of rewriting files.

    Repository variables, not organization ones: on GitHub's Free plan an
    org variable is listed for a private repo and empty at run time, which
    cost a whole afternoon on the dogfood (2026-09-30).
    """

    name = "set_repo_variables"
    rechecks = True  # GOJIRA_<ENV>_RUN_SA appears when the contract gains an identity

    def _wanted(self, ctx: ProvisionContext) -> dict[str, str]:
        want: dict[str, str] = {}
        spec = _app_spec(ctx)
        for env in spec.environments:
            want.update(env.platform_variables(spec.run_gsa(env)))
        if ctx.config.ci.runner:
            want["CI_RUNNER"] = ctx.config.ci.runner
        want.update(ctx.config.ci.repo_variables)
        return want

    def _stale(self, ctx: ProvisionContext, want: dict[str, str]) -> list[str]:
        repo = ctx.app.repo_full_name
        return [
            k for k, v in want.items()
            if self._github_call(ctx, lambda gh, k=k: gh.repo_variable(repo, k)) != v
        ]

    def check(self, ctx: ProvisionContext) -> str | None:
        want = self._wanted(ctx)
        return None if self._stale(ctx, want) else f"{len(want)} variables current"

    def apply(self, ctx: ProvisionContext) -> str:
        want = self._wanted(ctx)
        stale = self._stale(ctx, want)
        repo = ctx.app.repo_full_name
        for k in stale:
            self._github_call(ctx, lambda gh, k=k: gh.set_repo_variable(repo, k, want[k]))
        return "set " + ", ".join(stale)

    @staticmethod
    def _github_call(ctx: ProvisionContext, call):
        # GitHub files Actions variables under their own App permission,
        # "Variables", not under "Actions". An App set up for everything else
        # answers 403 here, and GitHub's sentence does not say which
        # permission is missing (the dogfood's first run, 2026-10-01).
        try:
            return call(_github(ctx))
        except Exception as exc:
            if "403" in str(exc):
                raise RuntimeError(
                    "the GitHub App cannot manage Actions variables, which the generated "
                    "workflows now depend on: give it the repository permission "
                    "Variables: Read & write and accept the change on the installation "
                    "(docs/github-app-setup.md)"
                ) from exc
            raise


def per_app_steps() -> list[Step]:
    """The repo flow: clone, render artifacts, the runner variable, open the setup PR, optionally merge it."""
    return [
        PrepareWorkdir(), GenerateArtifacts(),
        EnsureRunIdentity(), GrantRunRoles(), BindRunWorkloadIdentity(), BindClusterRoles(),
        SetRepoVariables(), OpenSetupPr(), MergeSetupPr(),
    ]
