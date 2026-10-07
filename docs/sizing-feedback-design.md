# Gojira — Sizing Feedback Loop (Phase 4 design)

**Status:** Proposal — for review
**Date:** 2026-06-23
**Authors:** Shaz + Ember (SRE AI)
**License:** AGPL-3.0-or-later

> **Design record.** Written in June 2026 to decide what to build, and kept as the
> record of why. Parts may no longer match the code; the [README](../README.md)
> and [install guide](install.md) describe Gojira as it is now.
> Ember (SRE AI) is the AI coding agent Shaz pairs with; see "How this was built"
> in the README.

> Extends [design.md](design.md). Implements the Phase 4 "Adapt" row —
> per-service thermostat / rightsizing from Prometheus — as a **closed feedback
> loop**: agents in the clusters Gojira deploys to observe real usage, report
> back, and Gojira refines per-app requests/limits *and* recalibrates the
> workload-class defaults that every future app inherits.

---

## The opinion this closes

Gojira's central claim is that workload classes are "calibrated against real
production behavior — not guesses copied from a blog post" ([README](../README.md)).
Today that calibration is a one-time table baked into
[`opinions/sizing.yaml`](../gojira/opinions/sizing.yaml). Nothing keeps it true
as workloads drift.

This design makes the calibration *live*. Gojira already wires Prometheus into
every app from its first deploy (design.md:87) — the signal exists. We add the
loop that consumes it.

Two outputs, increasing leverage:

