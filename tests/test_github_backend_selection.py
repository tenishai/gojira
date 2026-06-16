"""service.build_github_client picks the backend from config."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gojira.config import Config, GitHubConfig
from gojira.github import GhCLI, GitHubAppClient
from gojira.models import Base
from gojira.service import build_github_client


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/gojira.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_defaults_to_gh_cli_without_app_config(tmp_path):
    client = build_github_client(Config(), _session_factory(tmp_path))
    assert isinstance(client, GhCLI)


def test_falls_back_to_gh_cli_without_session_factory(tmp_path):
    key = tmp_path / "app.pem"
    key.write_text("dummy")
    config = Config(github=GitHubConfig(app_id="123", private_key_path=str(key)))
    assert isinstance(build_github_client(config, None), GhCLI)


def test_uses_app_backend_when_configured(tmp_path):
    key = tmp_path / "app.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nnot-validated-at-construction\n")
    config = Config(github=GitHubConfig(app_id="123", private_key_path=str(key)))
    client = build_github_client(config, _session_factory(tmp_path))
    assert isinstance(client, GitHubAppClient)
