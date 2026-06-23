"""Drift scan: detection upserts a finding and authors a capture PR on drift."""

import yaml
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from gojira.db import init_db, make_session_factory
from gojira.drift_scan import scan_env
from gojira.models import (
    App,
    AppStatus,
    Cluster,
    DriftFinding,
    DriftStatus,
    Environment,
)
from tests.fakes import FakeGitHubClient, FakeKubectlClient


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    init_db(engine)
    return make_session_factory(engine)()


def _seed(session, overlay_dir):
    cluster = Cluster(
        name="dev-cluster", gcp_project="example-project", location="us-central1",
        gke_cluster="dev", ar_repo="us-central1-docker.pkg.dev/example-project/apps",
    )
    session.add(cluster)
    session.commit()
    app = App(
        name="clips-api", repo_full_name="acme/clips-api", installation_id=1,
        workload_class="web", status=AppStatus.ACTIVE,
    )
    session.add(app)
    session.commit()
    env = Environment(app_id=app.id, name="dev", cluster_id=cluster.id, namespace="clips")
    session.add(env)
    session.commit()
    # A real overlay kustomization so the capture-PR wiring has something to edit.
    overlay_dir.mkdir(parents=True, exist_ok=True)
    (overlay_dir / "kustomization.yaml").write_text(
        yaml.safe_dump({"resources": ["../../base"], "namespace": "clips"})
    )
    return app, env


def _desired_deployment(cpu="250m"):
    return {
        "kind": "Deployment",
        "metadata": {"name": "clips-api"},
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [
                {"name": "app", "image": "gojira-app",
                 "resources": {"requests": {"cpu": cpu, "memory": "256Mi"}}}
            ]}},
        },
    }


def _live_deployment(cpu="250m"):
    return {
        "kind": "Deployment",
        "metadata": {"name": "clips-api", "resourceVersion": "5"},
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [
                {"name": "app", "image": "reg/clips-api:v3", "imagePullPolicy": "Always",
                 "resources": {"requests": {"cpu": cpu, "memory": "256Mi"}}}
            ]}},
        },
        "status": {"readyReplicas": 1},
    }


def test_no_drift_records_resolved_and_no_pr(tmp_path):
    session = _session()
    app, env = _seed(session, tmp_path / "k8s" / "overlays" / "dev")
    github = FakeGitHubClient()
    kubectl = FakeKubectlClient(resources={("clips", "deployment", "clips-api"): _live_deployment()})

    finding = scan_env(session, app, env, github, kubectl, tmp_path,
                       render_desired=lambda _overlay: [_desired_deployment()])

    assert finding.status == DriftStatus.RESOLVED
    assert finding.pr_url is None
    assert not any(c.startswith("open_pr") for c in github.calls)


def test_sre_patch_opens_capture_pr(tmp_path):
    session = _session()
    overlay = tmp_path / "k8s" / "overlays" / "dev"
    app, env = _seed(session, overlay)
    github = FakeGitHubClient()
    # Live cpu was patched to 800m by an operator; git still says 250m.
    kubectl = FakeKubectlClient(resources={("clips", "deployment", "clips-api"): _live_deployment(cpu="800m")})

    finding = scan_env(session, app, env, github, kubectl, tmp_path,
                       render_desired=lambda _overlay: [_desired_deployment(cpu="250m")])

    assert finding.status == DriftStatus.PR_OPENED
    assert finding.pr_url == "https://github.com/acme/clips-api/pull/1"
    assert github.branch == "gojira/drift-dev"
    # The capture patch carries the live value and is wired into the overlay.
    patch = yaml.safe_load((overlay / "drift-clips-api.yaml").read_text())
    container = patch["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"]["cpu"] == "800m"
    kust = yaml.safe_load((overlay / "kustomization.yaml").read_text())
    assert {"path": "drift-clips-api.yaml"} in kust["patches"]


def test_existing_pr_is_not_reauthored(tmp_path):
    session = _session()
    overlay = tmp_path / "k8s" / "overlays" / "dev"
    app, env = _seed(session, overlay)
    github = FakeGitHubClient()
    github.prs["gojira/drift-dev"] = "https://github.com/acme/clips-api/pull/7"
    kubectl = FakeKubectlClient(resources={("clips", "deployment", "clips-api"): _live_deployment(cpu="800m")})

    finding = scan_env(session, app, env, github, kubectl, tmp_path,
                       render_desired=lambda _overlay: [_desired_deployment(cpu="250m")])

    assert finding.status == DriftStatus.PR_OPENED
    assert finding.pr_url == "https://github.com/acme/clips-api/pull/7"
    assert not any(c.startswith("open_pr") for c in github.calls)  # reused, not re-opened


def test_resolved_clears_prior_finding(tmp_path):
    session = _session()
    overlay = tmp_path / "k8s" / "overlays" / "dev"
    app, env = _seed(session, overlay)
    # Pre-existing open finding from a prior scan.
    session.add(DriftFinding(app_id=app.id, environment="dev", status=DriftStatus.OPEN,
                             diff_json="[...]"))
    session.commit()
    github = FakeGitHubClient()
    kubectl = FakeKubectlClient(resources={("clips", "deployment", "clips-api"): _live_deployment()})

    finding = scan_env(session, app, env, github, kubectl, tmp_path,
                       render_desired=lambda _overlay: [_desired_deployment()])

    assert finding.status == DriftStatus.RESOLVED
    assert finding.diff_json is None
