"""Override-PR authoring: gojira.yaml editing keeps comments; the PR flow
respects the one-open-PR rule and recommendation lifecycle."""

import json

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gojira.models import (
    App,
    Base,
    RecommendationKind,
    RecommendationStatus,
    SizingRecommendation,
)
from gojira.sizing.pr import apply_overrides, open_override_pr, render_overrides_block
from tests.fakes import FakeGitHubClient

GOJIRA_YAML = """\
# Gojira app spec — this file is the contract between your repo and Gojira.
# Change the class or add overrides here; Gojira regenerates the manifests
# when this file changes.
app: clips-api
class: web
environments:
  - name: dev
    cluster: sandbox
    namespace: clips
# overrides:                  # needs a justification — see docs
#   requests: { cpu: "1", memory: 1Gi }
#   justification: "measured p99 over 14 days"
"""

PROPOSED = {"cpu": {"request": "800m", "limit": "1250m"}}


def test_append_preserves_comments_and_parses():
    block = render_overrides_block(PROPOSED, "cpu: p95 760m over 14d")
    out = apply_overrides(GOJIRA_YAML, block)
    # Every original comment line survives the edit.
    assert "# Gojira app spec" in out and "# overrides:" in out
    parsed = yaml.safe_load(out)
    assert parsed["overrides"]["requests"] == {"cpu": "800m"}
    assert parsed["overrides"]["limits"] == {"cpu": "1250m"}
    assert "p95 760m" in parsed["overrides"]["justification"]
    assert parsed["app"] == "clips-api"  # rest of the contract untouched


def test_replace_existing_block_instead_of_stacking():
    once = apply_overrides(GOJIRA_YAML, render_overrides_block(PROPOSED, "first"))
    twice = apply_overrides(
        once + "class2: ignored\n",  # a trailing top-level key after the block
        render_overrides_block({"cpu": {"request": "1", "limit": "2"}}, "second"),
    )
    parsed = yaml.safe_load(twice)
    assert parsed["overrides"]["requests"] == {"cpu": "1"}
    assert "second" in parsed["overrides"]["justification"]
    assert twice.count("overrides:\n") == 1  # replaced, not appended again
    assert parsed["class2"] == "ignored"  # following keys survive the replace


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def app_rec(session):
    app = App(name="clips-api", repo_full_name="acme/clips-api",
              installation_id=1, workload_class="web")
    session.add(app)
    session.commit()
    rec = SizingRecommendation(
        app_id=app.id, environment="dev", kind=RecommendationKind.OVERRIDE,
        current_json=json.dumps({"cpu": {"request": "250m", "limit": "500m"}}),
        proposed_json=json.dumps(PROPOSED),
        justification="cpu: p95 760m, p99 820m over 14d (20160 samples)",
    )
    session.add(rec)
    session.commit()
    return app, rec


class SpecRepoGitHub(FakeGitHubClient):
    """Fake whose clone drops a gojira.yaml + generated deployment into the tree."""

    def clone(self, repo, workdir):
        super().clone(repo, workdir)
        (workdir / "gojira.yaml").write_text(GOJIRA_YAML)
        from gojira.generator import EnvSpec, GeneratorSpec, render

        spec = GeneratorSpec(
            app="clips-api", repo_full_name=repo, workload_class="web",
            environments=[EnvSpec(
                name="dev", namespace="clips", project="p", cluster_name="sandbox",
                location="l", gke_cluster="g", ar_repo="l-docker.pkg.dev/p/apps",
                wif_provider="projects/1/locations/global/workloadIdentityPools/g/providers/gh",
                gsa_email="d@p.iam.gserviceaccount.com",
            )],
        )
        files = render(spec)
        for path in ("k8s/base/deployment.yaml", ".github/workflows/gojira-ci.yaml"):
            target = workdir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(files[path])


