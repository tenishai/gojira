"""Service layer: the seam between the web handlers and the provisioner.

Web handlers call into here; nothing in this module imports FastAPI. It builds
the real clients, creates working trees, drives provision_app in a background
thread, and exposes read models over the ledger/events for the UI to poll.
"""

import json
import logging
import re
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from gojira import drift_scan
from gojira.config import Config
from gojira.gcp import GcloudClient, GCPClient
from gojira.github import CI_WORKFLOW, GhCLI, GitHubAppClient, GitHubClient
from gojira.installations import DbInstallationResolver
from gojira.k8s import KubectlCLI, KubectlClient, kustomize_build
from gojira.models import (
    App,
    AppStatus,
    Cluster,
    DriftFinding,
    Environment,
    Event,
    ProvisionStep,
    SizingRecommendation,
    UsageProfileRow,
)
from gojira.provisioner import distinct_projects, provision_app
from gojira.steps import DEPLOY_ROLES, PROMOTE_READER_ROLE, Names, promotion_plan

log = logging.getLogger(__name__)

TERMINAL_STATUSES = {AppStatus.AWAITING_PR, AppStatus.ACTIVE, AppStatus.FAILED}


def default_ar_repo(gcp_project: str, location: str) -> str:
    """The per-project Artifact Registry repo Gojira uses by convention."""
    return f"{location}-docker.pkg.dev/{gcp_project}/apps"


def build_github_client(
    config: Config, session_factory: sessionmaker[Session] | None = None
) -> GitHubClient:
    """The App backend when a GitHub App is configured, else the gh-CLI backend.

    The App backend needs a durable resolver, so it requires a session factory;
    without one (or without App config) we fall back to GhCLI's operator session.
    """
    gh = config.github
    if gh.app_id and gh.private_key_path and session_factory is not None:
        private_key = Path(gh.private_key_path).read_text()
        log.info("using GitHub App backend (app_id=%s)", gh.app_id)
        return GitHubAppClient(gh.app_id, private_key, DbInstallationResolver(session_factory))
    log.info("using gh-CLI GitHub backend")
    return GhCLI()


