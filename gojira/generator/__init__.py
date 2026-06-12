"""Artifact generator: renders workflows, manifests, and gojira.yaml.

Pure function: GeneratorSpec in, {path: content} out. No I/O beyond
reading templates. The service layer assembles the spec (from app,
cluster, and provisioned resource refs) and commits the output via PR.

Opinion: every Gojira app listens on port 8080 and serves /health and
/metrics. This uniformity is what makes generated probes and scrape
configs work without per-app configuration.
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


class GeneratorSpec(BaseModel):
    app: str
    repo_full_name: str
    namespace: str
    workload_class: str
    cluster_name: str
    gcp_project: str
    location: str            # GKE cluster location
    gke_cluster: str
    ar_repo: str             # e.g. us-central1-docker.pkg.dev/<project>/apps
    wif_provider: str        # full resource name of the WIF provider
    gsa_email: str
    environments: list[str] = ["dev", "qa", "prod"]
    default_branch: str = "main"
    schedule: str | None = None  # cron class only

    @model_validator(mode="after")
    def validate_class(self):
        if self.workload_class not in WORKLOAD_CLASSES:
            raise ValueError(
                f"unknown workload class {self.workload_class!r}, "
                f"expected one of {WORKLOAD_CLASSES}"
            )
        if self.workload_class == "cron" and not self.schedule:
            raise ValueError("cron workload class requires a schedule")
        return self

    @property
    def image(self) -> str:
        return f"{self.ar_repo}/{self.app}"

    @property
    def ar_host(self) -> str:
        return self.ar_repo.split("/")[0]


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

    for env_name in spec.environments:
        files[f"k8s/overlays/{env_name}/kustomization.yaml"] = _t(
            "k8s/kustomization-overlay.yaml.j2", {**ctx, "env": env_name}
        )

    log.debug("rendered %d artifacts for %s (%s)", len(files), spec.app, spec.workload_class)
    return files


def _has_service(cls: WorkloadClass) -> bool:
    """Services exist to route traffic in; only HTTP-serving classes get one."""
    readiness = cls.probes.get("readiness")
    return readiness is not None and readiness.type == "http"


def _t(name: str, ctx: dict) -> str:
    return _env.get_template(name).render(**ctx)
