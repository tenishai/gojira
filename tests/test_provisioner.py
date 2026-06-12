"""Engine semantics: ordering, skip, halt-on-failure, resume, ledger truth."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gojira.config import Config
from gojira.models import App, AppStatus, Base, Cluster, Event, ProvisionStep, StepStatus
from gojira.provisioner import ProvisionContext, ProvisionEngine


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
    cluster = Cluster(
        name="dev",
        gcp_project="example-project",
        location="us-central1",
        gke_cluster="dev-gke",
        ar_repo="us-central1-docker.pkg.dev/example-project/apps",
    )
    session.add(cluster)
    session.commit()
    app = App(
        name="clips-api",
        repo_full_name="example/clips-api",
        installation_id=1,
        cluster_id=cluster.id,
        namespace="clips",
        workload_class="web",
    )
    session.add(app)
    session.commit()
    return ProvisionContext(app=app, cluster=cluster, config=Config())


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
    assert ctx.app.status == AppStatus.AWAITING_PR
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
    assert "apply:a" not in calls  # check short-circuits apply
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
    assert ctx.app.status == AppStatus.FAILED
    assert "check:c" not in calls  # c never ran
    assert ledger(session, ctx.app, "b").status == StepStatus.FAILED
    assert "exploded" in ledger(session, ctx.app, "b").error
    assert ledger(session, ctx.app, "c") is None  # no ledger row for unreached step


def test_resume_after_failure(session, ctx):
    calls = []
    flaky = FakeStep("b", calls, fail=True)
    steps = [FakeStep("a", calls), flaky, FakeStep("c", calls)]
    engine = ProvisionEngine(steps)

    assert not engine.run(session, ctx).completed

    flaky.fail = False  # operator fixed the underlying issue
    calls.clear()
    result = engine.run(session, ctx)

    assert result.completed
    assert calls == ["check:b", "apply:b", "check:c", "apply:c"]  # a not re-run
    assert result.steps_run == ["b", "c"]
    assert ctx.app.status == AppStatus.AWAITING_PR
    assert ledger(session, ctx.app, "b").error is None  # cleared on retry


def test_rerun_completed_is_noop(session, ctx):
    calls = []
    engine = ProvisionEngine([FakeStep("a", calls)])
    engine.run(session, ctx)
    calls.clear()

    result = engine.run(session, ctx)
    assert result.completed
    assert calls == []  # nothing re-executed
    assert result.steps_run == []


def test_events_emitted(session, ctx):
    engine = ProvisionEngine([FakeStep("a", [], fail=True)])
    engine.run(session, ctx)

    types = [e.type for e in session.scalars(select(Event)).all()]
    assert "provision.run_started" in types
    assert "provision.step_failed" in types
    assert "provision.run_completed" not in types


def test_duplicate_step_names_rejected():
    calls = []
    with pytest.raises(ValueError, match="duplicate"):
        ProvisionEngine([FakeStep("a", calls), FakeStep("a", calls)])