1. **Per-app override** — clips-api's measured p99 CPU is 2× its `web`-class
   request → a `gojira.yaml` override PR with a measured `justification`. The
   `justification` string the design already calls for ("p99 CPU is 800m
   measured over 14 days", design.md:123) is *generated from evidence*, not
   hand-typed.
2. **Class recalibration** — when the `web` class consistently over-provisions
   across many apps, the aggregated evidence lowers the `web` default in
   `sizing.yaml`. Every *future* app inherits the calibrated number. This is the
   "use them for future apps" goal and the highest-leverage output.

---

## Scope boundary — what this is *not*

Cluster discovery and YAML-finding are sometimes lumped in with this. They are
not part of this loop and do not need an agent stack:

- **Cluster discovery** = a GCP container-API enumeration; clusters are already
  data Gojira registers ([models.py:39](../gojira/models.py#L39)). A future
  `discover_clusters` provisioner step is a `gcloud container clusters list`, not
  an agent.
- **Finding live YAML** = `kubectl get -o yaml`, already the province of
  [`KubectlClient`](../gojira/k8s.py) and the Phase 2 drift detector.

This document is only the **usage → rightsizing** loop.

---

## Design principle — the OSS core stays dependency-clean

Gojira-the-tool must not take a hard dependency on a specific agent runtime.
The design already sets this precedent: it "defines a `TopologyProvider`
interface and ships without a default implementation" (design.md:246).

We follow the same pattern. **Gojira core defines interfaces and an ingestion
endpoint. The intelligent agent is one pluggable implementation, shipped as a
separate package** (`gojira-sizing-agent`). A Gojira operator who wants nothing
to do with the agent stack still gets a working percentile-based recommender;
an operator who wants the full fleet plugs it in.

```
        ┌──────────────── Gojira core (this repo, OSS) ────────────────┐
        │  POST /api/v1/usage   ──►   SizingStore   ──►   Recommender    │
        │  (ingestion endpoint)       (interface)        (interface)     │
        │                                 │                  │           │
        │                          default: gojira DB   default: percentile
        └─────────────────────────────────┼──────────────────┼──────────┘
                                           │                  │
              ┌────────────────────────────┘                  └───── PR generator
              │  optional plug-ins (separate packages)               (override / class)
              ▼
   techne-backed SizingStore        cabal LLMPersona Recommender
   (knowledge + decay + outbox)     (reasons about noise/patterns)
```

Three seams, each with a no-dependency default:

| Seam | Interface | Core default | Plug-in |
|---|---|---|---|
| **Ingestion** | `POST /api/v1/usage` (HTTP) | writes to `SizingStore` | — (agents are external) |
| **Storage** | `SizingStore` (ABC) | gojira DB tables | techne sizing domain store + outbox |
| **Recommendation** | `Recommender` (ABC) | percentile rule | cabal LLMPersona |

These mirror the existing `GCPClient` / `KubectlClient` / `GitHubClient` ABC
pattern ([k8s.py:21](../gojira/k8s.py#L21)) — interface in core, real backend
swappable, fakes in tests.

---

## Data flow

```
 ┌─ each managed cluster (a Gojira deploy target) ─────────────────────┐
 │  Prometheus  (Gojira wires this in — design.md:87)                   │
 │      ▲ query                                                         │
 │      │                                                               │
 │  ┌───┴──────────────── usage agent (CronJob) ──────────────────────┐ │
 │  │ reads per-app cpu/mem over a window, computes p50/p95/p99,        │ │
 │  │ compares to live requests/limits, distills a UsageProfile         │ │
 │  └───────────────┬──────────────────────────────┬──────────────────┘ │
 └──────────────────┼──────────────────────────────┼────────────────────┘
        notify (HTTP)│                              │ (increment 2: also
                     ▼                              ▼  commit to techne + outbox)
       Gojira  POST /api/v1/usage          central techne sizing knowledge
                     │                              │
                     ▼                              ▼
              SizingStore                   cross-fleet aggregation
                     │                              │
                     ▼                              ▼
            Recommender ──► per-app override PR    class recalibration PR
                                (gojira.yaml)         (opinions/sizing.yaml)
```

The agent is a **CronJob per cluster**, provisioned into the namespace Gojira
already creates, using the WIF/SA plumbing Gojira already builds. Note: Phase 2's
drift detector ended up *central* (it only needs the K8s API, which Gojira
reaches for every registered cluster — see design.md), so this agent is Gojira's
**first** in-cluster workload deployment. The sizing agent genuinely needs to run
in-cluster: its data source is each cluster's local Prometheus.

The agent **pushes** (reports back) rather than Gojira pulling. This is what you
asked for, and it means Gojira never needs a network path into each cluster's
Prometheus — the agent owns that access locally. Works across projects/VPCs.

---

## The wire contract — `UsageProfile`

What an agent POSTs to `/api/v1/usage`. Distilled, not raw samples — one small
JSON per app/env/window, not a metric firehose.

```json
{
  "app": "clips-api",
  "environment": "prod",
  "cluster": "my-prod-cluster",
  "window_days": 14,
  "samples": 20160,
  "cpu":    { "request": "250m", "limit": "500m",
              "p50": "180m", "p95": "760m", "p99": "820m", "max": "1100m" },
  "memory": { "request": "256Mi", "limit": "512Mi",
              "p50": "210Mi", "p95": "300Mi", "p99": "340Mi", "max": "402Mi" },
  "restarts": 0,
  "oom_kills": 0,
  "reported_by": "gojira-sizing-agent/my-prod-cluster",
  "reported_at": "2026-06-23T12:00:00Z"
}
```

`restarts` / `oom_kills` are guardrails: a recommendation to *lower* memory is
suppressed if the app has been OOM-killed in the window. Evidence over wishes.

---

## Core data model additions

Two tables, in keeping with the existing append-ledger style (design.md:171):

```
usage_profiles      append-only ingestion log (one row per report)
  id, app_id (fk), environment, window_days, samples,
  cpu_json, memory_json, restarts, oom_kills,
  reported_by, reported_at, received_at

sizing_recommendations   derived proposals, one open row per (app, env, kind)
  id, app_id (fk), environment, kind (override|class_recalibration),
  current_json, proposed_json, justification, confidence,
  status (open|pr_opened|merged|dismissed|stale), pr_url,
  based_on_profile_id (fk), created_at, updated_at
```

`usage_profiles` is the raw evidence ledger (cheap, append-only, prunable by
age). `sizing_recommendations` is the actionable derivative the UI shows and the
PR generator consumes — and it carries the `justification` that lands verbatim
in the generated PR.

---

## Seam 1 — ingestion endpoint (core)

A new authenticated route alongside the existing webhook surface
([web/__main__.py](../gojira/web/__main__.py)):

```
POST /api/v1/usage          # agent → Gojira; body = UsageProfile
  auth: per-cluster bearer token minted at agent-provision time
  effect: validate, resolve app_id by (app, environment, cluster),
          append usage_profiles row, enqueue recommendation recompute
```

Token model: when Gojira provisions a usage agent into a cluster it mints a
narrow token scoped to "report usage for apps on *this* cluster" and injects it
as a Secret. Symmetric with how Gojira already mints WIF/short-lived auth for
deploys — no long-lived broad credentials.

---

## Seam 2 — `SizingStore` (core interface)

```python
class SizingStore(ABC):
    """Where usage evidence accumulates. Default writes Gojira's own tables."""

    @abstractmethod
    def record(self, profile: UsageProfile) -> int: ...

    @abstractmethod
    def history(self, app: str, env: str, *, metric: str,
                since_days: int) -> list[UsageProfile]: ...
```

- **`DbSizingStore` (core default)** — writes `usage_profiles`, reads back by
  app/env/window. Zero extra dependencies.
- **`TechneSizingStore` (plug-in)** — additionally commits a distilled
  `knowledge` fact per app (`techne.knowledge.store(... type="fact",
  tags="app:clips-api metric:cpu")`) and enqueues it on the **outbox**
  (`sync:sizing`) so a fleet of clusters converges to one central techne. This
  is where techne earns its place:
  - **confidence decay** — a 30-day-old measurement *should* lose weight against
    a fresh one (`effective_confidence`, tunable half-life). Sizing evidence is
    perishable; decay models that natively.
  - **supersession** — a refined 14-day measurement supersedes the prior one
    (`knowledge.supersede`) instead of piling up duplicates.
  - **outbox/SyncAdapter** — purpose-built for many agents → one central store,
    async at idle boundaries, no central hub the agents must reach synchronously.

---

## Seam 3 — `Recommender` (core interface)

```python
class Recommender(ABC):
    @abstractmethod
    def recommend(self, app: str, env: str,
                  store: SizingStore) -> list[Recommendation]: ...
```

- **`PercentileRecommender` (core default)** — transparent rule, the kind of
  opinion Gojira is comfortable stating in the open:
  - request ≈ p95 (rounded to a sane step), limit ≈ max(p99 × 1.5, current).
  - Propose an **override** only past a materiality threshold (e.g. recommended
    differs from current by > 30%) — no PR churn over noise.
  - Suppress *downsizing* memory when `oom_kills > 0` or `restarts` elevated.
  - Emit a **class_recalibration** signal when ≥ N apps in a class drift the same
    direction by a similar margin.
- **`AgentRecommender` (plug-in, increment 2)** — a cabal `LLMPersona` that
  *reasons* over the history: discards deploy-correlated spikes, distinguishes
  bursty-but-cheap from steady-and-load-bearing, recommends HPA tuning vs request
  bumps, and writes the `justification` prose. Same interface; richer judgment.

When a recommendation crosses threshold, the existing **generator** opens a PR —
`gojira.yaml` `overrides` block for an override, `opinions/sizing.yaml` for a
class recalibration — reusing the generator + GitHub-App PR path Gojira already
has (design.md:208).

---

## The agent (the `gojira-sizing-agent` package)

Shipped separately from core. Built on the sibling stack so it's a real agent,
not a cron script that happens to curl Prometheus.

| Piece | Provided by | Role |
|---|---|---|
| **`metrics` handler** | limenal (new) | query Prometheus `query_range` / `query_instant`; ~4–6h per the handler interface (3 methods: `tools` / `capability_card` / `dispatch`). |
| **`kube` handler** | limenal (new) | read live requests/limits + restart/OOM counts (`kubectl get`, scoped read-only). |
| **`notify` handler** | limenal (built-in) | POST the `UsageProfile` to Gojira `/api/v1/usage`. Webhook transport already exists. |
| **`memory` handler** | limenal (built-in) | *later, if the techne store is justified* — commit distilled facts to techne. |
| **identity / creds** | cabal | *later, if the reasoning recommender is justified* — per-cluster persona, Prometheus URL via `cabal_tool_extras`, secrets via the credential resolver. |

Per-cluster config (Prometheus URL, Gojira endpoint, token) arrives through
limenal's per-persona `cabal_tool_extras` / `ctx.credentials` — the agent image
is identical across clusters; only the injected config differs.

**The agents are autonomous.** Increment 1 needs no central cabal core reachable
from every cluster: the agent reads Prometheus locally and POSTs to Gojira.
cabal enters only if increment 1's data justifies a reasoning recommender, and
even then primarily for identity/credential hygiene and optional on-demand
dispatch ("re-measure clips-api now") — never as a hard runtime dependency the
loop can't function without.

---

## Phasing

The seams exist precisely so we can **defer the plug-in choice until the data
demands it.** Increment 1 is the experiment; what (if anything) comes after is
decided by reading its output, not pre-committed here. We do not pull in
techne's central store or cabal's reasoning layer on spec — we earn them, or we
don't.

### Increment 1 — the reporter (standalone)

Delivers per-app rightsizing recommendations on Gojira's existing patterns, with
zero dependency on the sibling agent stack beyond the two limenal handlers the
agent itself needs.

- **Core:** `/api/v1/usage`, `usage_profiles` + `sizing_recommendations` tables,
  `DbSizingStore` (Gojira's own DB — no techne), `PercentileRecommender`,
  override-PR generation, a UI panel on the app page showing current vs observed
  vs recommended.
- **Agent package:** limenal `metrics` + `kube` handlers; CronJob manifest;
  `notify` → Gojira. A provisioner step that deploys the agent + mints its token.
  No cabal — the agent reads Prometheus locally and POSTs; nothing central to
  reach.
- **Definition of done:** an over-provisioned app on a managed cluster produces a
  correct `gojira.yaml` override PR with a measured justification, within one
  CronJob interval, never for noise below threshold.

### Decision point — read the data before building more

Increment 1 is the test of the whole premise. Once a few weeks of real profiles
have landed, look at what `PercentileRecommender` actually produced and ask:

- **Are the recommendations clean?** If a static p95/p99 rule gives sound
  proposals, *there is no reasoning layer to build* — cabal stays out. The most
  likely good outcome.
- **Is the rule fighting noise it can't see through?** Deploy-correlated spikes,
  bursty-but-cheap workloads, multi-container pods, cold-start transients. If the
  rule mis-fires in ways that need judgment rather than a better threshold, *that*
  is the evidence for an `AgentRecommender` — and only then.
- **Are stale duplicates piling up across clusters?** Once multiple clusters
  report, if the DB store is accumulating superseded evidence and you want decay
  + fleet convergence, *that* is the evidence for swapping in a
  `TechneSizingStore`. Not before.

Each candidate below is gated on one of those answers. Pick what the data
justifies; skip the rest. The interfaces make every one a swap, not a rewrite.

### Candidate: central knowledge store (techne) — *if* duplicates/staleness bite

Swap `DbSizingStore` → `TechneSizingStore`: knowledge with confidence decay,
supersession, and fleet convergence via outbox. Justified once many clusters
report and perishability/dedup actually matter — not for a single-cluster POC.

### Candidate: reasoning recommender (cabal) — *if* the percentile rule mis-fires

`AgentRecommender` as a cabal `LLMPersona`: judgment over raw percentiles,
discards deploy noise, distinguishes bursty from load-bearing, writes
justification prose, identifies class-wide drift. Brings per-cluster persona
identity + credential hygiene, and optional A2A dispatch for on-demand
investigation. Justified only if increment 1 shows the static rule isn't enough.

### Candidate: class recalibration — *once* per-app overrides are trusted

`class_recalibration` PRs against `opinions/sizing.yaml` — the "use them for
future apps" payoff. Independent of the recommender choice (the percentile rule
can detect class-wide drift too); gated on having enough trusted per-app evidence
to move a default that affects every app.

---

## Guardrails (carried across both increments)

- **Evidence over wishes** — never recommend a *downsize* against an app with
  OOM-kills or elevated restarts in the window.
- **Materiality threshold** — no PR unless the delta is worth a human's review.
- **Rate-of-change** — at most one open sizing PR per app at a time; supersede,
  don't stack. (Mirrors design.md:240 "rate-of-change guardrails".)
- **Human in the loop** — every output is a *reviewed PR*, never a silent patch.
  Consistent with "nothing silently reverts an operator's fix" (design.md:26).
- **Decay** — stale evidence loses weight automatically (techne half-life), so an
  old measurement can't justify a change forever.

---

## Open questions

1. **Recommendation cadence** — recompute on each report, or on a schedule? Each
   report is simpler; a schedule batches noise. Lean: recompute on report,
   debounce PR creation.
2. **Window length** — fixed 14d, or per-class (cron jobs need a longer window
   than a steady web service)? Probably per-class default, overridable.
3. **Class recalibration authority** — auto-open the `sizing.yaml` PR, or only
   surface the signal in the UI for an operator to trigger? The class default
   affects every app; this PR likely wants a higher bar than a per-app override.
4. **Agent footprint** — the agent must itself be sized honestly (it's a Gojira
   `worker`-class workload); dogfood it through Gojira's own classes.
5. **techne central store backing** — SQLite via outbox-to-filesystem for the
   POC, or a columnar backend (the `StoreAdapter` protocol allows swapping) once
   fleet volume grows?
