"""Synthetic-only execution and acceptance for lifecycle-scoped derivatives."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.archive import ArchiveSpec
from slagalpha.data.klines import RAW_COLUMNS, normalize_klines
from slagalpha.reporting.run_manifest import _publish_immutable, canonical_json_bytes
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
