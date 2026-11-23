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
import shlex

import yaml
from jinja2 import Environment, PackageLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from gojira.opinions import WORKLOAD_CLASSES, WorkloadClass, load_sizing

log = logging.getLogger(__name__)

APP_PORT = 8080
ARCHES = ("amd64", "arm64")
# The kustomize the generated workflow fetches onto a runner that has none
# (GitHub's hosted runners carry their own). Version and the sha256 of each
# linux tarball from the release's checksums.txt; bump both together.
KUSTOMIZE = {
    "version": "v5.8.3",
    "sha256": {
        "amd64": "cb9e31198d3f63b44848bd0afc4c8efc56e6cfef9c93a4a39a211940e6decd61",
        "arm64": "9867b76482cfc6d25b546abc1d2b5a32bd137505aba640649b4b378f64f6d68f",
    },
}

# Kinds the app's deploy identity (roles/container.developer) cannot create
# and must not: a base holding one fails every deploy after twenty retries
# (kobo, 2026-10-02, issue #86). Cluster access is asked for as
# `cluster_roles:` and bound by Gojira instead.
CLUSTER_SCOPED_KINDS = frozenset({
    "ClusterRole", "ClusterRoleBinding", "Namespace", "CustomResourceDefinition",
    "PriorityClass", "StorageClass", "MutatingWebhookConfiguration",
    "ValidatingWebhookConfiguration", "PersistentVolume", "IngressClass",
})

