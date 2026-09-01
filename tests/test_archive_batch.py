"""Tests for resumable, checksum-verified archive batch downloads."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import pandas as pd

from slagalpha.data.archive import (
    ArchiveDownloadManifest,
    ArchiveSpec,
    archive_path,
)
from slagalpha.data.archive_batch import download_capacity_plan


def row(spec: ArchiveSpec, size: int) -> dict[str, object]:
    key = (
        "data/futures/um/monthly/klines/"
        f"{spec.symbol}/{spec.interval}/{spec.filename}"
    )
    return {
        "symbol": spec.symbol,
        "interval": spec.interval,
        "period": spec.period,
        "key": key,
        "size": size,
        "etag": "etag",
        "last_modified": "2024-03-02T00:00:00+00:00",
        "checksum_key": f"{key}.CHECKSUM",
        "checksum_present": True,
        "listing_hash": "a" * 64,
    }


def manifest(spec: ArchiveSpec, content: bytes) -> ArchiveDownloadManifest:
    sha256 = hashlib.sha256(content).hexdigest()
    return ArchiveDownloadManifest(
        symbol=spec.symbol,
        interval=spec.interval,
        period=spec.period,
        url=spec.url,
        checksum_url=spec.checksum_url,
        filename=spec.filename,
        expected_sha256=sha256,
        actual_sha256=sha256,
        file_size=len(content),
        verified_at=datetime(2024, 3, 2, tzinfo=UTC),
    )


def spec(interval: str = "15m") -> ArchiveSpec:
    return ArchiveSpec(
        symbol="AAAUSDT",
        interval=cast(Literal["1m", "15m", "1h", "4h", "1d"], interval),
        year=2024,
        month=2,
    )


def test_batch_downloads_and_writes_verification_receipt(tmp_path: Path) -> None:
    item = spec()
    content = b"verified zip"

    def fetch(current: ArchiveSpec, raw_dir: Path) -> tuple[Path, ArchiveDownloadManifest]:
        destination = archive_path(raw_dir, current)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(content)
        return destination, manifest(current, content)

    result = download_capacity_plan(
        pd.DataFrame([row(item, len(content))]),
        plan_content_hash="b" * 64,
        raw_data_dir=tmp_path / "raw",
        manifests_dir=tmp_path / "manifests",
        fetcher=fetch,
    )

    assert result.complete is True
    assert result.downloaded_count == 1
    assert result.reused_count == 0
    receipt = (
        tmp_path
        / "manifests"
        / "archive_download"
        / item.symbol
        / item.interval
        / f"{hashlib.sha256(content).hexdigest()}.json"
    )
    assert receipt.exists()


def test_batch_reuses_locally_verified_file_without_network(tmp_path: Path) -> None:
    item = spec()
    content = b"already here"
    destination = archive_path(tmp_path / "raw", item)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(content)
    receipt = (
        tmp_path
        / "manifests"
        / "archive_download"
        / item.symbol
        / item.interval
        / f"{hashlib.sha256(content).hexdigest()}.json"
    )
    receipt.parent.mkdir(parents=True)
    receipt.write_text(manifest(item, content).model_dump_json(), encoding="utf-8")

    def fail_fetch(
        current: ArchiveSpec, raw_dir: Path
    ) -> tuple[Path, ArchiveDownloadManifest]:
        raise AssertionError("network must not be called")

    result = download_capacity_plan(
        pd.DataFrame([row(item, len(content))]),
        plan_content_hash="b" * 64,
        raw_data_dir=tmp_path / "raw",
        manifests_dir=tmp_path / "manifests",
        fetcher=fail_fetch,
    )

    assert result.complete is True
    assert result.downloaded_count == 0
    assert result.reused_count == 1


def test_size_mismatch_is_a_durable_failure(tmp_path: Path) -> None:
    item = spec()
    content = b"short"

    def fetch(current: ArchiveSpec, raw_dir: Path) -> tuple[Path, ArchiveDownloadManifest]:
        destination = archive_path(raw_dir, current)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(content)
        return destination, manifest(current, content)

    result = download_capacity_plan(
        pd.DataFrame([row(item, len(content) + 1)]),
        plan_content_hash="b" * 64,
        raw_data_dir=tmp_path / "raw",
        manifests_dir=tmp_path / "manifests",
        fetcher=fetch,
    )

    assert result.complete is False
    assert result.failed_count == 1
    assert "planned object size" in result.failures[0].error
    assert not list((tmp_path / "manifests").rglob("*.json"))


def test_batch_rejects_unchecked_capacity_row(tmp_path: Path) -> None:
    item = spec()
    unchecked = row(item, 1)
    unchecked["checksum_present"] = False

    result = download_capacity_plan(
        pd.DataFrame([unchecked]),
        plan_content_hash="b" * 64,
        raw_data_dir=tmp_path / "raw",
        manifests_dir=tmp_path / "manifests",
        fetcher=lambda current, raw_dir: (_ for _ in ()).throw(AssertionError()),
    )

    assert result.complete is False
    assert result.failed_count == 1
    assert "checksum" in result.failures[0].error.lower()
