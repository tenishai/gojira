.PHONY: venv dev install uninstall test lint check build run preflight clean

VENV := venv
# env -u PYTHONPATH: a globally-set PYTHONPATH leaks foreign packages into
# venvs and makes pip skip installing "already satisfied" deps. Strip it.
PY := env -u PYTHONPATH $(VENV)/bin/python

# uv.lock pins every dependency; `make dev` installs exactly that set (needs
# uv: https://docs.astral.sh/uv/). `make lock` re-resolves after a pyproject
# change — review the lock diff like code.
dev:
	UV_PROJECT_ENVIRONMENT=$(VENV) uv sync --locked --extra dev

lock:
	uv lock

# Install Gojira into your cluster from gojira.env (cp gojira.env.example).
install:
	./scripts/install.sh

# Remove Gojira again, in the order that works. ARGS="--purge --yes", or
# ARGS=--check to only list what Gojira has in the project (changes nothing).
uninstall:
	./scripts/uninstall.sh $(ARGS)

test:
	$(PY) -m pytest -q

lint:
	env -u PYTHONPATH $(VENV)/bin/ruff check gojira tests

check: lint test

# GKE nodes are amd64; an ARM laptop's plain `docker build` makes an image
# that dies with `exec format error` on the cluster. bootstrap.sh sidesteps
# this with Cloud Build; this target is for local runs.
build:
	docker build --platform linux/amd64 -t gojira:dev .

preflight:
	./scripts/preflight.sh

run:
	$(PY) -m gojira.web

clean:
	rm -rf $(VENV) *.egg-info .pytest_cache .ruff_cache
