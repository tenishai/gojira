"""Finding #12: apps arrive with secrets delivery, identity, and state.

The contract keys (secrets_from / service_account / data_mount) render inside
Gojira's deployment; the objects they reference stay app-authored and
preserve_resources keeps them in the build across regeneration.
"""

import json

import pytest
import yaml

from gojira.generator import (
    DataMount,
    EnvSpec,
    GeneratorSpec,
    preserve_resources,
    read_app_needs,
    render,
)


def spec(**over) -> GeneratorSpec:
    return GeneratorSpec(
        app="atlas",
        repo_full_name="acme/agent-atlas",
        workload_class="worker",
        environments=[EnvSpec(
            name="dev", namespace="atlas", project="p", cluster_name="acme-dev",
            location="us-east4", gke_cluster="g", ar_repo="us-east4-docker.pkg.dev/p/apps",
            wif_provider="projects/1/locations/global/workloadIdentityPools/gojira/providers/github",
            gsa_email="atlas-deploy@p.iam.gserviceaccount.com",
        )],
        **over,
    )


NEEDS = dict(
    secrets_from=["atlas-secrets"],
    service_account="atlas",
    data_mount=DataMount(pvc="atlas-data", path="/data"),
)


def test_needs_render_into_the_deployment():
    files = render(spec(**NEEDS))
    dep = yaml.safe_load(files["k8s/base/deployment.yaml"])
    pod = dep["spec"]["template"]["spec"]
    app = pod["containers"][0]
    assert pod["serviceAccountName"] == "atlas"
    assert app["envFrom"] == [{"secretRef": {"name": "atlas-secrets"}}]
    assert app["volumeMounts"] == [{"name": "data", "mountPath": "/data"}]
    assert pod["volumes"] == [
        {"name": "data", "persistentVolumeClaim": {"claimName": "atlas-data"}}
    ]
    # finding #15: a mounted PVC without fsGroup is unwritable for non-root
    # images — the sandbox's finding-#2 lesson, encoded in the vocabulary
    assert pod["securityContext"] == {"fsGroup": 999}


def test_optional_secret_references_render_optional_and_round_trip():
    """A Secret the pod reads when it exists and boots without when it does not
    (tenishai/kobo#434): rendered with `optional: true`, read back by name, and read
    the same way from an adopted deployment."""
    from gojira.generator import infer_app_needs

    files = render(spec(**NEEDS, secrets_from_optional=["atlas-slack", "atlas-env"]))
    dep = yaml.safe_load(files["k8s/base/deployment.yaml"])
    app = dep["spec"]["template"]["spec"]["containers"][0]
    assert app["envFrom"] == [
        {"secretRef": {"name": "atlas-secrets"}},
        {"secretRef": {"name": "atlas-slack", "optional": True}},
        {"secretRef": {"name": "atlas-env", "optional": True}},
    ]
    got = read_app_needs(files["gojira.yaml"])
    assert got["secrets_from"] == ["atlas-secrets"]
    assert got["secrets_from_optional"] == ["atlas-slack", "atlas-env"]
    # Optional references alone still render an envFrom block.
    only = render(spec(secrets_from_optional=["atlas-slack"]))
    assert yaml.safe_load(only["k8s/base/deployment.yaml"])["spec"]["template"]["spec"]["containers"][0]["envFrom"] == [
        {"secretRef": {"name": "atlas-slack", "optional": True}}
    ]
    inferred = infer_app_needs(files["k8s/base/deployment.yaml"])
    assert inferred["secrets_from"] == ["atlas-secrets"]
    assert inferred["secrets_from_optional"] == ["atlas-slack", "atlas-env"]


def test_a_bundle_image_mounts_read_only_at_bundle_pulled_by_the_node_and_round_trips():
    """tenishai/kobo#434: the agent's bundle travels as an OCI image the node pulls on every
    start (pullPolicy Always) and mounts read-only at /bundle, so the pod holds no registry
    credential and a bundle change is a restart. Rendered, read back, and inferred on adoption."""
    from gojira.generator import infer_app_needs

    ref = "us-east4-docker.pkg.dev/p/agents/atlas-bundle:current"
    files = render(spec(bundle_image=ref))
    pod = yaml.safe_load(files["k8s/base/deployment.yaml"])["spec"]["template"]["spec"]
    assert pod["volumes"] == [{"name": "bundle", "image": {"reference": ref, "pullPolicy": "Always"}}]
    assert pod["containers"][0]["volumeMounts"] == [{"name": "bundle", "mountPath": "/bundle", "readOnly": True}]
    assert read_app_needs(files["gojira.yaml"])["bundle_image"] == ref
    assert infer_app_needs(files["k8s/base/deployment.yaml"])["bundle_image"] == ref
    assert "bundle_image" not in read_app_needs(render(spec())["gojira.yaml"])


def test_needs_absent_renders_no_stubs():
    dep = yaml.safe_load(render(spec())["k8s/base/deployment.yaml"])
    pod = dep["spec"]["template"]["spec"]
    assert "serviceAccountName" not in pod and "volumes" not in pod
    assert "envFrom" not in pod["containers"][0]


def test_contract_round_trips():
    """read_app_needs(render(spec)) == the needs the spec carried — the same
    invariant overrides hold (finding #10's lesson, applied to needs)."""
    rendered = render(spec(**NEEDS))["gojira.yaml"]
    got = read_app_needs(rendered)
    assert got["secrets_from"] == ["atlas-secrets"]
    assert got["service_account"] == "atlas"
    assert got["data_mount"] == DataMount(pvc="atlas-data", path="/data", fs_group=999)
    # explicit fs_group round-trips too
    assert read_app_needs("data_mount: { pvc: d, path: /x, fs_group: 10001 }")[
        "data_mount"].fs_group == 10001
    assert read_app_needs(render(spec())["gojira.yaml"]) == {}


def test_read_app_needs_ignores_junk():
    assert read_app_needs("secrets_from: notalist\nservice_account: ''") == {}
    assert read_app_needs("data_mount: { pvc: x }") == {}  # path missing
    assert read_app_needs(":::not yaml") == {}


# --- preserve_resources ---------------------------------------------------------

RENDERED = """\
# Generated by Gojira.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - deployment.yaml
  - podmonitoring.yaml
  - rules.yaml
"""

EXISTING = """\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - serviceaccount.yaml
  - deployment.yaml
  - pvc.yaml
  - externalsecret.yaml
"""


