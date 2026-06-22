# Standing up the Gojira GitHub App

What it takes to run Gojira App-backed (the hosted, webhook-driven path) instead
of the operator's local `gh auth` session. Do this once per environment.

## 1. Register the App

Create a GitHub App in the **fubotv** org (Settings → Developer settings → GitHub
Apps → New). Org-owned, not user-owned, so it survives any one person leaving.

- **Homepage URL:** the Gojira URL (e.g. `https://gojira.fubotv.net`).
- **Webhook URL:** `https://<gojira-host>/webhook/github`
- **Webhook secret:** generate a strong random value — this is `webhook_secret`.
  Gojira HMAC-verifies every delivery against it (fail-closed).

### Repository permissions

The minimum the backend needs:

| Permission | Level | Why |
|---|---|---|
| Contents | Read & write | clone, push the `gojira/setup` branch, create release tags |
| Pull requests | Read & write | open the setup PR, detect merge |
| Metadata | Read | repo info, default branch (mandatory) |
| Actions | Read | required to subscribe to the `workflow_run` event |

No organization or account permissions are required.

### Subscribe to events

- **Pull request** — drives `awaiting_pr` → `active` on setup-PR merge.
- **Workflow run** — records CI / Promote outcomes in the UI.

`installation` and `installation_repositories` are delivered to an App's webhook
automatically (no checkbox) and keep the repo→installation cache current.

## 2. Private key + secrets

Generate a private key in the App settings (downloads a `.pem`). Store it and the
webhook secret in **GCP Secret Manager** (fubotv-prod), never in git:

```bash
gcloud secrets create gojira-github-app-key --data-file=github-app.pem --project=fubotv-prod
printf '%s' "$WEBHOOK_SECRET" | gcloud secrets create gojira-webhook-secret --data-file=- --project=fubotv-prod
```

Mount the key at `private_key_path` and inject the secret as `GOJIRA_WEBHOOK_SECRET`
(External Secrets Operator → a mounted file + env var is the fubo-standard pattern).

## 3. Install the App

Install it on the repos (or the whole org) Gojira should deploy. Each install
fires an `installation` webhook that seeds the `installation_repos` cache, so the
connect picker lists those repos and token minting works without a cold API call.

## 4. Configure Gojira

```yaml
github:
  app_id: ${GOJIRA_GITHUB_APP_ID}
  private_key_path: /secrets/github-app.pem
  webhook_secret: ${GOJIRA_WEBHOOK_SECRET}
database:
  url: postgresql://gojira:${DB_PASSWORD}@.../gojira   # Postgres for a hosted run
```

With `app_id` + `private_key_path` set, Gojira selects the App backend; omit them
and it falls back to `gh auth` (local dev). The webhook endpoint verifies as soon
as `webhook_secret` is set — unset means every delivery is rejected (fail-closed).

## 5. GCP identity for the provisioner (separate from the App)

The App handles GitHub; the **provisioner still talks to GCP** by shelling out to
`gcloud`. In-cluster that means binding Gojira's KSA to a GCP service account via
Workload Identity. That GSA needs enough IAM to create the per-project deploy
identity Gojira provisions — service accounts, WIF pools/providers, Artifact
Registry, project-level role bindings, and GKE access.

> **fubo gotcha:** project-level IAM on `fubotv-prod` (and creating SAs / WIF) must
> go through the **access-request channel**, not be granted directly. Scope the
> Gojira controller GSA's request deliberately — it's a privileged identity.

## 6. Reachability

GitHub must reach `POST /webhook/github` over the public internet. Front Gojira
with an HTTPS load balancer (or IAP-exempt the webhook path if behind IAP — the
HMAC secret is the auth there, IAP would block GitHub's unauthenticated POST).

## Verify

1. Install on a test repo → confirm an `installation` event lands (an
   `installation_repos` row appears).
2. Connect the repo in the UI → `App.installation_id` is set (not `0`).
3. Watch provisioning open the setup PR; merge it → the `pull_request` webhook
   flips the app to `active` with no manual "check PR merge".
4. Cut a release → the `workflow_run` webhook records `promote.success` / `.failure`.
