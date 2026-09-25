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

import hashlib
import json
import logging
import re

import yaml
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


class Overrides(BaseModel):
    """gojira.yaml's justified escape hatch, as the generator consumes it.

    Only cpu/memory land here — the sizing loop doesn't touch gpu or
    ephemeral-storage. Unknown keys in the file are ignored, not errors.
    """

    requests: dict[str, str] = {}
    limits: dict[str, str] = {}
    justification: str = ""


def read_overrides(gojira_yaml_text: str) -> Overrides | None:
    """The overrides block from a repo's gojira.yaml, or None if absent/unusable."""
    try:
        doc = yaml.safe_load(gojira_yaml_text) or {}
    except yaml.YAMLError:
        return None
    raw = doc.get("overrides")
    if not isinstance(raw, dict):
        return None
    keep = {"cpu", "memory"}
    return Overrides(
        requests={k: str(v) for k, v in (raw.get("requests") or {}).items() if k in keep},
        limits={k: str(v) for k, v in (raw.get("limits") or {}).items() if k in keep},
        justification=str(raw.get("justification") or ""),
    )


class DataMount(BaseModel):
    """A persistent volume the app needs mounted (approval DBs, caches)."""

    pvc: str   # claim name — the PVC object itself stays app-authored
    path: str  # mount path inside the container
    # fsGroup for the pod: kubelet chowns the volume to :fs_group and adds it
    # to the containers' supplemental groups, so any non-root image uid can
    # write regardless of its value (finding #15 — the sandbox's fsGroup
    # lesson, relearned by the first fleet app with state).
    fs_group: int = 999


def read_app_needs(gojira_yaml_text: str) -> dict:
    """The app-needs keys from a repo's gojira.yaml, as GeneratorSpec updates.

    Finding #12: real apps arrive with secrets delivery, identity, and state.
    These keys are the contract for the parts that must render inside Gojira's
    deployment; the objects they reference (ExternalSecret, ServiceAccount,
    PVC) stay app-authored and are preserved by preserve_resources().
    """
    try:
        doc = yaml.safe_load(gojira_yaml_text) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(doc, dict):
        return {}
    needs: dict = {}
    if isinstance(doc.get("secrets_from"), list):
        needs["secrets_from"] = [str(s) for s in doc["secrets_from"] if s]
    if isinstance(doc.get("config_from"), list):
        needs["config_from"] = [str(s) for s in doc["config_from"] if s]
    if doc.get("service_account"):
        needs["service_account"] = str(doc["service_account"])
    if isinstance(doc.get("env"), dict):
        needs["env_vars"] = {str(k): str(v) for k, v in doc["env"].items()}
    raw_admin = doc.get("admin_port")
    # bool is an int in Python, so `admin_port: true` would render
    # `containerPort: True` and break every apply until hand-fixed; and an
    # admin port equal to the app port would render a duplicate containerPort
    # whose {app}-admin Service routes to the data-plane listener - the
    # shared-listener accident this field exists to prevent. Both get the
    # absent/unusable -> ignore treatment (review findings on #43).
    if (
        isinstance(raw_admin, int)
        and not isinstance(raw_admin, bool)
        and 0 < raw_admin < 65536
        and raw_admin != APP_PORT
    ):
        needs["admin_port"] = raw_admin
    mount = doc.get("data_mount")
    if isinstance(mount, dict) and mount.get("pvc") and mount.get("path"):
        needs["data_mount"] = DataMount(
            pvc=str(mount["pvc"]), path=str(mount["path"]),
            **({"fs_group": int(mount["fs_group"])} if str(mount.get("fs_group", "")).isdigit() else {}),
        )
    return needs


