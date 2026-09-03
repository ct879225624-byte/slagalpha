"""Synthetic local archives and Parquet; never official rule or strategy research evidence."""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from slagalpha.data.archive import ArchiveDownloadManifest, ArchiveSpec, archive_path, sha256_file
from slagalpha.data.klines import INTERVAL_MILLISECONDS, RAW_COLUMNS, normalize_archive_to_parquet
from slagalpha.research.candle_inputs import (
    CandleInputError,
    CandlePartitionSource,
    contained_file,
    load_verified_candle_partition,
)
from test_klines import source_row


def _partition(
    root: Path, *, start: datetime = datetime(2024, 1, 1, tzinfo=UTC),
    interval: str = "15m", rows: int = 3, close_prices: tuple[str, ...] | None = None,
    row_overrides: dict[int, dict[str, str]] | None = None,
) -> CandlePartitionSource:
    spec = ArchiveSpec.model_validate({
        "symbol": "BTCUSDT", "interval": interval, "year": start.year, "month": start.month,
    })
    delta = INTERVAL_MILLISECONDS[interval]
    raw_rows = []
    for index in range(rows):
        at = int(start.timestamp() * 1000) + index * delta
        row = source_row(at)
        if close_prices is not None:
            price = Decimal(close_prices[index])
            row[1:5] = [str(price), str(price + 2), str(price - 2), str(price)]
        row[6] = str(at + delta - 1)
        for column, value in (row_overrides or {}).get(index, {}).items():
            row[RAW_COLUMNS.index(column)] = value
        raw_rows.append(",".join(row))
    archive = archive_path(root / "data/raw", spec)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(spec.filename.replace(".zip", ".csv"), "\n".join(raw_rows))
    digest = sha256_file(archive)
    download = ArchiveDownloadManifest(
        symbol=spec.symbol, interval=interval, period=spec.period,
        url=spec.url, checksum_url=spec.checksum_url, filename=spec.filename,
        actual_sha256=digest, expected_sha256=digest, file_size=archive.stat().st_size,
        verified_at=datetime(2024, 3, 1, tzinfo=UTC),
    )
    _, normalization = normalize_archive_to_parquet(
        archive, download, spec, root / "data/normalized",
        evaluated_at=datetime(2024, 3, 1, tzinfo=UTC),
    )
    return CandlePartitionSource(spec=spec, download=download, normalization=normalization)


@pytest.mark.parametrize("interval", ["15m", "1h", "4h", "1d"])
def test_exact_raw_decimal_values_survive_full_source_revalidation(
    tmp_path: Path, interval: str,
) -> None:
    source = _partition(tmp_path, interval=interval)
    frame = load_verified_candle_partition(project_dir=tmp_path, source=source)
    assert len(frame) == 3
    assert frame["close"].tolist() == [Decimal("105.00")] * 3
    assert frame["is_closed"].tolist() == [True] * 3
    assert frame["source"].unique().tolist() == ["PUBLIC_ARCHIVE"]
    pd.testing.assert_frame_equal(
        frame, load_verified_candle_partition(project_dir=tmp_path, source=source),
    )


@pytest.mark.parametrize("kind", ["raw", "parquet", "missing"])
def test_modified_or_missing_files_fail_closed(tmp_path: Path, kind: str) -> None:
    source = _partition(tmp_path)
    path = (archive_path(tmp_path / "data/raw", source.spec) if kind == "raw" else
            tmp_path / "data/normalized" / source.normalization.output_relative_path)
    if kind == "missing":
        path.unlink()
    else:
        path.write_bytes(b"changed")
    with pytest.raises(CandleInputError, match="hash|receipt|missing"):
        load_verified_candle_partition(project_dir=tmp_path, source=source)


@pytest.mark.parametrize("change", ["price", "closed", "metadata", "schema", "ingested_at"])
def test_rehashed_parquet_cannot_override_original_zip(tmp_path: Path, change: str) -> None:
    source = _partition(tmp_path)
    path = tmp_path / "data/normalized" / source.normalization.output_relative_path
    table = pq.ParquetFile(path).read()
    if change == "metadata":
        table = table.replace_schema_metadata({**table.schema.metadata, b"symbol": b"ETHUSDT"})
    else:
        column = {"price": "close", "closed": "is_closed", "schema": "is_closed",
                  "ingested_at": "ingested_at"}[change]
        value: Any = {"price": Decimal("106"), "closed": False, "schema": 1,
                      "ingested_at": datetime(2024, 3, 1, tzinfo=UTC)}[change]
        data_type = pa.int8() if change == "schema" else table.schema.field(column).type
        table = table.set_column(
            table.schema.get_field_index(column), column, pa.array([value] * 3, type=data_type),
        )
    pq.write_table(table, path)
    forged = source.model_copy(update={"normalization": source.normalization.model_copy(update={
        "parquet_sha256": sha256_file(path),
    })})
    with pytest.raises(CandleInputError, match="Parquet"):
        load_verified_candle_partition(project_dir=tmp_path, source=forged)


@pytest.mark.parametrize("updates", [
    {"symbol": "ETHUSDT"}, {"period": "2024-02"}, {"expected_sha256": "0" * 64},
    {"url": "https://example.com/forged.zip"}, {"verified_at": "2024-03-01T00:00:00"},
])
def test_download_identity_mismatch_rejected_before_file_io(
    tmp_path: Path, updates: dict[str, Any],
) -> None:
    payload = _partition(tmp_path).model_dump(mode="json")
    payload["download"].update(updates)
    with pytest.raises(ValidationError):
        CandlePartitionSource.model_validate(payload)


@pytest.mark.parametrize("updates", [
    {"normalized_row_count": 4}, {"normalized_content_hash": "0" * 64},
    {"first_open_time": datetime(2024, 1, 1, 0, 15, tzinfo=UTC)},
    {"output_relative_path": "../escape.parquet"},
])
def test_normalization_receipt_is_not_trusted(tmp_path: Path, updates: dict[str, Any]) -> None:
    source = _partition(tmp_path)
    source = source.model_copy(update={
        "normalization": source.normalization.model_copy(update=updates),
    })
    with pytest.raises(ValueError):
        load_verified_candle_partition(project_dir=tmp_path, source=source)


@pytest.mark.parametrize("relative", ["../outside", "/absolute", "C:/absolute", "a\\b", "a/./b"])
def test_untrusted_paths_are_rejected(tmp_path: Path, relative: str) -> None:
    with pytest.raises(CandleInputError, match="canonical"):
        contained_file(tmp_path, relative)