_env = Environment(
    loader=PackageLoader("gojira.generator", "templates"),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _quoted(value) -> str:
    """A YAML double-quoted scalar. Jinja's tojson is for HTML: it writes
    '>' as \\u003e, which YAML reads correctly and a person reads as
    damage — a sizing justification's "250m -> 50m" came out as
    "250m -\\u003e 50m" on a setup PR (gojira-demo, 2026-09-30)."""
    return json.dumps(value, ensure_ascii=False)


_env.filters["quoted"] = _quoted
_env.filters["shell"] = shlex.quote  # a value inside a `run:` step, as one word

# A contract value of exactly `${NAME}` is a reference to a repository
# variable the install sets (issue #102), not a literal: the contract says
# THAT the build needs a value, the install says WHICH.
VARIABLE_REF = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


def variable_ref(value: str) -> str | None:
    m = VARIABLE_REF.match(value or "")
    return m.group(1) if m else None


_env.filters["variable_ref"] = variable_ref


class EnvSpec(BaseModel):
    """One environment's deploy target + project-scoped identity."""

    name: str                # dev | qa | prod
    namespace: str
    project: str
    cluster_name: str
    location: str            # GKE cluster location
    gke_cluster: str
    ar_repo: str             # e.g. us-central1-docker.pkg.dev/<project>/apps
    # How the generated CI reaches the control plane: "dns" (GKE's IAM-
    # authenticated DNS endpoint, from anywhere) or "internal" (the private
    # endpoint IP, from a runner inside the cluster's network). The cluster's
    # registration says which (models.Cluster.access).
    access: str = "dns"
    wif_provider: str        # full resource name of the WIF provider
    gsa_email: str
    copy_from: str | None = None  # env name to crane-copy from; None = build
    gated: bool = False           # behind the "production" environment gate

    @property
    def ar_host(self) -> str:
        return self.ar_repo.split("/")[0]

    # The install's facts about this environment — registry, deploy identity,
    # cluster coordinates — reach the generated workflows as repository
    # Actions variables that Gojira sets (SetRepoVariables), never as
    # literals in the repo (issue #87): a repo then has no diff between
    # installs, and nobody's project id lives in somebody else's repo.
    # Repo-level, not org-level: on GitHub Free an org variable is listed for
    # a private repo and empty at run time (2026-09-30).
    @property
    def var_prefix(self) -> str:
        return "GOJIRA_" + re.sub(r"[^A-Z0-9]", "_", self.name.upper())

    def platform_variables(self, run_sa: str = "") -> dict[str, str]:
        p = self.var_prefix
        out = {
            f"{p}_AR_REPO": self.ar_repo,
            f"{p}_WIF_PROVIDER": self.wif_provider,
            f"{p}_DEPLOY_SA": self.gsa_email,
            f"{p}_PROJECT": self.project,
            f"{p}_LOCATION": self.location,
            f"{p}_GKE_CLUSTER": self.gke_cluster,
        }
        if run_sa:
            # The pod's GSA (identity:), applied to the KSA by CI's deploy
            # step: an email names a project, so it travels as a variable.
            out[f"{p}_RUN_SA"] = run_sa
        return out


class HpaOverride(BaseModel):
    """overrides.hpa: the app's own scaling bounds and targets, any subset.

    Honored only where the class opinion has an HPA (web, heavy); a class
    that must not scale (agent: one Socket Mode consumer) ignores it. A Go
    proxy that runs 30-500 replicas on CPU and memory is a web app, not a
    new class — the bounds are the app's, the shape is the platform's.
    """

    min: int | None = None
    max: int | None = None
    cpu: int | None = None     # target CPU utilization, percent
    memory: int | None = None  # target memory utilization, percent

    @model_validator(mode="after")
    def validate_bounds(self):
        for name in ("min", "max"):
            v = getattr(self, name)
            if v is not None and v < 1:
                raise ValueError(f"hpa.{name} must be >= 1")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("hpa.min must be <= hpa.max")
        for name in ("cpu", "memory"):
            v = getattr(self, name)
            if v is not None and not 1 <= v <= 100:
                raise ValueError(f"hpa.{name} must be a percent, 1-100")
        return self

    def apply(self, hpa):
        """The class HPA with these bounds laid over it."""
        return hpa.model_copy(update={
            k: v for k, v in {
                "min_replicas": self.min, "max_replicas": self.max,
                "target_cpu_percent": self.cpu, "target_memory_percent": self.memory,
            }.items() if v is not None
        })


class Overrides(BaseModel):
    """gojira.yaml's justified escape hatch, as the generator consumes it.

    Only cpu/memory land in requests/limits — the sizing loop doesn't touch
    gpu or ephemeral-storage. Unknown keys in the file are ignored, not
    errors.
    """

    requests: dict[str, str] = {}
    limits: dict[str, str] = {}
    hpa: HpaOverride | None = None
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
    hpa = None
    raw_hpa = raw.get("hpa")
    if isinstance(raw_hpa, dict):
        ints = {
            k: raw_hpa[k] for k in ("min", "max", "cpu", "memory")
            if isinstance(raw_hpa.get(k), int) and not isinstance(raw_hpa.get(k), bool)
        }
        try:
            hpa = HpaOverride(**ints) if ints else None
        except ValueError:
            hpa = None  # unusable bounds are ignored, like every other junk key
    return Overrides(
        requests={k: str(v) for k, v in (raw.get("requests") or {}).items() if k in keep},
        limits={k: str(v) for k, v in (raw.get("limits") or {}).items() if k in keep},
        hpa=hpa,
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
    if isinstance(doc.get("secrets_from_optional"), list):
        needs["secrets_from_optional"] = [str(s) for s in doc["secrets_from_optional"] if s]
    if isinstance(doc.get("bundle_image"), str) and doc["bundle_image"].strip():
        needs["bundle_image"] = doc["bundle_image"].strip()
    if isinstance(doc.get("config_from"), list):
        needs["config_from"] = [str(s) for s in doc["config_from"] if s]
    if doc.get("service_account"):
        needs["service_account"] = str(doc["service_account"])
    if isinstance(doc.get("env"), dict):
        needs["env_vars"] = {str(k): str(v) for k, v in doc["env"].items()}
    # The app's own listener and health path (2026-09-28: port 8080 and the
    # class's probe path had been constants, so any app that listened
    # elsewhere or answered /alive had to change itself to be hosted —
    # the opposite of the design goal). Class opinions stay the defaults.
    if _is_port(doc.get("port")) and doc["port"] != APP_PORT:
        needs["port"] = doc["port"]
    health = doc.get("health")
    if isinstance(health, str) and health.startswith("/"):
        needs["health"] = health
    raw_admin = doc.get("admin_port")
    # bool is an int in Python, so `admin_port: true` would render
    # `containerPort: True` and break every apply until hand-fixed; and an
    # admin port equal to the app port would render a duplicate containerPort
    # whose {app}-admin Service routes to the data-plane listener - the
    # shared-listener accident this field exists to prevent. Both get the
    # absent/unusable -> ignore treatment (review findings on #43).
    if _is_port(raw_admin) and raw_admin != needs.get("port", APP_PORT):
        needs["admin_port"] = raw_admin
    mount = doc.get("data_mount")
    if isinstance(mount, dict) and mount.get("pvc") and mount.get("path"):
        needs["data_mount"] = DataMount(
            pvc=str(mount["pvc"]), path=str(mount["path"]),
            **({"fs_group": int(mount["fs_group"])} if str(mount.get("fs_group", "")).isdigit() else {}),
        )
    if cms := _read_mounts(doc.get("config_mounts"), ConfigMount):
        needs["config_mounts"] = cms
    if secrets := _read_mounts(doc.get("secret_mounts"), SecretMount):
        needs["secret_mounts"] = secrets
    if isinstance(doc.get("identity"), dict):
        needs["identity"] = Identity.model_validate(doc["identity"])
    if isinstance(doc.get("cluster_roles"), list):
        needs["cluster_roles"] = [str(r) for r in doc["cluster_roles"] if r]
    if isinstance(doc.get("namespace_labels"), dict):
        # A YAML `true` is a bool; a label value is a string, and the gate that
        # reads it (a NetworkPolicy, a caller allowlist) compares "true".
        needs["namespace_labels"] = {
            str(k): (str(v).lower() if isinstance(v, bool) else str(v))
            for k, v in doc["namespace_labels"].items() if k
        }
    if isinstance(doc.get("build_args"), dict):
        needs["build_args"] = {str(k): str(v) for k, v in doc["build_args"].items() if k}
    if isinstance(doc.get("image"), str) and doc["image"].strip():
        needs["published_image"] = doc["image"].strip()
    if doc.get("arch") in ARCHES:
        needs["arch"] = doc["arch"]  # said, so kept — even amd64 on an arm64 platform
    if isinstance(doc.get("node_selector"), dict):
        needs["node_selector"] = {str(k): str(v) for k, v in doc["node_selector"].items() if k}
    return needs


def _is_port(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536


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
    selector = {str(k): str(v) for k, v in (pod.get("nodeSelector") or {}).items()}
    if selector.get("kubernetes.io/arch") in ARCHES and selector["kubernetes.io/arch"] != "amd64":
        needs["arch"] = selector["kubernetes.io/arch"]
    selector.pop("kubernetes.io/arch", None)
    if selector:
        needs["node_selector"] = selector
    # The listener and health path the app already runs with — so a foreign
    # app is hosted as it is, on whatever SDK or runtime it has.
    ports = [p.get("containerPort") for p in c0.get("ports") or [] if isinstance(p, dict)]
    if ports and _is_port(ports[0]) and ports[0] != APP_PORT:
        needs["port"] = ports[0]
    for probe in ("readinessProbe", "livenessProbe"):
        path = ((c0.get(probe) or {}).get("httpGet") or {}).get("path")
        if isinstance(path, str) and path.startswith("/"):
            needs["health"] = path
            break
    refs = [
        e["secretRef"]
        for e in c0.get("envFrom") or []
        if isinstance(e, dict) and e.get("secretRef", {}).get("name")
    ]
    secrets = [r["name"] for r in refs if not r.get("optional")]
    if secrets:
        needs["secrets_from"] = secrets
    optional = [r["name"] for r in refs if r.get("optional")]
    if optional:
        needs["secrets_from_optional"] = optional
    image_by_volume = {
        v["name"]: v["image"]["reference"]
        for v in pod.get("volumes") or []
        if isinstance(v, dict) and (v.get("image") or {}).get("reference")
    }
    for m in c0.get("volumeMounts") or []:
        if isinstance(m, dict) and m.get("name") in image_by_volume and m.get("mountPath") == "/bundle":
            needs["bundle_image"] = image_by_volume[m["name"]]
            break
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
    # Files the app reads from ConfigMaps and Secrets: a config file, a key
    # file. A subPath mount is one entry as one file; a plain mount is the
    # whole object as a directory. Either way the app said so.
    cm_by_volume = {
        v["name"]: v["configMap"]["name"]
        for v in pod.get("volumes") or []
        if isinstance(v, dict) and (v.get("configMap") or {}).get("name")
    }
    secret_by_volume = {
        v["name"]: v["secret"]["secretName"]
        for v in pod.get("volumes") or []
        if isinstance(v, dict) and (v.get("secret") or {}).get("secretName")
    }
    for m in c0.get("volumeMounts") or []:
        if not (isinstance(m, dict) and m.get("mountPath")):
            continue
        key = str(m.get("subPath") or "")
        if m.get("name") in cm_by_volume:
            needs.setdefault("config_mounts", []).append(
                ConfigMount(name=cm_by_volume[m["name"]], path=str(m["mountPath"]), key=key)
            )
        elif m.get("name") in secret_by_volume:
            needs.setdefault("secret_mounts", []).append(
                SecretMount(name=secret_by_volume[m["name"]], path=str(m["mountPath"]), key=key)
            )
    return needs


class Identity(BaseModel):
    """The pod's own GCP identity, made and granted by the platform.

    `gcp_roles` are project roles the app needs (Vertex, a bucket, …), each
    one on the operator's allowlist (iam.grantable_roles). Gojira creates
    `<app>-run@<project>` per project, grants the roles, binds the KSA
    through Workload Identity and has CI annotate the ServiceAccount from a
    repository variable — the GSA email names a project, so it is an
    install fact and never written into the repo. `gsa` adopts a
    hand-made account instead (granted and bound, never created or deleted):
    kobo-room and litellm on the dogfood, 2026-10-02. Design:
    docs/grants-and-platform-facts-design.md §1.
    """

    gcp_roles: list[str] = []
    gsa: str = ""

    @model_validator(mode="after")
    def _well_formed(self):
        for r in self.gcp_roles:
            if not re.fullmatch(r"(roles|projects/[^/]+/roles|organizations/\d+/roles)/[A-Za-z0-9_.]+", r):
                raise ValueError(f"identity.gcp_roles: {r!r} is not a role name")
        if self.gsa and not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]+\.iam\.gserviceaccount\.com", self.gsa):
            raise ValueError(f"identity.gsa: {self.gsa!r} is not a service account email")
        if not self.gcp_roles and not self.gsa:
            raise ValueError("identity: name gcp_roles, a gsa to adopt, or both")
        return self

    @property
    def project(self) -> str:
        """The adopted GSA's project, or '' when Gojira makes the account."""
        return self.gsa.split("@")[1].split(".")[0] if self.gsa else ""


class FileMount(BaseModel):
    """A ConfigMap or Secret the app reads as a file, not as env.

    Many apps take a config file (cabal's identity.toml, kobo's config.yaml)
    or a key file (a GitHub App's .pem) and cannot take the same thing as
    env. `key` picks one entry of the object and mounts it at `path` as that
    file (a subPath mount); no key mounts the whole object at `path` as a
    directory, one file per entry. The object itself stays app-authored,
    like the PVC behind data_mount (2026-09-30, the cabal install).
    """

    name: str
    path: str
    key: str = ""

    @property
    def volume(self) -> str:
        # A DNS label: the object name is one already; the prefix keeps a
        # ConfigMap and a Secret of the same name apart.
        return f"{self.kind_prefix}-{self.name}"[:63].rstrip("-")

    kind_prefix: str = "file"


class ConfigMount(FileMount):
    kind_prefix: str = "cm"


class SecretMount(FileMount):
    kind_prefix: str = "secret"
    # `optional: true` mounts an empty directory until the Secret exists: a
    # vendor credential a console writes after the app is up (bab's Datadog
    # and GitHub keys, read live from the directory on every call, so a
    # connect needs no restart — 2026-11-09). Required mounts keep the pod
    # from starting without the object, which is right for a key the app
    # cannot run without.
    optional: bool = False


def _read_mounts(raw, cls):
    out = []
    for m in raw if isinstance(raw, list) else []:
        if isinstance(m, dict) and m.get("name") and m.get("path"):
            fields = {"name": str(m["name"]), "path": str(m["path"]), "key": str(m.get("key") or "")}
            if cls is SecretMount:
                fields["optional"] = bool(m.get("optional", False))
            out.append(cls(**fields))
    return out


class GeneratorSpec(BaseModel):
    app: str
    repo_full_name: str
    workload_class: str
    environments: list[EnvSpec]
    default_branch: str = "main"
    # A public repository's workflows run on GitHub's hosted runners outright
    # and read no runner variable (issue #155; the rule every tenish repo
    # adopted for going public: hosted runners only on public repos). The
    # generated workflow runs on push and dispatch, never on pull_request, so
    # this is the rule, not a fork hazard. Private repos read
    # `vars.GOJIRA_CI_RUNNER`, which SetRepoVariables owns.
    public_repo: bool = False
    schedule: str | None = None  # cron class only
    overrides: Overrides | None = None  # from the repo's gojira.yaml, if any
    # App needs (finding #12) — sourced from the repo's gojira.yaml, like
    # overrides. Rendered into the deployment; referenced objects stay
    # app-authored.
    secrets_from: list[str] = []
    # Secrets the pod reads when they exist and boots without when they do not: a room
    # wires an agent's Slack and env Secrets this way at birth, so adding them later is
    # a Secret write and a restart, never a manifest change (tenishai/kobo#434).
    secrets_from_optional: list[str] = []
    # An OCI image whose filesystem is mounted read-only at /bundle, pulled by the node with
    # its image-pull credentials and refreshed on every pod start (Kubernetes image volume,
    # pullPolicy Always). A room's agent bundle travels this way so a change to it is a
    # restart, not a build, and the pod holds no registry credential (tenishai/kobo#434).
    bundle_image: str = ""
    config_from: list[str] = []  # envFrom ConfigMaps — non-secret config
    service_account: str = ""
    identity: Identity | None = None  # the pod's GCP identity, platform-made
    # Cluster-scoped access the pod needs: names of ClusterRoles the operator
    # defined (and allowlisted); Gojira binds the KSA to them. The deploy
    # identity cannot create cluster-scoped RBAC and must not be able to
    # (issue #86), so the role is the operator's and the binding is Gojira's.
    cluster_roles: list[str] = []
    # Labels the app's namespace must carry on every environment's cluster, for
    # what gates on them (2026-11-17: sero admits a machine caller by the label
    # `sero.dev/caller: "true"` on its namespace; the label had been hand-made on
    # two namespaces and would not survive a reinstall). Gojira adds them to the
    # namespace it made; the install's own labels (defaults.namespace_labels,
    # the managed label) are not the app's to set.
    namespace_labels: dict[str, str] = {}
    data_mount: DataMount | None = None
    config_mounts: list[ConfigMount] = []  # ConfigMaps read as files
    secret_mounts: list[SecretMount] = []  # Secrets read as files (key files)
    # Arguments to `docker build`, from the contract and so from git: never a
    # secret. AR_TOKEN is the one the platform adds itself, as a BuildKit
    # secret mount (cabal#250), never a build arg. What a build
    # needs to say to reach a package index, pick a base image, or set a
    # version is the app's to declare (2026-09-30: the tenish stack's images
    # install private packages from an index named this way).
    build_args: dict[str, str] = {}
    # A published image to deploy instead of building the repo (issue #108):
    # a full reference, registry/path:tag or @digest. The generated CI copies
    # it into the install's registry under the commit's tag; everything after
    # that (digest pin, apply, rollback, promote) is the same as for a build.
    published_image: str = ""

    def build_arg_refs(self) -> list[str]:
        """Repository variables the build args reference (`${NAME}` values)."""
        return sorted({r for r in map(variable_ref, self.build_args.values()) if r})
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
    # The app's listener and http health path. Class opinions are the
    # defaults (8080; /health or /healthz per class); the contract overrides
    # them, and adopt mode reads them from the deployment the app already runs.
    port: int = APP_PORT
    health: str = ""  # empty: the class's probe path
    # The CPU architecture the image is built for and the pod is scheduled
    # on. None: the contract did not say, so the platform's default applies
    # (defaults.arch; amd64 when the operator set none) — a platform fact
    # that never gets written into the repo. arm64 renders the build
    # platform, the arch nodeSelector and the toleration arm64 pools carry.
    # How a cluster provides arm64 is its own business (Autopilot wants a
    # compute class too) — that is what node_selector is for, with the same
    # contract-wins-over-platform rule (2026-10-02: the first agent kobo
    # created landed an arm64 image on an amd64 node).
    arch: str | None = None
    node_selector: dict[str, str] | None = None
    platform_arch: str = "amd64"
    platform_node_selector: dict[str, str] = {}

    @property
    def ksa_name(self) -> str:
        """The pod's Kubernetes ServiceAccount: named by the contract, or the
        app's own name once an identity or a cluster role makes Gojira render it."""
        return self.service_account or (self.app if (self.identity or self.cluster_roles) else "")

    def run_gsa(self, env: "EnvSpec") -> str:
        """The GSA the pod runs as in this environment's project: the adopted
        one, or the `<app>-run@<project>` account Gojira makes per project."""
        if not self.identity:
            return ""
        return self.identity.gsa or f"{self.app}-run@{env.project}.iam.gserviceaccount.com"

    @property
    def effective_arch(self) -> str:
        return self.arch or self.platform_arch or "amd64"

    @property
    def effective_node_selector(self) -> dict[str, str]:
        return self.node_selector if self.node_selector is not None else self.platform_node_selector

    @model_validator(mode="after")
    def validate_spec(self):
        if not _is_port(self.port):
            raise ValueError(f"port must be 1-65535, got {self.port!r}")
        if self.health and not self.health.startswith("/"):
            raise ValueError(f"health must be an absolute path, got {self.health!r}")
        if self.admin_port == self.port:
            raise ValueError("admin_port must differ from the app port")
        if self.arch is not None and self.arch not in ARCHES:
            raise ValueError(f"arch must be one of {ARCHES}, got {self.arch!r}")
        if self.platform_arch not in ARCHES:
            raise ValueError(f"platform arch must be one of {ARCHES}, got {self.platform_arch!r}")
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

    return add_resources(rendered_text, [e for e in entries if file_exists(e)])


def add_resources(
    rendered_text: str, entries: list[str], comment: str = "app-authored resources, preserved by Gojira:"
) -> str:
    """Append `entries` to a rendered base kustomization's `resources:`, under
    a one-line comment saying whose they are. Entries the render already lists
    are not repeated. Appended textually; the render's comments survive."""
    rendered = yaml.safe_load(rendered_text) or {}
    have = set(rendered.get("resources") or [])
    keep = [e for e in entries if e not in have]
    if not keep:
        return rendered_text

    sep = "" if rendered_text.endswith("\n") else "\n"
    lines = [f"  # {comment}"]
    lines += [f"  - {e}" for e in keep]
    return f"{rendered_text}{sep}" + "\n".join(lines) + "\n"


# A caller's seed for the first setup PR (POST /api/v1/apps `seed`): the
# contract keys it may name, and the bounds on the files it may add.
SEED_FILE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*\.yaml")
SEED_FILE_MAX = 8
SEED_FILE_MAX_BYTES = 16 * 1024
# What a seed file may hold: the objects a contract's own keys point at
# (`service_account:`, `data_mount:`, `config_from:`) and nothing that grants.
SEED_KINDS = frozenset({"ServiceAccount", "PersistentVolumeClaim", "ConfigMap"})
# The contract keys a seed may set: what the pod reads and mounts. Not
# `identity:` or `cluster_roles:`: those are granted by the steps that run
# before the setup PR is opened, so from a seed they would take effect with
# nobody having read them. Not the app's own image, port or build either.
SEED_NEEDS = frozenset({
    "service_account", "env", "secrets_from", "secrets_from_optional",
    "config_from", "data_mount", "config_mounts", "secret_mounts",
})


# A seed's `needs`, typed as gojira.yaml takes them and nothing looser. Strict:
# JSON `true` is not the string "true", `{a: 1}` is not a name, and a key the
# contract does not have is refused, so a wrong shape is turned away here
# rather than coerced by the reader into something the pod reads differently
# (review of #140). Names are DNS-1123 labels, paths are absolute.
_NAME_RE = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
_NAME = Field(pattern=_NAME_RE, max_length=253)
_PATH = Field(pattern=r"^/\S*$", max_length=1024)
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _SeedDataMount(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    pvc: str = _NAME
    path: str = _PATH
    fs_group: int = Field(default=999, ge=0, le=2**31 - 1)


class _SeedConfigMount(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    name: str = _NAME
    path: str = _PATH
    key: str = Field(default="", max_length=253)


class _SeedSecretMount(_SeedConfigMount):
    optional: bool = False


class SeedNeeds(BaseModel):
    """The contract keys a seed may set (`SEED_NEEDS`), each in the shape
    gojira.yaml takes. `identity` and `cluster_roles` are not fields: the
    steps that run before the setup PR would grant them unreviewed."""

    model_config = ConfigDict(strict=True, extra="forbid")
    service_account: str | None = Field(default=None, pattern=_NAME_RE, max_length=253)
    env: dict[str, str] | None = None
    secrets_from: list[str] | None = None
    secrets_from_optional: list[str] | None = None
    config_from: list[str] | None = None
    data_mount: _SeedDataMount | None = None
    config_mounts: list[_SeedConfigMount] | None = None
    secret_mounts: list[_SeedSecretMount] | None = None

    @model_validator(mode="after")
    def _names_and_keys(self):
        for key in ("secrets_from", "secrets_from_optional", "config_from"):
            for name in getattr(self, key) or []:
                if not re.fullmatch(_NAME_RE, name) or len(name) > 253:
                    raise ValueError(f"{key}: {name!r} is not a Kubernetes object name")
        for k in self.env or {}:
            if not _ENV_KEY.fullmatch(k):
                raise ValueError(f"env: {k!r} is not an environment variable name")
        return self


def seed_needs(needs: dict) -> dict:
    """A seed's `needs` as GeneratorSpec updates: the same keys, read by the
    same reader, as a repo's own gojira.yaml. Raises ValueError naming a key
    a seed may not set (`SEED_NEEDS`), a value not in the shape the contract
    takes (`SeedNeeds`), or one that would not read back from the written
    contract as it was asked for: a caller that asked for something must not
    find out from a pod that it was never rendered, nor a reviewer merge one
    name and run another."""
    if not isinstance(needs, dict):
        raise ValueError("seed.needs must be a mapping of gojira.yaml keys")
    refused = sorted(str(k) for k in needs if k not in SEED_NEEDS)
    if refused:
        raise ValueError(
            "seed.needs: a seed sets only " + ", ".join(sorted(SEED_NEEDS))
            + "; not " + ", ".join(refused)
        )
    try:
        typed = SeedNeeds.model_validate(needs)
    except ValidationError as exc:
        where = sorted({".".join(str(p) for p in e["loc"]) or "needs" for e in exc.errors()})
        raise ValueError("seed.needs: not in the shape gojira.yaml takes: " + ", ".join(where)) from exc
    data = typed.model_dump(exclude_none=True)
    read = read_app_needs(yaml.safe_dump(data))
    # read_app_needs names one of these keys differently on the spec.
    spec_key = {"env": "env_vars"}
    dropped = sorted(k for k in data if spec_key.get(k, k) not in read)
    if dropped:
        raise ValueError("seed.needs: not in the shape gojira.yaml takes: " + ", ".join(dropped))
    # The contract is written by a template, not a YAML dumper, so a name
    # such as `on`, `a #b` or one with a newline in it is one thing here and
    # another in the merged file. Write it, read it back, and refuse a
    # difference now rather than open a second pull request to undo it.
    spec = GeneratorSpec.model_construct(
        app="seed", repo_full_name="seed/seed", workload_class="service", environments=[],
    ).model_copy(update=read)
    back = read_app_needs(_t("gojira.yaml.j2", {"spec": spec}))
    changed = sorted(k for k in data if back.get(spec_key.get(k, k)) != read[spec_key.get(k, k)])
    if changed:
        raise ValueError(
            "seed.needs: would not read back from gojira.yaml as written: " + ", ".join(changed)
        )
    return read


def check_seed_files(base_files: dict) -> None:
    """Refuse a seed file that is not a plain `<name>.yaml` holding named
    core objects of the kinds a seed may add (`SEED_KINDS`): the objects a
    contract key names, never one that grants access or runs something. Two
    files naming the same object are refused too: kustomize would refuse the
    build, and a seed cannot be changed once it is on the row."""
    if not isinstance(base_files, dict):
        raise ValueError("seed.base_files must map a file name to its text")
    if len(base_files) > SEED_FILE_MAX:
        raise ValueError(f"seed.base_files: at most {SEED_FILE_MAX} files")
    seen: dict[tuple[str, str], str] = {}
    for name, text in base_files.items():
        if not isinstance(name, str) or not SEED_FILE_NAME.fullmatch(name) or name == "kustomization.yaml":
            raise ValueError(f"seed.base_files: {name!r} is not a plain <name>.yaml in k8s/base")
        if not isinstance(text, str) or len(text.encode()) > SEED_FILE_MAX_BYTES:
            raise ValueError(f"seed.base_files: {name} must be text of at most {SEED_FILE_MAX_BYTES} bytes")
        try:
            docs = [d for d in yaml.safe_load_all(text) if d is not None]
        except (yaml.YAMLError, RecursionError) as exc:  # nesting past the parser's depth is a RecursionError
            raise ValueError(f"seed.base_files: {name} is not YAML") from exc
        if not docs or not all(isinstance(d, dict) and isinstance(d.get("kind"), str) for d in docs):
            raise ValueError(f"seed.base_files: {name} must hold Kubernetes objects")
        for doc in docs:
            if doc["kind"] not in SEED_KINDS:
                raise ValueError(
                    f"seed.base_files: {name} holds a {doc['kind']}; a seed adds only "
                    + ", ".join(sorted(SEED_KINDS))
                )
            if doc.get("apiVersion") != "v1":
                raise ValueError(f"seed.base_files: {name}: a {doc['kind']} has apiVersion v1")
            meta = doc.get("metadata")
            obj = meta.get("name") if isinstance(meta, dict) else None
            if not isinstance(obj, str) or not obj:
                raise ValueError(f"seed.base_files: {name}: every object needs metadata.name")
            if (doc["kind"], obj) in seen:
                raise ValueError(
                    f"seed.base_files: {name} and {seen[(doc['kind'], obj)]} both hold {doc['kind']} {obj}"
                )
            seen[(doc["kind"], obj)] = name


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
        if spec.overrides.hpa is not None and cls.hpa is not None:
            cls.hpa = spec.overrides.hpa.apply(cls.hpa)
    # Effective probes: the class's shape (http/tcp, which probes exist),
    # with the contract's health path where the class probes over http.
    probes = {
        name: (p.model_copy(update={"path": spec.health}) if spec.health and p.type == "http" else p)
        for name, p in cls.probes.items()
    }
    ctx = {
        "spec": spec,
        "cls": cls,
        "probes": probes,
        "ephemeral": sizing.ephemeral_storage,
        "port": spec.port,
        "is_cron": spec.workload_class == "cron",
        "has_service": _has_service(cls),
        "kustomize": KUSTOMIZE,
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
    if spec.identity or spec.cluster_roles:
        files[f"{base}/serviceaccount.yaml"] = _t("k8s/serviceaccount.yaml.j2", ctx)
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
