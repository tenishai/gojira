"""Service-layer tests: cluster registration, connect, provision, status reads."""

import pytest
from fakes import FakeGCPClient, FakeGitHubClient, FakeKubectlClient, KubectlFactory
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

    service.delete_app(session, app_id, kube, FakeGCPClient(), delete_namespaces=True)

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
    service.delete_app(session, app.id, kube, FakeGCPClient(), delete_namespaces=False)
    assert service.app_by_repo(session, "o/a") is None
    assert kube.by_cluster == {}  # no kubectl touched


def test_delete_app_missing_returns_none(session):
    assert service.delete_app(session, 999, KubectlFactory(), FakeGCPClient()) is None


def test_delete_app_summary_shape(session):
    clusters = _three_clusters(session)
    app = service.connect_app(
        session, name="clips-api", repo_full_name="shaz/clips-api", workload_class="web",
        env_map={"dev": (clusters["dev"].id, "clips")},
    )
    summary = service.delete_app(
        session, app.id, KubectlFactory(), FakeGCPClient(), delete_namespaces=True
    )
    assert summary == {
        "app": "clips-api",
        "namespaces_deleted": ["clips"],
        "namespace_failures": [],
        "identity": {
            "bindings_removed": 0,
            "gsa_deleted": False,
            "images_deleted": 0,
            "failures": [],
        },
        "rows_deleted": True,
    }


def test_delete_app_removes_provisioned_identity(session):
    from gojira.steps import DEPLOY_ROLES

    clusters = _three_clusters(session)
    app = service.connect_app(
        session, name="clips-api", repo_full_name="shaz/clips-api", workload_class="web",
        env_map={"dev": (clusters["dev"].id, "clips")},
    )
    clients = _clients()
    service.run_provision(session, app, clients, Config())
    app_id = app.id

    summary = service.delete_app(
        session, app_id, KubectlFactory(), clients.gcp, delete_namespaces=False
    )

    assert summary["identity"]["bindings_removed"] == len(DEPLOY_ROLES)
    assert summary["identity"]["gsa_deleted"] is True
    assert summary["identity"]["failures"] == []
    assert any(c.startswith("remove_proj_bind:") for c in clients.gcp.calls)
    assert any(c.startswith("delete_sa:") for c in clients.gcp.calls)


def test_delete_app_seven_tables_cleaned_up(session):
    from gojira.models import (
        DriftFinding,
        DriftStatus,
        Environment,
        Event,
        ProvisionStep,
        RecommendationKind,
        SizingRecommendation,
        UsageProfileRow,
        utcnow,
    )

    clusters = _three_clusters(session)
    app = service.connect_app(
        session, name="clips-api", repo_full_name="shaz/clips-api", workload_class="web",
        env_map={"dev": (clusters["dev"].id, "clips")},
    )
    app_id = app.id
    service.run_provision(session, app, _clients(), Config())  # seeds ProvisionStep + Event

    session.add(DriftFinding(
        app_id=app_id, environment="dev", status=DriftStatus.OPEN, diff_json="[]",
    ))
    session.add(UsageProfileRow(
        app_id=app_id, environment="dev", window_days=7, samples=10,
        cpu_json="{}", memory_json="{}", reported_by="agent", reported_at=utcnow(),
    ))
    session.add(SizingRecommendation(
        app_id=app_id, environment="dev", kind=RecommendationKind.OVERRIDE,
        current_json="{}", proposed_json="{}", justification="because",
    ))
    session.commit()

    summary = service.delete_app(
        session, app_id, KubectlFactory(), FakeGCPClient(), delete_namespaces=False
    )

    assert summary is not None
    assert summary["rows_deleted"] is True
    assert service.app_by_repo(session, "shaz/clips-api") is None
    for model in (
        Environment, ProvisionStep, Event, DriftFinding, UsageProfileRow, SizingRecommendation,
    ):
        assert session.query(model).filter_by(app_id=app_id).count() == 0


class _RaisingGCPClient(FakeGCPClient):
    """A FakeGCPClient whose teardown calls always raise, for exercising
    delete_app's best-effort failure handling (every GCP failure recorded,
    teardown continues, rows still deleted)."""

    def remove_project_binding(self, project, member, role):
        raise RuntimeError("boom: remove_project_binding")

    def delete_service_account(self, project, email):
        raise RuntimeError("boom: delete_service_account")

    def delete_app_images(self, ar_repo, name):
        raise RuntimeError("boom: delete_app_images")


