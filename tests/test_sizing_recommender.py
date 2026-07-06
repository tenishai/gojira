"""PercentileRecommender: the rule, its guardrails, and upsert semantics.

The golden fixture describes an UNDER-provisioned app (cpu p95 760m vs request
250m) whose memory fits fine — so out of the box it yields exactly one
proposal (cpu up), which doubles as the ingest-integration expectation.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gojira.config import SizingConfig
from gojira.models import (
    App,
    Base,
    Cluster,
    Environment,
    RecommendationStatus,
    SizingRecommendation,
)
from gojira.sizing.profile import UsageProfile
from gojira.sizing.recommender import (
    PercentileRecommender,
    fmt_cpu,
    fmt_mem,
    recompute_for_app,
    upsert_recommendation,
)
from gojira.sizing.store import DbSizingStore
from tests.test_sizing_profile import _fixture


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def app_env(session):
    c = Cluster(name="my-prod-cluster", gcp_project="p", location="l",
                gke_cluster="g", ar_repo="r")
    session.add(c)
    session.commit()
    a = App(name="clips-api", repo_full_name="acme/clips-api",
            installation_id=1, workload_class="web")
    session.add(a)
    session.commit()
    session.add(Environment(app_id=a.id, name="prod", cluster_id=c.id, namespace="n"))
    session.commit()
    return a


def _profile(**patch) -> UsageProfile:
    return UsageProfile.model_validate({**_fixture(), **patch})


def _store_with(session, app, profile) -> DbSizingStore:
    store = DbSizingStore(session)
    store.record(app.id, profile)
    return store


def test_fmt_helpers():
    assert fmt_cpu(750) == "750m" and fmt_cpu(2000) == "2"
    assert fmt_mem(768 * 1024**2) == "768Mi" and fmt_mem(2 * 1024**3) == "2Gi"


def test_underprovisioned_cpu_gets_upsize(session, app_env):
    store = _store_with(session, app_env, _profile())
    proposals = PercentileRecommender().recommend("clips-api", "prod", store)
    cpu = {p.resource: p for p in proposals}["cpu"]
    # p95=760m rounds up to 800m; limit = p99 820m * 1.5 = 1230 -> 1250m
    assert cpu.proposed_request == "800m"
    assert cpu.proposed_limit == "1250m"
    assert "p95 760m" in cpu.evidence


def test_within_threshold_proposes_nothing(session, app_env):
    fits = _profile(cpu={"request": "250m", "limit": "500m", "p50": "200m",
                         "p95": "260m", "p99": "280m", "max": "300m"},
                    memory={"request": "256Mi", "limit": "512Mi", "p50": "200Mi",
                            "p95": "250Mi", "p99": "255Mi", "max": "256Mi"})
    store = _store_with(session, app_env, fits)
    assert PercentileRecommender().recommend("clips-api", "prod", store) == []


def test_memory_downsize_suppressed_by_oom(session, app_env):
    """Over-provisioned memory, but the app OOMed — evidence over wishes."""
    oomy = _profile(oom_kills=2,
                    memory={"request": "2Gi", "limit": "4Gi", "p50": "200Mi",
                            "p95": "300Mi", "p99": "340Mi", "max": "402Mi"})
    store = _store_with(session, app_env, oomy)
    proposals = PercentileRecommender().recommend("clips-api", "prod", store)
    assert "memory" not in {p.resource for p in proposals}


def test_memory_downsize_allowed_when_healthy(session, app_env):
    fat = _profile(memory={"request": "2Gi", "limit": "4Gi", "p50": "200Mi",
                           "p95": "300Mi", "p99": "340Mi", "max": "402Mi"})
    store = _store_with(session, app_env, fat)
    mem = {p.resource: p
           for p in PercentileRecommender().recommend("clips-api", "prod", store)}["memory"]
    assert mem.proposed_request == "320Mi"  # p95 300Mi rounded up to 64Mi step


def test_no_history_no_proposals(session, app_env):
    assert PercentileRecommender().recommend("clips-api", "prod", DbSizingStore(session)) == []


def test_upsert_supersedes_and_goes_stale(session, app_env):
    store = _store_with(session, app_env, _profile())
    rec = recompute_for_app(session, app_env, "prod", store)
    assert rec.status == RecommendationStatus.OPEN
    first_id = rec.id

    # Recompute with the same evidence: same row, refreshed, not stacked.
    rec2 = recompute_for_app(session, app_env, "prod", store)
    assert rec2.id == first_id
    assert session.scalar(select(SizingRecommendation)).id == first_id

    # Evidence dries up (empty proposals) -> the open row goes stale.
    rec3 = upsert_recommendation(session, app_env.id, "prod", [])
    assert rec3.status == RecommendationStatus.STALE


def test_pr_opened_status_survives_recompute(session, app_env):
    store = _store_with(session, app_env, _profile())
    rec = recompute_for_app(session, app_env, "prod", store)
    rec.status = RecommendationStatus.PR_OPENED
    rec.pr_url = "https://github.com/acme/clips-api/pull/12"
    session.commit()

    rec2 = recompute_for_app(session, app_env, "prod", store)
    assert rec2.status == RecommendationStatus.PR_OPENED  # PR stays the artifact
    assert rec2.pr_url.endswith("/12")


def test_materiality_knob(session, app_env):
    """A 10%-threshold config proposes what the 30% default ignores."""
    slight = _profile(cpu={"request": "250m", "limit": "500m", "p50": "200m",
                           "p95": "290m", "p99": "300m", "max": "310m"},
                      memory={"request": "256Mi", "limit": "512Mi", "p50": "200Mi",
                              "p95": "250Mi", "p99": "255Mi", "max": "256Mi"})
    store = _store_with(session, app_env, slight)
    assert PercentileRecommender().recommend("clips-api", "prod", store) == []
    eager = PercentileRecommender(SizingConfig(materiality_pct=10.0))
    assert {p.resource for p in eager.recommend("clips-api", "prod", store)} == {"cpu"}