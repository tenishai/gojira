---
card: arch-card:gojira-provisioner-steps
updated: 2026-09-28
anchor: gojira/provisioner.py sha=783b5de9459d28805ee562a2dcce2e69f14d97f7
anchor: gojira/steps.py sha=abc3da9be80359c19d0873e0203727af8b72aa8d
---
# provisioner-steps — the idempotent scoped step engine (Deploy loop)

WHAT / SEAM: `Step` Protocol (provisioner.py) — `name: str`,
`check(ctx) -> str | None` (resource ref if it exists), `apply(ctx) -> str`
(create it, return its ref). `ProvisionEngine(steps).run(session, ctx,
scope_key=None) -> RunResult` walks one step list, recording every outcome in
the `provision_steps` ledger. `provision_app(session, app, *, gcp, github,
kubectl_for, config, workdir)` (provisioner.py:195) is the orchestrator: it
derives scopes from the app and drives the engine across them.
OPS / FLOW: `provision_app` runs four engine passes, each over its own
`ProvisionContext` (one scope field set per pass): (1) per-project identity —
once per distinct GCP project (`steps.per_project_steps()`: APIs, SA, WIF
pool/provider, repo binding, IAM roles, Artifact Registry); (2) per-env
cluster setup (`per_env_steps()`); (3) per-app repo flow — once, unscoped
(`per_app_steps()`: generator render, commit, setup PR); (4) per-env observe
— AFTER (3), since it needs the contract in the working tree
(`observe_steps()`, see observe-argus card). Ledger rows key as
`{step.name}@{scope_key}`, so one step name run for several
projects/envs stays distinct. Passes (1)-(3) halt on the first FAILED step
(app -> FAILED); pass (4) never halts the run.
INVARIANTS:
- steps must be idempotent: the engine trusts `check()`'s "already exists"
  completely and never re-verifies `apply()`'s ref afterward.
- a DONE/SKIPPED ledger row is skipped on re-run UNLESS the step sets
  `ephemeral = True` (provisioner.py:76-82) — for steps whose "resource"
  doesn't outlive one run. Wrong either way: skip a step that must re-run
  (observe), or re-run a step that should be a one-time cloud mutation.
- `ProvisionEngine.__init__` raises on duplicate step names in one sequence
  (provisioner.py:113-115) — ledger keys would otherwise collide.
- promotion order is deterministic (`order_environments`, config's env order
  then `Environment.id`) — the LAST env crane-copies its predecessor's image
  rather than rebuilding (steps.py:8-12; this REVISES docs/design.md's
  original promotion model — trust steps.py, not the design doc).
- environments are validated non-empty BEFORE any state mutation
  (provisioner.py:213-217) — an app with none must never appear to have
  started a run.
- `gojira.steps` is imported lazily inside `provision_app`
  (provisioner.py:211,266) to avoid a provisioner<->steps import cycle.
CONSUMERS: gojira/service.py drives `provision_app` from a background thread
behind the UI's "Provision" button; web/__init__.py's `/apps/{id}/provision`
and `/api/v1/apps/{id}/provision` routes trigger it through service.py.
API: `Step`, `ProvisionContext`, `ProvisionEngine`, `RunResult`,
`provision_app`, `order_environments`, `distinct_projects`
(provisioner.py); concrete steps and `per_project_steps()` / `per_env_steps()`
/ `per_app_steps()` / `observe_steps()` (steps.py).
