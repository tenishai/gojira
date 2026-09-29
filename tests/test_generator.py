"""Generator tests: golden files + structural assertions.

The rendered output IS the product — snapshot it. To regenerate goldens
after an intentional template change:

    GOJIRA_UPDATE_GOLDEN=1 venv/bin/python -m pytest tests/test_generator.py

then review the golden diff like any other code change.
"""

import os
from pathlib import Path

import pytest
import yaml

from gojira.generator import EnvSpec, GeneratorSpec, render

GOLDEN_DIR = Path(__file__).parent / "golden"


def _env(app: str, name: str, project: str, copy_from=None, gated=False) -> EnvSpec:
    num = "123456789" if project == "example-project" else "987654321"
    return EnvSpec(
        name=name,
        namespace="clips",
        project=project,
        cluster_name=f"{name}-cluster",
        location="us-central1",
        gke_cluster=f"{name}-gke",
        ar_repo=f"us-central1-docker.pkg.dev/{project}/apps",
        wif_provider=(
            f"projects/{num}/locations/global/"
            "workloadIdentityPools/gojira/providers/github"
        ),
        gsa_email=f"{app}-deploy@{project}.iam.gserviceaccount.com",
        copy_from=copy_from,
        gated=gated,
    )


def _envs(app: str, multiproject: bool = False) -> list[EnvSpec]:
    prod_project = "prod-project" if multiproject else "example-project"
    return [
        _env(app, "dev", "example-project"),
        _env(app, "qa", "example-project"),
        _env(app, "prod", prod_project, copy_from="qa", gated=True),
    ]


def make_spec(app="clips-api", multiproject=False, environments=None, **kw) -> GeneratorSpec:
    return GeneratorSpec(
        app=app,
        repo_full_name=f"example/{app}",
        workload_class=kw.pop("workload_class", "web"),
        environments=environments if environments is not None else _envs(app, multiproject),
        **kw,
    )


GOLDEN_CASES = {
    "web": make_spec(),
    "worker": make_spec(app="clips-worker", workload_class="worker"),
    "cron": make_spec(app="clips-sweeper", workload_class="cron", schedule="0 3 * * *"),
    "multiproject": make_spec(multiproject=True),
}


@pytest.mark.parametrize("case", GOLDEN_CASES)
def test_golden(case):
    files = render(GOLDEN_CASES[case])
    golden_root = GOLDEN_DIR / case

    if os.environ.get("GOJIRA_UPDATE_GOLDEN"):
        for path, content in files.items():
            target = golden_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        pytest.skip("goldens updated")

    expected = {
        str(p.relative_to(golden_root)): p.read_text()
        for p in golden_root.rglob("*")
        if p.is_file()
    }
    assert files.keys() == expected.keys(), "artifact set changed — review and update goldens"
    for path in files:
        assert files[path] == expected[path], f"{case}/{path} drifted from golden"


def test_all_output_is_valid_yaml():
    for spec in GOLDEN_CASES.values():
        for path, content in render(spec).items():
            list(yaml.safe_load_all(content)), path


def test_web_gets_full_artifact_set():
    files = render(make_spec())
    assert set(files) == {
        ".github/workflows/gojira-ci.yaml",
        ".github/workflows/gojira-promote.yaml",
        "gojira.yaml",
        "k8s/base/kustomization.yaml",
        "k8s/base/deployment.yaml",
        "k8s/base/service.yaml",
        "k8s/base/hpa.yaml",
        "k8s/base/pdb.yaml",
        "k8s/base/podmonitoring.yaml",
        "k8s/base/rules.yaml",
        "k8s/overlays/dev/kustomization.yaml",
        "k8s/overlays/qa/kustomization.yaml",
        "k8s/overlays/prod/kustomization.yaml",
    }


def test_worker_has_no_service_or_hpa():
    files = render(make_spec(app="clips-worker", workload_class="worker"))
    assert "k8s/base/service.yaml" not in files
    assert "k8s/base/hpa.yaml" not in files
    assert "k8s/base/pdb.yaml" not in files
    assert "k8s/base/deployment.yaml" in files
    assert "k8s/base/podmonitoring.yaml" in files  # observability is non-optional


