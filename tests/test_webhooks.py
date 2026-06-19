"""Webhook ingestion: signature verification, event handlers, and the route.

Handlers are exercised directly with crafted payloads; the route is exercised
through TestClient with a real HMAC signature computed from the test secret.
"""

import hashlib
import hmac
import json

import pytest
from fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gojira import service, webhooks
from gojira.config import Config, GitHubConfig
from gojira.github import SETUP_BRANCH
from gojira.models import AppStatus, Base
from gojira.web import create_app

SECRET = "test-secret"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/gojira.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(session_factory):
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory()
    )
    config = Config(github=GitHubConfig(webhook_secret=SECRET))
    return TestClient(create_app(session_factory, clients=clients, config=config))


def _awaiting_app(session_factory, repo="acme/web", status=AppStatus.AWAITING_PR) -> int:
    """Connect an app and force it to the given status. Returns its id."""
    with session_factory() as s:
        c = service.register_cluster(
            s, name="dev", gcp_project="p", location="us-central1", gke_cluster="g"
        )
        app = service.connect_app(
            s, name="web", repo_full_name=repo, workload_class="web",
            env_map={"dev": (c.id, "web")},
        )
        app.status = status
        s.commit()
        return app.id


# --- payload builders --------------------------------------------------------


def pr_payload(repo, *, branch=SETUP_BRANCH, merged=True, action="closed"):
    return {
        "action": action,
        "pull_request": {"merged": merged, "head": {"ref": branch}},
        "repository": {"full_name": repo},
    }


def wr_payload(repo, name, *, conclusion="success", action="completed", ref="v1.2.3"):
    return {
        "action": action,
        "workflow_run": {
            "name": name,
            "conclusion": conclusion,
            "head_branch": ref,
            "head_sha": "abc123",
            "html_url": "https://github.com/acme/web/actions/runs/1",
        },
        "repository": {"full_name": repo},
    }


# --- signature verification --------------------------------------------------


def test_verify_signature_roundtrip():
    body = b'{"hello":"world"}'
    assert webhooks.verify_signature(SECRET, body, sign(body)) is True


def test_verify_signature_rejects_tampered_body():
    assert webhooks.verify_signature(SECRET, b"tampered", sign(b"original")) is False


def test_verify_signature_fails_closed_without_secret_or_header():
    body = b"{}"
    assert webhooks.verify_signature("", body, sign(body)) is False
    assert webhooks.verify_signature(SECRET, body, None) is False


# --- pull_request handler ----------------------------------------------------


def test_merged_setup_pr_activates_app(session_factory):
    app_id = _awaiting_app(session_factory)
    with session_factory() as s:
        result = webhooks.handle_event(s, "pull_request", pr_payload("acme/web"))
        assert "activated" in result
        view = service.app_status(s, app_id)
        assert view.app.status == "active"
        assert any(e.type == "app.activated" for e in view.events)


def test_merged_non_setup_branch_ignored(session_factory):
    app_id = _awaiting_app(session_factory)
    with session_factory() as s:
        assert webhooks.handle_event(s, "pull_request", pr_payload("acme/web", branch="feature")) is None
        assert service.app_status(s, app_id).app.status == "awaiting_pr"


def test_unmerged_setup_pr_ignored(session_factory):
    app_id = _awaiting_app(session_factory)
    with session_factory() as s:
        payload = pr_payload("acme/web", merged=False)
        assert webhooks.handle_event(s, "pull_request", payload) is None
        assert service.app_status(s, app_id).app.status == "awaiting_pr"


def test_pull_request_unknown_repo_is_noop(session_factory):
    _awaiting_app(session_factory)
    with session_factory() as s:
        assert webhooks.handle_event(s, "pull_request", pr_payload("someone/else")) is None


# --- workflow_run handler ----------------------------------------------------


def test_promote_success_records_event(session_factory):
    app_id = _awaiting_app(session_factory, status=AppStatus.ACTIVE)
    with session_factory() as s:
        result = webhooks.handle_event(s, "workflow_run", wr_payload("acme/web", "Gojira Promote"))
        assert result == "promote.success for web"
        events = service.app_status(s, app_id).events
        promote = next(e for e in events if e.type == "promote.success")
        assert json.loads(promote.payload_json)["ref"] == "v1.2.3"


def test_ci_failure_records_event(session_factory):
    app_id = _awaiting_app(session_factory, status=AppStatus.ACTIVE)
    with session_factory() as s:
        result = webhooks.handle_event(
            s, "workflow_run", wr_payload("acme/web", "Gojira CI", conclusion="failure")
        )
        assert result == "ci.failure for web"
        assert any(e.type == "ci.failure" for e in service.app_status(s, app_id).events)


def test_in_progress_workflow_run_ignored(session_factory):
    _awaiting_app(session_factory, status=AppStatus.ACTIVE)
    with session_factory() as s:
        payload = wr_payload("acme/web", "Gojira CI", action="requested")
        assert webhooks.handle_event(s, "workflow_run", payload) is None


def test_unrelated_workflow_ignored(session_factory):
    _awaiting_app(session_factory, status=AppStatus.ACTIVE)
    with session_factory() as s:
        assert webhooks.handle_event(s, "workflow_run", wr_payload("acme/web", "CodeQL")) is None


def test_unknown_event_type_ignored(session_factory):
    with session_factory() as s:
        assert webhooks.handle_event(s, "star", {"repository": {"full_name": "acme/web"}}) is None


# --- route -------------------------------------------------------------------


def test_route_rejects_bad_signature(client):
    r = client.post(
        "/webhook/github",
        content=json.dumps(pr_payload("acme/web")),
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=deadbeef"},
    )
    assert r.status_code == 401


def test_route_rejects_invalid_json(client):
    body = b"not json"
    r = client.post(
        "/webhook/github",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(body)},
    )
    assert r.status_code == 400


def test_route_valid_pull_request_activates(client, session_factory):
    app_id = _awaiting_app(session_factory)
    body = json.dumps(pr_payload("acme/web")).encode()
    r = client.post(
        "/webhook/github",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sign(body)},
    )
    assert r.status_code == 202
    assert r.json()["result"].endswith("activated")
    with session_factory() as s:
        assert service.app_status(s, app_id).app.status == "active"


def test_route_acks_ignored_event(client):
    body = json.dumps({"zen": "ping"}).encode()
    r = client.post(
        "/webhook/github",
        content=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sign(body)},
    )
    assert r.status_code == 202
    assert r.json()["result"] is None
