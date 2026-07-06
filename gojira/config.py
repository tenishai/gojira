"""Configuration loading: config.yaml with ${ENV_VAR} interpolation."""

import logging
import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

log = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


class GitHubConfig(BaseModel):
    app_id: str = ""
    private_key_path: str = ""
    webhook_secret: str = ""


class DatabaseConfig(BaseModel):
    url: str = "sqlite:///gojira.db"


class DefaultsConfig(BaseModel):
    ar_location: str = "us-central1"
    environments: list[str] = ["dev", "qa", "prod"]


class DriftConfig(BaseModel):
    """The central drift detector. Off by default — opt in per operator."""

    enabled: bool = False
    interval_seconds: int = 900  # 15 min — matches the Phase 2 DoD


class SizingConfig(BaseModel):
    """The Phase 4 rightsizing loop's knobs (see docs/sizing-feedback-design.md)."""

    materiality_pct: float = 30.0  # no proposal unless the request moves this much
    limit_headroom: float = 1.5    # proposed limit = p99 * headroom
    history_days: int = 30         # evidence window the recommender reads


class Config(BaseModel):
    github: GitHubConfig = GitHubConfig()
    database: DatabaseConfig = DatabaseConfig()
    defaults: DefaultsConfig = DefaultsConfig()
    drift: DriftConfig = DriftConfig()
    sizing: SizingConfig = SizingConfig()


def _interpolate(value: str) -> str:
    """Replace ${VAR} or ${VAR:-default} with environment values."""

    def repl(m: re.Match) -> str:
        var, default = m.group(1), m.group(2)
        resolved = os.environ.get(var, default)
        if resolved is None:
            raise ValueError(f"config references unset environment variable {var}")
        return resolved

    return _ENV_PATTERN.sub(repl, value)


def _walk(node):
    if isinstance(node, dict):
        return {k: _walk(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v) for v in node]
    if isinstance(node, str):
        return _interpolate(node)
    return node


def load_config(path: str | Path | None = None) -> Config:
    """Load config from path, $GOJIRA_CONFIG, or ./config.yaml; defaults if absent."""
    candidate = path or os.environ.get("GOJIRA_CONFIG") or "config.yaml"
    candidate = Path(candidate)
    if not candidate.exists():
        log.debug("no config file at %s, using defaults", candidate)
        return Config()
    raw = yaml.safe_load(candidate.read_text()) or {}
    log.debug("loaded config from %s", candidate)
    return Config.model_validate(_walk(raw))
