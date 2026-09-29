"""GcloudClient command construction, via a fake runner (mirrors test_k8s.py)."""

import pytest
from fakes import RecordingRunner

from gojira.gcp import GcloudClient, GCPError
from gojira.shell import CommandResult


class FakeRunner:
    def __init__(self, result: CommandResult):
        self.result = result
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, args, stdin=None):
        self.calls.append((args, stdin))
        return self.result


def test_get_gke_credentials_writes_dns_endpoint():
    runner = FakeRunner(CommandResult(0, "", ""))
    client = GcloudClient(runner=runner)

    context = client.get_gke_credentials("p", "us-east4", "c")

    args = runner.calls[0][0]
    assert args[:4] == ["gcloud", "container", "clusters", "get-credentials"]
    assert args[4:] == ["c", "--location=us-east4", "--project=p", "--dns-endpoint"]
    assert context == "gke_p_us-east4_c"


# --- teardown: remove_project_binding -----------------------------------------


def test_remove_project_binding_happy_path():
    runner = RecordingRunner()
    client = GcloudClient(runner=runner)

    removed = client.remove_project_binding("p", "user:x@example.com", "roles/viewer")

    assert removed is True
    assert runner.calls == [
        [
            "gcloud", "projects", "remove-iam-policy-binding", "p",
            "--member=user:x@example.com", "--role=roles/viewer", "--quiet",
        ]
    ]


def test_remove_project_binding_not_found_returns_false_without_raising():
    runner = RecordingRunner(
        results={
            "remove-iam-policy-binding": CommandResult(
                1,
                "",
                "ERROR: (gcloud.projects.remove-iam-policy-binding) Policy binding "
                "with the specified principal and role not found!",
            )
        }
    )
    client = GcloudClient(runner=runner)

    removed = client.remove_project_binding("p", "user:x@example.com", "roles/viewer")

    assert removed is False


def test_remove_project_binding_other_failure_raises():
    runner = RecordingRunner(
        results={
            "remove-iam-policy-binding": CommandResult(
                1, "", "ERROR: (gcloud.projects.remove-iam-policy-binding) PERMISSION_DENIED"
            )
        }
    )
    client = GcloudClient(runner=runner)

    with pytest.raises(GCPError, match="failed"):
        client.remove_project_binding("p", "user:x@example.com", "roles/viewer")


# --- teardown: delete_service_account -----------------------------------------


def test_delete_service_account_happy_path():
    runner = RecordingRunner()
    client = GcloudClient(runner=runner)

    deleted = client.delete_service_account("p", "sa@p.iam.gserviceaccount.com")

    assert deleted is True
    assert runner.calls == [
        [
            "gcloud", "iam", "service-accounts", "describe", "sa@p.iam.gserviceaccount.com",
            "--project=p", "--format=value(email)",
        ],
        [
            "gcloud", "iam", "service-accounts", "delete", "sa@p.iam.gserviceaccount.com",
            "--project=p", "--quiet",
        ],
    ]


def test_delete_service_account_absent_issues_no_delete_call():
    runner = RecordingRunner(
        results={
            "describe": CommandResult(
                1, "", "ERROR: (gcloud.iam.service-accounts.describe) NOT_FOUND: "
                "Service account sa@p.iam.gserviceaccount.com does not exist."
            )
        }
    )
    client = GcloudClient(runner=runner)

    deleted = client.delete_service_account("p", "sa@p.iam.gserviceaccount.com")

    assert deleted is False
    assert len(runner.calls) == 1  # describe only, no delete issued


# --- teardown: delete_app_images ----------------------------------------------


def test_delete_app_images_happy_path_deletes_each_digest():
    runner = RecordingRunner(
        results={
            "images list": CommandResult(
                0, "sha256:aaa\nsha256:bbb\nsha256:ccc", ""
            )
        }
    )
    client = GcloudClient(runner=runner)

    count = client.delete_app_images("us-east4-docker.pkg.dev/p/repo", "app")

    assert count == 3
    assert runner.calls[0] == [
        "gcloud", "artifacts", "docker", "images", "list",
        "us-east4-docker.pkg.dev/p/repo/app", "--format=value(DIGEST)",
    ]
    assert runner.calls[1:] == [
        [
            "gcloud", "artifacts", "docker", "images", "delete",
            "us-east4-docker.pkg.dev/p/repo/app@sha256:aaa", "--delete-tags", "--quiet",
        ],
        [
            "gcloud", "artifacts", "docker", "images", "delete",
            "us-east4-docker.pkg.dev/p/repo/app@sha256:bbb", "--delete-tags", "--quiet",
        ],
        [
            "gcloud", "artifacts", "docker", "images", "delete",
            "us-east4-docker.pkg.dev/p/repo/app@sha256:ccc", "--delete-tags", "--quiet",
        ],
    ]


def test_delete_app_images_empty_list_issues_no_delete_calls():
    runner = RecordingRunner(results={"images list": CommandResult(0, "", "")})
    client = GcloudClient(runner=runner)

    count = client.delete_app_images("us-east4-docker.pkg.dev/p/repo", "app")

    assert count == 0
    assert len(runner.calls) == 1  # list only, no delete issued


def test_delete_app_images_repo_not_found_returns_zero():
    runner = RecordingRunner(
        results={
            "images list": CommandResult(
                1, "", "ERROR: (gcloud.artifacts.docker.images.list) NOT_FOUND: "
                "Repository \"repo\" not found."
            )
        }
    )
    client = GcloudClient(runner=runner)

    count = client.delete_app_images("us-east4-docker.pkg.dev/p/repo", "app")

    assert count == 0
    assert len(runner.calls) == 1


# --- image_digest: tags list, never images describe ---------------------------


def test_image_digest_uses_tags_list_not_describe():
    """images describe smuggles a Container Analysis call
    (containeranalysis.occurrences.list) into a digest lookup — a permission
    no AR role carries. First rollback dogfood finding, 2026-09-17."""
    runner = FakeRunner(CommandResult(0, "cafe12\tsha256:abc123\n", ""))
    client = GcloudClient(runner=runner)

    digest = client.image_digest("us-east4-docker.pkg.dev/p/apps/web", "cafe12")

    args = runner.calls[0][0]
    assert args[:5] == ["gcloud", "artifacts", "docker", "tags", "list"]
    assert "describe" not in args
    assert "--filter=tag:cafe12" in args and "--format=value(tag,version)" in args
    assert digest == "sha256:abc123"


def test_image_digest_picks_the_exact_tag_among_prefix_matches():
    """gcloud's tag: filter is a prefix match. Live, 2026-09-29: tag:dev
    returned three rows, and value(version) alone would have produced a
    three-line 'digest'. The exact tag wins; a prefix-only match is None."""
    rows = "cafe12ab\tsha256:wrong1\ncafe12\tsha256:right\ncafe1200\tsha256:wrong2\n"
    assert GcloudClient(runner=FakeRunner(CommandResult(0, rows, ""))).image_digest("r/x", "cafe12") == "sha256:right"
    assert GcloudClient(runner=FakeRunner(CommandResult(0, "cafe12ab\tsha256:wrong1\n", ""))).image_digest("r/x", "cafe12") is None


def test_image_digest_not_found_returns_none():
    runner = FakeRunner(CommandResult(1, "", "NOT_FOUND: no such repo"))
    assert GcloudClient(runner=runner).image_digest("r/x", "t") is None


def test_image_digest_other_failure_raises():
    runner = FakeRunner(CommandResult(1, "", "PERMISSION_DENIED"))
    with pytest.raises(GCPError, match="tags list"):
        GcloudClient(runner=runner).image_digest("r/x", "t")
