"""GitHub webhook ingestion: signature verification + event dispatch.

The push-based counterpart to service.sync_app's poll. The web layer hands raw
bytes + headers in here; nothing in this module imports FastAPI, so handlers
are tested directly with crafted payloads.

Events are matched to apps by repository full name, so ingestion works under
either GitHub client backend (gh CLI today, the GitHub App later) — the payload
carries the repo, no installation lookup required.
"""

import hashlib
import hmac
import json
import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from gojira import service
from gojira.github import SETUP_BRANCH
from gojira.models import Event, InstallationRepo

log = logging.getLogger(__name__)

# The `name:` values our generator emits (see generator/templates/workflows).
CI_WORKFLOW = "Gojira CI"
PROMOTE_WORKFLOW = "Gojira Promote"


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Constant-time check of the X-Hub-Signature-256 header against the raw body.

    Returns False if the secret is unset (can't verify → don't trust) or the
    header is missing — fail closed.
    """
    if not secret or not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def handle_event(session: Session, event_type: str, payload: dict) -> str | None:
    """Dispatch a verified webhook to its handler.

    Returns a short result string for logging, or None if the event was ignored.
    Never raises on an unknown event type or an unmatched repo — GitHub sends
    plenty we don't care about, and we ack them all with a 2xx.
    """
    handler = {
        "pull_request": _handle_pull_request,
        "workflow_run": _handle_workflow_run,
        "installation": _handle_installation,
        "installation_repositories": _handle_installation_repositories,
    }.get(event_type)
    if handler is None:
        return None
    return handler(session, payload)


def _repo_app(session: Session, payload: dict):
    """The App for this payload's repository, or None if we don't track it."""
    repo = (payload.get("repository") or {}).get("full_name")
    return service.app_by_repo(session, repo) if repo else None


def _handle_pull_request(session: Session, payload: dict) -> str | None:
    """A merged gojira/setup PR activates the app — the push that replaces the
    sync_app poll. Anything else (still open, other branch) is ignored."""
    pr = payload.get("pull_request") or {}
    if payload.get("action") != "closed" or not pr.get("merged"):
        return None
    if (pr.get("head") or {}).get("ref") != SETUP_BRANCH:
        return None
    app = _repo_app(session, payload)
    if app is None:
        return None
    activated = service.activate_app(session, app)
    return f"setup PR merged → {app.name} {'activated' if activated else 'already active'}"


def _handle_workflow_run(session: Session, payload: dict) -> str | None:
    """Record the outcome of a completed Gojira CI / Promote run as an Event,
    giving the UI visibility into deploys it kicked off but couldn't see."""
    if payload.get("action") != "completed":
        return None
    run = payload.get("workflow_run") or {}
    name = run.get("name")
    if name not in (CI_WORKFLOW, PROMOTE_WORKFLOW):
        return None
    app = _repo_app(session, payload)
    if app is None:
        return None
    kind = "promote" if name == PROMOTE_WORKFLOW else "ci"
    conclusion = run.get("conclusion") or "unknown"  # success | failure | cancelled | ...
    event_type = f"{kind}.{conclusion}"
    session.add(
        Event(
            app_id=app.id,
            type=event_type,
            payload_json=json.dumps(
                {
                    "workflow": name,
                    "conclusion": conclusion,
                    "ref": run.get("head_branch"),  # tag name (v1.2.3) for a promote
                    "sha": run.get("head_sha"),
                    "run_url": run.get("html_url"),
                }
            ),
        )
    )
    session.commit()
    return f"{event_type} for {app.name}"


def _upsert_repos(session: Session, installation_id: int, repos: list[str]) -> None:
    """Point each repo at this installation in the cache table (insert or update)."""
    for full_name in repos:
        row = session.scalar(
            select(InstallationRepo).where(InstallationRepo.repo_full_name == full_name)
        )
        if row is None:
            session.add(
                InstallationRepo(repo_full_name=full_name, installation_id=installation_id)
            )
        else:
            row.installation_id = installation_id


def _handle_installation(session: Session, payload: dict) -> str | None:
    """App installed / uninstalled / repos changed at the installation level —
    keep the repo→installation cache in step with it."""
    inst = payload.get("installation") or {}
    iid = inst.get("id")
    if iid is None:
        return None
    action = payload.get("action")
    if action == "deleted":
        session.execute(delete(InstallationRepo).where(InstallationRepo.installation_id == iid))
        session.commit()
        return f"installation {iid} removed from cache"
    repos = [r["full_name"] for r in payload.get("repositories") or []]
    _upsert_repos(session, iid, repos)
    session.commit()
    return f"installation {iid}: cached {len(repos)} repos"


def _handle_installation_repositories(session: Session, payload: dict) -> str | None:
    """Repos added to / removed from an existing installation."""
    iid = (payload.get("installation") or {}).get("id")
    if iid is None:
        return None
    added = [r["full_name"] for r in payload.get("repositories_added") or []]
    removed = [r["full_name"] for r in payload.get("repositories_removed") or []]
    _upsert_repos(session, iid, added)
    if removed:
        session.execute(
            delete(InstallationRepo).where(InstallationRepo.repo_full_name.in_(removed))
        )
    session.commit()
    return f"installation {iid}: +{len(added)} -{len(removed)} repos"
