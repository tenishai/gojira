"""ObserveApp: the born-observed step (noto co-design, 2026-09-17/18, when noto was named argus).

Covers the config-absent skip, the Job spec handed to the cluster, the
summary-line ledger contract, failure isolation (observe never fails the
provision), and the cross-repo canonicalization vector noto asserts against.
"""

import json
import re
from pathlib import Path

import pytest
import yaml
from fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gojira import service
from gojira.config import Config, ObserveClusterConfig, ObserveConfig
from gojira.generator import contract_fingerprint
from gojira.models import AppStatus, Base
from gojira.steps import _observe_job_name

VECTOR_DIR = Path(__file__).parent / "fixtures" / "contract_vector"
JOB_IMAGE = "us-east4-docker.pkg.dev/p/apps/noto@sha256:" + "a" * 64


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def _app(session):
    cluster = service.register_cluster(
        session, name="dev", gcp_project="proj-a", location="us-east4",
        gke_cluster="dev-gke",
    )
    return service.connect_app(
        session, name="a", repo_full_name="o/a", workload_class="agent",
        env_map={"dev": (cluster.id, "a")},
    )


def _config(observe: ObserveConfig | None = None) -> Config:
    cfg = Config()
    if observe is not None:
        cfg.observe = observe
    return cfg


def _clients():
    return service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=KubectlFactory()
    )


def _observe_config() -> ObserveConfig:
    return ObserveConfig(
        job_image=JOB_IMAGE, clusters={"dev": ObserveClusterConfig()}
    )


def _step_rows(session, app):
    view = service.app_status(session, app.id)
    return {s.name + ("@" + s.scope if s.scope else ""): s for s in view.steps}


def test_config_absent_skips(session):
    app = _app(session)
    service.run_provision(session, app, _clients(), _config())

    steps = _step_rows(session, app)
    assert steps["observe@dev"].status == "skipped"
    assert "not configured" in steps["observe@dev"].resource_ref


def test_observe_job_spec_and_summary(session):
    app = _app(session)
    clients = _clients()
    service.run_provision(session, app, clients, _config(_observe_config()))

    kubectl = clients.kubectl_for.by_cluster["dev"]
    jobs = [m for m in kubectl.applied if "kind: Job" in m]
    assert len(jobs) == 1
    job = yaml.safe_load(jobs[0])
    assert job["metadata"]["namespace"] == "noto"
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "noto-observe"
    assert pod["restartPolicy"] == "Never"
    container = pod["containers"][0]
    assert container["image"] == JOB_IMAGE
    assert container["args"] == ["observe"]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["ARGUS_ENV"] == "dev"  # the wire name kept its birth name through the rename
    assert env["GOJIRA_CONTRACT_SHA256"] == contract_fingerprint(env["GOJIRA_CONTRACT_YAML"])
    assert job["spec"]["activeDeadlineSeconds"] == 900
    assert job["spec"]["ttlSecondsAfterFinished"] == 600
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    # The pod, not just the image, names the uid — pinned like every other
    # token-reading workload on the estate. (The first theory, that an unpinned
    # uid left the projected token unreadable and caused the first live run's
    # "platform record unreadable", was falsified — see the pinning commit's
    # message; the pin stayed as hardening.)
    sc = pod["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert (sc["runAsUser"], sc["runAsGroup"], sc["fsGroup"]) == (10001, 10001, 10001)

    steps = _step_rows(session, app)
    assert steps["observe@dev"].status == "done"
    # the ARGUS_SUMMARY payload (sans prefix) is the ledger row
    assert "steps applied" in steps["observe@dev"].resource_ref


def test_observe_failure_is_loud_but_isolated(session):
    """A failed observe records a FAILED ledger row carrying noto's own
    failure line — and the provision still completes, the app still lands
    in a healthy terminal status."""
    app = _app(session)
    inner = KubectlFactory()

    class FailingFactory:
        by_cluster = inner.by_cluster

        def __call__(self, cluster):
            k = inner(cluster)
            k.job_failure = "BackoffLimitExceeded"
            k.job_log_text = "ARGUS_SUMMARY observe failed: perses unreachable"
            return k

    clients2 = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=FailingFactory()
    )
    service.run_provision(session, app, clients2, _config(_observe_config()))

    steps = _step_rows(session, app)
    assert steps["observe@dev"].status == "failed"
    assert "perses unreachable" in (steps["observe@dev"].error or "")
    assert app.status in (AppStatus.AWAITING_PR, AppStatus.ACTIVE)  # never FAILED by observe


def test_observe_failure_without_summary_names_the_job_reason(session):
    """noto never got to print (image pull failure, deadline): the row
    carries the Job's failure reason instead of a bare 'did not complete'."""
    app = _app(session)
    inner = KubectlFactory()

    class FailingFactory:
        by_cluster = inner.by_cluster

        def __call__(self, cluster):
            k = inner(cluster)
            k.job_failure = "DeadlineExceeded"
            k.job_log_text = ""
            return k

    clients2 = service.ProvisionClients(
        gcp=FakeGCPClient(), github=FakeGitHubClient(), kubectl_for=FailingFactory()
    )
    service.run_provision(session, app, clients2, _config(_observe_config()))

    assert "DeadlineExceeded" in (_step_rows(session, app)["observe@dev"].error or "")


def test_observe_job_name_keeps_uniqueness_under_the_dns_cut():
    """A 60-char app name would otherwise push the epoch suffix past 63 and
    two runs inside the ttl would collide on an immutable Job."""
    app = "a" * 60
    n1 = _observe_job_name(app, "dev", 1_700_000_000)
    n2 = _observe_job_name(app, "dev", 1_700_000_001)
    assert n1 != n2
    for n in (n1, n2):
        assert len(n) <= 63
        assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", n), n
    assert _observe_job_name("short", "dev", 5) == "noto-observe-5-dev-short"


def test_observe_reruns_on_reprovision(session):
    """Ephemeral: Provision-again re-observes (the birth-only limitation
    dissolves — contract edits get observed with the button we already have)."""
    app = _app(session)
    clients = _clients()
    cfg = _config(_observe_config())
    service.run_provision(session, app, clients, cfg)
    service.run_provision(session, app, clients, cfg)

    kubectl = clients.kubectl_for.by_cluster["dev"]
    assert len([c for c in kubectl.calls if c.startswith("wait_job:")]) == 2


def test_contract_vector_cross_repo():
    """The shared canonicalization vector: noto recomputes this fingerprint
    with its own copy of the algorithm and must agree byte-for-byte. Break
    the canonicalization and one repo goes red before the fleet does."""
    text = (VECTOR_DIR / "gojira.yaml").read_text()
    expected = (VECTOR_DIR / "expected_sha256").read_text().strip()
    assert contract_fingerprint(text) == expected
    # reformatting must not change the meaning-hash
    doc = yaml.safe_load(text)
    reformatted = json.dumps(doc)  # different serialization, same meaning
    assert contract_fingerprint(reformatted) == expected
