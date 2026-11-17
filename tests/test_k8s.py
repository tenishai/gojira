"""KubectlCLI command construction and not-found handling, via a fake runner."""

import pytest

from gojira.k8s import K8sError, KubectlCLI
from gojira.shell import CommandResult


class FakeRunner:
    def __init__(self, result: CommandResult):
        self.result = result
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, args, stdin=None):
        self.calls.append((args, stdin))
        return self.result


def test_namespace_exists_true():
    runner = FakeRunner(CommandResult(0, "namespace/clips", ""))
    cli = KubectlCLI("gke_p_us-central1_c", runner=runner)
    assert cli.namespace_exists("clips") is True
    args = runner.calls[0][0]
    assert args[:2] == ["kubectl", "--context=gke_p_us-central1_c"]
    assert args[2:] == ["get", "namespace", "clips", "-o", "name"]


def test_namespace_exists_false_on_notfound():
    runner = FakeRunner(CommandResult(1, "", 'namespaces "clips" not found (NotFound)'))
    cli = KubectlCLI("ctx", runner=runner)
    assert cli.namespace_exists("clips") is False


def test_namespace_exists_raises_on_other_error():
    runner = FakeRunner(CommandResult(1, "", "Unable to connect to the server: timeout"))
    cli = KubectlCLI("ctx", runner=runner)
    with pytest.raises(K8sError, match="failed"):
        cli.namespace_exists("clips")


def test_ensure_namespace_applies_manifest_with_labels():
    runner = FakeRunner(CommandResult(0, "namespace/clips configured", ""))
    cli = KubectlCLI("ctx", runner=runner)
    cli.ensure_namespace("clips", {"gojira.dev/managed": "true"})

    args, stdin = runner.calls[0]
    assert args[2:] == ["apply", "-f", "-"]
    assert "kind: Namespace" in stdin
    assert "name: clips" in stdin
    assert "gojira.dev/managed: 'true'" in stdin


def test_label_namespace_overwrites_only_the_named_labels():
    runner = FakeRunner(CommandResult(0, "namespace/clips labeled", ""))
    cli = KubectlCLI("ctx", runner=runner)
    cli.label_namespace("clips", {"sero.dev/caller": "true"})
    args, stdin = runner.calls[0]
    assert args[2:] == ["label", "--overwrite", "namespace", "clips", "sero.dev/caller=true"]
    assert stdin is None


def test_label_namespace_raises_on_failure():
    runner = FakeRunner(CommandResult(1, "", "forbidden"))
    cli = KubectlCLI("ctx", runner=runner)
    with pytest.raises(K8sError, match="label namespace clips failed"):
        cli.label_namespace("clips", {"a": "b"})


def test_ensure_namespace_raises_on_failure():
    runner = FakeRunner(CommandResult(1, "", "forbidden"))
    cli = KubectlCLI("ctx", runner=runner)
    with pytest.raises(K8sError, match="failed"):
        cli.ensure_namespace("clips", {})
