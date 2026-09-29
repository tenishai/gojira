# Gojira 🦖

**An opinionated deploy platform for GKE. It tramples your hand-rolled YAML.**

Connect a GitHub repo, pick a cluster and a workload class, and Gojira
provisions everything needed to ship: service account, Workload Identity
Federation, Artifact Registry access, GitHub Actions workflows, Kustomize
manifests, and observability wiring. Merge the setup PR — after that,
push-to-deploy just works.

Then Gojira keeps working. It watches your clusters for drift and captures
operators' live fixes back into git as PRs. It ingests real usage evidence and
opens rightsizing PRs with the measurements in the justification. Merge one,
and your pods change shape.

> **Status:** working, and aggressively dogfooded. Gojira deploys itself,
> caught its own drift, and rightsized its own demo app from 750m/768Mi down
> to a measured 50m/64Mi — live, on a real GKE Autopilot cluster. Every
> finding from that dogfooding is fixed in this repo, each documented in
> the commit that tells its story (`git log -i --grep=dogfood --grep=finding`).

## Why

Every team deploying to Kubernetes hand-rolls the same things: CI workflows,
manifests, resource specs, auth plumbing. The results are inconsistent,
over-provisioned, and expensive to change org-wide. Gojira makes those
decisions for you — visibly, with an escape hatch for justified overrides.

Every opinion is a default with an override: sizing through
`overrides.requests`/`limits`, scaling bounds and targets through
`overrides.hpa` (`min`, `max`, `cpu`, `memory` percent), the listener and
health path through `port:` and `health:`. All justified, all in `gojira.yaml`.

Three opinions define it:

1. **The platform decides sizing.** Workload classes (`web`, `worker`,
   `cron`, `heavy`, `gpu`) map to burstable resource specs — and a feedback
   loop refines them against *measured* production behavior, not guesses
   copied from a blog post.
2. **No ArgoCD.** Deploys are git → Actions → `kubectl apply`. Operational
   fixes flow the other way: cluster → drift detector → PR. Nothing silently
   reverts an operator's work.
3. **Observable from the first deploy.** Prometheus wiring is generated,
   not optional.

## The three loops

```
 DEPLOY    repo ──▶ setup PR ──▶ merge ──▶ CI (keyless WIF) ──▶ cluster
 CAPTURE   cluster drift ──▶ semantic diff ──▶ capture PR ──▶ git catches up
 RIGHTSIZE usage evidence ──▶ recommendation ──▶ one reviewed PR ──▶ pods resize
```

**Deploy** — the provisioner is an idempotent, resumable step engine: every
GCP resource it creates is ledgered, re-runs skip what exists, failures resume
exactly where they stopped, and re-onboarding an already-configured repo
concludes cleanly instead of churning. CI authenticates via Workload Identity
Federation — no long-lived keys anywhere. Promotion: merge → dev, tag → qa,
prod behind a manual approval gate.

**Capture** — a central scanner diffs each app's live cluster state against
`kustomize build`, comparing only the fields Gojira manages (an allowlist
projection — API-server defaults, image tags, and HPA-owned replicas can't
produce false positives). Real drift becomes a **capture PR** that writes the
live values into the overlay: the cluster is authoritative during operations,
and git catches up through review. It caught GKE Autopilot silently rewriting
resources on its first real scan.

**Rightsize** — agents (or anything else) POST distilled usage evidence to
`/api/v1/usage` with a cluster-scoped token. A transparent percentile rule
(request ← p95, limit ← max(p99 × 1.5, request)) turns evidence into a
recommendation — guarded by a materiality threshold and a hard rule against
memory downsizing when the app has OOM kills. One click opens **one reviewed
PR** carrying the contract (`gojira.yaml` overrides + justification), the
manifest, and the retirement of any drift patch it supersedes. Merge it and
the next CI run deploys the new size.

## The workload classes

| Class | Requests | Limits | HPA | Probes |
|---|---|---|---|---|
| `web` | 250m / 256Mi | 500m / 512Mi | 70% CPU, 2–10 | readiness + liveness HTTP `/health` |
| `worker` | 500m / 512Mi | 1 / 1Gi | — | liveness TCP |
| `agent` | 250m / 512Mi | 1 / 1Gi | — (Recreate, single consumer) | liveness TCP |
| `gateway` | 250m / 256Mi | 500m / 512Mi | — (Recreate, single replica) | readiness + liveness HTTP `/healthz` |
| `cron` | 250m / 256Mi | 1 / 1Gi | — | — |
| `heavy` | 1 / 1Gi | 2 / 2Gi | 70% CPU, 2–6 | readiness + liveness |
| `gpu` | 1 / 4Gi + 1 GPU | 2 / 8Gi + 1 GPU | — | liveness |

Burstable QoS by default (requests < limits): Guaranteed QoS at low
utilization wastes nodes. Ephemeral storage always explicit. Overrides live in
`gojira.yaml` and **require a justification** — and once the rightsizing loop
has measured your app, Gojira writes that justification for you. All opinions
live in [`gojira/opinions/`](gojira/opinions/) — visible, documented,
overridable. That's the point of the tool.

