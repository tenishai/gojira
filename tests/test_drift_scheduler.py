"""The drift sweep visits every active app/env and survives per-env failures."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from gojira import drift_scan, service
from gojira.config import Config
from gojira.db import init_db, make_session_factory
from gojira.models import App, AppStatus, Cluster, DriftFinding, DriftStatus, Environment
from tests.fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory


@pytest.fixture(autouse=True)
def _no_real_kustomize(monkeypatch):
    """Keep sweeps hermetic — no shelling out to `kubectl kustomize`.

    Empty desired set → each scanned env resolves to RESOLVED (no drift),
    which is all these scheduler tests need.
    """
    monkeypatch.setattr(drift_scan, "kustomize_build", lambda _overlay: [])


def _factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    init_db(engine)
    return make_session_factory(engine)


def _clients(github=None):
    return service.ProvisionClients(
        gcp=FakeGCPClient(), github=github or FakeGitHubClient(), kubectl_for=KubectlFactory()
    )


def _seed(session):
    c = Cluster(name="dev-cluster", gcp_project="example-project", location="us-central1",
                gke_cluster="dev", ar_repo="us-central1-docker.pkg.dev/example-project/apps")
    session.add(c)
    session.commit()
    for name, status in [("alive", AppStatus.ACTIVE), ("pending", AppStatus.PENDING)]:
        a = App(name=name, repo_full_name=f"acme/{name}", installation_id=1,
                workload_class="web", status=status)
        session.add(a)
        session.commit()
        session.add(Environment(app_id=a.id, name="dev", cluster_id=c.id, namespace=name))
        session.commit()


def test_sweep_scans_only_active_apps_and_records_findings():
    factory = _factory()
    with factory() as s:
        _seed(s)
    clients = _clients()
    # No live deployment present anywhere → desired render returns nothing, so no
    # managed objects, so each active env resolves to "resolved" (no drift).
    with factory() as s:
        service.scan_all_apps(s, clients, Config())
        findings = s.query(DriftFinding).all()
    # Exactly one finding, for the single active app's env (pending app skipped).
    assert len(findings) == 1
    assert findings[0].status == DriftStatus.RESOLVED


def test_sweep_continues_past_a_failing_env():
    factory = _factory()
    with factory() as s:
        _seed(s)

    class ExplodingGitHub(FakeGitHubClient):
        def clone(self, repo, workdir):
            raise RuntimeError("network down")

    # Should not raise despite the clone failure; the bad env is just skipped.
    with factory() as s:
        service.scan_all_apps(s, _clients(github=ExplodingGitHub()), Config())
        assert s.query(DriftFinding).count() == 0  # nothing recorded, no crash


def test_scan_app_drift_targets_one_active_app():
    factory = _factory()
    with factory() as s:
        _seed(s)
        active = s.query(App).filter_by(name="alive").one().id
        pending = s.query(App).filter_by(name="pending").one().id
    clients = _clients()
    with factory() as s:
        service.scan_app_drift(s, active, clients)
        service.scan_app_drift(s, pending, clients)  # no-op: not active
        findings = s.query(DriftFinding).all()
    assert len(findings) == 1 and findings[0].status == DriftStatus.RESOLVED


def test_scanner_thread_starts_and_stops():
    factory = _factory()
    with factory() as s:
        _seed(s)
    cfg = Config()
    cfg.drift.interval_seconds = 3600  # long, so we control timing via stop
    t, stop = service.start_drift_scanner(factory, _clients(), cfg)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
