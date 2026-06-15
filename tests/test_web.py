"""Web smoke tests: routes render and the connect→provision→watch flow works
end to end against in-memory fakes (no gcloud/git/kubectl)."""

import time

import pytest
from fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gojira import service
from gojira.models import Base
from gojira.web import create_app


@pytest.fixture
def session_factory(tmp_path):
    # File-based SQLite: separate connection per thread, shared data — so the
    # background provision thread and the polling request don't fight over one
    # connection (which StaticPool would force). Matches the real run config.
    engine = create_engine(
        f"sqlite:///{tmp_path}/gojira.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def clients():
    return service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory()
    )


@pytest.fixture
def client(session_factory, clients):
    return TestClient(create_app(session_factory, clients=clients))


def _register_three(session_factory):
    with session_factory() as s:
        for name, project, gke in [
            ("dev", "proj-a", "dev-gke"),
            ("qa", "proj-a", "qa-gke"),
            ("prod", "proj-b", "prod-gke"),
        ]:
            service.register_cluster(
                s, name=name, gcp_project=project, location="us-central1", gke_cluster=gke
            )


def test_dashboard_empty(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "No apps yet" in r.text


def test_register_cluster_via_form(client):
    r = client.post(
        "/clusters",
        data={
            "name": "dev", "gcp_project": "proj-a", "location": "us-central1",
            "gke_cluster": "dev-gke", "ar_repo": "",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "dev-gke" in r.text
    assert "us-central1-docker.pkg.dev/proj-a/apps" in r.text  # AR defaulted


def test_connect_requires_cluster(client):
    assert "Register at least one" in client.get("/connect").text


def test_connect_lists_repos_from_github(client, session_factory):
    _register_three(session_factory)
    html = client.get("/connect").text
    assert 'name="repo_select"' in html
    assert "shaz/gojira-demo" in html  # from the fake's repo list


def test_connect_provisions_and_status_reaches_terminal(client, session_factory):
    _register_three(session_factory)
    with session_factory() as s:
        ids = {c.name: c.id for c in service.list_clusters(s)}

    r = client.post(
        "/connect",
        data={
            "name": "clips-api",
            "repo_select": "acme/clips-api",
            "workload_class": "web",
            "cluster_dev": ids["dev"], "namespace_dev": "clips",
            "cluster_qa": ids["qa"], "namespace_qa": "clips",
            "cluster_prod": ids["prod"], "namespace_prod": "clips",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    app_path = r.headers["location"]  # /apps/{id}
    app_id = int(app_path.rsplit("/", 1)[-1])

    # background provision finishes fast against fakes — poll the status partial
    deadline = time.time() + 10
    text = ""
    while time.time() < deadline:
        text = client.get(f"/apps/{app_id}/status").text
        if "awaiting_pr" in text or "b-failed" in text:
            break
        time.sleep(0.1)
    assert "awaiting_pr" in text
    assert "Setup PR" in text

    detail = client.get(app_path)
    assert detail.status_code == 200
    assert "clips-api" in detail.text
    assert "ensure_service_account" in detail.text  # scoped step rendered
    assert "@proj-a" in detail.text or "@proj-b" in detail.text


def test_connect_existing_repo_redirects_to_app(client, session_factory):
    _register_three(session_factory)
    with session_factory() as s:
        dev_id = next(c.id for c in service.list_clusters(s) if c.name == "dev")
        existing = service.connect_app(
            s, name="clips-api", repo_full_name="acme/clips-api", workload_class="web",
            env_map={"dev": (dev_id, "clips")},
        )
        existing_id = existing.id

    r = client.post(
        "/connect",
        data={
            "name": "dupe", "repo_select": "acme/clips-api", "workload_class": "web",
            "cluster_dev": dev_id, "namespace_dev": "clips",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/apps/{existing_id}"  # no 500, no duplicate


def test_connect_blocks_repo_without_dockerfile(session_factory):
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(),
        github=FakeGitHubClient(dockerfile=False),
        kubectl_for=KubectlFactory(),
    )
    client = TestClient(create_app(session_factory, clients=clients))
    _register_three(session_factory)
    with session_factory() as s:
        dev_id = next(c.id for c in service.list_clusters(s) if c.name == "dev")

    r = client.post(
        "/connect",
        data={
            "name": "nodock", "repo_select": "acme/web", "workload_class": "web",
            "cluster_dev": dev_id, "namespace_dev": "x",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/connect?error=")
    assert "Dockerfile" in r.headers["location"]
    with session_factory() as s:
        assert service.app_by_repo(s, "acme/web") is None  # not connected


def test_sync_activates_when_setup_pr_merged(session_factory):
    gh = FakeGitHubClient(pr_merged=False)
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=gh, kubectl_for=KubectlFactory()
    )
    client = TestClient(create_app(session_factory, clients=clients))
    _register_three(session_factory)
    with session_factory() as s:
        dev_id = next(c.id for c in service.list_clusters(s) if c.name == "dev")
    app_id = int(
        client.post(
            "/connect",
            data={"name": "hello", "repo_select": "acme/web", "workload_class": "web",
                  "cluster_dev": dev_id, "namespace_dev": "hello"},
            follow_redirects=False,
        ).headers["location"].rsplit("/", 1)[-1]
    )

    # wait for the background provision to reach awaiting_pr
    deadline = time.time() + 10
    while time.time() < deadline:
        with session_factory() as s:
            if service.app_status(s, app_id).app.status == "awaiting_pr":
                break
        time.sleep(0.1)

    # PR not merged yet → sync is a no-op, still awaiting
    client.post(f"/apps/{app_id}/sync", follow_redirects=False)
    with session_factory() as s:
        assert service.app_status(s, app_id).app.status == "awaiting_pr"

    # operator merges the PR → sync flips to active
    gh.pr_merged = True
    client.post(f"/apps/{app_id}/sync", follow_redirects=False)
    with session_factory() as s:
        assert service.app_status(s, app_id).app.status == "active"


def test_connect_blank_namespace_defaults_to_app_name(client, session_factory):
    _register_three(session_factory)
    with session_factory() as s:
        dev_id = next(c.id for c in service.list_clusters(s) if c.name == "dev")

    r = client.post(
        "/connect",
        data={
            "name": "myapp", "repo_select": "acme/web", "workload_class": "web",
            "cluster_dev": dev_id, "namespace_dev": "",  # blank → defaults to app name
        },
        follow_redirects=False,
    )
    app_id = int(r.headers["location"].rsplit("/", 1)[-1])
    with session_factory() as s:
        view = service.app_status(s, app_id)
    assert [(e.name, e.namespace) for e in view.environments] == [("dev", "myapp")]


def test_delete_app_removes_it_from_dashboard(client, clients, session_factory):
    _register_three(session_factory)
    with session_factory() as s:
        dev_id = next(c.id for c in service.list_clusters(s) if c.name == "dev")
        app = service.connect_app(
            s, name="goner", repo_full_name="acme/goner", workload_class="web",
            env_map={"dev": (dev_id, "goner")},
        )
        app_id = app.id

    r = client.post(
        f"/apps/{app_id}/delete", data={"delete_namespaces": "1"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/"
    with session_factory() as s:
        assert service.app_by_repo(s, "acme/goner") is None
    assert "delete_ns:goner" in clients.kubectl_for.by_cluster["dev"].calls


def test_connect_skips_unmapped_env(client, session_factory):
    _register_three(session_factory)
    with session_factory() as s:
        dev_id = next(c.id for c in service.list_clusters(s) if c.name == "dev")

    r = client.post(
        "/connect",
        data={
            "name": "solo", "repo_select": "acme/solo", "workload_class": "web",
            "cluster_dev": dev_id, "namespace_dev": "solo",
            "cluster_qa": "", "namespace_qa": "",
            "cluster_prod": "", "namespace_prod": "",
        },
        follow_redirects=False,
    )
    app_id = int(r.headers["location"].rsplit("/", 1)[-1])
    with session_factory() as s:
        view = service.app_status(s, app_id)
    assert [e.name for e in view.environments] == ["dev"]  # qa/prod skipped
