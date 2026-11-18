"""Generator tests: golden files + structural assertions.

The rendered output IS the product — snapshot it. To regenerate goldens
after an intentional template change:

    GOJIRA_UPDATE_GOLDEN=1 venv/bin/python -m pytest tests/test_generator.py

then review the golden diff like any other code change.
"""

import os
import re
from pathlib import Path

import pytest
import yaml

from gojira.generator import Identity, EnvSpec, GeneratorSpec, render

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
    "identity": make_spec(app="clips-vertex", identity=Identity(gcp_roles=["roles/aiplatform.user"])),
    # issue #108: a published image is copied into the install's registry, not built
    "image": make_spec(app="clips-published", multiproject=True, published_image="ghcr.io/example/clips:1.4.0"),
}


def test_internal_access_renders_the_private_endpoint():
    """A cluster registered with access internal: the workflows reach the
    control plane by its private IP (the runner is inside the network) and
    never ask for the DNS endpoint. Default clusters keep the golden text."""
    spec = GOLDEN_CASES["web"]
    internal = spec.model_copy(update={
        "environments": [e.model_copy(update={"access": "internal"}) for e in spec.environments],
    })
    files = render(internal)
    for name in (".github/workflows/gojira-ci.yaml", ".github/workflows/gojira-promote.yaml"):
        assert "use_internal_ip: true" in files[name]
        assert "use_dns_based_endpoint" not in files[name]
    for name in (".github/workflows/gojira-ci.yaml", ".github/workflows/gojira-promote.yaml"):
        assert "use_internal_ip" not in render(spec)[name]


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


PINNED_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def test_generated_workflows_pin_actions_to_a_commit_sha():
    """A tag can be moved after review; a commit SHA cannot. The generated
    workflow holds the deploy identity, and the repos it lands in are going public."""
    for case, spec in GOLDEN_CASES.items():
        for path, content in render(spec).items():
            if not path.startswith(".github/workflows/"):
                continue
            for job in yaml.safe_load(content)["jobs"].values():
                for step in job.get("steps", []):
                    uses = step.get("uses")
                    if uses and not uses.startswith("./"):
                        assert PINNED_ACTION.match(uses), f"{case}/{path}: pin {uses} to a full commit SHA"


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


def _build_script(spec=None) -> str:
    ci = render(spec or make_spec())[".github/workflows/gojira-ci.yaml"]
    job = yaml.safe_load(ci)["jobs"].popitem()[1]
    return next(s for s in job["steps"] if s.get("name") == "Build and push image")["run"]


def test_ci_reuses_a_build_only_for_a_dockerfile_that_reads_no_build_context():
    """tenishai/kobo#434, and Shaz's review on gojira#132: the image is a function of the
    Dockerfile alone only when it reads nothing from the context. A COPY, ADD or bind mount
    makes it a function of the commit, so it always builds; otherwise the registry is keyed
    by the Dockerfile's blob sha, an existing build is re-tagged (one exact row, since the
    tag filter is a prefix match), and a new build is tagged with the key after its push.
    The Redeploy button always builds."""
    build = _build_script()
    assert "grep -qiE '^[[:space:]]*(COPY|ADD)[[:space:]]|--mount=type=bind' Dockerfile" in build
    assert 'BUILD_KEY="build-$(git rev-parse "HEAD:Dockerfile" | cut -c1-12)"' in build
    assert 'if [ -n "$BUILD_KEY" ] && [ "${{ github.event_name }}" != "workflow_dispatch" ]' in build
    assert "--format='value(tag,version)'" in build and 'if (t[n] == k) { print $2; exit }' in build
    assert 'gcloud artifacts docker tags add "$IMAGE@$EXISTING" "$IMAGE:$TAG"' in build
    assert 'gcloud artifacts docker tags add "$IMAGE:$TAG" "$IMAGE:$BUILD_KEY"' in build


@pytest.mark.parametrize(
    "dockerfile, reuses",
    [
        ("FROM reg/runtime@sha256:abc\n", True),
        ("# image: the runtime\nFROM reg/runtime@sha256:abc\nENV X=1\n", True),
        ("FROM python:3.12\nCOPY app.py .\n", False),
        ("FROM reg/runtime@sha256:abc\nCOPY bundle/ /bundle\n", False),
        ("FROM x\n  add https://example/a.tgz /a\n", False),
        ("FROM x\nRUN --mount=type=bind,source=.,target=/src make\n", False),
    ],
)
def test_the_context_check_tells_a_runtime_only_dockerfile_from_one_that_reads_the_commit(tmp_path, dockerfile, reuses):
    """The exact grep the workflow runs, against real Dockerfiles."""
    import subprocess

    (tmp_path / "Dockerfile").write_text(dockerfile)
    reads = subprocess.run(
        ["grep", "-qiE", "^[[:space:]]*(COPY|ADD)[[:space:]]|--mount=type=bind", "Dockerfile"], cwd=tmp_path
    ).returncode == 0
    assert reads is (not reuses)


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


