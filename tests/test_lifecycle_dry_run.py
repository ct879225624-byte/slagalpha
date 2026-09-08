"""Synthetic and frozen-manifest tests for the lifecycle dry-run design audit."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.lifecycle_dry_run import (
    LifecycleDryRunDesignError,
    LifecycleDryRunDesignReport,
    build_lifecycle_dry_run_design_report,
    write_lifecycle_dry_run_design_report,
)
from slagalpha.research.lifecycle_remediation import LifecycleNormalizationRemediationPlan

PLAN_HASH = "fcafcdb44b6102321e4c43378911b8302caa863d3e5f3dad1133a18c2c791a79"
NORMALIZATION_HASH = "c86dd5d2fa055d5bb02364c8abfa1d5e81998bccdfabfc0c1d2e9554832955d2"
LIFECYCLE_AUDIT_HASH = "a8caec19488f3e875da8f2c548c8d7c7214c2365765b6bf7d3ae02282212c818"


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


def test_real_manifest_design_audit_is_blocked_and_lineage_complete(tmp_path: Path) -> None:
    report = build_lifecycle_dry_run_design_report(
        plan=_plan(),
        expected_plan_hash=PLAN_HASH,
        expected_normalization_result_hash=NORMALIZATION_HASH,
        expected_lifecycle_boundary_audit_hash=LIFECYCLE_AUDIT_HASH,
    )

    assert report.status == "BLOCKED"
    assert report.namespace_isolated is True
    assert report.action_count == 32
    assert report.lineage_complete_action_count == 32
    assert report.source_row_count == 30_877
    assert report.excluded_row_count == 20_724
    assert report.retained_row_count == 10_153
    assert report.excluded_row_count + report.retained_row_count == report.source_row_count
    assert report.settled_evidence_row_count == 341
    assert report.raw_market_data_read is False
    assert report.derivative_executor_called is False
    assert report.output_materialized is False
    assert report.research_authorized is False

    path = write_lifecycle_dry_run_design_report(report, tmp_path / "data")
    assert write_lifecycle_dry_run_design_report(report, tmp_path / "data") == path
    assert LifecycleDryRunDesignReport.model_validate_json(path.read_bytes()) == report
    path.write_bytes(b"tampered")
    with pytest.raises(LifecycleDryRunDesignError, match="existing dry-run"):
        write_lifecycle_dry_run_design_report(report, tmp_path / "data")


def test_dry_run_design_rejects_hash_mismatch_and_namespace_collision() -> None:
    with pytest.raises(LifecycleDryRunDesignError, match="unexpected remediation"):
        build_lifecycle_dry_run_design_report(
            plan=_plan(),
            expected_plan_hash="0" * 64,
            expected_normalization_result_hash=NORMALIZATION_HASH,
            expected_lifecycle_boundary_audit_hash=LIFECYCLE_AUDIT_HASH,
        )

    with pytest.raises(LifecycleDryRunDesignError, match="namespaces collide"):
        build_lifecycle_dry_run_design_report(
            plan=_plan(),
            expected_plan_hash=PLAN_HASH,
            expected_normalization_result_hash=NORMALIZATION_HASH,
            expected_lifecycle_boundary_audit_hash=LIFECYCLE_AUDIT_HASH,
            derivative_normalization_namespace="data/normalized/klines",
        )


def test_rehashed_report_cannot_break_row_conservation() -> None:
    report = build_lifecycle_dry_run_design_report(
        plan=_plan(),
        expected_plan_hash=PLAN_HASH,
        expected_normalization_result_hash=NORMALIZATION_HASH,
        expected_lifecycle_boundary_audit_hash=LIFECYCLE_AUDIT_HASH,
    )
    payload = report.model_dump(mode="json", exclude={"report_hash"})
    payload["retained_row_count"] += 1
    payload["report_hash"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    with pytest.raises(ValidationError, match="rows are not conserved"):
        LifecycleDryRunDesignReport.model_validate(payload)