def test_delete_app_gcp_failures_continue_and_rows_still_deleted(session):
    clusters = _three_clusters(session)
    app = service.connect_app(
        session, name="clips-api", repo_full_name="shaz/clips-api", workload_class="web",
        env_map={"dev": (clusters["dev"].id, "clips")},
    )
    app_id = app.id

    summary = service.delete_app(
        session, app_id, KubectlFactory(), _RaisingGCPClient(), delete_namespaces=False
    )

    assert summary is not None
    assert summary["rows_deleted"] is True
    assert service.app_by_repo(session, "shaz/clips-api") is None
    assert len(summary["identity"]["failures"]) > 0
    assert all("boom" in f for f in summary["identity"]["failures"])


class _RaisingKubectl(FakeKubectlClient):
    def delete_namespace(self, namespace):
        raise RuntimeError("cluster unreachable")


class _RaisingKubectlFactory:
    """kubectl_for that always raises on delete_namespace, for the
    namespace-failure-still-recorded-in-summary test."""

    def __init__(self):
        self._kube = _RaisingKubectl()

    def __call__(self, cluster):
        return self._kube


def test_delete_app_namespace_failure_recorded_in_summary(session):
    clusters = _three_clusters(session)
    app = service.connect_app(
        session, name="clips-api", repo_full_name="shaz/clips-api", workload_class="web",
        env_map={"dev": (clusters["dev"].id, "clips")},
    )
    app_id = app.id

    summary = service.delete_app(
        session, app_id, _RaisingKubectlFactory(), FakeGCPClient(), delete_namespaces=True
    )

    assert summary["namespaces_deleted"] == []
    assert summary["namespace_failures"] == [
        {"namespace": "clips", "reason": "cluster unreachable"}
    ]
    assert service.app_by_repo(session, "shaz/clips-api") is None


def test_cut_release_tags_head_and_normalizes_v():
    gh = FakeGitHubClient()
    tag = service.cut_release(gh, "acme/web", "1.2.3")
    assert tag == "v1.2.3"  # normalized to v*
    assert gh.tags == [("acme/web", "v1.2.3", "abc123def456")]

    service.cut_release(gh, "acme/web", "v2.0.0")  # already prefixed
    assert gh.tags[-1] == ("acme/web", "v2.0.0", "abc123def456")


def test_cut_release_rejects_empty_version():
    with pytest.raises(ValueError, match="version"):
        service.cut_release(FakeGitHubClient(), "acme/web", "  ")


def test_activate_app_clears_setup_pr_url(session):
    clusters = _three_clusters(session)
    app = service.connect_app(
        session, name="a", repo_full_name="o/a", workload_class="web",
        env_map={"dev": (clusters["dev"].id, "ns")},
    )
    app.status = AppStatus.AWAITING_PR
    app.setup_pr_url = "https://github.com/o/a/pull/1"
    session.commit()

    assert service.activate_app(session, app) is True
    assert app.status == AppStatus.ACTIVE
    # merged history — the provisioner recomputes status from the URL after
    # every run, so retaining it would drag the app back to awaiting_pr
    assert app.setup_pr_url is None


def test_redeploy_app_dispatches_ci_workflow(session):
    clusters = _three_clusters(session)
    app = service.connect_app(
        session, name="a", repo_full_name="o/a", workload_class="web",
        env_map={"dev": (clusters["dev"].id, "ns")},
    )
    gh = FakeGitHubClient()

    msg = service.redeploy_app(session, app.id, gh)

    assert gh.dispatched == [("o/a", "gojira-ci.yaml", "main")]
    assert "main" in msg
    view = service.app_status(session, app.id)
    assert any(e.type == "app.redeployed" for e in view.events)


def test_redeploy_app_unknown_app(session):
    with pytest.raises(ValueError, match="no such app"):
        service.redeploy_app(session, 999, FakeGitHubClient())


def test_list_apps_and_clusters(session):
    _three_clusters(session)
    service.connect_app(
        session, name="a1", repo_full_name="o/a1", workload_class="web",
        env_map={"dev": (1, "ns")},
    )
    assert [c.name for c in service.list_clusters(session)] == ["dev", "prod", "qa"]
    assert [a.name for a in service.list_apps(session)] == ["a1"]
