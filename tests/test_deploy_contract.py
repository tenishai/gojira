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
