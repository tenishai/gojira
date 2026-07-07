"""A6: the sizing panel renders evidence + recommendation; the button opens the PR."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gojira import service
from gojira.models import (
    App,
    AppStatus,
    Base,
    Cluster,
    Environment,
    RecommendationStatus,
    SizingRecommendation,
)
from gojira.sizing.profile import UsageProfile
from gojira.sizing.service import ingest_usage, mint_agent_token
from gojira.web import create_app
from tests.fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory
from tests.test_sizing_pr import GOJIRA_YAML
from tests.test_sizing_profile import _fixture


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/gojira.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class SpecRepoGitHub(FakeGitHubClient):
    """Clone drops a gojira.yaml so the sizing PR flow has a contract to edit."""

    def clone(self, repo, workdir):
        super().clone(repo, workdir)
        (workdir / "gojira.yaml").write_text(GOJIRA_YAML)


@pytest.fixture
def seeded(session_factory):
    """An active app with one ingested (under-provisioned) profile → live rec."""
    with session_factory() as s:
        cluster = Cluster(name="my-prod-cluster", gcp_project="p", location="l",
                          gke_cluster="g", ar_repo="r")
        s.add(cluster)
        s.commit()
        app = App(name="clips-api", repo_full_name="acme/clips-api",
                  installation_id=1, workload_class="web", status=AppStatus.ACTIVE)
        s.add(app)
        s.commit()
        s.add(Environment(app_id=app.id, name="prod",
                          cluster_id=cluster.id, namespace="clips"))
        s.commit()
        token = mint_agent_token(s, cluster)
        ingest_usage(s, UsageProfile.model_validate(_fixture()), token)
        app_id = app.id
    return app_id


@pytest.fixture
def client(session_factory):
    clients = service.ProvisionClients(
        gcp=FakeGCPClient(), github=SpecRepoGitHub(), kubectl_for=KubectlFactory()
    )
    return TestClient(create_app(session_factory, clients=clients))


def test_panel_renders_evidence_and_recommendation(client, seeded):
    r = client.get(f"/apps/{seeded}")
    assert r.status_code == 200
    assert "Sizing" in r.text
    assert "760m" in r.text                    # observed p95
    assert "250m → 800m" in r.text             # proposed request move
    assert "Open sizing PR" in r.text          # rec is open → button shows


def test_button_opens_pr_and_panel_links_it(client, seeded, session_factory):
    r = client.post(f"/apps/{seeded}/sizing/prod/open-pr", follow_redirects=False)
    assert r.status_code == 303

    with session_factory() as s:
        rec = s.query(SizingRecommendation).one()
        assert rec.status == RecommendationStatus.PR_OPENED
        assert rec.pr_url and "/pull/" in rec.pr_url

    page = client.get(f"/apps/{seeded}").text
    assert "sizing PR" in page and rec.pr_url in page
    assert "Open sizing PR" not in page        # button gone once PR exists


def test_open_pr_for_unknown_env_is_graceful(client, seeded):
    r = client.post(f"/apps/{seeded}/sizing/ghost/open-pr", follow_redirects=False)
    assert r.status_code == 303  # no-op redirect, no 500