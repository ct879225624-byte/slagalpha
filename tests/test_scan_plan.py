"""Synthetic Universe obligations only; these tests never run a strategy scan."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from slagalpha.domain.universe import ContractRegistry, RegistryVerification
from slagalpha.research.parameters import build_dev_parameter_version
from slagalpha.research.replay_inputs import ReplayDataInputError
from slagalpha.research.scan_plan import (
    DevScanDayPlan,
    DevScanPlan,
    build_dev_scan_plan,
    write_dev_scan_plan,
)
from slagalpha.research.sensitivity import build_default_sensitivity_plan
from slagalpha.research.splits import (
    audit_research_inputs,
    build_research_split,
    snapshot_sequence_hash,
)
from test_historical_replay import _rule
from test_research_splits import _hash, _snapshot


def _scan_context(*, ready: bool = True) -> dict[str, Any]:
    start = date(2024, 1, 1)
    snapshots = tuple(_snapshot(start + timedelta(days=i)) for i in range(4))
    split = build_research_split(
        research_start=start, research_end_exclusive=start + timedelta(days=4),
        universe_batch_run_version=_hash("synthetic-run"),
        daily_snapshot_hash=snapshot_sequence_hash(snapshots),
    )
    registry = ContractRegistry(
        registry_version="synthetic-rules",
        entries=(_rule(RegistryVerification.VERIFIED if ready
                       else RegistryVerification.UNVERIFIED),),
    )
    audit = audit_research_inputs(split=split, snapshots=snapshots, registry=registry)
    plan = build_default_sensitivity_plan(
        split=split, audit=audit, strategy_rules_sha256=_hash("synthetic-rules"),
    )
    parameter = build_dev_parameter_version(
        plan=plan, candidate_hash=plan.candidates[0].candidate_hash,
    )
    return {"split": split, "plan": plan, "parameter": parameter, "snapshots": snapshots}


def test_exact_obligations_and_cutover_ownership(tmp_path: Path) -> None:
    context = _scan_context()
    result = build_dev_scan_plan(**context)
    assert [day.time_count for day in result.days] == [96, 95]
    assert result.expected_record_count == 191
    assert result.days[0].first_confirmation == datetime(2024, 1, 1, 0, 15, tzinfo=UTC)
    assert result.days[0].end_exclusive == datetime(2024, 1, 2, 0, 15, tzinfo=UTC)
    assert result.days[-1].end_exclusive == datetime(2024, 1, 3, tzinfo=UTC)
    assert result.scan_executed is result.research_authorized is False
    assert result.upstream_blockers == ()
    context["snapshots"] = tuple(reversed(context["snapshots"]))
    assert build_dev_scan_plan(**context) == result
    path = write_dev_scan_plan(result, tmp_path)
    assert write_dev_scan_plan(result, tmp_path) == path
    assert DevScanPlan.model_validate_json(path.read_bytes()) == result
    path.write_bytes(b"tampered")
    with pytest.raises(ReplayDataInputError, match="changed"):
        write_dev_scan_plan(result, tmp_path)


@pytest.mark.parametrize("mode", ["missing", "duplicate", "version"])
def test_missing_duplicate_or_different_snapshot_sequence_rejected(mode: str) -> None:
    context = _scan_context()
    snapshots = context["snapshots"]
    if mode == "missing":
        context["snapshots"] = snapshots[:-1]
    elif mode == "duplicate":
        context["snapshots"] = (*snapshots[:-1], snapshots[0])
    else:
        context["snapshots"] = (
            snapshots[0].model_copy(update={"universe_version": _hash("different")}),
            *snapshots[1:],
        )
    with pytest.raises(ReplayDataInputError, match="snapshot"):
        build_dev_scan_plan(**context)


def test_member_content_is_bound_even_when_version_ids_do_not_change() -> None:
    context = _scan_context()
    original = build_dev_scan_plan(**context)
    first, *rest = context["snapshots"]
    changed = first.model_copy(update={
        "members": (first.members[0].model_copy(update={"symbol": "BBBUSDT"}),),
    })
    context["snapshots"] = (changed, *rest)
    result = build_dev_scan_plan(**context)
    assert result.daily_snapshot_hash == original.daily_snapshot_hash
    assert result.source_snapshot_content_hash != original.source_snapshot_content_hash
    assert result.days[0].universe_content_hash != original.days[0].universe_content_hash
    assert result.plan_hash != original.plan_hash


def test_empty_members_are_explicit_zero_obligations_not_execution() -> None:
    context = _scan_context()
    context["snapshots"] = tuple(
        item.model_copy(update={"member_count": 0, "members": ()})
        for item in context["snapshots"]
    )
    result = build_dev_scan_plan(**context)
    assert result.expected_record_count == 0
    assert result.scan_executed is result.research_authorized is False


def test_upstream_blockers_are_preserved_in_planning_metadata() -> None:
    context = _scan_context(ready=False)
    result = build_dev_scan_plan(**context)
    assert result.upstream_blockers == tuple(sorted(context["plan"].blockers))
    assert result.upstream_blockers
    assert result.scan_executed is result.research_authorized is False


def test_other_parameter_plan_and_bypassed_model_validation_are_rejected() -> None:
    context = _scan_context()
    context["parameter"] = _scan_context(ready=False)["parameter"]
    with pytest.raises(ValueError, match="does not belong"):
        build_dev_scan_plan(**context)
    context = _scan_context()
    context["plan"] = context["plan"].model_copy(update={"blockers": ("changed",)})
    with pytest.raises(ValidationError):
        build_dev_scan_plan(**context)


@pytest.mark.parametrize("updates", [
    {"symbols": ("AAAUSDT", "AAAUSDT")}, {"record_count": 1}, {"time_count": 1},
    {"first_confirmation": datetime(2024, 1, 1, 0, 15)},
    {"first_confirmation": datetime(2024, 1, 1, 8, 15,
                                    tzinfo=timezone(timedelta(hours=8)))},
    {"end_exclusive": datetime(2024, 1, 2, 0, 30, tzinfo=UTC)},
])
def test_invalid_day_obligations_rejected(updates: dict[str, Any]) -> None:
    day = build_dev_scan_plan(**_scan_context()).days[0].model_dump(mode="json")
    with pytest.raises(ValidationError):
        DevScanDayPlan.model_validate({**day, **updates})


@pytest.mark.parametrize("updates", [
    {"expected_record_count": 0}, {"scan_executed": True},
    {"research_authorized": True}, {"plan_hash": "0" * 64},
])
def test_plan_cannot_be_tampered_or_upgraded_to_execution(updates: dict[str, Any]) -> None:
    plan = build_dev_scan_plan(**_scan_context()).model_dump(mode="json")
    with pytest.raises(ValidationError):
        DevScanPlan.model_validate({**plan, **updates})