def test_open_pr_flow(session, app_rec, tmp_path):
    app, rec = app_rec
    github = SpecRepoGitHub()

    url = open_override_pr(session, app, rec, github, tmp_path)

    assert url == "https://github.com/acme/clips-api/pull/1"
    assert rec.status == RecommendationStatus.PR_OPENED and rec.pr_url == url
    assert github.branch == "gojira/sizing-dev"
    edited = (tmp_path / "gojira.yaml").read_text()
    assert yaml.safe_load(edited)["overrides"]["requests"] == {"cpu": "800m"}
    assert "# Gojira app spec" in edited
    # The manifest moved in the same PR — merging it actually resizes.
    dep = yaml.safe_load((tmp_path / "k8s" / "base" / "deployment.yaml").read_text())
    res = dep["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert res["requests"]["cpu"] == "800m" and res["limits"]["cpu"] == "1250m"
    assert res["requests"]["memory"] == "256Mi"  # untouched
    # Finding #20: the contract changed textually, so the workflow's stamp
    # must follow — or merging this PR fails every deploy as "stale".
    from gojira.generator import contract_fingerprint
    wf = (tmp_path / ".github" / "workflows" / "gojira-ci.yaml").read_text()
    assert f"GOJIRA_CONTRACT: {contract_fingerprint(edited)}" in wf


def test_existing_pr_reused_not_reauthored(session, app_rec, tmp_path):
    app, rec = app_rec
    github = SpecRepoGitHub()
    github.prs["gojira/sizing-dev"] = "https://github.com/acme/clips-api/pull/7"

    url = open_override_pr(session, app, rec, github, tmp_path)

    assert url.endswith("/7") and rec.status == RecommendationStatus.PR_OPENED
    assert github.cloned is None  # short-circuited before any git work


def test_non_open_recommendation_is_a_noop(session, app_rec, tmp_path):
    app, rec = app_rec
    rec.status = RecommendationStatus.DISMISSED
    session.commit()
    assert open_override_pr(session, app, rec, SpecRepoGitHub(), tmp_path) is None
    assert SpecRepoGitHub().cloned is None


def test_missing_gojira_yaml_skips(session, app_rec, tmp_path):
    app, rec = app_rec
    url = open_override_pr(session, app, rec, FakeGitHubClient(), tmp_path)
    assert url is None
    assert rec.status == RecommendationStatus.OPEN  # untouched, retryable

# --- finding #9: the sizing PR retires the drift patch it supersedes ---------

from gojira.sizing.pr import retire_superseded_drift  # noqa: E402

DRIFT_PATCH = """\
# Captured by Gojira's drift detector — the live cluster state.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: clips-api
spec:
  template:
    spec:
      containers:
      - name: app
        resources:
          requests:
            cpu: 750m
            memory: 768Mi
            ephemeral-storage: 4Gi
          limits:
            cpu: 1500m
            memory: 768Mi
            ephemeral-storage: 4Gi
        readinessProbe:
          initialDelaySeconds: 5
          periodSeconds: 10
          httpGet:
            path: /health
            port: http
"""


def _overlay(tmp_path, patch_text=DRIFT_PATCH):
    overlay = tmp_path / "k8s" / "overlays" / "dev"
    overlay.mkdir(parents=True)
    (overlay / "drift-clips-api.yaml").write_text(patch_text)
    (overlay / "kustomization.yaml").write_text(
        "# Generated by Gojira.\n"
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources:\n- ../../base\n"
        "patches:\n- path: drift-clips-api.yaml\n"
    )
    return overlay


def test_retire_removes_superseded_fields_keeps_the_rest(tmp_path):
    overlay = _overlay(tmp_path)
    assert retire_superseded_drift(overlay, "clips-api", {"cpu", "memory"})
    doc = yaml.safe_load((overlay / "drift-clips-api.yaml").read_text())
    res = doc["spec"]["template"]["spec"]["containers"][0]["resources"]
    # cpu/memory retired (sizing supersedes); ephemeral + probe survive
    assert res == {"requests": {"ephemeral-storage": "4Gi"},
                   "limits": {"ephemeral-storage": "4Gi"}}
    assert "readinessProbe" in yaml.safe_load(
        (overlay / "drift-clips-api.yaml").read_text()
    )["spec"]["template"]["spec"]["containers"][0]
    # still wired — the patch still says something
    assert "drift-clips-api.yaml" in (overlay / "kustomization.yaml").read_text()


def test_fully_superseded_patch_is_removed_and_unwired(tmp_path):
    only_resources = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: clips-api
spec:
  template:
    spec:
      containers:
      - name: app
        resources:
          requests:
            cpu: 750m
          limits:
            cpu: 1500m
"""
    overlay = _overlay(tmp_path, only_resources)
    assert retire_superseded_drift(overlay, "clips-api", {"cpu"})
    assert not (overlay / "drift-clips-api.yaml").exists()
    kust = (overlay / "kustomization.yaml").read_text()
    assert "drift-clips-api" not in kust and "patches:" not in kust
    assert "# Generated by Gojira." in kust          # comments survive the unwire
    assert yaml.safe_load(kust)["resources"] == ["../../base"]


def test_no_patch_is_a_noop(tmp_path):
    overlay = tmp_path / "k8s" / "overlays" / "dev"
    overlay.mkdir(parents=True)
    assert retire_superseded_drift(overlay, "clips-api", {"cpu"}) is False


def test_open_pr_flow_retires_drift_patch(session, app_rec, tmp_path):
    """End to end: the sizing PR carries contract + manifest + drift retirement."""
    class DriftyRepo(SpecRepoGitHub):
        def clone(self, repo, workdir):
            super().clone(repo, workdir)
            _overlay(workdir)

    app, rec = app_rec
    url = open_override_pr(session, app, rec, DriftyRepo(), tmp_path)
    assert url is not None
    doc = yaml.safe_load((tmp_path / "k8s/overlays/dev/drift-clips-api.yaml").read_text())
    res = doc["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert "cpu" not in res.get("requests", {})       # PROPOSED covers cpu only
    assert res["requests"]["memory"] == "768Mi"       # memory not proposed -> kept


def test_sizing_pr_block_carries_the_apps_hpa_bounds():
    """The sizing loop rewrites the whole overrides block; the team's
    scaling bounds must survive a request resize."""
    from gojira.generator import HpaOverride, read_overrides
    from gojira.sizing.pr import apply_overrides, render_overrides_block

    before = "app: x\nclass: web\noverrides:\n  requests: { cpu: \"1\" }\n  hpa: { min: 30, max: 500, memory: 70 }\n  justification: \"old\"\n"
    block = render_overrides_block({"cpu": {"request": "800m", "limit": "1250m"}}, "measured", hpa=read_overrides(before).hpa)
    after = apply_overrides(before, block)
    got = read_overrides(after)
    assert got.requests == {"cpu": "800m"} and got.hpa == HpaOverride(min=30, max=500, memory=70)
    assert got.justification == "measured"
