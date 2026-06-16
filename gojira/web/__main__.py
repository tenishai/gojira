"""Run the Gojira web app: `python -m gojira.web` (or via the Makefile)."""

import logging
import os

import uvicorn

from gojira.web import build_default_app

logging.basicConfig(level=logging.INFO)

app = build_default_app()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("GOJIRA_HOST", "127.0.0.1"),
        port=int(os.environ.get("GOJIRA_PORT", "8080")),
    )