def test_preserve_resources_keeps_app_authored_entries():
    out = preserve_resources(EXISTING, RENDERED, file_exists=lambda e: True)
    got = yaml.safe_load(out)["resources"]
    # rendered set intact and first; app-authored appended, no duplicates
    assert got[:3] == ["deployment.yaml", "podmonitoring.yaml", "rules.yaml"]
    assert set(got[3:]) == {"namespace.yaml", "serviceaccount.yaml", "pvc.yaml",
                            "externalsecret.yaml"}
    assert "# Generated by Gojira." in out  # textual append — comments survive


def test_preserve_resources_drops_missing_files():
    out = preserve_resources(
        EXISTING, RENDERED,
        file_exists=lambda e: e != "externalsecret.yaml",
    )
    got = yaml.safe_load(out)["resources"]
    assert "externalsecret.yaml" not in got and "pvc.yaml" in got


def test_preserve_resources_noops_without_extras():
    assert preserve_resources(RENDERED, RENDERED, file_exists=lambda e: True) == RENDERED
    assert preserve_resources(":::bad", RENDERED, file_exists=lambda e: True) == RENDERED


# --- the full agent shape through provisioning -----------------------------------


def test_iris_shaped_repo_survives_onboarding(tmp_path):
    """Integration: a repo with app-authored resources, a needs-carrying
    gojira.yaml, and an overlay patch targeting a preserved resource comes out
    of GenerateArtifacts deployable — nothing orphaned, nothing dropped."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from gojira.config import Config
    from gojira.models import App, Base, Cluster, Environment
    from gojira.provisioner import provision_app
    from tests.fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory

    class IrisShapedGitHub(FakeGitHubClient):
        def clone(self, repo, workdir):
            super().clone(repo, workdir)
            base = workdir / "k8s" / "base"
            base.mkdir(parents=True)
            (base / "externalsecret.yaml").write_text(
                "apiVersion: external-secrets.io/v1beta1\nkind: ExternalSecret\n"
                "metadata: { name: atlas-secrets }\n")
            (base / "serviceaccount.yaml").write_text(
                "apiVersion: v1\nkind: ServiceAccount\nmetadata: { name: atlas }\n")
            (base / "pvc.yaml").write_text(
                "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata: { name: atlas-data }\n")
            (base / "kustomization.yaml").write_text(
                "resources:\n  - deployment.yaml\n  - serviceaccount.yaml\n"
                "  - pvc.yaml\n  - externalsecret.yaml\n")
            overlay = workdir / "k8s" / "overlays" / "dev"
            (overlay / "patches").mkdir(parents=True)
            (overlay / "patches" / "sa-wif.yaml").write_text(
                "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: atlas\n"
                "  annotations: { iam.gke.io/gcp-service-account: atlas@p.iam.gserviceaccount.com }\n")
            (overlay / "kustomization.yaml").write_text(
                "resources:\n  - ../../base\npatches:\n  - path: patches/sa-wif.yaml\n")
            (workdir / "gojira.yaml").write_text(
                "app: atlas\nclass: worker\n"
                "service_account: atlas\nsecrets_from: [atlas-secrets]\n"
                "data_mount: { pvc: atlas-data, path: /data }\n")

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        c = Cluster(name="acme-dev", gcp_project="p", location="us-east4",
                    gke_cluster="g", ar_repo="us-east4-docker.pkg.dev/p/apps")
        session.add(c)
        session.commit()
        a = App(name="atlas", repo_full_name="acme/agent-atlas",
                installation_id=1, workload_class="worker")
        session.add(a)
        session.commit()
        session.add(Environment(app_id=a.id, name="dev", cluster_id=c.id, namespace="atlas"))
        session.commit()

        result = provision_app(
            session, a, gcp=FakeGCPClient(), github=IrisShapedGitHub(),
            kubectl_for=KubectlFactory(), config=Config(), workdir=tmp_path,
        )
        assert result.completed

    base_kust = yaml.safe_load((tmp_path / "k8s/base/kustomization.yaml").read_text())
    assert {"externalsecret.yaml", "serviceaccount.yaml", "pvc.yaml"} <= set(
        base_kust["resources"])

    dep = yaml.safe_load((tmp_path / "k8s/base/deployment.yaml").read_text())
    pod = dep["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "atlas"
    assert pod["containers"][0]["envFrom"] == [{"secretRef": {"name": "atlas-secrets"}}]
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "atlas-data"

    # the WIF patch targets the preserved ServiceAccount — not orphaned
    overlay = yaml.safe_load((tmp_path / "k8s/overlays/dev/kustomization.yaml").read_text())
    assert {"path": "patches/sa-wif.yaml"} in overlay.get("patches", [])

    # and the contract round-trips through the regenerated gojira.yaml
    regenerated = (tmp_path / "gojira.yaml").read_text()
    assert read_app_needs(regenerated)["secrets_from"] == ["atlas-secrets"]


# --- tenant feedback round 1 ---------------------------------------------


def test_env_vars_render_inline_and_round_trip():
    files = render(spec(env_vars={"AGENT_BUNDLE": "scout", "GCP_PROJECT_ID": "p"}))
    dep = yaml.safe_load(files["k8s/base/deployment.yaml"])
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "AGENT_BUNDLE", "value": "scout"} in env
    assert read_app_needs(files["gojira.yaml"])["env_vars"] == {
        "AGENT_BUNDLE": "scout", "GCP_PROJECT_ID": "p"}


def test_agent_class_recreate_no_surge():
    files = render(spec_class("agent"))
    dep = yaml.safe_load(files["k8s/base/deployment.yaml"])
    assert dep["spec"]["strategy"] == {"type": "Recreate"}
    assert dep["spec"]["replicas"] == 1
    # worker keeps the k8s default — no strategy block
    worker = yaml.safe_load(render(spec_class("worker"))["k8s/base/deployment.yaml"])
    assert "strategy" not in worker["spec"]


def spec_class(cls_name):
    s = spec()
    return s.model_copy(update={"workload_class": cls_name})


def test_class_change_in_contract_wins_over_db(tmp_path):
    """Finding #16: 'change the class here' must be true — a class merged to
    main regenerates the manifests and converges the DB, instead of the DB
    silently reverting the contract."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from gojira.config import Config
    from gojira.models import App, Base, Cluster, Environment
    from gojira.provisioner import provision_app
    from tests.fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory

    class AgentContractGitHub(FakeGitHubClient):
        def clone(self, repo, workdir):
            super().clone(repo, workdir)
            (workdir / "gojira.yaml").write_text("app: atlas\nclass: agent\n")

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        c = Cluster(name="acme-dev", gcp_project="p", location="us-east4",
                    gke_cluster="g", ar_repo="us-east4-docker.pkg.dev/p/apps")
        session.add(c)
        session.commit()
        a = App(name="atlas", repo_full_name="acme/agent-atlas",
                installation_id=1, workload_class="worker")  # DB says worker
        session.add(a)
        session.commit()
        session.add(Environment(app_id=a.id, name="dev", cluster_id=c.id, namespace="atlas"))
        session.commit()
        result = provision_app(
            session, a, gcp=FakeGCPClient(), github=AgentContractGitHub(),
            kubectl_for=KubectlFactory(), config=Config(), workdir=tmp_path,
        )
        assert result.completed
        assert a.workload_class == "agent"          # DB converged

    dep = yaml.safe_load((tmp_path / "k8s/base/deployment.yaml").read_text())
    assert dep["spec"]["strategy"] == {"type": "Recreate"}   # agent manifests
    assert "class: agent" in (tmp_path / "gojira.yaml").read_text()  # not reverted


