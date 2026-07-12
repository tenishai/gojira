"""Deploy the sizing agent into a registered cluster — Track C's first step.

This is Gojira's first in-cluster workload deploy of its own: everything the
platform previously asked the operator to do by hand (mint a token via
kubectl exec, apply RBAC, build a Secret, wire the CronJob, grant the agent
metrics access) becomes one idempotent call behind a button.

Redeploy is rotation: every run mints a fresh token and re-applies the
Secret in the same motion, so the old token dies the moment the new one
lands. The manifests mirror tenishai/gojira-sizing-agent's deploy/ — the
agent repo documents the manual path, this module is the platform path.
"""

import logging

import yaml
from sqlalchemy.orm import Session

from gojira.config import SizingConfig
from gojira.gcp import GCPClient
from gojira.k8s import KubectlClient
from gojira.models import Cluster
from gojira.sizing.service import mint_agent_token

log = logging.getLogger(__name__)

AGENT_NAMESPACE = "gojira"  # the platform's system namespace on any target
AGENT_NAME = "gojira-sizing-agent"
MONITORING_ROLE = "roles/monitoring.viewer"


class AgentDeployError(RuntimeError):
    """The deploy can't proceed; the message is operator-facing."""


def _gmp_url(project: str) -> str:
    return f"https://monitoring.googleapis.com/v1/projects/{project}/location/global/prometheus"


def _wif_member(project: str, project_number: str) -> str:
    """The agent KSA's Workload Identity principal — grantable directly,
    no Google service account in between."""
    return (
        f"principal://iam.googleapis.com/projects/{project_number}"
        f"/locations/global/workloadIdentityPools/{project}.svc.id.goog"
        f"/subject/ns/{AGENT_NAMESPACE}/sa/{AGENT_NAME}"
    )


def render_rbac_manifests() -> list[dict]:
    """Identity + read grants — everything that needs no token. Applied first:
    it doubles as the permission probe, so a Forbidden here aborts the deploy
    before the old token has been rotated away (finding #11)."""
    meta = {"name": AGENT_NAME, "namespace": AGENT_NAMESPACE}
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": dict(meta)},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": f"{AGENT_NAME}-read"},
            "rules": [
                {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list"]},
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": f"{AGENT_NAME}-read"},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": f"{AGENT_NAME}-read",
            },
            "subjects": [
                {"kind": "ServiceAccount", "name": AGENT_NAME, "namespace": AGENT_NAMESPACE}
            ],
        },
    ]


def render_workload_manifests(cluster: Cluster, token: str, sizing: SizingConfig) -> list[dict]:
    """The Secret (carrying the fresh token) and the CronJob that consumes it."""
    meta = {"name": AGENT_NAME, "namespace": AGENT_NAMESPACE}
    prometheus_url = sizing.agent_prometheus_url or _gmp_url(cluster.gcp_project)
    return [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": dict(meta),
            "stringData": {
                "cluster_name": cluster.name,
                "token": token,
                "prometheus_url": prometheus_url,
                "prometheus_auth": sizing.agent_prometheus_auth,
            },
        },
        {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": dict(meta),
            "spec": {
                "schedule": "0 */6 * * *",  # evidence windows are days long
                "concurrencyPolicy": "Forbid",
                "jobTemplate": {
                    "spec": {
                        "backoffLimit": 1,
                        "template": {
                            "spec": {
                                "serviceAccountName": AGENT_NAME,
                                "restartPolicy": "Never",
                                "enableServiceLinks": False,
                                "containers": [
                                    {
                                        "name": "agent",
                                        "image": sizing.agent_image,
                                        # mutable tags (:dev) must re-pull, or a
                                        # retag silently runs stale code
                                        "imagePullPolicy": "Always",
                                        "env": [
                                            {"name": "GOJIRA_URL", "value": sizing.agent_gojira_url},
                                            {"name": "CPU_QUERY", "value": sizing.agent_cpu_query},
                                            {"name": "MEM_QUERY", "value": sizing.agent_mem_query},
                                            *(
                                                {
                                                    "name": var,
                                                    "valueFrom": {
                                                        "secretKeyRef": {
                                                            "name": AGENT_NAME,
                                                            "key": key,
                                                        }
                                                    },
                                                }
                                                for var, key in [
                                                    ("CLUSTER_NAME", "cluster_name"),
                                                    ("GOJIRA_TOKEN", "token"),
                                                    ("PROMETHEUS_URL", "prometheus_url"),
                                                    ("PROMETHEUS_AUTH", "prometheus_auth"),
                                                ]
                                            ),
                                        ],
                                        "resources": {
                                            "requests": {
                                                "cpu": "100m",
                                                "memory": "128Mi",
                                                "ephemeral-storage": "1Gi",
                                            },
                                            "limits": {
                                                "cpu": "250m",
                                                "memory": "256Mi",
                                                "ephemeral-storage": "1Gi",
                                            },
                                        },
                                    }
                                ],
                            }
                        },
                    }
                },
            },
        },
    ]


def deploy_agent(
    session: Session,
    cluster: Cluster,
    *,
    gcp: GCPClient,
    kubectl: KubectlClient,
    sizing: SizingConfig,
) -> str:
    """Deploy (or redeploy) the sizing agent to a cluster. Returns an
    operator-facing summary. Idempotent; every run rotates the token."""
    if not sizing.agent_image:
        raise AgentDeployError(
            "sizing.agent_image is not configured — set it to the sizing-agent "
            "image in your registry (see the gojira-sizing-agent repo)"
        )

    # RBAC first, token after: the first apply is also the permission probe
    # (cluster-scoped RBAC needs container.admin — finding #11). If it fails,
    # a previously working agent keeps its still-valid token.
    kubectl.ensure_namespace(AGENT_NAMESPACE, {"app.kubernetes.io/managed-by": "gojira"})
    kubectl.apply(yaml.safe_dump_all(render_rbac_manifests(), sort_keys=False))

    token = mint_agent_token(session, cluster)
    kubectl.apply(yaml.safe_dump_all(render_workload_manifests(cluster, token, sizing),
                                     sort_keys=False))

    member = _wif_member(cluster.gcp_project, gcp.project_number(cluster.gcp_project))
    if not gcp.project_has_binding(cluster.gcp_project, member, MONITORING_ROLE):
        gcp.add_project_binding(cluster.gcp_project, member, MONITORING_ROLE)
        log.info("granted %s to agent principal on %s", MONITORING_ROLE, cluster.gcp_project)

    # A first sweep now, so the operator sees evidence in about a minute
    # instead of waiting for the next schedule tick.
    kubectl.create_job_from_cronjob(AGENT_NAMESPACE, f"{AGENT_NAME}-first-sweep", AGENT_NAME)

    log.info("sizing agent deployed to cluster %s", cluster.name)
    return f"sizing agent deployed to {cluster.name}; token rotated; first sweep started"
