"""Offline raw-to-Parquet revalidation for the multi-timeframe scanner input boundary."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Self

import pandas as pd
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, model_validator

from slagalpha.data.archive import (
    ArchiveDownloadManifest,
    ArchiveSpec,
    archive_path,
    sha256_file,
)
from slagalpha.data.klines import (
    CANDLE_SCHEMA_VERSION,
    PARSER_VERSION,
    ArchiveNormalizationManifest,
    _to_arrow_table,
    normalize_klines,
    normalized_parquet_path,
    read_archive_csv,
)


class CandleInputError(ValueError):
    """Scanner Candle evidence is inconsistent or incomplete."""


class CandlePartitionSource(BaseModel):
    """Exact saved evidence; its declarations are not exchange-signed provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec: ArchiveSpec
    download: ArchiveDownloadManifest
    normalization: ArchiveNormalizationManifest

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        spec, download, normalized = self.spec, self.download, self.normalization
        if spec.interval == "1m":
            raise ValueError("scan input partitions must use 15m, 1h, 4h or 1d")
        identity = (spec.symbol, spec.interval, spec.period)
        if any((item.symbol, item.interval, item.period) != identity
               for item in (download, normalized)):
            raise ValueError("partition receipts must match their exact archive identity")
        if (download.url, download.checksum_url, download.filename) != (
            spec.url, spec.checksum_url, spec.filename,
        ):
            raise ValueError("download receipt must identify the official archive path")
        if not (download.expected_sha256 == download.actual_sha256 == normalized.source_file_hash):
            raise ValueError("download and normalization source hashes disagree")
        expected_path = normalized_parquet_path(Path(), spec, normalized.source_file_hash)
        if normalized.output_relative_path != expected_path.as_posix():
            raise ValueError("normalized output must use its exact canonical partition path")
        for at in (download.verified_at, normalized.normalized_at,
                   normalized.first_open_time, normalized.last_open_time):
            if at.tzinfo is None or at.utcoffset() != timedelta(0):
                raise ValueError("partition receipt times must be UTC")
        if normalized.normalized_at < download.verified_at:
            raise ValueError("normalization cannot precede source verification")
        if (normalized.normalized_row_count <= 0 or normalized.parsed_row_count != (
            normalized.normalized_row_count + normalized.identical_duplicates_removed
        )):
            raise ValueError("normalization row counts do not reconcile")
        return self


def contained_file(project_dir: Path, relative: str) -> Path:
    """Reject traversal and symlinks before opening a local evidence file."""
    path = PurePosixPath(relative)
    if (not path.parts or path.is_absolute() or str(path) != relative or "\\" in relative
        or any(part == ".." or ":" in part for part in path.parts)):
        raise CandleInputError("Candle evidence path must be canonical and project-relative")
    candidate = project_dir
    if candidate.is_symlink():
        raise CandleInputError("Candle evidence root cannot be a symlink")
    for part in path.parts:
        candidate /= part
        if candidate.is_symlink():
            raise CandleInputError("Candle evidence path cannot contain a symlink")
    if not candidate.resolve().is_relative_to(project_dir.resolve()) or not candidate.is_file():
        raise CandleInputError("Candle evidence file is missing or outside project")
    return candidate


def load_verified_candle_partition(
    *, project_dir: Path, source: CandlePartitionSource,
) -> pd.DataFrame:
    """Recompute normalized values from the ZIP and compare every authoritative Parquet field."""
    source = CandlePartitionSource.model_validate(source.model_dump(mode="json"))
    spec, download, manifest = source.spec, source.download, source.normalization
    archive = contained_file(project_dir, archive_path(Path("data/raw"), spec).as_posix())
    parquet = contained_file(project_dir, "data/normalized/" + manifest.output_relative_path)
    if (archive.stat().st_size != download.file_size
        or sha256_file(archive) != download.actual_sha256):
        raise CandleInputError("raw archive no longer matches its verified download receipt")
    if sha256_file(parquet) != manifest.parquet_sha256:
        raise CandleInputError("Parquet file hash no longer matches normalization receipt")
    raw = read_archive_csv(archive, spec)
    normalized, duplicates, content_hash = normalize_klines(
        raw, spec, manifest.source_file_hash, evaluated_at=manifest.normalized_at,
        require_full_period=False,
    )
    if (len(raw), len(normalized), duplicates, content_hash,
        normalized["open_time"].iloc[0], normalized["open_time"].iloc[-1]) != (
        manifest.parsed_row_count, manifest.normalized_row_count,
        manifest.identical_duplicates_removed, manifest.normalized_content_hash,
        manifest.first_open_time, manifest.last_open_time,
    ):
        raise CandleInputError("normalization receipt does not match recomputed raw content")
    # Read the file itself, without inferring identity from unverified Hive directory names.
    table = pq.ParquetFile(parquet).read()
    expected_schema = _to_arrow_table(normalized, manifest.normalized_at).schema
    if not table.schema.equals(expected_schema, check_metadata=False):
        raise CandleInputError("Parquet physical schema does not match the frozen Candle schema")
    metadata = table.schema.metadata or {}
    expected_metadata = {
        b"candle_schema_version": CANDLE_SCHEMA_VERSION, b"parser_version": PARSER_VERSION,
        b"normalized_content_hash": content_hash, b"source_file_hash": manifest.source_file_hash,
        b"exchange": "BINANCE_USDM", b"symbol": spec.symbol, b"interval": spec.interval,
    }
    if any(metadata.get(key) != value.encode() for key, value in expected_metadata.items()):
        raise CandleInputError("Parquet metadata does not match its source identity and content")
    stored = table.to_pandas()
    identity_columns = ("exchange", "symbol", "interval")
    expected_columns = set(normalized.columns).difference(identity_columns) | {"ingested_at"}
    if len(stored.columns) != len(expected_columns) or set(stored.columns) != expected_columns:
        raise CandleInputError("Parquet columns do not match the frozen Candle schema")
    for column in identity_columns:
        stored[column] = normalized[column].iloc[0]
    try:
        # Arrow stores decimal scale 18; raw decimals retain their original scale. Compare
        # exact values, not their spelling, and never round through float64.
        pd.testing.assert_frame_equal(
            stored[list(normalized.columns)], normalized, check_dtype=False, check_exact=True,
        )
        observed_at = pd.DatetimeIndex(stored["ingested_at"])
        expected_at = _to_arrow_table(normalized.iloc[:1], manifest.normalized_at)[
            "ingested_at"
        ][0].as_py()
        if observed_at.tz is None or not bool(
            (observed_at == pd.Timestamp(expected_at)).all()
        ):
            raise CandleInputError("Parquet ingestion timestamps disagree with receipt")
    except (AssertionError, TypeError, KeyError, ValueError) as error:
        raise CandleInputError("Parquet values differ from recomputed raw Candles") from error
    return normalized
