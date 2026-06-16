"""Kubernetes operations behind an interface.

Same pattern as GCPClient: steps depend on the KubectlClient ABC, the
v0.1 backend shells out to `kubectl`. The client is bound to a kubeconfig
context (the one get-gke-credentials creates), so a single Gojira install
can address many clusters by constructing one client per context.
"""

import logging
from abc import ABC, abstractmethod

from gojira.shell import CommandRunner

log = logging.getLogger(__name__)


class K8sError(RuntimeError):
    """A kubectl command failed for a reason other than 'not found'."""


class KubectlClient(ABC):
    """The Kubernetes operations the provisioner needs. Implementations are idempotent."""

    @abstractmethod
    def namespace_exists(self, namespace: str) -> bool: ...

    @abstractmethod
    def ensure_namespace(self, namespace: str, labels: dict[str, str]) -> None: ...

    @abstractmethod
    def delete_namespace(self, namespace: str) -> None: ...


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
