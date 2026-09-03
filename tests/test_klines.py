"""Tests for deterministic kline validation and Parquet normalization."""

from __future__ import annotations

import hashlib
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from slagalpha.data.archive import ArchiveDownloadManifest, ArchiveSpec
from slagalpha.data.klines import (
    RAW_COLUMNS,
    ArchiveLayoutError,
    CandleGapError,
    CandleValidationError,
    DuplicateCandleConflictError,
    normalize_archive_to_parquet,
    normalize_klines,
    read_archive_csv,
)

INTERVAL_MS = 15 * 60_000
START_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z


def source_row(
    open_time: int,
    *,
    open_price: str = "100.00",
    high: str = "110.00",
    low: str = "90.00",
    close: str = "105.00",
    quote_volume: str = "1000.00",
) -> list[str]:
    return [
        str(open_time),
        open_price,
        high,
        low,
        close,
        "10.00",
        str(open_time + INTERVAL_MS - 1),
        quote_volume,
        "5",
        "6.00",
        "600.00",
        "0",
    ]


def source_frame(*rows: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=RAW_COLUMNS, dtype=str)


def test_normalize_valid_candles_uses_utc_decimal_and_semantic_hash() -> None:
    spec = ArchiveSpec.btcusdt_15m_january_2024()
    frame = source_frame(
        source_row(START_MS),
        source_row(START_MS + INTERVAL_MS),
        source_row(START_MS + 2 * INTERVAL_MS),
    )

    normalized, duplicates, content_hash = normalize_klines(
        frame,
        spec,
        "a" * 64,
        evaluated_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    repeated, _, repeated_hash = normalize_klines(
        frame,
        spec,
        "a" * 64,
        evaluated_at=datetime(2024, 2, 1, tzinfo=UTC),
    )

    assert duplicates == 0
    assert normalized["open"].iloc[0] == Decimal("100.00")
    assert str(normalized["open_time"].dtype) == "datetime64[ms, UTC]"
    assert content_hash == repeated_hash
    pd.testing.assert_frame_equal(normalized, repeated)


def test_identical_duplicate_is_removed() -> None:
    row = source_row(START_MS)
    frame = source_frame(row, row, source_row(START_MS + INTERVAL_MS))

    normalized, duplicates, _ = normalize_klines(
        frame,
        ArchiveSpec.btcusdt_15m_january_2024(),
        "a" * 64,
        evaluated_at=datetime(2024, 2, 1, tzinfo=UTC),
    )

    assert len(normalized) == 2
    assert duplicates == 1


def test_conflicting_duplicate_fails_closed() -> None:
    frame = source_frame(
        source_row(START_MS, close="105.00"),
        source_row(START_MS, close="106.00"),
    )

    with pytest.raises(DuplicateCandleConflictError):
        normalize_klines(
            frame,
            ArchiveSpec.btcusdt_15m_january_2024(),
            "a" * 64,
            evaluated_at=datetime(2024, 2, 1, tzinfo=UTC),
        )


def test_candle_gap_fails_closed() -> None:
    frame = source_frame(
        source_row(START_MS),
        source_row(START_MS + 2 * INTERVAL_MS),
    )

    with pytest.raises(CandleGapError):
        normalize_klines(
            frame,
            ArchiveSpec.btcusdt_15m_january_2024(),
            "a" * 64,
            evaluated_at=datetime(2024, 2, 1, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (source_row(START_MS, high="99.00"), "OHLC"),
        (source_row(START_MS, quote_volume="-1"), "quote_volume"),
        (source_row(START_MS, open_price="NaN"), "non-finite"),
    ],
)
def test_invalid_numeric_candle_fails_closed(row: list[str], message: str) -> None:
    with pytest.raises(CandleValidationError, match=message):
        normalize_klines(
            source_frame(row),
            ArchiveSpec.btcusdt_15m_january_2024(),
            "a" * 64,
            evaluated_at=datetime(2024, 2, 1, tzinfo=UTC),
        )


def test_unclosed_candle_at_evaluation_time_fails() -> None:
    with pytest.raises(CandleValidationError, match="not closed"):
        normalize_klines(
            source_frame(source_row(START_MS)),
            ArchiveSpec.btcusdt_15m_january_2024(),
            "a" * 64,
            evaluated_at=datetime(2024, 1, 1, tzinfo=UTC),
        )


def test_read_archive_accepts_expected_csv_with_header(tmp_path: Path) -> None:
    spec = ArchiveSpec.btcusdt_15m_january_2024()
    archive = tmp_path / spec.filename
    csv_text = ",".join(RAW_COLUMNS) + "\n" + ",".join(source_row(START_MS)) + "\n"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("BTCUSDT-15m-2024-01.csv", csv_text)

    frame = read_archive_csv(archive, spec)
    assert len(frame) == 1
    assert frame["open_time"].iloc[0] == str(START_MS)


def test_read_archive_rejects_unexpected_member(tmp_path: Path) -> None:
    spec = ArchiveSpec.btcusdt_15m_january_2024()
    archive = tmp_path / spec.filename
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("wrong.csv", "data")

    with pytest.raises(ArchiveLayoutError, match="expected only"):
        read_archive_csv(archive, spec)


def test_archive_to_parquet_preserves_decimal_schema_and_metadata(tmp_path: Path) -> None:
    spec = ArchiveSpec.btcusdt_15m_january_2024()
    archive = tmp_path / spec.filename
    csv_text = "\n".join(
        [
            ",".join(source_row(START_MS)),
            ",".join(source_row(START_MS + INTERVAL_MS)),
        ]
    )
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("BTCUSDT-15m-2024-01.csv", csv_text)
    source_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    download = ArchiveDownloadManifest(
        symbol=spec.symbol,
        interval=spec.interval,
        period=spec.period,
        url=spec.url,
        checksum_url=spec.checksum_url,
        filename=spec.filename,
        expected_sha256=source_hash,
        actual_sha256=source_hash,
        file_size=archive.stat().st_size,
        verified_at=datetime(2024, 2, 1, tzinfo=UTC),
    )

    parquet_path, manifest = normalize_archive_to_parquet(
        archive,
        download,
        spec,
        tmp_path / "normalized",
        evaluated_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    table = pq.read_table(parquet_path)

    assert table.num_rows == 2
    assert str(table.schema.field("open").type) == "decimal128(38, 18)"
    assert table.schema.metadata is not None
    assert table.schema.metadata[b"normalized_content_hash"].decode() == (
        manifest.normalized_content_hash
    )

    repeated_path, repeated_manifest = normalize_archive_to_parquet(
        archive, download, spec, tmp_path / "normalized",
        evaluated_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    assert repeated_path == parquet_path
    assert repeated_manifest == manifest
    assert table["ingested_at"][0].as_py() == manifest.normalized_at
