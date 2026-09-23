# hello-app

The smallest app Gojira can deploy: a Dockerfile at the root, a process on
port 8080, `/health` and `/metrics`. Use it to prove an install end to end
before pointing Gojira at anything real.

```bash
# 1. make it a repo of your own (Gojira deploys repos, not directories)
cp -r examples/hello-app ~/hello-app && cd ~/hello-app
git init -q && git add -A && git commit -qm "hello-app" && git branch -M main
gh repo create <your-org>/hello-app --private --source=. --push

# 2. install your Gojira GitHub App on it (docs/github-app-setup.md §3)

# 3. in the Gojira UI: Connect repo → <your-org>/hello-app, class `web`,
#    environments dev → merge the setup PR it opens → it deploys.
```

Run it locally to see what Gojira's probes will see:

```bash
python3 app.py &  curl -s localhost:8080/health; curl -s localhost:8080/metrics
```
