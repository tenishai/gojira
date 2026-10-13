"""Configuration loading: config.yaml with ${ENV_VAR} interpolation."""

import logging
import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

log = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


class GitHubConfig(BaseModel):
    app_id: str = ""
    private_key_path: str = ""
    webhook_secret: str = ""


class DatabaseConfig(BaseModel):
    url: str = "sqlite:///gojira.db"


class DefaultsConfig(BaseModel):
    environments: list[str] = ["dev", "qa", "prod"]
    # Stamped on every namespace Gojira creates (and re-asserted on
    # re-onboard). Secrets infrastructure commonly gates on namespace labels
    # (e.g. an ESO ClusterSecretStore matching example.com/team) — a missing
    # label means ExternalSecrets silently never sync (a tenant's finding).
    namespace_labels: dict[str, str] = {}
    # The architecture apps are built for and scheduled on when their
    # contract does not say (`arch:` in gojira.yaml wins). A platform fact:
    # it depends on what your runners can build and your clusters can run,
    # so it lives here and is never written into a repo. Empty = amd64.
    # Prefer arm64 where both sides can (cheaper and more available than
    # amd64 on GCP and AWS, 2026); a GitHub-hosted ubuntu-latest runner
    # cannot build it without QEMU, so this is the operator's call.
    arch: str = ""
    # Added to every pod's nodeSelector when the contract has none: how
    # your clusters provide that arch (Autopilot: a compute class; Standard:
    # a pool label). YAML flow map, like namespace_labels.
    node_selector: dict[str, str] = {}
    # The cluster an environment name means on this install when a connect
    # request names none ({dev: gojira-sandbox}). With it, a contract or an
    # API call can say `environments: [dev]` and be deployable under any
    # Gojira (issue #87). YAML flow map, like namespace_labels.
    environment_clusters: dict[str, str] = {}

    @field_validator("namespace_labels", "node_selector", "environment_clusters", mode="before")
    @classmethod
    def _labels_from_string(cls, v):
        # The in-cluster configmap interpolates these from one env var each
        # (GOJIRA_NAMESPACE_LABELS, GOJIRA_DEFAULT_NODE_SELECTOR,
        # GOJIRA_ENVIRONMENT_CLUSTERS: YAML flow maps); empty means none.
        if isinstance(v, str):
            return yaml.safe_load(v) or {}
        return v

    @field_validator("arch")
    @classmethod
    def _arch_known(cls, v: str) -> str:
        if v and v not in ("amd64", "arm64"):
            raise ValueError(f"defaults.arch must be amd64 or arm64, got {v!r}")
        return v


class DriftConfig(BaseModel):
    """The central drift detector. Off by default — opt in per operator."""

    enabled: bool = False
    interval_seconds: int = 900  # 15 min — matches the Phase 2 DoD


class SizingConfig(BaseModel):
    """The Phase 4 rightsizing loop's knobs (see docs/sizing-feedback-design.md)."""

    materiality_pct: float = 30.0  # no proposal unless the request moves this much
    limit_headroom: float = 1.5    # proposed limit = p99 * headroom
    history_days: int = 30         # evidence window the recommender reads

    # The sizing agent Gojira deploys into registered clusters (Track C).
    # agent_image is the one required knob — there is no public image yet.
    agent_image: str = ""
    # Where deployed agents report back. The default assumes the agent shares
    # a cluster with Gojira; remote clusters need Gojira's external URL here.
    agent_gojira_url: str = "http://gojira.gojira.svc.cluster.local"
    # Empty → Google Managed Prometheus for the target cluster's own project.
    agent_prometheus_url: str = ""
    agent_prometheus_auth: str = "google"
    # GMP metric names by default — Gojira is GKE-opinionated. Literal PromQL
    # braces are doubled: the agent runs these through str.format, which
    # reserves single braces for the {namespace}/{app} slots.
    agent_cpu_query: str = (
        "sum by (pod_name) (rate(kubernetes_io:container_cpu_core_usage_time"
        '{{namespace_name="{namespace}", pod_name=~"{app}-.*"}}[5m]))'
    )
    agent_mem_query: str = (
        "max by (pod_name) (kubernetes_io:container_memory_used_bytes"
        '{{namespace_name="{namespace}", pod_name=~"{app}-.*", memory_type="non-evictable"}})'
    )


class AuthConfig(BaseModel):
    """Identity at the front door — the trust-the-proxy pattern.

    Gojira does not speak OAuth itself: an Identity-Aware Proxy in front of
    the install authenticates the human and asserts the identity in the
    x-goog-authenticated-user-email header; Gojira trusts that header and
    makes the DECISIONS (allowlist, and someday roles). Off by default —
    laptop mode and port-forward installs keep working unchanged. Never
    enable without IAP (or an equivalent trusted proxy) actually in front:
    the header is trivially forgeable on a bare endpoint.
    """

    enabled: bool = False
    allowed_domain: str = ""        # e.g. example.com — any user in the domain
    allowed_emails: list[str] = []  # explicit allowlist, additive to domain
    # Bearer token gating the IAP-exempt privileged /api/v1 onboarding routes
    # (api_connect, api_app_status) — that namespace is exempted from the IAP
    # gate above on the premise its routes carry their own auth; unset here
    # while enabled is True means those routes have none, so callers must
    # configure this (web/auth.py's api_token_error fails closed in that case).
    api_token: str | None = None

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def _emails_from_string(cls, v):
        # GOJIRA_ALLOWED_EMAILS=a@x.com,b@x.com in the install values.
        if isinstance(v, str):
            return [e.strip() for e in v.split(",") if e.strip()]
        return v


