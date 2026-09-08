"""Tests for the non-executing lifecycle derivative executor contract."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.lifecycle_dry_run import LifecycleDryRunDesignReport
from slagalpha.research.lifecycle_executor_contract import (
    LifecycleExecutorContractError,
    LifecycleExecutorInterfaceContract,
    build_lifecycle_executor_interface_contract,
    write_lifecycle_executor_interface_contract,
)
from slagalpha.research.lifecycle_remediation import LifecycleNormalizationRemediationPlan

PLAN_HASH = "fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79"
DRY_RUN_HASH = "124c2c1ed41ff3008c61b39fb8f02b70e30e8a650c3d49961368255483fb1898"


def _plan() -> LifecycleNormalizationRemediationPlan:
    root = Path(__file__).resolve().parents[1]
    path = (
        root
        / "data"
        / "manifests"
        / "lifecycle_normalization_remediation_plan"
        / f"{PLAN_HASH}.json"
    )
    return LifecycleNormalizationRemediationPlan.model_validate_json(path.read_bytes())


def _dry_run() -> LifecycleDryRunDesignReport:
    root = Path(__file__).resolve().parents[1]
    path = (
        root
        / "data"
        / "manifests"
        / "lifecycle_derivative_dry_run_design"
        / f"{DRY_RUN_HASH}.json"
    )
    return LifecycleDryRunDesignReport.model_validate_json(path.read_bytes())


def _contract() -> LifecycleExecutorInterfaceContract:
    return build_lifecycle_executor_interface_contract(
        plan=_plan(),
        dry_run_report=_dry_run(),
        expected_plan_hash=PLAN_HASH,
        expected_dry_run_report_hash=DRY_RUN_HASH,
    )


def _rehash(payload: dict[str, object]) -> dict[str, object]:
    unsigned = {key: value for key, value in payload.items() if key != "contract_hash"}
    payload["contract_hash"] = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    return payload


def test_real_manifests_freeze_blocked_executor_interface(tmp_path: Path) -> None:
    contract = _contract()

    assert contract.status == "BLOCKED"
    assert contract.action_count == 32
    assert contract.expected_source_row_count == 30_877
    assert contract.expected_excluded_row_count == 20_724
    assert contract.expected_retained_row_count == 10_153
    assert contract.frozen_namespace == "data/normalized/klines"
    assert contract.derivative_namespace == "data/normalized/lifecycle_scoped/v0.1.0"
    assert contract.settled_input_mode == "EVIDENCE_ONLY_NEVER_MERGE"
    assert contract.frozen_normalization_preserved is True
    assert contract.raw_archive_mutation_authorized is False
    assert contract.executor_implementation_authorized is False
    assert contract.normalization_execution_authorized is False
    assert contract.output_materialized is False
    assert contract.atr_reset_authorized is False
    assert contract.history_seed_authorized is False
    assert contract.historical_rule_gate_relaxation_authorized is False
    assert contract.research_authorized is False
    assert contract.strategy_executed is False
    assert contract.locked_test_consumed is False

    path = write_lifecycle_executor_interface_contract(contract, tmp_path / "data")
    assert write_lifecycle_executor_interface_contract(contract, tmp_path / "data") == path
    assert LifecycleExecutorInterfaceContract.model_validate_json(path.read_bytes()) == contract
    path.write_bytes(b"tampered")
    with pytest.raises(LifecycleExecutorContractError, match="existing executor"):
        write_lifecycle_executor_interface_contract(contract, tmp_path / "data")


def test_executor_contract_rejects_untrusted_hashes_and_lineage() -> None:
    with pytest.raises(LifecycleExecutorContractError, match="unexpected remediation"):
        build_lifecycle_executor_interface_contract(
            plan=_plan(),
            dry_run_report=_dry_run(),
            expected_plan_hash="0" * 64,
            expected_dry_run_report_hash=DRY_RUN_HASH,
        )
    with pytest.raises(LifecycleExecutorContractError, match="unexpected dry-run"):
        build_lifecycle_executor_interface_contract(
            plan=_plan(),
            dry_run_report=_dry_run(),
            expected_plan_hash=PLAN_HASH,
            expected_dry_run_report_hash="0" * 64,
        )


@pytest.mark.parametrize("field", ["required_checks", "blockers"])
def test_rehashed_contract_cannot_tamper_safety_lists(field: str) -> None:
    payload = _contract().model_dump(mode="json")
    payload[field] = payload[field][:-1]
    with pytest.raises(ValidationError, match="must (be complete|remain frozen)"):
        LifecycleExecutorInterfaceContract.model_validate(_rehash(payload))


def test_rehashed_contract_cannot_break_row_conservation() -> None:
    payload = _contract().model_dump(mode="json")
    payload["expected_retained_row_count"] += 1
    with pytest.raises(ValidationError, match="row counts do not reconcile"):
        LifecycleExecutorInterfaceContract.model_validate(_rehash(payload))
