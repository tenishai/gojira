"""GitHubAppClient: auth chain, REST surface, and token-authed git.

A stateful FakeGitHubServer answers api.github.com over httpx.MockTransport;
git is a RecordingRunner so we can assert the clone URL without running git.
A throwaway RSA keypair exercises the real RS256 JWT path.
"""

import re

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fakes import MemoryInstallationResolver, RecordingRunner

from gojira.github import GITHUB_API, GitHubAppClient, GitHubError
from gojira.shell import CommandResult

APP_ID = "123456"
NOW = 1_000_000.0
FUTURE_ISO = "2099-01-01T00:00:00Z"  # always > NOW, so minted tokens look fresh


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return pem, pub


class FakeGitHubServer:
    """Minimal stateful GitHub for the endpoints the App client touches."""

    def __init__(self):
        self.installations = [{"id": 100}]
        self.repo_installation = {"acme/web": 100}
        self.repos_by_installation = {100: [{"full_name": "acme/web", "pushed_at": "2026-01-02"}]}
        self.contents = {("acme/web", "Dockerfile"): True}
        self.pulls: list[dict] = []
        self.default_branch = "main"
        self.commit_sha = "deadbeef"
        self.created_refs: list[dict] = []
        self.created_pulls: list[dict] = []
        self.token_mints = 0
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        params = request.url.params

        if method == "POST" and path.endswith("/access_tokens"):
            self.token_mints += 1
            iid = path.split("/")[3]
            return httpx.Response(201, json={"token": f"tok-{iid}", "expires_at": FUTURE_ISO})

        if method == "GET" and path == "/app/installations":
            return httpx.Response(200, json=self.installations)

        if method == "GET" and path == "/installation/repositories":
            iid = int(request.headers["Authorization"].rsplit("-", 1)[1])
            return httpx.Response(200, json={"repositories": self.repos_by_installation.get(iid, [])})

        if method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+/installation", path):
            repo = "/".join(path.split("/")[2:4])
            if repo not in self.repo_installation:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={"id": self.repo_installation[repo]})

        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/contents/(.+)", path)
        if method == "GET" and m:
            present = self.contents.get((m.group(1), m.group(2)), False)
            return httpx.Response(200 if present else 404, json={})

        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/pulls", path)
        if method == "GET" and m:
            branch = params.get("head", ":").split(":", 1)[1]
            state = params.get("state")
            hits = [
                p for p in self.pulls
                if p["head"] == branch and (state in (None, "all") or p["state"] == state)
            ]
            return httpx.Response(200, json=hits)
        if method == "POST" and m:
            body = _json(request)
            self.created_pulls.append(body)
            return httpx.Response(201, json={"html_url": f"https://github.com/{m.group(1)}/pull/7"})

        if method == "POST" and re.fullmatch(r"/repos/[^/]+/[^/]+/git/refs", path):
            self.created_refs.append(_json(request))
            return httpx.Response(201, json={})

        m = re.fullmatch(r"/repos/[^/]+/[^/]+/commits/[^/]+", path)
        if method == "GET" and m:
            return httpx.Response(200, json={"sha": self.commit_sha})

        if method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+", path):
            return httpx.Response(200, json={"default_branch": self.default_branch})

        return httpx.Response(500, json={"message": f"unhandled {method} {path}"})


def _json(request: httpx.Request) -> dict:
    import json
    return json.loads(request.content)


def make_client(server, keypair, *, resolver=None, runner=None):
    http = httpx.Client(base_url=GITHUB_API, transport=httpx.MockTransport(server.handler))
    return GitHubAppClient(
        APP_ID, keypair[0], resolver or MemoryInstallationResolver(),
        http=http, runner=runner or RecordingRunner(), now=lambda: NOW,
    )


# --- resolution --------------------------------------------------------------


def test_installation_id_resolves_via_api_and_caches(keypair):
    server = FakeGitHubServer()
    resolver = MemoryInstallationResolver()
    client = make_client(server, keypair, resolver=resolver)

    assert client.installation_id_for("acme/web") == 100
    assert resolver.map["acme/web"] == 100  # seeded the cache

    # second call is served from the resolver — no extra /installation lookup
    before = sum(1 for r in server.requests if r.url.path.endswith("/installation"))
    client.installation_id_for("acme/web")
    after = sum(1 for r in server.requests if r.url.path.endswith("/installation"))
    assert before == after == 1


