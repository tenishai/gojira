"""Recommender — evidence in, rightsizing proposals out.

The recommendation seam: PercentileRecommender is the core default, a
transparent rule an operator can predict from the README. A reasoning
backend (LLM judgment over noisy histories) implements the same ABC from
outside — built only if this rule's output on real data proves insufficient.

The rule:
    request  <- p95, rounded up to a sane step
    limit    <- max(p99 * limit_headroom, proposed request)
Guardrails:
    - materiality: no proposal unless request moves >= materiality_pct
    - evidence over wishes: never propose LESS memory for an app with
      OOM kills or restarts in the window
    - one live proposal per (app, env, kind): recompute supersedes in place
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from gojira.config import SizingConfig
from gojira.drift import normalize_quantity
from gojira.models import (
    App,
    RecommendationKind,
    RecommendationStatus,
    SizingRecommendation,
)
from gojira.sizing.profile import UsageProfile
from gojira.sizing.store import SizingStore

log = logging.getLogger(__name__)

CPU_STEP_M = 50           # round proposed cpu up to 50m steps
MEM_STEP = 64 * 1024**2   # round proposed memory up to 64Mi steps


def fmt_cpu(millicores: int) -> str:
    return f"{millicores // 1000}" if millicores % 1000 == 0 else f"{millicores}m"


def fmt_mem(nbytes: int) -> str:
    if nbytes % 1024**3 == 0:
        return f"{nbytes // 1024**3}Gi"
    return f"{max(1, round(nbytes / 1024**2))}Mi"


def _round_up(value: int, step: int) -> int:
    return ((value + step - 1) // step) * step


@dataclass(frozen=True)
class Proposal:
    """One resource's move: current -> proposed, with the evidence line."""

    resource: str            # cpu | memory
    current_request: str
    current_limit: str
    proposed_request: str
    proposed_limit: str
    evidence: str


class Recommender(ABC):
    @abstractmethod
    def recommend(self, app: str, env: str, store: SizingStore) -> list[Proposal]:
        """Proposals for (app, env) from the store's evidence. May be empty."""


class PercentileRecommender(Recommender):
    def __init__(self, config: SizingConfig | None = None):
        self._cfg = config or SizingConfig()

    def recommend(self, app: str, env: str, store: SizingStore) -> list[Proposal]:
        history = store.history(app, env, since_days=self._cfg.history_days)
        if not history:
            return []
        latest = history[0]
        proposals = []
        for resource in ("cpu", "memory"):
            p = self._propose(resource, latest)
            if p is not None:
                proposals.append(p)
        return proposals

    def _propose(self, resource: str, profile: UsageProfile) -> Proposal | None:
        usage = getattr(profile, resource)
        key = "cpu" if resource == "cpu" else "memory"
        step = CPU_STEP_M if resource == "cpu" else MEM_STEP
        fmt = fmt_cpu if resource == "cpu" else fmt_mem

        current_req = normalize_quantity(key, usage.request)
        p95 = normalize_quantity(key, usage.p95)
        p99 = normalize_quantity(key, usage.p99)

        proposed_req = _round_up(max(p95, 1), step)
        delta_pct = abs(proposed_req - current_req) / current_req * 100
        if delta_pct < self._cfg.materiality_pct:
            return None

        # Evidence over wishes: no memory downsizing against distress signals.
        downsizing = proposed_req < current_req
        if resource == "memory" and downsizing and (profile.oom_kills > 0 or profile.restarts > 0):
            log.info(
                "suppressing memory downsize for %s/%s: oom_kills=%d restarts=%d",
                profile.app, profile.environment, profile.oom_kills, profile.restarts,
            )
            return None

        proposed_limit = _round_up(
            max(int(p99 * self._cfg.limit_headroom), proposed_req), step
        )
        return Proposal(
            resource=resource,
            current_request=usage.request,
            current_limit=usage.limit,
            proposed_request=fmt(proposed_req),
            proposed_limit=fmt(proposed_limit),
            evidence=(
                f"{resource}: p95 {usage.p95}, p99 {usage.p99} over "
                f"{profile.window_days}d ({profile.samples} samples); "
                f"request {usage.request} -> {fmt(proposed_req)}"
            ),
        )


def upsert_recommendation(
    session: Session, app_id: int, env: str, proposals: list[Proposal],
    based_on_profile_id: int | None = None,
) -> SizingRecommendation | None:
    """Persist proposals as the single live OVERRIDE recommendation for
    (app, env) — update in place, don't stack. No proposals + no existing row
    is a no-op; no proposals with an OPEN row marks it stale."""
    existing = session.scalar(
        select(SizingRecommendation).where(
            SizingRecommendation.app_id == app_id,
            SizingRecommendation.environment == env,
            SizingRecommendation.kind == RecommendationKind.OVERRIDE,
        )
    )
    if not proposals:
        if existing is not None and existing.status == RecommendationStatus.OPEN:
            existing.status = RecommendationStatus.STALE
            session.commit()
        return existing

    current = {p.resource: {"request": p.current_request, "limit": p.current_limit}
               for p in proposals}
    proposed = {p.resource: {"request": p.proposed_request, "limit": p.proposed_limit}
                for p in proposals}
    justification = "; ".join(p.evidence for p in proposals)

    if existing is None:
        existing = SizingRecommendation(
            app_id=app_id, environment=env, kind=RecommendationKind.OVERRIDE,
        )
        session.add(existing)
    # A PR_OPENED row keeps its status (the PR is the live artifact); fresh
    # evidence still refreshes the payload the PR generator reads.
    if existing.status != RecommendationStatus.PR_OPENED:
        existing.status = RecommendationStatus.OPEN
    existing.current_json = json.dumps(current)
    existing.proposed_json = json.dumps(proposed)
    existing.justification = justification
    existing.based_on_profile_id = based_on_profile_id
    session.commit()
    log.info("recommendation upserted for app_id=%s env=%s: %s", app_id, env, justification)
    return existing


def recompute_for_app(
    session: Session, app: App, env: str, store: SizingStore,
    config: SizingConfig | None = None, based_on_profile_id: int | None = None,
) -> SizingRecommendation | None:
    """The A2->A4 hook: called after each ingested report."""
    proposals = PercentileRecommender(config).recommend(app.name, env, store)
    return upsert_recommendation(
        session, app.id, env, proposals, based_on_profile_id=based_on_profile_id
    )