def test_promote_decides_copy_or_not_at_run_time():
    """Whether prod shares qa's registry is the install's fact, read from the
    repository variables at run time (issue #87) — the rendered workflow is
    the same either way: qa builds once; prod copies only when the two
    registries differ, and never builds."""
    promote = render(make_spec())[".github/workflows/gojira-promote.yaml"]
    assert promote.count("docker build") == 1
    assert 'if [ "$SOURCE_IMAGE" = "$IMAGE" ]' in promote
    assert "crane copy \"$SOURCE_IMAGE:$GITHUB_REF_NAME\" \"$IMAGE:$GITHUB_REF_NAME\"" in promote
    assert "SOURCE_IMAGE: ${{ vars.GOJIRA_QA_AR_REPO }}/" in promote


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


def test_the_rendered_repo_carries_none_of_the_installs_facts():
    """Issue #87: registry, project, WIF provider, deploy GSA and cluster
    reach CI as repository variables Gojira sets. Nothing generated into the
    repo names them, so the repo is the same under any install."""
    from gojira.generator import Identity

    spec = make_spec(multiproject=True, identity=Identity(gcp_roles=["roles/aiplatform.user"]))
    files = render(spec)
    generated = "\n".join(
        text for path, text in files.items()
        if path.startswith(".github/") or path.startswith("k8s/")
    )
    for env in spec.environments:
        for fact in (env.ar_repo, env.project, env.wif_provider, env.gsa_email, env.gke_cluster, spec.run_gsa(env)):
            assert fact not in generated, f"{fact} leaked into the generated files"
    for env in spec.environments:
        for name in env.platform_variables(spec.run_gsa(env)):
            assert f"vars.{name}" in files[".github/workflows/gojira-ci.yaml"] + files[".github/workflows/gojira-promote.yaml"]


def test_the_variables_guard_fails_an_unconnected_repo(tmp_path):
    """The first workflow step refuses to run without the install's variables,
    naming the missing one — run for real, in bash, like the contract check."""
    import re
    import subprocess

    ci = render(make_spec())[".github/workflows/gojira-ci.yaml"]
    step = yaml.safe_load(ci)["jobs"]["build-and-deploy-dev"]["steps"][1]
    assert step["name"] == "Platform variables for dev"
    env = {k: "x" for k in step["env"]}
    ok = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    env["GOJIRA_DEV_WIF_PROVIDER"] = ""
    bad = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
    assert bad.returncode == 1
    assert re.search(r"::error::GOJIRA_DEV_WIF_PROVIDER is not set", bad.stdout)


def test_every_golden_overlay_builds_with_kustomize(tmp_path):
    """The generated overlays must build as rendered (finding #83): the
    provisioner now builds them before opening a PR, and the generated CI
    builds once before its apply retries. kubectl's bundled kustomize is the
    builder in both places."""
    import shutil
    import subprocess

    if not shutil.which("kubectl"):
        pytest.skip("kubectl not on PATH")
    for case, spec in GOLDEN_CASES.items():
        root = tmp_path / case
        for relpath, content in render(spec).items():
            target = root / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        for env in spec.environments:
            overlay = root / "k8s" / "overlays" / env.name
            proc = subprocess.run(
                ["kubectl", "kustomize", str(overlay)], capture_output=True, text=True
            )
            assert proc.returncode == 0, f"{case}/{env.name}: {proc.stderr}"


def test_generated_ci_builds_once_before_the_apply_retries():
    """A build error is deterministic; it fails on the first attempt with
    kustomize's message, not after twenty 'waiting for cluster to settle'."""
    for spec in GOLDEN_CASES.values():
        ci = render(spec)[".github/workflows/gojira-ci.yaml"]
        build = ci.index("kubectl kustomize . > /dev/null")
        loop = ci.index("for attempt in $(seq 1 20)")
        assert build < loop


# --- issue #102: a build arg may reference a repository variable -----------