def test_unknown_class_in_contract_falls_back_to_db():
    from gojira.generator import read_app_class
    assert read_app_class("class: warlock") == "warlock"  # reader is dumb...
    # ...the guard lives in GenerateArtifacts (WORKLOAD_CLASSES check) — the
    # spec keeps the DB class and provision proceeds instead of dying.
    assert read_app_class("app: x") is None


# --- adopt-mode inference (zero-touch onboarding) -------------------------------

PROBE_SHAPED = """\
apiVersion: apps/v1
kind: Deployment
metadata: { name: probe, namespace: probe }
spec:
  template:
    spec:
      serviceAccountName: probe
      securityContext: { fsGroup: 999 }
      containers:
        - name: probe
          image: gcr.io/x/probe:latest
          envFrom:
            - secretRef: { name: probe-secrets }
          env:
            - name: LOG_LEVEL
              value: DEBUG
            - name: FROM_FIELD
              valueFrom: { fieldRef: { fieldPath: metadata.name } }
          volumeMounts:
            - { name: data, mountPath: /data }
      volumes:
        - name: data
          persistentVolumeClaim: { claimName: probe-data }
"""


def test_infer_reads_the_apps_declared_needs():
    from gojira.generator import infer_app_needs
    needs = infer_app_needs(PROBE_SHAPED)
    assert needs["service_account"] == "probe"
    assert needs["secrets_from"] == ["probe-secrets"]
    assert needs["env_vars"] == {"LOG_LEVEL": "DEBUG"}   # valueFrom skipped
    assert needs["data_mount"] == DataMount(pvc="probe-data", path="/data",
                                            fs_group=999)


def test_infer_never_makes_things_worse():
    from gojira.generator import infer_app_needs
    assert infer_app_needs(":::bad") == {}
    assert infer_app_needs("kind: Service") == {}
    assert infer_app_needs("kind: Deployment\nspec: {}") == {}


