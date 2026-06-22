"""DbInstallationResolver: the durable repo→installation cache."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gojira.installations import DbInstallationResolver
from gojira.models import Base


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/gojira.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_resolve_miss_returns_none(session_factory):
    r = DbInstallationResolver(session_factory)
    assert r.resolve("acme/web") is None


def test_remember_then_resolve(session_factory):
    r = DbInstallationResolver(session_factory)
    r.remember("acme/web", 100)
    assert r.resolve("acme/web") == 100


def test_remember_updates_existing(session_factory):
    r = DbInstallationResolver(session_factory)
    r.remember("acme/web", 100)
    r.remember("acme/web", 200)  # re-installed under a new id
    assert r.resolve("acme/web") == 200


def test_forget(session_factory):
    r = DbInstallationResolver(session_factory)
    r.remember("acme/web", 100)
    r.forget("acme/web")
    assert r.resolve("acme/web") is None
