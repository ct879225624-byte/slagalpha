"""Build a non-executing plan for lifecycle-scoped normalization derivatives."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.normalization_batch import NormalizationBatchResult
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
from slagalpha.research.lifecycle_boundaries import (
    ArchiveFingerprint,
    ArchiveInterval,
    LifecycleBoundaryAuditReport,
    SymbolLifecycleBoundaryAudit,
)

BoundaryBucketDisposition = Literal[
    "RETAIN_ALIGNED_BOUNDARY_BUCKET",
    "EXCLUDE_PARTIAL_BOUNDARY_BUCKET",
]

_SYMBOL = re.compile(r"^[A-Z0-9]+USDT$")
_PERIOD = re.compile(r"^\d{4}-\d{2}$")
_INTERVAL_ORDER: tuple[ArchiveInterval, ...] = ("15m", "1h", "4h", "1d")
_INTERVAL_INDEX = {interval: index for index, interval in enumerate(_INTERVAL_ORDER)}
_INTERVAL_DELTA: dict[ArchiveInterval, timedelta] = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}
_MANDATORY_BLOCKERS = {
    "ATR_RESET_OR_HISTORY_SEED_NOT_AUTHORIZED",
    "DERIVATIVE_NORMALIZATION_NOT_EXECUTED",
    "HISTORICAL_RULE_GATE_REMAINS_BLOCKED",
}


class LifecycleRemediationPlanError(ValueError):
    """Raised when an audit cannot safely produce a remediation plan."""


def _require_sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("remediation references must be lowercase SHA-256")
    return value


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("remediation timestamps must use UTC")
    return value


def _floor_to_interval(value: datetime, interval: ArchiveInterval) -> datetime:
    step_ms = int(_INTERVAL_DELTA[interval].total_seconds() * 1000)
    value_ms = int(value.timestamp() * 1000)
    return datetime.fromtimestamp((value_ms - value_ms % step_ms) / 1000, UTC)


def _ceil_to_interval(value: datetime, interval: ArchiveInterval) -> datetime:
    floor = _floor_to_interval(value, interval)
    return floor if value == floor else floor + _INTERVAL_DELTA[interval]


class LifecycleIntervalRemediationAction(BaseModel):
    """One review-only cutoff for all primary partitions of an interval."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    settled_symbol: str
    interval: ArchiveInterval
    evidence_period: str
    identity_effective_from: datetime
    identity_source_ref: str
    source_boundary_bucket_open_time: datetime
    retain_from_open_time: datetime
    exclude_before_open_time: datetime
    boundary_bucket_disposition: BoundaryBucketDisposition
    boundary_month_source_row_count: int = Field(gt=0)
    boundary_month_excluded_row_count: int = Field(ge=0)
    boundary_month_expected_retained_row_count: int = Field(ge=0)
    boundary_month_whole_partition_excluded: bool = Field(strict=True)
    boundary_month_operation: Literal[
        "FILTER_PRIMARY_PARTITION", "EXCLUDE_PRIMARY_PARTITION"
    ]
    primary_archive_sha256: str
    primary_rows_sha256: str
    settled_archive_sha256: str
    settled_rows_sha256: str
    settled_evidence_row_count: int = Field(gt=0)
    audit_reason_codes: tuple[str, ...]
    partition_scope: Literal["ALL_PRIMARY_SYMBOL_PARTITIONS"] = (
        "ALL_PRIMARY_SYMBOL_PARTITIONS"
    )
    filter_operation: Literal["RETAIN_OPEN_TIME_GTE"] = "RETAIN_OPEN_TIME_GTE"
    settled_archive_disposition: Literal["EVIDENCE_ONLY_NEVER_MERGE"] = (
        "EVIDENCE_ONLY_NEVER_MERGE"
    )
    state_initialization: Literal["UNRESOLVED_NO_RESET_OR_SEED_AUTHORIZED"] = (
        "UNRESOLVED_NO_RESET_OR_SEED_AUTHORIZED"
    )
    requires_source_revalidation: Literal[True] = True

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if _SYMBOL.fullmatch(value) is None:
            raise ValueError("remediation symbol must be a canonical USDT symbol")
        return value

    @field_validator("settled_symbol")
    @classmethod
    def validate_settled_symbol(cls, value: str, info: Any) -> str:
        symbol = info.data.get("symbol")
        if symbol is not None and value != f"{symbol}SETTLED":
            raise ValueError("settled symbol must be the exact primary symbol plus SETTLED")
        return value

    @field_validator("audit_reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("action reason codes must be unique and canonical")
        return value

    @field_validator("evidence_period")
    @classmethod
    def validate_period(cls, value: str) -> str:
        if _PERIOD.fullmatch(value) is None:
            raise ValueError("remediation evidence period must use YYYY-MM")
        try:
            datetime.strptime(value, "%Y-%m")
        except ValueError as error:
            raise ValueError("remediation evidence period must be a real calendar month") from error
        return value

    @field_validator("identity_source_ref")
    @classmethod
    def validate_source_ref(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identity source reference must not be empty")
        return value

    @field_validator(
        "identity_effective_from",
        "source_boundary_bucket_open_time",
        "retain_from_open_time",
        "exclude_before_open_time",
    )
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator(
        "primary_archive_sha256",
        "primary_rows_sha256",
        "settled_archive_sha256",
        "settled_rows_sha256",
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _require_sha256(value)

    @model_validator(mode="after")
    def validate_cutoff(self) -> Self:
        expected_floor = _floor_to_interval(self.identity_effective_from, self.interval)
        if self.source_boundary_bucket_open_time != expected_floor:
            raise ValueError("source boundary bucket must contain identity effective_from")
        expected_retain = _ceil_to_interval(self.identity_effective_from, self.interval)
        if self.retain_from_open_time != expected_retain:
            raise ValueError("retain_from must use the conservative interval ceiling")
        if self.exclude_before_open_time != self.retain_from_open_time:
            raise ValueError("excluded and retained ranges must meet at one exact cutoff")
        expected_disposition = (
            "RETAIN_ALIGNED_BOUNDARY_BUCKET"
            if self.identity_effective_from == expected_floor
            else "EXCLUDE_PARTIAL_BOUNDARY_BUCKET"
        )
        if self.boundary_bucket_disposition != expected_disposition:
            raise ValueError("boundary bucket disposition disagrees with alignment")
        if (
            self.boundary_month_excluded_row_count
            + self.boundary_month_expected_retained_row_count
            != self.boundary_month_source_row_count
        ):
            raise ValueError("boundary-month row counts do not reconcile")
        if self.boundary_month_whole_partition_excluded != (
            self.boundary_month_expected_retained_row_count == 0
        ):
            raise ValueError("whole-partition disposition does not reconcile")
        expected_operation = (
            "EXCLUDE_PRIMARY_PARTITION"
            if self.boundary_month_whole_partition_excluded
            else "FILTER_PRIMARY_PARTITION"
        )
        if self.boundary_month_operation != expected_operation:
            raise ValueError("boundary-month operation does not reconcile")
        return self


class ExcludedLifecycleSymbol(BaseModel):
    """An unresolved symbol that cannot receive any cutoff action."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    intervals: tuple[ArchiveInterval, ...] = _INTERVAL_ORDER
    disposition: Literal["EXCLUDE_ALL_INTERVALS"] = "EXCLUDE_ALL_INTERVALS"
    audit_reason_codes: tuple[str, ...] = Field(min_length=1)
    required_evidence: tuple[
        Literal["REPEAT_LIFECYCLE_BOUNDARY_AUDIT"],
        Literal["VERIFIED_IDENTITY_EFFECTIVE_FROM"],
    ] = (
        "REPEAT_LIFECYCLE_BOUNDARY_AUDIT",
        "VERIFIED_IDENTITY_EFFECTIVE_FROM",
    )

    @model_validator(mode="after")
    def validate_exclusion(self) -> Self:
        if _SYMBOL.fullmatch(self.symbol) is None:
            raise ValueError("excluded symbol must be a canonical USDT symbol")
        if self.intervals != _INTERVAL_ORDER:
            raise ValueError("excluded intervals must cover canonical 15m/1h/4h/1d")
        if self.audit_reason_codes != tuple(sorted(set(self.audit_reason_codes))):
            raise ValueError("exclusion reason codes must be unique and canonical")
        return self


class LifecycleNormalizationRemediationPlan(BaseModel):
    """Content-addressed plan that cannot authorize or execute normalization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["lifecycle-normalization-remediation-plan/0.1.0"] = (
        "lifecycle-normalization-remediation-plan/0.1.0"
    )
    derivative_namespace: Literal["lifecycle-scoped-normalization/0.1.0"] = (
        "lifecycle-scoped-normalization/0.1.0"
    )
    source_normalization_result_hash: str
    source_normalization_plan_content_hash: str
    source_dataset_content_hash: str
    lifecycle_boundary_audit_hash: str
    identity_registry_hash: str
    target_symbol_count: int = Field(gt=0)
    planned_symbol_count: int = Field(ge=0)
    excluded_symbol_count: int = Field(ge=0)
    action_count: int = Field(ge=0)
    aligned_boundary_action_count: int = Field(ge=0)
    partial_boundary_action_count: int = Field(ge=0)
    whole_boundary_partition_exclusion_count: int = Field(ge=0)
    boundary_month_source_row_count: int = Field(ge=0)
    boundary_month_excluded_row_count: int = Field(ge=0)
    boundary_month_expected_retained_row_count: int = Field(ge=0)
    settled_evidence_row_count: int = Field(ge=0)
    planned_symbols: tuple[str, ...]
    excluded_symbols: tuple[ExcludedLifecycleSymbol, ...]
    actions: tuple[LifecycleIntervalRemediationAction, ...]
    status: Literal["BLOCKED"] = "BLOCKED"
    blockers: tuple[str, ...] = Field(min_length=1)
    source_normalization_result_preserved: Literal[True] = True
    new_content_addressed_result_required: Literal[True] = True
    source_raw_archive_mutation_authorized: Literal[False] = False
    plan_executed: Literal[False] = False
    output_materialized: Literal[False] = False
    market_data_download_authorized: Literal[False] = False
    normalization_execution_authorized: Literal[False] = False
    atr_reset_authorized: Literal[False] = False
    history_seed_authorized: Literal[False] = False
    historical_rule_gate_relaxation_authorized: Literal[False] = False
    research_authorized: Literal[False] = False
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    replacement_normalization_result_hash: None = None
    replacement_dataset_content_hash: None = None
    plan_hash: str

    @field_validator(
        "source_normalization_result_hash",
        "source_normalization_plan_content_hash",
        "source_dataset_content_hash",
        "lifecycle_boundary_audit_hash",
        "identity_registry_hash",
        "plan_hash",
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _require_sha256(value)

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        action_keys = tuple(
            (action.symbol, _INTERVAL_INDEX[action.interval]) for action in self.actions
        )
        if action_keys != tuple(sorted(set(action_keys))):
            raise ValueError("remediation actions must be unique and canonical")
        planned = tuple(sorted(set(action.symbol for action in self.actions)))
        excluded = tuple(item.symbol for item in self.excluded_symbols)
        if self.planned_symbols != planned:
            raise ValueError("planned symbols do not reconcile with actions")
        if excluded != tuple(sorted(set(excluded))):
            raise ValueError("excluded symbols must be unique and canonical")
        if set(planned) & set(excluded):
            raise ValueError("one symbol cannot be both planned and excluded")
        if self.target_symbol_count != len(planned) + len(excluded):
            raise ValueError("remediation target count does not reconcile")
        if self.planned_symbol_count != len(planned):
            raise ValueError("planned symbol count does not reconcile")
        if self.excluded_symbol_count != len(excluded):
            raise ValueError("excluded symbol count does not reconcile")
        if self.action_count != len(self.actions):
            raise ValueError("remediation action count does not reconcile")
        for symbol in planned:
            intervals = tuple(
                action.interval for action in self.actions if action.symbol == symbol
            )
            if intervals != _INTERVAL_ORDER:
                raise ValueError("each planned symbol must have exactly four interval actions")
        if self.aligned_boundary_action_count != sum(
            action.boundary_bucket_disposition == "RETAIN_ALIGNED_BOUNDARY_BUCKET"
            for action in self.actions
        ):
            raise ValueError("aligned boundary action count does not reconcile")
        if self.partial_boundary_action_count != sum(
            action.boundary_bucket_disposition == "EXCLUDE_PARTIAL_BOUNDARY_BUCKET"
            for action in self.actions
        ):
            raise ValueError("partial boundary action count does not reconcile")
        if self.aligned_boundary_action_count + self.partial_boundary_action_count != (
            self.action_count
        ):
            raise ValueError("boundary dispositions do not cover every action")
        if self.whole_boundary_partition_exclusion_count != sum(
            action.boundary_month_whole_partition_excluded for action in self.actions
        ):
            raise ValueError("whole boundary partition exclusion count does not reconcile")
        aggregate_counts = (
            (
                self.boundary_month_source_row_count,
                "boundary_month_source_row_count",
            ),
            (
                self.boundary_month_excluded_row_count,
                "boundary_month_excluded_row_count",
            ),
            (
                self.boundary_month_expected_retained_row_count,
                "boundary_month_expected_retained_row_count",
            ),
            (self.settled_evidence_row_count, "settled_evidence_row_count"),
        )
        for aggregate, field_name in aggregate_counts:
            if aggregate != sum(cast(int, getattr(action, field_name)) for action in self.actions):
                raise ValueError(f"aggregate {field_name} does not reconcile")
        expected_blockers = set(_MANDATORY_BLOCKERS)
        expected_blockers.update(
            f"UNRESOLVED_LIFECYCLE_SYMBOL_EXCLUDED:{symbol}" for symbol in excluded
        )
        if self.blockers != tuple(sorted(expected_blockers)):
            raise ValueError("remediation blockers do not preserve required gates")
        payload = self.model_dump(mode="json", exclude={"plan_hash"})
        if self.plan_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("lifecycle remediation plan content hash mismatch")
        return self


def _source_rows_for_action(
    symbol_audit: SymbolLifecycleBoundaryAudit,
    interval: ArchiveInterval,
) -> tuple[ArchiveFingerprint, ArchiveFingerprint, datetime, int, int]:
    if interval == "1d":
        daily_evidence = symbol_audit.daily
        primary = daily_evidence.primary_archive
        settled = daily_evidence.settled_archive
        boundary = daily_evidence.expected_boundary_open_time
        if primary is None or settled is None or boundary is None:
            raise LifecycleRemediationPlanError("confirmed daily lifecycle evidence is incomplete")
        old_row_count = daily_evidence.primary_rows_before_boundary
        new_row_count = daily_evidence.primary_rows_at_or_after_boundary
        if old_row_count + new_row_count != primary.row_count:
            raise LifecycleRemediationPlanError("daily boundary row counts do not reconcile")
        return primary, settled, boundary, old_row_count, new_row_count

    intraday_evidence = next(
        item for item in symbol_audit.intraday if item.interval == interval
    )
    primary = intraday_evidence.primary_archive
    settled = intraday_evidence.settled_archive
    boundary = intraday_evidence.expected_boundary_open_time
    if primary is None or settled is None or boundary is None or len(primary.gaps) != 1:
        raise LifecycleRemediationPlanError("confirmed intraday lifecycle evidence is incomplete")
    old_row_count = primary.gaps[0].row_position
    new_row_count = primary.row_count - old_row_count
    if new_row_count <= 0:
        raise LifecycleRemediationPlanError("intraday boundary has no new lifecycle rows")
    return primary, settled, boundary, old_row_count, new_row_count


def _build_action(
    symbol_audit: SymbolLifecycleBoundaryAudit,
    interval: ArchiveInterval,
) -> LifecycleIntervalRemediationAction:
    if (
        symbol_audit.period is None
        or symbol_audit.identity_effective_from is None
        or symbol_audit.identity_source_ref is None
    ):
        raise LifecycleRemediationPlanError("confirmed symbol identity evidence is incomplete")
    primary, settled, boundary, old_row_count, new_row_count = _source_rows_for_action(
        symbol_audit, interval
    )
    if (
        primary.symbol != symbol_audit.symbol
        or settled.symbol != symbol_audit.settled_symbol
        or primary.interval != interval
        or settled.interval != interval
        or primary.period != symbol_audit.period
        or settled.period != symbol_audit.period
    ):
        raise LifecycleRemediationPlanError("archive evidence identity does not match action")
    effective_from = symbol_audit.identity_effective_from
    expected_boundary = _floor_to_interval(effective_from, interval)
    if boundary != expected_boundary:
        raise LifecycleRemediationPlanError("audit boundary does not contain effective_from")
    retain_from = _ceil_to_interval(effective_from, interval)
    partial_boundary = retain_from != boundary
    if partial_boundary and new_row_count < 1:
        raise LifecycleRemediationPlanError("partial boundary bucket is absent")
    excluded_row_count = old_row_count + int(partial_boundary)
    retained_row_count = new_row_count - int(partial_boundary)
    return LifecycleIntervalRemediationAction(
        symbol=symbol_audit.symbol,
        settled_symbol=symbol_audit.settled_symbol,
        interval=interval,
        evidence_period=symbol_audit.period,
        identity_effective_from=effective_from,
        identity_source_ref=symbol_audit.identity_source_ref,
        source_boundary_bucket_open_time=boundary,
        retain_from_open_time=retain_from,
        exclude_before_open_time=retain_from,
        boundary_bucket_disposition=(
            "EXCLUDE_PARTIAL_BOUNDARY_BUCKET"
            if partial_boundary
            else "RETAIN_ALIGNED_BOUNDARY_BUCKET"
        ),
        boundary_month_source_row_count=primary.row_count,
        boundary_month_excluded_row_count=excluded_row_count,
        boundary_month_expected_retained_row_count=retained_row_count,
        boundary_month_whole_partition_excluded=retained_row_count == 0,
        boundary_month_operation=(
            "EXCLUDE_PRIMARY_PARTITION"
            if retained_row_count == 0
            else "FILTER_PRIMARY_PARTITION"
        ),
        primary_archive_sha256=primary.source_sha256,
        primary_rows_sha256=primary.rows_sha256,
        settled_archive_sha256=settled.source_sha256,
        settled_rows_sha256=settled.rows_sha256,
        settled_evidence_row_count=settled.row_count,
        audit_reason_codes=tuple(
            sorted(
                set(symbol_audit.reason_codes)
                | set(symbol_audit.daily.reason_codes)
            )
        ),
    )


def _recompute_normalization_result_hash(result: NormalizationBatchResult) -> str:
    payload = {
        "plan_content_hash": result.plan_content_hash,
        "requested_count": result.requested_count,
        "normalized_count": result.normalized_count,
        "reused_count": result.reused_count,
        "failed_count": result.failed_count,
        "normalized_row_count": result.normalized_row_count,
        "normalized_parquet_bytes": result.normalized_parquet_bytes,
        "dataset_content_hash": result.dataset_content_hash,
        "failures": [item.model_dump(mode="json") for item in result.failures],
    }
    content = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(content).hexdigest()


def build_lifecycle_normalization_remediation_plan(
    *,
    normalization: NormalizationBatchResult,
    lifecycle_audit: LifecycleBoundaryAuditReport,
    expected_normalization_result_hash: str,
    expected_lifecycle_audit_hash: str,
    required_unresolved_symbols: tuple[str, ...],
) -> LifecycleNormalizationRemediationPlan:
    """Convert a verified boundary audit to review-only conservative cutoff actions."""

    normalization = NormalizationBatchResult.model_validate(
        normalization.model_dump(mode="json")
    )
    lifecycle_audit = LifecycleBoundaryAuditReport.model_validate(
        lifecycle_audit.model_dump(mode="json")
    )
    try:
        _require_sha256(expected_normalization_result_hash)
        _require_sha256(expected_lifecycle_audit_hash)
    except ValueError as error:
        raise LifecycleRemediationPlanError("expected source hash is invalid") from error
    if normalization.result_hash != _recompute_normalization_result_hash(normalization):
        raise LifecycleRemediationPlanError("source normalization result hash is invalid")
    if normalization.result_hash != expected_normalization_result_hash:
        raise LifecycleRemediationPlanError("unexpected frozen normalization result")
    if lifecycle_audit.report_hash != expected_lifecycle_audit_hash:
        raise LifecycleRemediationPlanError("unexpected lifecycle boundary audit")
    if lifecycle_audit.normalization_result_hash != normalization.result_hash:
        raise LifecycleRemediationPlanError(
            "lifecycle audit and source normalization result do not match"
        )
    required_unresolved = tuple(sorted(set(required_unresolved_symbols)))
    if required_unresolved != required_unresolved_symbols or any(
        _SYMBOL.fullmatch(symbol) is None for symbol in required_unresolved
    ):
        raise LifecycleRemediationPlanError(
            "required unresolved symbols must be unique and canonical"
        )
    observed_unresolved = tuple(
        item.symbol for item in lifecycle_audit.symbols if item.status == "UNRESOLVED"
    )
    if observed_unresolved != required_unresolved:
        raise LifecycleRemediationPlanError(
            "audit unresolved symbol set differs from the required unresolved symbol set"
        )

    actions: list[LifecycleIntervalRemediationAction] = []
    exclusions: list[ExcludedLifecycleSymbol] = []
    for symbol_audit in lifecycle_audit.symbols:
        if symbol_audit.status == "UNRESOLVED":
            exclusions.append(
                ExcludedLifecycleSymbol(
                    symbol=symbol_audit.symbol,
                    audit_reason_codes=symbol_audit.reason_codes,
                )
            )
            continue
        actions.extend(
            _build_action(symbol_audit, interval) for interval in _INTERVAL_ORDER
        )
    ordered_actions = tuple(
        sorted(actions, key=lambda item: (item.symbol, _INTERVAL_INDEX[item.interval]))
    )
    ordered_exclusions = tuple(sorted(exclusions, key=lambda item: item.symbol))
    planned_symbols = tuple(sorted(set(action.symbol for action in ordered_actions)))
    blockers = set(_MANDATORY_BLOCKERS)
    blockers.update(
        f"UNRESOLVED_LIFECYCLE_SYMBOL_EXCLUDED:{item.symbol}"
        for item in ordered_exclusions
    )
    payload: dict[str, Any] = {
        "schema_version": "lifecycle-normalization-remediation-plan/0.1.0",
        "derivative_namespace": "lifecycle-scoped-normalization/0.1.0",
        "source_normalization_result_hash": normalization.result_hash,
        "source_normalization_plan_content_hash": normalization.plan_content_hash,
        "source_dataset_content_hash": normalization.dataset_content_hash,
        "lifecycle_boundary_audit_hash": lifecycle_audit.report_hash,
        "identity_registry_hash": lifecycle_audit.identity_registry_hash,
        "target_symbol_count": len(lifecycle_audit.symbols),
        "planned_symbol_count": len(planned_symbols),
        "excluded_symbol_count": len(ordered_exclusions),
        "action_count": len(ordered_actions),
        "aligned_boundary_action_count": sum(
            action.boundary_bucket_disposition == "RETAIN_ALIGNED_BOUNDARY_BUCKET"
            for action in ordered_actions
        ),
        "partial_boundary_action_count": sum(
            action.boundary_bucket_disposition == "EXCLUDE_PARTIAL_BOUNDARY_BUCKET"
            for action in ordered_actions
        ),
        "whole_boundary_partition_exclusion_count": sum(
            action.boundary_month_whole_partition_excluded for action in ordered_actions
        ),
        "boundary_month_source_row_count": sum(
            action.boundary_month_source_row_count for action in ordered_actions
        ),
        "boundary_month_excluded_row_count": sum(
            action.boundary_month_excluded_row_count for action in ordered_actions
        ),
        "boundary_month_expected_retained_row_count": sum(
            action.boundary_month_expected_retained_row_count for action in ordered_actions
        ),
        "settled_evidence_row_count": sum(
            action.settled_evidence_row_count for action in ordered_actions
        ),
        "planned_symbols": list(planned_symbols),
        "excluded_symbols": [item.model_dump(mode="json") for item in ordered_exclusions],
        "actions": [item.model_dump(mode="json") for item in ordered_actions],
        "status": "BLOCKED",
        "blockers": sorted(blockers),
        "source_normalization_result_preserved": True,
        "new_content_addressed_result_required": True,
        "source_raw_archive_mutation_authorized": False,
        "plan_executed": False,
        "output_materialized": False,
        "market_data_download_authorized": False,
        "normalization_execution_authorized": False,
        "atr_reset_authorized": False,
        "history_seed_authorized": False,
        "historical_rule_gate_relaxation_authorized": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
        "replacement_normalization_result_hash": None,
        "replacement_dataset_content_hash": None,
    }
    return LifecycleNormalizationRemediationPlan.model_validate(
        {
            **payload,
            "plan_hash": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        }
    )


def write_lifecycle_normalization_remediation_plan(
    plan: LifecycleNormalizationRemediationPlan,
    data_dir: Path,
    *,
    normalization: NormalizationBatchResult,
    lifecycle_audit: LifecycleBoundaryAuditReport,
    expected_normalization_result_hash: str,
    expected_lifecycle_audit_hash: str,
    required_unresolved_symbols: tuple[str, ...],
) -> Path:
    """Publish an immutable remediation plan without running any of its actions."""

    plan = LifecycleNormalizationRemediationPlan.model_validate(
        plan.model_dump(mode="json")
    )
    expected_plan = build_lifecycle_normalization_remediation_plan(
        normalization=normalization,
        lifecycle_audit=lifecycle_audit,
        expected_normalization_result_hash=expected_normalization_result_hash,
        expected_lifecycle_audit_hash=expected_lifecycle_audit_hash,
        required_unresolved_symbols=required_unresolved_symbols,
    )
    if plan != expected_plan:
        raise LifecycleRemediationPlanError(
            "remediation plan does not reconcile with its trusted source audit"
        )
    destination = (
        data_dir
        / "manifests"
        / "lifecycle_normalization_remediation_plan"
        / f"{plan.plan_hash}.json"
    )
    content = canonical_json_bytes(plan.model_dump(mode="json"))
    if destination.is_symlink() or (
        destination.exists() and destination.read_bytes() != content
    ):
        raise LifecycleRemediationPlanError(
            "existing lifecycle normalization remediation plan changed"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
