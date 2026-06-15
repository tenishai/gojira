"""Artifact generator: renders workflows, manifests, and gojira.yaml.

Pure function: GeneratorSpec in, {path: content} out. No I/O beyond reading
templates. The service layer assembles the spec (from app, environments, and
provisioned resource refs) and commits the output via PR.

A spec carries a list of environments. The first is the CI target (build +
deploy on push). Later envs are promote targets: each builds from scratch
except the last, which crane-copies the image its predecessor built and tested.
Each env carries its own project-scoped identity, so envs may span projects.

Opinion: every Gojira app listens on port 8080 and serves /health and
/metrics. This uniformity is what makes generated probes and scrape configs
work without per-app configuration.
"""

import logging

from jinja2 import Environment, PackageLoader, StrictUndefined
from pydantic import BaseModel, model_validator

from gojira.opinions import WORKLOAD_CLASSES, WorkloadClass, load_sizing

log = logging.getLogger(__name__)

APP_PORT = 8080

_env = Environment(
    loader=PackageLoader("gojira.generator", "templates"),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


class EnvSpec(BaseModel):
    """One environment's deploy target + project-scoped identity."""

    name: str                # dev | qa | prod
    namespace: str
    project: str
    cluster_name: str
    location: str            # GKE cluster location
    gke_cluster: str
    ar_repo: str             # e.g. us-central1-docker.pkg.dev/<project>/apps
    wif_provider: str        # full resource name of the WIF provider
    gsa_email: str
    copy_from: str | None = None  # env name to crane-copy from; None = build
    gated: bool = False           # behind the "production" environment gate

    @property
    def ar_host(self) -> str:
        return self.ar_repo.split("/")[0]


class GeneratorSpec(BaseModel):
    app: str
    repo_full_name: str
    workload_class: str
    environments: list[EnvSpec]
    default_branch: str = "main"
    schedule: str | None = None  # cron class only

    @model_validator(mode="after")
    def validate_spec(self):
        if self.workload_class not in WORKLOAD_CLASSES:
            raise ValueError(
                f"unknown workload class {self.workload_class!r}, "
                f"expected one of {WORKLOAD_CLASSES}"
            )
        if self.workload_class == "cron" and not self.schedule:
            raise ValueError("cron workload class requires a schedule")
        if not self.environments:
            raise ValueError("spec needs at least one environment")
        return self

    @property
    def ci_env(self) -> EnvSpec:
        """The environment CI builds and deploys on push."""
        return self.environments[0]

    @property
    def promote_envs(self) -> list[EnvSpec]:
        """The downstream environments the promote workflow walks, in order."""
        return self.environments[1:]

    def image(self, env: EnvSpec) -> str:
        return f"{env.ar_repo}/{self.app}"


def render(spec: GeneratorSpec) -> dict[str, str]:
    """Render all artifacts for an app. Returns {repo-relative path: content}."""
    sizing = load_sizing()
    cls = sizing.classes[spec.workload_class]
    ctx = {
        "spec": spec,
        "cls": cls,
        "ephemeral": sizing.ephemeral_storage,
        "port": APP_PORT,
        "is_cron": spec.workload_class == "cron",
        "has_service": _has_service(cls),
    }

    files: dict[str, str] = {}
    files[".github/workflows/gojira-ci.yaml"] = _t("workflows/ci.yaml.j2", ctx)
    if spec.promote_envs:
        files[".github/workflows/gojira-promote.yaml"] = _t("workflows/promote.yaml.j2", ctx)
    files["gojira.yaml"] = _t("gojira.yaml.j2", ctx)

    base = "k8s/base"
    if spec.workload_class == "cron":
        files[f"{base}/cronjob.yaml"] = _t("k8s/cronjob.yaml.j2", ctx)
    else:
        files[f"{base}/deployment.yaml"] = _t("k8s/deployment.yaml.j2", ctx)
        files[f"{base}/podmonitoring.yaml"] = _t("k8s/podmonitoring.yaml.j2", ctx)
        files[f"{base}/rules.yaml"] = _t("k8s/rules.yaml.j2", ctx)
    if ctx["has_service"]:
        files[f"{base}/service.yaml"] = _t("k8s/service.yaml.j2", ctx)
    if cls.hpa:
        files[f"{base}/hpa.yaml"] = _t("k8s/hpa.yaml.j2", ctx)
        files[f"{base}/pdb.yaml"] = _t("k8s/pdb.yaml.j2", ctx)
    files[f"{base}/kustomization.yaml"] = _t(
        "k8s/kustomization-base.yaml.j2",
        {**ctx, "resources": sorted(
            p.removeprefix(f"{base}/") for p in files if p.startswith(base)
        )},
    )

    for env in spec.environments:
        files[f"k8s/overlays/{env.name}/kustomization.yaml"] = _t(
            "k8s/kustomization-overlay.yaml.j2", {**ctx, "env": env}
        )

    log.debug("rendered %d artifacts for %s (%s)", len(files), spec.app, spec.workload_class)
    return files


def _has_service(cls: WorkloadClass) -> bool:
    """Services exist to route traffic in; only HTTP-serving classes get one."""
    readiness = cls.probes.get("readiness")
    return readiness is not None and readiness.type == "http"


def _t(name: str, ctx: dict) -> str:
    return _env.get_template(name).render(**ctx)
