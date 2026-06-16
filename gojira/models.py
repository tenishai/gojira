"""Data model: clusters, apps, provision steps, events.

provision_steps is the idempotency ledger: one row per step per app run.
Re-runs skip done steps; failures resume from the failed step; teardown
reads resource_ref to know exactly what was created.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AppStatus(enum.StrEnum):
    PENDING = "pending"          # connected, provisioning not started
    PROVISIONING = "provisioning"
    AWAITING_PR = "awaiting_pr"  # setup PR open, not yet merged
    ACTIVE = "active"            # setup PR merged, deploys flowing
    FAILED = "failed"            # provisioning halted on a failed step


class StepStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"          # check() found the resource already exists
    FAILED = "failed"


class Cluster(Base):
    """A registered deploy target. Connectivity is validated at registration.

    A cluster carries its own project + Artifact Registry, so the set of
    projects an app touches is derived from the clusters its environments
    bind to. Clusters in the same project share one AR repo.
    """

    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(63), unique=True)
    gcp_project: Mapped[str] = mapped_column(String(63))
    location: Mapped[str] = mapped_column(String(63))
    gke_cluster: Mapped[str] = mapped_column(String(63))
    ar_repo: Mapped[str] = mapped_column(String(255))  # shared AR repo for this project
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())

    environments: Mapped[list["Environment"]] = relationship(back_populates="cluster")


class App(Base):
    """A connected GitHub repo deployed through Gojira.

    An App holds only what's environment-independent (repo, workload class).
    Where it deploys — cluster + namespace per env — lives on Environment.
    """

    __tablename__ = "apps"
    __table_args__ = (UniqueConstraint("repo_full_name", name="uq_apps_repo"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(63))
    repo_full_name: Mapped[str] = mapped_column(String(255))  # owner/repo
    installation_id: Mapped[int]                              # GitHub App installation
    workload_class: Mapped[str] = mapped_column(String(16))
    status: Mapped[AppStatus] = mapped_column(String(16), default=AppStatus.PENDING)
    setup_pr_url: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())

    environments: Mapped[list["Environment"]] = relationship(
        back_populates="app", order_by="Environment.id", cascade="all, delete-orphan"
    )
    steps: Mapped[list["ProvisionStep"]] = relationship(
        back_populates="app", order_by="ProvisionStep.id"
    )
    events: Mapped[list["Event"]] = relationship(back_populates="app")


class Environment(Base):
    """One stage in an app's promotion path: a name bound to (cluster, namespace).

    An environment is always a distinct cluster + namespace — never just a
    namespace in a shared cluster. Environments may live in the same project
    or in different projects; the cluster carries the project, so multi-project
    is just the case where the distinct-project count is greater than one.
    """

    __tablename__ = "environments"
    __table_args__ = (UniqueConstraint("app_id", "name", name="uq_env_app_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"))
    name: Mapped[str] = mapped_column(String(16))  # dev | qa | prod
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"))
    namespace: Mapped[str] = mapped_column(String(63))

    app: Mapped[App] = relationship(back_populates="environments")
    cluster: Mapped[Cluster] = relationship(back_populates="environments")


class ProvisionStep(Base):
    """Audit + idempotency ledger: one row per provisioner step per app."""

    __tablename__ = "provision_steps"
    __table_args__ = (UniqueConstraint("app_id", "step_name", name="uq_steps_app_step"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id"))
    step_name: Mapped[str] = mapped_column(String(63))
    status: Mapped[StepStatus] = mapped_column(String(16), default=StepStatus.PENDING)
    resource_ref: Mapped[str | None] = mapped_column(String(512))  # what was created
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]

    app: Mapped[App] = relationship(back_populates="steps")


class InstallationRepo(Base):
    """Which GitHub App installation covers a repo — a persisted cache.

    The source of truth is the GitHub API (GET /repos/{repo}/installation);
    rows are refreshed on a cache miss and kept current by the installation*
    webhooks. Lets the App backend mint an installation token for a repo
    without an API round-trip on the hot path, and backs the connect picker's
    repo list when running App-backed.
    """

    __tablename__ = "installation_repos"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_full_name: Mapped[str] = mapped_column(String(255), unique=True)
    installation_id: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, server_default=func.now(), onupdate=utcnow
    )


class Event(Base):
    """Append-only log: webhooks received, deploys observed, operator actions."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int | None] = mapped_column(ForeignKey("apps.id"))
    type: Mapped[str] = mapped_column(String(63))
    payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())

    app: Mapped[App | None] = relationship(back_populates="events")