def test_zero_touch_onboarding_seeds_the_contract(tmp_path):
    """The acceptance test for 'press the button, touch nothing': a repo with
    hand-built manifests and NO gojira.yaml comes out of provisioning with a
    contract carrying its inferred needs and a deployment that honors them."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from gojira.config import Config
    from gojira.models import App, Base, Cluster, Environment
    from gojira.provisioner import provision_app
    from tests.fakes import FakeGCPClient, FakeGitHubClient, KubectlFactory

    class UntouchedRepo(FakeGitHubClient):
        def clone(self, repo, workdir):
            super().clone(repo, workdir)
            base = workdir / "k8s" / "base"
            base.mkdir(parents=True)
            (base / "deployment.yaml").write_text(PROBE_SHAPED)
            (base / "kustomization.yaml").write_text(
                "resources:\n  - deployment.yaml\n  - externalsecret.yaml\n")
            (base / "externalsecret.yaml").write_text(
                "apiVersion: external-secrets.io/v1\nkind: ExternalSecret\n"
                "metadata: { name: probe-secrets }\n")
            # deliberately NO gojira.yaml — nobody touched this repo

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        c = Cluster(name="acme-dev", gcp_project="p", location="us-east4",
                    gke_cluster="g", ar_repo="us-east4-docker.pkg.dev/p/apps")
        session.add(c)
        session.commit()
        a = App(name="watson", repo_full_name="acme/watson",
                installation_id=1, workload_class="agent")
        session.add(a)
        session.commit()
        session.add(Environment(app_id=a.id, name="dev", cluster_id=c.id,
                                namespace="watson"))
        session.commit()
        result = provision_app(
            session, a, gcp=FakeGCPClient(), github=UntouchedRepo(),
            kubectl_for=KubectlFactory(), config=Config(), workdir=tmp_path,
        )
        assert result.completed

    contract = (tmp_path / "gojira.yaml").read_text()
    got = read_app_needs(contract)
    assert got["secrets_from"] == ["probe-secrets"]
    assert got["service_account"] == "probe"
    assert got["data_mount"].fs_group == 999

    dep = yaml.safe_load((tmp_path / "k8s/base/deployment.yaml").read_text())
    pod = dep["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "probe"
    assert pod["securityContext"] == {"fsGroup": 999}
    assert {"externalsecret.yaml"} <= set(yaml.safe_load(
        (tmp_path / "k8s/base/kustomization.yaml").read_text())["resources"])


def _onboard(tmp_path, github, *, auto_merge, seed=None, handed_over=False):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from gojira.config import Config
    from gojira.models import App, Base, Cluster, Environment
    from gojira.provisioner import provision_app
    from tests.fakes import FakeGCPClient, KubectlFactory

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        c = Cluster(name="acme-dev", gcp_project="p", location="us-east4",
                    gke_cluster="g", ar_repo="us-east4-docker.pkg.dev/p/apps")
        session.add(c)
        session.commit()
        a = App(name="watson", repo_full_name="acme/watson", installation_id=1,
                workload_class="agent", auto_merge_setup_pr=auto_merge, handed_over=handed_over,
                setup_seed_json=json.dumps(seed) if seed else None)
        session.add(a)
        session.commit()
        session.add(Environment(app_id=a.id, name="dev", cluster_id=c.id, namespace="watson"))
        session.commit()
        result = provision_app(session, a, gcp=FakeGCPClient(), github=github,
                               kubectl_for=KubectlFactory(), config=Config(), workdir=tmp_path)
        return result, a


def test_adopted_repo_refuses_auto_merge(tmp_path):
    """Finding #22 (live, 2026-09-25): a deployer UI onboarded a PRODUCTION
    agent's existing repo with auto_merge_setup_pr=True — the flag it sets for
    repos it authored. Gojira would have regenerated a human's k8s/ (renaming
    their Deployment out from under the prod overlay) and merged it unreviewed.
    Auto-merge is a waiver for repos with no human content; adopt mode is the
    proof there IS human content, so the merge step refuses and the PR waits."""
    from gojira.models import AppStatus
    from tests.fakes import FakeGitHubClient

    class HandBuilt(FakeGitHubClient):
        def clone(self, repo, workdir):
            super().clone(repo, workdir)
            base = workdir / "k8s" / "base"
            base.mkdir(parents=True)
            (base / "deployment.yaml").write_text(PROBE_SHAPED)
            (base / "kustomization.yaml").write_text("resources:\n  - deployment.yaml\n")

    gh = HandBuilt()
    result, app = _onboard(tmp_path, gh, auto_merge=True)
    assert result.completed
    assert gh.merged == []                       # nothing merged
    assert app.setup_pr_url                      # the PR is open, for a person
    assert app.status == AppStatus.AWAITING_PR
    assert any(s.startswith("merge_setup_pr") for s in result.steps_skipped)


def test_handed_over_repo_is_merged_and_a_refused_merge_waits_for_a_person(tmp_path):
    """The one thing that lifts finding #22: the caller says the repo's owner
    handed its deploy files to the platform. Then an adopted repo's setup PR
    is merged without a person, and its body says whose word that was. The
    repo is still its owner's on GitHub, so a merge a branch rule refuses is
    a person's merge and not a failed provision: the PR stays open and the
    app waits in awaiting_pr."""
    from gojira.github import GitHubError
    from gojira.models import AppStatus
    from tests.fakes import FakeGitHubClient

    class HandBuilt(FakeGitHubClient):
        def clone(self, repo, workdir):
            super().clone(repo, workdir)
            base = workdir / "k8s" / "base"
            base.mkdir(parents=True)
            (base / "deployment.yaml").write_text(PROBE_SHAPED)
            (base / "kustomization.yaml").write_text("resources:\n  - deployment.yaml\n")

        def open_pr(self, repo, head, base, title, body):
            self.body = body
            return super().open_pr(repo, head, base, title, body)

    gh = HandBuilt()
    result, app = _onboard(tmp_path / "merged", gh, auto_merge=True, handed_over=True)
    assert result.completed
    assert gh.merged and app.status == AppStatus.ACTIVE and app.setup_pr_url is None
    assert "Gojira merges this itself" in gh.body

    class Protected(HandBuilt):
        def merge_pr(self, repo, pr_url):
            raise GitHubError(f"merge PR {pr_url} failed: 405 review required")

    gh = Protected()
    result, app = _onboard(tmp_path / "refused", gh, auto_merge=True, handed_over=True)
    assert result.completed                      # not a failed provision
    assert app.setup_pr_url                      # the PR is open, for a person
    assert app.status == AppStatus.AWAITING_PR


def test_authored_repo_still_auto_merges(tmp_path):
    """The control: a repo the caller authored (no k8s/, no contract) keeps
    the one-click contract — the waiver is theirs to give."""
    from gojira.models import AppStatus
    from tests.fakes import FakeGitHubClient

    gh = FakeGitHubClient()
    result, app = _onboard(tmp_path, gh, auto_merge=True)
    assert result.completed
    assert gh.merged                             # merged
    assert app.status == AppStatus.ACTIVE


# --- finding #18: config is a need too ------------------------------------------

PROBE_WITH_CONFIG = PROBE_SHAPED.replace(
    """          envFrom:
            - secretRef: { name: probe-secrets }""",
    """          envFrom:
            - secretRef: { name: probe-secrets }
            - configMapRef: { name: probe-env }""")


def test_infer_reads_configmap_env_sources():
    from gojira.generator import infer_app_needs
    needs = infer_app_needs(PROBE_WITH_CONFIG)
    assert needs["config_from"] == ["probe-env"]
    assert needs["secrets_from"] == ["probe-secrets"]


def test_config_from_renders_and_round_trips():
    files = render(spec(config_from=["probe-env"], secrets_from=["s"]))
    dep = yaml.safe_load(files["k8s/base/deployment.yaml"])
    env_from = dep["spec"]["template"]["spec"]["containers"][0]["envFrom"]
    assert {"configMapRef": {"name": "probe-env"}} in env_from
    assert {"secretRef": {"name": "s"}} in env_from
    assert read_app_needs(files["gojira.yaml"])["config_from"] == ["probe-env"]


def test_preserve_generators_keeps_configmap_generator():
    from gojira.generator import preserve_generators
    existing = """\
namespace: probe
resources:
  - ../../base
generatorOptions:
  disableNameSuffixHash: true
configMapGenerator:
  - name: probe-env
    literals:
      - LOG_LEVEL=INFO
      - REGION=us-east4
