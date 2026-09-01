"""Exact, content-addressed capacity planning for Binance monthly archives."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

import httpx
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.archive import ArchiveSpec
from slagalpha.data.inventory import (
    MONTHLY_KLINE_PREFIX,
    ArchiveInventory,
    fetch_archive_inventory,
)

DEFAULT_INTERVALS = ("15m", "1h", "4h", "1d")
DEFAULT_THRESHOLD_BYTES = 20 * 1024**3
CAPACITY_COLUMNS = (
    "symbol",
    "interval",
    "period",
    "key",
    "size",
    "etag",
    "last_modified",
    "checksum_key",
    "checksum_present",
    "listing_hash",
)


class ArchiveCapacityError(RuntimeError):
    """Raised when an archive capacity plan or artifact is invalid."""


class PrefixInventoryReceipt(BaseModel):
    """Hash receipt proving one exact prefix listing was inspected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prefix: str
    listing_hash: str
    object_count: int = Field(ge=0)

    @field_validator("listing_hash")
    @classmethod
    def validate_listing_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("listing_hash must be a SHA-256")
        return normalized


class ArchiveCapacityEstimate(BaseModel):
    """Immutable upper-bound estimate based on exact official object sizes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["archive-capacity-estimate/0.1.0"] = (
        "archive-capacity-estimate/0.1.0"
    )
    root_listing_hash: str
    exchange_info_hash: str
    start_period: str
    end_period_exclusive: str
    intervals: tuple[str, ...]
    symbol_count: int = Field(ge=0)
    requested_prefix_count: int = Field(ge=0)
    successful_prefix_count: int = Field(ge=0)
    prefix_receipts: tuple[PrefixInventoryReceipt, ...]
    failed_prefixes: tuple[str, ...]
    complete: bool
    zip_object_count: int = Field(ge=0)
    checksum_missing_count: int = Field(ge=0)
    downloadable_zip_count: int = Field(ge=0)
    exact_zip_bytes: int = Field(ge=0)
    downloadable_zip_bytes: int = Field(ge=0)
    projection_multiplier: Decimal
    projected_total_bytes: int = Field(ge=0)
    threshold_bytes: int = Field(ge=0)
    within_threshold: bool
    plan_content_hash: str

    @field_validator("root_listing_hash", "exchange_info_hash", "plan_content_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("capacity hashes must be SHA-256")
        return normalized

    @field_validator("projection_multiplier")
    @classmethod
    def validate_multiplier(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 1:
            raise ValueError("projection_multiplier must be finite and at least 1")
        return value

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        if self.successful_prefix_count + len(self.failed_prefixes) != (
            self.requested_prefix_count
        ):
            raise ValueError("prefix counts do not reconcile")
        if self.successful_prefix_count != len(self.prefix_receipts):
            raise ValueError("successful_prefix_count does not match receipts")
        receipt_prefixes = tuple(item.prefix for item in self.prefix_receipts)
        if receipt_prefixes != tuple(sorted(set(receipt_prefixes))):
            raise ValueError("prefix_receipts must be unique and canonical")
        if self.complete != (not self.failed_prefixes):
            raise ValueError("complete does not match failed_prefixes")
        if self.downloadable_zip_count + self.checksum_missing_count != (
            self.zip_object_count
        ):
            raise ValueError("ZIP counts do not reconcile")
        expected_projection = int(
            (Decimal(self.downloadable_zip_bytes) * self.projection_multiplier).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        if self.projected_total_bytes != expected_projection:
            raise ValueError("projected_total_bytes does not match exact input bytes")
        expected_gate = self.complete and self.projected_total_bytes <= self.threshold_bytes
        if self.within_threshold != expected_gate:
            raise ValueError("within_threshold does not match the capacity gate")
        if self.failed_prefixes != tuple(sorted(set(self.failed_prefixes))):
            raise ValueError("failed_prefixes must be unique and canonical")
        return self


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _month_sequence(start: date, end_exclusive: date) -> tuple[date, ...]:
    if start.day != 1 or end_exclusive.day != 1:
        raise ArchiveCapacityError("month range boundaries must use the first day")
    if end_exclusive <= start:
        raise ArchiveCapacityError("end_exclusive must be later than start")
    months = []
    current = start
    while current < end_exclusive:
        months.append(current)
        current = date(
            current.year + (1 if current.month == 12 else 0),
            1 if current.month == 12 else current.month + 1,
            1,
        )
    return tuple(months)


def _normalize_inputs(
    symbols: tuple[str, ...], intervals: tuple[str, ...], start: date
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized_symbols = tuple(
        sorted(
            {
                ArchiveSpec(
                    symbol=symbol,
                    interval="15m",
                    year=start.year,
                    month=start.month,
                ).symbol
                for symbol in symbols
            }
        )
    )
    if not normalized_symbols:
        raise ArchiveCapacityError("at least one symbol is required")
    if not intervals or len(set(intervals)) != len(intervals):
        raise ArchiveCapacityError("intervals must be non-empty and unique")
    invalid = set(intervals).difference(DEFAULT_INTERVALS)
    if invalid:
        raise ArchiveCapacityError(f"unsupported capacity intervals: {sorted(invalid)}")
    normalized_intervals = tuple(
        interval for interval in DEFAULT_INTERVALS if interval in intervals
    )
    return normalized_symbols, normalized_intervals


def _inventory_rows(
    inventory: ArchiveInventory,
    *,
    symbol: str,
    interval: str,
    months: tuple[date, ...],
) -> list[dict[str, object]]:
    prefix = f"{MONTHLY_KLINE_PREFIX}{symbol}/{interval}/"
    if inventory.prefix != prefix:
        raise ArchiveCapacityError(
            f"inventory prefix {inventory.prefix!r} does not match {prefix!r}"
        )
    by_key = {item.key: item for item in inventory.objects}
    rows = []
    for month in months:
        spec = ArchiveSpec(
            symbol=symbol,
            interval=interval,  # type: ignore[arg-type]
            year=month.year,
            month=month.month,
        )
        key = f"{prefix}{spec.filename}"
        item = by_key.get(key)
        if item is None:
            continue
        checksum_key = f"{key}.CHECKSUM"
        rows.append(
            {
                "symbol": symbol,
                "interval": interval,
                "period": spec.period,
                "key": key,
                "size": item.size,
                "etag": item.etag,
                "last_modified": item.last_modified.isoformat(),
                "checksum_key": checksum_key,
                "checksum_present": checksum_key in by_key,
                "listing_hash": inventory.listing_hash,
            }
        )
    return rows


def estimate_archive_capacity(
    *,
    symbols: tuple[str, ...],
    intervals: tuple[str, ...] = DEFAULT_INTERVALS,
    start: date,
    end_exclusive: date,
    root_listing_hash: str,
    exchange_info_hash: str,
    fetch_inventory: Callable[[str], ArchiveInventory] | None = None,
    max_workers: int = 1,
    projection_multiplier: Decimal = Decimal("4"),
    threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[ArchiveCapacityEstimate, pd.DataFrame]:
    """List exact official prefixes and calculate a conservative storage gate."""

    if max_workers < 1:
        raise ArchiveCapacityError("max_workers must be at least 1")
    if threshold_bytes < 0:
        raise ArchiveCapacityError("threshold_bytes must not be negative")
    if not projection_multiplier.is_finite() or projection_multiplier < 1:
        raise ArchiveCapacityError("projection_multiplier must be finite and at least 1")
    months = _month_sequence(start, end_exclusive)
    normalized_symbols, normalized_intervals = _normalize_inputs(symbols, intervals, start)
    targets = tuple(
        (
            symbol,
            interval,
            f"{MONTHLY_KLINE_PREFIX}{symbol}/{interval}/",
        )
        for symbol in normalized_symbols
        for interval in normalized_intervals
    )

    rows: list[dict[str, object]] = []
    receipts: list[PrefixInventoryReceipt] = []
    failed_prefixes: list[str] = []

    def run(active_fetch: Callable[[str], ArchiveInventory]) -> None:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(active_fetch, prefix): (symbol, interval, prefix)
                for symbol, interval, prefix in targets
            }
            completed = 0
            for future in as_completed(futures):
                symbol, interval, prefix = futures[future]
                try:
                    inventory = future.result()
                    rows.extend(
                        _inventory_rows(
                            inventory,
                            symbol=symbol,
                            interval=interval,
                            months=months,
                        )
                    )
                    receipts.append(
                        PrefixInventoryReceipt(
                            prefix=prefix,
                            listing_hash=inventory.listing_hash,
                            object_count=len(inventory.objects),
                        )
                    )
                except Exception:  # The failed exact prefix is the durable evidence.
                    failed_prefixes.append(prefix)
                completed += 1
                if progress is not None:
                    progress(completed, len(targets))

    if fetch_inventory is not None:
        run(fetch_inventory)
    else:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": "slagalpha/0.1.0"},
        ) as client:
            run(lambda prefix: fetch_archive_inventory(prefix, client=client))

    frame = pd.DataFrame(rows, columns=CAPACITY_COLUMNS)
    if not frame.empty:
        interval_order = {value: index for index, value in enumerate(DEFAULT_INTERVALS)}
        frame["_interval_order"] = frame["interval"].map(interval_order)
        frame = (
            frame.sort_values(
                ["symbol", "_interval_order", "period", "key"],
                kind="stable",
            )
            .drop(columns="_interval_order")
            .reset_index(drop=True)
        )
    failed = tuple(sorted(set(failed_prefixes)))
    canonical_receipts = tuple(sorted(receipts, key=lambda item: item.prefix))
    checksum_present = frame["checksum_present"].astype(bool)
    exact_zip_bytes = int(frame["size"].sum())
    downloadable_zip_bytes = int(frame.loc[checksum_present, "size"].sum())
    projected_total_bytes = int(
        (Decimal(downloadable_zip_bytes) * projection_multiplier).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    semantic_rows = frame.to_dict(orient="records")
    hash_payload = {
        "root_listing_hash": root_listing_hash,
        "exchange_info_hash": exchange_info_hash,
        "start_period": start.strftime("%Y-%m"),
        "end_period_exclusive": end_exclusive.strftime("%Y-%m"),
        "intervals": normalized_intervals,
        "symbols": normalized_symbols,
        "prefix_receipts": [
            receipt.model_dump(mode="json") for receipt in canonical_receipts
        ],
        "failed_prefixes": failed,
        "projection_multiplier": str(projection_multiplier),
        "threshold_bytes": threshold_bytes,
        "rows": semantic_rows,
    }
    plan_content_hash = _canonical_sha256(hash_payload)
    complete = not failed
    estimate = ArchiveCapacityEstimate(
        root_listing_hash=root_listing_hash,
        exchange_info_hash=exchange_info_hash,
        start_period=start.strftime("%Y-%m"),
        end_period_exclusive=end_exclusive.strftime("%Y-%m"),
        intervals=normalized_intervals,
        symbol_count=len(normalized_symbols),
        requested_prefix_count=len(targets),
        successful_prefix_count=len(canonical_receipts),
        prefix_receipts=canonical_receipts,
        failed_prefixes=failed,
        complete=complete,
        zip_object_count=len(frame),
        checksum_missing_count=int((~checksum_present).sum()),
        downloadable_zip_count=int(checksum_present.sum()),
        exact_zip_bytes=exact_zip_bytes,
        downloadable_zip_bytes=downloadable_zip_bytes,
        projection_multiplier=projection_multiplier,
        projected_total_bytes=projected_total_bytes,
        threshold_bytes=threshold_bytes,
        within_threshold=complete and projected_total_bytes <= threshold_bytes,
        plan_content_hash=plan_content_hash,
    )
    return estimate, frame


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(destination: Path, content: bytes) -> Path:
    if destination.exists():
        if destination.read_bytes() != content:
            raise ArchiveCapacityError(
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


def write_archive_capacity_artifacts(
    estimate: ArchiveCapacityEstimate,
    rows: pd.DataFrame,
    data_dir: Path,
) -> tuple[Path, Path]:
    """Persist the exact object table and its content-addressed planning receipt."""

    expected_columns = list(CAPACITY_COLUMNS)
    if list(rows.columns) != expected_columns:
        raise ArchiveCapacityError("capacity rows do not use the canonical schema")
    parquet_path = (
        data_dir
        / "normalized"
        / "archive_capacity"
        / f"{estimate.plan_content_hash}.parquet"
    )
    if not parquet_path.exists():
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = parquet_path.with_name(f".{parquet_path.name}.{uuid4().hex}.part")
        try:
            rows.to_parquet(temporary, index=False)
            temporary.replace(parquet_path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        existing = pd.read_parquet(parquet_path)
        try:
            pd.testing.assert_frame_equal(existing, rows, check_dtype=False)
        except AssertionError as error:
            raise ArchiveCapacityError(
                f"existing content-addressed Parquet changed: {parquet_path}"
            ) from error

    manifest_path = (
        data_dir
        / "manifests"
        / "archive_capacity"
        / f"{estimate.plan_content_hash}.json"
    )
    manifest = {
        "estimate": estimate.model_dump(mode="json"),
        "parquet_sha256": _sha256_file(parquet_path),
        "row_count": len(rows),
    }
    content = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    _atomic_write(manifest_path, content)
    return parquet_path, manifest_path