def test_cron_renders_cronjob_not_deployment():
    files = render(make_spec(app="clips-sweeper", workload_class="cron", schedule="0 3 * * *"))
    assert "k8s/base/cronjob.yaml" in files
    assert "k8s/base/deployment.yaml" not in files
    cronjob = yaml.safe_load(files["k8s/base/cronjob.yaml"])
    assert cronjob["spec"]["schedule"] == "0 3 * * *"


def test_cron_without_schedule_rejected():
    with pytest.raises(ValueError, match="schedule"):
        make_spec(app="clips-sweeper", workload_class="cron")


def test_unknown_class_rejected():
    with pytest.raises(ValueError, match="workload class"):
        make_spec(workload_class="mega")


def test_empty_environments_rejected():
    with pytest.raises(ValueError, match="environment"):
        make_spec(environments=[])


def test_base_kustomization_lists_exactly_its_files():
    files = render(make_spec())
    kustomization = yaml.safe_load(files["k8s/base/kustomization.yaml"])
    base_files = {
        p.removeprefix("k8s/base/")
        for p in files
        if p.startswith("k8s/base/") and not p.endswith("kustomization.yaml")
    }
    assert set(kustomization["resources"]) == base_files


def test_deployment_encodes_sizing_opinion():
    files = render(make_spec())
    deployment = yaml.safe_load(files["k8s/base/deployment.yaml"])
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"]["cpu"] == "250m"
    assert container["resources"]["limits"]["cpu"] == "500m"
    assert container["resources"]["requests"]["ephemeral-storage"] == "4Gi"
    assert "replicas" not in deployment["spec"]


def test_workflows_use_wif_not_keys():
    files = render(make_spec())
    for wf in (".github/workflows/gojira-ci.yaml", ".github/workflows/gojira-promote.yaml"):
        assert "workload_identity_provider" in files[wf]
        assert "credentials_json" not in files[wf]  # no long-lived keys, ever


def test_prod_deploy_behind_environment_gate():
    promote = yaml.safe_load(render(make_spec())[".github/workflows/gojira-promote.yaml"])
    assert promote["jobs"]["deploy-prod"]["environment"] == "production"
    assert promote["jobs"]["deploy-prod"]["needs"] == "deploy-qa"


def test_ci_targets_first_env():
    ci = yaml.safe_load(render(make_spec())[".github/workflows/gojira-ci.yaml"])
    assert "build-and-deploy-dev" in ci["jobs"]


def test_concurrency_scoped_by_workflow():
    """ci and promote never target the same cluster (envs are disjoint by
    construction), so they must not serialize against each other — but
    repeated runs of the *same* workflow (the actual field race) still queue.
    """
    files = render(make_spec())
    for wf in (".github/workflows/gojira-ci.yaml", ".github/workflows/gojira-promote.yaml"):
        parsed = yaml.safe_load(files[wf])
        concurrency = parsed["concurrency"]
        assert concurrency["cancel-in-progress"] is False
        assert "github.repository" in concurrency["group"]
        assert "github.workflow" in concurrency["group"]


def test_sameproject_promote_deploys_without_crane_copy():
    """prod shares qa's registry, so the tested image is already present."""
    promote = render(make_spec())[".github/workflows/gojira-promote.yaml"]
    assert "crane copy" not in promote
    # qa builds from scratch; prod neither builds nor copies — it deploys as-is
    assert promote.count("docker build") == 1


def test_multiproject_promote_crane_copies_into_prod():
    promote = render(make_spec(multiproject=True))[".github/workflows/gojira-promote.yaml"]
    assert "crane copy" in promote
    assert "imjasonh/setup-crane" in promote
    # qa still builds; prod copies instead of building
    assert promote.count("docker build") == 1


def test_single_env_has_no_promote_workflow():
    spec = make_spec(environments=[_env("clips-api", "dev", "example-project")])
    files = render(spec)
    assert ".github/workflows/gojira-promote.yaml" not in files
    assert ".github/workflows/gojira-ci.yaml" in files