def test_unknown_repo_resolution_raises(keypair):
    server = FakeGitHubServer()
    server.repo_installation = {}
    client = make_client(server, keypair)
    with pytest.raises(GitHubError):
        client.installation_id_for("acme/web")


# --- auth --------------------------------------------------------------------


def test_app_jwt_is_signed_with_app_key(keypair):
    server = FakeGitHubServer()
    client = make_client(server, keypair)
    client.file_exists("acme/web", "Dockerfile")  # triggers a token mint

    mint = next(r for r in server.requests if r.url.path.endswith("/access_tokens"))
    token = mint.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(
        token, keypair[1], algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == APP_ID


def test_installation_token_is_cached(keypair):
    server = FakeGitHubServer()
    client = make_client(server, keypair)
    client.file_exists("acme/web", "Dockerfile")
    client.file_exists("acme/web", "Dockerfile")
    assert server.token_mints == 1  # second call reused the cached token


# --- REST surface ------------------------------------------------------------


def test_file_exists_true_and_false(keypair):
    server = FakeGitHubServer()
    client = make_client(server, keypair)
    assert client.file_exists("acme/web", "Dockerfile") is True
    assert client.file_exists("acme/web", "missing.txt") is False


def test_setup_pr_merged(keypair):
    server = FakeGitHubServer()
    client = make_client(server, keypair)
    assert client.setup_pr_merged("acme/web") is False
    server.pulls.append({"head": "gojira/setup", "state": "closed", "merged_at": "2026-01-01"})
    assert client.setup_pr_merged("acme/web") is True


def test_default_branch_and_head_sha(keypair):
    server = FakeGitHubServer()
    client = make_client(server, keypair)
    assert client.default_branch("acme/web") == "main"
    assert client.head_sha("acme/web") == "deadbeef"


def test_create_tag_posts_ref(keypair):
    server = FakeGitHubServer()
    client = make_client(server, keypair)
    client.create_tag("acme/web", "v1.2.3", "deadbeef")
    assert server.created_refs == [{"ref": "refs/tags/v1.2.3", "sha": "deadbeef"}]


def test_existing_pr_none_then_url(keypair):
    server = FakeGitHubServer()
    client = make_client(server, keypair)
    assert client.existing_pr("acme/web", "gojira/setup") is None
    server.pulls.append(
        {"head": "gojira/setup", "state": "open", "html_url": "https://github.com/acme/web/pull/3"}
    )
    assert client.existing_pr("acme/web", "gojira/setup") == "https://github.com/acme/web/pull/3"


def test_open_pr_returns_url(keypair):
    server = FakeGitHubServer()
    client = make_client(server, keypair)
    url = client.open_pr("acme/web", "gojira/setup", "main", "Setup", "body")
    assert url == "https://github.com/acme/web/pull/7"
    assert server.created_pulls[0]["head"] == "gojira/setup"


def test_list_repos_unions_installations_and_seeds_resolver(keypair):
    server = FakeGitHubServer()
    server.installations = [{"id": 100}, {"id": 200}]
    server.repos_by_installation = {
        100: [{"full_name": "acme/web", "pushed_at": "2026-01-02"}],
        200: [{"full_name": "acme/api", "pushed_at": "2026-03-01"}],
    }
    resolver = MemoryInstallationResolver()
    client = make_client(server, keypair, resolver=resolver)

    repos = client.list_repos()
    assert repos == ["acme/api", "acme/web"]  # newest push first
    assert resolver.map == {"acme/web": 100, "acme/api": 200}


# --- git ---------------------------------------------------------------------


def test_clone_uses_token_authed_remote(keypair, tmp_path):
    server = FakeGitHubServer()
    runner = RecordingRunner()
    client = make_client(server, keypair, runner=runner)

    client.clone("acme/web", tmp_path / "wt")
    clone = next(c for c in runner.calls if "clone" in c)
    url = clone[2]
    assert url == "https://x-access-token:tok-100@github.com/acme/web.git"


def test_clone_failure_raises(keypair, tmp_path):
    server = FakeGitHubServer()
    runner = RecordingRunner(results={"clone": CommandResult(128, "", "fatal: nope")})
    client = make_client(server, keypair, runner=runner)
    with pytest.raises(GitHubError, match="clone acme/web failed"):
        client.clone("acme/web", tmp_path / "wt")
