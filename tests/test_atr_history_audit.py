from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from slagalpha.reporting.run_manifest import canonical_json_bytes
from slagalpha.research.atr_history_audit import (
    AtrHistoryAuditError,
    LifecycleWarmupInput,
    build_atr_history_seed_audit,
    write_atr_history_seed_audit,
)
from slagalpha.research.lifecycle_replacement import LifecycleReplacementNormalizationResult


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _replacement() -> LifecycleReplacementNormalizationResult:
    payload: dict[str, Any] = {
        "source_normalization_result_hash": _hash("normalization"),
        "source_dataset_content_hash": _hash("dataset"),
        "remediation_plan_hash": _hash("plan"),
        "real_execution_receipt_hash": _hash("receipt"),
        "overlay_policy": "DERIVATIVE_SHADOWS_FROZEN_SAME_PARTITION",
        "requested_partition_count": 6,
        "source_available_partition_count": 3,
        "resolved_failure_count": 0,
        "shadowed_daily_partition_count": 1,
        "materialized_overlay_partition_count": 1,
        "excluded_overlay_partition_count": 0,
        "replacement_available_partition_count": 3,
        "unavailable_partition_count": 3,
        "source_normalized_row_count": 100,
        "shadowed_daily_source_row_count": 10,
        "derivative_retained_row_count": 20,
        "replacement_row_count": 110,
        "resolved_failure_identities": (),
        "shadowed_daily_partition_identities": ("TESTUSDT/1d/2025-01",),
        "excluded_partition_identities": (),
        "remaining_failure_identities": (
            "AERGOUSDT/15m/2025-04",
            "AERGOUSDT/1h/2025-04",
            "AERGOUSDT/4h/2025-04",
        ),
        "replacement_dataset_content_hash": _hash("replacement"),
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
    candidate = LifecycleReplacementNormalizationResult.model_construct(
        **payload, result_hash="0" * 64
    )
    digest = hashlib.sha256(
        canonical_json_bytes(candidate.model_dump(mode="json", exclude={"result_hash"}))
    ).hexdigest()
    return LifecycleReplacementNormalizationResult.model_validate(
        {**payload, "result_hash": digest}
    )


def _input(
    *, count: int = 180, complete: bool = True, contiguous: bool = True
) -> LifecycleWarmupInput:
    return LifecycleWarmupInput(
        identity="TESTUSDT/1d",
        lifecycle_start=datetime(2025, 1, 1, tzinfo=UTC),
        verified_post_cutoff_row_count=count,
        complete_same_lifecycle_prefix=complete,
        contiguous_same_lifecycle_prefix=contiguous,
        source_reference=_hash("source"),
    )


def test_sufficient_post_cutoff_prefix_is_provable_but_not_authorized() -> None:
    replacement = _replacement()
    report = build_atr_history_seed_audit(
        replacement=replacement,
        expected_replacement_hash=replacement.result_hash,
        inputs=(_input(),),
    )
    assert report.status == "CHECKS_PASSED"
    assert report.blockers == ()
    assert report.sufficient_stream_count == 1
    assert report.streams[0].first_usable_open_time == datetime(2025, 6, 30, tzinfo=UTC)
    assert report.history_seed_authorized is False


def test_non_aligned_lifecycle_uses_conservative_interval_prefix_start() -> None:
    replacement = _replacement()
    item = _input().model_copy(update={
        "lifecycle_start": datetime(2025, 1, 1, 0, 15, tzinfo=UTC),
        "warmup_prefix_start": datetime(2025, 1, 2, tzinfo=UTC),
    })
    report = build_atr_history_seed_audit(
        replacement=replacement,
        expected_replacement_hash=replacement.result_hash,
        inputs=(item,),
    )
    assert report.schema_version == "atr-history-seed-audit/0.2.0"
    assert report.streams[0].first_usable_open_time == datetime(2025, 7, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("count", "complete", "contiguous", "reason"),
    [
        (179, True, True, "INSUFFICIENT_POST_CUTOFF_WARMUP"),
        (180, False, True, "SAME_LIFECYCLE_PREFIX_NOT_PROVEN"),
        (180, True, False, "SAME_LIFECYCLE_CONTINUITY_NOT_PROVEN"),
        (0, True, True, "NO_POST_CUTOFF_ROWS_VERIFIED"),
    ],
)
def test_unproven_warmup_fails_closed(
    count: int, complete: bool, contiguous: bool, reason: str
) -> None:
    replacement = _replacement()
    report = build_atr_history_seed_audit(
        replacement=replacement,
        expected_replacement_hash=replacement.result_hash,
        inputs=(_input(count=count, complete=complete, contiguous=contiguous),),
    )
    assert report.status == "BLOCKED"
    assert reason in report.streams[0].reason_codes
    assert "ATR_HISTORY_WARMUP_NOT_PROVEN_AFTER_CUTOFF" in report.blockers


def test_unresolved_lifecycle_and_missing_action_are_rejected() -> None:
    replacement = _replacement()
    with pytest.raises(AtrHistoryAuditError, match="cover replacement"):
        build_atr_history_seed_audit(
            replacement=replacement,
            expected_replacement_hash=replacement.result_hash,
            inputs=(),
        )
    report = build_atr_history_seed_audit(
        replacement=replacement,
        expected_replacement_hash=replacement.result_hash,
        inputs=(_input(),),
        unresolved_lifecycle_identities=("AERGOUSDT/15m/2025-04",),
    )
    assert "UNRESOLVED_LIFECYCLE_IDENTITY:AERGOUSDT/15m/2025-04" in report.blockers


def test_report_writer_is_immutable(tmp_path: Path) -> None:
    replacement = _replacement()
    report = build_atr_history_seed_audit(
        replacement=replacement,
        expected_replacement_hash=replacement.result_hash,
        inputs=(_input(),),
    )
    path = write_atr_history_seed_audit(report, tmp_path)
    assert path.exists()
    assert write_atr_history_seed_audit(report, tmp_path) == path
    path.write_bytes(b"changed")
    with pytest.raises(AtrHistoryAuditError, match="changed"):
        write_atr_history_seed_audit(report, tmp_path)
