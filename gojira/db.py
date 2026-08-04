"""Database engine and session factory. SQLite by default, Postgres via DATABASE_URL."""

import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from gojira.config import Config
from gojira.models import Base

log = logging.getLogger(__name__)


def make_engine(config: Config):
    url = config.database.url
    kwargs = {}
    if url.startswith("sqlite"):
        # FastAPI handlers may touch the session from different threads
        kwargs["connect_args"] = {"check_same_thread": False}
    log.debug("creating engine for %s", url.split("://")[0])
    return create_engine(url, **kwargs)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine) -> None:
    """Create all tables. Idempotent.

    create_all only creates tables that don't exist yet — it never alters an
    existing table's columns, so a DB provisioned before a column was added
    to models.py would otherwise 500 on the first query that touches it. This
    is a lightweight, dialect-agnostic (SQLite + Postgres) backfill for that
    case; add a line here whenever a column is added to an existing table.
    """
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("apps")}
    if "auto_merge_setup_pr" not in cols:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE apps ADD COLUMN auto_merge_setup_pr BOOLEAN NOT NULL DEFAULT 0"
            ))
        log.info("migrated apps table: added auto_merge_setup_pr")
    log.info("database initialized")
