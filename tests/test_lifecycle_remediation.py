"""Synthetic tests for non-executing lifecycle normalization remediation plans."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.lifecycle_boundaries import (
    LifecycleBoundaryAuditReport,
    build_lifecycle_boundary_audit,
)
from slagalpha.research.lifecycle_remediation import (
    LifecycleNormalizationRemediationPlan,
    LifecycleRemediationPlanError,
    build_lifecycle_normalization_remediation_plan,
    write_lifecycle_normalization_remediation_plan,
)
from test_lifecycle_boundaries import (
    EFFECTIVE_FROM,
    PERIOD,
    SYMBOL,
    _archive,
    _normalization,
    _registry,
    _write_boundary_archives,
)
from test_research_splits import _hash


def _trusted_sources(
    root: Path, *, include_identity: bool = True
) -> tuple[NormalizationBatchResult, LifecycleBoundaryAuditReport]:
    normalization = _normalization()
    result_payload = {
        "plan_content_hash": normalization.plan_content_hash,
        "requested_count": normalization.requested_count,
        "normalized_count": normalization.normalized_count,
        "reused_count": normalization.reused_count,
        "failed_count": normalization.failed_count,
        "normalized_row_count": normalization.normalized_row_count,
        "normalized_parquet_bytes": normalization.normalized_parquet_bytes,
        "dataset_content_hash": normalization.dataset_content_hash,
        "failures": [item.model_dump(mode="json") for item in normalization.failures],
    }
    result_hash = hashlib.sha256(
        json.dumps(
            result_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    normalization = normalization.model_copy(update={"result_hash": result_hash})
    audit = build_lifecycle_boundary_audit(
        normalization=normalization,
        identity_registry=_registry(include_symbol=include_identity),
        target_symbols=(SYMBOL,),
        monthly_klines_dir=root,
    )
    return normalization, audit


def _build_plan(
    normalization: NormalizationBatchResult,
    audit: LifecycleBoundaryAuditReport,
    *,
    required_unresolved_symbols: tuple[str, ...] = (),
) -> LifecycleNormalizationRemediationPlan:
    return build_lifecycle_normalization_remediation_plan(
        normalization=normalization,
        lifecycle_audit=audit,
        expected_normalization_result_hash=normalization.result_hash,
        expected_lifecycle_audit_hash=audit.report_hash,
        required_unresolved_symbols=required_unresolved_symbols,
    )


def test_confirmed_boundary_produces_conservative_non_executing_actions(
    tmp_path: Path,
) -> None:
    _write_boundary_archives(tmp_path)
    normalization, audit = _trusted_sources(tmp_path)

    plan = _build_plan(normalization, audit)

    assert plan.action_count == 4
    assert plan.planned_symbols == (SYMBOL,)
    assert plan.excluded_symbols == ()
    assert tuple(action.interval for action in plan.actions) == (
        "15m",
        "1h",
        "4h",
        "1d",
    )
    by_interval = {action.interval: action for action in plan.actions}
    assert by_interval["15m"].retain_from_open_time == EFFECTIVE_FROM
    assert by_interval["1h"].retain_from_open_time == EFFECTIVE_FROM
    assert by_interval["4h"].source_boundary_bucket_open_time == datetime(
        2025, 4, 16, 8, tzinfo=UTC
    )
    assert by_interval["4h"].retain_from_open_time == datetime(
        2025, 4, 16, 12, tzinfo=UTC
    )
    assert by_interval["1d"].retain_from_open_time == datetime(
        2025, 4, 17, tzinfo=UTC
    )
    assert by_interval["15m"].boundary_bucket_disposition == (
        "RETAIN_ALIGNED_BOUNDARY_BUCKET"
    )
    assert by_interval["4h"].boundary_bucket_disposition == (
        "EXCLUDE_PARTIAL_BOUNDARY_BUCKET"
    )
    assert by_interval["1d"].boundary_bucket_disposition == (
        "EXCLUDE_PARTIAL_BOUNDARY_BUCKET"
    )
    assert plan.aligned_boundary_action_count == 2
    assert plan.partial_boundary_action_count == 2
    assert plan.boundary_month_source_row_count == 12
    assert plan.boundary_month_excluded_row_count == 6
    assert plan.boundary_month_expected_retained_row_count == 6
    assert plan.settled_evidence_row_count == 7
    assert all(
        action.exclude_before_open_time == action.retain_from_open_time
        and action.settled_symbol == f"{SYMBOL}SETTLED"
        and action.partition_scope == "ALL_PRIMARY_SYMBOL_PARTITIONS"
        and action.settled_archive_disposition == "EVIDENCE_ONLY_NEVER_MERGE"
        and action.state_initialization
        == "UNRESOLVED_NO_RESET_OR_SEED_AUTHORIZED"
        for action in plan.actions
    )
    assert all(
        action.audit_reason_codes == ("PRIMARY_ARCHIVE_CONTAINS_PRE_BOUNDARY_ROWS",)
        for action in plan.actions
    )
    assert plan.status == "BLOCKED"
    assert plan.source_normalization_result_preserved is True
    assert plan.new_content_addressed_result_required is True
    assert plan.plan_executed is False
    assert plan.normalization_execution_authorized is False
    assert plan.atr_reset_authorized is False
    assert plan.history_seed_authorized is False
    assert plan.historical_rule_gate_relaxation_authorized is False
    assert plan.research_authorized is False
    assert plan.strategy_executed is False
    assert plan.locked_test_consumed is False

    path = write_lifecycle_normalization_remediation_plan(
        plan,
        tmp_path / "data",
        normalization=normalization,
        lifecycle_audit=audit,
        expected_normalization_result_hash=normalization.result_hash,
        expected_lifecycle_audit_hash=audit.report_hash,
        required_unresolved_symbols=(),
    )
    assert (
        write_lifecycle_normalization_remediation_plan(
            plan,
            tmp_path / "data",
            normalization=normalization,
            lifecycle_audit=audit,
            expected_normalization_result_hash=normalization.result_hash,
            expected_lifecycle_audit_hash=audit.report_hash,
            required_unresolved_symbols=(),
        )
        == path
    )
    assert LifecycleNormalizationRemediationPlan.model_validate_json(path.read_bytes()) == plan
    path.write_bytes(b"tampered")
    with pytest.raises(LifecycleRemediationPlanError, match="existing lifecycle"):
        write_lifecycle_normalization_remediation_plan(
            plan,
            tmp_path / "data",
            normalization=normalization,
            lifecycle_audit=audit,
            expected_normalization_result_hash=normalization.result_hash,
            expected_lifecycle_audit_hash=audit.report_hash,
            required_unresolved_symbols=(),
        )
    payload = plan.model_dump(mode="json")
    payload["source_dataset_content_hash"] = "a" * 64
    with pytest.raises(ValidationError, match="content hash"):
        LifecycleNormalizationRemediationPlan.model_validate(payload)


def test_unresolved_symbol_is_excluded_and_never_receives_an_action(tmp_path: Path) -> None:
    _write_boundary_archives(tmp_path)
    normalization, audit = _trusted_sources(tmp_path, include_identity=False)

    plan = _build_plan(
        normalization,
        audit,
        required_unresolved_symbols=(SYMBOL,),
    )

    assert plan.actions == ()
    assert plan.planned_symbols == ()
    assert len(plan.excluded_symbols) == 1
    excluded = plan.excluded_symbols[0]
    assert excluded.symbol == SYMBOL
    assert excluded.disposition == "EXCLUDE_ALL_INTERVALS"
    assert excluded.required_evidence == (
        "REPEAT_LIFECYCLE_BOUNDARY_AUDIT",
        "VERIFIED_IDENTITY_EFFECTIVE_FROM",
    )
    assert f"UNRESOLVED_LIFECYCLE_SYMBOL_EXCLUDED:{SYMBOL}" in plan.blockers
    assert plan.research_authorized is False


def test_required_unresolved_symbol_cannot_be_silently_promoted(tmp_path: Path) -> None:
    _write_boundary_archives(tmp_path)
    normalization, audit = _trusted_sources(tmp_path)

    with pytest.raises(LifecycleRemediationPlanError, match="unresolved symbol set"):
        build_lifecycle_normalization_remediation_plan(
            normalization=normalization,
            lifecycle_audit=audit,
            expected_normalization_result_hash=normalization.result_hash,
            expected_lifecycle_audit_hash=audit.report_hash,
            required_unresolved_symbols=(SYMBOL,),
        )


def test_mismatched_normalization_or_partial_bucket_tamper_fails_closed(
    tmp_path: Path,
) -> None:
    _write_boundary_archives(tmp_path)
    normalization, audit = _trusted_sources(tmp_path)
    forged_normalization = normalization.model_copy(update={"result_hash": _hash("other")})
    with pytest.raises(LifecycleRemediationPlanError, match="normalization result"):
        build_lifecycle_normalization_remediation_plan(
            normalization=forged_normalization,
            lifecycle_audit=audit,
            expected_normalization_result_hash=normalization.result_hash,
            expected_lifecycle_audit_hash=audit.report_hash,
            required_unresolved_symbols=(),
        )

    plan = _build_plan(normalization, audit)
    payload = plan.model_dump(mode="json")
    four_hour = next(
        action for action in payload["actions"] if action["interval"] == "4h"
    )
    four_hour["retain_from_open_time"] = four_hour["source_boundary_bucket_open_time"]
    four_hour["exclude_before_open_time"] = four_hour[
        "source_boundary_bucket_open_time"
    ]
    payload.pop("plan_hash")
    payload["plan_hash"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    with pytest.raises(ValidationError, match="conservative interval ceiling"):
        LifecycleNormalizationRemediationPlan.model_validate(payload)


def test_internally_rehashed_audit_and_plan_cannot_replace_trusted_sources(
    tmp_path: Path,
) -> None:
    _write_boundary_archives(tmp_path)
    normalization, audit = _trusted_sources(tmp_path)

    audit_payload = audit.model_dump(mode="json", exclude={"report_hash"})
    audit_payload["symbols"][0]["intraday"][0]["primary_archive"][
        "source_sha256"
    ] = "f" * 64
    forged_audit = LifecycleBoundaryAuditReport.model_validate(
        {
            **audit_payload,
            "report_hash": hashlib.sha256(
                canonical_json_bytes(audit_payload)
            ).hexdigest(),
        }
    )
    with pytest.raises(LifecycleRemediationPlanError, match="unexpected lifecycle"):
        build_lifecycle_normalization_remediation_plan(
            normalization=normalization,
            lifecycle_audit=forged_audit,
            expected_normalization_result_hash=normalization.result_hash,
            expected_lifecycle_audit_hash=audit.report_hash,
            required_unresolved_symbols=(),
        )

    plan = _build_plan(normalization, audit)
    plan_payload = plan.model_dump(mode="json", exclude={"plan_hash"})
    plan_payload["actions"][0]["primary_archive_sha256"] = "e" * 64
    forged_plan = LifecycleNormalizationRemediationPlan.model_validate(
        {
            **plan_payload,
            "plan_hash": hashlib.sha256(
                canonical_json_bytes(plan_payload)
            ).hexdigest(),
        }
    )
    with pytest.raises(LifecycleRemediationPlanError, match="trusted source audit"):
        write_lifecycle_normalization_remediation_plan(
            forged_plan,
            tmp_path / "data",
            normalization=normalization,
            lifecycle_audit=audit,
            expected_normalization_result_hash=normalization.result_hash,
            expected_lifecycle_audit_hash=audit.report_hash,
            required_unresolved_symbols=(),
        )


def test_partial_daily_boundary_can_exclude_the_whole_boundary_partition(
    tmp_path: Path,
) -> None:
    _write_boundary_archives(tmp_path)
    _archive(
        tmp_path,
        SYMBOL,
        "1d",
        (
            datetime(2025, 4, 15, tzinfo=UTC),
            datetime(2025, 4, 16, tzinfo=UTC),
        ),
        price="1",
    )
    normalization, audit = _trusted_sources(tmp_path)

    plan = _build_plan(normalization, audit)
    daily = next(action for action in plan.actions if action.interval == "1d")

    assert daily.evidence_period == PERIOD
    assert daily.boundary_month_expected_retained_row_count == 0
    assert daily.boundary_month_whole_partition_excluded is True
    assert daily.boundary_month_operation == "EXCLUDE_PRIMARY_PARTITION"
    assert plan.whole_boundary_partition_exclusion_count == 1
