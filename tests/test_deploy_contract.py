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
    # bootstrap computes these: auth from hostname + allowlist, version from the checkout
    derived = {"GOJIRA_AUTH_ENABLED", "GOJIRA_VERSION"}
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
    assert cfg.observe.job_image == "" and cfg.observe.clusters == {}  # born-observed off

    monkeypatch.setenv("GOJIRA_AUTH_ENABLED", "true")
    monkeypatch.setenv("GOJIRA_ALLOWED_DOMAIN", "example.com")
    monkeypatch.setenv("GOJIRA_NAMESPACE_LABELS", "{example.com/team: sre}")
    monkeypatch.setenv("GOJIRA_SIZING_AGENT_IMAGE", "img:dev")
    monkeypatch.setenv("GOJIRA_OBSERVE_JOB_IMAGE", "noto@sha256:" + "a" * 64)
    monkeypatch.setenv("GOJIRA_OBSERVE_CLUSTERS", "dev=argus/argus-observe,qa")
    cfg = load_config(path)
    assert cfg.auth.enabled is True and cfg.auth.allowed_domain == "example.com"
    assert cfg.defaults.namespace_labels == {"example.com/team": "sre"}
    assert cfg.sizing.agent_image == "img:dev"
    assert cfg.observe.job_image.startswith("noto@sha256:")
    assert set(cfg.observe.clusters) == {"dev", "qa"}
    assert (cfg.observe.clusters["dev"].namespace, cfg.observe.clusters["qa"].namespace) == ("argus", "noto")


# --- first install into a project that has never seen Gojira -------------------


def test_bootstrap_names_docs_that_exist():
    """The closing message sent operators to docs/dogfood.md for a while
    after the walkthrough could have moved; a printed path is a link too."""
    named = set(re.findall(r"docs/[\w-]+\.md", BOOTSTRAP))
    assert named and all((ROOT / doc).is_file() for doc in named), named


def test_bootstrap_registers_the_cluster_under_its_real_name():
    assert "name: ${CLUSTER} " in BOOTSTRAP and "name: sandbox" not in BOOTSTRAP


FAKE_GCLOUD = """#!/usr/bin/env bash
case "$1 $2 $3 $4" in
  "artifacts docker tags add") echo "tags add $5 $6" >> "$TAGS"; exit 0 ;;
  "artifacts docker tags list") printf 'abc123def456-other sha256:f00d\\nabc123def456 sha256:c0ffee\\n'; exit 0 ;;
esac
n=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 )); echo $n > "$STATE"
case "$MODE" in
  lag) [ $n -lt 2 ] && { echo "ERROR: (gcloud.builds.submit) PERMISSION_DENIED: The caller does not have permission." >&2; exit 1; }
       echo built ;;
  broken) echo "ERROR: build step 0 failed" >&2; exit 1 ;;
  denied) echo "ERROR: PERMISSION_DENIED" >&2; exit 1 ;;
esac
"""


def _run_build_step(tmp_path, mode: str, through: str = 'rm -f "$BUILD_LOG"') -> tuple[int, int]:
    """Run bootstrap's Cloud Build block against a fake gcloud; (exit, calls).
    The block starts at BUILD_LOG= and ends at the first line equal to
    `through` — the retry loop by default, the digest pin when asked."""
    import os
    import subprocess

    lines = BOOTSTRAP.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("BUILD_LOG="))
    end = next(i for i in range(start, len(lines)) if lines[i] == through)
    block = "\n".join(lines[start : end + 1]).replace("sleep 45", "sleep 0")
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    (bindir / "gcloud").write_text(FAKE_GCLOUD)
    (bindir / "gcloud").chmod(0o755)
    state = tmp_path / "calls"
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}", "MODE": mode, "STATE": str(state),
        "TAGS": str(tmp_path / "tags"),
        "AR_PATH": "r", "PROJECT": "p", "REPO_ROOT": ".", "IMAGE_TAG": "abc123def456",
    }
    done = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", block], env=env, capture_output=True, text=True
    )
    _run_build_step.last = done  # type: ignore[attr-defined]
    return done.returncode, int(state.read_text())


def test_cloud_build_waits_out_a_fresh_projects_permission_lag(tmp_path):
    """A project's first build is refused for about a minute after the API is
    enabled — to the project's own owner (first fresh-project install,
    2026-09-29). That error is retried; a build that fails on its own merits
    is not rebuilt three times."""
    assert _run_build_step(tmp_path / "lag", "lag") == (0, 2)


def test_cloud_build_does_not_retry_a_broken_build(tmp_path):
    assert _run_build_step(tmp_path / "broken", "broken") == (1, 1)


def test_cloud_build_gives_up_on_a_real_permission_problem(tmp_path):
    assert _run_build_step(tmp_path / "denied", "denied") == (1, 3)


def test_the_build_is_tagged_with_the_commit_and_the_pod_pinned_to_its_digest(tmp_path):
    """Gojira held its apps to tag@digest (#37) while its own pod ran a
    mutable ":dev" — prod did for two months, found 2026-11-13 when a reader
    asked why. The build is tagged with the commit, ":dev" becomes a floating
    alias of it, and the digest the registry reports for exactly that tag (the
    tag: filter is a prefix match, #47) is what the manifest pins."""
    code, calls = _run_build_step(tmp_path / "pin", "lag", through='echo "built ${IMAGE_REF}"')
    assert (code, calls) == (0, 2)
    out = _run_build_step.last.stdout  # type: ignore[attr-defined]
    assert "built r/gojira:abc123def456@sha256:c0ffee" in out, out
    assert (tmp_path / "pin" / "tags").read_text() == "tags add r/gojira:abc123def456 r/gojira:dev\n"
    assert '--tag "${AR_PATH}/gojira:${IMAGE_TAG}"' in BOOTSTRAP
    # the tag is the nearest release (git describe), fetched first so an old clone sees it
    assert "git -C \"$REPO_ROOT\" describe --tags --always" in BOOTSTRAP
    assert 'fetch --quiet --tags origin' in BOOTSTRAP
    assert '--from-literal=GOJIRA_VERSION="$IMAGE_TAG"' in BOOTSTRAP


def test_every_placeholder_in_deploy_is_filled_by_bootstrap():
    """deploy/ is applied from a temp copy with its REPLACE_ placeholders
    substituted; one left behind is a pod that cannot pull or a service
    account that does not exist."""
    placeholders = set()
    for path in (ROOT / "deploy").glob("*.yaml"):
        placeholders |= set(re.findall(r"REPLACE_[A-Z_]+", path.read_text()))
    assert placeholders >= {"REPLACE_AR_PATH", "REPLACE_IMAGE_TAG", "REPLACE_GSA_EMAIL"}
    filled = set(re.findall(r's#(REPLACE_[A-Z_]+)#', BOOTSTRAP))
    assert placeholders <= filled, placeholders - filled
