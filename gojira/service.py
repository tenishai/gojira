"""Service layer: the seam between the web handlers and the provisioner.

Web handlers call into here; nothing in this module imports FastAPI. It builds
the real clients, creates working trees, drives provision_app in a background
thread, and exposes read models over the ledger/events for the UI to poll.
"""

import logging
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gojira.config import Config
from gojira.gcp import GCPClient, GcloudClient
from gojira.github import GhCLI, GitHubClient
from gojira.k8s import KubectlCLI, KubectlClient
from gojira.models import App, AppStatus, Cluster, Environment, Event, ProvisionStep
from gojira.provisioner import provision_app

log = logging.getLogger(__name__)

TERMINAL_STATUSES = {AppStatus.AWAITING_PR, AppStatus.ACTIVE, AppStatus.FAILED}


def default_ar_repo(gcp_project: str, location: str) -> str:
    """The per-project Artifact Registry repo Gojira uses by convention."""
    return f"{location}-docker.pkg.dev/{gcp_project}/apps"


@dataclass
class ProvisionClients:
    """The three clients provision_app needs. Swap fakes in for tests."""

    gcp: GCPClient
    github: GitHubClient
    kubectl_for: Callable[[Cluster], KubectlClient]

    @classmethod
    def real(cls) -> "ProvisionClients":
        gcp = GcloudClient()

        def kubectl_for(cluster: Cluster) -> KubectlClient:
            ctx = gcp.get_gke_credentials(
                cluster.gcp_project, cluster.location, cluster.gke_cluster
            )
            return KubectlCLI(context=ctx)

        return cls(gcp=gcp, github=GhCLI(), kubectl_for=kubectl_for)


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
    by fetching credentials first (raises if the cluster is unreachable)."""
    if gcp is not None:
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


# --- read models for the UI --------------------------------------------------


@dataclass
class StepView:
    name: str
    scope: str  # the "@<scope>" suffix, or "" for unscoped per-app steps
    status: str
    resource_ref: str | None
    error: str | None


@dataclass
class AppStatusView:
    app: App
    environments: list[Environment]
    steps: list[StepView]
    events: list[Event]
    terminal: bool
    counts: dict[str, int] = field(default_factory=dict)


def list_repos(github: GitHubClient, limit: int = 200) -> list[str]:
    """The operator's GitHub repos for the connect picker; [] if gh is unhappy."""
    try:
        return github.list_repos(limit)
    except Exception as exc:
        log.warning("failed to list repos: %s", exc)
        return []


def app_by_repo(session: Session, repo_full_name: str) -> App | None:
    return session.scalar(select(App).where(App.repo_full_name == repo_full_name))


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
    return AppStatusView(
        app=app,
        environments=list(app.environments),
        steps=steps,
        events=events,
        terminal=app.status in TERMINAL_STATUSES,
        counts=counts,
    )