def test_build_arg_reference_reads_a_repository_variable():
    """`${NAME}` renders as the Actions variable and is required up front;
    a literal next to it is still shell-quoted into the command."""
    spec = GOLDEN_CASES["web"].model_copy(update={"build_args": {
        "EXTRA_INDEX_URL": "${GOJIRA_PYTHON_INDEX}",
        "BASE_IMAGE": "python:3.12-slim",
    }})
    assert spec.build_arg_refs() == ["GOJIRA_PYTHON_INDEX"]
    ci = render(spec)[".github/workflows/gojira-ci.yaml"]
    assert '--build-arg EXTRA_INDEX_URL="${{ vars.GOJIRA_PYTHON_INDEX }}"' in ci
    assert "--build-arg BASE_IMAGE=python:3.12-slim" in ci
    assert "GOJIRA_PYTHON_INDEX: ${{ vars.GOJIRA_PYTHON_INDEX }}" in ci    # the guard step
    assert "GOJIRA_PYTHON_INDEX" in ci.split("for name in ")[1].split("; do")[0]
    assert "pkg.dev" not in ci                                             # nothing of an install


def test_only_the_exact_reference_form_is_a_reference():
    from gojira.generator import variable_ref

    assert variable_ref("${GOJIRA_PYTHON_INDEX}") == "GOJIRA_PYTHON_INDEX"
    for literal in ("https://x/${GOJIRA_PYTHON_INDEX}", "${lower}", "$GOJIRA_X", "${GOJIRA-X}", ""):
        assert variable_ref(literal) is None


# --- issue #108: image: deploys a published image ---------------------------


def test_image_contract_round_trips_and_renders_in_place():
    from gojira.generator import read_app_needs

    spec = GOLDEN_CASES["image"]
    files = render(spec)
    assert read_app_needs(files["gojira.yaml"])["published_image"] == "ghcr.io/example/clips:1.4.0"
    assert "published_image" not in read_app_needs("app: x\nimage: ''\n")     # empty is absent
    assert "published_image" not in read_app_needs("app: x\nimage: [a]\n")    # not a string: ignored


def test_image_ci_copies_instead_of_building(tmp_path):
    """The copy step runs for real with a fake crane and gcloud on PATH: the
    published image lands in the install's registry under the commit's tag,
    the source registry is only logged into when it is a Google one, and no
    docker build happens anywhere in the workflow."""
    import subprocess

    ci = render(GOLDEN_CASES["image"])[".github/workflows/gojira-ci.yaml"]
    assert "docker build" not in ci and "--platform" not in ci and "setup-crane" in ci
    step = next(s for s in yaml.safe_load(ci)["jobs"]["build-and-deploy-dev"]["steps"]
                if s.get("name", "").startswith("Copy the published image"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    for tool in ("crane", "gcloud"):
        (bindir / tool).write_text(f"#!/usr/bin/env bash\necho \"{tool} $*\" >> {calls}\n")
        (bindir / tool).chmod(0o755)
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "IMAGE": "us-central1-docker.pkg.dev/proj/apps/clips-published",
           "GITHUB_SHA": "0123456789abcdef0123"}
    done = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    recorded = calls.read_text().splitlines()
    assert recorded == ["crane copy ghcr.io/example/clips:1.4.0 us-central1-docker.pkg.dev/proj/apps/clips-published:0123456789ab"]
    # a Google source registry gets a docker login first
    env["IMAGE"] = "europe-docker.pkg.dev/proj/apps/x"
    src = render(make_spec(app="x", published_image="us-central1-docker.pkg.dev/other/tenish-images/x:9"))
    step = next(s for s in yaml.safe_load(src[".github/workflows/gojira-ci.yaml"])["jobs"]["build-and-deploy-dev"]["steps"]
                if s.get("name", "").startswith("Copy the published image"))
    calls.write_text("")
    subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True, check=True)
    assert calls.read_text().splitlines()[0] == "gcloud auth configure-docker us-central1-docker.pkg.dev --quiet"


def test_image_promote_copies_the_contracts_image_where_it_would_have_built():
    """A build-from-scratch environment in the promote flow copies the
    contract's image; the final environment still crane-copies from its
    predecessor, as before."""
    promote = render(GOLDEN_CASES["image"])[".github/workflows/gojira-promote.yaml"]
    assert "docker build" not in promote
    assert promote.count("crane copy") == 2
    assert 'crane copy "$SOURCE" "$IMAGE:$GITHUB_REF_NAME"' in promote
    assert 'crane copy "$SOURCE_IMAGE:$GITHUB_REF_NAME"' in promote


def test_a_build_contract_renders_exactly_as_before():
    """No image: means the build path, byte for byte (the goldens pin it)."""
    ci = render(GOLDEN_CASES["web"])[".github/workflows/gojira-ci.yaml"]
    assert "docker build" in ci and "crane" not in ci
