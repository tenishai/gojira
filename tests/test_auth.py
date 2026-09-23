"""IAP-gate middleware: identity in, allowlist decisions, exempt machine routes."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gojira import service
from gojira.config import Config
from gojira.models import Base
from gojira.web import create_app
from gojira.web.auth import allowed
from gojira.config import AuthConfig
from tests.fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory

IAP = "x-goog-authenticated-user-email"


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/gojira.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    cfg = Config()
    cfg.auth.enabled = True
    cfg.auth.allowed_domain = "example.com"
    cfg.auth.allowed_emails = ["guest@example.com"]
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory()
    )
    return TestClient(create_app(sf, clients=clients, config=cfg))


def test_no_identity_is_401(client):
    assert client.get("/").status_code == 401


def test_domain_user_admitted(client):
    r = client.get("/", headers={IAP: "accounts.google.com:someone@example.com"})
    assert r.status_code == 200


def test_explicit_email_admitted_and_stranger_denied(client):
    assert client.get("/", headers={IAP: "guest@example.com"}).status_code == 200
    assert client.get("/", headers={IAP: "intruder@evil.dev"}).status_code == 403


def test_machine_routes_exempt(client):
    # No IAP identity: webhook + usage API must still reach their own auth
    # (which then rejects them for their own reasons — not 401-from-IAP-gate).
    r = client.post("/api/v1/usage", json={})
    assert r.status_code != 401 or "identity" not in r.text
    r2 = client.post("/webhook/github", content=b"{}")
    assert r2.status_code != 401 or "identity" not in r2.text


def test_disabled_auth_changes_nothing(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/g2.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory()
    )
    c = TestClient(create_app(sf, clients=clients, config=Config()))
    assert c.get("/").status_code == 200  # no header, no gate


def test_allowlist_logic():
    a = AuthConfig(enabled=True, allowed_domain="example.com", allowed_emails=["X@Ex.com"])
    assert allowed("user@example.com", a)
    assert allowed("x@ex.com", a)          # case-insensitive
    assert not allowed("user@example.com.evil.com", a)
    assert not allowed("", a)


def test_healthz_exempt_from_identity(client):
    """Finding #17: kubelet probes carry no IAP header — the auth gate must
    never make the platform unready under its own protection."""
    assert client.get("/healthz").status_code == 200


def test_exempt_paths_match_on_segment_boundaries():
    from gojira.web.auth import _exempt

    assert _exempt("/metrics") and _exempt("/healthz") and _exempt("/webhook/x")
    assert _exempt("/metrics/") and _exempt("/api/v1/usage")
    # lookalikes never slip the identity gate
    assert not _exempt("/metricsfoo") and not _exempt("/healthzz")
