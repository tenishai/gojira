"""GCP operations behind an interface.

Steps depend on the GCPClient ABC, never on gcloud directly — so they're
testable with a fake and the backend is swappable. The v0.1 backend shells
out to `gcloud`: transparent (operators see the real commands in logs),
matches the manual runbook, and avoids guessing at SDK surface for the
fiddly bits (Workload Identity Federation). Cost: a runtime gcloud
dependency, documented and acceptable for v0.1.

All operations are idempotent at the gcloud layer (add-iam-policy-binding,
services enable, etc. are no-ops when already applied); the step engine's
check()/apply() split layers explicit idempotency on top for clean ledger
semantics.
"""

import logging
from abc import ABC, abstractmethod

from gojira.shell import CommandRunner

log = logging.getLogger(__name__)

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
WIF_ATTRIBUTE_MAPPING = (
    "google.subject=assertion.sub,"
    "attribute.repository=assertion.repository,"
    "attribute.repository_owner=assertion.repository_owner"
)


class GCPError(RuntimeError):
    """A gcloud command failed for a reason other than 'not found'."""


class GCPClient(ABC):
    """The GCP operations the provisioner needs. Implementations must be idempotent."""

    @abstractmethod
    def project_number(self, project: str) -> str: ...

    @abstractmethod
    def api_enabled(self, project: str, api: str) -> bool: ...

    @abstractmethod
    def enable_api(self, project: str, api: str) -> None: ...

    @abstractmethod
    def service_account_exists(self, project: str, email: str) -> bool: ...

    @abstractmethod
    def create_service_account(self, project: str, account_id: str, display_name: str) -> str: ...

    @abstractmethod
    def wif_pool_exists(self, project: str, pool: str) -> bool: ...

    @abstractmethod
    def create_wif_pool(self, project: str, pool: str, display_name: str) -> None: ...

    @abstractmethod
    def wif_provider_exists(self, project: str, pool: str, provider: str) -> bool: ...

    @abstractmethod
    def create_wif_provider(
        self, project: str, pool: str, provider: str, allowed_owner: str
    ) -> None: ...

    @abstractmethod
    def sa_has_binding(self, project: str, sa_email: str, member: str, role: str) -> bool: ...

    @abstractmethod
    def add_sa_binding(self, project: str, sa_email: str, member: str, role: str) -> None: ...

    @abstractmethod
    def project_has_binding(self, project: str, member: str, role: str) -> bool: ...

    @abstractmethod
    def add_project_binding(self, project: str, member: str, role: str) -> None: ...

    @abstractmethod
    def artifact_repo_exists(self, project: str, location: str, repo: str) -> bool: ...

    @abstractmethod
    def create_artifact_repo(self, project: str, location: str, repo: str) -> None: ...

    @abstractmethod
    def get_gke_credentials(self, project: str, location: str, cluster: str) -> str:
        """Populate local kubeconfig for the cluster; return the context name."""
        ...

    @abstractmethod
    def dns_access_enabled(self, project: str, location: str, cluster: str) -> bool: ...

    @abstractmethod
    def enable_dns_access(self, project: str, location: str, cluster: str) -> None:
        """Allow external traffic on the cluster's IAM-authenticated DNS endpoint.

        Generated CI reaches control planes exclusively through it (finding
        #14): GitHub runners cannot reach a private endpoint IP, and the DNS
        endpoint authenticates every request through IAM either way.
        """
        ...


