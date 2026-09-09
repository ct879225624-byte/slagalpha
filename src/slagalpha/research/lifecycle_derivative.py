"""Synthetic-only execution and acceptance for lifecycle-scoped derivatives."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, Literal, Self

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.archive import ArchiveSpec
from slagalpha.data.klines import (
    RAW_COLUMNS,
    KlineArchiveError,
    _normalized_content_hash,
    normalize_klines,
    read_archive_csv,
    write_normalized_parquet,
)
from slagalpha.reporting.run_manifest import (
    RunManifestError,
    _publish_immutable,
    canonical_json_bytes,
)
from slagalpha.research.lifecycle_executor_contract import (
    LifecycleExecutorInterfaceContract,
)
from slagalpha.research.lifecycle_remediation import (
    LifecycleIntervalRemediationAction,
    LifecycleNormalizationRemediationPlan,
)

_SYMBOL = re.compile(r"^[A-Z0-9]+USDT$")
_PERIOD = re.compile(r"^\d{4}-\d{2}$")


class LifecycleDerivativeAcceptanceError(ValueError):
    """Raised when a synthetic lifecycle derivative fails closed acceptance."""


def _require_sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("lifecycle derivative references must be lowercase SHA-256")
    return normalized


def _source_rows_hash(source_frame: pd.DataFrame) -> str:
    rows = source_frame.loc[:, RAW_COLUMNS].astype(str).values.tolist()
    return hashlib.sha256(canonical_json_bytes({"rows": rows})).hexdigest()


def _acceptance_hash(payload: dict[str, Any]) -> str:
    candidate = LifecycleScopedDerivativeAcceptance.model_construct(
        **payload, acceptance_hash="0" * 64
    )
    canonical = candidate.model_dump(mode="json", exclude={"acceptance_hash"})
    return hashlib.sha256(canonical_json_bytes(canonical)).hexdigest()


class LifecycleScopedDerivativeAcceptance(BaseModel):
    """Content-addressed receipt for one in-memory synthetic derivative."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["lifecycle-scoped-normalization-acceptance/0.1.0"] = (
        "lifecycle-scoped-normalization-acceptance/0.1.0"
    )
    execution_scope: Literal["SYNTHETIC_ONLY"] = "SYNTHETIC_ONLY"
    remediation_plan_hash: str
    symbol: str
    settled_symbol: str
    interval: Literal["15m", "1h", "4h", "1d"]
    evidence_period: str
    source_archive_sha256: str
    source_rows_sha256: str
    retain_from_open_time: datetime
    source_row_count: int = Field(gt=0)
    excluded_row_count: int = Field(ge=0)
    retained_row_count: int = Field(ge=0)
    derivative_content_hash: str | None
    derivative_row_count: int = Field(ge=0)
    status: Literal["ACCEPTED", "EXCLUDED_EMPTY_BOUNDARY_PARTITION"]
    output_materialized: Literal[False] = False
    normalization_execution_authorized: Literal[False] = False
    atr_reset_authorized: Literal[False] = False
    history_seed_authorized: Literal[False] = False
    historical_rule_gate_relaxation_authorized: Literal[False] = False
    research_authorized: Literal[False] = False
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    acceptance_hash: str

    @field_validator(
        "remediation_plan_hash",
        "source_archive_sha256",
        "source_rows_sha256",
        "acceptance_hash",
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("derivative_content_hash")
    @classmethod
    def validate_optional_hash(cls, value: str | None) -> str | None:
        return None if value is None else _require_sha256(value)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if _SYMBOL.fullmatch(value) is None:
            raise ValueError("lifecycle derivative symbols must be canonical USDT symbols")
        return value

    @field_validator("settled_symbol")
    @classmethod
    def validate_settled_symbol(cls, value: str) -> str:
        if re.fullmatch(r"^[A-Z0-9]+USDTSETTLED$", value) is None:
            raise ValueError("lifecycle derivative settled symbol is not canonical")
        return value

    @field_validator("evidence_period")
    @classmethod
    def validate_period(cls, value: str) -> str:
        if _PERIOD.fullmatch(value) is None:
            raise ValueError("lifecycle derivative period must use YYYY-MM")
        try:
            datetime.strptime(value, "%Y-%m")
        except ValueError as error:
            raise ValueError("lifecycle derivative period must be a real calendar month") from error
        return value

    @field_validator("retain_from_open_time")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("lifecycle derivative timestamps must use UTC")
        return value

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if self.settled_symbol != f"{self.symbol}SETTLED":
            raise ValueError("settled symbol must match the primary symbol")
        if self.excluded_row_count + self.retained_row_count != self.source_row_count:
            raise ValueError("lifecycle derivative row counts do not reconcile")
        if self.status == "ACCEPTED":
            if self.retained_row_count == 0 or self.derivative_content_hash is None:
                raise ValueError("accepted derivative must contain normalized rows")
            if self.derivative_row_count != self.retained_row_count:
                raise ValueError("accepted derivative row count does not reconcile")
        else:
            if self.retained_row_count != 0 or self.derivative_content_hash is not None:
                raise ValueError("empty derivative exclusion does not reconcile")
            if self.derivative_row_count != 0:
                raise ValueError("empty derivative must have zero normalized rows")
        payload = self.model_dump(mode="json", exclude={"acceptance_hash"})
        if self.acceptance_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("lifecycle derivative acceptance hash mismatch")
        return self


class LifecycleSyntheticBatchOutput(BaseModel):
    """One canonical output entry in a synthetic batch receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    interval: Literal["15m", "1h", "4h", "1d"]
    status: Literal["ACCEPTED", "EXCLUDED_EMPTY_BOUNDARY_PARTITION"]
    acceptance_hash: str
    output_relative_path: str
    output_sha256: str
    source_row_count: int = Field(gt=0)
    excluded_row_count: int = Field(ge=0)
    retained_row_count: int = Field(ge=0)
    derivative_row_count: int = Field(ge=0)

    @field_validator("acceptance_hash", "output_sha256")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("output_relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("synthetic batch output path must be canonical and relative")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.excluded_row_count + self.retained_row_count != self.source_row_count:
            raise ValueError("synthetic batch output rows do not reconcile")
        if self.derivative_row_count != self.retained_row_count:
            raise ValueError("synthetic batch derivative rows do not reconcile")
        return self


def _batch_hash(payload: dict[str, Any]) -> str:
    candidate = LifecycleSyntheticBatchReceipt.model_construct(
        **payload, batch_hash="0" * 64
    )
    canonical = candidate.model_dump(mode="json", exclude={"batch_hash"})
    return hashlib.sha256(canonical_json_bytes(canonical)).hexdigest()


class LifecycleSyntheticBatchReceipt(BaseModel):
    """Content-addressed completion marker for a fully successful synthetic batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["lifecycle-derivative-synthetic-batch/0.1.0"] = (
        "lifecycle-derivative-synthetic-batch/0.1.0"
    )
    execution_scope: Literal["SYNTHETIC_ONLY"] = "SYNTHETIC_ONLY"
    executor_contract_hash: str
    remediation_plan_hash: str
    action_count: int = Field(gt=0)
    accepted_action_count: int = Field(ge=0)
    excluded_action_count: int = Field(ge=0)
    source_row_count: int = Field(ge=0)
    excluded_row_count: int = Field(ge=0)
    retained_row_count: int = Field(ge=0)
    derivative_row_count: int = Field(ge=0)
    outputs: tuple[LifecycleSyntheticBatchOutput, ...]
    synthetic_output_materialized: Literal[True] = True
    real_output_materialized: Literal[False] = False
    normalization_execution_authorized: Literal[False] = False
    atr_reset_authorized: Literal[False] = False
    history_seed_authorized: Literal[False] = False
    historical_rule_gate_relaxation_authorized: Literal[False] = False
    research_authorized: Literal[False] = False
    strategy_executed: Literal[False] = False
    locked_test_consumed: Literal[False] = False
    status: Literal["SYNTHETIC_ACCEPTED"] = "SYNTHETIC_ACCEPTED"
    batch_hash: str

    @field_validator("executor_contract_hash", "remediation_plan_hash", "batch_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        return _require_sha256(value)

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        keys = tuple((item.symbol, item.interval) for item in self.outputs)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("synthetic batch outputs must be unique and canonical")
        if len(self.outputs) != self.action_count:
            raise ValueError("synthetic batch output count does not match actions")
        if self.accepted_action_count != sum(
            item.status == "ACCEPTED" for item in self.outputs
        ) or self.excluded_action_count != sum(
            item.status == "EXCLUDED_EMPTY_BOUNDARY_PARTITION" for item in self.outputs
        ):
            raise ValueError("synthetic batch action statuses do not reconcile")
        if sum(item.source_row_count for item in self.outputs) != self.source_row_count:
            raise ValueError("synthetic batch source rows do not reconcile")
        if sum(item.excluded_row_count for item in self.outputs) != self.excluded_row_count:
            raise ValueError("synthetic batch excluded rows do not reconcile")
        if sum(item.retained_row_count for item in self.outputs) != self.retained_row_count:
            raise ValueError("synthetic batch retained rows do not reconcile")
        if sum(item.derivative_row_count for item in self.outputs) != self.derivative_row_count:
            raise ValueError("synthetic batch derivative rows do not reconcile")
        payload = self.model_dump(mode="json", exclude={"batch_hash"})
        if self.batch_hash != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
            raise ValueError("synthetic lifecycle batch hash mismatch")
        return self


def execute_synthetic_lifecycle_derivative(
    plan: LifecycleNormalizationRemediationPlan,
    action: LifecycleIntervalRemediationAction,
    source_frame: pd.DataFrame,
    *,
    source_archive_sha256: str,
    evaluated_at: datetime | None = None,
) -> tuple[pd.DataFrame | None, LifecycleScopedDerivativeAcceptance]:
    """Filter and normalize one action entirely in memory for synthetic acceptance."""

    try:
        plan = LifecycleNormalizationRemediationPlan.model_validate(
            plan.model_dump(mode="json")
        )
        action = LifecycleIntervalRemediationAction.model_validate(
            action.model_dump(mode="json")
        )
        _require_sha256(source_archive_sha256)
    except ValueError as error:
        raise LifecycleDerivativeAcceptanceError(
            "synthetic derivative source is invalid"
        ) from error
    if action not in plan.actions:
        raise LifecycleDerivativeAcceptanceError(
            "action is not part of the trusted remediation plan"
        )
    if source_archive_sha256 != action.primary_archive_sha256:
        raise LifecycleDerivativeAcceptanceError("source archive hash disagrees with action")
    if tuple(source_frame.columns) != tuple(RAW_COLUMNS):
        raise LifecycleDerivativeAcceptanceError(
            "synthetic source columns do not match RAW_COLUMNS"
        )
    if source_frame.empty:
        raise LifecycleDerivativeAcceptanceError("synthetic source frame must not be empty")
    frame = source_frame.loc[:, RAW_COLUMNS].copy()
    if len(frame) != action.boundary_month_source_row_count:
        raise LifecycleDerivativeAcceptanceError("synthetic source row count disagrees with action")
    rows_hash = _source_rows_hash(frame)
    if rows_hash != action.primary_rows_sha256:
        raise LifecycleDerivativeAcceptanceError("synthetic source rows hash disagrees with action")

    try:
        open_times = pd.to_numeric(frame["open_time"], errors="raise").astype("int64")
    except (TypeError, ValueError, OverflowError) as error:
        raise LifecycleDerivativeAcceptanceError("synthetic open times are invalid") from error
    cutoff_ms = int(action.retain_from_open_time.timestamp() * 1000)
    retained_mask = open_times >= cutoff_ms
    retained = frame.loc[retained_mask].reset_index(drop=True)
    excluded_count = len(frame) - len(retained)
    if excluded_count != action.boundary_month_excluded_row_count:
        raise LifecycleDerivativeAcceptanceError(
            "synthetic excluded row count disagrees with action"
        )
    if len(retained) != action.boundary_month_expected_retained_row_count:
        raise LifecycleDerivativeAcceptanceError(
            "synthetic retained row count disagrees with action"
        )
    if not retained.empty and int(retained.iloc[0]["open_time"]) != cutoff_ms:
        raise LifecycleDerivativeAcceptanceError(
            "synthetic retained rows do not begin at the exact cutoff"
        )

    common = {
        "remediation_plan_hash": plan.plan_hash,
        "symbol": action.symbol,
        "settled_symbol": action.settled_symbol,
        "interval": action.interval,
        "evidence_period": action.evidence_period,
        "source_archive_sha256": source_archive_sha256,
        "source_rows_sha256": rows_hash,
        "retain_from_open_time": action.retain_from_open_time,
        "source_row_count": len(frame),
        "excluded_row_count": excluded_count,
        "retained_row_count": len(retained),
        "output_materialized": False,
        "normalization_execution_authorized": False,
        "atr_reset_authorized": False,
        "history_seed_authorized": False,
        "historical_rule_gate_relaxation_authorized": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
    }
    if retained.empty:
        payload = {
            **common,
            "derivative_content_hash": None,
            "derivative_row_count": 0,
            "status": "EXCLUDED_EMPTY_BOUNDARY_PARTITION",
        }
        acceptance = LifecycleScopedDerivativeAcceptance.model_validate(
            {
                **payload,
                "acceptance_hash": _acceptance_hash(payload),
            }
        )
        return None, acceptance

    month = datetime.strptime(action.evidence_period, "%Y-%m").date()
    spec = ArchiveSpec(
        symbol=action.symbol,
        interval=action.interval,
        year=month.year,
        month=month.month,
    )
    normalized, _, derivative_hash = normalize_klines(
        retained,
        spec,
        source_archive_sha256,
        evaluated_at=evaluated_at or datetime.now(UTC),
    )
    payload = {
        **common,
        "derivative_content_hash": derivative_hash,
        "derivative_row_count": len(normalized),
        "status": "ACCEPTED",
    }
    acceptance = LifecycleScopedDerivativeAcceptance.model_validate(
        {
            **payload,
            "acceptance_hash": _acceptance_hash(payload),
        }
    )
    return normalized, acceptance


def execute_synthetic_archive_lifecycle_derivative(
    contract: LifecycleExecutorInterfaceContract,
    plan: LifecycleNormalizationRemediationPlan,
    action: LifecycleIntervalRemediationAction,
    *,
    expected_contract_hash: str,
    expected_plan_hash: str,
    primary_archive: bytes,
    settled_archive: bytes,
    evaluated_at: datetime | None = None,
) -> tuple[pd.DataFrame | None, LifecycleScopedDerivativeAcceptance]:
    """Exercise the frozen executor path using in-memory synthetic ZIP bytes only."""

    try:
        contract = LifecycleExecutorInterfaceContract.model_validate(
            contract.model_dump(mode="json")
        )
        plan = LifecycleNormalizationRemediationPlan.model_validate(
            plan.model_dump(mode="json")
        )
        action = LifecycleIntervalRemediationAction.model_validate(
            action.model_dump(mode="json")
        )
    except ValueError as error:
        raise LifecycleDerivativeAcceptanceError(
            "synthetic archive executor inputs are invalid"
        ) from error
    if contract.contract_hash != expected_contract_hash:
        raise LifecycleDerivativeAcceptanceError("unexpected executor contract")
    if plan.plan_hash != expected_plan_hash:
        raise LifecycleDerivativeAcceptanceError("unexpected remediation plan")
    if contract.remediation_plan_hash != plan.plan_hash:
        raise LifecycleDerivativeAcceptanceError("executor contract does not reference the plan")
    if contract.source_normalization_result_hash != plan.source_normalization_result_hash:
        raise LifecycleDerivativeAcceptanceError("normalization lineage does not reconcile")
    if contract.lifecycle_boundary_audit_hash != plan.lifecycle_boundary_audit_hash:
        raise LifecycleDerivativeAcceptanceError("lifecycle audit lineage does not reconcile")
    if action not in plan.actions:
        raise LifecycleDerivativeAcceptanceError(
            "action is not part of the trusted remediation plan"
        )

    primary_archive_hash = hashlib.sha256(primary_archive).hexdigest()
    if primary_archive_hash != action.primary_archive_sha256:
        raise LifecycleDerivativeAcceptanceError("primary archive hash disagrees with action")
    settled_archive_hash = hashlib.sha256(settled_archive).hexdigest()
    if settled_archive_hash != action.settled_archive_sha256:
        raise LifecycleDerivativeAcceptanceError("settled archive hash disagrees with action")

    month = datetime.strptime(action.evidence_period, "%Y-%m").date()
    primary_spec = ArchiveSpec(
        symbol=action.symbol,
        interval=action.interval,
        year=month.year,
        month=month.month,
    )
    settled_spec = primary_spec.model_copy(update={"symbol": action.settled_symbol})
    try:
        primary_frame = read_archive_csv(BytesIO(primary_archive), primary_spec)
        settled_frame = read_archive_csv(BytesIO(settled_archive), settled_spec)
    except KlineArchiveError as error:
        raise LifecycleDerivativeAcceptanceError(
            "synthetic archive layout is invalid"
        ) from error
    if _source_rows_hash(primary_frame) != action.primary_rows_sha256:
        raise LifecycleDerivativeAcceptanceError("primary rows hash disagrees with action")
    if _source_rows_hash(settled_frame) != action.settled_rows_sha256:
        raise LifecycleDerivativeAcceptanceError("settled rows hash disagrees with action")
    if len(settled_frame) != action.settled_evidence_row_count:
        raise LifecycleDerivativeAcceptanceError("settled evidence row count disagrees with action")

    return execute_synthetic_lifecycle_derivative(
        plan,
        action,
        primary_frame,
        source_archive_sha256=primary_archive_hash,
        evaluated_at=evaluated_at,
    )


def publish_synthetic_lifecycle_derivative(
    contract: LifecycleExecutorInterfaceContract,
    action: LifecycleIntervalRemediationAction,
    normalized: pd.DataFrame | None,
    acceptance: LifecycleScopedDerivativeAcceptance,
    *,
    expected_contract_hash: str,
    synthetic_workspace_root: Path,
    normalized_at: datetime,
) -> tuple[Path, str]:
    """Stage, validate, and immutably publish one synthetic executor result."""

    try:
        contract = LifecycleExecutorInterfaceContract.model_validate(
            contract.model_dump(mode="json")
        )
        action = LifecycleIntervalRemediationAction.model_validate(
            action.model_dump(mode="json")
        )
        acceptance = LifecycleScopedDerivativeAcceptance.model_validate(
            acceptance.model_dump(mode="json")
        )
    except ValueError as error:
        raise LifecycleDerivativeAcceptanceError(
            "synthetic publication inputs are invalid"
        ) from error
    if contract.contract_hash != expected_contract_hash:
        raise LifecycleDerivativeAcceptanceError("unexpected executor contract")
    if acceptance.remediation_plan_hash != contract.remediation_plan_hash:
        raise LifecycleDerivativeAcceptanceError("acceptance does not reference the trusted plan")
    if (
        acceptance.symbol,
        acceptance.settled_symbol,
        acceptance.interval,
        acceptance.evidence_period,
        acceptance.source_archive_sha256,
        acceptance.source_rows_sha256,
        acceptance.retain_from_open_time,
    ) != (
        action.symbol,
        action.settled_symbol,
        action.interval,
        action.evidence_period,
        action.primary_archive_sha256,
        action.primary_rows_sha256,
        action.retain_from_open_time,
    ):
        raise LifecycleDerivativeAcceptanceError("acceptance does not match the action")
    if normalized_at.tzinfo is None or normalized_at.utcoffset() != timedelta(0):
        raise LifecycleDerivativeAcceptanceError("normalized_at must use UTC")
    if normalized_at.microsecond % 1000:
        raise LifecycleDerivativeAcceptanceError("normalized_at must use millisecond precision")

    root = synthetic_workspace_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    action_hash = hashlib.sha256(
        canonical_json_bytes(action.model_dump(mode="json"))
    ).hexdigest()
    relative = contract.output_partition_template.format(
        interval=action.interval,
        symbol=action.symbol,
        year=action.retain_from_open_time.year,
        month=f"{action.retain_from_open_time.month:02d}",
        action_hash=action_hash[:16],
    )
    destination = (root / contract.derivative_namespace / relative).resolve()
    if not destination.is_relative_to(root):
        raise LifecycleDerivativeAcceptanceError("synthetic output escapes workspace root")
    repository_normalized = Path(__file__).resolve().parents[3] / "data" / "normalized"
    if destination.is_relative_to(repository_normalized):
        raise LifecycleDerivativeAcceptanceError(
            "synthetic publication cannot target repository normalized data"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    if normalized is None:
        if acceptance.status != "EXCLUDED_EMPTY_BOUNDARY_PARTITION":
            raise LifecycleDerivativeAcceptanceError("missing accepted derivative frame")
        destination = destination.with_suffix(".exclusion.json")
        content = canonical_json_bytes(acceptance.model_dump(mode="json"))
        if destination.is_symlink() or (
            destination.exists() and destination.read_bytes() != content
        ):
            raise LifecycleDerivativeAcceptanceError(
                "existing synthetic exclusion conflicts with acceptance"
            )
        try:
            _publish_immutable(destination, content)
        except RunManifestError as error:
            raise LifecycleDerivativeAcceptanceError(
                "synthetic exclusion publication conflict"
            ) from error
        return destination, hashlib.sha256(content).hexdigest()
    if acceptance.status != "ACCEPTED":
        raise LifecycleDerivativeAcceptanceError("empty exclusion cannot publish Parquet")
    if len(normalized) != acceptance.derivative_row_count:
        raise LifecycleDerivativeAcceptanceError("normalized row count disagrees with acceptance")
    if _normalized_content_hash(normalized) != acceptance.derivative_content_hash:
        raise LifecycleDerivativeAcceptanceError(
            "normalized content hash disagrees with acceptance"
        )

    with TemporaryDirectory(prefix=".lifecycle-stage-", dir=root) as stage_dir:
        staged = Path(stage_dir) / destination.name
        staged_hash = write_normalized_parquet(
            normalized,
            staged,
            normalized_at=normalized_at,
            normalized_content_hash=acceptance.derivative_content_hash,
            source_file_hash=acceptance.source_archive_sha256,
        )
        if write_normalized_parquet(
            normalized,
            staged,
            normalized_at=normalized_at,
            normalized_content_hash=acceptance.derivative_content_hash,
            source_file_hash=acceptance.source_archive_sha256,
        ) != staged_hash:
            raise LifecycleDerivativeAcceptanceError("staged synthetic derivative changed")
        staged_content = staged.read_bytes()
        if hashlib.sha256(staged_content).hexdigest() != staged_hash:
            raise LifecycleDerivativeAcceptanceError("staged synthetic derivative hash mismatch")
        if destination.is_symlink() or (
            destination.exists() and destination.read_bytes() != staged_content
        ):
            raise LifecycleDerivativeAcceptanceError(
                "existing synthetic derivative conflicts with staged output"
            )
        try:
            _publish_immutable(destination, staged_content)
        except RunManifestError as error:
            raise LifecycleDerivativeAcceptanceError(
                "synthetic derivative publication conflict"
            ) from error
    if destination.read_bytes() != staged_content:
        raise LifecycleDerivativeAcceptanceError("published synthetic derivative changed")
    return destination, hashlib.sha256(staged_content).hexdigest()


def execute_and_publish_synthetic_lifecycle_batch(
    contract: LifecycleExecutorInterfaceContract,
    plan: LifecycleNormalizationRemediationPlan,
    archives: Mapping[tuple[str, str], tuple[bytes, bytes]],
    *,
    expected_contract_hash: str,
    expected_plan_hash: str,
    synthetic_workspace_root: Path,
    normalized_at: datetime,
) -> tuple[LifecycleSyntheticBatchReceipt, Path]:
    """Complete every synthetic action before publishing one batch receipt."""

    try:
        contract = LifecycleExecutorInterfaceContract.model_validate(
            contract.model_dump(mode="json")
        )
        plan = LifecycleNormalizationRemediationPlan.model_validate(
            plan.model_dump(mode="json")
        )
    except ValueError as error:
        raise LifecycleDerivativeAcceptanceError(
            "synthetic batch inputs are invalid"
        ) from error
    if contract.contract_hash != expected_contract_hash:
        raise LifecycleDerivativeAcceptanceError("unexpected executor contract")
    if plan.plan_hash != expected_plan_hash or contract.remediation_plan_hash != plan.plan_hash:
        raise LifecycleDerivativeAcceptanceError("unexpected remediation plan")
    if (
        contract.action_count,
        contract.expected_source_row_count,
        contract.expected_excluded_row_count,
        contract.expected_retained_row_count,
    ) != (
        plan.action_count,
        plan.boundary_month_source_row_count,
        plan.boundary_month_excluded_row_count,
        plan.boundary_month_expected_retained_row_count,
    ):
        raise LifecycleDerivativeAcceptanceError("batch contract row counts do not match plan")
    expected_keys = {(action.symbol, action.interval) for action in plan.actions}
    if set(archives) != expected_keys:
        raise LifecycleDerivativeAcceptanceError("synthetic batch archive set is incomplete")

    root = synthetic_workspace_root.resolve()
    outputs: list[LifecycleSyntheticBatchOutput] = []
    for action in sorted(plan.actions, key=lambda item: (item.symbol, item.interval)):
        primary_archive, settled_archive = archives[(action.symbol, action.interval)]
        normalized, acceptance = execute_synthetic_archive_lifecycle_derivative(
            contract,
            plan,
            action,
            expected_contract_hash=expected_contract_hash,
            expected_plan_hash=expected_plan_hash,
            primary_archive=primary_archive,
            settled_archive=settled_archive,
            evaluated_at=normalized_at,
        )
        output_path, output_hash = publish_synthetic_lifecycle_derivative(
            contract,
            action,
            normalized,
            acceptance,
            expected_contract_hash=expected_contract_hash,
            synthetic_workspace_root=root,
            normalized_at=normalized_at,
        )
        outputs.append(
            LifecycleSyntheticBatchOutput(
                symbol=action.symbol,
                interval=action.interval,
                status=acceptance.status,
                acceptance_hash=acceptance.acceptance_hash,
                output_relative_path=output_path.relative_to(root).as_posix(),
                output_sha256=output_hash,
                source_row_count=acceptance.source_row_count,
                excluded_row_count=acceptance.excluded_row_count,
                retained_row_count=acceptance.retained_row_count,
                derivative_row_count=acceptance.derivative_row_count,
            )
        )

    output_tuple = tuple(outputs)
    payload: dict[str, Any] = {
        "executor_contract_hash": contract.contract_hash,
        "remediation_plan_hash": plan.plan_hash,
        "action_count": len(output_tuple),
        "accepted_action_count": sum(item.status == "ACCEPTED" for item in output_tuple),
        "excluded_action_count": sum(
            item.status == "EXCLUDED_EMPTY_BOUNDARY_PARTITION" for item in output_tuple
        ),
        "source_row_count": sum(item.source_row_count for item in output_tuple),
        "excluded_row_count": sum(item.excluded_row_count for item in output_tuple),
        "retained_row_count": sum(item.retained_row_count for item in output_tuple),
        "derivative_row_count": sum(item.derivative_row_count for item in output_tuple),
        "outputs": output_tuple,
        "synthetic_output_materialized": True,
        "real_output_materialized": False,
        "normalization_execution_authorized": False,
        "atr_reset_authorized": False,
        "history_seed_authorized": False,
        "historical_rule_gate_relaxation_authorized": False,
        "research_authorized": False,
        "strategy_executed": False,
        "locked_test_consumed": False,
        "status": "SYNTHETIC_ACCEPTED",
    }
    receipt = LifecycleSyntheticBatchReceipt.model_validate(
        {**payload, "batch_hash": _batch_hash(payload)}
    )
    destination = (
        root
        / "data"
        / "manifests"
        / "lifecycle_derivative_synthetic_batch"
        / f"{receipt.batch_hash}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = canonical_json_bytes(receipt.model_dump(mode="json"))
    if destination.is_symlink() or (
        destination.exists() and destination.read_bytes() != content
    ):
        raise LifecycleDerivativeAcceptanceError("existing synthetic batch receipt changed")
    try:
        _publish_immutable(destination, content)
    except RunManifestError as error:
        raise LifecycleDerivativeAcceptanceError(
            "synthetic batch receipt publication conflict"
        ) from error
    return receipt, destination


def write_lifecycle_scoped_derivative_acceptance(
    acceptance: LifecycleScopedDerivativeAcceptance,
    data_dir: Path,
) -> Path:
    """Write one synthetic acceptance receipt to a caller-selected directory."""

    acceptance = LifecycleScopedDerivativeAcceptance.model_validate(
        acceptance.model_dump(mode="json")
    )
    destination = (
        data_dir
        / "manifests"
        / "lifecycle_scoped_normalization_acceptance"
        / f"{acceptance.acceptance_hash}.json"
    )
    content = canonical_json_bytes(acceptance.model_dump(mode="json"))
    if destination.is_symlink() or (
        destination.exists() and destination.read_bytes() != content
    ):
        raise LifecycleDerivativeAcceptanceError(
            "existing lifecycle derivative acceptance changed"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    _publish_immutable(destination, content)
    return destination
