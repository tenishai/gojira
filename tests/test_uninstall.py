"""scripts/uninstall.sh is an ordering contract.

Gojira's record of what it created lives in the cluster it runs in: apps
have to be removed through Gojira before its namespace goes, and the
namespace (which releases the data disk — GKE keeps disks when a cluster is
deleted) before the cluster. These tests run the real script against fake
gcloud / kubectl / curl / terraform that log every call.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE = r"""#!/usr/bin/env bash
tool="$(basename "$0")"
echo "$tool $*" >> "$CALLS"
case "$tool $*" in
  "gcloud container clusters describe"*) [ -n "$NO_CLUSTER" ] && exit 1; exit 0 ;;
  "gcloud projects describe"*) echo 123 ;;
  "gcloud auth print-access-token"*) echo tok ;;
  "gcloud iam workload-identity-pools describe"*) echo "${POOL_STATE:-ACTIVE}" ;;
  "gcloud iam service-accounts list"*) [ -n "$LEFT_SA" ] && ! grep -q "service-accounts delete $LEFT_SA" "$CALLS" && echo "$LEFT_SA" ;;
  "gcloud projects get-iam-policy"*"--filter=bindings.members:serviceAccount:"*) echo roles/container.developer ;;
  "gcloud projects get-iam-policy"*) ;;
  "gcloud compute disks list"*) ;;
  "gcloud artifacts docker images describe"*apps/hello*) exit 0 ;;
  "gcloud artifacts docker images describe"*) exit 1 ;;
  "gcloud artifacts docker images list"*) for i in ${OTHER_IMAGES:-}; do echo "us-central1-docker.pkg.dev/proj/apps/$i"; done ;;
  "gcloud compute addresses describe"*) exit 1 ;;
  "gcloud artifacts repositories describe gojira"*)
    grep -q -E "terraform .*destroy|repositories delete gojira" "$CALLS" && exit 1; exit 0 ;;
  "kubectl -n gojira get secret"*) printf dG9r ;;
  "kubectl -n gojira port-forward"*) exec sleep 30 ;;
  "kubectl -n gojira get ingress"*) [ -n "$FRONT_DOOR" ] && echo ingress.networking.k8s.io/gojira ;;
  "curl"*"-X DELETE"*) cat "$FIXTURES/delete.json" ;;
  "curl"*"/api/v1/apps") [ -n "$OLD_IMAGE" ] && exit 22; cat "$FIXTURES/apps.json" ;;
  "curl"*"/api/v1/clusters") echo '{"clusters": []}' ;;