"""
    rendered = "# Generated by Gojira.\nresources:\n  - ../../base\n"
    out = preserve_generators(existing, rendered)
    parsed = yaml.safe_load(out)
    assert parsed["configMapGenerator"][0]["name"] == "probe-env"
    assert parsed["generatorOptions"] == {"disableNameSuffixHash": True}
    assert "# Generated by Gojira." in out
    # no generators → untouched; junk → untouched
    assert preserve_generators("resources: [x.yaml]", rendered) == rendered
    assert preserve_generators(":::bad", rendered) == rendered


# --- gateway class + admin_port (liminal-shaped apps, 2026-09-07) -------------


def _gw(**over) -> GeneratorSpec:
    """The spec() fixture with the gateway class -- model_copy because the
    helper hardcodes workload_class and pydantic refuses the duplicate."""
    return spec().model_copy(update={"workload_class": "gateway", **over})


def test_admin_port_renders_second_port_and_its_own_service():
    files = render(_gw(admin_port=9101))
    dep = yaml.safe_load(files["k8s/base/deployment.yaml"])
    ports = dep["spec"]["template"]["spec"]["containers"][0]["ports"]
    assert {"name": "admin", "containerPort": 9101} in ports
    assert {"name": "http", "containerPort": 8080} in ports

    admin = yaml.safe_load(files["k8s/base/admin-service.yaml"])
    assert admin["metadata"]["name"] == "atlas-admin"
    assert admin["spec"]["ports"] == [
        {"name": "admin", "port": 80, "targetPort": "admin"}
    ]
    # The data-plane Service is untouched: one http port, nothing admin.
    svc = yaml.safe_load(files["k8s/base/service.yaml"])
    assert [p["name"] for p in svc["spec"]["ports"]] == ["http"]
    # kustomization lists the new manifest.
    kust = yaml.safe_load(files["k8s/base/kustomization.yaml"])
    assert "admin-service.yaml" in kust["resources"]


def test_admin_port_round_trips_through_the_contract():
    files = render(_gw(admin_port=9101))
    assert read_app_needs(files["gojira.yaml"]).get("admin_port") == 9101


def test_gateway_class_opinions_single_replica_healthz():
    files = render(_gw())
    dep = yaml.safe_load(files["k8s/base/deployment.yaml"])
    assert dep["spec"]["strategy"] == {"type": "Recreate"}
    probe = dep["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    assert probe["httpGet"]["path"] == "/healthz"
    assert "k8s/base/hpa.yaml" not in files
    assert "k8s/base/service.yaml" in files  # http readiness => a Service


def test_no_admin_port_renders_no_admin_artifacts():
    files = render(_gw())
    assert "k8s/base/admin-service.yaml" not in files
    dep = yaml.safe_load(files["k8s/base/deployment.yaml"])
    ports = dep["spec"]["template"]["spec"]["containers"][0]["ports"]
    assert [p["name"] for p in ports] == ["http"]


def test_admin_port_ignores_nonsense_values():
    assert "admin_port" not in read_app_needs("admin_port: 99999")
    assert "admin_port" not in read_app_needs('admin_port: "9101"')
    # bool is an int: `true` must not render containerPort: True
    assert "admin_port" not in read_app_needs("admin_port: true")
    # equal to the app port: a duplicate containerPort whose admin Service
    # would route to the data-plane listener
    assert "admin_port" not in read_app_needs("admin_port: 8080")


# --- port + health: the app's listener is its own (2026-09-28) --------------------

def test_port_and_health_render_and_round_trip():
    """A Go proxy on 9000 answering /alive is hosted as it is: containerPort,
    both http probes and the contract carry the app's values; the class keeps
    its probe shape. read_app_needs(render(spec)) gives the values back."""
    files = render(spec().model_copy(update={"workload_class": "web", "port": 9000, "health": "/alive"}))
    dep = yaml.safe_load(files["k8s/base/deployment.yaml"])
    c0 = dep["spec"]["template"]["spec"]["containers"][0]
    assert c0["ports"][0] == {"name": "http", "containerPort": 9000}
    assert c0["readinessProbe"]["httpGet"]["path"] == "/alive"
    assert c0["livenessProbe"]["httpGet"]["path"] == "/alive"
    assert "port: 9000" in files["gojira.yaml"] and "health: /alive" in files["gojira.yaml"]
    got = read_app_needs(files["gojira.yaml"])
    assert got["port"] == 9000 and got["health"] == "/alive"


def test_defaults_stay_the_class_opinion():
    files = render(spec().model_copy(update={"workload_class": "web"}))
    assert "port:" not in files["gojira.yaml"] and "health:" not in files["gojira.yaml"]
    dep = yaml.safe_load(files["k8s/base/deployment.yaml"])
    c0 = dep["spec"]["template"]["spec"]["containers"][0]
    assert c0["ports"][0]["containerPort"] == 8080
    assert c0["readinessProbe"]["httpGet"]["path"] == "/health"


def test_port_and_health_reject_junk():
    """`port: true`, an out-of-range port, a relative health path: ignored,
    never rendered. admin_port equal to the app's real port is ignored too."""
    assert "port" not in read_app_needs("port: true\n")
    assert "port" not in read_app_needs("port: 70000\n")
    assert "health" not in read_app_needs("health: alive\n")
    assert read_app_needs("port: 9000\nadmin_port: 9000\n") == {"port": 9000}
    import pytest

    with pytest.raises(ValueError):
        spec(port=9000, admin_port=9000)


def test_infer_reads_port_and_health_from_the_running_deployment():
    """Adopt mode: a foreign app on port 9000 with /alive probes comes in with
    its own contract — no SDK or runtime change to be hosted."""
    from gojira.generator import infer_app_needs

    text = """\
apiVersion: apps/v1
kind: Deployment
metadata: { name: segment-proxy }
spec:
  template:
    spec:
      containers:
        - name: proxy
          image: x
          ports:
            - containerPort: 9000
          readinessProbe:
            httpGet: { path: /alive, port: 9000 }
          livenessProbe:
            httpGet: { path: /alive, port: 9000 }
"""
    got = infer_app_needs(text)
    assert got["port"] == 9000 and got["health"] == "/alive"
    # an app on the default port with no http probes infers neither
    assert not {"port", "health"} & set(infer_app_needs(text.replace("9000", "8080").replace("readinessProbe", "x").replace("livenessProbe", "y")))


# --- files and build args: the needs the tenish stack's own apps arrived with ---
# (2026-09-30: cabal takes identity.toml as a file and its image installs
# private packages from an index named at build time; kobo reads a GitHub
# App key from a file. None of that was sayable in the contract.)


def _first_container(files):
    dep = yaml.safe_load(files["k8s/base/deployment.yaml"])
    pod = dep["spec"]["template"]["spec"]
    return pod, pod["containers"][0]


def test_config_and_secret_mounts_render_as_files():
    from gojira.generator import ConfigMount, SecretMount

    files = render(spec(
        config_mounts=[ConfigMount(name="cabal-identity", path="/app/identity.toml", key="identity.toml")],
        secret_mounts=[SecretMount(name="kobo-github", path="/secrets/github")],
    ))
    pod, c0 = _first_container(files)
    mounts = {m["mountPath"]: m for m in c0["volumeMounts"]}
    assert mounts["/app/identity.toml"]["subPath"] == "identity.toml"  # one entry, as that file
    assert "subPath" not in mounts["/secrets/github"]                  # the whole object, as a dir
    assert all(m["readOnly"] for m in mounts.values())
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes[mounts["/app/identity.toml"]["name"]]["configMap"] == {"name": "cabal-identity"}
    assert volumes[mounts["/secrets/github"]["name"]]["secret"] == {"secretName": "kobo-github"}


def test_an_optional_secret_mount_is_an_empty_dir_until_the_secret_exists():
    """A vendor key a console writes after the app is up (bab, 2026-11-09):
    the pod starts without it and reads the directory live once it lands."""
    from gojira.generator import SecretMount

    files = render(spec(secret_mounts=[SecretMount(name="bab-github", path="/etc/bab/github", optional=True),
                                       SecretMount(name="bab-secrets", path="/etc/bab/core")]))
    pod, c0 = _first_container(files)
    by_mount = {m["mountPath"]: m["name"] for m in c0["volumeMounts"]}
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes[by_mount["/etc/bab/github"]]["secret"] == {"secretName": "bab-github", "optional": True}
    assert volumes[by_mount["/etc/bab/core"]]["secret"] == {"secretName": "bab-secrets"}   # required: no key
    needs = read_app_needs(files["gojira.yaml"])
    assert [m.optional for m in needs["secret_mounts"]] == [True, False]
    assert read_app_needs("secret_mounts:\n  - {name: x, path: /x}\n")["secret_mounts"][0].optional is False


