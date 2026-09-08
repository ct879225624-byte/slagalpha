"""Generate a blocked, content-addressed lifecycle normalization plan."""

from __future__ import annotations

import json
from pathlib import Path

from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.research.lifecycle_boundaries import LifecycleBoundaryAuditReport
from slagalpha.research.lifecycle_remediation import (
    build_lifecycle_normalization_remediation_plan,
    write_lifecycle_normalization_remediation_plan,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "data" / "manifests"
NORMALIZATION_HASH = "c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2"
LIFECYCLE_AUDIT_HASH = "a8caec19488f3e875da8f2c548c8d7c7214c2365765b6bf7d3ae02282212c818"
REQUIRED_UNRESOLVED_SYMBOLS = ("AERGOUSDT",)


def main() -> int:
    normalization = NormalizationBatchResult.model_validate_json(
        (MANIFESTS / "normalization_batch" / f"{NORMALIZATION_HASH}.json").read_bytes()
    )
    lifecycle_audit = LifecycleBoundaryAuditReport.model_validate_json(
        (
            MANIFESTS
            / "lifecycle_boundary_audit"
            / f"{LIFECYCLE_AUDIT_HASH}.json"
        ).read_bytes()
    )
    plan = build_lifecycle_normalization_remediation_plan(
        normalization=normalization,
        lifecycle_audit=lifecycle_audit,
        expected_normalization_result_hash=NORMALIZATION_HASH,
        expected_lifecycle_audit_hash=LIFECYCLE_AUDIT_HASH,
        required_unresolved_symbols=REQUIRED_UNRESOLVED_SYMBOLS,
    )
    path = write_lifecycle_normalization_remediation_plan(
        plan,
        ROOT / "data",
        normalization=normalization,
        lifecycle_audit=lifecycle_audit,
        expected_normalization_result_hash=NORMALIZATION_HASH,
        expected_lifecycle_audit_hash=LIFECYCLE_AUDIT_HASH,
        required_unresolved_symbols=REQUIRED_UNRESOLVED_SYMBOLS,
    )
    print(
        json.dumps(
            {
                "status": plan.status,
                "plan_hash": plan.plan_hash,
                "target_symbol_count": plan.target_symbol_count,
                "planned_symbol_count": plan.planned_symbol_count,
                "excluded_symbol_count": plan.excluded_symbol_count,
                "action_count": plan.action_count,
                "aligned_boundary_action_count": plan.aligned_boundary_action_count,
                "partial_boundary_action_count": plan.partial_boundary_action_count,
                "whole_boundary_partition_exclusion_count": (
                    plan.whole_boundary_partition_exclusion_count
                ),
                "boundary_month_source_row_count": plan.boundary_month_source_row_count,
                "boundary_month_excluded_row_count": plan.boundary_month_excluded_row_count,
                "boundary_month_expected_retained_row_count": (
                    plan.boundary_month_expected_retained_row_count
                ),
                "settled_evidence_row_count": plan.settled_evidence_row_count,
                "planned_symbols": list(plan.planned_symbols),
                "excluded_symbols": [item.symbol for item in plan.excluded_symbols],
                "blockers": list(plan.blockers),
                "normalization_execution_authorized": False,
                "output_materialized": False,
                "atr_reset_authorized": False,
                "history_seed_authorized": False,
                "historical_rule_gate_relaxation_authorized": False,
                "research_authorized": False,
                "strategy_executed": False,
                "locked_test_consumed": False,
                "plan_path": str(path.relative_to(ROOT)),
            },
            sort_keys=True,
        )
    )
    return 1 if plan.status == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
