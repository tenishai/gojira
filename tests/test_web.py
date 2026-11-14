"""Web smoke tests: routes render and the connect→provision→watch flow works
end to end against in-memory fakes (no gcloud/git/kubectl)."""

import inspect
import json
import time

import pytest
from fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gojira import models, service
from gojira.config import Config
from gojira.github import SETUP_BRANCH
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


def test_app_detail_renders_drift_panel(client, session_factory):
    from gojira.models import App, AppStatus, Cluster, DriftFinding, DriftStatus, Environment

    with session_factory() as s:
        c = Cluster(name="dev", gcp_project="proj-a", location="us-central1",
                    gke_cluster="dev-gke", ar_repo="x")
        s.add(c)
        s.commit()
        a = App(name="clips-api", repo_full_name="acme/clips-api", installation_id=1,
                workload_class="web", status=AppStatus.ACTIVE)
        s.add(a)
        s.commit()
        s.add(Environment(app_id=a.id, name="dev", cluster_id=c.id, namespace="clips"))
        s.add(DriftFinding(
            app_id=a.id, environment="dev", status=DriftStatus.PR_OPENED,
            pr_url="https://github.com/acme/clips-api/pull/1",
            diff_json='[{"kind":"Deployment","name":"clips-api","fields":'
                      '[{"path":"containers.app.resources.requests.cpu","desired":250,"live":800}]}]',
        ))
        s.commit()
        app_id = a.id

    r = client.get(f"/apps/{app_id}")
    assert r.status_code == 200
    assert "Drift" in r.text
    assert "containers.app.resources.requests.cpu" in r.text
    assert "capture PR" in r.text and "/pull/1" in r.text


def test_app_detail_offers_provision_again_when_healthy(client, session_factory):
    from gojira.models import App, AppStatus

    with session_factory() as s:
        a = App(name="ok", repo_full_name="acme/ok", installation_id=1,
                workload_class="web", status=AppStatus.ACTIVE)
        s.add(a)
        s.commit()
        app_id = a.id

    r = client.get(f"/apps/{app_id}")
    assert r.status_code == 200
    # a healthy app can re-provision without delete + reconnect
    assert "Provision again" in r.text
    assert f"/apps/{app_id}/provision" in r.text
    assert "Retry" not in r.text  # that label is the failed-state affordance


def test_app_detail_keeps_retry_for_failed(client, session_factory):
    from gojira.models import App, AppStatus

    with session_factory() as s:
        a = App(name="broken", repo_full_name="acme/broken", installation_id=1,
                workload_class="web", status=AppStatus.FAILED)
        s.add(a)
        s.commit()
        app_id = a.id

    r = client.get(f"/apps/{app_id}")
    assert "Retry" in r.text
    assert "Provision again" not in r.text


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
    assert "acme/hello-app" in html  # from the fake's repo list


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
    gh._repo_ever_merged = True
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


