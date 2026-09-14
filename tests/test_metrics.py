"""RED metrics: every request lands in http_requests_total, /metrics serves them."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gojira import service
from gojira.config import Config
from gojira.models import Base
from gojira.web import create_app
from tests.fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/gojira.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory()
    )
    return TestClient(create_app(sf, clients=clients, config=Config()))


def test_requests_land_in_the_red_series(client):
    client.get("/healthz")
    body = client.get("/metrics").text
    # The observability layer's web profile queries these exact names.
    assert 'http_requests_total{method="GET",status="200"}' in body
    assert "http_request_duration_seconds_bucket" in body


def test_metrics_endpoint_does_not_count_itself(client):
    client.get("/metrics")
    body = client.get("/metrics").text
    assert 'http_requests_total{method="GET",status="200"}' not in body


def test_two_apps_coexist(tmp_path):
    # Per-app registry: a second create_app must not raise Duplicated timeseries.
    engine = create_engine(
        f"sqlite:///{tmp_path}/gojira.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory()
    )
    create_app(sf, clients=clients, config=Config())
    create_app(sf, clients=clients, config=Config())


def test_metrics_exempt_from_identity_gate(tmp_path):
    # The in-cluster scraper carries no IAP identity - /metrics answers anyway.
    engine = create_engine(
        f"sqlite:///{tmp_path}/gojira.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    cfg = Config()
    cfg.auth.enabled = True
    cfg.auth.allowed_domain = "fubo.tv"
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory()
    )
    c = TestClient(create_app(sf, clients=clients, config=cfg))
    r = c.get("/metrics", follow_redirects=True)
    assert r.status_code == 200
    assert "http_request_duration_seconds" in r.text


def test_unhandled_exception_counts_as_500(tmp_path):
    # An unhandled crash produces no response object - it must still reach
    # http_requests_total or a crashing app looks healthy to 5xx alerts.
    engine = create_engine(
        f"sqlite:///{tmp_path}/gojira.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory()
    )
    app = create_app(sf, clients=clients, config=Config())

    @app.get("/boom")
    def boom():
        raise RuntimeError("unhandled")

    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/boom").status_code == 500
    text = c.get("/metrics").text
    assert 'http_requests_total{method="GET",status="500"} 1.0' in text
