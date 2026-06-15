"""Service-layer tests: cluster registration, connect, provision, status reads."""

import pytest
from fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gojira import service
from gojira.config import Config
from gojira.models import AppStatus, Base


@pytest.fixture
def session_factory():
    # StaticPool shares one in-memory DB across connections so the background
    # provision thread sees the same schema/data as the test thread.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory):
    with session_factory() as s:
        yield s


def _clients():
    return service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory()
    )


def _three_clusters(session):
    return {
        "dev": service.register_cluster(
            session, name="dev", gcp_project="proj-a", location="us-central1",
            gke_cluster="dev-gke",
        ),
        "qa": service.register_cluster(
            session, name="qa", gcp_project="proj-a", location="us-central1",
            gke_cluster="qa-gke",
        ),
        "prod": service.register_cluster(
            session, name="prod", gcp_project="proj-b", location="us-central1",
            gke_cluster="prod-gke",
        ),
    }


# --- register_cluster --------------------------------------------------------


def test_register_cluster_defaults_ar_repo(session):
    c = service.register_cluster(
        session, name="dev", gcp_project="proj-a", location="us-central1", gke_cluster="dev-gke"
    )
    assert c.ar_repo == "us-central1-docker.pkg.dev/proj-a/apps"


def test_register_cluster_validates_connectivity(session):
    gcp = FakeGCPClient()
    service.register_cluster(
        session, name="dev", gcp_project="proj-a", location="us-central1",
        gke_cluster="dev-gke", gcp=gcp,
    )
    assert "get_creds:dev-gke" in gcp.calls  # connectivity check ran


# --- connect_app -------------------------------------------------------------


def test_connect_app_creates_environments_in_order(session):
    clusters = _three_clusters(session)
    app = service.connect_app(
        session,
        name="clips-api",
        repo_full_name="shaz/clips-api",
        workload_class="web",
        env_map={
            "dev": (clusters["dev"].id, "clips"),
            "qa": (clusters["qa"].id, "clips"),
            "prod": (clusters["prod"].id, "clips"),
        },
    )
    assert app.status == AppStatus.PENDING
    assert [e.name for e in app.environments] == ["dev", "qa", "prod"]
    assert app.environments[2].cluster.gcp_project == "proj-b"


def test_connect_app_rejects_empty_env_map(session):
    with pytest.raises(ValueError, match="environment"):
        service.connect_app(
            session, name="x", repo_full_name="o/x", workload_class="web", env_map={}
        )


# --- provision + status ------------------------------------------------------


def test_run_provision_drives_full_flow(session):
    clusters = _three_clusters(session)
    app = service.connect_app(
        session, name="clips-api", repo_full_name="shaz/clips-api", workload_class="web",
        env_map={n: (c.id, "clips") for n, c in clusters.items()},
    )
    clients = _clients()
    service.run_provision(session, app, clients, Config())

    assert app.status == AppStatus.AWAITING_PR
    assert app.setup_pr_url == "https://github.com/shaz/clips-api/pull/1"
    # identity provisioned once per distinct project
    assert "clips-api-deploy@proj-a.iam.gserviceaccount.com" in clients.gcp.service_accounts
    assert "clips-api-deploy@proj-b.iam.gserviceaccount.com" in clients.gcp.service_accounts


def test_app_status_view_parses_scoped_steps(session):
    clusters = _three_clusters(session)
    app = service.connect_app(
        session, name="clips-api", repo_full_name="shaz/clips-api", workload_class="web",
        env_map={n: (c.id, "clips") for n, c in clusters.items()},
    )
    service.run_provision(session, app, _clients(), Config())

    view = service.app_status(session, app.id)
    assert view.terminal  # awaiting_pr is terminal for the progress view
    assert {e.name for e in view.environments} == {"dev", "qa", "prod"}
    # scoped ledger names split into (name, scope)
    sa = next(s for s in view.steps if s.name == "ensure_service_account")
    assert sa.scope in {"proj-a", "proj-b"}
    ns = next(s for s in view.steps if s.name == "ensure_namespace")
    assert ns.scope in {"dev", "qa", "prod"}
    assert view.counts["done"] + view.counts["skipped"] == len(view.steps)


def test_start_provision_runs_in_thread(session_factory):
    with session_factory() as s:
        clusters = _three_clusters(s)
        app = service.connect_app(
            s, name="clips-api", repo_full_name="shaz/clips-api", workload_class="web",
            env_map={"dev": (clusters["dev"].id, "clips")},
        )
        app_id = app.id

    t = service.start_provision(session_factory, app_id, clients=_clients())
    t.join(timeout=10)
    assert not t.is_alive()

    with session_factory() as s:
        view = service.app_status(s, app_id)
        assert view.app.status == AppStatus.AWAITING_PR


def test_delete_app_removes_rows_and_namespaces(session):
    clusters = _three_clusters(session)
    app = service.connect_app(
        session, name="clips-api", repo_full_name="shaz/clips-api", workload_class="web",
        env_map={n: (c.id, "clips") for n, c in clusters.items()},
    )
    kube = KubectlFactory()
    # seed the namespaces so the fakes have something to delete
    for n, c in clusters.items():
        kube(c).namespaces["clips"] = {"managed": "true"}
    service.run_provision(session, app, _clients(), Config())
    app_id = app.id

    service.delete_app(session, app_id, kube, delete_namespaces=True)

    assert service.app_by_repo(session, "shaz/clips-api") is None
    from gojira.models import Environment, Event, ProvisionStep
    assert session.query(Environment).filter_by(app_id=app_id).count() == 0
    assert session.query(ProvisionStep).filter_by(app_id=app_id).count() == 0
    assert session.query(Event).filter_by(app_id=app_id).count() == 0
    # each env's namespace was deleted on its own cluster
    assert "delete_ns:clips" in kube.by_cluster["dev"].calls
    assert "delete_ns:clips" in kube.by_cluster["prod"].calls


def test_delete_app_can_keep_namespaces(session):
    clusters = _three_clusters(session)
    app = service.connect_app(
        session, name="a", repo_full_name="o/a", workload_class="web",
        env_map={"dev": (clusters["dev"].id, "ns")},
    )
    kube = KubectlFactory()
    service.delete_app(session, app.id, kube, delete_namespaces=False)
    assert service.app_by_repo(session, "o/a") is None
    assert kube.by_cluster == {}  # no kubectl touched


def test_list_apps_and_clusters(session):
    _three_clusters(session)
    service.connect_app(
        session, name="a1", repo_full_name="o/a1", workload_class="web",
        env_map={"dev": (1, "ns")},
    )
    assert [c.name for c in service.list_clusters(session)] == ["dev", "prod", "qa"]
    assert [a.name for a in service.list_apps(session)] == ["a1"]