def test_a_configmap_and_a_secret_of_the_same_name_get_distinct_volumes():
    from gojira.generator import ConfigMount, SecretMount

    files = render(spec(
        config_mounts=[ConfigMount(name="cfg", path="/a")],
        secret_mounts=[SecretMount(name="cfg", path="/b")],
    ))
    pod, _ = _first_container(files)
    names = [v["name"] for v in pod["volumes"]]
    assert len(names) == len(set(names)) == 2


def test_mounts_and_build_args_round_trip_through_the_contract():
    from gojira.generator import ConfigMount, SecretMount

    s = spec(
        config_mounts=[ConfigMount(name="cabal-identity", path="/app/identity.toml", key="identity.toml")],
        secret_mounts=[SecretMount(name="kobo-github", path="/secrets/github")],
        build_args={"EXTRA_INDEX_URL": "https://us-central1-python.pkg.dev/p/tenish/simple/"},
    )
    needs = read_app_needs(render(s)["gojira.yaml"])
    assert needs["config_mounts"] == s.config_mounts
    assert needs["secret_mounts"] == s.secret_mounts
    assert needs["build_args"] == s.build_args


def test_build_args_reach_docker_build_as_single_shell_words():
    files = render(spec(build_args={"MODEL": "claude sonnet $LATEST", "EXTRA_INDEX_URL": "https://x/simple/"}))
    ci = files[".github/workflows/gojira-ci.yaml"]
    build = next(line for line in ci.splitlines() if "docker build" in line)
    assert "--secret id=ar_token,env=AR_TOKEN" in build  # the platform's own, as a BuildKit secret
    assert "--build-arg AR_TOKEN" not in ci  # a build arg would print in the build log
    assert "--build-arg EXTRA_INDEX_URL=https://x/simple/" in ci
    assert "--build-arg 'MODEL=claude sonnet $LATEST'" in ci  # quoted: no word split, no expansion


def test_no_build_args_leaves_the_build_line_as_it_was():
    ci = render(spec())[".github/workflows/gojira-ci.yaml"]
    build = next(line for line in ci.splitlines() if "docker build" in line)
    assert build.strip().endswith('-t "$IMAGE:$TAG" .')


PRUDENCE_SHAPED = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: pr-reviewer}
spec:
  template:
    spec:
      containers:
        - name: app
          image: pr-reviewer:latest
          volumeMounts:
            - {name: config, mountPath: /app/config.yaml, subPath: config.yaml}
            - {name: github-key, mountPath: /secrets/github, readOnly: true}
            - {name: cache, mountPath: /data}
      volumes:
        - name: config
          configMap: {name: pr-reviewer-config}
        - name: github-key
          secret: {secretName: github-private-key}
        - name: cache
          persistentVolumeClaim: {claimName: pr-reviewer-cache}
