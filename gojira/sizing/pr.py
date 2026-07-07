"""Override-PR authoring: a live recommendation becomes a gojira.yaml PR.

gojira.yaml is the user's contract file — its comments are documentation —
so the overrides block is edited textually (append, or replace an existing
block) rather than YAML-round-tripped, which would strip every comment.
The result is still validated by parsing before it's committed.

One open sizing PR per (app, env): a fixed branch name plus the existing-PR
check give supersede-don't-stack for free, same pattern as drift capture.
"""

import json
import logging
import re
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from gojira.generator import apply_resource_overrides
from gojira.github import GitHubClient
from gojira.models import App, RecommendationStatus, SizingRecommendation

log = logging.getLogger(__name__)

# An uncommented top-level `overrides:` block: the key line plus every
# following indented (or blank) line, up to the next top-level key.
_OVERRIDES_BLOCK = re.compile(r"^overrides:\n(?:[ \t].*\n|\n)*", re.MULTILINE)


def render_overrides_block(proposed: dict, justification: str) -> str:
    """The overrides block for gojira.yaml from a recommendation's proposal.

    `proposed` is the recommendation's proposed_json shape:
    {"cpu": {"request": "800m", "limit": "1250m"}, ...}
    """
    requests = {res: v["request"] for res, v in proposed.items()}
    limits = {res: v["limit"] for res, v in proposed.items()}

    def inline(d: dict) -> str:
        # Quantities are always quoted: a bare `1` would parse as an int,
        # and "1" (one core) is a string in the k8s quantity dialect.
        return "{ " + ", ".join(f'{k}: "{v}"' for k, v in sorted(d.items())) + " }"

    return (
        "overrides:\n"
        f"  requests: {inline(requests)}\n"
        f"  limits: {inline(limits)}\n"
        f"  justification: {json.dumps(justification)}\n"
    )


def apply_overrides(gojira_yaml_text: str, block: str) -> str:
    """Replace the existing overrides block or append one. Comments survive."""
    if _OVERRIDES_BLOCK.search(gojira_yaml_text):
        updated = _OVERRIDES_BLOCK.sub(block, gojira_yaml_text, count=1)
    else:
        sep = "" if gojira_yaml_text.endswith("\n") else "\n"
        updated = f"{gojira_yaml_text}{sep}{block}"

    parsed = yaml.safe_load(updated)  # belt and braces: still valid YAML?
    if "overrides" not in parsed:
        raise ValueError("overrides block failed to apply")
    return updated


def open_override_pr(
    session: Session,
    app: App,
    rec: SizingRecommendation,
    github: GitHubClient,
    workdir: Path,
) -> str | None:
    """Author the sizing PR for a live recommendation. Returns the PR URL,
    or None if there was nothing to do (no OPEN rec, or no content change)."""
    if rec.status != RecommendationStatus.OPEN:
        return rec.pr_url

    branch = f"gojira/sizing-{rec.environment}"
    existing = github.existing_pr(app.repo_full_name, branch)
    if existing:
        rec.status = RecommendationStatus.PR_OPENED
        rec.pr_url = existing
        session.commit()
        return existing

    github.clone(app.repo_full_name, workdir)
    github.checkout_branch(workdir, branch)

    spec_path = workdir / "gojira.yaml"
    if not spec_path.exists():
        log.warning("no gojira.yaml in %s — app not fully onboarded, skipping PR",
                    app.repo_full_name)
        return None

    proposed = json.loads(rec.proposed_json)
    block = render_overrides_block(proposed, rec.justification)
    spec_path.write_text(apply_overrides(spec_path.read_text(), block))

    # The manifest moves in the same PR as the contract — merging it is what
    # actually resizes the workload (CI applies k8s/, not gojira.yaml).
    deployment = workdir / "k8s" / "base" / "deployment.yaml"
    if deployment.exists():
        requests = {res: v["request"] for res, v in proposed.items()}
        limits = {res: v["limit"] for res, v in proposed.items()}
        deployment.write_text(
            apply_resource_overrides(deployment.read_text(), requests, limits)
        )

    # Finding #9: a drift-capture patch in the env overlay layers OVER base at
    # kustomize build time — left alone, it silently re-imposes the captured
    # values and the merged sizing PR changes nothing on the cluster. The
    # sizing decision is the later, reviewed one: retire the fields it
    # supersedes from the patch (other captured fields, e.g. probes or
    # ephemeral-storage, stay).
    retire_superseded_drift(
        workdir / "k8s" / "overlays" / rec.environment, app.name, set(proposed)
    )

    if not github.commit_all(workdir, f"Rightsize {app.name} ({rec.environment}): measured evidence"):
        log.info("override for %s produced no diff — already applied", app.name)
        return None
    github.push(workdir, branch)

    url = github.open_pr(
        app.repo_full_name,
        head=branch,
        base=github.default_branch(app.repo_full_name),
        title=f"Rightsize {app.name}: {_summary(proposed)}",
        body=_pr_body(app, rec, proposed),
    )
    rec.status = RecommendationStatus.PR_OPENED
    rec.pr_url = url
    session.commit()
    log.info("sizing PR opened for %s/%s: %s", app.name, rec.environment, url)
    return url


