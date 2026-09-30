# Standing up the Gojira GitHub App

Gojira acts on your repos as a GitHub App: it clones, pushes the setup branch,
opens PRs and receives webhooks. Do this once per Gojira install. Ten minutes.

## 1. Create the App — in your organization, not your user account

Go to **`https://github.com/organizations/<your-org>/settings/apps/new`**
(Organization settings → Developer settings → GitHub Apps → New GitHub App).

> **Org-owned matters.** A GitHub App created under a personal account can only
> be installed on that account's own repos — it cannot be installed into an
> organization, and the failure is silent: the org just never appears in the
> install picker. It also survives any one person leaving.

Fill in:

| Field | Value |
|---|---|
| GitHub App name | `gojira-<org>` (must be globally unique) |
| Homepage URL | your Gojira URL, or the repo URL if you have no front door yet |
| Webhook → Active | on if Gojira has a public HTTPS hostname; **off** for a port-forward install (you'll use the in-UI check buttons instead) |
| Webhook URL | `https://<gojira-host>/webhook/github` |
| Webhook secret | leave blank for now — bootstrap mints one and tells you how to read it (step 4) |
| Where can this App be installed? | Only on this account |

### Repository permissions

| Permission | Level | Why |
|---|---|---|
| Contents | Read & write | clone, push the `gojira/setup` branch, tag releases |
| Pull requests | Read & write | open the setup PR, detect merge |
| Workflows | Read & write | the setup branch adds `.github/workflows/*.yaml`; GitHub rejects that push without this, separately from Contents |
| Metadata | Read | repo info, default branch (GitHub sets it for you) |
| Actions | Read & write | Redeploy, rollback and promotion start the workflow themselves, and GitHub answers 403 `Resource not accessible by integration` without write; read alone only covers subscribing to `workflow_run` |

No organization or account permissions.

### Subscribe to events

- **Pull request** — setup-PR merge flips the app from `awaiting_pr` to `active`.
- **Workflow run** — CI and promotion outcomes show up in the UI.

(`installation` events are delivered to every App automatically; they keep
Gojira's repo→installation cache current.)

Click **Create GitHub App**. Note the **App ID** shown at the top of the App's
page — that is `GOJIRA_GITHUB_APP_ID`.

## 2. Private key

On the App's page, scroll to **Private keys → Generate a private key**. A
`.pem` downloads. Move it next to your Gojira checkout and point
`GOJIRA_GITHUB_APP_PEM` at it in `gojira.env`. `*.pem` is gitignored; bootstrap
puts it in a Kubernetes Secret and nowhere else.

## 3. Install the App

App page → **Install App** → your org → choose **Only select repositories** and
pick the repos Gojira should deploy (start with
[examples/hello-app](../examples/hello-app/README.md) pushed to a repo of your
own). You can add repos later from the same page.

If you changed the App's permissions after installing, GitHub asks the org
to **accept** the new permissions on the installation page; until it's
accepted, Gojira's tokens keep the old scopes.

## 4. Run the install, then hand GitHub the webhook secret

```bash
cp gojira.env.example gojira.env   # PROJECT, GOJIRA_GITHUB_APP_ID, GOJIRA_GITHUB_APP_PEM, …
make install
```

Bootstrap mints the webhook secret and, at the end, prints the command that
reads it back:

```bash
kubectl -n gojira get secret gojira-github-app -o jsonpath='{.data.webhook_secret}' | base64 -d; echo
```

If your App's webhook is active, paste that value into the App's **Webhook
secret** field and save. Gojira HMAC-verifies every delivery against it and
rejects everything otherwise (fail-closed).

## 5. Verify

1. **Install event landed**: Gojira's log shows `installation` for your org
   (or the connect picker lists the repos you selected).
2. **Connect a repo** in the UI → the app row shows an installation id, not `0`.
3. **Setup PR opens** on the repo; merge it. With a live webhook the app flips
   to `active` on its own; without one, click **I merged the PR — check**.
4. Push to main → the generated CI builds, pins the digest, deploys.

## What the App does not cover

The App is Gojira's GitHub identity. Its **GCP** identity is separate: in-cluster
Gojira runs as a Kubernetes service account bound through Workload Identity to
the `gojira@<project>` service account that `terraform/` or
`scripts/prepare-project.sh` creates. That GSA is privileged — it creates
per-app deploy identities, Workload Identity pools, Artifact Registry repos and
project role bindings — so grant it to a project you mean Gojira to run.