def test_promote_cuts_tag_and_redirects_to_actions(client, clients, session_factory):
    _register_three(session_factory)
    with session_factory() as s:
        dev_id = next(c.id for c in service.list_clusters(s) if c.name == "dev")
        app = service.connect_app(
            s, name="rel", repo_full_name="acme/rel", workload_class="web",
            env_map={"dev": (dev_id, "rel")},
        )
        app_id = app.id

    r = client.post(
        f"/apps/{app_id}/promote", data={"version": "0.3.0"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "https://github.com/acme/rel/actions"
    assert clients.github.tags == [("acme/rel", "v0.3.0", "abc123def456")]


def test_redeploy_dispatches_ci_and_redirects_to_actions(client, clients, session_factory):
    _register_three(session_factory)
    with session_factory() as s:
        dev_id = next(c.id for c in service.list_clusters(s) if c.name == "dev")
        app = service.connect_app(
            s, name="lazarus", repo_full_name="acme/lazarus", workload_class="web",
            env_map={"dev": (dev_id, "lazarus")},
        )
        app_id = app.id

    r = client.post(f"/apps/{app_id}/redeploy", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://github.com/acme/lazarus/actions"
    assert clients.github.dispatched == [("acme/lazarus", "gojira-ci.yaml", "main")]


def test_redeploy_failure_surfaces_on_app_page(client, clients, session_factory):
    _register_three(session_factory)
    with session_factory() as s:
        dev_id = next(c.id for c in service.list_clusters(s) if c.name == "dev")
        app = service.connect_app(
            s, name="stuck", repo_full_name="acme/stuck", workload_class="web",
            env_map={"dev": (dev_id, "stuck")},
        )
        app_id = app.id
    clients.github.dispatch_error = "workflow has no workflow_dispatch trigger"

    r = client.post(f"/apps/{app_id}/redeploy", follow_redirects=True)
    assert r.status_code == 200
    assert "Redeploy failed" in r.text
    assert "workflow_dispatch" in r.text


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


def test_delete_app_form_unchecked_teardown_identity_leaves_gcp_untouched(
    client, clients, session_factory
):
    _register_three(session_factory)
    with session_factory() as s:
        dev_id = next(c.id for c in service.list_clusters(s) if c.name == "dev")
        app = service.connect_app(
            s, name="keeper", repo_full_name="acme/keeper", workload_class="web",
            env_map={"dev": (dev_id, "keeper")},
        )
        service.run_provision(s, app, clients, Config())
        app_id = app.id

    # Checkbox off means the browser omits the field entirely — same idiom
    # as the delete_namespaces checkbox above.
    r = client.post(
        f"/apps/{app_id}/delete", data={"delete_namespaces": "1"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert not any(c.startswith("delete_sa:") for c in clients.gcp.calls)
    assert not any(c.startswith("remove_proj_bind:") for c in clients.gcp.calls)
    assert not any(c.startswith("delete_images:") for c in clients.gcp.calls)


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


def _api_body(**overrides):
    body = {
        "name": "clio",
        "repo_full_name": "acme/agent-clio",
        "workload_class": "agent",
        "environments": [{"name": "dev", "cluster": "dev", "namespace": "clio"}],
        "auto_merge_setup_pr": False,
    }
    body.update(overrides)
    return body


def test_api_connect_creates_app_and_provisions(client, session_factory, clients):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body())
    assert resp.status_code == 201
    app_id = resp.json()["id"]

    deadline = time.time() + 5
    while time.time() < deadline:
        status = client.get(f"/api/v1/apps/{app_id}").json()
        if status["status"] in ("awaiting_pr", "active", "failed"):
            break
        time.sleep(0.05)
    assert status["status"] == "awaiting_pr"
    assert status["setup_pr_url"] == "https://github.com/acme/agent-clio/pull/1"
    assert {"name", "status", "error"} <= set(status["steps"][0])


def test_api_connect_unknown_cluster_404(client, session_factory):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(
        environments=[{"name": "dev", "cluster": "nope", "namespace": "x"}]))
    assert resp.status_code == 404


def test_api_connect_without_a_cluster_uses_the_installs_default(session_factory, clients):
    """Issue #87: `environments: [{name: dev}]` means the cluster this install
    registers for 'dev' (defaults.environment_clusters) — so a contract or a
    kobo call need not name an install-specific cluster."""
    cfg = Config.model_validate({"defaults": {"environment_clusters": {"dev": "dev"}}})
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(environments=[{"name": "dev", "namespace": "clio"}]))
    assert resp.status_code == 201
    with session_factory() as s:
        envs = [(e.name, e.cluster.name) for e in service.app_status(s, resp.json()["id"]).environments]
    assert envs == [("dev", "dev")]


def test_api_connect_without_a_cluster_and_no_default_is_422(client, session_factory):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(environments=[{"name": "dev", "namespace": "clio"}]))
    assert resp.status_code == 422
    assert "defaults.environment_clusters" in resp.json()["error"]


def test_api_connect_duplicate_repo_409(client, session_factory):
    _register_three(session_factory)
    assert client.post("/api/v1/apps", json=_api_body()).status_code == 201
    assert client.post("/api/v1/apps", json=_api_body(name="other")).status_code == 409


def test_api_connect_duplicate_race_maps_integrity_error_to_409(
    client, session_factory, monkeypatch
):
    # Simulate the TOCTOU race: the pre-check misses (as it would for a
    # concurrent request) but the row already exists, so the insert inside
    # service.connect_app trips the uq_apps_repo unique constraint. The route
    # must map that IntegrityError to 409, not let it surface as a 500.
    _register_three(session_factory)
    assert client.post("/api/v1/apps", json=_api_body()).status_code == 201
    monkeypatch.setattr(service, "app_by_repo", lambda session, repo: None)
    resp = client.post("/api/v1/apps", json=_api_body(name="other"))
    assert resp.status_code == 409


def test_api_connect_bad_class_422(client, session_factory):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(workload_class="mainframe"))
    assert resp.status_code == 422


