"""Tests for resumable local normalization of a capacity plan."""

from __future__ import annotations

import hashlib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from slagalpha.data.archive import ArchiveDownloadManifest, ArchiveSpec, archive_path
from slagalpha.data.normalization_batch import normalize_capacity_plan


def capacity_row(spec: ArchiveSpec, size: int) -> dict[str, object]:
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


def create_archive_and_receipt(base: Path) -> tuple[ArchiveSpec, Path]:
    spec = ArchiveSpec(symbol="AAAUSDT", interval="15m", year=2024, month=2)
    destination = archive_path(base / "raw", spec)
    destination.parent.mkdir(parents=True)
    start = 1_706_745_600_000
    rows = []
    for offset in (0, 900_000):
        open_time = start + offset
        rows.append(
            ",".join(
                [
                    str(open_time),
                    "100",
                    "110",
                    "90",
                    "105",
                    "10",
                    str(open_time + 899_999),
                    "1000",
                    "5",
                    "6",
                    "600",
                    "0",
                ]
            )
        )
    with zipfile.ZipFile(destination, "w") as bundle:
        bundle.writestr(spec.filename.replace(".zip", ".csv"), "\n".join(rows))
    sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    receipt = ArchiveDownloadManifest(
        symbol=spec.symbol,
        interval=spec.interval,
        period=spec.period,
        url=spec.url,
        checksum_url=spec.checksum_url,
        filename=spec.filename,
        expected_sha256=sha256,
        actual_sha256=sha256,
        file_size=destination.stat().st_size,
        verified_at=datetime(2024, 3, 1, tzinfo=UTC),
    )
    receipt_path = (
        base
        / "manifests"
        / "archive_download"
        / spec.symbol
        / spec.interval
        / f"{sha256}.json"
    )
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(receipt.model_dump_json(), encoding="utf-8")
    return spec, destination


def test_normalization_batch_is_resumable(tmp_path: Path) -> None:
    spec, archive = create_archive_and_receipt(tmp_path)
    frame = pd.DataFrame([capacity_row(spec, archive.stat().st_size)])

    first = normalize_capacity_plan(
        frame,
        plan_content_hash="b" * 64,
        raw_data_dir=tmp_path / "raw",
        normalized_data_dir=tmp_path / "normalized",
        manifests_dir=tmp_path / "manifests",
        evaluated_at=datetime(2024, 3, 1, tzinfo=UTC),
        max_workers=2,
    )
    second = normalize_capacity_plan(
        frame,
        plan_content_hash="b" * 64,
        raw_data_dir=tmp_path / "raw",
        normalized_data_dir=tmp_path / "normalized",
        manifests_dir=tmp_path / "manifests",
        evaluated_at=datetime(2024, 3, 1, tzinfo=UTC),
    )

    assert first.complete is True
    assert first.normalized_count == 1
    assert first.reused_count == 0
    assert first.normalized_row_count == 2
    assert second.complete is True
    assert second.normalized_count == 0
    assert second.reused_count == 1
    assert second.dataset_content_hash == first.dataset_content_hash


def test_missing_download_receipt_fails_closed(tmp_path: Path) -> None:
    spec, archive = create_archive_and_receipt(tmp_path)
    for receipt in (tmp_path / "manifests" / "archive_download").rglob("*.json"):
        receipt.unlink()

    result = normalize_capacity_plan(
        pd.DataFrame([capacity_row(spec, archive.stat().st_size)]),
        plan_content_hash="b" * 64,
        raw_data_dir=tmp_path / "raw",
        normalized_data_dir=tmp_path / "normalized",
        manifests_dir=tmp_path / "manifests",
        evaluated_at=datetime(2024, 3, 1, tzinfo=UTC),
    )

    assert result.complete is False
    assert result.failed_count == 1
    assert "download receipt" in result.failures[0].error
    assert not list((tmp_path / "normalized").rglob("*.parquet"))