@dataclass
class ProvisionClients:
    """The three clients provision_app needs. Swap fakes in for tests."""

    gcp: GCPClient
    github: GitHubClient
    kubectl_for: Callable[[Cluster], KubectlClient]

    @classmethod
    def real(
        cls,
        config: Config | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> "ProvisionClients":
        gcp = GcloudClient()

        def kubectl_for(cluster: Cluster) -> KubectlClient:
            ctx = gcp.get_gke_credentials(
                cluster.gcp_project, cluster.location, cluster.gke_cluster
            )
            return KubectlCLI(context=ctx)

        github = build_github_client(config or Config(), session_factory)
        return cls(gcp=gcp, github=github, kubectl_for=kubectl_for)


# --- writes ------------------------------------------------------------------


def register_cluster(
    session: Session,
    *,
    name: str,
    gcp_project: str,
    location: str,
    gke_cluster: str,
    ar_repo: str | None = None,
    gcp: GCPClient | None = None,
) -> Cluster:
    """Register a deploy target. If a GCP client is given, validate connectivity
    by fetching credentials first (raises if the cluster is unreachable).

    Credentials are fetched over GKE's DNS-based endpoint — the only path
    that works for private control planes off-VPC and from CI runners
    (finding #14) — so that endpoint is enabled here, at registration, not
    first at provision time: a fresh cluster with it off used to fail
    registration with a bare gcloud error and nothing saying why."""
    if gcp is not None:
        if not gcp.dns_access_enabled(gcp_project, location, gke_cluster):
            log.info("enabling DNS-based control-plane access on %s", gke_cluster)
            gcp.enable_dns_access(gcp_project, location, gke_cluster)
        gcp.get_gke_credentials(gcp_project, location, gke_cluster)
    cluster = Cluster(
        name=name,
        gcp_project=gcp_project,
        location=location,
        gke_cluster=gke_cluster,
        ar_repo=ar_repo or default_ar_repo(gcp_project, location),
    )
    session.add(cluster)
    session.commit()
    log.info("registered cluster %s (%s/%s)", name, gcp_project, gke_cluster)
    return cluster


def connect_app(
    session: Session,
    *,
    name: str,
    repo_full_name: str,
    workload_class: str,
    env_map: dict[str, tuple[int, str]],
    installation_id: int = 0,
) -> App:
    """Create the App and its Environment rows.

    env_map maps an env name to (cluster_id, namespace), e.g.
    {"dev": (1, "clips"), "qa": (2, "clips"), "prod": (3, "clips")}.
    Insertion order is preserved as the promotion order's tiebreak.
    """
    if not env_map:
        raise ValueError("connect_app needs at least one environment")
    app = App(
        name=name,
        repo_full_name=repo_full_name,
        installation_id=installation_id,
        workload_class=workload_class,
    )
    session.add(app)
    session.commit()
    for env_name, (cluster_id, namespace) in env_map.items():
        session.add(
            Environment(app_id=app.id, name=env_name, cluster_id=cluster_id, namespace=namespace)
        )
    session.commit()
    log.info("connected app %s (%s) across %s", name, repo_full_name, list(env_map))
    return app


def run_provision(
    session: Session, app: App, clients: ProvisionClients, config: Config | None = None
) -> None:
    """Synchronous provision: drive provision_app with a fresh working tree."""
    config = config or Config()
    workdir = Path(tempfile.mkdtemp(prefix=f"gojira-{app.name}-"))
    provision_app(
        session,
        app,
        gcp=clients.gcp,
        github=clients.github,
        kubectl_for=clients.kubectl_for,
        config=config,
        workdir=workdir,
    )


def start_provision(
    session_factory: sessionmaker[Session],
    app_id: int,
    clients: ProvisionClients | None = None,
    config: Config | None = None,
) -> threading.Thread:
    """Kick provisioning in a daemon thread with its own session. Returns the
    thread (tests can join it). Failures are logged; status lands in the ledger."""
    clients = clients or ProvisionClients.real()

    def worker():
        with session_factory() as s:
            app = s.get(App, app_id)
            try:
                run_provision(s, app, clients, config)
            except Exception:
                log.exception("provision thread failed for app %s", app_id)

    t = threading.Thread(target=worker, name=f"provision-{app_id}", daemon=True)
    t.start()
    return t


# --- drift detection ---------------------------------------------------------


def _scan_app(session: Session, app: App, clients: ProvisionClients) -> None:
    """Scan every environment of one app. Per-env failures are logged, not fatal
    — one unreachable repo or cluster can't halt the rest. Each env gets a fresh
    working tree, removed when done."""
    for env in app.environments:
        workdir = Path(tempfile.mkdtemp(prefix=f"gojira-drift-{app.name}-{env.name}-"))
        try:
            kubectl = clients.kubectl_for(env.cluster)
            drift_scan.scan_env(session, app, env, clients.github, kubectl, workdir)
        except Exception:
            log.exception("drift scan failed for %s/%s", app.name, env.name)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


def scan_all_apps(session: Session, clients: ProvisionClients, config: Config | None = None) -> None:
    """One drift sweep over every active app/env (the scheduler's unit of work)."""
    apps = session.scalars(select(App).where(App.status == AppStatus.ACTIVE)).all()
    for app in apps:
        _scan_app(session, app, clients)


def scan_app_drift(session: Session, app_id: int, clients: ProvisionClients) -> None:
    """Scan one app on demand (the UI's 'scan now' button). No-op unless active."""
    app = session.get(App, app_id)
    if app is None or app.status != AppStatus.ACTIVE:
        return
    _scan_app(session, app, clients)


def start_drift_scanner(
    session_factory: sessionmaker[Session],
    clients: ProvisionClients | None = None,
    config: Config | None = None,
) -> tuple[threading.Thread, threading.Event]:
    """Run drift sweeps on an interval in a daemon thread.

    Returns (thread, stop_event); set the event to end the loop cleanly. The
    first sweep runs immediately, then every `config.drift.interval_seconds`.
    """
    config = config or Config()
    clients = clients or ProvisionClients.real(config, session_factory)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                with session_factory() as s:
                    scan_all_apps(s, clients, config)
            except Exception:
                log.exception("drift sweep crashed")
            stop.wait(config.drift.interval_seconds)

    t = threading.Thread(target=loop, name="drift-scanner", daemon=True)
    t.start()
    return t, stop


# --- read models for the UI --------------------------------------------------


@dataclass
class StepView:
    name: str
    scope: str  # the "@<scope>" suffix, or "" for unscoped per-app steps
    status: str
    resource_ref: str | None
    error: str | None


@dataclass
class DriftView:
    """One environment's drift state for the app page."""

    environment: str
    status: str
    pr_url: str | None
    fields: list[tuple[str, str, str]]  # (path, git/desired, cluster/live)


@dataclass
class AppStatusView:
    app: App
    environments: list[Environment]
    steps: list[StepView]
    events: list[Event]
    terminal: bool
    counts: dict[str, int] = field(default_factory=dict)
    drift: list[DriftView] = field(default_factory=list)
    sizing: list["SizingView"] = field(default_factory=list)
    # The live rollback, if one is in effect as far as Gojira can see: the
    # most recent app.rolled_back payload, unless a redeploy or revert PR
    # superseded it. Pushes are invisible without the webhook, so the banner
    # this feeds must say "a later push re-advances" rather than claim truth.
    rollback: dict | None = None


def _drift_views(session: Session, app_id: int) -> list[DriftView]:
    rows = session.scalars(
        select(DriftFinding)
        .where(DriftFinding.app_id == app_id)
        .order_by(DriftFinding.environment)
    )
    views: list[DriftView] = []
    for r in rows:
        fields: list[tuple[str, str, str]] = []
        for d in json.loads(r.diff_json) if r.diff_json else []:
            for f in d["fields"]:
                label = f"{d['name']}.{f['path']}"
                fields.append((label, _fmt(f["desired"]), _fmt(f["live"])))
        views.append(DriftView(r.environment, str(r.status), r.pr_url, fields))
    return views


def _fmt(v) -> str:
    return "absent" if v is None else str(v)


@dataclass
class SizingView:
    """One environment's usage evidence + live recommendation for the app page."""

    environment: str
    reported_at: str                        # latest profile timestamp, iso-ish
    usage: list[tuple[str, str, str, str, str]]  # (resource, request, p95, p99, limit)
    rec_status: str | None                  # open | pr_opened | ... | None = no rec
    proposed: list[tuple[str, str, str]]    # (resource, request -> , limit ->)
    justification: str | None
    pr_url: str | None


def _sizing_views(session: Session, app_id: int) -> list[SizingView]:
    from gojira.models import RecommendationKind, SizingRecommendation, UsageProfileRow

    envs = session.scalars(
        select(UsageProfileRow.environment)
        .where(UsageProfileRow.app_id == app_id)
        .distinct()
    ).all()
    views: list[SizingView] = []
    for env in sorted(envs):
        latest = session.scalar(
            select(UsageProfileRow)
            .where(UsageProfileRow.app_id == app_id, UsageProfileRow.environment == env)
            .order_by(UsageProfileRow.reported_at.desc())
            .limit(1)
        )
        usage = []
        for resource, blob in (("cpu", latest.cpu_json), ("memory", latest.memory_json)):
            u = json.loads(blob)
            usage.append((resource, u["request"], u["p95"], u["p99"], u["limit"]))
        rec = session.scalar(
            select(SizingRecommendation).where(
                SizingRecommendation.app_id == app_id,
                SizingRecommendation.environment == env,
                SizingRecommendation.kind == RecommendationKind.OVERRIDE,
            )
        )
        proposed = []
        if rec is not None:
            cur = json.loads(rec.current_json)
            for resource, p in sorted(json.loads(rec.proposed_json).items()):
                c = cur.get(resource, {})
                proposed.append((
                    resource,
                    f"{c.get('request', '?')} → {p['request']}",
                    f"{c.get('limit', '?')} → {p['limit']}",
                ))
        views.append(SizingView(
            environment=env,
            reported_at=str(latest.reported_at),
            usage=usage,
            rec_status=str(rec.status) if rec else None,
            proposed=proposed,
            justification=rec.justification if rec else None,
            pr_url=rec.pr_url if rec else None,
        ))
    return views


def list_repos(github: GitHubClient, limit: int = 200) -> list[str]:
    """The operator's GitHub repos for the connect picker; [] if gh is unhappy."""
    try:
        return github.list_repos(limit)
    except Exception as exc:
        log.warning("failed to list repos: %s", exc)
        return []


def app_by_repo(session: Session, repo_full_name: str) -> App | None:
    return session.scalar(select(App).where(App.repo_full_name == repo_full_name))


def delete_app(
    session: Session,
    app_id: int,
    kubectl_for: Callable[[Cluster], KubectlClient],
    gcp: GCPClient,
    delete_namespaces: bool = True,
    teardown_identity: bool = True,
) -> dict | None:
    """Tear an app down: namespaces, per-project identity, images, then every
    ledger table, then the app row itself. None if the app is gone.

    Per-project identity (deploy GSA, its project IAM bindings, and its
    pushed images) is removed too by default — the same real-gate names
    provisioning used to create them (DEPLOY_ROLES, Names.for_project,
    distinct_projects), so teardown addresses exactly what provisioning
    built. It also mirrors GrantPromoteSourceReader's own pair-derivation
    (promotion_plan, PROMOTE_READER_ROLE) to remove the cross-project reader
    binding a copy-target env's GSA holds on its source env's project — a
    pair the per-project loop can't reach on its own, since it lives under a
    different project x member combination. Every GCP call is best-effort: a
    failure is appended to identity["failures"] and teardown continues, so a
    stuck binding or an already-gone resource never blocks the DB cleanup.

    Two flags temper that default:

    - teardown_identity=False skips the identity loop entirely (bindings,
      GSA, images, promote-reader removal) — the disconnect-only mode the
      delete+reconnect flow relies on to keep cloud identity alive across a
      UI-side delete. The identity sub-dict in the summary is still present,
      just zeroed/False, so callers get a stable shape either way.
    - teardown_identity=True with delete_namespaces=False still tears down
      bindings/GSA/promote-reader, but skips delete_app_images: kept
      namespaces mean the workloads are still running, and deleting every
      digest they run from just delays their death until the next
      reschedule ImagePullBackOffs. The skip (and why) is recorded in
      identity["images_skipped"].
    """
    app = session.get(App, app_id)
    if app is None:
        return None

    namespaces_deleted: list[str] = []
    namespace_failures: list[dict] = []
    if delete_namespaces:
        for env in app.environments:
            try:
                kubectl_for(env.cluster).delete_namespace(env.namespace)
                namespaces_deleted.append(env.namespace)
            except Exception as exc:  # cluster unreachable etc — don't block the delete
                log.warning(
                    "could not delete ns %s on %s: %s", env.namespace, env.cluster.name, exc
                )
                namespace_failures.append({"namespace": env.namespace, "reason": str(exc)})

    bindings_removed = 0
    gsa_deleted = False
    images_deleted = 0
    images_skipped: str | None = None
    failures: list[str] = []
    if teardown_identity:
        if not delete_namespaces:
            images_skipped = "namespaces kept, images left for running pods"
        for scope in distinct_projects(app.environments):
            names = Names.for_project(app, scope.project, scope.ar_repo)
            for role in DEPLOY_ROLES:
                try:
                    if gcp.remove_project_binding(scope.project, names.gsa_member, role):
                        bindings_removed += 1
                except Exception as exc:
                    log.warning(
                        "could not remove binding %s for app %s on %s: %s",
                        role, app.name, scope.project, exc,
                    )
                    failures.append(str(exc))
            try:
                if gcp.delete_service_account(scope.project, names.gsa_email):
                    gsa_deleted = True
            except Exception as exc:
                log.warning(
                    "could not delete GSA %s for app %s on %s: %s",
                    names.gsa_email, app.name, scope.project, exc,
                )
                failures.append(str(exc))
            if delete_namespaces:
                try:
                    images_deleted += gcp.delete_app_images(scope.ar_repo, app.name)
                except Exception as exc:
                    log.warning(
                        "could not delete images for app %s on %s: %s",
                        app.name, scope.project, exc,
                    )
                    failures.append(str(exc))

        # GrantPromoteSourceReader (steps.py) grants a cross-project reader binding
        # on the SOURCE env's project for the copy-TARGET env's GSA — a pair the
        # per-project loop above can't reach (different member x project than its
        # own DEPLOY_ROLES bindings). Mirror the grant's own pair-derivation so
        # teardown addresses exactly what provisioning built.
        plan = promotion_plan(app.environments)
        for env in app.environments:
            source = plan[env.id].copy_from
            if source is None or source.cluster.gcp_project == env.cluster.gcp_project:
                continue
            names = Names.for_project(app, env.cluster.gcp_project, env.cluster.ar_repo)
            try:
                if gcp.remove_project_binding(
                    source.cluster.gcp_project, names.gsa_member, PROMOTE_READER_ROLE
                ):
                    bindings_removed += 1
            except Exception as exc:
                log.warning(
                    "could not remove promote-reader binding for app %s on %s: %s",
                    app.name, source.cluster.gcp_project, exc,
                )
                failures.append(str(exc))

    name = app.name
    session.execute(delete(ProvisionStep).where(ProvisionStep.app_id == app_id))
    session.execute(delete(Event).where(Event.app_id == app_id))
    session.execute(delete(DriftFinding).where(DriftFinding.app_id == app_id))
    session.execute(delete(UsageProfileRow).where(UsageProfileRow.app_id == app_id))
    session.execute(delete(SizingRecommendation).where(SizingRecommendation.app_id == app_id))
    session.delete(app)  # environments cascade via App.environments delete-orphan
    session.commit()
    log.info(
        "deleted app %s (namespaces removed=%s, identity torn down=%s)",
        name, delete_namespaces, teardown_identity,
    )
    return {
        "app": name,
        "namespaces_deleted": namespaces_deleted,
        "namespace_failures": namespace_failures,
        "identity": {
            "bindings_removed": bindings_removed,
            "gsa_deleted": gsa_deleted,
            "images_deleted": images_deleted,
            "images_skipped": images_skipped,
            "failures": failures,
        },
        "rows_deleted": True,
    }


def activate_app(session: Session, app: App) -> bool:
    """Flip an awaiting-PR app to active and log the event. Idempotent: returns
    False without changing anything if the app isn't awaiting its setup PR.

    Shared by the sync poll (sync_app) and the pull_request webhook, so both
    paths converge on one state transition.
    """
    if app.status != AppStatus.AWAITING_PR:
        return False
    app.status = AppStatus.ACTIVE
    # The URL is merged history now. The provisioner recomputes status from
    # setup_pr_url after every run, so retaining it would drag the app back
    # to awaiting_pr on the next provision even with nothing left to merge.
    app.setup_pr_url = None
    session.add(Event(app_id=app.id, type="app.activated"))
    session.commit()
    log.info("app %s activated (setup PR merged)", app.name)
    return True


def sync_app(session: Session, app_id: int, github: GitHubClient) -> bool:
    """Advance an awaiting-PR app to active if its setup PR has merged.

    A pull, not a push: the manual fallback for when the webhook isn't wired
    (local single-tenant runs). Returns True if the status changed.
    """
    app = session.get(App, app_id)
    if app is None or app.status != AppStatus.AWAITING_PR:
        return False
    if not github.setup_pr_merged(app.repo_full_name):
        return False
    return activate_app(session, app)


def deploy_history(
    session: Session, app_id: int, github: GitHubClient, limit: int = 10
) -> list[dict]:
    """The app's successful CI deploys, newest first — the rollback picker.

    CI deploys the first environment; promote runs (later envs) are a
    follow-up surface."""
    app = session.get(App, app_id)
    if app is None:
        raise ValueError(f"no such app: {app_id}")
    branch = github.default_branch(app.repo_full_name)
    return github.deploy_history(app.repo_full_name, CI_WORKFLOW, branch, limit=limit)


_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def rollback_app(
    session: Session,
    app_id: int,
    sha: str,
    actor: str,
    clients: ProvisionClients,
    render=None,
) -> str:
    """Re-apply the manifests of a previously deployed commit — no rebuild.

    Digest-pinned deploys make every past green run a receipt: check the
    commit out, point its overlay at the digest the registry holds for its
    tag (the git copy deliberately says `newTag: latest` — CI pins in its
    own workspace, so we pin in ours), build, apply to the env's cluster.

    Git does NOT move: main still points at the newer commit, so the next
    drift scan will honestly show the cluster behind git, and the next push
    or Redeploy re-advances it. Rollback is the fast lever for a bad deploy;
    the durable fix is a revert PR — the returned message says so.

    First environment only (the one CI deploys); rolling back promote envs
    is a follow-up.
    """
    render = render or kustomize_build
    app = session.get(App, app_id)
    if app is None:
        raise ValueError(f"no such app: {app_id}")
    if app.status != AppStatus.ACTIVE:
        raise ValueError(f"app is {app.status}, not active — nothing to roll back")
    if not _FULL_SHA.fullmatch(sha):
        raise ValueError(f"not a full commit sha: {sha!r}")
    env = app.environments[0]
    image = f"{env.cluster.ar_repo}/{app.name}"
    tag = sha[:12]
    digest = clients.gcp.image_digest(image, tag)
    if digest is None:
        raise ValueError(
            f"the registry holds no image for {tag} — that commit was never built here"
        )

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp) / "repo"
        clients.github.clone(app.repo_full_name, workdir)
        clients.github.checkout_sha(workdir, sha)
        overlay = workdir / "k8s" / "overlays" / env.name
        kust_path = overlay / "kustomization.yaml"
        if not kust_path.is_file():
            raise ValueError(f"commit {tag} has no k8s/overlays/{env.name} — can't roll back to it")
        # The same override CI applies at deploy time. Comments in the
        # workspace copy don't survive the round-trip; the file in git does.
        kust = yaml.safe_load(kust_path.read_text()) or {}
        kust["images"] = [{"name": "gojira-app", "newName": image, "newTag": tag, "digest": digest}]
        kust_path.write_text(yaml.safe_dump(kust, sort_keys=False))
        docs = render(overlay)
        if not docs:
            raise ValueError(f"commit {tag} renders no objects — refusing an empty apply")
        clients.kubectl_for(env.cluster).apply(yaml.safe_dump_all(docs, sort_keys=False))

    session.add(Event(app_id=app.id, type="app.rolled_back", payload_json=json.dumps({
        "environment": env.name, "sha": sha,
        "image": f"{image}:{tag}@{digest}", "actor": actor,
    })))
    session.commit()
    log.info("rollback applied for %s (%s -> %s) by %s", app.name, env.name, tag, actor or "?")
    return (
        f"{env.name} now runs {tag}. Git still points at the newer commit — "
        "open a revert PR to make this stick; the next push or Redeploy re-advances."
    )


