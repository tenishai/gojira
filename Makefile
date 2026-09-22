.PHONY: venv install test lint check build run preflight clean

VENV := venv
# env -u PYTHONPATH: a globally-set PYTHONPATH leaks foreign packages into
# venvs and makes pip skip installing "already satisfied" deps. Strip it.
PY := env -u PYTHONPATH $(VENV)/bin/python

venv:
	python3 -m venv $(VENV)

install: venv
	$(PY) -m pip install -q -e ".[dev]"

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
