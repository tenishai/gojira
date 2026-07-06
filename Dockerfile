# Gojira runs anywhere the operator has gcloud creds — laptop, VM, or in-cluster
# with Workload Identity. The v0.1 backend shells out to git, gcloud, and
# kubectl (with the GKE auth plugin), so the image carries all three. REST to
# GitHub is httpx; no `gh` needed when running the GitHub App backend.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    USE_GKE_GCLOUD_AUTH_PLUGIN=True \
    GOJIRA_HOST=0.0.0.0 \
    GOJIRA_PORT=8080

# git (clone/push), plus the Google Cloud CLI repo for gcloud + kubectl +
# the GKE auth plugin that `get-credentials` needs to build a working kubeconfig.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl gnupg ca-certificates apt-transport-https \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        google-cloud-cli kubectl google-cloud-cli-gke-gcloud-auth-plugin \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY gojira ./gojira
RUN pip install .

# Non-root. /home/gojira holds the kubeconfig gcloud writes at runtime.
RUN useradd --create-home --uid 10001 gojira && chown -R gojira:gojira /app
USER gojira

EXPOSE 8080
CMD ["python", "-m", "gojira.web"]