def open_revert_pr(
    session: Session, app_id: int, sha: str, github: GitHubClient, actor: str = ""
) -> str:
    """The durable half of rollback: a PR that moves git back to the tree the
    cluster now runs. rollback_env is the fast lever (cluster only, honest
    message); this makes it stick through review. Merging rebuilds the old
    tree through the normal pipeline — a fresh image of the old code.

    Idempotent per target: the branch name is derived from the sha, so a
    second click finds the open PR instead of stacking a twin. If git already
    matches the target tree (rolled back to the current tip), says so instead
    of opening an empty PR.
    """
    app = session.get(App, app_id)
    if app is None:
        raise ValueError(f"no such app: {app_id}")
    if app.status != AppStatus.ACTIVE:
        raise ValueError(f"app is {app.status}, not active")
    if not _FULL_SHA.fullmatch(sha):
        raise ValueError(f"not a full commit sha: {sha!r}")
    tag = sha[:12]
    branch = f"gojira/revert-{tag}"
    existing = github.existing_pr(app.repo_full_name, branch)
    if existing:
        return f"revert PR already open: {existing}"

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp) / "repo"
        github.clone(app.repo_full_name, workdir)
        base = github.default_branch(app.repo_full_name)
        github.checkout_branch(workdir, branch)
        github.restore_tree(workdir, sha)
        committed = github.commit_all(
            workdir,
            f"revert: back to {tag} — making a rollback durable\n\n"
            f"An operator rolled the running environment back to {tag} via "
            "Gojira; this PR moves git to the same tree so the next push "
            "starts from what is actually running. Merging rebuilds this "
            "tree through the normal pipeline.",
        )
        if not committed:
            return f"git already matches {tag} — nothing to revert"
        github.push(workdir, branch)
        url = github.open_pr(
            app.repo_full_name,
            branch,
            base,
            f"revert: back to {tag} (Gojira rollback made durable)",
            f"The running environment was rolled back to `{tag}`"
            + (f" by {actor}" if actor else "")
            + " via Gojira's rollback button. Git still pointed at a newer "
            "commit — merging this aligns git with the cluster, so the next "
            "push builds forward from what is actually running.\n\n"
            "🤖 Generated by Gojira",
        )
    session.add(Event(app_id=app.id, type="app.revert_pr_opened", payload_json=json.dumps({
        "sha": sha, "pr_url": url, "actor": actor,
    })))
    session.commit()
    log.info("revert PR for %s back to %s: %s (by %s)", app.name, tag, url, actor or "?")
    return f"revert PR opened: {url}"


