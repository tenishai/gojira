"""Sizing service layer: ingestion behind the web handler.

Mirrors the service.py pattern — nothing here imports FastAPI. The web layer
hands in the parsed UsageProfile and the presented bearer token; this module
authenticates it against agent_tokens, resolves the app, and appends to the
evidence ledger.

Token model: one bearer token per cluster, minted at agent-deploy time; only
its SHA-256 lives in the DB. A cluster's token can only report apps that have
an environment on that cluster — a compromised sandbox token can't feed
evidence for prod apps elsewhere.
"""

import hashlib
import json
import logging
import secrets
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from gojira.config import SizingConfig
from gojira.models import (
    AgentToken,
    App,
    Cluster,
    Environment,
    RecommendationKind,
    SizingRecommendation,
    UsageProfileRow,
    utcnow,
)
from gojira.sizing.pr import open_override_pr
from gojira.sizing.profile import UsageProfile
from gojira.sizing.recommender import recompute_for_app
from gojira.sizing.store import DbSizingStore, _row_to_profile

log = logging.getLogger(__name__)


class IngestError(Exception):
    """Rejected report. `status` maps to the HTTP response code."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint_agent_token(session: Session, cluster: Cluster) -> str:
    """Mint (or replace) the ingestion token for a cluster. Returns the
    plaintext exactly once — only the hash is stored."""
    token = secrets.token_urlsafe(32)
    row = session.scalar(select(AgentToken).where(AgentToken.cluster_id == cluster.id))
    if row is None:
        row = AgentToken(cluster_id=cluster.id, token_sha256=_sha256(token))
        session.add(row)
    else:
        row.token_sha256 = _sha256(token)  # rotation: old token dies here
        row.created_at = utcnow()  # the row outlives tokens; the date is the token's
    session.commit()
    log.info("minted agent token for cluster %s", cluster.name)
    return token


def agent_tokens_by_cluster(session: Session) -> dict[int, AgentToken]:
    """cluster_id → its token row. A row's existence is the 'agent deployed'
    signal the clusters page shows; created_at is the current token's mint time."""
    return {t.cluster_id: t for t in session.scalars(select(AgentToken))}


def open_pr_for(session: Session, app_id: int, env: str, github) -> str | None:
    """The UI button: author the sizing PR for (app, env)'s live recommendation.

    Returns the PR URL, or None when there's no OPEN recommendation or nothing
    to change. Clones into a throwaway tree, same as the drift scanner.
    """
    app = session.get(App, app_id)
    if app is None:
        return None
    rec = session.scalar(
        select(SizingRecommendation).where(
            SizingRecommendation.app_id == app_id,
            SizingRecommendation.environment == env,
            SizingRecommendation.kind == RecommendationKind.OVERRIDE,
        )
    )
    if rec is None:
        return None
    workdir = Path(tempfile.mkdtemp(prefix=f"gojira-sizing-{app.name}-"))
    try:
        return open_override_pr(session, app, rec, github, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def usage_history(
    session: Session, app_id: int, *, env: str | None = None, since_days: int = 30,
) -> dict | None:
    """The read side of /api/v1/usage: the evidence ledger and live
    recommendation per environment, in the same wire shape the agent POSTed.

    Built for a second evidence producer (argus): during a parity window it diffs
    its own summary against what the sizing agent reported AND against the
    call the recommender made from it. Every environment the app has is
    listed, so "no evidence yet" is an explicit empty list, not an absence.
    None for an unknown app; an unknown env (when given) reads as None too.
    """
    app = session.get(App, app_id)
    if app is None:
        return None
    envs = {e.name: e.cluster.name for e in app.environments}
    if env is not None:
        if env not in envs:
            return None
        envs = {env: envs[env]}
    cutoff = utcnow().replace(tzinfo=None) - timedelta(days=since_days)
    out: dict[str, dict] = {}
    for name, cluster_name in sorted(envs.items()):
        rows = session.scalars(
            select(UsageProfileRow)
            .where(
                UsageProfileRow.app_id == app_id,
                UsageProfileRow.environment == name,
                UsageProfileRow.reported_at >= cutoff,
            )
            .order_by(UsageProfileRow.reported_at.desc())
        ).all()
        rec = session.scalar(
            select(SizingRecommendation).where(
                SizingRecommendation.app_id == app_id,
                SizingRecommendation.environment == name,
                SizingRecommendation.kind == RecommendationKind.OVERRIDE,
            )
        )
        out[name] = {
            "cluster": cluster_name,
            "profiles": [
                {
                    "profile_id": r.id,
                    "received_at": r.received_at.isoformat() if r.received_at else None,
                    **json.loads(_row_to_profile(app.name, cluster_name, r).model_dump_json()),
                }
                for r in rows
            ],
            "recommendation": None if rec is None else {
                "status": str(rec.status),
                "current": json.loads(rec.current_json),
                "proposed": json.loads(rec.proposed_json),
                "justification": rec.justification,
                "pr_url": rec.pr_url,
                "based_on_profile_id": rec.based_on_profile_id,
            },
        }
    return {"id": app.id, "name": app.name, "since_days": since_days, "environments": out}


def ingest_usage(
    session: Session,
    profile: UsageProfile,
    bearer_token: str,
    config: SizingConfig | None = None,
) -> UsageProfileRow:
    """Authenticate, resolve, append, recompute. Raises IngestError (401/403/404)."""
    token_row = session.scalar(
        select(AgentToken).where(AgentToken.token_sha256 == _sha256(bearer_token))
    )
    if token_row is None:
        raise IngestError(401, "unknown token")

    cluster = session.get(Cluster, token_row.cluster_id)
    if cluster is None or cluster.name != profile.cluster:
        raise IngestError(403, "token not valid for this cluster")

    app = session.scalar(select(App).where(App.name == profile.app))
    env = (
        session.scalar(
            select(Environment).where(
                Environment.app_id == app.id,
                Environment.name == profile.environment,
                Environment.cluster_id == cluster.id,
            )
        )
        if app
        else None
    )
    if app is None or env is None:
        raise IngestError(404, "no such app/environment on this cluster")

    store = DbSizingStore(session)
    row_id = store.record(app.id, profile)
    log.info(
        "usage profile ingested: %s/%s window=%dd samples=%d",
        profile.app, profile.environment, profile.window_days, profile.samples,
    )
    # Recompute on report (design decision: recompute-on-report, debounced PRs).
    recompute_for_app(
        session, app, profile.environment, store,
        config=config, based_on_profile_id=row_id,
    )
    return session.get(UsageProfileRow, row_id)
