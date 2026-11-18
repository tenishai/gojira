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
import re
from abc import ABC, abstractmethod

from gojira.shell import CommandRunner

log = logging.getLogger(__name__)

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"

# The provider's attribute condition names the GitHub owners (users or orgs)
# whose repos may exchange tokens at all — defense in depth on top of the
# per-repo binding. Gojira writes one of two shapes and reads both back;
# anything else is a person's own condition and is left alone.
_OWNER_EQ = re.compile(r"^assertion\.repository_owner\s*==\s*'([^']+)'$")
_OWNER_IN = re.compile(r"^assertion\.repository_owner\s+in\s+\[([^\]]*)\]$")


def owner_condition(owners: list[str]) -> str:
    names = sorted(set(owners))
    if len(names) == 1:
        return f"assertion.repository_owner == '{names[0]}'"
    return "assertion.repository_owner in [" + ", ".join(f"'{n}'" for n in names) + "]"


def owners_in_condition(condition: str) -> list[str] | None:
    """The owners a Gojira-written condition admits; None for any other shape."""
    text = condition.strip()
    if m := _OWNER_EQ.match(text):
        return [m.group(1)]
    if m := _OWNER_IN.match(text):
        return re.findall(r"'([^']+)'", m.group(1))
    return None
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

    def wif_provider_condition(self, project: str, pool: str, provider: str) -> str:
        """The provider's attribute condition, verbatim ("" when none)."""
        ...

    def set_wif_provider_owners(
        self, project: str, pool: str, provider: str, owners: list[str]
    ) -> None:
        """Rewrite the condition to admit exactly these GitHub owners."""
        ...

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
    def get_gke_credentials(self, project: str, location: str, cluster: str, access: str = "dns") -> str:
        """Populate local kubeconfig for the cluster; return the context name.

        `access` is the cluster's (models.CLUSTER_ACCESS): "dns" reaches the
        IAM-authenticated DNS endpoint, "internal" the private endpoint IP."""
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
    def remove_sa_binding(self, project: str, sa_email: str, member: str, role: str) -> bool:
        """Remove a binding on a service account's own policy (a Workload
        Identity user, say). False if it was already gone."""
        ...

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
        # list, not describe. For a service account deleted in the recent
        # past describe answers PERMISSION_DENIED "(or it may not exist)" —
        # to the project's owner as well — where a name never used answers
        # NOT_FOUND. An app removed and connected again could not get its
        # identity back (cinder, 2026-07-27; the dogfood reinstall,
        # 2026-09-29). list simply leaves the deleted account out.
        out = self._exec(
            "iam", "service-accounts", "list", f"--project={project}",
            f"--filter=email={email}", "--format=value(email)",
        )
        return email in out.split()

    def create_service_account(self, project: str, account_id: str, display_name: str) -> str:
        self._exec(
            "iam", "service-accounts", "create", account_id,
            f"--display-name={display_name}", f"--project={project}",
        )
        return f"{account_id}@{project}.iam.gserviceaccount.com"

    # --- workload identity federation ----------------------------------------

    def _wif_state(self, *describe: str) -> str | None:
        """ACTIVE, DELETED, or None for never-existed. A deleted pool or
        provider is kept for 30 days: describe still answers (state DELETED)
        and create answers ALREADY_EXISTS, so "describe succeeded" is not
        "exists" — seen on the first reinstall after an uninstall --purge
        (2026-09-29). Within those 30 days the way back is undelete."""
        result = self._run([self._gcloud, *describe, "--format=value(state)"])
        if result.returncode == 0:
            return result.stdout.strip() or "ACTIVE"
        if "NOT_FOUND" in result.stderr or "not found" in result.stderr.lower():
            return None
        raise GCPError(f"gcloud {' '.join(describe)} failed: {result.stderr}")

    def _pool_args(self, verb: str, project: str, pool: str) -> tuple[str, ...]:
        return (
            "iam", "workload-identity-pools", verb, pool,
            "--location=global", f"--project={project}",
        )

    def _provider_args(self, verb: str, project: str, pool: str, provider: str) -> tuple[str, ...]:
        return (
            "iam", "workload-identity-pools", "providers", verb, provider,
            f"--workload-identity-pool={pool}", "--location=global", f"--project={project}",
        )

    def wif_pool_exists(self, project: str, pool: str) -> bool:
        return self._wif_state(*self._pool_args("describe", project, pool)) == "ACTIVE"

    def create_wif_pool(self, project: str, pool: str, display_name: str) -> None:
        if self._wif_state(*self._pool_args("describe", project, pool)) == "DELETED":
            self._exec(*self._pool_args("undelete", project, pool))
            return
        self._exec(
            *self._pool_args("create", project, pool), f"--display-name={display_name}"
        )

    def wif_provider_exists(self, project: str, pool: str, provider: str) -> bool:
        state = self._wif_state(*self._provider_args("describe", project, pool, provider))
        return state == "ACTIVE"

    def wif_provider_condition(self, project: str, pool: str, provider: str) -> str:
        return self._exec(
            *self._provider_args("describe", project, pool, provider),
            "--format=value(attributeCondition)",
        ).strip()

    def set_wif_provider_owners(
        self, project: str, pool: str, provider: str, owners: list[str]
    ) -> None:
        self._exec(
            *self._provider_args("update-oidc", project, pool, provider),
            f"--attribute-condition={owner_condition(owners)}",
        )

    def create_wif_provider(
        self, project: str, pool: str, provider: str, allowed_owner: str
    ) -> None:
        describe = self._provider_args("describe", project, pool, provider)
        if self._wif_state(*describe) == "DELETED":
            self._exec(*self._provider_args("undelete", project, pool, provider))
            return
        # The attribute-condition restricts token issuance to one GitHub owner —
        # defense in depth on top of the per-repo principalSet binding. v0.1
        # assumes one GitHub owner/org per Gojira install.
        self._exec(
            "iam", "workload-identity-pools", "providers", "create-oidc", provider,
            f"--workload-identity-pool={pool}", "--location=global",
            f"--issuer-uri={GITHUB_OIDC_ISSUER}",
            f"--attribute-mapping={WIF_ATTRIBUTE_MAPPING}",
            f"--attribute-condition={owner_condition([allowed_owner])}",
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
    def get_gke_credentials(self, project: str, location: str, cluster: str, access: str = "dns") -> str:
        # "dns": the pipeline has already ensured DNS access (ensure_cluster_dns_access);
        # the default endpoint form writes a private IP that is unreachable off-VPC
        # and silently clobbers the shared kubeconfig. "internal": that private IP
        # on purpose, from a Gojira that runs inside the cluster's network.
        endpoint = "--internal-ip" if access == "internal" else "--dns-endpoint"
        self._exec(
            "container", "clusters", "get-credentials", cluster,
            f"--location={location}", f"--project={project}", endpoint,
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

    def remove_sa_binding(self, project: str, sa_email: str, member: str, role: str) -> bool:
        return self._exists(
            "iam", "service-accounts", "remove-iam-policy-binding", sa_email,
            f"--project={project}", f"--member={member}", f"--role={role}", "--quiet",
        )

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
        """A multi-arch image is an index plus one manifest per platform, and
        the registry refuses to delete a manifest an index still lists
        ("manifest is referenced by parent manifests"). `image:` copies whole
        indexes, so list order decided whether an app's teardown succeeded
        (2026-10-22: litellm went, cabal/liminal/kobo failed). Tagged digests,
        the indexes deploys pinned, go first; a manifest whose parent was an
        untagged index (a tag that later moved) is retried once after."""
        image = f"{ar_repo}/{name}"
        out = self._list_or_empty(
            "artifacts", "docker", "images", "list", image,
            "--include-tags", "--format=value(DIGEST,TAGS)",
        )
        rows = [line.split("\t") for line in out.splitlines() if line.strip()]
        pending = [r[0].strip() for r in sorted(rows, key=lambda r: not (len(r) > 1 and r[1].strip()))]
        deleted = 0
        for last_pass in (False, True):
            refused = []
            for digest in pending:
                try:
                    self._exec(
                        "artifacts", "docker", "images", "delete", f"{image}@{digest}",
                        "--delete-tags", "--quiet",
                    )
                    deleted += 1
                except GCPError as exc:
                    if last_pass or "referenced by parent" not in str(exc):
                        raise
                    refused.append(digest)
            pending = refused
            if not pending:
                break
        return deleted


def _policy_has(policy_json: str, member: str, role: str) -> bool:
    import json

    policy = json.loads(policy_json) if policy_json else {}
    for binding in policy.get("bindings", []):
        if binding.get("role") == role and member in binding.get("members", []):
            return True
    return False
