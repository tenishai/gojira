"""The running version is readable without kubectl: /healthz carries it and
every page's footer shows it. It is the image tag bootstrap derived from the
checkout (a release tag, or tag-distance-commit between releases) handed in
through the gojira-install ConfigMap as config.version; run by hand, the
package version stands in."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gojira import __version__, service
from gojira.config import Config
from gojira.models import Base
from gojira.web import create_app
from tests.fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory


def _client(tmp_path, cfg: Config) -> TestClient:
    engine = create_engine(f"sqlite:///{tmp_path}/gojira.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    clients = service.ProvisionClients(gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory())
    return TestClient(create_app(sf, clients=clients, config=cfg))


def test_healthz_and_footer_name_the_install_version(tmp_path):
    client = _client(tmp_path, Config(version="v1.2.0-3-gc21f2ce21f21"))
    assert client.get("/healthz").json() == {"ok": True, "version": "v1.2.0-3-gc21f2ce21f21"}
    assert "Gojira v1.2.0-3-gc21f2ce21f21" in client.get("/").text


def test_without_an_install_version_the_package_version_stands_in(tmp_path):
    client = _client(tmp_path, Config())
    assert client.get("/healthz").json()["version"] == __version__
    assert f"Gojira {__version__}" in client.get("/clusters").text
