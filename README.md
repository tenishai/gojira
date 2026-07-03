# Gojira

**An opinionated deploy platform for GKE. It tramples your hand-rolled YAML.**

Connect a GitHub repo, pick a cluster and a workload class, and Gojira
provisions everything needed to ship: service account, Workload Identity
Federation, Artifact Registry access, GitHub Actions workflows, Kustomize
manifests, and observability wiring. Merge the setup PR — after that,
push-to-deploy just works.

> **Status:** early development. Not yet usable. See
> [docs/design.md](docs/design.md) for the full design.

## Why

Every team deploying to Kubernetes hand-rolls the same things: CI workflows,
manifests, resource specs, auth plumbing. The results are inconsistent,
over-provisioned, and expensive to change org-wide. Gojira makes those
decisions for you — visibly, with an escape hatch for justified overrides.

Three opinions define it:

1. **The platform decides sizing.** Workload classes (`web`, `worker`,
   `cron`, `heavy`, `gpu`) map to burstable resource specs calibrated
   against real production behavior — not guesses copied from a blog post.
2. **No ArgoCD.** Deploys are git → Actions → `kubectl apply`. Operational
   fixes flow the other way: cluster → drift detector → PR. Nothing silently
   reverts an operator's work.
3. **Observable from the first deploy.** Prometheus wiring is generated,
   not optional.

## Stack

Python · FastAPI · SQLAlchemy (SQLite default, Postgres supported) ·
Jinja2 + htmx · GitHub App · GCP (ADC)

## License

[AGPL-3.0-or-later](LICENSE)