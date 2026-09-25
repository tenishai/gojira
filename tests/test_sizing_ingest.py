"""Ingestion: token auth, cluster scoping, app resolution, ledger append."""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gojira import service
from gojira.models import App, AppStatus, Base, Cluster, Environment, UsageProfileRow, utcnow
from gojira.sizing.profile import UsageProfile
from gojira.sizing.service import IngestError, ingest_usage, mint_agent_token, usage_history
from gojira.web import create_app
from tests.fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory
from tests.test_sizing_profile import _fixture


@pytest.fixture
def session_factory(tmp_path):
    # File-based, like test_web.py: the TestClient serves requests on another
    # thread/connection, and a plain in-memory SQLite is per-connection.
    engine = create_engine(
        f"sqlite:///{tmp_path}/gojira.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture
def seeded(session):
    """A cluster named as in the golden fixture, its app + prod env, a token."""
    cluster = Cluster(name="my-prod-cluster", gcp_project="example-project",
                      location="us-central1", gke_cluster="prod", ar_repo="r")
    session.add(cluster)
    session.commit()
    app = App(name="clips-api", repo_full_name="acme/clips-api",
              installation_id=1, workload_class="web", status=AppStatus.ACTIVE)
    session.add(app)
    session.commit()
    session.add(Environment(app_id=app.id, name="prod",
                            cluster_id=cluster.id, namespace="clips"))
    session.commit()
    token = mint_agent_token(session, cluster)
    return cluster, app, token


def _profile() -> UsageProfile:
    return UsageProfile.model_validate(_fixture())


def test_ingest_appends_to_ledger(session, seeded):
    _, app, token = seeded
    row = ingest_usage(session, _profile(), token)
    assert row.app_id == app.id
    assert json.loads(row.cpu_json)["p95"] == "760m"


def test_ingest_triggers_recommendation(session, seeded):
    """The A2->A4 hook: the golden fixture is under-provisioned on cpu, so a
    single ingest should leave a live OVERRIDE recommendation behind."""
    from gojira.models import SizingRecommendation

    _, app, token = seeded
    row = ingest_usage(session, _profile(), token)
    rec = session.scalar(select(SizingRecommendation))
    assert rec is not None and rec.app_id == app.id
    assert rec.based_on_profile_id == row.id
    assert json.loads(rec.proposed_json)["cpu"]["request"] == "800m"
    assert "p95 760m" in rec.justification


def test_unknown_token_401(session, seeded):
    with pytest.raises(IngestError) as e:
        ingest_usage(session, _profile(), "not-a-token")
    assert e.value.status == 401


def test_token_scoped_to_its_cluster_403(session, seeded):
    """A token from cluster X can't report a profile claiming cluster Y."""
    other = Cluster(name="other-cluster", gcp_project="p", location="l",
                    gke_cluster="g", ar_repo="r")
    session.add(other)
    session.commit()
    other_token = mint_agent_token(session, other)
    with pytest.raises(IngestError) as e:
        ingest_usage(session, _profile(), other_token)  # profile says my-prod-cluster
    assert e.value.status == 403


def test_unknown_app_or_env_404(session, seeded):
    _, _, token = seeded
    p = _profile().model_copy(update={"app": "ghost"})
    with pytest.raises(IngestError) as e:
        ingest_usage(session, p, token)
    assert e.value.status == 404


def test_token_rotation_invalidates_old(session, seeded):
    cluster, _, old = seeded
    new = mint_agent_token(session, cluster)
    with pytest.raises(IngestError):
        ingest_usage(session, _profile(), old)
    assert ingest_usage(session, _profile(), new).id


def test_endpoint_round_trip(session_factory):
    """Through the real route: 201 + row landed; bad token 401; no auth 401."""
    with session_factory() as s:
        cluster = Cluster(name="my-prod-cluster", gcp_project="p", location="l",
                          gke_cluster="g", ar_repo="r")
        s.add(cluster)
        s.commit()
        app_row = App(name="clips-api", repo_full_name="acme/clips-api",
                      installation_id=1, workload_class="web", status=AppStatus.ACTIVE)
        s.add(app_row)
        s.commit()
        s.add(Environment(app_id=app_row.id, name="prod",
                          cluster_id=cluster.id, namespace="clips"))
        s.commit()
        token = mint_agent_token(s, cluster)

    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory()
    )
    client = TestClient(create_app(session_factory, clients=clients))

    r = client.post("/api/v1/usage", json=_fixture(),
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201, r.text
    with session_factory() as s:
        assert s.scalar(select(UsageProfileRow)).samples == 20160

    assert client.post("/api/v1/usage", json=_fixture()).status_code == 401
    assert client.post("/api/v1/usage", json=_fixture(),
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/v1/usage", json={"bad": "payload"},
                       headers={"Authorization": f"Bearer {token}"}).status_code == 422


def _fresh_profile() -> UsageProfile:
    return _profile()  # _fixture() already stamps reported_at to now (#36's lesson)


def _old_profile() -> UsageProfile:
    return _profile().model_copy(update={"reported_at": utcnow() - timedelta(days=40)})


def test_usage_history_reads_ledger_and_recommendation(session, seeded):
    """The read twin: every env listed (empty ones explicitly), profiles in
    the POSTed wire shape newest-first with their ids, the live recommendation
    pointing at the profile it was computed from."""
    cluster, app, token = seeded
    session.add(Environment(app_id=app.id, name="staging", cluster_id=cluster.id,
                            namespace="clips-staging"))
    session.commit()
    row = ingest_usage(session, _fresh_profile(), token)

    body = usage_history(session, app.id, since_days=30)
    assert body["name"] == "clips-api"
    assert list(body["environments"]) == ["prod", "staging"]
    prod = body["environments"]["prod"]
    assert prod["cluster"] == "my-prod-cluster"
    assert [p["profile_id"] for p in prod["profiles"]] == [row.id]
    profile = prod["profiles"][0]
    assert profile["cpu"]["p95"] == "760m"
    assert profile["reported_by"] == "gojira-sizing-agent/my-prod-cluster"
    assert UsageProfile.model_validate(profile)  # still the wire contract
    assert prod["recommendation"]["based_on_profile_id"] == row.id
    assert prod["recommendation"]["proposed"]["cpu"]["request"] == "800m"
    assert prod["recommendation"]["status"] == "open"
    staging = body["environments"]["staging"]
    assert staging["profiles"] == [] and staging["recommendation"] is None


def test_usage_history_env_filter_and_window(session, seeded):
    _, app, token = seeded
    ingest_usage(session, _fresh_profile(), token)
    ingest_usage(session, _old_profile(), token)  # 40 days back: outside 30d, inside 90d

    body = usage_history(session, app.id, env="prod", since_days=30)
    assert list(body["environments"]) == ["prod"]
    assert len(body["environments"]["prod"]["profiles"]) == 1
    assert len(usage_history(session, app.id, since_days=90)["environments"]["prod"]["profiles"]) == 2
    assert usage_history(session, app.id, env="ghost") is None
    assert usage_history(session, 999) is None


def test_usage_read_endpoint(session_factory):
    """GET /api/v1/apps/{id}/usage: platform bearer gate, 404 unknown app or
    env, 422 on a window the ledger can't answer."""
    from gojira.config import Config

    with session_factory() as s:
        cluster = Cluster(name="my-prod-cluster", gcp_project="p", location="l",
                          gke_cluster="g", ar_repo="r")
        s.add(cluster)
        s.commit()
        app_row = App(name="clips-api", repo_full_name="acme/clips-api",
                      installation_id=1, workload_class="web", status=AppStatus.ACTIVE)
        s.add(app_row)
        s.commit()
        s.add(Environment(app_id=app_row.id, name="prod",
                          cluster_id=cluster.id, namespace="clips"))
        s.commit()
        app_id = app_row.id
        ingest_usage(s, _fresh_profile(), mint_agent_token(s, cluster))

    cfg = Config()
    cfg.auth.api_token = "s3cr3t"
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory()
    )
    client = TestClient(create_app(session_factory, clients=clients, config=cfg))
    hdr = {"Authorization": "Bearer s3cr3t"}

    assert client.get(f"/api/v1/apps/{app_id}/usage").status_code == 401
    r = client.get(f"/api/v1/apps/{app_id}/usage", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["since_days"] == cfg.sizing.history_days
    assert body["environments"]["prod"]["profiles"][0]["memory"]["p99"] == "340Mi"
    assert client.get(f"/api/v1/apps/{app_id}/usage?env=prod&since_days=7",
                      headers=hdr).json()["since_days"] == 7
    assert client.get(f"/api/v1/apps/{app_id}/usage?env=ghost", headers=hdr).status_code == 404
    assert client.get("/api/v1/apps/999/usage", headers=hdr).status_code == 404
    assert client.get(f"/api/v1/apps/{app_id}/usage?since_days=0", headers=hdr).status_code == 422
    assert client.get(f"/api/v1/apps/{app_id}/usage?since_days=91", headers=hdr).status_code == 422