esac
exit 0
"""

DELETED = {
    "app": "hello", "namespaces_deleted": ["hello"], "namespace_failures": [],
    "identity": {"bindings_removed": 2, "gsa_deleted": True, "images_deleted": 1,
                 "images_skipped": None, "failures": []},
    "rows_deleted": True,
}


@pytest.fixture
def run(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("gcloud", "kubectl", "curl", "terraform"):
        (bindir / tool).write_text(FAKE)
        (bindir / tool).chmod(0o755)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "apps.json").write_text(json.dumps(
        {"apps": [{"id": 1, "name": "hello"}, {"id": 2, "name": "api"}]}))
    (fixtures / "delete.json").write_text(json.dumps(DELETED))
    calls = tmp_path / "calls"

    def _run(*args, stdin="", delete=DELETED, **env):
        calls.write_text("")
        (fixtures / "delete.json").write_text(json.dumps(delete))
        done = subprocess.run(
            ["bash", str(ROOT / "scripts" / "uninstall.sh"), *args],
            input=stdin, capture_output=True, text=True,
            env={
                "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path),
                "CALLS": str(calls), "FIXTURES": str(fixtures),
                "GOJIRA_ENV": str(tmp_path / "absent.env"), "PROJECT": "proj",
                "GOJIRA_PREPARE_PROJECT": "1", **env,
            },
        )
        return done, calls.read_text().splitlines()

    return _run


def _first(calls: list[str], needle: str) -> int:
    hits = [i for i, c in enumerate(calls) if needle in c]
    assert hits, f"never called: {needle}"
    return hits[0]


def _mutations(calls: list[str]) -> list[str]:
    verbs = (" delete", " destroy", " rm ", "remove-iam-policy-binding", "-X DELETE")
    return [c for c in calls if any(v in c for v in verbs)]


def test_apps_go_before_the_namespace_and_the_namespace_before_the_platform(run):
    done, calls = run("--yes")
    assert done.returncode == 0, done.stdout + done.stderr
    assert sum("-X DELETE" in c for c in calls) == 2  # every app, through Gojira
    assert (
        _first(calls, "/api/v1/apps/1")
        < _first(calls, "kubectl delete namespace gojira")
        < _first(calls, "gcloud iam service-accounts delete gojira@proj")
    )
    assert "Gojira is removed" in done.stdout


def test_a_cluster_that_was_never_gojiras_is_never_deleted(run):
    _, calls = run("--yes", "--purge")
    assert not [c for c in calls if "clusters delete" in c or c.startswith("terraform")]


def test_shared_resources_survive_without_purge(run):
    _, calls = run("--yes")
    shared = ("workload-identity-pools delete", "repositories delete apps", "storage rm")
    assert not [c for c in calls if any(s in c for s in shared)]
    _, calls = run("--yes", "--purge")
    assert all(any(s in c for c in calls) for s in shared)


def test_nothing_happens_without_the_project_id(run):
    done, calls = run(stdin="yes\n")
    assert done.returncode == 1 and "not removed" in done.stdout
    assert _mutations(calls) == []
    done, calls = run(stdin="proj\n")
    assert done.returncode == 0 and _mutations(calls)


def test_check_changes_nothing_and_judges_nothing(run):
    """--check on a healthy install lists everything Gojira has. It read as a
    failed teardown the first time Shaz ran it (2026-09-30): "left" lines and
    exit 1 before any uninstall. Now it is an inventory: "found", exit 0."""
    done, calls = run("--check")
    assert _mutations(calls) == []
    assert not [c for c in calls if c.startswith(("kubectl", "curl", "terraform"))]
    assert done.returncode == 0
    assert "found image repo gojira" in done.stdout and "left" not in done.stdout
    assert "only kept lines should remain" in done.stdout


def test_an_app_gojira_could_not_fully_remove_fails_the_uninstall(run):
    broken = json.loads(json.dumps(DELETED))
    broken["identity"]["failures"] = ["PERMISSION_DENIED: cannot delete hello-deploy@"]
    done, calls = run("--yes", delete=broken)
    assert done.returncode == 1
    assert "FAILED: PERMISSION_DENIED" in done.stdout and "NOT fully removed" in done.stderr
    assert _first(calls, "kubectl delete namespace gojira")  # the rest still ran


def test_a_gojira_older_than_the_script_is_upgraded_not_guessed_at(run):
    done, calls = run("--yes", OLD_IMAGE="1")
    assert done.returncode == 1 and "make install" in done.stderr
    assert _mutations(calls) == []


def test_the_front_door_goes_before_the_namespace(run):
    _, calls = run("--yes", FRONT_DOOR="1")
    assert _first(calls, "delete ingress --all") < _first(calls, "kubectl delete namespace gojira")


def test_with_the_cluster_already_gone_the_project_is_still_cleaned(run):
    done, calls = run("--yes", NO_CLUSTER="1")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not [c for c in calls if c.startswith(("kubectl", "curl"))]
    assert _first(calls, "gcloud iam service-accounts delete gojira@proj")


def test_a_rerun_after_a_partial_purge_does_not_die_on_the_pool_it_already_deleted(run):
    """Google keeps a deleted identity pool answering describe for 30 days
    (state DELETED); deleting it again is NOT_FOUND. The first purge of
    2026-10-14 deleted the pool and stopped later; the rerun died here before
    reaching the sweep. Only an ACTIVE pool is deleted; a deleted one is neither
    deleted nor listed as left."""
    done, calls = run("--purge", "--yes", POOL_STATE="DELETED", NO_CLUSTER="1")
    assert "6/6 what is left" in done.stdout            # reached the sweep (the fake's repo and bucket always exist)
    assert not [c for c in calls if "workload-identity-pools delete" in c]
    assert "left  identity pool" not in done.stdout


def test_terraform_state_for_this_project_means_terraform_takes_it_down(run, tmp_path):
    """The script reads the repo's own terraform/ dir, so point a copy of the
    script at a tree whose state names the project."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "terraform").mkdir()
    for name in ("uninstall.sh", "env.sh", "tf.sh"):
        (tree / "scripts" / name).write_text((ROOT / "scripts" / name).read_text())
    (tree / "terraform" / "terraform.tfstate").write_text('{"x": {"project": "proj"}}')
    bindir = tmp_path / "bin"
    calls = tmp_path / "calls"
    calls.write_text("")
    done = subprocess.run(
        ["bash", str(tree / "scripts" / "uninstall.sh"), "--yes"],
        capture_output=True, text=True,
        env={
            "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path),
            "CALLS": str(calls), "FIXTURES": str(tmp_path / "fixtures"),
            "GOJIRA_ENV": str(tmp_path / "absent.env"), "PROJECT": "proj",
        },
    )
    lines = calls.read_text().splitlines()
    assert _first(lines, "kubectl delete namespace gojira") < _first(lines, "terraform -chdir=")
    assert "destroy -auto-approve" in lines[_first(lines, "terraform -chdir=")]
    assert not [c for c in lines if "service-accounts delete" in c]  # terraform's job
    # the fake cluster never disappears, and the sweep says so rather than lie
    assert done.returncode == 1 and "left  cluster" in done.stdout


