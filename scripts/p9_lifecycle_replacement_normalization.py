"""Build the non-destructive lifecycle replacement-normalization lineage."""

from __future__ import annotations

import json
from pathlib import Path

from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.research.lifecycle_real_execution import LifecycleRealExecutionReceipt
from slagalpha.research.lifecycle_remediation import LifecycleNormalizationRemediationPlan
from slagalpha.research.lifecycle_replacement import (
    build_lifecycle_replacement_normalization,
    write_lifecycle_replacement_normalization,
)

ROOT = Path(__file__).resolve().parents[1]
NORMALIZATION_HASH = "c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2"
PLAN_HASH = "fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79"
EXECUTION_HASH = "3d87c7db4e0a4287b12e6009cbc3e3139c2d65a0f7a8f1abcb7e87071038b86e"


def main() -> int:
    data_dir = ROOT / "data"
    manifests = data_dir / "manifests"
    normalization = NormalizationBatchResult.model_validate_json(
        (manifests / "normalization_batch" / f"{NORMALIZATION_HASH}.json").read_bytes()
    )
    plan = LifecycleNormalizationRemediationPlan.model_validate_json(
        (manifests / "lifecycle_normalization_remediation_plan" / f"{PLAN_HASH}.json").read_bytes()
    )
    execution = LifecycleRealExecutionReceipt.model_validate_json(
        (manifests / "lifecycle_real_execution" / f"{EXECUTION_HASH}.json").read_bytes()
    )
    result = build_lifecycle_replacement_normalization(
        normalization,
        plan,
        execution,
        expected_normalization_hash=NORMALIZATION_HASH,
        expected_plan_hash=PLAN_HASH,
        expected_execution_hash=EXECUTION_HASH,
    )
    path = write_lifecycle_replacement_normalization(result, data_dir)
    print(
        json.dumps(
            {
                "result_hash": result.result_hash,
                "path": str(path.relative_to(ROOT)),
                "requested_partition_count": result.requested_partition_count,
                "replacement_available_partition_count": (
                    result.replacement_available_partition_count
                ),
                "unavailable_partition_count": result.unavailable_partition_count,
                "replacement_row_count": result.replacement_row_count,
                "status": result.status,
                "blockers": result.blockers,
                "research_authorized": result.research_authorized,
                "strategy_executed": result.strategy_executed,
                "locked_test_consumed": result.locked_test_consumed,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