def test_agent_class_renders_service():
    spec = make_spec(workload_class="agent")
    files = render(spec)
    assert "k8s/base/service.yaml" in files
    assert "readinessProbe" in files["k8s/base/deployment.yaml"]
    assert "/healthz" in files["k8s/base/deployment.yaml"]


# --- finding #20: CI refuses a contract/manifest mismatch -------------------

def test_fingerprint_ignores_formatting_but_not_meaning():
    from gojira.generator import contract_fingerprint

    a = "app: x\nsecrets_from: [one, two]\nenv:\n  A: \"1\"\n"
    b = "# reformatted by a machine\nenv:\n  A: '1'\napp: x\nsecrets_from:\n- one\n- two\n"
    c = "app: x\nsecrets_from: [one, two, three]\nenv:\n  A: \"1\"\n"
    assert contract_fingerprint(a) == contract_fingerprint(b)
    assert contract_fingerprint(a) != contract_fingerprint(c)


def test_workflows_stamped_with_their_contracts_fingerprint():
    from gojira.generator import contract_fingerprint

    files = render(make_spec())
    sha = contract_fingerprint(files["gojira.yaml"])
    for wf in (".github/workflows/gojira-ci.yaml", ".github/workflows/gojira-promote.yaml"):
        assert f"GOJIRA_CONTRACT: {sha}" in files[wf]
        assert "Verify manifests match gojira.yaml" in files[wf]


def test_ci_check_computes_the_same_hash_as_the_generator(tmp_path):
    """Run the generated step's shell for real: a stale contract fails it,
    the rendered one passes. Guards the 'same algorithm on both sides' claim."""
    import subprocess

    from gojira.generator import contract_fingerprint

    files = render(make_spec())
    wf = yaml.safe_load(files[".github/workflows/gojira-ci.yaml"])
    step = next(s for s in wf["jobs"]["build-and-deploy-dev"]["steps"]
                if s.get("name") == "Verify manifests match gojira.yaml")
    sha = wf["env"]["GOJIRA_CONTRACT"]
    assert sha == contract_fingerprint(files["gojira.yaml"])

    # `python3` on the snippet's PATH is this interpreter (it has PyYAML, a
    # gojira dependency) — the shape of a GitHub-hosted runner, where python3
    # ships with yaml. On a self-hosted runner setup-python's pristine build
    # came first and the fallback pip install decided the test, not the hash.
    import sys
    path = f"{os.path.dirname(sys.executable)}:{os.environ['PATH']}"

    def run(contract: str) -> int:
        (tmp_path / "gojira.yaml").write_text(contract)
        return subprocess.run(
            ["bash", "-e", "-c", step["run"]], cwd=tmp_path,
            env={"PATH": path, "HOME": os.environ.get("HOME", str(tmp_path)), "GOJIRA_CONTRACT": sha},
            capture_output=True,
        ).returncode

    assert run(files["gojira.yaml"]) == 0
    assert run("# comment only, keys reordered\n" + files["gojira.yaml"].replace(
        "class: web\napp: clips-api", "app: clips-api\nclass: web")) == 0
    assert run(files["gojira.yaml"] + "secrets_from: [added-later]\n") == 1


def test_stamp_contract_rewrites_only_the_fingerprint():
    from gojira.generator import contract_fingerprint, stamp_contract

    files = render(make_spec())
    edited = files["gojira.yaml"] + "secrets_from: [clio-slack]\n"
    stamped = stamp_contract(files[".github/workflows/gojira-ci.yaml"], edited)
    assert f"GOJIRA_CONTRACT: {contract_fingerprint(edited)}" in stamped
    assert stamped.replace(contract_fingerprint(edited), "X") == \
        files[".github/workflows/gojira-ci.yaml"].replace(contract_fingerprint(files["gojira.yaml"]), "X")
    with pytest.raises(ValueError):
        stamp_contract("name: hand-written\n", edited)
