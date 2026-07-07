# Increment 1 — Task Breakdown

**Status:** Planning — for review
**Date:** 2026-06-23
**Parent:** [sizing-feedback-design.md](sizing-feedback-design.md)

Goal (the DoD): *an over-provisioned app on a managed cluster produces a correct
`gojira.yaml` override PR with a measured justification, within one CronJob
interval, never for noise below threshold.*

No techne, no cabal. Two deliverables plus the glue between them:

- **A — gojira core** (this repo): ingest → store → recommend → PR → show.
- **B — `gojira-sizing-agent`** (new repo): the in-cluster reporter.
- **C — provisioning glue** (this repo): deploy B into a cluster, mint its token.

Ordering follows the house rule — **data model → API → logic → UI**, tests
alongside each.

---

## Task 0 — Freeze the wire contract (gate for A and B)

Both tracks hang off one JSON shape. Freeze it before either side builds far.

- Write `UsageProfile` as a pydantic model in core
  (`gojira/sizing/profile.py`) — the schema from the design doc (app, env,
  cluster, window_days, samples, cpu{request,limit,p50,p95,p99,max},
  memory{…}, restarts, oom_kills, reported_by, reported_at).
- Commit a golden fixture `tests/fixtures/usage_profile.json`. Both A's
  ingestion test and B's distiller test assert against it — that's the contract
  test, no shared package needed. The agent re-declares its own model; the
  fixture keeps them honest.
- **Done when:** the fixture validates against the core model and is referenced
  by a test on each side.

---

## Track A — gojira core

### A1. Data model
- Add `UsageProfileRow` + `SizingRecommendation` to
  [`gojira/models.py`](../gojira/models.py), matching the design's two-table
  shape (raw evidence ledger + derived proposals). Append-only ledger style,
  consistent with `events`/`provision_steps`.
- Add the per-cluster agent token: a column or small table holding a hash of the
  token Gojira mints when it deploys an agent (Task C2).
- Alembic/`db.py` migration so existing DBs upgrade cleanly.
- **Done when:** `create_all` + migration produce the tables; a round-trip test
  inserts and reads a profile and a recommendation.

### A2. Ingestion endpoint
- `POST /api/v1/usage` in [`gojira/web/__init__.py`](../gojira/web/__init__.py),
  next to the existing webhook route.
- Bearer-token auth: resolve the cluster by token hash, reject unknown/mismatched
  (a token for cluster X can only report apps on X).
- Validate body → resolve `app_id` by (app, environment, cluster) → append a
  `usage_profiles` row → trigger recommendation recompute (A4).
- Thin handler; logic in the service layer (`gojira/sizing/service.py`),
  mirroring how `web` calls into [`service.py`](../gojira/service.py) and never
  imports FastAPI itself.
- **Done when:** a posted golden profile lands a row; bad token → 401; unknown
  app → 404; malformed body → 422. Tests with a fake session.

