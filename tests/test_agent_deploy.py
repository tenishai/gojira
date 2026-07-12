"""Agent deploy: Gojira's first in-cluster workload of its own (Track C).

One call = mint token, apply manifests, grant metrics access, first sweep.
Redeploy rotates. The manifests must round-trip as the agent repo expects:
Secret keys cluster_name/token/prometheus_url/prometheus_auth, CronJob env
CPU_QUERY/MEM_QUERY/GOJIRA_URL.
"""

import hashlib

import pytest
import yaml
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gojira.config import SizingConfig
from gojira.models import AgentToken, Base, Cluster
from gojira.sizing.agent_deploy import AgentDeployError, deploy_agent
from tests.fakes import FakeGCPClient, FakeKubectlClient


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def cluster(session):
    c = Cluster(name="sandbox", gcp_project="my-project", location="us-central1",
                gke_cluster="sandbox-gke", ar_repo="r")
    session.add(c)
    session.commit()
    return c


def sizing(**over) -> SizingConfig:
    return SizingConfig(agent_image="us-docker.pkg.dev/p/r/agent:dev", **over)


def objects(kubectl) -> dict[str, dict]:
    """kind → object across the applied manifest streams of the last deploy
    (RBAC and workload are applied separately — see finding #11 ordering)."""
    docs = [d for m in kubectl.applied[-2:] for d in yaml.safe_load_all(m)]
    return {d["kind"]: d for d in docs}


def test_deploy_applies_the_agent_and_starts_a_sweep(session, cluster):
    gcp, kubectl = FakeGCPClient(), FakeKubectlClient()
    msg = deploy_agent(session, cluster, gcp=gcp, kubectl=kubectl, sizing=sizing())

    objs = objects(kubectl)
    assert set(objs) == {"ServiceAccount", "ClusterRole", "ClusterRoleBinding",
                         "Secret", "CronJob"}

    # the Secret carries the freshly minted token — hash must match the DB row
    secret = objs["Secret"]["stringData"]
    row = session.scalar(select(AgentToken).where(AgentToken.cluster_id == cluster.id))
    assert hashlib.sha256(secret["token"].encode()).hexdigest() == row.token_sha256
    assert secret["cluster_name"] == "sandbox"
    # GMP for the cluster's own project, by default
    assert secret["prometheus_url"] == (
        "https://monitoring.googleapis.com/v1/projects/my-project/location/global/prometheus"
    )
    assert secret["prometheus_auth"] == "google"

    container = objs["CronJob"]["spec"]["jobTemplate"]["spec"]["template"]["spec"][
        "containers"][0]
    assert container["image"] == "us-docker.pkg.dev/p/r/agent:dev"
    assert container["imagePullPolicy"] == "Always"  # mutable tags must re-pull
    env = {e["name"]: e for e in container["env"]}
    assert "{{" in env["CPU_QUERY"]["value"]  # braces stay doubled for str.format
    assert env["GOJIRA_TOKEN"]["valueFrom"]["secretKeyRef"]["key"] == "token"

    # metrics access granted to the KSA's WIF principal, and a sweep kicked off
    member = ("principal://iam.googleapis.com/projects/773958892580/locations/global/"
              "workloadIdentityPools/my-project.svc.id.goog/subject/ns/gojira/sa/"
              "gojira-sizing-agent")
    assert gcp.project_has_binding("my-project", member, "roles/monitoring.viewer")
    assert "job:gojira/gojira-sizing-agent-first-sweep<-gojira-sizing-agent" in kubectl.calls
    assert "sandbox" in msg


