"""UsageProfile — the wire contract between in-cluster agents and Gojira.

Agents POST one of these per (app, environment, window) to /api/v1/usage:
distilled percentiles, not raw samples. Quantities are Kubernetes quantity
strings ("250m", "256Mi") — the same dialect as manifests and kubectl, so
agents pass values through without unit conversion. Normalization to
comparable numbers happens on Gojira's side (gojira.drift.normalize_quantity).

The golden fixture tests/fixtures/usage_profile.json is the cross-repo
contract test: the agent repo asserts its serializer produces it; this model
asserts it validates. Change the contract → change the fixture → both sides
notice.
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from gojira.drift import normalize_quantity

MAX_WINDOW_DAYS = 90


class ResourceUsage(BaseModel):
    """One resource's declared spec and observed distribution over the window."""

    request: str
    limit: str
    p50: str
    p95: str
    p99: str
    max: str

    @field_validator("request", "limit", "p50", "p95", "p99", "max")
    @classmethod
    def _parseable_quantity(cls, v: str) -> str:
        # Reject junk at the door — "" or "lots" fails here, not in the
        # recommender. A ResourceUsage doesn't know which resource it describes,
        # so accept either dialect: cpu ("250m", "1") or bytes ("256Mi").
        for key in ("cpu", "memory"):
            try:
                normalize_quantity(key, v)
                return v
            except ValueError:
                continue
        raise ValueError(f"not a Kubernetes quantity: {v!r}")


class UsageProfile(BaseModel):
    """One report: an app's measured usage in one environment over one window."""

    app: str = Field(min_length=1, max_length=63)
    environment: str = Field(min_length=1, max_length=16)
    cluster: str = Field(min_length=1, max_length=63)
    window_days: int = Field(gt=0, le=MAX_WINDOW_DAYS)
    samples: int = Field(gt=0)
    cpu: ResourceUsage
    memory: ResourceUsage
    restarts: int = Field(ge=0)
    oom_kills: int = Field(ge=0)
    reported_by: str = Field(min_length=1, max_length=255)
    reported_at: datetime
