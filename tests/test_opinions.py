"""Sizing opinions: the table loads, and the principles hold."""

import pytest

from gojira.opinions import WORKLOAD_CLASSES, load_sizing


@pytest.fixture(scope="module")
def sizing():
    return load_sizing()


def test_all_classes_present(sizing):
    assert set(sizing.classes) == set(WORKLOAD_CLASSES)


def test_burstable_qos_everywhere(sizing):
    """The core opinion: requests < limits for every class."""

    def millicores(v: str) -> int:
        return int(v[:-1]) if v.endswith("m") else int(v) * 1000

    def mebibytes(v: str) -> int:
        return int(v[:-2]) * (1024 if v.endswith("Gi") else 1)

    for name, cls in sizing.classes.items():
        assert millicores(cls.requests.cpu) < millicores(cls.limits.cpu), name
        assert mebibytes(cls.requests.memory) < mebibytes(cls.limits.memory), name


def test_web_class_has_hpa_and_probes(sizing):
    web = sizing.classes["web"]
    assert web.hpa is not None
    assert web.hpa.min_replicas >= 2  # no single-replica web services
    assert "readiness" in web.probes
    assert "liveness" in web.probes


def test_gpu_class_requests_gpu(sizing):
    gpu = sizing.classes["gpu"]
    assert gpu.requests.gpu == 1
    assert gpu.limits.gpu == 1


def test_ephemeral_storage_explicit(sizing):
    assert sizing.ephemeral_storage["requests"]
    assert sizing.ephemeral_storage["limits"]


def test_agent_class_has_http_readiness_on_healthz(sizing):
    cls = sizing.classes["agent"]
    readiness = cls.probes["readiness"]
    assert readiness.type == "http"
    assert readiness.path == "/healthz"
    assert cls.probes["liveness"].type == "tcp"
    assert cls.strategy == "Recreate"
