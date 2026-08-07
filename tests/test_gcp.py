"""GcloudClient command construction, via a fake runner (mirrors test_k8s.py)."""

from gojira.gcp import GcloudClient
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
