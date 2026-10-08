"""Every job in the repo's own CI keeps fork PRs off the self-hosted runner."""

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yaml"
FORK_SAFE = (
    "${{ github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name != github.repository && "
    "'ubuntu-latest' || vars.CI_RUNNER || 'ubuntu-latest' }}"
)


def test_every_job_sends_fork_prs_to_a_hosted_runner():
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    assert jobs
    for name, job in jobs.items():
        assert job["runs-on"] == FORK_SAFE, f"job {name!r} can reach the self-hosted runner from a fork PR"


def test_every_action_is_pinned_to_a_commit_sha():
    """A tag can be moved after review; a commit SHA cannot."""
    pinned = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    unpinned = [
        step["uses"]
        for job in jobs.values()
        for step in job.get("steps", [])
        if "uses" in step and not step["uses"].startswith("./") and not pinned.match(step["uses"])
    ]
    assert not unpinned, f"pin these to a full commit SHA with a `# vX.Y.Z` comment: {unpinned}"
