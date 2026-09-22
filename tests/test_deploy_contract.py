"""The install path is a contract between deploy/ and scripts/bootstrap.sh.

deploy/deployment.yaml names the keys it reads from Gojira's secrets;
bootstrap.sh is what creates those secrets on a fresh install. When the
onboarding API added ``api_token`` to the deployment (2026-08-04) the key was
added to the live cluster by hand and never to the script, so every fresh
bootstrap produced a pod that could not start — for six weeks, unnoticed,
because the authors never installed fresh. These tests make that drift a
test failure.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = (ROOT / "scripts" / "bootstrap.sh").read_text()


def _manifest_docs(path: Path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _secret_refs(doc) -> set[tuple[str, str]]:
    """Every (secret name, key) a pod spec reads via secretKeyRef or envFrom."""
    refs: set[tuple[str, str]] = set()
    for c in doc["spec"]["template"]["spec"].get("containers", []):
        for env in c.get("env", []):
            ref = env.get("valueFrom", {}).get("secretKeyRef")
            if ref and not ref.get("optional"):
                refs.add((ref["name"], ref["key"]))
    return refs


def _keys_bootstrap_creates(secret: str) -> set[str]:
    """Keys given to `kubectl create secret generic <secret>` in bootstrap.sh."""
    lines = BOOTSTRAP.splitlines()
    starts = [i for i, line in enumerate(lines) if f"create secret generic {secret} " in line]
    assert starts, f"bootstrap.sh never creates secret {secret}"
    block, i = [], starts[0]
    while lines[i].rstrip().endswith("\\"):  # follow the line continuations
        i += 1
        block.append(lines[i])
    return set(re.findall(r"--from-(?:literal|file)=(\w+)=", "\n".join(block)))


def test_bootstrap_creates_every_secret_key_the_deployment_reads():
    (deployment,) = [
        d for d in _manifest_docs(ROOT / "deploy" / "deployment.yaml") if d["kind"] == "Deployment"
    ]
    by_secret: dict[str, set[str]] = {}
    for name, key in _secret_refs(deployment):
        by_secret.setdefault(name, set()).add(key)
    assert by_secret, "deployment reads no secrets? the test is looking at the wrong file"
    for secret, keys in by_secret.items():
        missing = keys - _keys_bootstrap_creates(secret)
        assert not missing, f"deployment.yaml reads {secret}/{sorted(missing)} but bootstrap.sh never creates them"


def test_secret_example_matches_bootstrap():
    example = yaml.safe_load((ROOT / "deploy" / "secret.example.yaml").read_text())
    assert set(example["stringData"]) == _keys_bootstrap_creates(example["metadata"]["name"])


def test_bootstrap_runs_preflight_first():
    assert "scripts/preflight.sh" in BOOTSTRAP or "preflight.sh" in BOOTSTRAP


# --- the committed configmap must load with exactly the env the pod gets ------

CONFIGMAP = ROOT / "deploy" / "configmap.yaml"
DEPLOYMENT = ROOT / "deploy" / "deployment.yaml"
ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _pod_env_names() -> set[str]:
    (dep,) = [d for d in _manifest_docs(DEPLOYMENT) if d["kind"] == "Deployment"]
    (container,) = dep["spec"]["template"]["spec"]["containers"]
    return {e["name"] for e in container.get("env", [])}


def _install_configmap_keys() -> set[str]:
    lines = BOOTSTRAP.splitlines()
    (start,) = [i for i, line in enumerate(lines) if "create configmap gojira-install " in line]
    block, i = [], start
    while lines[i].rstrip().endswith("\\"):
        i += 1
        block.append(lines[i])
    return set(re.findall(r"--from-literal=(\w+)=", "\n".join(block)))


def test_configmap_env_refs_are_all_provided():
    """A ${VAR} without a default must be in the pod's env: (fail-closed
    knobs). A ${VAR:-default} must be a key bootstrap puts in the
    gojira-install ConfigMap, or nothing in gojira.env can ever reach it."""
    config_yaml = yaml.safe_load(CONFIGMAP.read_text())["data"]["config.yaml"]
    refs = [(m.group(1), m.group(2)) for m in ENV_REF.finditer(config_yaml)]
    required = {var for var, default in refs if default is None and var.startswith("GOJIRA_")}
    optional = {var for var, default in refs if default is not None}
    assert required <= _pod_env_names(), required - _pod_env_names()
    assert optional == _install_configmap_keys(), optional ^ _install_configmap_keys()


def test_example_values_cover_every_install_knob():
    example = (ROOT / "gojira.env.example").read_text()
    derived = {"GOJIRA_AUTH_ENABLED"}  # bootstrap computes it from hostname + allowlist
    for key in _install_configmap_keys() - derived:
        assert re.search(rf"^{key}=", example, re.M), f"{key} missing from gojira.env.example"


def test_committed_configmap_loads_like_the_pod(monkeypatch, tmp_path):
    from gojira.config import load_config

    for var in ("GOJIRA_API_TOKEN", "GOJIRA_GITHUB_APP_ID", "GOJIRA_WEBHOOK_SECRET"):
        monkeypatch.setenv(var, "x")
    for var in _install_configmap_keys():
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_load(CONFIGMAP.read_text())["data"]["config.yaml"])
    cfg = load_config(path)  # defaults: no front door, no labels, no agent
    assert cfg.auth.enabled is False and cfg.defaults.namespace_labels == {}
    assert cfg.sizing.agent_image == "" and cfg.drift.enabled is True

    monkeypatch.setenv("GOJIRA_AUTH_ENABLED", "true")
    monkeypatch.setenv("GOJIRA_ALLOWED_DOMAIN", "example.com")
    monkeypatch.setenv("GOJIRA_NAMESPACE_LABELS", "{example.com/team: sre}")
    monkeypatch.setenv("GOJIRA_SIZING_AGENT_IMAGE", "img:dev")
    cfg = load_config(path)
    assert cfg.auth.enabled is True and cfg.auth.allowed_domain == "example.com"
    assert cfg.defaults.namespace_labels == {"example.com/team": "sre"}
    assert cfg.sizing.agent_image == "img:dev"
