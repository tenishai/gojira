"""Sizing tables: evidence ledger round-trip, one-live-recommendation rule,
per-cluster token uniqueness."""

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from gojira.models import (
    AgentToken,
    App,
    Base,
    Cluster,
    RecommendationKind,
    RecommendationStatus,
    SizingRecommendation,
    UsageProfileRow,
)
from gojira.sizing.profile import UsageProfile

from tests.test_sizing_profile import _fixture


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def app(session):
    a = App(name="clips-api", repo_full_name="acme/clips-api",
            installation_id=1, workload_class="web")
    session.add(a)
    session.commit()
    return a


def _row_from_profile(app_id: int, p: UsageProfile) -> UsageProfileRow:
    return UsageProfileRow(
        app_id=app_id,
        environment=p.environment,
        window_days=p.window_days,
        samples=p.samples,
        cpu_json=p.cpu.model_dump_json(),
        memory_json=p.memory.model_dump_json(),
        restarts=p.restarts,
        oom_kills=p.oom_kills,
        reported_by=p.reported_by,
        reported_at=p.reported_at.replace(tzinfo=None),
    )


def test_profile_wire_to_row_round_trip(session, app):
    """The golden fixture survives wire → model → row → read-back."""
    p = UsageProfile.model_validate(_fixture())
    session.add(_row_from_profile(app.id, p))
    session.commit()

    row = session.scalar(select(UsageProfileRow).where(UsageProfileRow.app_id == app.id))
    assert row.environment == "prod" and row.samples == 20160
    assert json.loads(row.cpu_json)["p99"] == "820m"
    assert row.received_at is not None  # server default stamped


def test_one_live_recommendation_per_app_env_kind(session, app):
    def rec():
        return SizingRecommendation(
            app_id=app.id, environment="prod", kind=RecommendationKind.OVERRIDE,
            current_json="{}", proposed_json="{}", justification="p95 evidence",
        )

    session.add(rec())
    session.commit()
    session.add(rec())
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    # Recompute updates in place instead of stacking.
    existing = session.scalar(select(SizingRecommendation))
    existing.status = RecommendationStatus.PR_OPENED
    existing.pr_url = "https://github.com/acme/clips-api/pull/9"
    session.commit()
    assert session.scalar(select(SizingRecommendation)).pr_url.endswith("/9")


def test_agent_token_unique_per_cluster(session):
    c = Cluster(name="sandbox", gcp_project="p", location="us-central1",
                gke_cluster="g", ar_repo="r")
    session.add(c)
    session.commit()

    session.add(AgentToken(cluster_id=c.id, token_sha256="a" * 64))
    session.commit()
    session.add(AgentToken(cluster_id=c.id, token_sha256="b" * 64))
    with pytest.raises(IntegrityError):
        session.commit()
