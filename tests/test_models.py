"""Model behavior: relationships, constraints, ledger semantics."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from gojira.models import (
    App,
    AppStatus,
    Base,
    Cluster,
    Environment,
    Event,
    ProvisionStep,
    StepStatus,
)


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


def make_app(repo: str = "example/clips-api") -> App:
    return App(
        name="clips-api",
        repo_full_name=repo,
        installation_id=12345,
        workload_class="web",
    )


def add_env(session, app, cluster, name="dev", namespace="clips") -> Environment:
    env = Environment(app_id=app.id, name=name, cluster_id=cluster.id, namespace=namespace)
    session.add(env)
    session.commit()
    return env


def test_app_defaults_to_pending(session):
    app = make_app()
    session.add(app)
    session.commit()
    assert app.status == AppStatus.PENDING
    assert app.created_at is not None


def test_repo_can_only_be_connected_once(session):
    session.add(make_app())
    session.commit()
    session.add(make_app())
    with pytest.raises(IntegrityError):
        session.commit()


def test_step_ledger_unique_per_app(session):
    app = make_app()
    session.add(app)
    session.commit()
    session.add(ProvisionStep(app_id=app.id, step_name="ensure_gsa@p"))
    session.commit()
    session.add(ProvisionStep(app_id=app.id, step_name="ensure_gsa@p"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_step_name_allowed_across_apps(session):
    a1, a2 = make_app(), make_app(repo="example/other")
    session.add_all([a1, a2])
    session.commit()
    session.add_all(
        [
            ProvisionStep(app_id=a1.id, step_name="ensure_gsa@p"),
            ProvisionStep(app_id=a2.id, step_name="ensure_gsa@p"),
        ]
    )
    session.commit()  # no conflict


def test_scoped_step_names_coexist_for_one_app(session):
    """The same step run for two projects keeps two ledger rows."""
    app = make_app()
    session.add(app)
    session.commit()
    session.add_all(
        [
            ProvisionStep(app_id=app.id, step_name="ensure_service_account@proj-a"),
            ProvisionStep(app_id=app.id, step_name="ensure_service_account@proj-b"),
        ]
    )
    session.commit()  # distinct scope keys -> distinct rows, no conflict


def test_steps_ordered_by_id(session):
    app = make_app()
    session.add(app)
    session.commit()
    for name in ["ensure_gsa", "bind_wif_repo", "open_setup_pr"]:
        session.add(ProvisionStep(app_id=app.id, step_name=name, status=StepStatus.DONE))
        session.commit()
    session.refresh(app)
    assert [s.step_name for s in app.steps] == ["ensure_gsa", "bind_wif_repo", "open_setup_pr"]


def test_step_records_resource_ref(session):
    app = make_app()
    session.add(app)
    session.commit()
    session.add(
        ProvisionStep(
            app_id=app.id,
            step_name="ensure_gsa",
            status=StepStatus.DONE,
            resource_ref="clips-api-deploy@example-project.iam.gserviceaccount.com",
        )
    )
    session.commit()
    assert "clips-api-deploy@" in app.steps[0].resource_ref


def test_event_without_app(session):
    session.add(Event(type="webhook.ping", payload_json="{}"))
    session.commit()


def test_environment_relationships(session, cluster):
    app = make_app()
    session.add(app)
    session.commit()
    env = add_env(session, app, cluster, name="dev")
    assert app.environments[0].name == "dev"
    assert env.cluster.name == "dev-cluster"
    assert env.app.name == "clips-api"
    assert cluster.environments[0].namespace == "clips"


def test_environment_name_unique_per_app(session, cluster):
    app = make_app()
    session.add(app)
    session.commit()
    add_env(session, app, cluster, name="dev")
    session.add(Environment(app_id=app.id, name="dev", cluster_id=cluster.id, namespace="other"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_app_cascades_environments(session, cluster):
    app = make_app()
    session.add(app)
    session.commit()
    add_env(session, app, cluster, name="dev")
    session.delete(app)
    session.commit()
    assert session.query(Environment).count() == 0