class ObserveClusterConfig(BaseModel):
    """Where argus lives on one cluster. The minimal real-world entry is the
    defaults — an empty mapping value enables observe for that cluster."""

    namespace: str = "argus"
    service_account: str = "argus-observe"


class ObserveConfig(BaseModel):
    """Born observed: hand each provisioned app's contract to an in-cluster
    `argus observe` Job (co-designed with argus, 2026-09-17). Feature-off-
    when-absent: no job_image or no entry for an env's cluster = the step
    skips. job_image should be digest-pinned (tag for humans, digest for
    receipts — #37).
    """

    job_image: str = ""
    ttl_seconds: int = 600            # completed Jobs clean themselves up
    active_deadline_seconds: int = 900  # argus's own outer bound on the summary line
    clusters: dict[str, ObserveClusterConfig] = {}

    @field_validator("clusters", mode="before")
    @classmethod
    def _clusters_from_string(cls, v):
        # The in-cluster configmap hands this over as one env var
        # (GOJIRA_OBSERVE_CLUSTERS, a comma list of registered cluster names):
        # ${VAR} interpolation reaches values, never mapping keys, so a
        # string is the only shape the install path can deliver. Each name
        # gets the defaults (argus namespace, argus-observe SA); an operator
        # who needs anything else writes the mapping form. Empty means none.
        if isinstance(v, str):
            return {name.strip(): {} for name in v.split(",") if name.strip()}
        return v


class CIConfig(BaseModel):
    # A self-hosted runner label (e.g. "spark"). Generated workflows run on
    # `${{ vars.CI_RUNNER || 'ubuntu-latest' }}`; when set, provisioning
    # writes CI_RUNNER as a repository variable on every connected repo. A
    # repository variable, not an organization one: on GitHub's Free plan
    # org-level variables never reach private repositories (2026-09-30).
    # Empty: GitHub's hosted runners.
    runner: str = ""
    # Values a contract may reference instead of writing: `build_args:
    # {EXTRA_INDEX_URL: ${GOJIRA_PYTHON_INDEX}}` in gojira.yaml reads
    # `vars.GOJIRA_PYTHON_INDEX` at build time, and this map names what each
    # such variable is on THIS install (GOJIRA_REPO_VARIABLES, YAML flow map).
    # Set on every connected repo next to the GOJIRA_<ENV>_* facts, so a
    # package index in the install's own project never lands in a repo
    # (issue #102). Names: upper-case, digits and underscores.
    repo_variables: dict[str, str] = {}

    @field_validator("repo_variables", mode="before")
    @classmethod
    def _map_from_string(cls, v):
        if isinstance(v, str):
            return yaml.safe_load(v) or {}
        return v

    @field_validator("repo_variables")
    @classmethod
    def _variable_names(cls, v):
        bad = [k for k in v if not re.fullmatch(r"[A-Z][A-Z0-9_]*", str(k))]
        if bad:
            raise ValueError(f"repo_variables names must be upper-case identifiers: {bad}")
        return {str(k): str(val) for k, val in v.items()}


class IamConfig(BaseModel):
    # The project roles an app may ask for in gojira.yaml `identity.gcp_roles`
    # (GOJIRA_GRANTABLE_ROLES, comma list). A role not on the list fails the
    # provision naming it: this is a security boundary, enforced, not the
    # overrides-style social `justification`. Empty: identity: is refused.
    grantable_roles: list[str] = []
    # ClusterRoles an app may ask to be bound to in `cluster_roles`
    # (GOJIRA_GRANTABLE_CLUSTER_ROLES, comma list). The operator applies the
    # ClusterRole itself; Gojira only makes the binding. Empty: refused.
    grantable_cluster_roles: list[str] = []

    @field_validator("grantable_roles", "grantable_cluster_roles", mode="before")
    @classmethod
    def _roles_from_string(cls, v):
        if isinstance(v, str):
            return [r.strip() for r in v.split(",") if r.strip()]
        return v


class Config(BaseModel):
    auth: AuthConfig = AuthConfig()
    ci: CIConfig = CIConfig()
    iam: IamConfig = IamConfig()
    github: GitHubConfig = GitHubConfig()
    database: DatabaseConfig = DatabaseConfig()
    defaults: DefaultsConfig = DefaultsConfig()
    drift: DriftConfig = DriftConfig()
    sizing: SizingConfig = SizingConfig()
    observe: ObserveConfig = ObserveConfig()


def _interpolate(value: str) -> str:
    """Replace ${VAR} or ${VAR:-default} with environment values."""

    def repl(m: re.Match) -> str:
        var, default = m.group(1), m.group(2)
        resolved = os.environ.get(var, default)
        if resolved is None:
            raise ValueError(f"config references unset environment variable {var}")
        return resolved

    return _ENV_PATTERN.sub(repl, value)


def _walk(node):
    if isinstance(node, dict):
        return {k: _walk(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v) for v in node]
    if isinstance(node, str):
        return _interpolate(node)
    return node


def load_config(path: str | Path | None = None) -> Config:
    """Load config from path, $GOJIRA_CONFIG, or ./config.yaml; defaults if absent."""
    candidate = path or os.environ.get("GOJIRA_CONFIG") or "config.yaml"
    candidate = Path(candidate)
    if not candidate.exists():
        log.debug("no config file at %s, using defaults", candidate)
        return Config()
    raw = yaml.safe_load(candidate.read_text()) or {}
    log.debug("loaded config from %s", candidate)
    return Config.model_validate(_walk(raw))
