"""Resumable ZIP-to-Parquet validation for an accepted capacity plan."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Self, cast
from uuid import uuid4

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.archive import (
    ArchiveDownloadManifest,
    ArchiveSpec,
    archive_path,
    sha256_file,
)
from slagalpha.data.capacity import CAPACITY_COLUMNS
from slagalpha.data.klines import (
    ArchiveNormalizationManifest,
    normalize_archive_to_parquet,
    write_normalization_manifest,
)


class NormalizationBatchError(RuntimeError):
    """Raised when batch normalization inputs or evidence are invalid."""


class NormalizationFailure(BaseModel):
    """One fail-closed archive normalization result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str
    error: str


class NormalizationBatchResult(BaseModel):
    """Deterministic aggregate receipt for a complete local normalization pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["normalization-batch/0.1.0"] = (
        "normalization-batch/0.1.0"
    )
    plan_content_hash: str
    requested_count: int = Field(ge=0)
    normalized_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    normalized_row_count: int = Field(ge=0)
    normalized_parquet_bytes: int = Field(ge=0)
    dataset_content_hash: str
    failures: tuple[NormalizationFailure, ...]
    complete: bool
    result_hash: str

    @field_validator("plan_content_hash", "dataset_content_hash", "result_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("normalization hashes must be SHA-256")
        return normalized

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.normalized_count + self.reused_count + self.failed_count != (
            self.requested_count
        ):
            raise ValueError("normalization counts do not reconcile")
        if self.failed_count != len(self.failures):
            raise ValueError("failed_count does not match failures")
        identities = tuple(item.identity for item in self.failures)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("failures must be unique and canonical")
        if self.complete != (self.failed_count == 0):
            raise ValueError("complete does not match failed_count")
        return self


@dataclass(frozen=True)
class _WorkItem:
    spec: ArchiveSpec
    expected_size: int

    @property
    def identity(self) -> str:
        return f"{self.spec.symbol}/{self.spec.interval}/{self.spec.period}"


@dataclass(frozen=True)
class _SuccessRecord:
    identity: str
    source_file_hash: str
    normalized_content_hash: str
    normalized_row_count: int
    first_open_time: str
    last_open_time: str
    parquet_sha256: str
    parquet_bytes: int

    def semantic_dict(self) -> dict[str, str | int]:
        return {
            "identity": self.identity,
            "source_file_hash": self.source_file_hash,
            "normalized_content_hash": self.normalized_content_hash,
            "normalized_row_count": self.normalized_row_count,
            "first_open_time": self.first_open_time,
            "last_open_time": self.last_open_time,
            "parquet_sha256": self.parquet_sha256,
            "parquet_bytes": self.parquet_bytes,
        }


def _canonical_sha256(value: Any) -> str:
    content = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(content).hexdigest()


def _work_item(row: pd.Series) -> _WorkItem:
    period = str(row["period"])
    try:
        month = date.fromisoformat(f"{period}-01")
    except ValueError as error:
        raise NormalizationBatchError(f"invalid period {period!r}") from error
    interval = str(row["interval"])
    spec = ArchiveSpec(
        symbol=str(row["symbol"]),
        interval=cast(Literal["1m", "15m", "1h", "4h", "1d"], interval),
        year=month.year,
        month=month.month,
    )
    expected_key = (
        "data/futures/um/monthly/klines/"
        f"{spec.symbol}/{spec.interval}/{spec.filename}"
    )
    if str(row["key"]) != expected_key:
        raise NormalizationBatchError(f"planned key does not match {expected_key}")
    expected_size = int(row["size"])
    if expected_size < 0:
        raise NormalizationBatchError("planned object size must not be negative")
    return _WorkItem(spec=spec, expected_size=expected_size)


def _download_receipt(
    item: _WorkItem,
    *,
    raw_data_dir: Path,
    manifests_dir: Path,
) -> tuple[Path, ArchiveDownloadManifest]:
    archive = archive_path(raw_data_dir, item.spec)
    if not archive.is_file() or archive.stat().st_size != item.expected_size:
        raise NormalizationBatchError("local archive is absent or has the wrong planned size")
    source_hash = sha256_file(archive)
    receipt_path = (
        manifests_dir
        / "archive_download"
        / item.spec.symbol
        / item.spec.interval
        / f"{source_hash}.json"
    )
    if not receipt_path.is_file():
        raise NormalizationBatchError("verified download receipt is absent")
    receipt = ArchiveDownloadManifest.model_validate_json(
        receipt_path.read_text(encoding="utf-8")
    )
    if (
        receipt.symbol != item.spec.symbol
        or receipt.interval != item.spec.interval
        or receipt.period != item.spec.period
        or receipt.actual_sha256 != source_hash
        or receipt.expected_sha256 != source_hash
        or receipt.file_size != item.expected_size
    ):
        raise NormalizationBatchError("verified download receipt does not match archive")
    return archive, receipt


def _success_record(
    item: _WorkItem,
    manifest: ArchiveNormalizationManifest,
    parquet: Path,
) -> _SuccessRecord:
    return _SuccessRecord(
        identity=item.identity,
        source_file_hash=manifest.source_file_hash,
        normalized_content_hash=manifest.normalized_content_hash,
        normalized_row_count=manifest.normalized_row_count,
        first_open_time=manifest.first_open_time.isoformat(),
        last_open_time=manifest.last_open_time.isoformat(),
        parquet_sha256=manifest.parquet_sha256,
        parquet_bytes=parquet.stat().st_size,
    )


def _existing_normalization(
    item: _WorkItem,
    source_hash: str,
    *,
    normalized_data_dir: Path,
    manifests_dir: Path,
) -> tuple[ArchiveNormalizationManifest, Path] | None:
    receipt_dir = (
        manifests_dir / "archive_normalization" / item.spec.symbol / item.spec.interval
    )
    if not receipt_dir.is_dir():
        return None
    matches: list[tuple[ArchiveNormalizationManifest, Path]] = []
    for receipt_path in receipt_dir.glob("*.json"):
        try:
            manifest = ArchiveNormalizationManifest.model_validate_json(
                receipt_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        if (
            manifest.source_file_hash != source_hash
            or manifest.symbol != item.spec.symbol
            or manifest.interval != item.spec.interval
            or manifest.period != item.spec.period
        ):
            continue
        parquet = normalized_data_dir / manifest.output_relative_path
        if (
            parquet.is_file()
            and sha256_file(parquet) == manifest.parquet_sha256
        ):
            matches.append((manifest, parquet))
    if not matches:
        return None
    semantic = {
        (
            manifest.normalized_content_hash,
            manifest.parquet_sha256,
            manifest.normalized_row_count,
        )
        for manifest, _ in matches
    }
    if len(semantic) != 1:
        raise NormalizationBatchError("conflicting normalization receipts exist")
    return matches[0]


def _normalize_one(
    item: _WorkItem,
    *,
    raw_data_dir: Path,
    normalized_data_dir: Path,
    manifests_dir: Path,
    evaluated_at: datetime,
) -> tuple[Literal["normalized", "reused"], _SuccessRecord]:
    archive, download = _download_receipt(
        item,
        raw_data_dir=raw_data_dir,
        manifests_dir=manifests_dir,
    )
    existing = _existing_normalization(
        item,
        download.actual_sha256,
        normalized_data_dir=normalized_data_dir,
        manifests_dir=manifests_dir,
    )
    if existing is not None:
        manifest, parquet = existing
        return "reused", _success_record(item, manifest, parquet)
    parquet, manifest = normalize_archive_to_parquet(
        archive,
        download,
        item.spec,
        normalized_data_dir,
        evaluated_at=evaluated_at,
        require_full_period=False,
    )
    write_normalization_manifest(manifest, manifests_dir)
    return "normalized", _success_record(item, manifest, parquet)


def normalize_capacity_plan(
    rows: pd.DataFrame,
    *,
    plan_content_hash: str,
    raw_data_dir: Path,
    normalized_data_dir: Path,
    manifests_dir: Path,
    evaluated_at: datetime,
    max_workers: int = 1,
    progress: Callable[[int, int], None] | None = None,
) -> NormalizationBatchResult:
    """Validate every ZIP and write deterministic, resumable Parquet partitions."""

    if list(rows.columns) != list(CAPACITY_COLUMNS):
        raise NormalizationBatchError("capacity rows do not use the canonical schema")
    if max_workers < 1:
        raise NormalizationBatchError("max_workers must be at least 1")
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise NormalizationBatchError("evaluated_at must be timezone-aware")
    evaluated_at = evaluated_at.astimezone(UTC)

    items = [_work_item(row) for _, row in rows.iterrows()]
    identities = tuple(item.identity for item in items)
    if len(set(identities)) != len(identities):
        raise NormalizationBatchError("capacity plan contains duplicate archives")

    normalized_count = 0
    reused_count = 0
    successes: list[_SuccessRecord] = []
    failures: list[NormalizationFailure] = []
    executor_type = ThreadPoolExecutor if max_workers == 1 else ProcessPoolExecutor
    with executor_type(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _normalize_one,
                item,
                raw_data_dir=raw_data_dir,
                normalized_data_dir=normalized_data_dir,
                manifests_dir=manifests_dir,
                evaluated_at=evaluated_at,
            ): item
            for item in items
        }
        completed = 0
        for future in as_completed(futures):
            item = futures[future]
            try:
                status, record = future.result()
                successes.append(record)
                if status == "normalized":
                    normalized_count += 1
                else:
                    reused_count += 1
            except Exception as error:
                failures.append(
                    NormalizationFailure(
                        identity=item.identity,
                        error=f"{type(error).__name__}: {error}",
                    )
                )
            completed += 1
            if progress is not None:
                progress(completed, len(items))

    canonical_successes = tuple(sorted(successes, key=lambda item: item.identity))
    canonical_failures = tuple(sorted(failures, key=lambda item: item.identity))
    dataset_content_hash = _canonical_sha256(
        [record.semantic_dict() for record in canonical_successes]
    )
    normalized_row_count = sum(
        record.normalized_row_count for record in canonical_successes
    )
    normalized_parquet_bytes = sum(
        record.parquet_bytes for record in canonical_successes
    )
    result_payload = {
        "plan_content_hash": plan_content_hash.lower(),
        "requested_count": len(items),
        "normalized_count": normalized_count,
        "reused_count": reused_count,
        "failed_count": len(canonical_failures),
        "normalized_row_count": normalized_row_count,
        "normalized_parquet_bytes": normalized_parquet_bytes,
        "dataset_content_hash": dataset_content_hash,
        "failures": [item.model_dump(mode="json") for item in canonical_failures],
    }
    result_hash = _canonical_sha256(result_payload)
    return NormalizationBatchResult(
        plan_content_hash=plan_content_hash.lower(),
        requested_count=len(items),
        normalized_count=normalized_count,
        reused_count=reused_count,
        failed_count=len(canonical_failures),
        normalized_row_count=normalized_row_count,
        normalized_parquet_bytes=normalized_parquet_bytes,
        dataset_content_hash=dataset_content_hash,
        failures=canonical_failures,
        complete=not canonical_failures,
        result_hash=result_hash,
    )


def write_normalization_batch_result(
    result: NormalizationBatchResult,
    manifests_dir: Path,
) -> Path:
    """Persist one immutable aggregate normalization receipt."""

    destination = (
        manifests_dir / "normalization_batch" / f"{result.result_hash}.json"
    )
    content = (
        json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    if destination.exists():
        if destination.read_bytes() != content:
            raise NormalizationBatchError(
                f"existing content-addressed batch result changed: {destination}"
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
