"""Fail-closed metadata and monthly-object coverage-loss accounting."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.capacity import DEFAULT_INTERVALS
from slagalpha.data.exchange_info import ExchangeContract

DETAIL_COLUMNS = (
    "symbol",
    "period",
    "interval_count",
    "required_interval_count",
    "all_required_intervals",
    "classification",
)


class CoverageLossError(RuntimeError):
    """Raised when coverage inputs or content-addressed artifacts are invalid."""


class CoverageLossReport(BaseModel):
    """Immutable partition of historical monthly-object and metadata coverage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["coverage-loss/0.1.0"] = "coverage-loss/0.1.0"
    capacity_plan_hash: str
    exchange_info_hash: str
    research_start: date
    research_end_exclusive: date
    required_intervals: tuple[str, ...]
    root_symbol_count: int = Field(ge=0)
    archive_symbol_count: int = Field(ge=0)
    observation_archive_symbol_count: int = Field(ge=0)
    metadata_contract_count: int = Field(ge=0)
    identity_metadata_available_symbols: tuple[str, ...]
    identity_metadata_without_archive_symbols: tuple[str, ...]
    archive_without_metadata_symbols: tuple[str, ...]
    metadata_ineligible_archive_symbols: tuple[str, ...]
    observation_symbol_month_count: int = Field(ge=0)
    identity_metadata_available_symbol_month_count: int = Field(ge=0)
    metadata_missing_symbol_month_count: int = Field(ge=0)
    metadata_ineligible_symbol_month_count: int = Field(ge=0)
    partial_interval_symbol_month_count: int = Field(ge=0)
    locked_research_ready: Literal[False] = False
    locked_research_blockers: tuple[str, ...]
    coverage_hash: str

    @field_validator("capacity_plan_hash", "exchange_info_hash", "coverage_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("coverage hashes must be SHA-256")
        return normalized

    @model_validator(mode="after")
    def validate_partition(self) -> Self:
        if self.research_start.day != 1 or self.research_end_exclusive.day != 1:
            raise ValueError("research boundaries must use the first day of a month")
        if self.research_end_exclusive <= self.research_start:
            raise ValueError("research_end_exclusive must follow research_start")
        for symbols in (
            self.identity_metadata_available_symbols,
            self.identity_metadata_without_archive_symbols,
            self.archive_without_metadata_symbols,
            self.metadata_ineligible_archive_symbols,
        ):
            if symbols != tuple(sorted(set(symbols))):
                raise ValueError("coverage symbol sets must be unique and canonical")
        if self.observation_symbol_month_count != (
            self.identity_metadata_available_symbol_month_count
            + self.metadata_missing_symbol_month_count
            + self.metadata_ineligible_symbol_month_count
        ):
            raise ValueError("symbol-month metadata partition does not reconcile")
        if not self.locked_research_blockers:
            raise ValueError("fail-closed report must explain its remaining blockers")
        return self


def _canonical_sha256(value: Any) -> str:
    content = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(content).hexdigest()


def _identity_metadata_available(contract: ExchangeContract) -> bool:
    return (
        contract.contract_type == "PERPETUAL"
        and contract.quote_asset == "USDT"
        and contract.margin_asset == "USDT"
        and contract.status in {"TRADING", "SETTLING"}
    )


def build_coverage_loss_report(
    capacity_rows: pd.DataFrame,
    *,
    contracts: tuple[ExchangeContract, ...],
    capacity_plan_hash: str,
    exchange_info_hash: str,
    root_symbol_count: int,
    research_start: date,
    research_end_exclusive: date,
) -> tuple[CoverageLossReport, pd.DataFrame]:
    """Classify exact symbol-months without inferring identity from symbol names."""

    required_columns = {"symbol", "period", "interval"}
    if not required_columns.issubset(capacity_rows.columns):
        raise CoverageLossError("capacity rows lack symbol, period, or interval")
    if research_start.day != 1 or research_end_exclusive.day != 1:
        raise CoverageLossError("research boundaries must use the first day of a month")
    if research_end_exclusive <= research_start:
        raise CoverageLossError("research_end_exclusive must follow research_start")
    if root_symbol_count < 0:
        raise CoverageLossError("root_symbol_count must not be negative")

    contract_by_symbol = {contract.symbol: contract for contract in contracts}
    if len(contract_by_symbol) != len(contracts):
        raise CoverageLossError("contracts must have unique symbols")
    eligible_metadata = {
        symbol
        for symbol, contract in contract_by_symbol.items()
        if _identity_metadata_available(contract)
    }
    metadata_symbols = set(contract_by_symbol)
    archive_symbols = set(capacity_rows["symbol"].astype(str))

    start_period = research_start.strftime("%Y-%m")
    end_period = research_end_exclusive.strftime("%Y-%m")
    observation = capacity_rows.loc[
        (capacity_rows["period"].astype(str) >= start_period)
        & (capacity_rows["period"].astype(str) < end_period),
        ["symbol", "period", "interval"],
    ].copy()
    observation["symbol"] = observation["symbol"].astype(str)
    observation["period"] = observation["period"].astype(str)
    observation["interval"] = observation["interval"].astype(str)
    invalid_intervals = set(observation["interval"]).difference(DEFAULT_INTERVALS)
    if invalid_intervals:
        raise CoverageLossError(
            f"coverage rows contain unsupported intervals: {sorted(invalid_intervals)}"
        )

    grouped = (
        observation.groupby(["symbol", "period"], sort=True)["interval"]
        .nunique()
        .rename("interval_count")
        .reset_index()
    )
    grouped["required_interval_count"] = len(DEFAULT_INTERVALS)
    grouped["all_required_intervals"] = grouped["interval_count"].eq(
        len(DEFAULT_INTERVALS)
    )

    def classification(symbol: str) -> str:
        if symbol not in metadata_symbols:
            return "METADATA_MISSING"
        if symbol not in eligible_metadata:
            return "METADATA_INELIGIBLE"
        return "IDENTITY_METADATA_AVAILABLE"

    grouped["classification"] = grouped["symbol"].map(classification)
    details = grouped.loc[:, DETAIL_COLUMNS].reset_index(drop=True)
    class_counts = details["classification"].value_counts()
    blockers = (
        "historical contract-rule intervals have not been independently reviewed",
        "monthly ZIP presence has not yet been promoted to validated candle-grid datasets",
        "archive-only symbols remain fail-closed without authoritative identity metadata",
    )

    hash_payload = {
        "capacity_plan_hash": capacity_plan_hash,
        "exchange_info_hash": exchange_info_hash,
        "research_start": research_start.isoformat(),
        "research_end_exclusive": research_end_exclusive.isoformat(),
        "required_intervals": DEFAULT_INTERVALS,
        "root_symbol_count": root_symbol_count,
        "metadata_contract_count": len(contracts),
        "details": details.to_dict(orient="records"),
        "locked_research_blockers": blockers,
    }
    coverage_hash = _canonical_sha256(hash_payload)
    report = CoverageLossReport(
        capacity_plan_hash=capacity_plan_hash,
        exchange_info_hash=exchange_info_hash,
        research_start=research_start,
        research_end_exclusive=research_end_exclusive,
        required_intervals=DEFAULT_INTERVALS,
        root_symbol_count=root_symbol_count,
        archive_symbol_count=len(archive_symbols),
        observation_archive_symbol_count=details["symbol"].nunique(),
        metadata_contract_count=len(contracts),
        identity_metadata_available_symbols=tuple(
            sorted(eligible_metadata.intersection(archive_symbols))
        ),
        identity_metadata_without_archive_symbols=tuple(
            sorted(eligible_metadata.difference(archive_symbols))
        ),
        archive_without_metadata_symbols=tuple(
            sorted(archive_symbols.difference(metadata_symbols))
        ),
        metadata_ineligible_archive_symbols=tuple(
            sorted(archive_symbols.intersection(metadata_symbols).difference(eligible_metadata))
        ),
        observation_symbol_month_count=len(details),
        identity_metadata_available_symbol_month_count=int(
            class_counts.get("IDENTITY_METADATA_AVAILABLE", 0)
        ),
        metadata_missing_symbol_month_count=int(
            class_counts.get("METADATA_MISSING", 0)
        ),
        metadata_ineligible_symbol_month_count=int(
            class_counts.get("METADATA_INELIGIBLE", 0)
        ),
        partial_interval_symbol_month_count=int(
            (~details["all_required_intervals"].astype(bool)).sum()
        ),
        locked_research_blockers=blockers,
        coverage_hash=coverage_hash,
    )
    return report, details


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(destination: Path, content: bytes) -> Path:
    if destination.exists():
        if destination.read_bytes() != content:
            raise CoverageLossError(
                f"existing content-addressed file changed: {destination}"
            )
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def write_coverage_loss_artifacts(
    report: CoverageLossReport,
    details: pd.DataFrame,
    data_dir: Path,
) -> tuple[Path, Path]:
    """Persist canonical detail rows and the report that binds their file hash."""

    if list(details.columns) != list(DETAIL_COLUMNS):
        raise CoverageLossError("coverage details do not use the canonical schema")
    parquet_path = (
        data_dir
        / "normalized"
        / "coverage_loss"
        / f"{report.coverage_hash}.parquet"
    )
    if not parquet_path.exists():
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = parquet_path.with_name(f".{parquet_path.name}.{uuid4().hex}.part")
        try:
            details.to_parquet(temporary, index=False)
            temporary.replace(parquet_path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        existing = pd.read_parquet(parquet_path)
        try:
            pd.testing.assert_frame_equal(existing, details, check_dtype=False)
        except AssertionError as error:
            raise CoverageLossError(
                f"existing content-addressed Parquet changed: {parquet_path}"
            ) from error

    manifest_path = (
        data_dir
        / "manifests"
        / "coverage_loss"
        / f"{report.coverage_hash}.json"
    )
    manifest = {
        "report": report.model_dump(mode="json"),
        "detail_parquet_sha256": _sha256_file(parquet_path),
        "detail_row_count": len(details),
    }
    content = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    _atomic_write(manifest_path, content)
    return parquet_path, manifest_path