def test_api_status_unknown_id_404(client):
    assert client.get("/api/v1/apps/999").status_code == 404


def test_api_connect_echoes_auto_merge_setup_pr_on_status(client, session_factory):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(auto_merge_setup_pr=True))
    assert resp.status_code == 201
    app_id = resp.json()["id"]
    status = client.get(f"/api/v1/apps/{app_id}").json()
    assert status["auto_merge_setup_pr"] is True


def test_api_connect_keeps_a_seed_and_refuses_one_it_could_not_render(client, session_factory):
    """`seed` is the caller's share of the first setup PR. It is kept on the
    row (every provision run renders from a fresh clone) and refused whole,
    before anything is connected, when it names a key gojira.yaml does not
    have, a file that is not a plain object of the kinds a seed may add, or
    asks for the pull request to be merged unreviewed."""
    _register_three(session_factory)
    account = "apiVersion: v1\nkind: ServiceAccount\nmetadata: {name: agent-clio}\n"
    for seed in (
        {"needs": {"service_acount": "agent-clio"}},
        {"needs": {"data_mount": "agent-clio-data"}},
        {"base_files": {"../workflow.yaml": account}},
        {"base_files": {"kustomization.yaml": account}},
        {"base_files": {"role.yaml": "apiVersion: rbac.authorization.k8s.io/v1\nkind: RoleBinding\nmetadata: {name: x}\n"}},
        {"base_files": {"notes.yaml": "just: words\n"}},
    ):
        refused = client.post("/api/v1/apps", json=_api_body(seed=seed))
        assert refused.status_code == 422, seed
    unreviewed = client.post("/api/v1/apps", json=_api_body(
        seed={"needs": {"service_account": "agent-clio"}}, auto_merge_setup_pr=True))
    assert unreviewed.status_code == 422
    with session_factory() as s:
        assert s.query(models.App).count() == 0

    seed = {"needs": {"service_account": "agent-clio"}, "base_files": {"serviceaccount.yaml": account}, "note": "why"}
    resp = client.post("/api/v1/apps", json=_api_body(seed=seed))
    assert resp.status_code == 201
    with session_factory() as s:
        assert json.loads(s.get(models.App, resp.json()["id"]).setup_seed_json) == seed


def test_api_connect_auto_merge_activates(client, session_factory, clients):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(auto_merge_setup_pr=True))
    app_id = resp.json()["id"]

    deadline = time.time() + 5
    while time.time() < deadline:
        status = client.get(f"/api/v1/apps/{app_id}").json()
        if status["status"] in ("active", "failed"):
            break
        time.sleep(0.05)
    assert status["status"] == "active"
    assert any(s["name"] == "merge_setup_pr" for s in status["steps"])


