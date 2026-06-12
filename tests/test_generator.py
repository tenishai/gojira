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

from gojira.generator import GeneratorSpec, render

GOLDEN_DIR = Path(__file__).parent / "golden"


def make_spec(**kw) -> GeneratorSpec:
    defaults = dict(
        app="clips-api",
        repo_full_name="example/clips-api",
        namespace="clips",
        workload_class="web",
        cluster_name="dev-cluster",
        gcp_project="example-project",
        location="us-central1",
        gke_cluster="example-gke",
        ar_repo="us-central1-docker.pkg.dev/example-project/apps",
        wif_provider=(
            "projects/123456789/locations/global/"
            "workloadIdentityPools/gojira/providers/github"
        ),
        gsa_email="clips-api-deploy@example-project.iam.gserviceaccount.com",
    )
    return GeneratorSpec(**{**defaults, **kw})


GOLDEN_CASES = {
    "web": make_spec(),
    "worker": make_spec(workload_class="worker", app="clips-worker"),
    "cron": make_spec(workload_class="cron", app="clips-sweeper", schedule="0 3 * * *"),
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
    files = render(make_spec(workload_class="worker"))
    assert "k8s/base/service.yaml" not in files
    assert "k8s/base/hpa.yaml" not in files
    assert "k8s/base/pdb.yaml" not in files
    assert "k8s/base/deployment.yaml" in files
    assert "k8s/base/podmonitoring.yaml" in files  # observability is non-optional


def test_cron_renders_cronjob_not_deployment():
    files = render(make_spec(workload_class="cron", schedule="0 3 * * *"))
    assert "k8s/base/cronjob.yaml" in files
    assert "k8s/base/deployment.yaml" not in files
    cronjob = yaml.safe_load(files["k8s/base/cronjob.yaml"])
    assert cronjob["spec"]["schedule"] == "0 3 * * *"


def test_cron_without_schedule_rejected():
    with pytest.raises(ValueError, match="schedule"):
        make_spec(workload_class="cron")


def test_unknown_class_rejected():
    with pytest.raises(ValueError, match="workload class"):
        make_spec(workload_class="mega")


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
    # HPA owns replicas for web class — Deployment must not set them
    assert "replicas" not in deployment["spec"]


def test_workflows_use_wif_not_keys():
    files = render(make_spec())
    for wf in (".github/workflows/gojira-ci.yaml", ".github/workflows/gojira-promote.yaml"):
        assert "workload_identity_provider" in files[wf]
        assert "credentials_json" not in files[wf]  # no long-lived keys, ever


def test_prod_deploy_behind_environment_gate():
    files = render(make_spec())
    promote = yaml.safe_load(files[".github/workflows/gojira-promote.yaml"])
    assert promote["jobs"]["deploy-prod"]["environment"] == "production"
    assert promote["jobs"]["deploy-prod"]["needs"] == "deploy-qa"
