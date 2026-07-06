"""Engine semantics: ordering, skip, halt, resume, scoping, ledger truth.

The engine is scope-agnostic and does not touch app status or emit run
lifecycle events — those are the orchestrator's job (covered in test_steps.py).
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gojira.config import Config
from gojira.models import App, Base, Cluster, Environment, Event, ProvisionStep, StepStatus
from gojira.provisioner import (
    ProvisionContext,
    ProvisionEngine,
    distinct_projects,
    order_environments,
)


class FakeStep:
    """Configurable test step. Records call order in a shared log."""

    def __init__(self, name, calls, exists=False, fail=False):
        self.name = name
        self.calls = calls
        self.exists = exists
        self.fail = fail

    def check(self, ctx):
        self.calls.append(f"check:{self.name}")
        return f"ref:{self.name}" if self.exists else None

    def apply(self, ctx):
        self.calls.append(f"apply:{self.name}")
        if self.fail:
            raise RuntimeError(f"{self.name} exploded")
        return f"ref:{self.name}"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def ctx(session):
    app = App(
        name="clips-api",
        repo_full_name="example/clips-api",
        installation_id=1,
        workload_class="web",
    )
    session.add(app)
    session.commit()
    return ProvisionContext(app=app, config=Config())


def ledger(session, app, name) -> ProvisionStep:
    return session.scalar(
        select(ProvisionStep).where(
            ProvisionStep.app_id == app.id, ProvisionStep.step_name == name
        )
    )


def test_happy_path(session, ctx):
    calls = []
    engine = ProvisionEngine([FakeStep("a", calls), FakeStep("b", calls)])
    result = engine.run(session, ctx)

    assert result.completed
    assert result.steps_run == ["a", "b"]
    assert calls == ["check:a", "apply:a", "check:b", "apply:b"]
    for name in ("a", "b"):
        row = ledger(session, ctx.app, name)
        assert row.status == StepStatus.DONE
        assert row.resource_ref == f"ref:{name}"
        assert row.started_at is not None and row.finished_at is not None


def test_existing_resource_skipped(session, ctx):
    calls = []
    engine = ProvisionEngine([FakeStep("a", calls, exists=True), FakeStep("b", calls)])
    result = engine.run(session, ctx)

    assert result.completed
    assert result.steps_skipped == ["a"]
    assert result.steps_run == ["b"]
    assert "apply:a" not in calls
    assert ledger(session, ctx.app, "a").status == StepStatus.SKIPPED
    assert ledger(session, ctx.app, "a").resource_ref == "ref:a"


def test_failure_halts_run(session, ctx):
    calls = []
    engine = ProvisionEngine(
        [FakeStep("a", calls), FakeStep("b", calls, fail=True), FakeStep("c", calls)]
    )
    result = engine.run(session, ctx)

    assert not result.completed
    assert result.failed_step == "b"
    assert "exploded" in result.error
    assert "check:c" not in calls
    assert ledger(session, ctx.app, "b").status == StepStatus.FAILED
    assert "exploded" in ledger(session, ctx.app, "b").error
    assert ledger(session, ctx.app, "c") is None


def test_resume_after_failure(session, ctx):
    calls = []
    flaky = FakeStep("b", calls, fail=True)
    engine = ProvisionEngine([FakeStep("a", calls), flaky, FakeStep("c", calls)])

    assert not engine.run(session, ctx).completed

    flaky.fail = False
    calls.clear()
    result = engine.run(session, ctx)

    assert result.completed
    assert calls == ["check:b", "apply:b", "check:c", "apply:c"]  # a not re-run
    assert result.steps_run == ["b", "c"]
    assert ledger(session, ctx.app, "b").error is None


def test_rerun_completed_is_noop(session, ctx):
    calls = []
    engine = ProvisionEngine([FakeStep("a", calls)])
    engine.run(session, ctx)
    calls.clear()

    result = engine.run(session, ctx)
    assert result.completed
    assert calls == []
    assert result.steps_run == []


def test_scope_key_scopes_ledger_rows(session, ctx):
    """The same step run under two scopes keeps two independent ledger rows."""
    engine = ProvisionEngine([FakeStep("ensure_sa", [])])
    engine.run(session, ctx, scope_key="proj-a")
    engine.run(session, ctx, scope_key="proj-b")

    assert ledger(session, ctx.app, "ensure_sa@proj-a").status == StepStatus.DONE
    assert ledger(session, ctx.app, "ensure_sa@proj-b").status == StepStatus.DONE
    assert ledger(session, ctx.app, "ensure_sa") is None  # unscoped row never created


def test_engine_emits_step_failed_only(session, ctx):
    """The engine emits the granular failure event but no run lifecycle events."""
    ProvisionEngine([FakeStep("a", [], fail=True)]).run(session, ctx)
    types = [e.type for e in session.scalars(select(Event)).all()]
    assert types == ["provision.step_failed"]


def test_duplicate_step_names_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        ProvisionEngine([FakeStep("a", []), FakeStep("a", [])])


# --- scope derivation helpers ------------------------------------------------


def _app_with_envs(session, env_specs):
    """env_specs: list of (name, project). Returns the app with environments."""
    app = App(name="clips-api", repo_full_name="x/clips-api", installation_id=1, workload_class="web")
    session.add(app)
    session.commit()
    for name, project in env_specs:
        cluster = Cluster(
            name=f"{name}-cluster",
            gcp_project=project,
            location="us-central1",
            gke_cluster=f"{name}-gke",
            ar_repo=f"us-central1-docker.pkg.dev/{project}/apps",
        )
        session.add(cluster)
        session.commit()
        session.add(
            Environment(app_id=app.id, name=name, cluster_id=cluster.id, namespace="clips")
        )
        session.commit()
    return app


def test_order_environments_follows_config(session):
    # insert out of order; expect dev, qa, prod
    app = _app_with_envs(session, [("prod", "p3"), ("dev", "p1"), ("qa", "p2")])
    ordered = order_environments(app.environments, Config())
    assert [e.name for e in ordered] == ["dev", "qa", "prod"]


def test_distinct_projects_dedupes_same_project(session):
    # dev + qa share a project, prod separate -> two distinct project scopes
    app = _app_with_envs(session, [("dev", "shared"), ("qa", "shared"), ("prod", "prod-proj")])
    scopes = distinct_projects(order_environments(app.environments, Config()))
    assert [s.project for s in scopes] == ["shared", "prod-proj"]


def test_ephemeral_step_reruns_on_resume(session, ctx):
    """A DONE ephemeral step re-runs on retry — its workdir died with the run.

    Regression: first dogfood retry skipped prepare_workdir/generate_artifacts
    (ledger DONE from run 1) and open_setup_pr ran `git add` in an empty fresh
    workdir: 'fatal: not a git repository'.
    """
    calls = []
    prepare = FakeStep("prepare_workdir", calls)
    prepare.ephemeral = True
    push = FakeStep("open_setup_pr", calls, fail=True)  # run 1 fails here

    engine = ProvisionEngine([prepare, push])
    r1 = engine.run(session, ctx)
    assert not r1.completed and r1.failed_step == "open_setup_pr"
    assert "apply:prepare_workdir" in calls

    calls.clear()
    push.fail = False
    r2 = engine.run(session, ctx)
    assert r2.completed
    # The ephemeral step ran again (fresh workdir); the durable skip semantics
    # for non-ephemeral steps are covered by the existing resume tests.
    assert "check:prepare_workdir" in calls and "apply:prepare_workdir" in calls
    assert "apply:open_setup_pr" in calls


def test_ephemeral_step_can_still_skip_via_check(session, ctx):
    """Ephemeral means 'don't trust the ledger' — check() still decides."""
    calls = []
    prepare = FakeStep("prepare_workdir", calls, exists=True)  # workdir intact
    prepare.ephemeral = True
    engine = ProvisionEngine([prepare])

    assert engine.run(session, ctx).completed
    calls.clear()
    assert engine.run(session, ctx).completed
    assert "check:prepare_workdir" in calls and "apply:prepare_workdir" not in calls
