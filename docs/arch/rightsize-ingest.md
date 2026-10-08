---
card: arch-card:gojira-rightsize-ingest
updated: 2026-10-07
anchor: gojira/sizing/service.py sha=f7c4ead763f6371db967e9b88a58bee4484b7533
anchor: gojira/sizing/recommender.py sha=451b9a415055c9c9f0d5ddcca0f9da5402bfa8f7
anchor: gojira/sizing/profile.py sha=d56d01b67d9b5f47810d9561ed17ae3f49b7418e
---
# rightsize-ingest — usage evidence in, sizing PRs out (Rightsize loop)

WHAT / SEAM: `POST /api/v1/usage` accepts a `UsageProfile` (sizing/profile.py)
from an in-cluster agent bearing a cluster-scoped token. `ingest_usage()`
(sizing/service.py) authenticates, appends to the evidence ledger,
recomputes. `PercentileRecommender` (sizing/recommender.py) turns evidence
into a `Proposal`. This is Gojira's side of the cross-repo contract owned by
tenishai/gojira-sizing-agent's `usage-profile-contract` card (wire-format
invariants live there).
OPS / FLOW: bearer token -> `_sha256` -> `AgentToken` -> `Cluster` -> scoped
`App`+`Environment` lookup -> `DbSizingStore.record()` (append-only
`UsageProfileRow`) -> `recompute_for_app()` -> `PercentileRecommender.
recommend()` over `store.history()` -> `upsert_recommendation()` writes the
one live `OVERRIDE` row. Rule (recommender.py module docstring): `request <- p95`
(rounded to 50m cpu / 64Mi mem steps), `limit <- max(p99 * limit_headroom,
request)` (default headroom 1.5). UI's "open PR" -> `open_pr_for()` ->
`sizing.pr.open_override_pr()`. `usage_history()` (service.py) is the
read twin behind `GET /api/v1/apps/{id}/usage`.
INVARIANTS:
- a cluster's token can only ingest for apps with an Environment on THAT
  cluster (`ingest_usage`) — Cluster resolves from the token first, then
  Environment is scoped to it; a compromised sandbox token can't feed prod.
- only the token's SHA-256 is stored; `mint_agent_token` rotates in place,
  killing the old hash the instant a new one is minted (`mint_agent_token`).
- one live `SizingRecommendation` per (app, env, OVERRIDE) —
  `upsert_recommendation` updates in place, never stacks (`SizingRecommendation`'s `uq_sizing_app_env_kind`).
- materiality guardrail: no proposal unless the move is >= `materiality_pct`
  (default 30%, `PercentileRecommender._propose`).
- evidence-over-wishes: memory is NEVER proposed down for an app with
  `oom_kills > 0` or `restarts > 0` in the window, even if materiality is met
  (`_propose`).
- a `PR_OPENED` recommendation keeps that status on new evidence — recompute
  refreshes the payload but never invalidates a PR under human review
  (`upsert_recommendation`).
- `usage_history()` lists every environment even with zero evidence
  (`usage_history`) — "no data yet" is an explicit empty list, not a
  missing key.
CONSUMERS: tenishai/gojira-sizing-agent (sole producer). gojira/sizing/
agent_deploy.py deploys that repo's manifests and mints its token in one
idempotent call. Routes: `POST /api/v1/usage`, `GET /api/v1/apps/{id}/usage`,
`POST /apps/{id}/sizing/{env}/open-pr` (web/__init__.py).
API: `UsageProfile`, `ResourceUsage` (profile.py); `ingest_usage`,
`usage_history`, `mint_agent_token`, `open_pr_for`, `IngestError`
(service.py); `Recommender`, `PercentileRecommender`, `recompute_for_app`,
`upsert_recommendation` (recommender.py).
