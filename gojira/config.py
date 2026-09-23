"""Configuration loading: config.yaml with ${ENV_VAR} interpolation."""

import logging
import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

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
    # label means ExternalSecrets silently never sync (Rook's finding).
    namespace_labels: dict[str, str] = {}


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


class Config(BaseModel):
    auth: AuthConfig = AuthConfig()
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
