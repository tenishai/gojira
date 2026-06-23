"""Drift scan orchestration: render desired, read live, record, PR on drift.

Pure comparison lives in `drift`; cloud I/O lives in the clients. This module
is the seam that wires them per (app, environment) and turns a finding into a
capture PR. The scheduler ([service.start_drift_scanner]) calls `scan_env`.

One clone per (app, env): each env's capture PR is its own branch, and sharing
one working tree across branches would cross-contaminate patches. Drift scans
run on a schedule, not a hot path, so the extra clone is fine.
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from gojira.drift import (
    MANAGED_KINDS,
    ResourceDrift,
    capture_patch,
    detect,
    hpa_targets,
)
from gojira.github import GitHubClient
from gojira.k8s import KubectlClient, kustomize_build
from gojira.models import App, DriftFinding, DriftStatus, Environment

log = logging.getLogger(__name__)

# render_desired(overlay_dir) -> list of desired objects. Injectable for tests.
RenderDesired = Callable[[Path], list[dict]]


def _overlay_dir(workdir: Path, env_name: str) -> Path:
    return workdir / "k8s" / "overlays" / env_name


def drifts_to_json(drifts: list[ResourceDrift]) -> str:
    return json.dumps(
        [
            {
                "kind": d.kind,
                "name": d.name,
                "fields": [{"path": f.path, "desired": f.desired, "live": f.live} for f in d.fields],
            }
            for d in drifts
        ]
    )


def scan_env(
    session: Session,
    app: App,
    env: Environment,
    github: GitHubClient,
    kubectl: KubectlClient,
    workdir: Path,
    *,
    render_desired: RenderDesired | None = None,
) -> DriftFinding:
    """Scan one environment, upsert its finding, open a capture PR on drift.

    `kubectl` is already bound to env's cluster. `workdir` is a fresh tree to
    clone into. `render_desired` defaults to `kubectl kustomize`; tests inject.
    """
    render_desired = render_desired or (lambda overlay: kustomize_build(overlay))
    github.clone(app.repo_full_name, workdir)
    desired = render_desired(_overlay_dir(workdir, env.name))

    live: dict[tuple[str, str], dict | None] = {}
    for obj in desired:
        kind, name = obj.get("kind"), obj.get("metadata", {}).get("name")
        if kind in MANAGED_KINDS and name:
            live[(kind, name)] = kubectl.get_resource(env.namespace, kind, name)

    drifts = detect(desired, live)
    finding = _upsert_finding(session, app, env, drifts)

    if drifts and finding.status != DriftStatus.PR_OPENED:
        branch = f"gojira/drift-{env.name}"
        existing = github.existing_pr(app.repo_full_name, branch)
        url = existing or _author_capture_pr(
            github, app, env, workdir, desired, live, drifts, branch
        )
        if url:
            finding.status = DriftStatus.PR_OPENED
            finding.pr_url = url

    session.commit()
    log.info("drift scan %s/%s: %d drifted, status=%s", app.name, env.name, len(drifts), finding.status)
    return finding


def _upsert_finding(
    session: Session, app: App, env: Environment, drifts: list[ResourceDrift]
) -> DriftFinding:
    finding = session.scalar(
        select(DriftFinding).where(
            DriftFinding.app_id == app.id, DriftFinding.environment == env.name
        )
    )
    if finding is None:
        finding = DriftFinding(app_id=app.id, environment=env.name)
        session.add(finding)

    if not drifts:
        finding.status = DriftStatus.RESOLVED
        finding.diff_json = None
        finding.pr_url = None
        return finding

    finding.diff_json = drifts_to_json(drifts)
    # An already-open PR keeps PR_OPENED (don't re-author); fresh drift is OPEN.
    if finding.status != DriftStatus.PR_OPENED:
        finding.status = DriftStatus.OPEN
    return finding


def _author_capture_pr(
    github: GitHubClient,
    app: App,
    env: Environment,
    workdir: Path,
    desired: list[dict],
    live: dict[tuple[str, str], dict | None],
    drifts: list[ResourceDrift],
    branch: str,
) -> str | None:
    """Write capture patches + wire them into the overlay, commit, push, PR.

    Returns the PR URL, or None if nothing capturable (e.g. only deletions).
    """
    hpa = hpa_targets(desired)
    overlay = _overlay_dir(workdir, env.name)
    github.checkout_branch(workdir, branch)

    patch_files: list[str] = []
    for d in drifts:
        live_obj = live.get((d.kind, d.name))
        if live_obj is None:
            # A managed resource was deleted in-cluster — record it (the finding
            # carries the diff), but there's nothing live to capture into a patch.
            continue
        fname = f"drift-{d.name}.yaml"
        (overlay / fname).write_text(capture_patch(live_obj, hpa_managed=d.name in hpa))
        patch_files.append(fname)

    if not patch_files:
        return None

    _wire_patches(overlay / "kustomization.yaml", patch_files)
    github.commit_all(workdir, f"Capture cluster drift on {env.name}")
    github.push(workdir, branch)
    return github.open_pr(
        app.repo_full_name,
        head=branch,
        base=github.default_branch(app.repo_full_name),
        title=f"Capture cluster drift: {app.name}/{env.name}",
        body=_pr_body(app, env, drifts),
    )


def _wire_patches(kustomization: Path, patch_files: list[str]) -> None:
    """Ensure the overlay's kustomization references each capture patch once."""
    doc = yaml.safe_load(kustomization.read_text()) if kustomization.exists() else {}
    patches = doc.setdefault("patches", [])
    have = {p.get("path") for p in patches if isinstance(p, dict)}
    for fname in patch_files:
        if fname not in have:
            patches.append({"path": fname})
    kustomization.write_text(yaml.safe_dump(doc, sort_keys=False))


def _pr_body(app: App, env: Environment, drifts: list[ResourceDrift]) -> str:
    lines = [
        f"Gojira detected drift between the live `{env.name}` cluster and git.",
        "",
        "The cluster is authoritative during operations — this PR catches git up "
        "to what's running, rather than reverting it. Review and merge to keep them aligned.",
        "",
        "## Drifted fields",
    ]
    for d in drifts:
        lines.append(f"- **{d.kind}/{d.name}**")
        for f in d.fields:
            desired = "absent" if f.desired is None else f"`{f.desired}`"
            live = "absent" if f.live is None else f"`{f.live}`"
            lines.append(f"  - `{f.path}`: git {desired} → cluster {live}")
    return "\n".join(lines)
