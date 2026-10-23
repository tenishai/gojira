"""Suite-wide environment.

kubectl must never reach a real cluster from a test, and must never wait on one.
The suite runs the real `kubectl kustomize` (generator goldens, GenerateArtifacts
builds every overlay). gcloud's kubectl is a dispatcher: before any subcommand,
even an offline one, it connects to the current context's server to pick a
client version. With no kubeconfig that server is localhost:8080, so whatever
happens to listen there decides how long every overlay build takes; on
2026-10-23 a stalled port-forward on a developer box made each build wait 5s and
the web tests time out, on main as much as on a branch. A kubeconfig whose
server refuses instantly makes the suite deterministic on any machine.
"""
import pytest

_NULL_KUBECONFIG = """\
apiVersion: v1
kind: Config
current-context: none
contexts:
- name: none
  context: {cluster: none, user: none}
clusters:
- name: none
  cluster: {server: "http://127.0.0.1:1"}
users:
- name: none
  user: {}
"""


@pytest.fixture(scope="session", autouse=True)
def kubectl_sees_no_cluster(tmp_path_factory):
    path = tmp_path_factory.mktemp("kube") / "config"
    path.write_text(_NULL_KUBECONFIG)
    mp = pytest.MonkeyPatch()
    mp.setenv("KUBECONFIG", str(path))
    yield
    mp.undo()
