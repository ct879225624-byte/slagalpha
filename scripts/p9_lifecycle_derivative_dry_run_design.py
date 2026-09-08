"""Audit the real-input lifecycle derivative dry-run design without executing it."""

from __future__ import annotations

import json
from pathlib import Path

from slagalpha.research.lifecycle_dry_run import (
    build_lifecycle_dry_run_design_report,
    write_lifecycle_dry_run_design_report,
)
from slagalpha.research.lifecycle_remediation import LifecycleNormalizationRemediationPlan

ROOT = Path(__file__).resolve().parents[1]
PLAN_HASH = "fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79"
NORMALIZATION_HASH = "c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2"
LIFECYCLE_AUDIT_HASH = "a8caec19488f3e875da8f2c548c8d7c7214c2365765b6bf7d3ae02282212c818"


def main() -> int:
    plan_path = (
        ROOT
        / "data"
        / "manifests"
        / "lifecycle_normalization_remediation_plan"
        / f"{PLAN_HASH}.json"
    )
    plan = LifecycleNormalizationRemediationPlan.model_validate_json(plan_path.read_bytes())
    report = build_lifecycle_dry_run_design_report(
        plan=plan,
        expected_plan_hash=PLAN_HASH,
        expected_normalization_result_hash=NORMALIZATION_HASH,
        expected_lifecycle_boundary_audit_hash=LIFECYCLE_AUDIT_HASH,
    )
    path = write_lifecycle_dry_run_design_report(report, ROOT / "data")
    print(
        json.dumps(
            {
                "status": report.status,
                "report_hash": report.report_hash,
                "action_count": report.action_count,
                "lineage_complete_action_count": report.lineage_complete_action_count,
                "source_row_count": report.source_row_count,
                "excluded_row_count": report.excluded_row_count,
                "retained_row_count": report.retained_row_count,
                "settled_evidence_row_count": report.settled_evidence_row_count,
                "namespace_isolated": report.namespace_isolated,
                "raw_market_data_read": report.raw_market_data_read,
                "derivative_executor_called": report.derivative_executor_called,
                "output_materialized": report.output_materialized,
                "research_authorized": report.research_authorized,
                "blockers": list(report.blockers),
                "report_path": str(path.relative_to(ROOT)),
            },
            sort_keys=True,
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
