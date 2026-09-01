"""Synthetic planning fixtures; no real strategy results or historical rules are fabricated."""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from slagalpha.backtest.replay import TradeReplayRequest
from slagalpha.research.sensitivity import (
    SensitivityCandidate,
    SensitivityParameters,
    SensitivityPlan,
    SensitivityPlanError,
    build_default_sensitivity_plan,
    default_candidates,
    require_dev_execution_inputs,
    write_sensitivity_plan,
)
from slagalpha.research.splits import (
    ResearchInputAuditReport,
    ResearchSplitManifest,
    build_research_split,
)
from slagalpha.strategy.pivots import detect_confirmed_pivots
from slagalpha.strategy.plans import build_entry_stop
from slagalpha.strategy.setup import evaluate_four_hour


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _split() -> ResearchSplitManifest:
    return build_research_split(
        research_start=date(2024, 1, 1),
        research_end_exclusive=date(2024, 1, 5),
        universe_batch_run_version=_hash("synthetic-run"),
        daily_snapshot_hash=_hash("synthetic-snapshots"),
    )


def _audit(*, ready: bool = False) -> ResearchInputAuditReport:
    split = _split()
    payload = {
        "split_hash": split.split_hash,
        "contract_registry_version": "synthetic-rules",
        "daily_snapshot_hash": split.daily_snapshot_hash,
        "roles": [{
            "role": segment.role.value,
            "expected_day_count": segment.day_count,
            "snapshot_day_count": segment.day_count,
            "member_day_count": segment.day_count,
            "rule_eligible_member_day_count": segment.day_count if ready else 0,
            "rule_blocked_member_day_count": 0 if ready else segment.day_count,
            "rule_reason_counts": {} if ready else {
                "CONTRACT_RULE_UNVERIFIED": segment.day_count
            },
            "readiness": "READY" if ready else "BLOCKED",
            "blockers": [] if ready else ["NO_VERIFIED_HISTORICAL_CONTRACT_RULE_MEMBER_DAYS"],
        } for segment in split.segments],
        "overall_readiness": "READY" if ready else "BLOCKED",
        "locked_test_consumed": False,
        "deferred_checks": [],
    }
    return ResearchInputAuditReport.model_validate({**payload, "report_hash": _hash(payload)})


def _plan(*, ready: bool = False) -> SensitivityPlan:
    return build_default_sensitivity_plan(
        split=_split(), audit=_audit(ready=ready), strategy_rules_sha256=_hash("synthetic-rules")
    )


def test_exactly_ten_default_centered_candidates_and_thirty_evaluations() -> None:
    plan = _plan()
    assert len(plan.candidates) == 10
    assert plan.planned_evaluation_count == 30
    assert len({candidate.candidate_hash for candidate in plan.candidates}) == 10
    assert plan.candidates[0].changed_parameter == "BASELINE"
    baseline = SensitivityParameters().model_dump()
    for candidate in plan.candidates[1:]:
        changed = [key for key, value in candidate.parameters.model_dump().items()
                   if value != baseline[key]]
        assert changed == [candidate.changed_parameter]
    assert default_candidates() == plan.candidates


def test_defaults_match_existing_strategy_and_replay_apis() -> None:
    parameters = SensitivityParameters()
    pivot_defaults = inspect.signature(detect_confirmed_pivots).parameters
    assert parameters.pivot_window == (
        pivot_defaults["left"].default, pivot_defaults["right"].default
    )
    setup_defaults = inspect.signature(evaluate_four_hour).parameters
    assert parameters.compression_threshold == Decimal(str(
        setup_defaults["compression_threshold"].default
    ))
    plan_defaults = inspect.signature(build_entry_stop).parameters
    assert parameters.stop_atr_multiplier == plan_defaults["stop_atr_multiplier"].default
    assert parameters.entry_ttl_bars == plan_defaults["entry_ttl_bars"].default
    assert parameters.max_holding_bars == (
        TradeReplayRequest.model_fields["max_holding_bars"].default
    )


@pytest.mark.parametrize("updates", [
    {"pivot_window": (2, 3)}, {"compression_threshold": "0.6"},
    {"stop_atr_multiplier": "0.12"}, {"entry_ttl_bars": 3},
    {"max_holding_bars": 24}, {"sma_periods": [20, 50]},
    {"entry_ttl_bars": True},
])
def test_unsupported_axes_and_values_are_rejected(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SensitivityParameters.model_validate(updates)


def test_decimal_spelling_does_not_change_candidate_hash() -> None:
    first = SensitivityParameters(compression_threshold=Decimal("0.5"))
    second = SensitivityParameters(compression_threshold=Decimal("0.500"))
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_multi_factor_candidate_is_rejected_even_with_matching_hash() -> None:
    payload = {
        "changed_parameter": "entry_ttl_bars",
        "parameters": SensitivityParameters(
            entry_ttl_bars=2, max_holding_bars=16
        ).model_dump(mode="json"),
    }
    with pytest.raises(ValidationError, match="exactly its one"):
        SensitivityCandidate.model_validate({**payload, "candidate_hash": _hash(payload)})


def test_blocked_dev_cannot_pass_and_plan_writes_are_immutable(tmp_path: Path) -> None:
    plan = _plan()
    with pytest.raises(SensitivityPlanError, match="DEV research blocked"):
        require_dev_execution_inputs(plan, _audit())
    path = write_sensitivity_plan(plan, tmp_path)
    assert write_sensitivity_plan(plan, tmp_path) == path
    assert SensitivityPlan.model_validate_json(path.read_text(encoding="utf-8")) == plan
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SensitivityPlanError, match="changed"):
        write_sensitivity_plan(plan, tmp_path)


def test_validated_fixture_can_pass_prerequisite_without_executing_strategy() -> None:
    plan = _plan(ready=True)
    require_dev_execution_inputs(plan, _audit(ready=True))
    assert plan.strategy_executed is False
    assert plan.locked_test_consumed is False


@pytest.mark.parametrize("role", ["VALIDATION", "LOCKED_TEST"])
def test_sensitivity_plan_rejects_non_dev_roles(role: str) -> None:
    payload = _plan().model_dump(mode="json")
    payload["dataset_role"] = role
    with pytest.raises(ValidationError):
        SensitivityPlan.model_validate(payload)


def test_modified_readiness_cannot_reuse_existing_audit_hash() -> None:
    payload = _audit(ready=True).model_dump(mode="json")
    payload["report_hash"] = _audit().report_hash
    with pytest.raises(ValidationError, match="content hash mismatch"):
        ResearchInputAuditReport.model_validate(payload)


def test_changed_cost_model_or_candidate_list_is_rejected() -> None:
    payload = _plan().model_dump(mode="json")
    payload["cost_rates"].pop("STRESS")
    with pytest.raises(ValidationError, match="three frozen"):
        SensitivityPlan.model_validate(payload)
    payload = _plan().model_dump(mode="json")
    payload["candidates"].pop()
    with pytest.raises(ValidationError, match="complete canonical"):
        SensitivityPlan.model_validate(payload)