### A3. `SizingStore` interface + DB backend
- `SizingStore` ABC (`record`, `history`) in `gojira/sizing/store.py`, same
  ABC-behind-interface pattern as [`KubectlClient`](../gojira/k8s.py#L21).
- `DbSizingStore` — writes/reads the A1 tables. The only backend in increment 1.
- **Done when:** unit tests cover record + history-by-window against a fake/real
  session; interface has a fake for downstream tests.

### A4. `PercentileRecommender`
- `Recommender` ABC (`recommend(app, env, store) -> list[Recommendation]`) +
  `PercentileRecommender` in `gojira/sizing/recommender.py`.
- Baseline comes from [`opinions.load_sizing()`](../gojira/opinions/__init__.py)
  (current class request/limit) vs the app's `gojira.yaml` override if present.
- Rule: request ≈ p95, limit ≈ max(p99 × 1.5, current); propose only past the
  **materiality threshold** (default 30%); **suppress memory downsizing** when
  `oom_kills > 0` or restarts elevated; one open recommendation per (app, env).
- Threshold/window/half-life-style knobs in a new `SizingConfig` block in
  [`config.py`](../gojira/config.py) — no hardcoded values.
- **Done when:** table-driven unit tests: over-provisioned → downsize proposal;
  within threshold → nothing; OOM history → no downsize; idempotent re-runs
  supersede rather than stack.

### A5. Override-PR generation
- A renderer that produces the updated `gojira.yaml` `overrides` block (with the
  generated `justification`) — mirror the pure `spec → {path: content}` shape of
  [`generator.render()`](../gojira/generator/__init__.py); reuse the existing
  GitHub-App branch/commit/PR path from the provisioner.
- Edits the app's existing `gojira.yaml`, doesn't regenerate the whole tree.
- One open sizing PR per app at a time (supersede/update, don't stack).
- **Done when:** a **golden-file test** asserts the rendered `gojira.yaml` diff
  for a known recommendation (the design already golden-tests generator output);
  a service test opens a PR via a fake GitHub client and records `pr_url`.

### A6. UI panel (last)
- On the app page, a "Sizing" panel: current vs observed (p95/p99) vs
  recommended per resource, the open recommendation + its PR link, last
  `reported_at`. Reads the A1 ledger, htmx-polled like the existing status views.
- **Done when:** panel renders from seeded data; shows "no data yet" cleanly
  before the first report.

---

## Track B — `gojira-sizing-agent` (new repo)

Parallelizable with A once Task 0 lands. Built on limenal so it's a real agent,
not a curl-in-cron — but increment 1 needs no cabal and no central anything.

### B0. Repo scaffold
- `git-repo-setup` (private, branch protection), `pyproject.toml` depending on
  `limenal`, `Makefile` (`build`/`run`/`test`), `Dockerfile`. House rule: local
  `make build` before any CI.

### B1. `metrics` limenal handler
- Implement the 3-method handler (`tools` / `capability_card` / `dispatch`)
  exposing `query_range` / `query_instant` against Prometheus. Prometheus URL
  from `ctx.credentials` / `ctx.extra` (per-cluster, no `.env`).
- Register via the `limenal.handlers` entry point.
- **Done when:** unit tests dispatch against a mocked Prometheus; missing URL →
  graceful `ToolResult.failure`.

### B2. `kube` limenal handler
- Read-only: live container requests/limits, restart counts, recent OOM-kill
  events for managed apps in the cluster (scoped `kubectl get`).
- **Done when:** unit tests against fake kubectl output; read-only enforced.

### B3. Distiller + report
- The agent loop: for each managed app/env, pull a window of cpu/mem from `metrics`,
  current spec + restart/OOM from `kube`, compute p50/p95/p99/max, build a
  `UsageProfile`, POST to Gojira via the built-in `notify` (webhook) handler.
- **Done when:** given recorded Prometheus/kube fixtures, it emits a
  `UsageProfile` byte-equal to the Task 0 golden fixture; a dry-run mode logs
  instead of POSTing.

### B4. Packaging
- CronJob manifest (schedule = the window cadence), worker-class resources (the
  agent sizes itself honestly — dogfood Gojira's classes), config via env/Secret,
  read-only RBAC.
- **Done when:** `make build` produces the image; the CronJob applies and runs
  end-to-end against a test cluster, landing a profile in a dev Gojira.

---

## Track C — provisioning glue (this repo)

Depends on B's image and A's token model. Phase 2's drift detector shipped
central, so C1 is Gojira's *first* deploy-a-workload-into-a-cluster step — it
establishes the pattern, rather than reusing one.

### C1. Agent-deploy provisioner step
- A `Step` in [`steps.py`](../gojira/steps.py) (idempotent, ledgered like the
  rest): apply the agent CronJob + read-only RBAC into the cluster's namespace,
  parameterized with the cluster's Prometheus URL and Gojira's `/api/v1/usage`
  endpoint.
- **Done when:** running it against a registered cluster lands the CronJob;
  re-run is a no-op (skipped); recorded in `provision_steps`.

### C2. Token mint + inject
- On agent deploy, mint a per-cluster token, store its hash (A1), inject the
  plaintext as a K8s Secret the agent reads. Narrow scope: report usage for apps
  on this cluster only.
- **Done when:** the agent authenticates to `/api/v1/usage` with the injected
  token; a wrong/rotated token is rejected.

### C3. End-to-end check
- The `verify`-style pass: deploy agent → it reports → recommendation appears →
  PR opens → UI shows it. The DoD, demonstrated.
- **Done when:** the full loop runs against a test cluster with one deliberately
  over-provisioned app and produces the expected override PR; a near-threshold
  app produces none.

---

## Suggested sequence

1. **Task 0** (contract) — unblocks both tracks.
2. **A1 → A2 → A3 → A4** (data → ingest → store → recommend) in core, **in
   parallel with B0 → B1 → B2 → B3** in the agent repo.
3. **A5** (PR gen) once A4 lands; **B4** (packaging) once B3 lands.
4. **C1 → C2 → C3** (glue + token + e2e) once A2/A5 and B4 are ready.
5. **A6** (UI) last — it only reads what the rest produced.

Critical path to the DoD runs through A1–A5 + C; the UI (A6) and the niceties of
B (real Prometheus tuning) can trail.

## Deliberately deferred (the decision point)

`TechneSizingStore`, the cabal `AgentRecommender`, and class recalibration are
**not** in this breakdown — they're gated on reading increment 1's output, per
the parent design's "Decision point" section.
