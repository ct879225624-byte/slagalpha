from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.research.lifecycle_real_execution import LifecycleRealExecutionReceipt
from slagalpha.research.lifecycle_remediation import LifecycleNormalizationRemediationPlan
from slagalpha.research.lifecycle_replacement import (
    LifecycleReplacementError,
    LifecycleReplacementNormalizationResult,
    build_lifecycle_replacement_normalization,
    write_lifecycle_replacement_normalization,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "data" / "manifests"
NORMALIZATION_HASH = "c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2"
PLAN_HASH = "fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79"
EXECUTION_HASH = "3d87c7db4e0a4287b12e6009cbc3e3139c2d65a0f7a8f1abcb7e87071038b86e"


def _trusted_inputs() -> tuple[
    NormalizationBatchResult,
    LifecycleNormalizationRemediationPlan,
    LifecycleRealExecutionReceipt,
]:
    normalization = NormalizationBatchResult.model_validate_json(
        (MANIFESTS / "normalization_batch" / f"{NORMALIZATION_HASH}.json").read_bytes()
    )
    plan = LifecycleNormalizationRemediationPlan.model_validate_json(
        (MANIFESTS / "lifecycle_normalization_remediation_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    execution = LifecycleRealExecutionReceipt.model_validate_json(
        (MANIFESTS / "lifecycle_real_execution" / f"{EXECUTION_HASH}.json").read_bytes()
    )
    return normalization, plan, execution


def _result() -> LifecycleReplacementNormalizationResult:
    normalization, plan, execution = _trusted_inputs()
    return build_lifecycle_replacement_normalization(
        normalization,
        plan,
        execution,
        expected_normalization_hash=NORMALIZATION_HASH,
        expected_plan_hash=PLAN_HASH,
        expected_execution_hash=EXECUTION_HASH,
    )


def test_builds_non_destructive_replacement_lineage() -> None:
    result = _result()

    assert result.requested_partition_count == 73_340
    assert result.source_available_partition_count == 73_313
    assert result.resolved_failure_count == 24
    assert result.shadowed_daily_partition_count == 8
    assert result.materialized_overlay_partition_count == 31
    assert result.excluded_overlay_partition_count == 1
    assert result.replacement_available_partition_count == 73_336
    assert result.unavailable_partition_count == 4
    assert result.source_normalized_row_count == 69_189_525
    assert result.shadowed_daily_source_row_count == 247
    assert result.derivative_retained_row_count == 10_153
    assert result.replacement_row_count == 69_199_431
    assert result.excluded_partition_identities == ("CTKUSDT/1d/2025-04",)
    assert result.remaining_failure_identities == (
        "AERGOUSDT/15m/2025-04",
        "AERGOUSDT/1h/2025-04",
        "AERGOUSDT/4h/2025-04",
    )
    assert result.frozen_normalization_preserved is True
    assert result.replacement_dataset_view_materialized is False
    assert result.status == "BLOCKED"
    assert result.research_authorized is False
    assert result.strategy_executed is False
    assert result.locked_test_consumed is False


def test_replacement_manifest_publication_is_idempotent_and_immutable(tmp_path: Path) -> None:
    result = _result()

    path = write_lifecycle_replacement_normalization(result, tmp_path)
    repeated = write_lifecycle_replacement_normalization(result, tmp_path)

    assert repeated == path
    assert path.stem == result.result_hash
    assert LifecycleReplacementNormalizationResult.model_validate_json(path.read_bytes()) == result

    path.write_bytes(b"changed")
    with pytest.raises(LifecycleReplacementError, match="changed"):
        write_lifecycle_replacement_normalization(result, tmp_path)


def test_replacement_rejects_untrusted_source_hash() -> None:
    normalization, plan, execution = _trusted_inputs()

    with pytest.raises(LifecycleReplacementError, match="unexpected source normalization"):
        build_lifecycle_replacement_normalization(
            normalization,
            plan,
            execution,
            expected_normalization_hash="0" * 64,
            expected_plan_hash=PLAN_HASH,
            expected_execution_hash=EXECUTION_HASH,
        )


def test_replacement_result_rejects_count_tampering() -> None:
    payload = _result().model_dump(mode="json")
    payload["replacement_available_partition_count"] += 1

    with pytest.raises(ValidationError, match="replacement available count"):
        LifecycleReplacementNormalizationResult.model_validate(payload)