def test_redeploy_rotates_the_token_and_skips_the_grant(session, cluster):
    gcp, kubectl = FakeGCPClient(), FakeKubectlClient()
    deploy_agent(session, cluster, gcp=gcp, kubectl=kubectl, sizing=sizing())
    first_hash = session.scalar(select(AgentToken)).token_sha256
    grants = len([c for c in gcp.calls if c.startswith("proj_bind")])

    deploy_agent(session, cluster, gcp=gcp, kubectl=kubectl, sizing=sizing())
    rows = session.scalars(select(AgentToken)).all()
    assert len(rows) == 1 and rows[0].token_sha256 != first_hash
    # binding already present — no second IAM write
    assert len([c for c in gcp.calls if c.startswith("proj_bind")]) == grants
    # the new secret carries the new token
    assert hashlib.sha256(
        objects(kubectl)["Secret"]["stringData"]["token"].encode()
    ).hexdigest() == rows[0].token_sha256


def test_operator_prometheus_url_wins_over_gmp_default(session, cluster):
    kubectl = FakeKubectlClient()
    deploy_agent(session, cluster, gcp=FakeGCPClient(), kubectl=kubectl,
                 sizing=sizing(agent_prometheus_url="http://prom:9090",
                               agent_prometheus_auth="none"))
    secret = objects(kubectl)["Secret"]["stringData"]
    assert secret["prometheus_url"] == "http://prom:9090"
    assert secret["prometheus_auth"] == "none"


def test_forbidden_cluster_write_never_rotates_a_working_token(session, cluster):
    """Finding #11: RBAC apply is the permission probe. If the platform can't
    write to the cluster, the incumbent agent must keep its valid token."""
    from gojira.k8s import K8sError

    class ForbiddenKubectl(FakeKubectlClient):
        def apply(self, manifest):
            raise K8sError("clusterroles.rbac.authorization.k8s.io is forbidden")

    gcp, kubectl = FakeGCPClient(), FakeKubectlClient()
    deploy_agent(session, cluster, gcp=gcp, kubectl=kubectl, sizing=sizing())
    incumbent = session.scalar(select(AgentToken)).token_sha256

    with pytest.raises(K8sError, match="forbidden"):
        deploy_agent(session, cluster, gcp=gcp, kubectl=ForbiddenKubectl(), sizing=sizing())
    assert session.scalar(select(AgentToken)).token_sha256 == incumbent


def test_missing_image_refuses_before_touching_anything(session, cluster):
    gcp, kubectl = FakeGCPClient(), FakeKubectlClient()
    with pytest.raises(AgentDeployError, match="agent_image"):
        deploy_agent(session, cluster, gcp=gcp, kubectl=kubectl, sizing=SizingConfig())
    assert not kubectl.calls and not gcp.calls
    assert session.scalar(select(AgentToken)) is None


# --- the clusters-page button ---------------------------------------------------


@pytest.fixture
def web(tmp_path):
    """TestClient + its fakes, with a registered cluster and agent_image set."""
    from fastapi.testclient import TestClient

    from gojira import service
    from gojira.config import Config
    from gojira.web import create_app
    from tests.fakes import FakeGitHubClient, KubectlFactory

    engine = create_engine(
        f"sqlite:///{tmp_path}/gojira.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as s:
        s.add(Cluster(name="sandbox", gcp_project="my-project", location="l",
                      gke_cluster="g", ar_repo="r"))
        s.commit()
    kubectl_for = KubectlFactory()
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=kubectl_for
    )
    config = Config()
    config.sizing.agent_image = "us-docker.pkg.dev/p/r/agent:dev"
    return TestClient(create_app(session_factory, clients=clients, config=config)), kubectl_for


def test_deploy_button_deploys_and_page_shows_token(web):
    client, kubectl_for = web
    page = client.get("/clusters").text
    assert "Deploy agent" in page and "token minted" not in page

    r = client.post("/clusters/1/deploy-agent", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/clusters"
    assert kubectl_for.by_cluster["sandbox"].applied  # manifests reached the cluster

    page = client.get("/clusters").text
    assert "token minted" in page and "rotate token" in page


def test_deploy_button_surfaces_failure(web):
    from urllib.parse import unquote

    client, _ = web
    r = client.post("/clusters/99/deploy-agent", follow_redirects=False)
    assert "error=no such cluster" in unquote(r.headers["location"])