def read_app_class(gojira_yaml_text: str) -> str | None:
    """The class: key from a repo's gojira.yaml, or None if absent/unusable.

    Finding #16: the contract file promises "change the class here" — but the
    generator took class from the DB (set at connect time) and regeneration
    actively REVERTED a class change merged to main. The contract wins; the
    DB converges (the finding-#10 lesson, applied to class)."""
    try:
        doc = yaml.safe_load(gojira_yaml_text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    cls = doc.get("class")
    return str(cls) if cls else None


def infer_app_needs(deployment_text: str) -> dict:
    """Adopt-mode: read an existing deployment's declared needs.

    A brownfield repo without a gojira.yaml still SAYS what it needs — in the
    deployment it already runs: serviceAccountName, envFrom secretRefs, plain
    env vars, a PVC mount, an fsGroup. Zero-touch onboarding reads them and
    seeds the contract the setup PR proposes, instead of shipping a deployment
    that silently dropped them (finding #12's completion — the platform learns
    the app by reading it, and the operator reviews what it learned).

    First container only (the drift detector's convention). Env vars with
    valueFrom are skipped — secret- and field-backed values belong to their
    own mechanisms. Returns {} for anything unparseable: inference must never
    make onboarding worse than no inference.
    """
    try:
        doc = yaml.safe_load(deployment_text) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
        return {}
    pod = (doc.get("spec") or {}).get("template", {}).get("spec") or {}
    containers = pod.get("containers") or []
    if not containers:
        return {}
    c0 = containers[0]

    needs: dict = {}
    if pod.get("serviceAccountName"):
        needs["service_account"] = str(pod["serviceAccountName"])
    secrets = [
        e["secretRef"]["name"]
        for e in c0.get("envFrom") or []
        if isinstance(e, dict) and e.get("secretRef", {}).get("name")
    ]
    if secrets:
        needs["secrets_from"] = secrets
    configs = [
        e["configMapRef"]["name"]
        for e in c0.get("envFrom") or []
        if isinstance(e, dict) and e.get("configMapRef", {}).get("name")
    ]
    if configs:
        needs["config_from"] = configs  # finding #18: config is a need too
    env = {
        str(e["name"]): str(e["value"])
        for e in c0.get("env") or []
        if isinstance(e, dict) and e.get("name") and "value" in e
    }
    if env:
        needs["env_vars"] = env

    pvc_by_volume = {
        v["name"]: v["persistentVolumeClaim"]["claimName"]
        for v in pod.get("volumes") or []
        if isinstance(v, dict) and v.get("persistentVolumeClaim", {}).get("claimName")
    }
    for m in c0.get("volumeMounts") or []:
        if isinstance(m, dict) and m.get("name") in pvc_by_volume and m.get("mountPath"):
            fs_group = (pod.get("securityContext") or {}).get("fsGroup")
            needs["data_mount"] = DataMount(
                pvc=pvc_by_volume[m["name"]], path=str(m["mountPath"]),
                **({"fs_group": int(fs_group)} if fs_group else {}),
            )
            break
    return needs


class GeneratorSpec(BaseModel):
    app: str
    repo_full_name: str
    workload_class: str
    environments: list[EnvSpec]
    default_branch: str = "main"
    schedule: str | None = None  # cron class only
    overrides: Overrides | None = None  # from the repo's gojira.yaml, if any
    # App needs (finding #12) — sourced from the repo's gojira.yaml, like
    # overrides. Rendered into the deployment; referenced objects stay
    # app-authored.
    secrets_from: list[str] = []
    config_from: list[str] = []  # envFrom ConfigMaps — non-secret config
    service_account: str = ""
    data_mount: DataMount | None = None
    # Plain, non-secret configuration (tenant identity, endpoints). Rendered
    # inline into the deployment: config changes stay reviewed git commits,
    # not secret-store writes (a tenant's finding).
    env_vars: dict[str, str] = {}
    # A second listener beside the app port (gateway-shaped apps: liminal's
    # separable admin plane). Renders a named containerPort plus its own
    # ClusterIP Service ({app}-admin) so reachability is a NetworkPolicy /
    # caller decision, never a shared-listener accident. The app port stays
    # the only port the main Service routes.
    admin_port: int | None = None

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


def apply_resource_overrides(
    deployment_text: str, requests: dict[str, str], limits: dict[str, str]
) -> str:
    """Rewrite cpu/memory values in a generated deployment's resources block.

    Used by the sizing PR so the contract (gojira.yaml) and the manifest move
    in ONE reviewed PR — merging it actually resizes the workload. Textual, so
    the file's comments survive; we generated the file, so its shape is known.
    Validated by parsing afterwards.
    """
    lines = deployment_text.splitlines(keepends=True)
    section = None  # None | "requests" | "limits"
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped == "requests:":
            section = "requests"
        elif stripped == "limits:":
            section = "limits"
        elif section and stripped and not line.startswith(" " * 14):
            # left the resources sub-block (keys live at 14-space indent)
            section = None
        if section:
            wanted = requests if section == "requests" else limits
            for key, value in wanted.items():
                if stripped.startswith(f"{key}:"):
                    indent = line[: len(line) - len(line.lstrip())]
                    line = f"{indent}{key}: {value}\n"
                    break
        out.append(line)
    updated = "".join(out)

    parsed = yaml.safe_load(updated)  # belt and braces
    got = parsed["spec"]["template"]["spec"]["containers"][0]["resources"]
    for key, value in requests.items():
        if str(got["requests"].get(key)) != str(value):
            raise ValueError(f"override failed to apply: requests.{key}")
    for key, value in limits.items():
        if str(got["limits"].get(key)) != str(value):
            raise ValueError(f"override failed to apply: limits.{key}")
    return updated


class _BlockDumper(yaml.SafeDumper):
    """Multi-line strings as `|` blocks, so a preserved inline patch reads in
    the overlay exactly the way its author wrote it."""


def _str_presenter(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_BlockDumper.add_representer(str, _str_presenter)


def preserve_patches(
    existing_text: str, rendered_text: str, keep_path=None, keep_target=None
) -> str:
    """Carry an existing kustomization's `patches:` into a fresh render.

    Regenerating an overlay must not discard operator-owned state: drift
    capture (and operators by hand) wire strategic-merge patches into the
    overlay kustomization, and a blind overwrite silently reverts them on
    re-onboard — the exact thing the design forbids (design.md:26).

    kustomize accepts two shapes of entry and both are carried whole: a file
    patch (`path:`) and an inline one (`target:` + `patch:`). The first
    brownfield repo this ran on wrote its ConfigMap wiring inline, and the
    old `p['path']` here raised KeyError before the setup PR existed
    (2026-09-25). An entry that is neither is not a patch kustomize would
    build either, and is dropped with a log line rather than crashing.

    `keep_path(path) -> bool` and `keep_target(kind, name) -> bool` let the
    caller drop patches whose target no longer exists in the render (an app
    rename orphans them, and kustomize hard-fails the whole build on an
    orphaned patch — found live). An inline patch whose target names no
    kind and name (label selectors only) is kept. Default: keep everything.

    The block is appended textually so the render's comments survive. Only
    `patches` is preserved: every other field is Gojira's to regenerate.
    """
    try:
        existing = yaml.safe_load(existing_text) or {}
    except yaml.YAMLError:
        return rendered_text  # unparseable old file — regenerate clean
    patches = existing.get("patches") or []
    if not patches:
        return rendered_text

    rendered = yaml.safe_load(rendered_text) or {}
    rendered_patches = [p for p in rendered.get("patches", []) if isinstance(p, dict)]
    have_paths = {str(p["path"]) for p in rendered_patches if p.get("path") is not None}
    have_inline = [(p.get("target"), p.get("patch")) for p in rendered_patches if "patch" in p]

    keep: list[dict] = []
    seen_paths: set[str] = set()
    seen_inline: list[tuple] = []
    for p in patches:
        if not isinstance(p, dict):
            continue
        if p.get("path") is not None:
            path = str(p["path"])
            if path in have_paths or path in seen_paths:
                continue
            if keep_path is not None and not keep_path(path):
                continue
            seen_paths.add(path)
            keep.append(p)
        elif "patch" in p:
            key = (p.get("target"), p.get("patch"))
            if key in have_inline or key in seen_inline:
                continue
            target = p.get("target") if isinstance(p.get("target"), dict) else {}
            kind, name = target.get("kind"), target.get("name")
            if keep_target is not None and kind and name and not keep_target(str(kind), str(name)):
                continue
            seen_inline.append(key)
            keep.append(p)
        else:
            log.warning("dropping kustomization patch entry with neither path nor patch: %r", p)
    if not keep:
        return rendered_text

    block = yaml.dump(
        {"patches": keep}, Dumper=_BlockDumper, sort_keys=False, default_flow_style=False
    )
    sep = "" if rendered_text.endswith("\n") else "\n"
    return f"{rendered_text}{sep}{block}"


def preserve_resources(existing_text: str, rendered_text: str, file_exists) -> str:
    """Carry an existing kustomization's app-authored `resources:` entries into
    a fresh render.

    Finding #12: the generator does not own the whole manifest tree. Apps
    arrive with objects Gojira doesn't model — ExternalSecrets, ServiceAccounts,
    PVCs — and regeneration must not orphan them (the files survive on disk but
    silently leave the build, which is how a Slack agent lost its tokens).

    `file_exists(entry) -> bool` drops entries whose file is gone (same
    rename-safety stance as preserve_patches: a stale entry hard-fails the
    whole kustomize build). Entries the render already produces are not
    duplicated. Appended textually; the render's comments survive.
    """
    try:
        existing = yaml.safe_load(existing_text) or {}
    except yaml.YAMLError:
        return rendered_text  # unparseable old file — regenerate clean
    if not isinstance(existing, dict):
        return rendered_text
    entries = [r for r in existing.get("resources") or [] if isinstance(r, str)]
    if not entries:
        return rendered_text

    rendered = yaml.safe_load(rendered_text) or {}
    have = set(rendered.get("resources") or [])
    keep = [e for e in entries if e not in have and file_exists(e)]
    if not keep:
        return rendered_text

    sep = "" if rendered_text.endswith("\n") else "\n"
    lines = ["  # app-authored resources, preserved by Gojira:"]
    lines += [f"  - {e}" for e in keep]
    return f"{rendered_text}{sep}" + "\n".join(lines) + "\n"


GENERATOR_KEYS = ("configMapGenerator", "secretGenerator", "generatorOptions")


def preserve_generators(existing_text: str, rendered_text: str) -> str:
    """Carry an existing kustomization's generator blocks into a fresh render.

    Finding #18: a tenant app's non-secret config was born from an overlay
    configMapGenerator — regeneration dropped it and the app lost its env.
    configMapGenerator / secretGenerator / generatorOptions survive verbatim
    (re-serialized; comments inside the blocks are not preserved). Rendered
    kustomizations never carry generators of their own, so there is nothing
    to merge — only to keep.
    """
    try:
        existing = yaml.safe_load(existing_text) or {}
    except yaml.YAMLError:
        return rendered_text
    if not isinstance(existing, dict):
        return rendered_text
    keep = {k: existing[k] for k in GENERATOR_KEYS if k in existing}
    if not keep:
        return rendered_text
    rendered = yaml.safe_load(rendered_text) or {}
    keep = {k: v for k, v in keep.items() if k not in rendered}
    if not keep:
        return rendered_text
    sep = "" if rendered_text.endswith("\n") else "\n"
    block = yaml.safe_dump(keep, sort_keys=False, default_flow_style=False)
    return f"{rendered_text}{sep}# app-authored generators, preserved by Gojira:\n{block}"


def contract_fingerprint(gojira_yaml_text: str) -> str:
    """sha256 of gojira.yaml's MEANING: parsed, then dumped as sorted JSON.

    Reformatting, reordering keys, or editing comments leaves it unchanged;
    any change the generator would act on changes it. CI recomputes this
    with the same one-liner (see workflows/_deploy.j2) and refuses to
    deploy when it differs from the value stamped into the workflow at
    render time — finding #20: a gojira.yaml push and its setup PR land as
    two commits, and the first one deploys a new image against stale
    manifests.
    """
    doc = yaml.safe_load(gojira_yaml_text)
    canon = json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


_CONTRACT_LINE = re.compile(r"^(\s*GOJIRA_CONTRACT:\s*)[0-9a-f]{64}$", re.MULTILINE)


def stamp_contract(workflow_text: str, gojira_yaml_text: str) -> str:
    """Rewrite a generated workflow's GOJIRA_CONTRACT to match gojira.yaml.

    For paths that edit the contract textually instead of re-rendering
    (the sizing PR) — otherwise their merge would fail every deploy with a
    stale-manifests error that is a lie.
    """
    sha = contract_fingerprint(gojira_yaml_text)
    updated, n = _CONTRACT_LINE.subn(lambda m: f"{m.group(1)}{sha}", workflow_text)
    if n == 0:
        raise ValueError("workflow carries no GOJIRA_CONTRACT line — regenerate it")
    return updated


def render(spec: GeneratorSpec) -> dict[str, str]:
    """Render all artifacts for an app. Returns {repo-relative path: content}."""
    sizing = load_sizing()
    cls = sizing.classes[spec.workload_class]
    if spec.overrides is not None:
        # The justified escape hatch wins over the class opinion — and doing it
        # here keeps regeneration idempotent: a re-onboard re-reads gojira.yaml
        # and re-applies the same overrides instead of reverting to class
        # defaults (the finding-8 lesson, applied to sizing).
        cls = cls.model_copy(deep=True)
        for key, value in spec.overrides.requests.items():
            setattr(cls.requests, key, value)
        for key, value in spec.overrides.limits.items():
            setattr(cls.limits, key, value)
    ctx = {
        "spec": spec,
        "cls": cls,
        "ephemeral": sizing.ephemeral_storage,
        "port": APP_PORT,
        "is_cron": spec.workload_class == "cron",
        "has_service": _has_service(cls),
    }

    files: dict[str, str] = {}
    files["gojira.yaml"] = _t("gojira.yaml.j2", ctx)
    # The workflows carry the fingerprint of the contract they were rendered
    # from; CI refuses to deploy a repo whose gojira.yaml no longer matches.
    ctx["contract_sha"] = contract_fingerprint(files["gojira.yaml"])
    files[".github/workflows/gojira-ci.yaml"] = _t("workflows/ci.yaml.j2", ctx)
    if spec.promote_envs:
        files[".github/workflows/gojira-promote.yaml"] = _t("workflows/promote.yaml.j2", ctx)

    base = "k8s/base"
    if spec.workload_class == "cron":
        files[f"{base}/cronjob.yaml"] = _t("k8s/cronjob.yaml.j2", ctx)
    else:
        files[f"{base}/deployment.yaml"] = _t("k8s/deployment.yaml.j2", ctx)
        files[f"{base}/podmonitoring.yaml"] = _t("k8s/podmonitoring.yaml.j2", ctx)
        files[f"{base}/rules.yaml"] = _t("k8s/rules.yaml.j2", ctx)
    if ctx["has_service"]:
        files[f"{base}/service.yaml"] = _t("k8s/service.yaml.j2", ctx)
    if spec.admin_port and not ctx["is_cron"]:
        files[f"{base}/admin-service.yaml"] = _t("k8s/admin-service.yaml.j2", ctx)
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