def redeploy_app(session: Session, app_id: int, github: GitHubClient) -> str:
    """Re-run the app's CI deploy from the current default branch — no commit
    required. The resurrection path for a namespace whose workloads are gone
    while the contract is fine (delete-then-reconnect, a wiped namespace): CI
    is the only deploy path, so redeploy = dispatch the generated workflow.
    """
    app = session.get(App, app_id)
    if app is None:
        raise ValueError("no such app")
    branch = github.default_branch(app.repo_full_name)
    github.dispatch_workflow(app.repo_full_name, CI_WORKFLOW, branch)
    session.add(Event(app_id=app.id, type="app.redeployed"))
    session.commit()
    log.info("redeploy dispatched for %s (%s@%s)", app.name, app.repo_full_name, branch)
    return f"CI dispatched on {branch}"


def cut_release(github: GitHubClient, repo: str, version: str) -> str:
    """Tag the default branch's HEAD as version, triggering the promote workflow.

    Normalizes to a 'v*' tag (what the promote workflow listens for). Returns the
    tag actually created.
    """
    tag = version.strip()
    if not tag:
        raise ValueError("a version is required")
    if not tag.startswith("v"):
        tag = f"v{tag}"
    github.create_tag(repo, tag, github.head_sha(repo))
    log.info("cut release %s for %s", tag, repo)
    return tag


