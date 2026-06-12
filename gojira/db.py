"""Database engine and session factory. SQLite by default, Postgres via DATABASE_URL."""

import logging

from sqlalchemy import create_engine
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
    """Create all tables. Idempotent."""
    Base.metadata.create_all(engine)
    log.info("database initialized")
