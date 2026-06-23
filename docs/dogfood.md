# Dogfooding Gojira

End-to-end: stand up a throwaway GKE sandbox, run Gojira **in-cluster**, and use
it to deploy [`gojira-demo`](https://github.com/shaz/gojira-demo) — then patch
the running app and watch Gojira open a drift-capture PR.

This is the production-shaped path (in-cluster Gojira, GitHub App auth). It
skips the public webhook on purpose — GitHub reaching a public HTTPS endpoint is
the fiddly part — so PR-merge and drift are driven by the in-UI **check PR
merge** and **Scan now** buttons instead.

---

## Prerequisites

- A **sandbox GCP project** with billing (throwaway — Gojira gets broad IAM in it).
- `gcloud` authed as a principal who can apply the Terraform (project IAM admin).
- `terraform`, `docker`, `kubectl` locally.
- A **GitHub App** (see [github-app-setup.md](github-app-setup.md)) installed on
  `shaz/gojira-demo`. You need its **App ID**, a **private key** (`.pem`), and a
  **webhook secret** string (any random value — we don't wire webhooks, but the
  config field is required).

```bash
export PROJECT=your-sandbox-project
export REGION=us-central1
```

---

## 1 — Stand up the sandbox

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # set project_id
terraform init
terraform apply
```

Note the outputs — you'll reuse them:

```bash
terraform output                       # gojira_gsa_email, ar_repo_path, get_credentials_cmd, ...
eval "$(terraform output -raw get_credentials_cmd)"   # kubeconfig for the sandbox
cd ..
```

## 2 — Build and push Gojira's image

```bash
export AR_PATH=$(cd terraform && terraform output -raw ar_repo_path)   # e.g. us-central1-docker.pkg.dev/$PROJECT/gojira
gcloud auth configure-docker ${REGION}-docker.pkg.dev -q
docker build -t $AR_PATH/gojira:dev .
docker push $AR_PATH/gojira:dev
```

## 3 — Create the GitHub App secret

Keep the private key out of git — create the Secret directly:

```bash
kubectl create namespace gojira --dry-run=client -o yaml | kubectl apply -f -   # ns first; kustomize is idempotent over it
kubectl -n gojira create secret generic gojira-github-app \
  --from-literal=app_id="$GOJIRA_GITHUB_APP_ID" \
  --from-literal=webhook_secret="$GOJIRA_WEBHOOK_SECRET" \
  --from-file=private_key=./github-app.pem
```

## 4 — Deploy Gojira in-cluster

Fill the two placeholders in `deploy/` (GSA email for Workload Identity, the
image path), then apply:

```bash
GSA=$(cd terraform && terraform output -raw gojira_gsa_email)
sed -i "s#REPLACE_GSA_EMAIL#$GSA#"  deploy/serviceaccount.yaml
sed -i "s#REPLACE_AR_PATH#$AR_PATH#" deploy/kustomization.yaml

kubectl apply -k deploy/
kubectl -n gojira rollout status deploy/gojira
```

> The `sed` edits are local only — don't commit them. (Or template them with a
> kustomize overlay if you prefer.)

## 5 — Open the UI

```bash
kubectl -n gojira port-forward svc/gojira 8080:80
```

Browse to <http://localhost:8080>.

---

## 6 — Register the sandbox cluster

In the UI → **Clusters** → add:

| Field | Value |
|---|---|
| name | `sandbox` |
| gcp_project | `$PROJECT` |
| location | `$REGION` |
| gke_cluster | `gojira-sandbox` |

Registration probes connectivity (`get-credentials` + `kubectl auth can-i`); a
green result means Gojira (via Workload Identity) can reach the cluster.

## 7 — Connect gojira-demo

UI → **Connect** → pick `shaz/gojira-demo`, then:

- **class:** `web`
- **environment(s):** one `dev` env on the `sandbox` cluster, namespace `demo`

Gojira provisions GCP-side identity (deploy GSA, WIF pool + per-repo binding,
Artifact Registry, namespace) and opens a **setup PR** on `gojira-demo`.

## 8 — Merge the setup PR → deploy

1. Merge the setup PR on GitHub (it adds `.github/workflows`, `k8s/`, `gojira.yaml`).
2. Back in the UI, click **check PR merge** on the app page — Gojira flips the
   app to `active`.
3. The merge triggers `gojira-ci` in the demo repo (WIF auth, no keys): it builds,
   pushes to Artifact Registry, and `kubectl apply`s to the sandbox. Watch the
   Actions run, then:

```bash
kubectl -n demo get deploy,pods
```

You now have an app Gojira deployed, with no hand-written YAML.

---

## 9 — Exercise drift (the Phase 2 payoff)

Simulate an operator hot-patching production:

```bash
kubectl -n demo set resources deploy/gojira-demo --requests=cpu=750m
```

In the UI, on the app page → **Drift** → **Scan now**. Within the scan you'll see:

- a **Drift** finding: `containers.app.resources.requests.cpu` — git `250m` → cluster `750m`
- a **capture PR** link on `gojira-demo` that writes the live value into the env
  overlay, so merging it makes git match the cluster.

The cluster's change is **captured, not reverted** — exactly the Phase 2 DoD.
(Leave `drift.enabled: true` in the ConfigMap and the central scheduler does the
same sweep every 15 min without the button.)

---

## Teardown

```bash
cd terraform && terraform destroy
```

This deletes the cluster, AR repo, and Gojira's GSA. Per-app GSAs/WIF that
Gojira created in the project are not Terraform-managed — remove them with
`gojira destroy` (when it lands) or by hand if you're keeping the project.
