"""Resumable batch downloads bound to an accepted archive capacity plan."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, Self, cast
from uuid import uuid4

import httpx
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.archive import (
    ArchiveDownloadManifest,
    ArchiveSpec,
    archive_path,
    fetch_archive,
    sha256_file,
    write_download_manifest,
)
from slagalpha.data.capacity import CAPACITY_COLUMNS

ArchiveFetcher = Callable[
    [ArchiveSpec, Path], tuple[Path, ArchiveDownloadManifest]
]


class ArchiveBatchError(RuntimeError):
    """Raised when a batch plan or its persisted evidence is invalid."""


class ArchiveBatchFailure(BaseModel):
    """One failed planned archive and its bounded diagnostic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str
    error: str


class ArchiveBatchResult(BaseModel):
    """Deterministic completion summary for one capacity plan attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["archive-batch-result/0.1.0"] = (
        "archive-batch-result/0.1.0"
    )
    plan_content_hash: str
    requested_count: int = Field(ge=0)
    downloaded_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    verified_bytes: int = Field(ge=0)
    failures: tuple[ArchiveBatchFailure, ...]
    complete: bool
    result_hash: str

    @field_validator("plan_content_hash", "result_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("batch hashes must be SHA-256")
        return normalized

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.downloaded_count + self.reused_count + self.failed_count != (
            self.requested_count
        ):
            raise ValueError("batch counts do not reconcile")
        if self.failed_count != len(self.failures):
            raise ValueError("failed_count does not match failures")
        identities = tuple(item.identity for item in self.failures)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("failures must have unique canonical identities")
        if self.complete != (self.failed_count == 0):
            raise ValueError("complete does not match failed_count")
        expected_hash = _result_hash(
            plan_content_hash=self.plan_content_hash,
            requested_count=self.requested_count,
            downloaded_count=self.downloaded_count,
            reused_count=self.reused_count,
            failed_count=self.failed_count,
            verified_bytes=self.verified_bytes,
            failures=self.failures,
        )
        if self.result_hash != expected_hash:
            raise ValueError("result_hash does not match batch content")
        return self


@dataclass(frozen=True)
class _WorkItem:
    spec: ArchiveSpec
    expected_key: str
    expected_size: int

    @property
    def identity(self) -> str:
        return f"{self.spec.symbol}/{self.spec.interval}/{self.spec.period}"


def _canonical_sha256(value: Any) -> str:
    content = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(content).hexdigest()


def _result_hash(
    *,
    plan_content_hash: str,
    requested_count: int,
    downloaded_count: int,
    reused_count: int,
    failed_count: int,
    verified_bytes: int,
    failures: tuple[ArchiveBatchFailure, ...],
) -> str:
    return _canonical_sha256(
        {
            "plan_content_hash": plan_content_hash,
            "requested_count": requested_count,
            "downloaded_count": downloaded_count,
            "reused_count": reused_count,
            "failed_count": failed_count,
            "verified_bytes": verified_bytes,
            "failures": [item.model_dump(mode="json") for item in failures],
        }
    )


def _work_item(row: pd.Series) -> _WorkItem:
    symbol = str(row["symbol"])
    interval = str(row["interval"])
    period = str(row["period"])
    try:
        month = date.fromisoformat(f"{period}-01")
    except ValueError as error:
        raise ArchiveBatchError(f"invalid period {period!r}") from error
    spec = ArchiveSpec(
        symbol=symbol,
        interval=cast(Literal["1m", "15m", "1h", "4h", "1d"], interval),
        year=month.year,
        month=month.month,
    )
    expected_key = (
        "data/futures/um/monthly/klines/"
        f"{spec.symbol}/{spec.interval}/{spec.filename}"
    )
    if str(row["key"]) != expected_key:
        raise ArchiveBatchError(f"planned key does not match identity: {expected_key}")
    expected_size = int(row["size"])
    if expected_size < 0:
        raise ArchiveBatchError("planned object size must not be negative")
    return _WorkItem(
        spec=spec,
        expected_key=expected_key,
        expected_size=expected_size,
    )


def _valid_existing_receipt(
    item: _WorkItem,
    raw_data_dir: Path,
    manifests_dir: Path,
) -> bool:
    destination = archive_path(raw_data_dir, item.spec)
    if not destination.is_file() or destination.stat().st_size != item.expected_size:
        return False
    actual_sha256 = sha256_file(destination)
    receipt_path = (
        manifests_dir
        / "archive_download"
        / item.spec.symbol
        / item.spec.interval
        / f"{actual_sha256}.json"
    )
    if not receipt_path.is_file():
        return False
    try:
        receipt = ArchiveDownloadManifest.model_validate_json(
            receipt_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    return (
        receipt.symbol == item.spec.symbol
        and receipt.interval == item.spec.interval
        and receipt.period == item.spec.period
        and receipt.filename == item.spec.filename
        and receipt.actual_sha256 == actual_sha256
        and receipt.expected_sha256 == actual_sha256
        and receipt.file_size == item.expected_size
        and receipt.checksum_verified is True
    )


def _download_one(
    item: _WorkItem,
    *,
    raw_data_dir: Path,
    manifests_dir: Path,
    fetcher: ArchiveFetcher,
) -> tuple[Literal["downloaded", "reused"], int]:
    if _valid_existing_receipt(item, raw_data_dir, manifests_dir):
        return "reused", item.expected_size
    destination, receipt = fetcher(item.spec, raw_data_dir)
    actual_size = destination.stat().st_size
    if actual_size != item.expected_size or receipt.file_size != item.expected_size:
        raise ArchiveBatchError(
            f"planned object size {item.expected_size} differs from verified file size "
            f"{actual_size}"
        )
    if (
        receipt.symbol != item.spec.symbol
        or receipt.interval != item.spec.interval
        or receipt.period != item.spec.period
        or receipt.filename != item.spec.filename
        or receipt.actual_sha256 != sha256_file(destination)
    ):
        raise ArchiveBatchError("download receipt does not match verified local archive")
    write_download_manifest(receipt, manifests_dir)
    return "downloaded", actual_size


def download_capacity_plan(
    rows: pd.DataFrame,
    *,
    plan_content_hash: str,
    raw_data_dir: Path,
    manifests_dir: Path,
    fetcher: ArchiveFetcher | None = None,
    max_workers: int = 1,
    progress: Callable[[int, int], None] | None = None,
) -> ArchiveBatchResult:
    """Download every checked plan row with per-object receipts and safe resume."""

    if list(rows.columns) != list(CAPACITY_COLUMNS):
        raise ArchiveBatchError("capacity rows do not use the canonical schema")
    if max_workers < 1:
        raise ArchiveBatchError("max_workers must be at least 1")
    if len(plan_content_hash) != 64 or any(
        character not in "0123456789abcdef" for character in plan_content_hash.lower()
    ):
        raise ArchiveBatchError("plan_content_hash must be a SHA-256")

    items: list[_WorkItem] = []
    failures: list[ArchiveBatchFailure] = []
    seen_identities: set[str] = set()
    for _, row in rows.iterrows():
        item = _work_item(row)
        if item.identity in seen_identities:
            raise ArchiveBatchError(f"duplicate planned archive: {item.identity}")
        seen_identities.add(item.identity)
        if not bool(row["checksum_present"]):
            failures.append(
                ArchiveBatchFailure(
                    identity=item.identity,
                    error="official checksum object is absent",
                )
            )
            continue
        items.append(item)

    downloaded_count = 0
    reused_count = 0
    verified_bytes = 0

    def run(active_fetcher: ArchiveFetcher) -> None:
        nonlocal downloaded_count, reused_count, verified_bytes
        completed = len(failures)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _download_one,
                    item,
                    raw_data_dir=raw_data_dir,
                    manifests_dir=manifests_dir,
                    fetcher=active_fetcher,
                ): item
                for item in items
            }
            for future in as_completed(futures):
                item = futures[future]
                try:
                    status, size = future.result()
                    verified_bytes += size
                    if status == "downloaded":
                        downloaded_count += 1
                    else:
                        reused_count += 1
                except Exception as error:
                    failures.append(
                        ArchiveBatchFailure(
                            identity=item.identity,
                            error=f"{type(error).__name__}: {error}",
                        )
                    )
                completed += 1
                if progress is not None:
                    progress(completed, len(rows))

    if fetcher is not None:
        run(fetcher)
    else:
        with httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": "slagalpha/0.1.0"},
        ) as client:
            run(
                lambda spec, raw_dir: fetch_archive(
                    spec,
                    raw_dir,
                    client=client,
                )
            )

    canonical_failures = tuple(sorted(failures, key=lambda item: item.identity))
    result_hash = _result_hash(
        plan_content_hash=plan_content_hash.lower(),
        requested_count=len(rows),
        downloaded_count=downloaded_count,
        reused_count=reused_count,
        failed_count=len(canonical_failures),
        verified_bytes=verified_bytes,
        failures=canonical_failures,
    )
    return ArchiveBatchResult(
        plan_content_hash=plan_content_hash.lower(),
        requested_count=len(rows),
        downloaded_count=downloaded_count,
        reused_count=reused_count,
        failed_count=len(canonical_failures),
        verified_bytes=verified_bytes,
        failures=canonical_failures,
        complete=not canonical_failures,
        result_hash=result_hash,
    )


def write_archive_batch_result(
    result: ArchiveBatchResult,
    manifests_dir: Path,
) -> Path:
    """Persist one content-addressed batch completion or failure report."""

    destination = manifests_dir / "archive_batch" / f"{result.result_hash}.json"
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
            raise ArchiveBatchError(
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