def repo_has_dockerfile(github: GitHubClient, repo: str) -> bool:
    """Pre-flight: Gojira can only build a repo that ships a Dockerfile. On a
    transient gh error, don't block — let provisioning proceed."""
    try:
        return github.file_exists(repo, "Dockerfile")
    except Exception as exc:
        log.warning("Dockerfile pre-flight failed for %s: %s", repo, exc)
        return True


def list_apps(session: Session) -> list[App]:
    return list(session.scalars(select(App).order_by(App.id.desc())))


def list_clusters(session: Session) -> list[Cluster]:
    return list(session.scalars(select(Cluster).order_by(Cluster.name)))


def app_status(session: Session, app_id: int) -> AppStatusView:
    """Everything the app-detail page renders: status, ledger steps, events."""
    app = session.get(App, app_id)
    if app is None:
        raise KeyError(f"app {app_id} not found")
    steps: list[StepView] = []
    counts = {"done": 0, "skipped": 0, "running": 0, "failed": 0, "pending": 0}
    rows = session.scalars(
        select(ProvisionStep).where(ProvisionStep.app_id == app_id).order_by(ProvisionStep.id)
    )
    for r in rows:
        base, _, scope = r.step_name.partition("@")
        status = str(r.status)
        steps.append(StepView(base, scope, status, r.resource_ref, r.error))
        counts[status] = counts.get(status, 0) + 1
    events = list(
        session.scalars(
            select(Event).where(Event.app_id == app_id).order_by(Event.id.desc())
        )
    )[:20]
    rollback = None
    for e in events:  # newest first — first hit decides
        if e.type in ("app.redeployed", "app.revert_pr_opened"):
            break
        if e.type == "app.rolled_back":
            rollback = json.loads(e.payload_json or "{}")
            rollback["at"] = str(e.created_at)
            break
    return AppStatusView(
        app=app,
        environments=list(app.environments),
        steps=steps,
        events=events,
        terminal=app.status in TERMINAL_STATUSES,
        counts=counts,
        drift=_drift_views(session, app_id),
        sizing=_sizing_views(session, app_id),
        rollback=rollback,
    )
