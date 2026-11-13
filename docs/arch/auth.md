---
card: arch-card:gojira-auth
updated: 2026-11-13
anchor: gojira/web/auth.py sha=a1241f6cfab86d6380710dc2c8a3085cd6b5e5e5
anchor: gojira/config.py sha=b5dc87fe7a1c2e52554e4675d29a3576c2be10f1
---
# auth — three independent front-door mechanisms

WHAT / SEAM: three disjoint auth mechanisms, each the ONLY check on its own
route family — none substitutes for another:
  1. **IAP-trust middleware** (`web.auth.install`) — the human UI. Trusts
     `x-goog-authenticated-user-email`, enforces `AuthConfig.allowed_domain`
     / `allowed_emails`. Off by default.
  2. **`auth.api_token`** (`api_token_error()`) — shared bearer
     token gating every `/api/v1/*` route except `POST /api/v1/usage`
     (mechanism 3). All `/api/v1/*` routes are EXEMPT from mechanism 1.
  3. **Cluster-scoped agent tokens** (`AgentToken`, see rightsize-ingest
     card) — gate only `POST /api/v1/usage`; not part of `AuthConfig`.
OPS / FLOW: `install()` no-ops unless `auth.enabled`. `EXEMPT_PREFIXES =
("/webhook/", "/api/v1/", "/healthz", "/metrics")` (`EXEMPT_PREFIXES` in auth.py), matched by
segment boundary (`_exempt()` — `/metrics` doesn't match `/metricsfoo`).
Non-exempt paths need a valid IAP header or get 401/403; the email lands on
`request.state.user_email` for audit. Exempt paths carry their OWN auth:
`/webhook/github` is HMAC-verified in webhooks.py; `/api/v1/usage` is gated
inside `ingest_usage()`; every OTHER `/api/v1/*` route must call
`api_token_error(request, auth)` itself as its first line.
INVARIANTS:
- the `/api/v1/*` IAP exemption is safe ONLY because every route under it
  carries its own auth (`api_token_error` docstring). A new route that skips calling
  `api_token_error()` is reachable with NO auth while IAP is enabled — this
  already happened once (the onboarding routes originally had none).
- `api_token_error` fails CLOSED when IAP is enabled but no `api_token` is
  set (the `auth.enabled` branch of `api_token_error`) — never silently no-auth while `auth.enabled = True`.
- token comparison: `secrets.compare_digest` on ENCODED bytes, None-checked
  first (`api_token_error`) — `compare_digest` raises on `None` and on non-ASCII
  str input, which is attacker-controlled here; encoding turns that into an
  ordinary 401 instead of a 500.
- `/healthz` + `/metrics` are exempt so the kubelet probe and in-cluster
  scraper (below IAP) never need identity (#17 — the gate once 401'd
  Gojira's own readiness probe).
- never enable mechanism 1 without an actual trusted proxy in front — the
  header is trivially forgeable on a bare endpoint.
CONSUMERS: `gojira.web.create_app()` calls `auth.install(app, config.auth)`
once. Every `/api/v1/*` handler except `/api/v1/usage` is expected to call
`api_token_error` itself — this card's anchor on config.py does NOT prove
every route does; verify new routes in web/__init__.py directly.
API: `install`, `identity_from`, `allowed`, `api_token_error` (web/auth.py);
`AuthConfig` (config.py).

CHANGE 2026-10-13: build_args may reference a repository variable as ${NAME}; CIConfig.repo_variables (GOJIRA_REPO_VARIABLES) names the values and SetRepoVariables sets them; an unknown reference refuses the provision (#102).

CHANGE 2026-10-23: config.py — ObserveClusterConfig defaults are noto/noto-observe; the GOJIRA_OBSERVE_CLUSTERS string accepts name=namespace/service_account (auth untouched).

CHANGE 2026-11-13: config.py — Config.version (GOJIRA_VERSION, set by bootstrap from the checkout's nearest release tag) names the running build; /healthz carries it and stays identity-exempt, the footer shows it. Auth untouched.
