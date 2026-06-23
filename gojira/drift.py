"""Drift detection: semantic diff of live cluster spec vs git desired state.

The hard part of comparing a live `kubectl get -o json` against a freshly built
`kustomize build` is noise — status, managedFields, defaulted fields the API
server injects, HPA-owned replica counts, CI-owned image tags. A raw diff is
almost entirely false positives.

The approach here is an **allowlist projection**: from both sides we extract
only the fields Gojira manages (container resources, probes, replicas), then
diff the projections. Anything not in the projection — defaults, status, image
tags — cannot produce a finding, because it isn't compared at all.

Two deliberate exclusions, both required by "never reverted" (design.md:26):
  - **image** — set by CI at deploy time, not drift.
  - **replicas under an HPA** — owned by the autoscaler; comparing them would
    make the detector fight the HPA forever.

Pure functions only. The cloud I/O (read live, render desired) lives in the
clients and the service layer; this module just compares dicts.
"""

import logging
from dataclasses import dataclass, field

import yaml

log = logging.getLogger(__name__)

# Kinds we project today. Deployment covers the SRE-patch case in the Phase 2
# DoD (resources / replicas / probes). Service, HPA, and PDB drift are follow-ons.
MANAGED_KINDS = ("Deployment",)

# Probe sub-fields Gojira sets in its templates. Live probes carry extra
# defaulted fields (timeoutSeconds, successThreshold, ...) we must ignore — and
# the defaults reach inside the handler too (httpGet.scheme), so the allowlist
# has to descend into the handler, not stop at it.
_PROBE_KEYS = ("initialDelaySeconds", "periodSeconds")
_HANDLER_KEYS = {"httpGet": ("path", "port"), "tcpSocket": ("port",), "exec": ("command",)}
_CONTAINER_KEYS = ("resources", "readinessProbe", "livenessProbe")

_MEM_SUFFIX = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5,
    "k": 1000, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "P": 1000**5,
}


@dataclass(frozen=True)
class FieldDiff:
    """One managed field that differs. `path` is dotted, values are normalized."""

    path: str
    desired: object
    live: object


@dataclass(frozen=True)
class ResourceDrift:
    """All managed-field divergences for one live object."""

    kind: str
    name: str
    fields: list[FieldDiff] = field(default_factory=list)


def normalize_quantity(key: str, value) -> int:
    """K8s quantity → comparable int. CPU → millicores; memory/storage → bytes.

    So `cpu: "1"` and `cpu: "1000m"` compare equal, as do `memory: "1Gi"` and
    `memory: "1073741824"`. Plain counts (gpu) pass through.
    """
    if isinstance(value, (int, float)):
        # CPU as a bare number is cores; everything else is already base units.
        return int(value * 1000) if key == "cpu" else int(value)
    s = str(value).strip()
    if key == "cpu":
        return int(s[:-1]) if s.endswith("m") else int(float(s) * 1000)
    for suffix, mult in _MEM_SUFFIX.items():
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(float(s))


def _normalize_resources(res: dict) -> dict:
    """requests/limits with every quantity normalized to comparable ints."""
    out: dict = {}
    for bucket in ("requests", "limits"):
        if bucket in res:
            out[bucket] = {k: normalize_quantity(k, v) for k, v in res[bucket].items()}
    return out


def _project_probe(probe: dict) -> dict:
    out = {k: probe[k] for k in _PROBE_KEYS if k in probe}
    for handler, keys in _HANDLER_KEYS.items():
        if handler in probe:
            out[handler] = {k: probe[handler][k] for k in keys if k in probe[handler]}
    return out


def _container_projection(container: dict) -> dict:
    out: dict = {}
    if "resources" in container:
        out["resources"] = _normalize_resources(container["resources"])
    for pk in ("readinessProbe", "livenessProbe"):
        if pk in container:
            out[pk] = _project_probe(container[pk])
    return out


