"""init_db against a pre-existing, older-schema database.

Base.metadata.create_all only creates tables that don't exist yet — it never
alters an existing table's columns. A DB provisioned before
apps.auto_merge_setup_pr was added would otherwise 500 on every App query
(sqlite3.OperationalError: no such column) the moment the new code rolls out
against it. init_db must detect and backfill that column."""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from gojira.db import init_db
from gojira.models import App


def _make_old_schema_engine(tmp_path):
    """A DB whose apps table predates auto_merge_setup_pr — hand-built with
    raw SQL rather than Base.metadata so it reflects a real pre-migration
    rollout, not just "the ORM minus one mapped attribute"."""
    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE apps (
                id INTEGER PRIMARY KEY,
                name VARCHAR(63) NOT NULL,
                repo_full_name VARCHAR(255) NOT NULL,
                installation_id INTEGER NOT NULL,
                workload_class VARCHAR(16) NOT NULL,
                status VARCHAR(16) NOT NULL,
                setup_pr_url VARCHAR(512),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(
            "INSERT INTO apps (name, repo_full_name, installation_id, "
            "workload_class, status) VALUES "
            "('clips-api', 'acme/clips-api', 1, 'web', 'active')"
        ))
    return engine


def test_init_db_backfills_missing_cluster_access_column(tmp_path):
    """A clusters table from before Cluster.access: the column is added with
    'dns', the behaviour every registered cluster had."""
    from gojira.models import Cluster

    engine = create_engine(f"sqlite:///{tmp_path}/old-clusters.db")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE clusters (
                id INTEGER PRIMARY KEY,
                name VARCHAR(63) NOT NULL UNIQUE,
                gcp_project VARCHAR(63) NOT NULL,
                location VARCHAR(63) NOT NULL,
                gke_cluster VARCHAR(63) NOT NULL,
                ar_repo VARCHAR(255) NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text(
            "INSERT INTO clusters (name, gcp_project, location, gke_cluster, ar_repo) VALUES "
            "('dev', 'acme-dev', 'us-central1', 'dev-gke', 'us-central1-docker.pkg.dev/acme-dev/apps')"
        ))
    init_db(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    with sf() as session:
        assert session.query(Cluster).filter_by(name="dev").one().access == "dns"


def test_init_db_backfills_missing_auto_merge_column(tmp_path):
    engine = _make_old_schema_engine(tmp_path)

    init_db(engine)

    sf = sessionmaker(bind=engine, expire_on_commit=False)
    with sf() as session:
        app = session.query(App).filter_by(repo_full_name="acme/clips-api").one()
        assert app.auto_merge_setup_pr is False


def test_init_db_column_backfill_is_idempotent(tmp_path):
    """Running init_db twice against the same (already-migrated) DB must not
    raise (e.g. 'duplicate column name')."""
    engine = _make_old_schema_engine(tmp_path)

    init_db(engine)
    init_db(engine)  # must not raise

    sf = sessionmaker(bind=engine, expire_on_commit=False)
    with sf() as session:
        app = session.query(App).filter_by(repo_full_name="acme/clips-api").one()
        assert app.auto_merge_setup_pr is False


def test_init_db_on_fresh_db_still_creates_all_tables(tmp_path):
    """New installs (no existing tables at all) must keep working unchanged."""
    engine = create_engine(f"sqlite:///{tmp_path}/fresh.db")

    init_db(engine)

    sf = sessionmaker(bind=engine, expire_on_commit=False)
    with sf() as session:
        assert session.query(App).count() == 0
