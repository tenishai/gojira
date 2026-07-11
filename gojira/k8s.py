"""Kubernetes operations behind an interface.

Same pattern as GCPClient: steps depend on the KubectlClient ABC, the
v0.1 backend shells out to `kubectl`. The client is bound to a kubeconfig
context (the one get-gke-credentials creates), so a single Gojira install
can address many clusters by constructing one client per context.
"""

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path

import yaml

from gojira.shell import CommandRunner

log = logging.getLogger(__name__)


class K8sError(RuntimeError):
    """A kubectl command failed for a reason other than 'not found'."""


def kustomize_build(
    overlay_dir: Path, runner: CommandRunner | None = None, kubectl: str = "kubectl"
) -> list[dict]:
    """Render an overlay to its desired objects via `kubectl kustomize`.

    Uses kubectl's built-in kustomize — no separate binary. This is the git
    side of drift: what the manifests in the repo *say* should be running. Pure
    local build, not bound to any cluster context.
    """
    runner = runner or CommandRunner()
    result = runner.run([kubectl, "kustomize", str(overlay_dir)])
    if result.returncode != 0:
        raise K8sError(f"kubectl kustomize {overlay_dir} failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


class KubectlClient(ABC):
    """The Kubernetes operations the provisioner needs. Implementations are idempotent."""

    @abstractmethod
    def namespace_exists(self, namespace: str) -> bool: ...

    @abstractmethod
    def ensure_namespace(self, namespace: str, labels: dict[str, str]) -> None: ...

    @abstractmethod
    def delete_namespace(self, namespace: str) -> None: ...

    @abstractmethod
    def get_resource(self, namespace: str, kind: str, name: str) -> dict | None:
        """The live object as a parsed dict, or None if it doesn't exist.

        Used by the drift detector to read what's actually running. Read-only.
        """

    @abstractmethod
    def apply(self, manifest: str) -> None:
        """Apply a YAML manifest (may hold multiple documents). Idempotent."""

    @abstractmethod
    def create_job_from_cronjob(self, namespace: str, name: str, cronjob: str) -> None:
        """Run one Job off a CronJob's template now. Jobs are immutable, so a
        previous run of the same name is deleted first."""


class KubectlCLI(KubectlClient):
    """kubectl-backed client, pinned to one kubeconfig context."""

    def __init__(self, context: str, runner: CommandRunner | None = None, kubectl: str = "kubectl"):
        self._context = context
        self._runner = runner or CommandRunner()
        self._kubectl = kubectl

    def _run(self, *args: str, stdin: str | None = None):
        full = [self._kubectl, f"--context={self._context}", *args]
        return self._runner.run(full, stdin=stdin)

    def namespace_exists(self, namespace: str) -> bool:
        result = self._run("get", "namespace", namespace, "-o", "name")
        if result.returncode == 0:
            return True
        if "NotFound" in result.stderr or "not found" in result.stderr.lower():
            return False
        raise K8sError(f"kubectl get namespace {namespace} failed: {result.stderr}")

    def ensure_namespace(self, namespace: str, labels: dict[str, str]) -> None:
        # Apply a manifest via stdin — idempotent, and carries labels in one shot.
        label_lines = "\n".join(f"    {k}: {repr(v)}" for k, v in labels.items())
        manifest = (
            "apiVersion: v1\n"
            "kind: Namespace\n"
            "metadata:\n"
            f"  name: {namespace}\n"
            "  labels:\n"
            f"{label_lines}\n"
        )
        result = self._run("apply", "-f", "-", stdin=manifest)
        if result.returncode != 0:
            raise K8sError(f"kubectl apply namespace {namespace} failed: {result.stderr}")

    def delete_namespace(self, namespace: str) -> None:
        # --wait=false: return immediately; termination runs async on the cluster.
        result = self._run("delete", "namespace", namespace, "--ignore-not-found", "--wait=false")
        if result.returncode != 0:
            raise K8sError(f"kubectl delete namespace {namespace} failed: {result.stderr}")

    def get_resource(self, namespace: str, kind: str, name: str) -> dict | None:
        result = self._run("get", kind, name, "-n", namespace, "-o", "json")
        if result.returncode == 0:
            return json.loads(result.stdout)
        if "NotFound" in result.stderr or "not found" in result.stderr.lower():
            return None
        raise K8sError(f"kubectl get {kind} {name} -n {namespace} failed: {result.stderr}")

    def apply(self, manifest: str) -> None:
        # stdin, not argv: manifests can carry secrets, which must never
        # appear in a process listing.
        result = self._run("apply", "-f", "-", stdin=manifest)
        if result.returncode != 0:
            raise K8sError(f"kubectl apply failed: {result.stderr}")

    def create_job_from_cronjob(self, namespace: str, name: str, cronjob: str) -> None:
        result = self._run("delete", "job", name, "-n", namespace, "--ignore-not-found")
        if result.returncode != 0:
            raise K8sError(f"kubectl delete job {name} failed: {result.stderr}")
        result = self._run(
            "create", "job", name, "-n", namespace, f"--from=cronjob/{cronjob}"
        )
        if result.returncode != 0:
            raise K8sError(f"kubectl create job {name} failed: {result.stderr}")
