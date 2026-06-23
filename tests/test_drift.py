"""Drift detection: the projection must see real patches and ignore K8s noise."""

from gojira.drift import (
    detect,
    diff_projection,
    normalize_quantity,
    project,
)


def _desired_deployment(cpu_req="250m", mem_req="256Mi", replicas=None, probe=True):
    """A Deployment as `kustomize build` would emit it — clean, no defaults."""
    container = {
        "name": "app",
        "image": "gojira-app",  # kustomize sets the real image; excluded from drift
        "resources": {
            "requests": {"cpu": cpu_req, "memory": mem_req, "ephemeral-storage": "4Gi"},
            "limits": {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "8Gi"},
        },
    }
    if probe:
        container["readinessProbe"] = {
            "httpGet": {"path": "/health", "port": "http"},
            "initialDelaySeconds": 5,
            "periodSeconds": 10,
        }
    spec = {"template": {"spec": {"containers": [container]}}}
    if replicas is not None:
        spec["replicas"] = replicas
    return {"kind": "Deployment", "metadata": {"name": "clips-api"}, "spec": spec}


def _live_deployment(cpu_req="250m", mem_req="256Mi", replicas=1, image="reg/clips-api:abc123"):
    """The same Deployment as the API server returns it — full of noise."""
    return {
        "kind": "Deployment",
        "metadata": {
            "name": "clips-api",
            "resourceVersion": "99812",
            "uid": "f1e2-d3c4",
            "creationTimestamp": "2026-06-01T00:00:00Z",
            "managedFields": [{"manager": "kubectl"}],
        },
        "spec": {
            "replicas": replicas,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",  # API-defaulted
                            "terminationMessagePath": "/dev/termination-log",
                            "resources": {
                                "requests": {"cpu": cpu_req, "memory": mem_req, "ephemeral-storage": "4Gi"},
                                "limits": {"cpu": "500m", "memory": "512Mi", "ephemeral-storage": "8Gi"},
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": "http", "scheme": "HTTP"},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 10,
                                "timeoutSeconds": 1,        # API-defaulted
                                "successThreshold": 1,      # API-defaulted
                                "failureThreshold": 3,      # API-defaulted
                            },
                        }
                    ]
                }
            },
        },
        "status": {"readyReplicas": 1, "conditions": [{"type": "Available"}]},
    }


# --- quantity normalization -------------------------------------------------

def test_cpu_normalization_equates_cores_and_millicores():
    assert normalize_quantity("cpu", "1") == normalize_quantity("cpu", "1000m") == 1000
    assert normalize_quantity("cpu", "250m") == 250
    assert normalize_quantity("cpu", 2) == 2000  # bare number = cores


def test_memory_normalization_equates_suffixes_and_bytes():
    assert normalize_quantity("memory", "1Gi") == 1073741824
    assert normalize_quantity("memory", "1073741824") == 1073741824
    assert normalize_quantity("memory", "256Mi") == 268435456


# --- projection -------------------------------------------------------------

def test_projection_drops_replicas_when_hpa_managed():
    dep = _desired_deployment(replicas=3)
    assert "replicas" not in project(dep, hpa_managed=True)
    assert project(dep, hpa_managed=False)["replicas"] == 3


def test_projection_strips_defaulted_probe_fields():
    proj = project(_live_deployment(), hpa_managed=False)
    probe = proj["containers"]["app"]["readinessProbe"]
    assert set(probe) == {"httpGet", "initialDelaySeconds", "periodSeconds"}
    assert "timeoutSeconds" not in probe and "failureThreshold" not in probe


# --- the core: no false positives, catches real patches ---------------------

def test_no_drift_when_live_matches_desired_despite_noise():
    """The whole point: API-server noise and image tags must not read as drift."""
    desired = [_desired_deployment(replicas=1)]
    live = {("Deployment", "clips-api"): _live_deployment(image="reg/clips-api:v9")}
    assert detect(desired, live) == []


def test_sre_cpu_patch_is_detected():
    desired = [_desired_deployment(cpu_req="250m", replicas=1)]
    live = {("Deployment", "clips-api"): _live_deployment(cpu_req="800m")}
    drifts = detect(desired, live)
    assert len(drifts) == 1
    paths = {f.path for f in drifts[0].fields}
    assert "containers.app.resources.requests.cpu" in paths
    fd = next(f for f in drifts[0].fields if f.path.endswith(".cpu"))
    assert fd.desired == 250 and fd.live == 800


def test_replicas_drift_ignored_under_hpa():
    """Live scaled to 7 by the HPA must NOT be drift when an HPA is present."""
    hpa = {
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "clips-api"},
        "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "clips-api"}},
    }
    desired = [_desired_deployment(replicas=None), hpa]
    live = {("Deployment", "clips-api"): _live_deployment(replicas=7)}
    assert detect(desired, live) == []


def test_replicas_drift_detected_without_hpa():
    desired = [_desired_deployment(replicas=2)]
    live = {("Deployment", "clips-api"): _live_deployment(replicas=5)}
    drifts = detect(desired, live)
    assert any(f.path == "replicas" and f.desired == 2 and f.live == 5 for f in drifts[0].fields)


def test_deleted_managed_resource_is_drift():
    desired = [_desired_deployment(replicas=1)]
    live = {("Deployment", "clips-api"): None}
    drifts = detect(desired, live)
    assert len(drifts) == 1 and drifts[0].name == "clips-api"


def test_equal_projections_have_no_diff():
    assert diff_projection({"a": 1, "b": {"c": 2}}, {"a": 1, "b": {"c": 2}}) == []
