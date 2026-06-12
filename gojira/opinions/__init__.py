"""Gojira's opinions, loaded from data files in this package.

The opinions are the product: visible, documented, overridable.
"""

import logging
from importlib import resources

import yaml
from pydantic import BaseModel

log = logging.getLogger(__name__)


class Resources(BaseModel):
    cpu: str
    memory: str
    gpu: int | None = None


class HPA(BaseModel):
    target_cpu_percent: int
    min_replicas: int
    max_replicas: int


class Probe(BaseModel):
    type: str  # http | tcp | exec
    path: str | None = None


class WorkloadClass(BaseModel):
    name: str
    requests: Resources
    limits: Resources
    hpa: HPA | None = None
    probes: dict[str, Probe] = {}


class SizingOpinions(BaseModel):
    ephemeral_storage: dict[str, str]
    classes: dict[str, WorkloadClass]


def load_sizing() -> SizingOpinions:
    raw = yaml.safe_load(
        resources.files("gojira.opinions").joinpath("sizing.yaml").read_text()
    )
    classes = {
        name: WorkloadClass(name=name, **spec) for name, spec in raw["classes"].items()
    }
    log.debug("loaded sizing opinions: %s", list(classes))
    return SizingOpinions(ephemeral_storage=raw["ephemeral_storage"], classes=classes)


WORKLOAD_CLASSES = ("web", "worker", "cron", "heavy", "gpu")
