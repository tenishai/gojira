"""SizingStore — where usage evidence accumulates.

The storage seam from the design doc: the core ships DbSizingStore (Gojira's
own tables, zero extra dependencies); richer backends (e.g. a techne-backed
store with confidence decay and fleet convergence) implement the same ABC
from outside, if the data ever justifies them.
"""

import json
import logging
from abc import ABC, abstractmethod
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from gojira.models import App, Cluster, Environment, UsageProfileRow, utcnow
from gojira.sizing.profile import ResourceUsage, UsageProfile

log = logging.getLogger(__name__)


class SizingStore(ABC):
    """Evidence in, history out. Implementations own their persistence."""

    @abstractmethod
    def record(self, app_id: int, profile: UsageProfile) -> int:
        """Append one report. Returns the stored row/entry id."""

    @abstractmethod
    def history(self, app: str, env: str, *, since_days: int) -> list[UsageProfile]:
        """Reports for (app, env) whose reported_at falls in the window,
        newest first."""


class DbSizingStore(SizingStore):
    """The core default: usage_profiles rows in Gojira's own database."""

    def __init__(self, session: Session):
        self._session = session

    def record(self, app_id: int, profile: UsageProfile) -> int:
        row = UsageProfileRow(
            app_id=app_id,
            environment=profile.environment,
            window_days=profile.window_days,
            samples=profile.samples,
            cpu_json=profile.cpu.model_dump_json(),
            memory_json=profile.memory.model_dump_json(),
            restarts=profile.restarts,
            oom_kills=profile.oom_kills,
            reported_by=profile.reported_by,
            reported_at=profile.reported_at.replace(tzinfo=None),
        )
        self._session.add(row)
        self._session.commit()
        return row.id

    def history(self, app: str, env: str, *, since_days: int) -> list[UsageProfile]:
        cutoff = utcnow().replace(tzinfo=None) - timedelta(days=since_days)
        rows = self._session.scalars(
            select(UsageProfileRow)
            .join(App, App.id == UsageProfileRow.app_id)
            .where(
                App.name == app,
                UsageProfileRow.environment == env,
                UsageProfileRow.reported_at >= cutoff,
            )
            .order_by(UsageProfileRow.reported_at.desc())
        ).all()
        if not rows:
            return []
        # The row doesn't carry the cluster; the (app, env) binding does.
        cluster_name = self._session.scalar(
            select(Cluster.name)
            .join(Environment, Environment.cluster_id == Cluster.id)
            .join(App, App.id == Environment.app_id)
            .where(App.name == app, Environment.name == env)
        ) or "unknown"
        return [_row_to_profile(app, cluster_name, r) for r in rows]


def _row_to_profile(app_name: str, cluster_name: str, row: UsageProfileRow) -> UsageProfile:
    return UsageProfile(
        app=app_name,
        environment=row.environment,
        cluster=cluster_name,
        window_days=row.window_days,
        samples=row.samples,
        cpu=ResourceUsage.model_validate(json.loads(row.cpu_json)),
        memory=ResourceUsage.model_validate(json.loads(row.memory_json)),
        restarts=row.restarts,
        oom_kills=row.oom_kills,
        reported_by=row.reported_by,
        reported_at=row.reported_at,
    )
