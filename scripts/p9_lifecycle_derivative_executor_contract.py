"""Freeze the future lifecycle derivative executor interface without implementing it."""

from __future__ import annotations

import json
from pathlib import Path

from slagalpha.research.lifecycle_dry_run import LifecycleDryRunDesignReport
from slagalpha.research.lifecycle_executor_contract import (
    build_lifecycle_executor_interface_contract,
    write_lifecycle_executor_interface_contract,
)
from slagalpha.research.lifecycle_remediation import LifecycleNormalizationRemediationPlan

ROOT = Path(__file__).resolve().parents[1]
PLAN_HASH = "fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79"
DRY_RUN_HASH = "124c2c1ed41ff3008c61b39fb8f02b70e30e8a650c3d49961368255483fb1898"


def main() -> int:
    plan_path = (
        ROOT
        / "data"
        / "manifests"
        / "lifecycle_normalization_remediation_plan"
        / f"{PLAN_HASH}.json"
    )
    dry_run_path = (
        ROOT
        / "data"
        / "manifests"
        / "lifecycle_derivative_dry_run_design"
        / f"{DRY_RUN_HASH}.json"
    )
    plan = LifecycleNormalizationRemediationPlan.model_validate_json(plan_path.read_bytes())
    dry_run = LifecycleDryRunDesignReport.model_validate_json(dry_run_path.read_bytes())
    contract = build_lifecycle_executor_interface_contract(
        plan=plan,
        dry_run_report=dry_run,
        expected_plan_hash=PLAN_HASH,
        expected_dry_run_report_hash=DRY_RUN_HASH,
    )
    path = write_lifecycle_executor_interface_contract(contract, ROOT / "data")
    print(
        json.dumps(
            {
                "status": contract.status,
                "contract_hash": contract.contract_hash,
                "action_count": contract.action_count,
                "source_row_count": contract.expected_source_row_count,
                "excluded_row_count": contract.expected_excluded_row_count,
                "retained_row_count": contract.expected_retained_row_count,
                "input_mode": contract.input_mode,
                "settled_input_mode": contract.settled_input_mode,
                "publication_policy": contract.publication_policy,
                "executor_implementation_authorized": (
                    contract.executor_implementation_authorized
                ),
                "normalization_execution_authorized": (
                    contract.normalization_execution_authorized
                ),
                "output_materialized": contract.output_materialized,
                "research_authorized": contract.research_authorized,
                "blockers": list(contract.blockers),
                "contract_path": str(path.relative_to(ROOT)),
            },
            sort_keys=True,
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
