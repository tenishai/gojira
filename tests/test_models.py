"""Model behavior: relationships, constraints, ledger semantics."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from gojira.models import App, AppStatus, Base, Cluster, Event, ProvisionStep, StepStatus


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        yield s


@pytest.fixture
def cluster(session: Session) -> Cluster:
    c = Cluster(
        name="dev-cluster",
        gcp_project="example-project",
        location="us-central1",
        gke_cluster="dev-gke",
        ar_repo="us-central1-docker.pkg.dev/example-project/apps",
    )
    session.add(c)
    session.commit()
    return c


def make_app(cluster: Cluster, repo: str = "example/clips-api") -> App:
    return App(
        name="clips-api",
        repo_full_name=repo,
        installation_id=12345,
        cluster_id=cluster.id,
        namespace="clips",
        workload_class="web",
    )


def test_app_defaults_to_pending(session, cluster):
    app = make_app(cluster)
    session.add(app)
    session.commit()
    assert app.status == AppStatus.PENDING
    assert app.created_at is not None


def test_repo_can_only_be_connected_once(session, cluster):
    session.add(make_app(cluster))
    session.commit()
    session.add(make_app(cluster))
    with pytest.raises(IntegrityError):
        session.commit()


def test_step_ledger_unique_per_app(session, cluster):
    app = make_app(cluster)
    session.add(app)
    session.commit()
    session.add(ProvisionStep(app_id=app.id, step_name="ensure_gsa"))
    session.commit()
    session.add(ProvisionStep(app_id=app.id, step_name="ensure_gsa"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_step_name_allowed_across_apps(session, cluster):
    a1, a2 = make_app(cluster), make_app(cluster, repo="example/other")
    session.add_all([a1, a2])
    session.commit()
    session.add_all(
        [
            ProvisionStep(app_id=a1.id, step_name="ensure_gsa"),
            ProvisionStep(app_id=a2.id, step_name="ensure_gsa"),
        ]
    )
    session.commit()  # no conflict


def test_steps_ordered_by_id(session, cluster):
    app = make_app(cluster)
    session.add(app)
    session.commit()
    for name in ["ensure_gsa", "bind_wif_repo", "open_setup_pr"]:
        session.add(ProvisionStep(app_id=app.id, step_name=name, status=StepStatus.DONE))
        session.commit()
    session.refresh(app)
    assert [s.step_name for s in app.steps] == ["ensure_gsa", "bind_wif_repo", "open_setup_pr"]


def test_step_records_resource_ref(session, cluster):
    app = make_app(cluster)
    session.add(app)
    session.commit()
    step = ProvisionStep(
        app_id=app.id,
        step_name="ensure_gsa",
        status=StepStatus.DONE,
        resource_ref="clips-api-deploy@example-project.iam.gserviceaccount.com",
    )
    session.add(step)
    session.commit()
    assert "clips-api-deploy@" in app.steps[0].resource_ref


def test_event_without_app(session):
    session.add(Event(type="webhook.ping", payload_json="{}"))
    session.commit()


def test_cluster_relationship(session, cluster):
    app = make_app(cluster)
    session.add(app)
    session.commit()
    assert app.cluster.name == "dev-cluster"
    assert cluster.apps[0].name == "clips-api"
