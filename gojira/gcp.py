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

    # --- teardown --------------------------------------------------------

    @abstractmethod
    def remove_project_binding(self, project: str, member: str, role: str) -> bool:
        """Remove a project-level IAM binding. False if it was already gone."""
        ...

    @abstractmethod
    def delete_service_account(self, project: str, email: str) -> bool:
        """Delete a service account. False if it was already gone.

        No unbind-WIF-repo-member method exists on purpose: the principalSet
        binding granting a repo impersonation of this GSA lives on the GSA's
        own IAM policy (add_sa_binding), not on the pool or provider, so it
        is destroyed along with the GSA by this call. Nothing else
        references it.
        """
        ...

    @abstractmethod
    def delete_app_images(self, ar_repo: str, name: str) -> int:
        """Delete every image digest under {ar_repo}/{name}; return the count deleted."""
        ...

    @abstractmethod
    def image_digest(self, image: str, tag: str) -> str | None:
        """The sha256 digest the registry holds for image:tag, or None when the
        tag doesn't exist (that commit was never built and pushed here). The
        receipt the rollback path pins to — same lookup the generated CI does
        at deploy time."""
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
        """Run a command whose success/not-found is carried by exit code and
        stderr text: True if it succeeds, False if the target is already
        gone, raise otherwise. Covers both describe-style existence checks
        and check-then-act teardown verbs (e.g. remove-iam-policy-binding),
        which share the same not-found convention.
        """
        result = self._run([self._gcloud, *args])
        if result.returncode == 0:
            return True
        if "NOT_FOUND" in result.stderr or "not found" in result.stderr.lower():
            return False
        raise GCPError(f"gcloud {' '.join(args)} failed: {result.stderr}")

    def _list_or_empty(self, *args: str) -> str:
        """Run a list-style command; return stdout, or empty string if the
        resource the list is scoped to is already gone (NOT_FOUND).
        """
        result = self._run([self._gcloud, *args])
        if result.returncode == 0:
            return result.stdout
        if "NOT_FOUND" in result.stderr or "not found" in result.stderr.lower():
            return ""
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


    def image_digest(self, image: str, tag: str) -> str | None:
        # tags list, NOT images describe: describe also queries Container
        # Analysis for its vulnerability summary, which needs
        # containeranalysis.occurrences.list — a permission no Artifact
        # Registry role carries. We asked for a digest and got a security
        # report's ACL instead (first rollback dogfood, 2026-09-17). tags
        # list answers from AR alone, covered by reader and up.
        # gcloud's `tag:` filter is a prefix match (tag:dev listed three rows
        # on the live registry, 2026-09-29), and the old value(version) output
        # would have joined them into one newline-riddled "digest" — a broken
        # image ref, or a wrong pin. Ask for the tag column too and pick the
        # exact tag ourselves.
        result = self._run([
            self._gcloud, "artifacts", "docker", "tags", "list", image,
            f"--filter=tag:{tag}", "--format=value(tag,version)",
        ])
        if result.returncode != 0:
            err = result.stderr or ""
            if "not found" in err.lower() or "NOT_FOUND" in err:
                return None
            raise GCPError(f"tags list {image}:{tag} failed: {err}")
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == tag:
                return parts[1]
        return None
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

    # --- teardown --------------------------------------------------------

    def remove_project_binding(self, project: str, member: str, role: str) -> bool:
        # gcloud raises IamPolicyBindingNotFound ("Policy binding with the
        # specified principal and role not found!") when there is nothing to
        # remove; that message satisfies _exists's not-found substring match.
        return self._exists(
            "projects", "remove-iam-policy-binding", project,
            f"--member={member}", f"--role={role}", "--quiet",
        )

    def delete_service_account(self, project: str, email: str) -> bool:
        if not self.service_account_exists(project, email):
            return False
        self._exec(
            "iam", "service-accounts", "delete", email,
            f"--project={project}", "--quiet",
        )
        return True

    def delete_app_images(self, ar_repo: str, name: str) -> int:
        image = f"{ar_repo}/{name}"
        out = self._list_or_empty(
            "artifacts", "docker", "images", "list", image, "--format=value(DIGEST)",
        )
        digests = [line.strip() for line in out.splitlines() if line.strip()]
        for digest in digests:
            self._exec(
                "artifacts", "docker", "images", "delete", f"{image}@{digest}",
                "--delete-tags", "--quiet",
            )
        return len(digests)


def _policy_has(policy_json: str, member: str, role: str) -> bool:
    import json

    policy = json.loads(policy_json) if policy_json else {}
    for binding in policy.get("bindings", []):
        if binding.get("role") == role and member in binding.get("members", []):
            return True
    return False