def retire_superseded_drift(overlay_dir: Path, app_name: str, resources: set[str]) -> bool:
    """Remove `resources` (e.g. {"cpu","memory"}) from a drift-capture patch.

    If the patch's requests/limits become empty they're dropped; if the whole
    patch then says nothing beyond container names, the file is deleted and
    its kustomization wiring removed. Returns True if anything changed.
    """
    patch_path = overlay_dir / f"drift-{app_name}.yaml"
    if not patch_path.exists():
        return False
    try:
        doc = yaml.safe_load(patch_path.read_text())
    except yaml.YAMLError:
        return False

    containers = (
        doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    )
    changed = False
    for c in containers:
        res = c.get("resources", {})
        for bucket in ("requests", "limits"):
            for key in resources:
                if key in res.get(bucket, {}):
                    del res[bucket][key]
                    changed = True
            if bucket in res and not res[bucket]:
                del res[bucket]
        if "resources" in c and not c["resources"]:
            del c["resources"]
    if not changed:
        return False

    # Anything left worth keeping? (probes, ephemeral-storage, ...)
    substantial = any(set(c) - {"name"} for c in containers)
    if substantial:
        header = "# Captured by Gojira's drift detector — the live cluster state.\n"
        patch_path.write_text(header + yaml.safe_dump(doc, sort_keys=False))
        log.info("retired %s from drift patch %s", sorted(resources), patch_path.name)
        return True

    patch_path.unlink()
    kust_path = overlay_dir / "kustomization.yaml"
    if kust_path.exists():
        # Textual unwire, so the kustomization's comments survive.
        lines = [
            line for line in kust_path.read_text().splitlines(keepends=True)
            if f"path: {patch_path.name}" not in line
        ]
        text = "".join(lines)
        parsed = yaml.safe_load(text) or {}
        if not parsed.get("patches"):
            lines = [line for line in lines if line.strip() != "patches:"]
        kust_path.write_text("".join(lines))
    log.info("drift patch %s fully superseded — removed", patch_path.name)
    return True


def _summary(proposed: dict) -> str:
    return ", ".join(f"{res} → {v['request']}" for res, v in sorted(proposed.items()))


def _pr_body(app: App, rec: SizingRecommendation, proposed: dict) -> str:
    current = json.loads(rec.current_json)
    lines = [
        f"Gojira measured `{app.name}`'s real usage on `{rec.environment}` and "
        "proposes rightsizing its `gojira.yaml` overrides.",
        "",
        "| resource | request (now → proposed) | limit (now → proposed) |",
        "|---|---|---|",
    ]
    for res in sorted(proposed):
        cur, new = current.get(res, {}), proposed[res]
        lines.append(
            f"| {res} | `{cur.get('request', '?')}` → `{new['request']}` "
            f"| `{cur.get('limit', '?')}` → `{new['limit']}` |"
        )
    lines += [
        "",
        f"**Evidence:** {rec.justification}",
        "",
        "This PR updates both the contract (`gojira.yaml`) and the manifest "
        "(`k8s/base/deployment.yaml`) — merging it deploys the new sizing on the "
        "next CI run. Close it to dismiss the recommendation.",
    ]
    return "\n".join(lines)
