# AGENTS.md — gojira

Instructions for AI coding agents working in this repo. Claude Code loads this via
`CLAUDE.md` (`@AGENTS.md`); other AGENTS.md-aware tools read it directly.

Start with the [README](README.md) for what Gojira is, and
[docs/install.md](docs/install.md) for how it is installed. The other files in
`docs/` are design records: read them for the reasons, not as a spec of the
current code.

## Layout

- `gojira/` — the service. `web/` is the FastAPI app and UI; `provisioner.py`
  runs the provision steps in `steps.py`; `generator/` renders what Gojira
  writes into a tenant repo (templates in `generator/templates/`); `opinions/`
  holds the workload-class defaults; `sizing/` is the rightsizing loop.
- `tests/` — pytest. `tests/fakes.py` stands in for GitHub, GCP and kubectl;
  `tests/golden/` holds rendered tenant files.
- `deploy/`, `scripts/`, `terraform/` — installing Gojira itself.
- `examples/hello-app/` — the app the install guide deploys.

## Commands

```bash
make dev      # venv/ with the package and dev extras
make check    # ruff + pytest
```

The rendered tenant files are the product, so `tests/test_generator.py` snapshots
them. After an intended template change, regenerate and review the diff:

```bash
GOJIRA_UPDATE_GOLDEN=1 venv/bin/python -m pytest tests/test_generator.py
```

## Rules

- Sign off every commit: `git commit -s` (see [CONTRIBUTING.md](CONTRIBUTING.md)).
- Stage files by path. `gojira.env`, `*.pem`, `scratch*.txt` and `CLAUDE.local.md`
  are gitignored local files; never commit them.
- A tenant's `gojira.yaml` says what the app needs; Gojira supplies what the
  platform knows. Defaults are opinions with overrides, never requirements on the
  app.
