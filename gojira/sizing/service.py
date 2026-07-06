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
import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from gojira.config import SizingConfig
from gojira.models import AgentToken, App, Cluster, Environment, UsageProfileRow
from gojira.sizing.profile import UsageProfile
from gojira.sizing.recommender import recompute_for_app
from gojira.sizing.store import DbSizingStore

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
    session.commit()
    log.info("minted agent token for cluster %s", cluster.name)
    return token


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