class GcloudClient(GCPClient):
    """gcloud-backed GCPClient. Each method maps to one or two gcloud commands."""

    def __init__(self, runner: CommandRunner | None = None, gcloud: str = "gcloud"):
        self._run = (runner or CommandRunner()).run
        self._gcloud = gcloud

    # --- helpers -------------------------------------------------------------

    def _exec(self, *args: str) -> str:
        """Run a gcloud command that must succeed; return stdout."""
        result = self._run([self._gcloud, *args])
        if result.returncode != 0:
            raise GCPError(f"gcloud {' '.join(args)} failed: {result.stderr}")
        return result.stdout

    def _exists(self, *args: str) -> bool:
        """Run a describe-style command; True if it succeeds, False if not-found."""
        result = self._run([self._gcloud, *args])
        if result.returncode == 0:
            return True
        if "NOT_FOUND" in result.stderr or "not found" in result.stderr.lower():
            return False
        raise GCPError(f"gcloud {' '.join(args)} failed: {result.stderr}")

    # --- project -------------------------------------------------------------

    def project_number(self, project: str) -> str:
        return self._exec(
            "projects", "describe", project, "--format=value(projectNumber)"
        )

    def api_enabled(self, project: str, api: str) -> bool:
        out = self._exec(
            "services", "list", "--enabled", f"--project={project}",
            f"--filter=config.name={api}", "--format=value(config.name)",
        )
        return api in out

    def enable_api(self, project: str, api: str) -> None:
        self._exec("services", "enable", api, f"--project={project}")

    # --- service accounts ----------------------------------------------------

    def service_account_exists(self, project: str, email: str) -> bool:
        return self._exists(
            "iam", "service-accounts", "describe", email,
            f"--project={project}", "--format=value(email)",
        )

    def create_service_account(self, project: str, account_id: str, display_name: str) -> str:
        self._exec(
            "iam", "service-accounts", "create", account_id,
            f"--display-name={display_name}", f"--project={project}",
        )
        return f"{account_id}@{project}.iam.gserviceaccount.com"

    # --- workload identity federation ----------------------------------------

    def wif_pool_exists(self, project: str, pool: str) -> bool:
        return self._exists(
            "iam", "workload-identity-pools", "describe", pool,
            "--location=global", f"--project={project}", "--format=value(name)",
        )

    def create_wif_pool(self, project: str, pool: str, display_name: str) -> None:
        self._exec(
            "iam", "workload-identity-pools", "create", pool,
            "--location=global", f"--display-name={display_name}", f"--project={project}",
        )

    def wif_provider_exists(self, project: str, pool: str, provider: str) -> bool:
        return self._exists(
            "iam", "workload-identity-pools", "providers", "describe", provider,
            f"--workload-identity-pool={pool}", "--location=global",
            f"--project={project}", "--format=value(name)",
        )

    def create_wif_provider(
        self, project: str, pool: str, provider: str, allowed_owner: str
    ) -> None:
        # The attribute-condition restricts token issuance to one GitHub owner —
        # defense in depth on top of the per-repo principalSet binding. v0.1
        # assumes one GitHub owner/org per Gojira install.
        self._exec(
            "iam", "workload-identity-pools", "providers", "create-oidc", provider,
            f"--workload-identity-pool={pool}", "--location=global",
            f"--issuer-uri={GITHUB_OIDC_ISSUER}",
            f"--attribute-mapping={WIF_ATTRIBUTE_MAPPING}",
            f"--attribute-condition=assertion.repository_owner == '{allowed_owner}'",
            f"--project={project}",
        )

    # --- IAM bindings --------------------------------------------------------

    def sa_has_binding(self, project: str, sa_email: str, member: str, role: str) -> bool:
        out = self._exec(
            "iam", "service-accounts", "get-iam-policy", sa_email,
            f"--project={project}", "--format=json",
        )
        return _policy_has(out, member, role)

    def add_sa_binding(self, project: str, sa_email: str, member: str, role: str) -> None:
        self._exec(
            "iam", "service-accounts", "add-iam-policy-binding", sa_email,
            f"--member={member}", f"--role={role}", f"--project={project}",
        )

    def project_has_binding(self, project: str, member: str, role: str) -> bool:
        out = self._exec(
            "projects", "get-iam-policy", project, "--format=json",
        )
        return _policy_has(out, member, role)

    def add_project_binding(self, project: str, member: str, role: str) -> None:
        self._exec(
            "projects", "add-iam-policy-binding", project,
            f"--member={member}", f"--role={role}",
        )

    # --- artifact registry ---------------------------------------------------

    def artifact_repo_exists(self, project: str, location: str, repo: str) -> bool:
        return self._exists(
            "artifacts", "repositories", "describe", repo,
            f"--location={location}", f"--project={project}", "--format=value(name)",
        )

    def create_artifact_repo(self, project: str, location: str, repo: str) -> None:
        self._exec(
            "artifacts", "repositories", "create", repo,
            "--repository-format=docker", f"--location={location}", f"--project={project}",
        )

    # --- gke -----------------------------------------------------------------

    def get_gke_credentials(self, project: str, location: str, cluster: str) -> str:
        # The pipeline has already ensured DNS access (ensure_cluster_dns_access);
        # the default endpoint form writes a private IP that is unreachable
        # off-VPC and silently clobbers the shared kubeconfig.
        self._exec(
            "container", "clusters", "get-credentials", cluster,
            f"--location={location}", f"--project={project}", "--dns-endpoint",
        )
        return f"gke_{project}_{location}_{cluster}"

    def dns_access_enabled(self, project: str, location: str, cluster: str) -> bool:
        out = self._exec(
            "container", "clusters", "describe", cluster,
            f"--location={location}", f"--project={project}",
            "--format=value(controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic)",
        )
        return out.strip().lower() == "true"

    def enable_dns_access(self, project: str, location: str, cluster: str) -> None:
        self._exec(
            "container", "clusters", "update", cluster,
            f"--location={location}", f"--project={project}", "--enable-dns-access",
        )


def _policy_has(policy_json: str, member: str, role: str) -> bool:
    import json

    policy = json.loads(policy_json) if policy_json else {}
    for binding in policy.get("bindings", []):
        if binding.get("role") == role and member in binding.get("members", []):
            return True
    return False