class _RefreshingGitHubClient(FakeGitHubClient):
    """Unlike the base fake (which always hands back pull/1), open_pr mints a
    fresh PR number on every call — modeling GitHub reality: a merged PR is
    closed, so a later push under the same setup branch opens a brand new PR
    rather than reusing the merged one's number."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._next_pr = 1

    def open_pr(self, repo, head, base, title, body):
        self.calls.append(f"open_pr:{head}->{base}")
        url = f"https://github.com/{repo}/pull/{self._next_pr}"
        self._next_pr += 1
        self.prs[head] = url
        return url


def test_reprovision_of_auto_merge_app_merges_a_fresh_pr(session_factory):
    """The merge guard (steps.py's MergeSetupPr) is PR-specific, not repo
    history: re-provisioning an auto-merge app after a generator upgrade
    opens a FRESH setup PR under the same branch, and that fresh PR must get
    merged too — a stale "this repo has had a PR merged before" must never
    short-circuit the new PR's own merge (which would erase setup_pr_url and
    silently orphan it)."""
    gh = _RefreshingGitHubClient()
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=gh, kubectl_for=KubectlFactory()
    )
    client = TestClient(create_app(session_factory, clients=clients))
    _register_three(session_factory)

    resp = client.post("/api/v1/apps", json=_api_body(auto_merge_setup_pr=True))
    app_id = resp.json()["id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        status = client.get(f"/api/v1/apps/{app_id}").json()
        if status["status"] in ("active", "failed"):
            break
        time.sleep(0.05)
    assert status["status"] == "active"
    first_pr = gh.merged[-1]

    # The branch's PR closed on merge, so it's no longer "existing" for the
    # branch — re-provision (e.g. after a generator upgrade) must open a
    # fresh one instead of silently treating the old merge as covering it.
    gh.prs.pop(SETUP_BRANCH, None)
    client.post(f"/apps/{app_id}/provision")
    deadline = time.time() + 5
    while time.time() < deadline:
        status = client.get(f"/api/v1/apps/{app_id}").json()
        if status["status"] in ("active", "failed"):
            break
        time.sleep(0.05)
    assert status["status"] == "active"

    second_pr = gh.merged[-1]
    assert second_pr != first_pr
    assert gh.merged == [first_pr, second_pr]  # both PRs actually merged


def test_api_connect_no_dockerfile_422(session_factory):
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(),
        github=FakeGitHubClient(dockerfile=False),
        kubectl_for=KubectlFactory(),
    )
    client = TestClient(create_app(session_factory, clients=clients))
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body())
    assert resp.status_code == 422
    assert "Dockerfile" in resp.json()["error"]


def test_api_connect_duplicate_environment_names_422(client, session_factory):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(
        environments=[
            {"name": "dev", "cluster": "dev", "namespace": "a"},
            {"name": "dev", "cluster": "qa", "namespace": "b"},
        ]
    ))
    assert resp.status_code == 422


# --- name/namespace/repo charset validation (finding #3: auto-merge removes
# the human-PR sanitization backstop, so these must be enforced up front,
# before the values ever reach Jinja2-rendered YAML or a GSA id) -----------


def test_api_connect_name_with_newline_422(client, session_factory):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(name="clio\nmalicious"))
    assert resp.status_code == 422


def test_api_connect_name_with_uppercase_or_underscore_422(client, session_factory):
    _register_three(session_factory)
    assert client.post("/api/v1/apps", json=_api_body(name="Clio")).status_code == 422
    assert client.post("/api/v1/apps", json=_api_body(name="cli_o")).status_code == 422


def test_api_connect_namespace_with_slash_422(client, session_factory):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(
        environments=[{"name": "dev", "cluster": "dev", "namespace": "a/../b"}]
    ))
    assert resp.status_code == 422


def test_api_connect_namespace_with_trailing_newline_422(client, session_factory):
    """`$` in the DNS-1123 charset regex matches just before a trailing
    newline, so `.match()` alone lets 'clio\\n' through; the validator must
    use `.fullmatch()` (or an equivalent `\\Z`-anchored check) to reject it
    before the raw newline reaches the DB/YAML pipeline."""
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(
        environments=[{"name": "dev", "cluster": "dev", "namespace": "clio\n"}]
    ))
    assert resp.status_code == 422


def test_api_connect_namespace_blank_still_allowed(client, session_factory):
    """Blank namespace is a legitimate sentinel meaning "derive it from the
    app name" (see api_connect's env_map construction) — not a raw value that
    ever reaches k8s, so it must not be rejected by the charset check."""
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(
        environments=[{"name": "dev", "cluster": "dev", "namespace": ""}]
    ))
    assert resp.status_code == 201


def test_api_connect_repo_full_name_traversal_422(client, session_factory):
    _register_three(session_factory)
    # A single ".." segment matches the old single-slash charset regex fine
    # (two non-slash groups) — it's exactly the traversal case that regex
    # alone doesn't catch and the dedicated segment check must reject.
    resp = client.post("/api/v1/apps", json=_api_body(repo_full_name="acme/.."))
    assert resp.status_code == 422


def test_api_connect_repo_full_name_leading_dash_422(client, session_factory):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body(repo_full_name="acme/-rf"))
    assert resp.status_code == 422


def test_api_connect_valid_body_still_passes(client, session_factory):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body())
    assert resp.status_code == 201


# --- /api/v1 bearer-token gate (the IAP-exempt namespace's own auth) --------


def test_api_token_configured_correct_header_allows(session_factory, clients):
    cfg = Config()
    cfg.auth.api_token = "s3cr3t"
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    _register_three(session_factory)

    resp = client.post(
        "/api/v1/apps", json=_api_body(), headers={"Authorization": "Bearer s3cr3t"}
    )
    assert resp.status_code == 201
    app_id = resp.json()["id"]
    status = client.get(
        f"/api/v1/apps/{app_id}", headers={"Authorization": "Bearer s3cr3t"}
    )
    assert status.status_code == 200


def test_api_token_configured_missing_header_401(session_factory, clients):
    cfg = Config()
    cfg.auth.api_token = "s3cr3t"
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    _register_three(session_factory)

    resp = client.post("/api/v1/apps", json=_api_body())
    assert resp.status_code == 401


def test_api_token_configured_wrong_header_401(session_factory, clients):
    cfg = Config()
    cfg.auth.api_token = "s3cr3t"
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    _register_three(session_factory)

    resp = client.post(
        "/api/v1/apps", json=_api_body(), headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401


def test_api_status_route_also_requires_configured_token(session_factory, clients):
    cfg = Config()
    cfg.auth.api_token = "s3cr3t"
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    _register_three(session_factory)
    app_id = client.post(
        "/api/v1/apps", json=_api_body(), headers={"Authorization": "Bearer s3cr3t"}
    ).json()["id"]

    assert client.get(f"/api/v1/apps/{app_id}").status_code == 401
    assert client.get(
        f"/api/v1/apps/{app_id}", headers={"Authorization": "Bearer s3cr3t"}
    ).status_code == 200


def test_api_no_token_and_iap_enabled_fails_closed(session_factory, clients):
    """The /api/v1/* exemption from the IAP gate rests on the premise those
    routes carry their own auth — with no api_token configured, they don't,
    so an operator who enables IAP without also setting one must be refused
    rather than silently left wide open."""
    cfg = Config()
    cfg.auth.enabled = True
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    _register_three(session_factory)

    resp = client.post("/api/v1/apps", json=_api_body())
    assert resp.status_code == 401
    assert "api_token" in resp.json()["error"]


def test_api_no_token_and_iap_disabled_allows(client, session_factory):
    """Laptop mode (no api_token, IAP disabled): the dev experience every
    other /api/v1 test in this file already exercises — asserted here
    explicitly for this finding."""
    _register_three(session_factory)
    resp = client.post("/api/v1/apps", json=_api_body())
    assert resp.status_code == 201


def test_api_token_non_ascii_header_401_not_500(session_factory, clients):
    """secrets.compare_digest raises TypeError on two strs where one has
    non-ASCII content once encoded to bytes of mismatched length/type in some
    paths — a stray non-ASCII Authorization header must 401, not 500."""
    cfg = Config()
    cfg.auth.api_token = "s3cr3t"
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    _register_three(session_factory)

    resp = client.post(
        "/api/v1/apps",
        json=_api_body(),
        # httpx's TestClient rejects a plain non-ASCII str header outright
        # (encodes as ascii client-side) — send raw bytes so the non-ASCII
        # byte actually reaches the server, decoded per HTTP as latin-1 into
        # a non-ASCII str, same as a real client would deliver it.
        headers={"Authorization": "Bearer café".encode()},
    )
    assert resp.status_code == 401


# --- POST /api/v1/apps/{id}/provision (programmatic reprovision kick) -------


def _settle(client, app_id, headers=None, want=("awaiting_pr", "active", "failed")):
    deadline = time.time() + 5
    while time.time() < deadline:
        status = client.get(f"/api/v1/apps/{app_id}", headers=headers or {}).json()
        if status["status"] in want:
            return status
        time.sleep(0.05)
    raise AssertionError(f"app {app_id} never settled: {status}")


def test_api_provision_kicks_new_run(client, session_factory, clients):
    """The automation-tier equivalent of the UI's Provision-again button:
    a room that created an app over /api/v1 must be able to re-kick a run
    through the same token-gated namespace (the UI route sits behind the
    IAP-identity gate, which a server-to-server caller can never satisfy)."""
    _register_three(session_factory)
    app_id = client.post("/api/v1/apps", json=_api_body()).json()["id"]
    _settle(client, app_id)

    resp = client.post(f"/api/v1/apps/{app_id}/provision")
    assert resp.status_code == 202
    assert resp.json() == {"id": app_id, "status": "provisioning"}
    assert _settle(client, app_id)["status"] == "awaiting_pr"


def test_api_provision_unknown_id_404(client, session_factory):
    _register_three(session_factory)
    resp = client.post("/api/v1/apps/999/provision")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


def test_api_provision_requires_configured_token(session_factory, clients):
    cfg = Config()
    cfg.auth.api_token = "s3cr3t"
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    _register_three(session_factory)
    ok = {"Authorization": "Bearer s3cr3t"}
    app_id = client.post("/api/v1/apps", json=_api_body(), headers=ok).json()["id"]
    _settle(client, app_id, headers=ok)

    assert client.post(f"/api/v1/apps/{app_id}/provision").status_code == 401
    assert (
        client.post(
            f"/api/v1/apps/{app_id}/provision",
            headers={"Authorization": "Bearer nope"},
        ).status_code
        == 401
    )
    assert client.post(f"/api/v1/apps/{app_id}/provision", headers=ok).status_code == 202


def test_api_provision_fails_closed_iap_enabled_no_token(session_factory, clients):
    cfg = Config()
    cfg.auth.enabled = True
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    resp = client.post("/api/v1/apps/1/provision")
    assert resp.status_code == 401
    assert "api_token" in resp.json()["error"]


# --- DELETE /api/v1/apps/{id} (programmatic teardown) -----------------------


def test_api_delete_route_does_not_block_the_event_loop(client):
    # api_delete_app runs the whole blocking teardown (gcloud/kubectl
    # subprocesses, realistically 15-60s) inline. An async-def handler runs
    # that on the event loop itself, stalling every other request for the
    # duration — the route must instead be a plain-def handler (or otherwise
    # dispatch the blocking work off-loop) so Starlette's threadpool absorbs
    # it, same as its UI twin (/apps/{id}/delete) already does.
    routes = [
        r
        for r in client.app.routes
        if getattr(r, "path", None) == "/api/v1/apps/{app_id}"
        and "DELETE" in getattr(r, "methods", set())
    ]
    assert len(routes) == 1, "expected exactly one DELETE route for /api/v1/apps/{app_id}"
    assert not inspect.iscoroutinefunction(routes[0].endpoint), (
        "api_delete_app must not be async def — it runs blocking gcloud/kubectl "
        "teardown inline, which would stall the event loop for every other request"
    )


def test_api_delete_removes_app_returns_202_and_summary(client, session_factory, clients):
    _register_three(session_factory)
    app_id = client.post("/api/v1/apps", json=_api_body()).json()["id"]
    _settle(client, app_id)

    resp = client.request("DELETE", f"/api/v1/apps/{app_id}")
    assert resp.status_code == 202
    body = resp.json()
    assert body["app"] == "clio"
    assert body["rows_deleted"] is True
    assert "identity" in body

    assert client.get(f"/api/v1/apps/{app_id}").status_code == 404


def test_api_delete_unknown_id_404(client, session_factory):
    _register_three(session_factory)
    resp = client.request("DELETE", "/api/v1/apps/999")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


def test_api_delete_repeat_returns_404(client, session_factory):
    _register_three(session_factory)
    app_id = client.post("/api/v1/apps", json=_api_body()).json()["id"]
    _settle(client, app_id)

    assert client.request("DELETE", f"/api/v1/apps/{app_id}").status_code == 202
    assert client.request("DELETE", f"/api/v1/apps/{app_id}").status_code == 404


def test_api_delete_body_flag_honored(client, session_factory, clients):
    _register_three(session_factory)
    app_id = client.post("/api/v1/apps", json=_api_body(
        environments=[{"name": "dev", "cluster": "dev", "namespace": "clio"}]
    )).json()["id"]
    _settle(client, app_id)

    resp = client.request(
        "DELETE", f"/api/v1/apps/{app_id}", json={"delete_namespaces": False}
    )
    assert resp.status_code == 202
    assert resp.json()["namespaces_deleted"] == []
    assert "delete_ns:clio" not in clients.kubectl_for.by_cluster["dev"].calls


def test_api_delete_empty_body_defaults_to_delete_namespaces_true(client, session_factory, clients):
    _register_three(session_factory)
    app_id = client.post("/api/v1/apps", json=_api_body(
        environments=[{"name": "dev", "cluster": "dev", "namespace": "clio"}]
    )).json()["id"]
    _settle(client, app_id)

    resp = client.request("DELETE", f"/api/v1/apps/{app_id}")  # no body at all
    assert resp.status_code == 202
    body = resp.json()
    assert body["namespaces_deleted"] == ["clio"]
    assert "delete_ns:clio" in clients.kubectl_for.by_cluster["dev"].calls
    # teardown_identity defaults true too — full identity teardown, no skip.
    assert body["identity"]["gsa_deleted"] is True
    assert body["identity"]["images_skipped"] is None


def test_api_delete_teardown_identity_false_skips_identity_and_images(
    client, session_factory, clients
):
    _register_three(session_factory)
    app_id = client.post("/api/v1/apps", json=_api_body(
        environments=[{"name": "dev", "cluster": "dev", "namespace": "clio"}]
    )).json()["id"]
    _settle(client, app_id)
    clients.gcp.calls.clear()

    resp = client.request(
        "DELETE", f"/api/v1/apps/{app_id}", json={"teardown_identity": False}
    )
    assert resp.status_code == 202
    assert resp.json()["identity"] == {
        "bindings_removed": 0,
        "gsa_deleted": False,
        "images_deleted": 0,
        "images_skipped": None,
        "failures": [],
    }
    assert clients.gcp.calls == []


def test_api_delete_requires_configured_token(session_factory, clients):
    cfg = Config()
    cfg.auth.api_token = "s3cr3t"
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    _register_three(session_factory)
    ok = {"Authorization": "Bearer s3cr3t"}
    app_id = client.post("/api/v1/apps", json=_api_body(), headers=ok).json()["id"]
    _settle(client, app_id, headers=ok)

    assert client.request("DELETE", f"/api/v1/apps/{app_id}").status_code == 401
    assert (
        client.request(
            "DELETE", f"/api/v1/apps/{app_id}", headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )
    assert client.request("DELETE", f"/api/v1/apps/{app_id}", headers=ok).status_code == 202


def test_api_delete_fails_closed_iap_enabled_no_token(session_factory, clients):
    cfg = Config()
    cfg.auth.enabled = True
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    resp = client.request("DELETE", "/api/v1/apps/1")
    assert resp.status_code == 401
    assert "api_token" in resp.json()["error"]


def test_rollback_banner_renders_with_revert_button(client, session_factory):
    import json as _json
    from gojira.models import App, AppStatus, Event

    with session_factory() as s:
        a = App(name="rb", repo_full_name="acme/rb", installation_id=1,
                workload_class="web", status=AppStatus.ACTIVE)
        s.add(a)
        s.commit()
        s.add(Event(app_id=a.id, type="app.rolled_back", payload_json=_json.dumps(
            {"environment": "dev", "sha": "c" * 40, "image": "img", "actor": "op@x.tv"}
        )))
        s.commit()
        app_id = a.id

    r = client.get(f"/apps/{app_id}")
    assert r.status_code == 200
    assert "was rolled back to" in r.text
    assert "Open revert PR" in r.text
    assert f"/apps/{app_id}/revert-pr" in r.text
    assert ("c" * 12) in r.text


def test_api_sync_advances_a_merged_setup_pr(client, session_factory, clients):
    """The automation-tier "check PR merge": a caller that parked an app on a
    person (a connected repo in kobo) asks whether the setup PR merged. On an
    install without the webhook, the status read alone says awaiting_pr for
    ever — this is the pull that answers it, with one GitHub read."""
    _register_three(session_factory)
    app_id = client.post("/api/v1/apps", json=_api_body()).json()["id"]
    status = _settle(client, app_id)
    assert status["status"] == "awaiting_pr"

    resp = client.post(f"/api/v1/apps/{app_id}/sync")
    assert resp.json() == {"id": app_id, "status": "awaiting_pr", "changed": False}

    clients.github._repo_ever_merged = True  # a person merged it (sync_app asks repo history)
    resp = client.post(f"/api/v1/apps/{app_id}/sync")
    assert resp.json() == {"id": app_id, "status": "active", "changed": True}
    assert client.get(f"/api/v1/apps/{app_id}").json()["status"] == "active"


def test_api_sync_unknown_id_404(client, session_factory):
    _register_three(session_factory)
    assert client.post("/api/v1/apps/999/sync").status_code == 404


def test_api_sync_requires_configured_token(session_factory, clients):
    cfg = Config()
    cfg.auth.api_token = "s3cr3t"
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    _register_three(session_factory)
    app_id = client.post("/api/v1/apps", json=_api_body(), headers={"Authorization": "Bearer s3cr3t"}).json()["id"]
    assert client.post(f"/api/v1/apps/{app_id}/sync").status_code == 401
    assert client.post(f"/api/v1/apps/{app_id}/sync", headers={"Authorization": "Bearer s3cr3t"}).status_code == 200


# --- list routes: what uninstall (and any other machine) walks ----------------


def test_api_list_apps_names_every_app_and_where_it_runs(client, session_factory):
    _register_three(session_factory)
    assert client.get("/api/v1/apps").json() == {"apps": []}
    first = client.post("/api/v1/apps", json=_api_body()).json()["id"]
    second = client.post(
        "/api/v1/apps",
        json=_api_body(name="web", repo_full_name="acme/web", workload_class="web",
                       environments=[{"name": "dev", "cluster": "dev", "namespace": ""}]),
    ).json()["id"]

    apps = client.get("/api/v1/apps").json()["apps"]
    assert [a["id"] for a in apps] == [first, second]
    assert apps[0]["repo_full_name"] == "acme/agent-clio"
    assert apps[0]["environments"] == [{"name": "dev", "cluster": "dev", "namespace": "clio"}]
    assert apps[1]["environments"][0]["namespace"] == "web"  # blank = the app name


def test_api_list_clusters(client, session_factory):
    _register_three(session_factory)
    clusters = client.get("/api/v1/clusters").json()["clusters"]
    assert {c["name"] for c in clusters} >= {"dev"}
    assert {"name", "gcp_project", "location", "gke_cluster", "ar_repo"} == set(clusters[0])


def test_api_list_routes_are_token_gated(session_factory, clients):
    cfg = Config()
    cfg.auth.api_token = "s3cr3t"
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    for path in ("/api/v1/apps", "/api/v1/clusters"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer s3cr3t"}).status_code == 200


# --- issue #108: a repo with no Dockerfile connects when its contract names an image


IMAGE_CONTRACT = "app: web\nclass: web\nimage: ghcr.io/acme/web:1.2.0\n"


def test_connect_accepts_a_repo_whose_contract_names_an_image(session_factory):
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(),
        github=FakeGitHubClient(dockerfile=False, contract=IMAGE_CONTRACT),
        kubectl_for=KubectlFactory(),
    )
    client = TestClient(create_app(session_factory, clients=clients))
    _register_three(session_factory)
    with session_factory() as s:
        dev_id = next(c.id for c in service.list_clusters(s) if c.name == "dev")
    r = client.post(
        "/connect",
        data={"name": "published", "repo_select": "acme/web", "workload_class": "web",
              "cluster_dev": dev_id, "namespace_dev": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert not r.headers["location"].startswith("/connect?error="), r.headers["location"]
    with session_factory() as s:
        assert service.app_by_repo(s, "acme/web") is not None


def test_api_connect_accepts_an_image_contract_and_refuses_neither(session_factory):
    _register_three(session_factory)
    accepted = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(dockerfile=False, contract=IMAGE_CONTRACT),
        kubectl_for=KubectlFactory(),
    )
    resp = TestClient(create_app(session_factory, clients=accepted)).post("/api/v1/apps", json=_api_body())
    assert resp.status_code == 201, resp.text
    neither = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(dockerfile=False, contract="app: web\nclass: web\n"),
        kubectl_for=KubectlFactory(),
    )
    body = {**_api_body(), "name": "other", "repo_full_name": "acme/other"}
    resp = TestClient(create_app(session_factory, clients=neither)).post("/api/v1/apps", json=body)
    assert resp.status_code == 422
    assert "Dockerfile" in resp.json()["error"] and "image:" in resp.json()["error"]
