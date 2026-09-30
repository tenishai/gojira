"""scripts/install.sh with GOJIRA_TERRAFORM=1: one command from an empty
project to a running Gojira — and never the command that replaces a cluster.

`make install` is rerun freely (it is also the upgrade). Terraform under it
must therefore stop at any plan that deletes something and leave that for a
person to read. Runs the real install.sh and tf.sh against a fake terraform
and a stub bootstrap.sh.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE = r"""#!/usr/bin/env bash
tool="$(basename "$0")"
echo "$tool $* [project=$TF_VAR_project_id cluster=$TF_VAR_cluster_name token=$GOOGLE_OAUTH_ACCESS_TOKEN]" >> "$CALLS"
case "$tool $*" in
  "gcloud auth print-access-token"*) echo tok ;;
  "terraform "*" show -json"*) cat "$PLAN_JSON" ;;
esac
exit 0
"""


def _plan(*actions: list[str]) -> str:
    return json.dumps({"resource_changes": [
        {"address": f"google_thing.r{i}", "change": {"actions": a}} for i, a in enumerate(actions)
    ]})


@pytest.fixture
def run(tmp_path):
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "terraform").mkdir()
    for name in ("install.sh", "env.sh", "tf.sh"):
        (tree / "scripts" / name).write_text((ROOT / "scripts" / name).read_text())
    calls = tmp_path / "calls"
    for name in ("bootstrap.sh", "prepare-project.sh"):
        (tree / "scripts" / name).write_text(f'#!/usr/bin/env bash\necho "{name}" >> "$CALLS"\n')
        (tree / "scripts" / name).chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("gcloud", "terraform"):
        (bindir / tool).write_text(FAKE)
        (bindir / tool).chmod(0o755)
    plan = tmp_path / "plan.json"

    def _run(plan_json=_plan(["create"]), tfvars=None, **env):
        calls.write_text("")
        plan.write_text(plan_json)
        if tfvars is not None:
            (tree / "terraform" / "terraform.tfvars").write_text(tfvars)
        done = subprocess.run(
            ["bash", str(tree / "scripts" / "install.sh")], capture_output=True, text=True,
            env={
                "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path),
                "CALLS": str(calls), "PLAN_JSON": str(plan),
                "GOJIRA_ENV": str(tmp_path / "absent.env"),
                "PROJECT": "proj", "CLUSTER": "mine", **env,
            },
        )
        return done, calls.read_text().splitlines()

    return _run


def test_terraform_runs_before_bootstrap_with_the_values_from_gojira_env(run):
    done, calls = run(GOJIRA_TERRAFORM="1")
    assert done.returncode == 0, done.stderr
    order = [next(i for i, c in enumerate(calls) if needle in c)
             for needle in (" init ", " plan ", " apply ", "bootstrap.sh")]
    assert order == sorted(order)
    applied = next(c for c in calls if " apply " in c)
    assert "[project=proj cluster=mine token=tok]" in applied  # no tfvars, no second login


def test_without_the_setting_terraform_is_never_touched(run):
    done, calls = run()
    assert done.returncode == 0
    assert calls == ["bootstrap.sh"]


@pytest.mark.parametrize("actions", [["delete"], ["delete", "create"]])
def test_a_plan_that_deletes_or_replaces_is_not_applied(run, actions):
    """A provider upgrade that forces a new cluster must not take Gojira's
    database with it because someone reran make install."""
    done, calls = run(_plan(["create"], actions), GOJIRA_TERRAFORM="1")
    assert done.returncode == 1
    assert "google_thing.r1" in done.stderr and "Review it yourself" in done.stderr
    assert not [c for c in calls if " apply " in c]
    assert "bootstrap.sh" not in calls


def test_a_no_op_plan_is_applied_and_install_goes_on(run):
    done, calls = run(_plan(["no-op"], ["update"]), GOJIRA_TERRAFORM="1")
    assert done.returncode == 0 and calls[-1] == "bootstrap.sh"


def test_terraform_and_prepare_project_together_are_refused(run):
    done, calls = run(GOJIRA_TERRAFORM="1", GOJIRA_PREPARE_PROJECT="1")
    assert done.returncode == 1 and "Pick one" in done.stderr
    assert calls == []


def test_a_tfvars_file_for_another_project_is_refused(run):
    """terraform.tfvars beats TF_VAR_*: a stale one would build in the wrong
    project while every other step used the right one."""
    done, calls = run(tfvars='project_id = "other"\n', GOJIRA_TERRAFORM="1")
    assert done.returncode == 1 and "names another project" in done.stderr
    assert not [c for c in calls if c.startswith("terraform")]
    done, _ = run(tfvars='project_id = "proj"\nregion = "us-central1"\n', GOJIRA_TERRAFORM="1")
    assert done.returncode == 0
