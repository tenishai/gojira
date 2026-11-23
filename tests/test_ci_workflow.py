"""The repo's own CI: GitHub-hosted runners only, and one `ci` summary job that
is green only when every other job is (agora discussion 71, rule 4)."""

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yaml"


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def test_every_job_runs_on_a_github_hosted_runner():
    jobs = _jobs()
    assert jobs
    for name, job in jobs.items():
        assert job["runs-on"] == "ubuntu-latest", f"job {name!r} could reach a self-hosted runner"


def test_the_ci_job_needs_every_other_job_and_fails_unless_all_passed():
    jobs = _jobs()
    ci = jobs["ci"]
    others = sorted(n for n in jobs if n != "ci")
    assert sorted(ci["needs"]) == others, "a job the summary doesn't need can fail without failing `ci`"
    assert ci["if"] == "always()", "a skipped `ci` would let a failed job pass the ruleset"
    expected = ",".join(["success"] * len(others))
    assert any(f'= "{expected}"' in step.get("run", "") for step in ci["steps"]), \
        "the summary must compare every needed result with success"


def test_every_action_is_pinned_to_a_commit_sha():
    """A tag can be moved after review; a commit SHA cannot."""
    pinned = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
    unpinned = [
        step["uses"]
        for job in _jobs().values()
        for step in job.get("steps", [])
        if "uses" in step and not step["uses"].startswith("./") and not pinned.match(step["uses"])
    ]
    assert not unpinned, f"pin these to a full commit SHA with a `# vX.Y.Z` comment: {unpinned}"