# --- the purge finishes a failed app teardown, and the run leaves a record --


def test_purge_removes_a_deploy_identity_a_failed_teardown_left(run, tmp_path):
    """An app whose delete failed partway leaves `<app>-deploy@`; under --purge
    the script recognises Gojira's own naming and removes it rather than
    listing it as 'left' for the operator (2026-10-08, liminal)."""
    broken = json.loads(json.dumps(DELETED))
    broken["identity"]["failures"] = ["PERMISSION_DENIED: cannot delete hello-deploy@"]
    done, calls = run("--yes", "--purge", delete=broken,
                      LEFT_SA="hello-deploy@proj.iam.gserviceaccount.com")
    assert _first(calls, "gcloud iam service-accounts delete hello-deploy@proj.iam.gserviceaccount.com")
    assert "deleted hello-deploy@proj.iam.gserviceaccount.com (left by a failed app teardown)" in done.stdout
    assert done.returncode == 1                     # the app's own failure is still reported
    assert "transcript:" in done.stdout


def test_purge_deletes_the_removed_apps_images_and_names_the_rest(run):
    done, calls = run("--yes", "--purge", OTHER_IMAGES="someone-elses")
    assert _first(calls, "gcloud artifacts docker images delete") and "apps/hello" in "\n".join(calls)
    assert "image repo apps kept: it holds images Gojira did not build (someone-elses)" in done.stdout
    assert not [c for c in calls if "repositories delete apps" in c]


def test_without_purge_nothing_of_the_leftover_identity_is_touched(run):
    done, calls = run("--yes", LEFT_SA="hello-deploy@proj.iam.gserviceaccount.com")
    assert not [c for c in calls if "service-accounts delete hello-deploy" in c]


def test_the_transcript_is_written_where_the_operator_can_find_it(run, tmp_path):
    done, _ = run("--yes", GOJIRA_UNINSTALL_LOG=str(tmp_path / "t.log"))
    text = (tmp_path / "t.log").read_text()
    assert "1/6 connected apps" in text and "removing hello" in text
    assert f"transcript: {tmp_path / 't.log'}" in done.stdout


def test_check_writes_no_transcript(run, tmp_path):
    run("--check", GOJIRA_UNINSTALL_LOG=str(tmp_path / "t.log"))
    assert not (tmp_path / "t.log").exists()
