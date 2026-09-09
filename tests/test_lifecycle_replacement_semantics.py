"""Synthetic acceptance for replacement-aware DEV input semantics."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from slagalpha.data.normalization_batch import NormalizationBatchResult, NormalizationFailure
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.execution_inputs import (
    InputArtifactRole,
    InputArtifactSelection,
    inspect_dev_execution_inputs,
)
from slagalpha.research.execution_semantics import inspect_dev_execution_semantics
from slagalpha.research.lifecycle_real_execution import (
    LifecycleRealActionResult,
    LifecycleRealExecutionReceipt,
)
from slagalpha.research.lifecycle_replacement import (
    LifecycleReplacementNormalizationResult,
)
from test_execution_semantics import _core_fixture


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _replacement_fixture(
    root: Path,
    selections: tuple[InputArtifactSelection, ...],
) -> tuple[
    tuple[InputArtifactSelection, ...],
    LifecycleReplacementNormalizationResult,
    Path,
]:
    normalization = NormalizationBatchResult(
        plan_content_hash=_hash("normalization-plan"),
        requested_count=7,
        normalized_count=4,
        reused_count=0,
        failed_count=3,
        normalized_row_count=10,
        normalized_parquet_bytes=100,
        dataset_content_hash=_hash("source-dataset"),
        failures=(
            NormalizationFailure(identity="AERGOUSDT/15m/2025-04", error="lifecycle"),
            NormalizationFailure(identity="AERGOUSDT/1h/2025-04", error="lifecycle"),
            NormalizationFailure(identity="AERGOUSDT/4h/2025-04", error="lifecycle"),
        ),
        complete=False,
        result_hash=_hash("normalization-result"),
    )
    normalization_bytes = normalization.model_dump_json().encode()
    normalization_relative = "inputs/replacement-normalization.json"
    normalization_path = root / normalization_relative
    normalization_path.parent.mkdir(parents=True, exist_ok=True)
    normalization_path.write_bytes(normalization_bytes)
    selections = (
        *selections,
        InputArtifactSelection(
            role=InputArtifactRole.CANDLE_MULTI_TIMEFRAME,
            relative_path=normalization_relative,
            expected_sha256=hashlib.sha256(normalization_bytes).hexdigest(),
        ),
    )

    output_bytes = b"verified lifecycle derivative"
    output_relative = "data/normalized/lifecycle_scoped/v0.1.0/1d/TESTUSDT/test.parquet"
    output_path = root.joinpath(*Path(output_relative).parts)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(output_bytes)
    action = LifecycleRealActionResult(
        symbol="TESTUSDT",
        interval="1d",
        evidence_period="2025-01",
        action_hash=_hash("action"),
        primary_archive_sha256=_hash("primary-archive"),
        primary_rows_sha256=_hash("primary-rows"),
        settled_archive_sha256=_hash("settled-archive"),
        settled_rows_sha256=_hash("settled-rows"),
        source_row_count=1,
        excluded_row_count=0,
        retained_row_count=1,
        derivative_content_hash=_hash("derivative"),
        output_relative_path=output_relative,
        output_sha256=hashlib.sha256(output_bytes).hexdigest(),
        status="MATERIALIZED",
    )
    receipt_payload: dict[str, Any] = {
        "authorization_hash": _hash("authorization"),
        "executor_contract_hash": _hash("contract"),
        "remediation_plan_hash": _hash("remediation-plan"),
        "source_normalization_result_hash": normalization.result_hash,
        "action_count": 1,
        "materialized_action_count": 1,
        "excluded_action_count": 0,
        "source_row_count": 1,
        "excluded_row_count": 0,
        "retained_row_count": 1,
        "outputs": (action,),
        "frozen_normalization_preserved": True,
        "real_output_materialized": True,
        "atr_reset_authorized": False,
        "history_seed_authorized": False,
        "historical_rule_gate_relaxation_authorized": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
        "status": "MATERIALIZED_BOUNDARY_MONTHS",
    }
    receipt_candidate = LifecycleRealExecutionReceipt.model_construct(
        **receipt_payload, receipt_hash="0" * 64
    )
    receipt_hash = hashlib.sha256(
        canonical_json_bytes(receipt_candidate.model_dump(mode="json", exclude={"receipt_hash"}))
    ).hexdigest()
    receipt = LifecycleRealExecutionReceipt.model_validate(
        {**receipt_payload, "receipt_hash": receipt_hash}
    )
    receipt_path = root / "data" / "manifests" / "lifecycle_real_execution" / f"{receipt_hash}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(canonical_json_bytes(receipt.model_dump(mode="json")))

    replacement_payload: dict[str, Any] = {
        "source_normalization_result_hash": normalization.result_hash,
        "source_dataset_content_hash": normalization.dataset_content_hash,
        "remediation_plan_hash": receipt.remediation_plan_hash,
        "real_execution_receipt_hash": receipt.receipt_hash,
        "overlay_policy": "DERIVATIVE_SHADOWS_FROZEN_SAME_PARTITION",
        "requested_partition_count": 7,
        "source_available_partition_count": 4,
        "resolved_failure_count": 0,
        "shadowed_daily_partition_count": 1,
        "materialized_overlay_partition_count": 1,
        "excluded_overlay_partition_count": 0,
        "replacement_available_partition_count": 4,
        "unavailable_partition_count": 3,
        "source_normalized_row_count": 10,
        "shadowed_daily_source_row_count": 1,
        "derivative_retained_row_count": 1,
        "replacement_row_count": 10,
        "resolved_failure_identities": (),
        "shadowed_daily_partition_identities": ("TESTUSDT/1d/2025-01",),
        "excluded_partition_identities": (),
        "remaining_failure_identities": (
            "AERGOUSDT/15m/2025-04",
            "AERGOUSDT/1h/2025-04",
            "AERGOUSDT/4h/2025-04",
        ),
        "replacement_dataset_content_hash": _hash("replacement-dataset"),
        "frozen_normalization_preserved": True,
        "replacement_dataset_view_materialized": False,
        "atr_reset_authorized": False,
        "history_seed_authorized": False,
        "historical_rule_gate_relaxation_authorized": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
        "status": "BLOCKED",
        "blockers": (
            "ATR_RESET_OR_HISTORY_SEED_NOT_AUTHORIZED",
            "FUNDING_INPUTS_MISSING",
            "HISTORICAL_RULE_GATE_REMAINS_BLOCKED",
            "ONE_MINUTE_INPUTS_MISSING",
            "UNRESOLVED_LIFECYCLE_SYMBOL:AERGOUSDT",
        ),
    }
    replacement_candidate = LifecycleReplacementNormalizationResult.model_construct(
        **replacement_payload, result_hash="0" * 64
    )
    replacement_hash = hashlib.sha256(
        canonical_json_bytes(replacement_candidate.model_dump(mode="json", exclude={"result_hash"}))
    ).hexdigest()
    replacement = LifecycleReplacementNormalizationResult.model_validate(
        {**replacement_payload, "result_hash": replacement_hash}
    )
    return selections, replacement, output_path


def test_verified_lifecycle_replacement_is_recognized_but_stays_blocked(
    tmp_path: Path,
) -> None:
    plan, parameter, selections = _core_fixture(tmp_path)
    selections, replacement, _ = _replacement_fixture(tmp_path, selections)
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections
    )

    semantic = inspect_dev_execution_semantics(
        project_dir=tmp_path,
        plan=plan,
        parameter=parameter,
        content_report=content,
        replacement=replacement,
        expected_replacement_hash=replacement.result_hash,
    )

    assert semantic.schema_version == "dev-execution-semantics/0.2.0"
    assert semantic.replacement_lineage is not None
    assert semantic.replacement_lineage.verified_output_count == 1
    assert semantic.replacement_lineage.unavailable_partition_count == 3
    assert "RUN_INPUT_SEMANTIC_INCOMPLETE_LIFECYCLE_REPLACEMENT_CANDLE_MULTI_TIMEFRAME" in (
        semantic.blockers
    )
    assert "RUN_INPUT_REPLACEMENT:UNRESOLVED_LIFECYCLE_SYMBOL:AERGOUSDT" in semantic.blockers
    assert "RUN_INPUT_SEMANTIC_INCOMPLETE_OR_MISMATCH_CANDLE_MULTI_TIMEFRAME" not in (
        semantic.blockers
    )
    assert InputArtifactRole.CANDLE_MULTI_TIMEFRAME in semantic.deferred_roles
    assert semantic.status == "BLOCKED"
    assert semantic.research_authorized is False


def test_lifecycle_replacement_rehashes_derivative_outputs(tmp_path: Path) -> None:
    plan, parameter, selections = _core_fixture(tmp_path)
    selections, replacement, output_path = _replacement_fixture(tmp_path, selections)
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections
    )
    output_path.write_bytes(b"tampered")

    semantic = inspect_dev_execution_semantics(
        project_dir=tmp_path,
        plan=plan,
        parameter=parameter,
        content_report=content,
        replacement=replacement,
        expected_replacement_hash=replacement.result_hash,
    )

    assert semantic.schema_version == "dev-execution-semantics/0.2.0"
    assert semantic.replacement_lineage is None
    assert "RUN_INPUT_SEMANTIC_INVALID_LIFECYCLE_REPLACEMENT" in semantic.blockers
    assert "RUN_INPUT_SEMANTIC_INCOMPLETE_OR_MISMATCH_CANDLE_MULTI_TIMEFRAME" in (semantic.blockers)


def test_lifecycle_replacement_requires_trusted_hash(tmp_path: Path) -> None:
    plan, parameter, selections = _core_fixture(tmp_path)
    selections, replacement, _ = _replacement_fixture(tmp_path, selections)
    content = inspect_dev_execution_inputs(
        project_dir=tmp_path, plan=plan, parameter=parameter, selections=selections
    )

    with pytest.raises(ValueError, match="unexpected lifecycle replacement"):
        inspect_dev_execution_semantics(
            project_dir=tmp_path,
            plan=plan,
            parameter=parameter,
            content_report=content,
            replacement=replacement,
            expected_replacement_hash="0" * 64,
        )
