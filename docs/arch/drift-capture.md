---
card: arch-card:gojira-drift-capture
updated: 2026-10-07
anchor: gojira/drift.py sha=250e8b2f517e1aaca2e5265150fdf729d97df01d
anchor: gojira/drift_scan.py sha=2b0441a04c2752624d3a465d54ab47b44aa6bd10
---
# drift-capture — allowlist-projection semantic diff (Capture loop)

WHAT / SEAM: `detect(desired_objs, live) -> list[ResourceDrift]`
compares `kustomize build` output against live `kubectl get -o json`, but
ONLY over an allowlist projection — `project(manifest, *, hpa_managed)`
(drift.py) extracts just the fields Gojira manages (container resources,
readiness/liveness probes, replicas), never a raw diff. `scan_env`
(drift_scan.py) is the orchestration: clone, render desired, fetch live,
`detect()`, upsert a `DriftFinding`, author a capture PR.
OPS / FLOW: `scan_env(session, app, env, github, kubectl, workdir,
render_desired=None)` — `render_desired` defaults to `kubectl kustomize`,
injectable for tests — clones the repo, renders the overlay, fetches live
state per `(kind, name)` in `MANAGED_KINDS` (today: only `Deployment`),
`detect()`s, upserts `DriftFinding`. If drift exists and no PR is open,
`_author_capture_pr` writes one `capture_patch()` strategic-merge file per
drifted object, wires each into `kustomization.yaml`'s `patches:`, commits,
pushes to `gojira/drift-<env>`, opens a PR. `service.start_drift_scanner`
(opt-in, default 900s) calls `scan_env` per (app, env) on a schedule.
INVARIANTS (docs/design.md: "The cluster is authoritative during operations"):
- image tags are NEVER compared (CI sets them at deploy time); replicas under
  an HPA are NEVER compared (`hpa_targets()`) — comparing
  either would make every deploy or every HPA move look like drift.
  `hpa_managed` must be threaded consistently through both `project()` calls
  in `detect()` AND `capture_patch()`, or a patch could re-introduce a field
  the autoscaler then fights.
- only allowlisted fields can ever produce a finding — defaults, `status`,
  `managedFields` are structurally excluded, not filtered after.
- `normalize_quantity()` makes `"1"` == `"1000m"` cpu and
  `"1Gi"` == `"1073741824"` memory comparable, or equivalent notations would
  show as false drift.
- a desired object with no live counterpart IS reported as drift (an
  operator-deleted managed resource) — but nothing live means
  `_author_capture_pr` skips writing a patch for it rather than failing.
- an already-`PR_OPENED` finding is never re-authored; `existing_pr()` is
  also checked first — fresh drift refreshes `diff_json` only.
- one clone per (app, env) scan — sharing a working tree across envs' branches
  would cross-contaminate patches.
CONSUMERS: gojira/service.py's drift scheduler calls `scan_env` on a timer;
web/__init__.py's `/apps/{id}/scan-drift` route triggers an ad hoc scan.
API: `detect`, `project`, `diff_projection`, `normalize_quantity`,
`capture_patch`, `hpa_targets`, `FieldDiff`, `ResourceDrift`, `MANAGED_KINDS`
(drift.py, pure); `scan_env`, `drifts_to_json` (drift_scan.py, I/O-bound).
