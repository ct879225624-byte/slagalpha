"""Build a non-destructive overlay lineage for lifecycle-remediated normalization."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.lifecycle_real_execution import LifecycleRealExecutionReceipt
from slagalpha.research.lifecycle_remediation import LifecycleNormalizationRemediationPlan

_AERGO_FAILURES = (
    "AERGOUSDT/15m/2025-04",
    "AERGOUSDT/1h/2025-04",
    "AERGOUSDT/4h/2025-04",
)
_BLOCKERS = (
    "ATR_RESET_OR_HISTORY_SEED_NOT_AUTHORIZED",
    "FUNDING_INPUTS_MISSING",
    "HISTORICAL_RULE_GATE_REMAINS_BLOCKED",
    "ONE_MINUTE_INPUTS_MISSING",
    "UNRESOLVED_LIFECYCLE_SYMBOL:AERGOUSDT",
)


class LifecycleReplacementError(ValueError):
    """Raised when a lifecycle overlay cannot preserve complete lineage."""


class LifecycleReplacementNormalizationResult(BaseModel):
    """Content-addressed overlay of frozen normalization and lifecycle derivatives."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["lifecycle-replacement-normalization/0.1.0"] = (
        "lifecycle-replacement-normalization/0.1.0"
    )
    source_normalization_result_hash: str
    source_dataset_content_hash: str
    remediation_plan_hash: str
    real_execution_receipt_hash: str
    overlay_policy: Literal["DERIVATIVE_SHADOWS_FROZEN_SAME_PARTITION"] = (
        "DERIVATIVE_SHADOWS_FROZEN_SAME_PARTITION"
    )
    requested_partition_count: int = Field(gt=0)
    source_available_partition_count: int = Field(ge=0)
    resolved_failure_count: int = Field(ge=0)
    shadowed_daily_partition_count: int = Field(ge=0)
    materialized_overlay_partition_count: int = Field(ge=0)
    excluded_overlay_partition_count: int = Field(ge=0)
    replacement_available_partition_count: int = Field(ge=0)
    unavailable_partition_count: int = Field(ge=0)
    source_normalized_row_count: int = Field(ge=0)
    shadowed_daily_source_row_count: int = Field(ge=0)
    derivative_retained_row_count: int = Field(ge=0)
    replacement_row_count: int = Field(ge=0)
    resolved_failure_identities: tuple[str, ...]
    shadowed_daily_partition_identities: tuple[str, ...]
    excluded_partition_identities: tuple[str, ...]
    remaining_failure_identities: tuple[str, ...]
    replacement_dataset_content_hash: str
    frozen_normalization_preserved: Literal[True] = True
    replacement_dataset_view_materialized: Literal[False] = False
    atr_reset_authorized: Literal[False] = False
    history_seed_authorized: Literal[False] = False
    historical_rule_gate_relaxation_authorized: Literal[False] = False
    research_authorized: Literal[False] = False
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    status: Literal["BLOCKED"] = "BLOCKED"
    blockers: tuple[str, ...] = _BLOCKERS
    result_hash: str

    @field_validator(
        "source_normalization_result_hash",
        "source_dataset_content_hash",
        "remediation_plan_hash",
        "real_execution_receipt_hash",
        "replacement_dataset_content_hash",
        "result_hash",
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("replacement references must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        for identities in (
            self.resolved_failure_identities,
            self.shadowed_daily_partition_identities,
            self.excluded_partition_identities,
            self.remaining_failure_identities,
        ):
            if identities != tuple(sorted(set(identities))):
                raise ValueError("replacement identities must be unique and canonical")
        if self.remaining_failure_identities != _AERGO_FAILURES:
            raise ValueError("AERGO failures must remain unresolved")
        if self.blockers != _BLOCKERS:
            raise ValueError("replacement blockers must remain frozen")
        if self.resolved_failure_count != len(self.resolved_failure_identities):
            raise ValueError("resolved failure count does not reconcile")
        if self.shadowed_daily_partition_count != len(self.shadowed_daily_partition_identities):
            raise ValueError("shadowed daily partition count does not reconcile")
        if self.excluded_overlay_partition_count != len(self.excluded_partition_identities):
            raise ValueError("excluded overlay partition count does not reconcile")
        if self.materialized_overlay_partition_count + self.excluded_overlay_partition_count != (
            self.resolved_failure_count + self.shadowed_daily_partition_count
        ):
            raise ValueError("overlay action counts do not reconcile")
        if self.replacement_available_partition_count != (
            self.source_available_partition_count
            - self.shadowed_daily_partition_count
            + self.materialized_overlay_partition_count
        ):
            raise ValueError("replacement available count does not reconcile")
        if self.unavailable_partition_count != (
            len(self.remaining_failure_identities) + self.excluded_overlay_partition_count
        ):
            raise ValueError("unavailable partition count does not reconcile")
        if self.replacement_available_partition_count + self.unavailable_partition_count != (
            self.requested_partition_count
        ):
            raise ValueError("replacement partition counts do not reconcile")
        if self.replacement_row_count != (
            self.source_normalized_row_count
            - self.shadowed_daily_source_row_count
            + self.derivative_retained_row_count
        ):
            raise ValueError("replacement row count does not reconcile")
        payload = self.model_dump(mode="json", exclude={"result_hash"})
        if self.result_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("replacement result hash mismatch")
        return self


def build_lifecycle_replacement_normalization(
    normalization: NormalizationBatchResult,
    plan: LifecycleNormalizationRemediationPlan,
    execution: LifecycleRealExecutionReceipt,
    *,
    expected_normalization_hash: str,
    expected_plan_hash: str,
    expected_execution_hash: str,
) -> LifecycleReplacementNormalizationResult:
    normalization = NormalizationBatchResult.model_validate(normalization.model_dump(mode="json"))
    plan = LifecycleNormalizationRemediationPlan.model_validate(plan.model_dump(mode="json"))
    execution = LifecycleRealExecutionReceipt.model_validate(execution.model_dump(mode="json"))
    if normalization.result_hash != expected_normalization_hash:
        raise LifecycleReplacementError("unexpected source normalization")
    if plan.plan_hash != expected_plan_hash or execution.receipt_hash != expected_execution_hash:
        raise LifecycleReplacementError("unexpected lifecycle remediation input")
    if (
        plan.source_normalization_result_hash != normalization.result_hash
        or plan.source_dataset_content_hash != normalization.dataset_content_hash
        or execution.remediation_plan_hash != plan.plan_hash
        or execution.source_normalization_result_hash != normalization.result_hash
    ):
        raise LifecycleReplacementError("lifecycle replacement lineage does not reconcile")

    failures = {item.identity for item in normalization.failures}
    resolved = tuple(
        sorted(
            f"{action.symbol}/{action.interval}/{action.evidence_period}"
            for action in plan.actions
            if action.interval != "1d"
        )
    )
    if not set(resolved).issubset(failures) or tuple(sorted(failures - set(resolved))) != (
        _AERGO_FAILURES
    ):
        raise LifecycleReplacementError("resolved and remaining failures do not reconcile")
    daily = tuple(
        sorted(
            f"{action.symbol}/{action.interval}/{action.evidence_period}"
            for action in plan.actions
            if action.interval == "1d"
        )
    )
    output_keys = {
        f"{item.symbol}/{item.interval}/{item.evidence_period}" for item in execution.outputs
    }
    if output_keys != {
        f"{action.symbol}/{action.interval}/{action.evidence_period}" for action in plan.actions
    }:
        raise LifecycleReplacementError("real execution outputs do not cover every action")
    excluded = tuple(
        sorted(
            f"{item.symbol}/{item.interval}/{item.evidence_period}"
            for item in execution.outputs
            if item.status == "EXCLUDED_EMPTY_BOUNDARY_PARTITION"
        )
    )
    if not set(excluded).issubset(daily):
        raise LifecycleReplacementError("only daily boundary partitions may be excluded")
    daily_source_rows = sum(
        action.boundary_month_source_row_count for action in plan.actions if action.interval == "1d"
    )
    source_available = normalization.normalized_count + normalization.reused_count
    replacement_available = source_available - len(daily) + execution.materialized_action_count
    unavailable = normalization.requested_count - replacement_available
    dataset_payload = {
        "source_dataset_content_hash": normalization.dataset_content_hash,
        "real_execution_receipt_hash": execution.receipt_hash,
        "overlay_policy": "DERIVATIVE_SHADOWS_FROZEN_SAME_PARTITION",
        "shadowed_daily_partition_identities": daily,
        "excluded_partition_identities": excluded,
    }
    replacement_dataset_hash = hashlib.sha256(canonical_json_bytes(dataset_payload)).hexdigest()
    payload: dict[str, Any] = {
        "source_normalization_result_hash": normalization.result_hash,
        "source_dataset_content_hash": normalization.dataset_content_hash,
        "remediation_plan_hash": plan.plan_hash,
        "real_execution_receipt_hash": execution.receipt_hash,
        "overlay_policy": "DERIVATIVE_SHADOWS_FROZEN_SAME_PARTITION",
        "requested_partition_count": normalization.requested_count,
        "source_available_partition_count": source_available,
        "resolved_failure_count": len(resolved),
        "shadowed_daily_partition_count": len(daily),
        "materialized_overlay_partition_count": execution.materialized_action_count,
        "excluded_overlay_partition_count": execution.excluded_action_count,
        "replacement_available_partition_count": replacement_available,
        "unavailable_partition_count": unavailable,
        "source_normalized_row_count": normalization.normalized_row_count,
        "shadowed_daily_source_row_count": daily_source_rows,
        "derivative_retained_row_count": execution.retained_row_count,
        "replacement_row_count": (
            normalization.normalized_row_count - daily_source_rows + execution.retained_row_count
        ),
        "resolved_failure_identities": resolved,
        "shadowed_daily_partition_identities": daily,
        "excluded_partition_identities": excluded,
        "remaining_failure_identities": _AERGO_FAILURES,
        "replacement_dataset_content_hash": replacement_dataset_hash,
        "frozen_normalization_preserved": True,
        "replacement_dataset_view_materialized": False,
        "atr_reset_authorized": False,
        "history_seed_authorized": False,
        "historical_rule_gate_relaxation_authorized": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
        "status": "BLOCKED",
        "blockers": _BLOCKERS,
    }
    candidate = LifecycleReplacementNormalizationResult.model_construct(
        **payload, result_hash="0" * 64
    )
    result_hash = hashlib.sha256(
        canonical_json_bytes(candidate.model_dump(mode="json", exclude={"result_hash"}))
    ).hexdigest()
    return LifecycleReplacementNormalizationResult.model_validate(
        {**payload, "result_hash": result_hash}
    )


def write_lifecycle_replacement_normalization(
    result: LifecycleReplacementNormalizationResult, data_dir: Path
) -> Path:
    result = LifecycleReplacementNormalizationResult.model_validate(result.model_dump(mode="json"))
    destination = (
        data_dir
        / "manifests"
        / "lifecycle_replacement_normalization"
        / f"{result.result_hash}.json"
    )
    content = canonical_json_bytes(result.model_dump(mode="json"))
    if destination.is_symlink() or (destination.exists() and destination.read_bytes() != content):
        raise LifecycleReplacementError("existing lifecycle replacement changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