def project(manifest: dict, *, hpa_managed: bool) -> dict:
    """Extract only the fields Gojira manages from a Deployment manifest.

    `hpa_managed` drops replicas from the projection (the autoscaler owns them).
    Containers are keyed by name so the diff is order-independent.
    """
    spec = manifest.get("spec", {})
    pod_spec = spec.get("template", {}).get("spec", {})
    containers = {
        c["name"]: _container_projection(c) for c in pod_spec.get("containers", [])
    }
    proj: dict = {"containers": containers}
    if not hpa_managed and "replicas" in spec:
        proj["replicas"] = spec["replicas"]
    return proj


def _diff_node(path: str, desired, live, out: list[FieldDiff]) -> None:
    if isinstance(desired, dict) and isinstance(live, dict):
        for key in sorted(set(desired) | set(live)):
            child = f"{path}.{key}" if path else key
            _diff_node(child, desired.get(key, _MISSING), live.get(key, _MISSING), out)
    elif desired != live:
        out.append(FieldDiff(path=path, desired=_show(desired), live=_show(live)))


_MISSING = object()


def _show(v):
    return None if v is _MISSING else v


def diff_projection(desired: dict, live: dict) -> list[FieldDiff]:
    out: list[FieldDiff] = []
    _diff_node("", desired, live, out)
    return out


def hpa_targets(desired_objs: list[dict]) -> set[str]:
    """Names of Deployments that a desired HPA scales (→ replicas are HPA-owned)."""
    targets = set()
    for obj in desired_objs:
        if obj.get("kind") == "HorizontalPodAutoscaler":
            ref = obj.get("spec", {}).get("scaleTargetRef", {})
            if ref.get("kind") == "Deployment" and ref.get("name"):
                targets.add(ref["name"])
    return targets


def _managed_container_raw(container: dict) -> dict:
    """Managed container fields with raw values — for authoring a capture patch.

    Unlike `_container_projection` (which normalizes quantities to ints for
    comparison), this keeps the live strings ("800m", "1Gi") so the patch is a
    valid manifest a human reviews and kustomize applies.
    """
    out: dict = {"name": container["name"]}
    if "resources" in container:
        out["resources"] = container["resources"]
    for pk in ("readinessProbe", "livenessProbe"):
        if pk in container:
            out[pk] = _project_probe(container[pk])
    return out


def capture_patch(live_obj: dict, *, hpa_managed: bool) -> str:
    """A strategic-merge patch encoding the live managed fields of a Deployment.

    Merging it into the env overlay makes git's desired state match what's
    actually running — git catches up to the cluster, the operator's change is
    preserved (design.md:26). Excludes image (CI-owned) and, under an HPA,
    replicas (autoscaler-owned).
    """
    spec = live_obj.get("spec", {})
    pod = spec.get("template", {}).get("spec", {})
    containers = [_managed_container_raw(c) for c in pod.get("containers", [])]
    patch: dict = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": live_obj["metadata"]["name"]},
        "spec": {"template": {"spec": {"containers": containers}}},
    }
    if not hpa_managed and "replicas" in spec:
        patch["spec"]["replicas"] = spec["replicas"]
    header = "# Captured by Gojira's drift detector — the live cluster state.\n"
    return header + yaml.safe_dump(patch, sort_keys=False)


def detect(desired_objs: list[dict], live: dict[tuple[str, str], dict | None]) -> list[ResourceDrift]:
    """Compare desired (from kustomize build) against live, per managed object.

    `live` maps (kind, name) → live manifest or None. A desired object with no
    live counterpart is reported as drift (the whole projection differs from an
    empty one) — that surfaces a managed resource an operator deleted.
    """
    hpa_managed = hpa_targets(desired_objs)
    drifts: list[ResourceDrift] = []
    for obj in desired_objs:
        kind, name = obj.get("kind"), obj.get("metadata", {}).get("name")
        if kind not in MANAGED_KINDS or not name:
            continue
        desired_proj = project(obj, hpa_managed=name in hpa_managed)
        live_obj = live.get((kind, name))
        live_proj = project(live_obj, hpa_managed=name in hpa_managed) if live_obj else {}
        fields = diff_projection(desired_proj, live_proj)
        if fields:
            drifts.append(ResourceDrift(kind=kind, name=name, fields=fields))
    return drifts