"""


def test_adopt_infers_file_mounts_from_the_deployment_the_app_runs():
    """A file the app already reads is a need; dropping it on adopt is how a
    reviewer loses her config (the #35 shape)."""
    from gojira.generator import infer_app_needs

    needs = infer_app_needs(PRUDENCE_SHAPED)
    assert [(m.name, m.path, m.key) for m in needs["config_mounts"]] == [
        ("pr-reviewer-config", "/app/config.yaml", "config.yaml")
    ]
    assert [(m.name, m.path, m.key) for m in needs["secret_mounts"]] == [
        ("github-private-key", "/secrets/github", "")
    ]
    assert needs["data_mount"].pvc == "pr-reviewer-cache"  # untouched by the new inference
    rendered = render(spec(**needs))
    assert read_app_needs(rendered["gojira.yaml"])["config_mounts"] == needs["config_mounts"]


# --- arch: the image's platform and the node it lands on (2026-09-30) ----------
# The dogfood builds on an arm64 machine that cannot emulate amd64, and
# Autopilot hands out arm64 nodes only to pods that also name its compute
# class. The contract says the arch; how a cluster provides it is the
# cluster's, said through node_selector.


def test_arm64_renders_platform_selector_and_toleration():
    files = render(spec(arch="arm64", node_selector={"cloud.google.com/compute-class": "Scale-Out"}))
    pod, _ = _first_container(files)
    assert pod["nodeSelector"] == {
        "kubernetes.io/arch": "arm64", "cloud.google.com/compute-class": "Scale-Out",
    }
    assert pod["tolerations"] == [
        {"key": "kubernetes.io/arch", "operator": "Equal", "value": "arm64", "effect": "NoSchedule"}
    ]
    assert "docker build --platform linux/arm64 " in files[".github/workflows/gojira-ci.yaml"]
    contract = files["gojira.yaml"]
    assert "arch: arm64" in contract and "cloud.google.com/compute-class" in contract
    needs = read_app_needs(contract)
    assert needs["arch"] == "arm64" and needs["node_selector"] == {"cloud.google.com/compute-class": "Scale-Out"}


def test_amd64_is_the_quiet_default():
    files = render(spec())
    pod, _ = _first_container(files)
    assert "nodeSelector" not in pod and "tolerations" not in pod
    assert "arch:" not in files["gojira.yaml"]
    assert "docker build --platform linux/amd64 " in files[".github/workflows/gojira-ci.yaml"]
    assert read_app_needs("arch: sparc\n") == {}  # not an arch we build: ignored, not rendered


def test_adopt_infers_arch_and_selector_from_the_running_deployment():
    from gojira.generator import infer_app_needs

    needs = infer_app_needs("""
apiVersion: apps/v1
kind: Deployment
metadata: {name: fast}
spec:
  template:
    spec:
      nodeSelector: {kubernetes.io/arch: arm64, cloud.google.com/gke-nodepool: axion}
      containers: [{name: app, image: fast:latest}]
""")
    assert needs["arch"] == "arm64"
    assert needs["node_selector"] == {"cloud.google.com/gke-nodepool": "axion"}  # arch is said once


def test_every_generated_workflow_takes_the_runner_from_a_variable():
    files = render(spec_class("web"))  # web has both workflows: ci and promote
    for path, text in files.items():
        if path.startswith(".github/workflows/"):
            assert "runs-on: ${{ vars.CI_RUNNER || 'ubuntu-latest' }}" in text, path


# --- the platform's arch: applies when the contract is silent (2026-10-02) ------
# The first agent kobo created said nothing about arch; the runner built arm64
# and the pod landed on an amd64 node ("exec format error"). Both facts are
# the platform's, so the operator states them once (defaults.arch,
# defaults.node_selector) and no repo has to know.


def test_platform_arch_applies_when_the_contract_is_silent():
    files = render(spec(platform_arch="arm64",
                        platform_node_selector={"cloud.google.com/compute-class": "Scale-Out"}))
    pod, _ = _first_container(files)
    assert pod["nodeSelector"] == {
        "kubernetes.io/arch": "arm64", "cloud.google.com/compute-class": "Scale-Out",
    }
    assert pod["tolerations"][0]["key"] == "kubernetes.io/arch"
    assert "docker build --platform linux/arm64 " in files[".github/workflows/gojira-ci.yaml"]
    # A platform fact never gets written into the repo's contract.
    assert "arch:" not in files["gojira.yaml"] and "node_selector:" not in files["gojira.yaml"]


def test_the_contract_wins_over_the_platform_arch():
    # Said amd64 on an arm64 platform: built and scheduled amd64, and the
    # contract keeps saying so (a silent contract would have gone arm64).
    files = render(spec(arch="amd64", node_selector={},
                        platform_arch="arm64",
                        platform_node_selector={"cloud.google.com/compute-class": "Scale-Out"}))
    pod, _ = _first_container(files)
    assert "nodeSelector" not in pod and "tolerations" not in pod
    assert "docker build --platform linux/amd64 " in files[".github/workflows/gojira-ci.yaml"]
    assert "arch: amd64" in files["gojira.yaml"]
    assert read_app_needs("arch: amd64\n") == {"arch": "amd64"}
    # Said a selector: it replaces the platform's, not merges with it.
    files = render(spec(node_selector={"cloud.google.com/gke-nodepool": "axion"},
                        platform_arch="arm64",
                        platform_node_selector={"cloud.google.com/compute-class": "Scale-Out"}))
    pod, _ = _first_container(files)
    assert pod["nodeSelector"] == {"kubernetes.io/arch": "arm64", "cloud.google.com/gke-nodepool": "axion"}


def test_unknown_arch_is_refused():
    import pytest
    with pytest.raises(ValueError, match="arch"):
        spec(arch="sparc")
    with pytest.raises(ValueError, match="platform arch"):
        spec(platform_arch="sparc")



# --- identity: the pod's GCP identity (2026-10-06, design §1) -------------------


def test_identity_renders_the_ksa_and_leaves_the_annotation_to_ci():
    from gojira.generator import Identity

    files = render(spec(identity=Identity(gcp_roles=["roles/aiplatform.user"])))
    sa = yaml.safe_load(files["k8s/base/serviceaccount.yaml"])
    assert sa["kind"] == "ServiceAccount" and sa["metadata"]["name"] == "atlas"
    assert "annotations" not in sa["metadata"]          # the email names a project: CI applies it
    pod, _ = _first_container(files)
    assert pod["serviceAccountName"] == "atlas"
    assert "serviceaccount.yaml" in files["k8s/base/kustomization.yaml"]
    ci = files[".github/workflows/gojira-ci.yaml"]
    assert "RUN_SA: ${{ vars.GOJIRA_DEV_RUN_SA }}" in ci
    assert "GOJIRA_DEV_RUN_SA" in ci.split("Platform variables for dev")[1].split("run:")[0]
    assert 'annotate serviceaccount atlas \\\n            "iam.gke.io/gcp-service-account=$RUN_SA" --overwrite' in ci
    # the contract round-trips; a named service_account is honored
    assert read_app_needs(files["gojira.yaml"])["identity"].gcp_roles == ["roles/aiplatform.user"]
    named = render(spec(service_account="svc", identity=Identity(gsa="shared-run@proj-x.iam.gserviceaccount.com")))
    assert yaml.safe_load(named["k8s/base/serviceaccount.yaml"])["metadata"]["name"] == "svc"
    assert "gsa: shared-run@proj-x.iam.gserviceaccount.com" in named["gojira.yaml"]


def test_without_an_identity_nothing_changes():
    files = render(spec())
    assert "k8s/base/serviceaccount.yaml" not in files
    assert "RUN_SA" not in files[".github/workflows/gojira-ci.yaml"]
    assert "annotate serviceaccount" not in files[".github/workflows/gojira-ci.yaml"]
    assert "identity:" not in files["gojira.yaml"]


def test_identity_contract_is_validated():
    import pytest
    from gojira.generator import Identity

    with pytest.raises(ValueError, match="not a role name"):
        Identity(gcp_roles=["owner"])
    with pytest.raises(ValueError, match="not a service account email"):
        Identity(gsa="not-an-email")
    with pytest.raises(ValueError, match="name gcp_roles"):
        Identity()
    assert Identity(gsa="kobo-room@shaz-learning.iam.gserviceaccount.com").project == "shaz-learning"
    assert read_app_needs("identity: {gcp_roles: [roles/storage.objectViewer]}\n")["identity"].gcp_roles == ["roles/storage.objectViewer"]



def test_cluster_roles_round_trip_and_render_the_ksa():
    files = render(spec(cluster_roles=["kobo-room"]))
    assert read_app_needs(files["gojira.yaml"])["cluster_roles"] == ["kobo-room"]
    assert yaml.safe_load(files["k8s/base/serviceaccount.yaml"])["metadata"]["name"] == "atlas"
    pod, _ = _first_container(files)
    assert pod["serviceAccountName"] == "atlas"
    assert "RUN_SA" not in files[".github/workflows/gojira-ci.yaml"]  # no GCP identity asked for


SEED = {
    "needs": {
        "service_account": "agent-watson",
        "secrets_from": ["watson-pass"],
        "secrets_from_optional": ["watson-slack"],
        "env": {"AGENT_MODEL": "balanced"},
        "data_mount": {"pvc": "agent-watson-data", "path": "/data"},
    },
    "base_files": {
        "serviceaccount.yaml": "apiVersion: v1\nkind: ServiceAccount\nmetadata: {name: agent-watson}\n",
        "storage.yaml": (
            "# the caller's own words, kept\napiVersion: v1\nkind: PersistentVolumeClaim\n"
            "metadata: {name: agent-watson-data}\n"
            "spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 1Gi}}}\n"
        ),
    },
    "note": "The room that connected this agent gives it storage and a pass to its tools.",
}


def test_a_seed_shapes_the_one_setup_pr_of_a_repo_with_nothing_of_its_own(tmp_path):
    """A caller that connects a repo and then needs a contract key and a file
    of its own in it used to open a second pull request after the setup PR,
    and a third followed when the contract changed. With `seed`, a repo that
    has no contract and no manifests gets all of it in the one setup PR: the
    keys in gojira.yaml and rendered into the Deployment, the files in
    k8s/base as written and listed in the kustomization, and the PR saying so
    to the person who reviews it."""
    from tests.fakes import FakeGitHubClient

    class Bodies(FakeGitHubClient):
        def open_pr(self, repo, head, base, title, body):
            self.body = body
            return super().open_pr(repo, head, base, title, body)

    gh = Bodies()
    result, app = _onboard(tmp_path, gh, auto_merge=False, seed=SEED)

    assert result.completed and app.setup_pr_url
    needs = read_app_needs((tmp_path / "gojira.yaml").read_text())
    assert needs["service_account"] == "agent-watson"
    assert (needs["secrets_from"], needs["secrets_from_optional"]) == (["watson-pass"], ["watson-slack"])
    assert needs["env_vars"] == {"AGENT_MODEL": "balanced"}
    assert needs["data_mount"].pvc == "agent-watson-data"
    base = tmp_path / "k8s" / "base"
    for name, text in SEED["base_files"].items():
        assert (base / name).read_text() == text
    assert {"deployment.yaml", "serviceaccount.yaml", "storage.yaml"} <= set(
        yaml.safe_load((base / "kustomization.yaml").read_text())["resources"])
    pod = yaml.safe_load((base / "deployment.yaml").read_text())["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "agent-watson"
    assert "> " + SEED["note"] in gh.body and "`k8s/base/storage.yaml`" in gh.body and "`data_mount`" in gh.body

    # The same repo again, before anyone merged: the same tree, from the row.
    again = tmp_path / "again"
    again.mkdir()
    from gojira.provisioner import ProvisionContext
    from gojira.steps import GenerateArtifacts
    from tests.fakes import FakeGCPClient
    from gojira.config import Config

    ctx = ProvisionContext(app=app, config=Config(), clients={"gcp": FakeGCPClient()}, workdir=again,
                           environments=list(app.environments))
    GenerateArtifacts().apply(ctx)
    kept = ("gojira.yaml", "k8s/base/kustomization.yaml", "k8s/base/deployment.yaml", "k8s/base/storage.yaml")
    for rel in kept:
        assert (again / rel).read_text() == (tmp_path / rel).read_text()

    # And once it is merged: the repo's own contract and files are what is
    # read, and they render to themselves. No second pull request follows.
    before = {rel: (tmp_path / rel).read_text() for rel in kept}
    merged = ProvisionContext(app=app, config=Config(), clients={"gcp": FakeGCPClient()}, workdir=tmp_path,
                              environments=list(app.environments))
    GenerateArtifacts().apply(merged)
    assert {rel: (tmp_path / rel).read_text() for rel in kept} == before
    assert merged.seed_needs == [] and merged.seed_files == []


def test_a_seed_is_never_laid_over_what_a_person_wrote(tmp_path):
    """The seed is for a repo with nothing to read. One with hand-built
    manifests is adopted from those, one with a contract is read from that,
    one with only a kustomization of its own keeps it, and in none of them
    does a caller's seed add a key or a file."""
    from tests.fakes import FakeGitHubClient

    class HandBuilt(FakeGitHubClient):
        def clone(self, repo, workdir):
            super().clone(repo, workdir)
            base = workdir / "k8s" / "base"
            base.mkdir(parents=True)
            (base / "deployment.yaml").write_text(PROBE_SHAPED)
            (base / "kustomization.yaml").write_text("resources:\n  - deployment.yaml\n")

    class Contract(FakeGitHubClient):
        def clone(self, repo, workdir):
            super().clone(repo, workdir)
            (workdir / "gojira.yaml").write_text("name: watson\nclass: agent\n")

    class OwnBase(FakeGitHubClient):
        def clone(self, repo, workdir):
            super().clone(repo, workdir)
            base = workdir / "k8s" / "base"
            base.mkdir(parents=True)
            (base / "extra.yaml").write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: extra}\n")
            (base / "kustomization.yaml").write_text("resources:\n  - extra.yaml\n")

    for i, gh in enumerate([HandBuilt(), Contract(), OwnBase()]):
        work = tmp_path / str(i)
        work.mkdir()
        result, _ = _onboard(work, gh, auto_merge=False, seed=SEED)
        assert result.completed
        needs = read_app_needs((work / "gojira.yaml").read_text())
        assert needs.get("service_account") != "agent-watson"
        assert "watson-slack" not in needs.get("secrets_from_optional", [])
        mount = needs.get("data_mount")
        assert mount is None or mount.pvc != "agent-watson-data"
        assert not (work / "k8s" / "base" / "storage.yaml").exists()


def test_a_seed_is_read_as_sent_or_refused():
    """What a seed asks for is what the contract says, in the shape gojira.yaml
    takes; a value of another shape is refused rather than coerced (review of
    #140: `{a: 1}` became the name "{'a': 1}", `true` the string "True",
    `fs_group: -5` quietly 999)."""
    from gojira.generator import seed_needs

    read = seed_needs({"env": {"A": "1"}, "data_mount": {"pvc": "d", "path": "/d"},
                       "secret_mounts": [{"name": "pem", "path": "/secrets", "optional": True}]})
    assert read["env_vars"] == {"A": "1"}
    assert (read["data_mount"].pvc, read["data_mount"].fs_group) == ("d", 999)
    assert read["secret_mounts"][0].optional is True
    for bad in ({"env": {"A": 1}}, {"service_account": {"a": 1}}, {"secrets_from": [""]},
                {"data_mount": {"pvc": "d", "path": "/d", "fs_group": "x"}},
                {"data_mount": {"pvc": "d", "path": "relative"}},
                {"config_mounts": [{"name": "c", "path": "/c", "mode": 420}]}):
        with pytest.raises(ValueError):
            seed_needs(bad)


def test_seed_files_are_named_core_objects_that_build_once():
    """A seed file kustomize would refuse is refused at the door, since the seed
    cannot be changed afterwards: nesting past the parser's depth, metadata
    that is not a mapping, an object with no name, two files for one object,
    a kind from another API group."""
    from gojira.generator import check_seed_files

    cm = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: same}\n"
    check_seed_files({"a.yaml": cm})
    for bad in ({"a.yaml": "kind: ConfigMap\nx: " + "[" * 5000 + "]" * 5000},
                {"a.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata: x\n"},
                {"a.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata: {labels: {a: b}}\n"},
                {"a.yaml": cm, "b.yaml": cm},
                {"a.yaml": "apiVersion: acme.io/v1\nkind: ConfigMap\nmetadata: {name: x}\n"}):
        with pytest.raises(ValueError):
            check_seed_files(bad)
