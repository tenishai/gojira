"""The UsageProfile wire contract: the golden fixture is the cross-repo handshake."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from gojira.sizing.profile import UsageProfile

FIXTURE = Path(__file__).parent / "fixtures" / "usage_profile.json"


def _fixture(fresh: bool = True) -> dict:
    """The golden fixture, with reported_at refreshed to now by default.

    The recommender reads evidence through a now-anchored history window
    (store.history: now - history_days), so any test that feeds the fixture
    into the store would silently expire history_days after the frozen date
    in the file — the whole sizing suite went red on 2026-08-05 that way.
    fresh=False serves the file verbatim for the wire-contract tests, which
    are about the frozen form (field shapes, the Z-suffixed timestamp).
    """
    data = json.loads(FIXTURE.read_text())
    if fresh:
        data["reported_at"] = (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )
    return data


def test_golden_fixture_validates():
    """The contract test. The agent repo asserts it EMITS this fixture;
    we assert we ACCEPT it. Break the contract → this fails on one side."""
    p = UsageProfile.model_validate(_fixture(fresh=False))
    assert p.app == "clips-api"
    assert p.window_days == 14
    assert p.cpu.p99 == "820m"
    assert p.memory.max == "402Mi"
    assert p.reported_at.year == 2026


def test_fixture_round_trips():
    p = UsageProfile.model_validate(_fixture())
    again = UsageProfile.model_validate(json.loads(p.model_dump_json()))
    assert again == p


@pytest.mark.parametrize(
    "patch",
    [
        {"window_days": 0},                      # empty window
        {"window_days": 365},                    # beyond MAX_WINDOW_DAYS
        {"samples": 0},                          # no data
        {"restarts": -1},                        # negative count
        {"app": ""},                             # unnamed app
        {"cpu": {"request": "lots", "limit": "500m", "p50": "1m",
                 "p95": "1m", "p99": "1m", "max": "1m"}},  # junk quantity
    ],
)
def test_rejects_bad_profiles(patch):
    bad = {**_fixture(), **patch}
    with pytest.raises(ValidationError):
        UsageProfile.model_validate(bad)


def test_accepts_both_quantity_dialects():
    """cpu fields carry millicores, memory fields carry byte suffixes —
    the shared ResourceUsage must accept each."""
    p = UsageProfile.model_validate(_fixture())
    assert p.cpu.request.endswith("m") and p.memory.request.endswith("Mi")
