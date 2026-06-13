"""Provisioner step engine: ordered, idempotent, resumable.

Each step implements check() -> existing resource ref (skip) or None,
and apply() -> created resource ref. The engine walks the sequence,
recording every outcome in the ProvisionStep ledger. Re-running an app
skips done/skipped steps and retries failed ones — resume-from-failure
comes for free from the ledger.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from gojira.config import Config
from gojira.models import App, AppStatus, Cluster, Event, ProvisionStep, StepStatus, utcnow

log = logging.getLogger(__name__)


@dataclass
class ProvisionContext:
    """Everything a step needs. Clients (GCP, GitHub) attach here as they exist."""

    app: App
    cluster: Cluster
    config: Config
    clients: dict = field(default_factory=dict)
    # The repo working tree: generate_artifacts writes here, open_setup_pr
    # commits and pushes it. Set by the service layer after cloning the repo.
    workdir: Path | None = None


class Step(Protocol):
    """A provisioner step. Implementations must be idempotent."""

    name: str

    def check(self, ctx: ProvisionContext) -> str | None:
        """Return the resource ref if it already exists (step will be skipped)."""
        ...

    def apply(self, ctx: ProvisionContext) -> str:
        """Create the resource. Return its ref for the ledger."""
        ...


@dataclass
class RunResult:
    completed: bool
    steps_run: list[str]
    steps_skipped: list[str]
    failed_step: str | None = None
    error: str | None = None


class ProvisionEngine:
    """Walks a step sequence for an app, recording outcomes in the ledger."""

    def __init__(self, steps: list[Step]):
        names = [s.name for s in steps]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate step names in sequence: {names}")
        self.steps = steps

    def run(self, session: Session, ctx: ProvisionContext) -> RunResult:
        app = ctx.app
        app.status = AppStatus.PROVISIONING
        self._emit(session, app, "provision.run_started")
        session.commit()

        result = RunResult(completed=False, steps_run=[], steps_skipped=[])

        for step in self.steps:
            ledger = self._ledger_row(session, app, step.name)

            if ledger.status in (StepStatus.DONE, StepStatus.SKIPPED):
                log.debug("step %s already %s, skipping", step.name, ledger.status)
                continue

            # PENDING or FAILED (retry): run it
            ledger.status = StepStatus.RUNNING
            ledger.started_at = utcnow()
            ledger.error = None
            session.commit()

            try:
                existing = step.check(ctx)
                if existing is not None:
                    ledger.status = StepStatus.SKIPPED
                    ledger.resource_ref = existing
                    result.steps_skipped.append(step.name)
                    log.info("step %s: exists (%s)", step.name, existing)
                else:
                    ledger.resource_ref = step.apply(ctx)
                    ledger.status = StepStatus.DONE
                    result.steps_run.append(step.name)
                    log.info("step %s: created (%s)", step.name, ledger.resource_ref)
                ledger.finished_at = utcnow()
                session.commit()
            except Exception as exc:
                ledger.status = StepStatus.FAILED
                ledger.error = str(exc)
                ledger.finished_at = utcnow()
                app.status = AppStatus.FAILED
                self._emit(
                    session, app, "provision.step_failed",
                    {"step": step.name, "error": str(exc)},
                )
                session.commit()
                log.error("step %s failed: %s", step.name, exc)
                result.failed_step = step.name
                result.error = str(exc)
                return result

        # The final step opens the setup PR, so a completed run awaits its merge.
        app.status = AppStatus.AWAITING_PR
        self._emit(session, app, "provision.run_completed")
        session.commit()
        result.completed = True
        return result

    def _ledger_row(self, session: Session, app: App, step_name: str) -> ProvisionStep:
        row = session.scalar(
            select(ProvisionStep).where(
                ProvisionStep.app_id == app.id, ProvisionStep.step_name == step_name
            )
        )
        if row is None:
            row = ProvisionStep(app_id=app.id, step_name=step_name)
            session.add(row)
            session.commit()
        return row

    @staticmethod
    def _emit(session: Session, app: App, event_type: str, payload: dict | None = None):
        session.add(
            Event(
                app_id=app.id,
                type=event_type,
                payload_json=json.dumps(payload) if payload else None,
            )
        )