By default an app serves HTTP on **8080** with `/health` and `/metrics`, so
probes and scrape configs need zero per-app configuration. An app that
listens elsewhere or answers `/alive` says so in `gojira.yaml` (`port:`,
`health:`), and adopt mode reads both from the deployment it already runs:
nothing about an app's SDK or runtime decides whether Gojira can host it.

## Quickstart

**Prereqs:** a GCP project you can create resources in, a GKE cluster,
`gcloud` (authed, with `gke-gcloud-auth-plugin`), `kubectl`, `openssl`,
Python 3.11+, and the [`gh`](https://cli.github.com) CLI (authed) for laptop
mode. `./scripts/preflight.sh` checks all of it, plus the cluster, in seconds.

### Laptop mode (~5 minutes)

Runs Gojira locally against your own `gh` and `gcloud` sessions — no GitHub
App required:

```bash
git clone https://github.com/tenishai/gojira && cd gojira
python3 -m venv venv && venv/bin/pip install -e .
venv/bin/python -m gojira.web          # UI on http://localhost:8080
```

Then in the UI: **Clusters** → register your GKE cluster (registration probes
connectivity before accepting) → **Connect repo** → pick a repo with a
`Dockerfile` at its root, choose a class and environments → merge the setup PR
it opens → push to main. Your app deploys.

### Production-shaped sandbox (~20 minutes)

Runs Gojira *in-cluster* under Workload Identity with a GitHub App — the way
you'd run it for a team. One values file, then two commands:

```bash
cd terraform && terraform apply        # Autopilot cluster + Gojira's identity
cd .. && cp gojira.env.example gojira.env   # fill in project, App id, pem path
make install                           # preflight, image, secrets, manifests, rollout
```

Every optional knob — front door hostname and IAP members, Gojira's own
allowlist, namespace labels, the sizing agent image, the argus observe image and clusters — lives in `gojira.env`
too; nothing under `deploy/` is edited by hand, and rerunning `make install`
applies changes.

The full walkthrough — GitHub App setup, connecting a demo app, exercising
drift capture and the rightsizing loop end to end — is in
[docs/dogfood.md](docs/dogfood.md). Teardown is `terraform destroy`, and
rebuilding from nothing is the same two commands; the platform's state that
matters lives in your repos, and survives.

### Existing cluster (brownfield)

Already have a cluster that terraform didn't create? Swap the terraform step
for [scripts/prepare-project.sh](scripts/prepare-project.sh) — it grants the
same platform identity (APIs, GSA, roles, AR, Workload Identity) into your
existing project, then `bootstrap.sh` proceeds unchanged with `CLUSTER`
pointed at your cluster:

```bash
# in gojira.env: CLUSTER=<your-cluster>  GOJIRA_PREPARE_PROJECT=1
make install
```

## Feeding the rightsizing loop

Evidence is one JSON document per app/env/window — Kubernetes quantity strings,
distilled percentiles, and distress signals:

```bash
curl -X POST http://<gojira>/api/v1/usage \
  -H "Authorization: Bearer $CLUSTER_TOKEN" \
  -d @usage_profile.json
```

See [`tests/fixtures/usage_profile.json`](tests/fixtures/usage_profile.json)
for the exact contract — it doubles as the cross-repo handshake for
[gojira-sizing-agent](https://github.com/tenishai/gojira-sizing-agent), the
in-cluster agent Gojira deploys from the Clusters page ("Deploy agent": mints
the cluster token, applies RBAC + CronJob, grants metrics access, runs a
first sweep). Anything else that can compute a p95 can feed the loop too.

The ledger reads back the same way, per environment, with the recommender's
live call alongside — so a second evidence producer can diff itself against
what the loop saw and what it decided:

```bash
curl http://<gojira>/api/v1/apps/<id>/usage?env=dev&since_days=30 \
  -H "Authorization: Bearer $GOJIRA_API_TOKEN"   # the platform token, not a cluster's
```

## What's built, what's next

| | |
|---|---|
| ✅ Connect: provisioner, generator, setup PRs, keyless CI, promotion | proven live |
| ✅ Capture: drift detection, capture PRs, central scheduler | proven live |
| ✅ Rightsize: evidence API, recommender, one-PR resizing | proven live |
| ✅ In-cluster usage agent (GMP/Prometheus → evidence, per cluster) | proven live |
| ✅ One-button agent deploy + token rotation (Clusters page) | proven live |
| 🔜 Public webhook ingress (auto PR-merge detection, regenerate-on-change) | buttons today |
| ✅ Adopt: zero-touch onboarding infers app needs from existing manifests | new |
| 🔜 Guard: Kyverno admission policies | designed |

## Stack

Python · FastAPI · SQLAlchemy (SQLite default, Postgres supported) ·
Jinja2 + htmx · GitHub App · GCP (`gcloud`/`kubectl`, Workload Identity
in-cluster) · a test suite with golden-file snapshots for everything generated.

## Docs

[Design](docs/design.md) · [Dogfood runbook](docs/dogfood.md) ·
[GitHub App setup](docs/github-app-setup.md) ·
[Environments](docs/environments.md) ·
[Sizing loop design](docs/sizing-feedback-design.md)

## License

[AGPL-3.0-or-later](LICENSE)
