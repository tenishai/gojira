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
    """Fake whose clone drops a gojira.yaml into the tree."""

    def clone(self, repo, workdir):
        super().clone(repo, workdir)
        (workdir / "gojira.yaml").write_text(GOJIRA_YAML)


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