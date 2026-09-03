"""Parse, validate, and normalize Binance USD-M monthly kline archives."""

from __future__ import annotations

import calendar
import hashlib
import json
import zipfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, field_validator

from slagalpha.data.archive import (
    ArchiveDownloadManifest,
    ArchiveSpec,
    sha256_file,
)

PARSER_VERSION = "binance-usdm-kline/0.1.0"
CANDLE_SCHEMA_VERSION = "candle/0.1.0"
NORMALIZATION_MANIFEST_SCHEMA = "archive-normalization/0.1.0"
MAX_UNCOMPRESSED_CSV_BYTES = 1024 * 1024 * 1024

RAW_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "base_volume",
    "source_close_time",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
]

DECIMAL_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "base_volume",
    "quote_volume",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
]

INTERVAL_MILLISECONDS = {
    "1m": 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


class KlineArchiveError(RuntimeError):
    """Base exception for malformed or invalid kline archive data."""


class ArchiveLayoutError(KlineArchiveError):
    """Raised when a ZIP does not contain the expected single CSV member."""


class CandleValidationError(KlineArchiveError):
    """Raised when a candle violates the normalized Candle contract."""


class DuplicateCandleConflictError(CandleValidationError):
    """Raised when one open_time has multiple different source rows."""


class CandleGapError(CandleValidationError):
    """Raised when adjacent source candles do not match the interval grid."""


class ArchiveNormalizationManifest(BaseModel):
    """Immutable evidence for one archive-to-Parquet normalization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["archive-normalization/0.1.0"] = (
        "archive-normalization/0.1.0"
    )
    parser_version: Literal["binance-usdm-kline/0.1.0"] = (
        "binance-usdm-kline/0.1.0"
    )
    source_file_hash: str
    symbol: str
    interval: str
    period: str
    parsed_row_count: int = Field(ge=0)
    normalized_row_count: int = Field(ge=0)
    identical_duplicates_removed: int = Field(ge=0)
    first_open_time: datetime
    last_open_time: datetime
    normalized_content_hash: str
    parquet_sha256: str
    output_relative_path: str
    normalized_at: datetime

    @field_validator("source_file_hash", "normalized_content_hash", "parquet_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        return normalized


def _expected_csv_filename(spec: ArchiveSpec) -> str:
    return f"{spec.filename.removesuffix('.zip')}.csv"


def read_archive_csv(archive: Path, spec: ArchiveSpec) -> pd.DataFrame:
    """Read exactly one expected CSV member as source strings."""

    expected_member = _expected_csv_filename(spec)
    try:
        with zipfile.ZipFile(archive) as bundle:
            csv_members = [info for info in bundle.infolist() if not info.is_dir()]
            if len(csv_members) != 1 or csv_members[0].filename != expected_member:
                names = [member.filename for member in csv_members]
                raise ArchiveLayoutError(
                    f"expected only {expected_member!r} in archive, found {names!r}"
                )
            if csv_members[0].file_size > MAX_UNCOMPRESSED_CSV_BYTES:
                raise ArchiveLayoutError("uncompressed CSV exceeds the 1 GiB safety limit")

            with bundle.open(csv_members[0]) as source:
                frame = pd.read_csv(
                    source,
                    header=None,
                    names=RAW_COLUMNS,
                    dtype=str,
                    keep_default_na=False,
                )
    except zipfile.BadZipFile as error:
        raise ArchiveLayoutError(f"invalid ZIP archive: {archive}") from error

    if frame.empty:
        raise ArchiveLayoutError("archive CSV is empty")

    first_value = str(frame.iloc[0]["open_time"]).strip().lower()
    if first_value in {"open_time", "open time"}:
        frame = frame.iloc[1:].reset_index(drop=True)
    if frame.empty:
        raise ArchiveLayoutError("archive CSV contains a header but no data")
    return frame


def _parse_integer_column(frame: pd.DataFrame, column: str) -> pd.Series:
    try:
        values = pd.to_numeric(frame[column], errors="raise")
    except (TypeError, ValueError) as error:
        raise CandleValidationError(f"{column} contains a non-integer value") from error

    fractional = values % 1 != 0
    if bool(fractional.any()):
        raise CandleValidationError(f"{column} contains a fractional value")
    try:
        return values.astype("int64")
    except (TypeError, ValueError, OverflowError) as error:
        raise CandleValidationError(f"{column} is outside int64 range") from error


def _parse_decimal(value: str, column: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise CandleValidationError(f"{column} contains an invalid decimal") from error
    if not parsed.is_finite():
        raise CandleValidationError(f"{column} contains a non-finite decimal")
    return parsed


def _deduplicate_source_rows(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    parsed_open_time = _parse_integer_column(frame, "open_time")
    working = frame.copy()
    working["open_time"] = parsed_open_time

    exact_deduplicated = working.drop_duplicates(ignore_index=True)
    duplicate_count = len(working) - len(exact_deduplicated)
    conflicting = exact_deduplicated.duplicated(subset=["open_time"], keep=False)
    if bool(conflicting.any()):
        conflict_times = sorted(
            int(value)
            for value in exact_deduplicated.loc[conflicting, "open_time"].unique()
        )
        raise DuplicateCandleConflictError(
            f"conflicting duplicate candles at open_time={conflict_times[:5]}"
        )
    return exact_deduplicated, duplicate_count


def normalize_klines(
    source_frame: pd.DataFrame,
    spec: ArchiveSpec,
    source_file_hash: str,
    *,
    evaluated_at: datetime | None = None,
    require_full_period: bool = False,
) -> tuple[pd.DataFrame, int, str]:
    """Validate source candles and return a deterministic normalized frame."""

    frame, duplicate_count = _deduplicate_source_rows(source_frame)
    frame = frame.sort_values("open_time", kind="stable").reset_index(drop=True)
    frame["source_close_time"] = _parse_integer_column(frame, "source_close_time")
    frame["trade_count"] = _parse_integer_column(frame, "trade_count")

    for column in DECIMAL_COLUMNS:
        frame[column] = frame[column].map(lambda value, name=column: _parse_decimal(value, name))

    interval_ms = INTERVAL_MILLISECONDS[spec.interval]
    open_times = frame["open_time"]
    if bool((open_times % interval_ms != 0).any()):
        raise CandleValidationError("open_time is not aligned to the interval grid")

    expected_close_times = open_times + interval_ms - 1
    if bool((frame["source_close_time"] != expected_close_times).any()):
        raise CandleValidationError("source_close_time does not equal interval end minus 1 ms")

    gaps = open_times.diff().iloc[1:]
    if bool((gaps != interval_ms).any()):
        bad_positions = [int(index) for index in gaps.index[gaps != interval_ms][:5]]
        raise CandleGapError(f"non-contiguous candle grid near row positions {bad_positions}")

    for index, row in frame.iterrows():
        open_price = row["open"]
        high_price = row["high"]
        low_price = row["low"]
        close_price = row["close"]
        if not all(
            isinstance(value, Decimal)
            for value in (open_price, high_price, low_price, close_price)
        ):
            raise CandleValidationError(f"price conversion failed at row {index}")
        if min(open_price, high_price, low_price, close_price) <= 0:
            raise CandleValidationError(f"price must be positive at row {index}")
        if low_price > min(open_price, close_price) or high_price < max(
            open_price, close_price
        ):
            raise CandleValidationError(f"OHLC relationship is invalid at row {index}")
        if low_price > high_price:
            raise CandleValidationError(f"low exceeds high at row {index}")

        for volume_column in (
            "base_volume",
            "quote_volume",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
        ):
            if row[volume_column] < 0:
                raise CandleValidationError(
                    f"{volume_column} must be non-negative at row {index}"
                )
        if row["trade_count"] < 0:
            raise CandleValidationError(f"trade_count must be non-negative at row {index}")

    open_datetimes = pd.to_datetime(open_times, unit="ms", utc=True)
    close_datetimes = pd.to_datetime(open_times + interval_ms, unit="ms", utc=True)
    month_start = datetime(spec.year, spec.month, 1, tzinfo=UTC)
    next_year = spec.year + (1 if spec.month == 12 else 0)
    next_month = 1 if spec.month == 12 else spec.month + 1
    next_month_start = datetime(next_year, next_month, 1, tzinfo=UTC)
    first_open = open_datetimes.iloc[0].to_pydatetime()
    last_close = close_datetimes.iloc[-1].to_pydatetime()
    if first_open < month_start or last_close > next_month_start:
        raise CandleValidationError("archive contains candles outside the requested month")

    as_of = evaluated_at or datetime.now(UTC)
    if as_of.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    if last_close > as_of.astimezone(UTC):
        raise CandleValidationError("archive contains a candle not closed at evaluation time")

    if require_full_period:
        expected_rows = (
            calendar.monthrange(spec.year, spec.month)[1]
            * 24
            * 60
            * 60
            * 1000
            // interval_ms
        )
        if first_open != month_start or last_close != next_month_start:
            raise CandleGapError("archive does not span the complete requested month")
        if len(frame) != expected_rows:
            raise CandleGapError(
                f"expected {expected_rows} rows for a full month, found {len(frame)}"
            )

    normalized = pd.DataFrame(
        {
            "schema_version": CANDLE_SCHEMA_VERSION,
            "exchange": "BINANCE_USDM",
            "symbol": spec.symbol,
            "interval": spec.interval,
            "open_time": open_datetimes,
            "close_time_exclusive": close_datetimes,
            "source_close_time": frame["source_close_time"],
            "open": frame["open"],
            "high": frame["high"],
            "low": frame["low"],
            "close": frame["close"],
            "base_volume": frame["base_volume"],
            "quote_volume": frame["quote_volume"],
            "trade_count": frame["trade_count"],
            "taker_buy_base_volume": frame["taker_buy_base_volume"],
            "taker_buy_quote_volume": frame["taker_buy_quote_volume"],
            "is_closed": True,
            "source": "PUBLIC_ARCHIVE",
            "source_file_hash": source_file_hash,
        }
    )
    content_hash = _normalized_content_hash(normalized)
    return normalized, duplicate_count, content_hash


def _normalized_content_hash(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    authority_columns = [
        "exchange",
        "symbol",
        "interval",
        "open_time",
        "close_time_exclusive",
        "source_close_time",
        *DECIMAL_COLUMNS[:4],
        "base_volume",
        "quote_volume",
        "trade_count",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
        "source_file_hash",
    ]
    for row in frame[authority_columns].itertuples(index=False, name=None):
        canonical_values = []
        for value in row:
            if isinstance(value, pd.Timestamp):
                canonical_values.append(value.isoformat())
            elif isinstance(value, Decimal):
                canonical_values.append(format(value, "f"))
            else:
                canonical_values.append(str(value))
        digest.update(("|".join(canonical_values) + "\n").encode())
    return digest.hexdigest()


def _to_arrow_table(frame: pd.DataFrame, normalized_at: datetime) -> pa.Table:
    decimal_type = pa.decimal128(38, 18)
    arrays: dict[str, pa.Array] = {
        "schema_version": pa.array(frame["schema_version"], type=pa.string()),
        "open_time": pa.Array.from_pandas(
            frame["open_time"], type=pa.timestamp("ms", tz="UTC")
        ),
        "close_time_exclusive": pa.Array.from_pandas(
            frame["close_time_exclusive"], type=pa.timestamp("ms", tz="UTC")
        ),
        "source_close_time": pa.array(frame["source_close_time"], type=pa.int64()),
        "trade_count": pa.array(frame["trade_count"], type=pa.int64()),
        "is_closed": pa.array(frame["is_closed"], type=pa.bool_()),
        "source": pa.array(frame["source"], type=pa.string()),
        "source_file_hash": pa.array(frame["source_file_hash"], type=pa.string()),
        "ingested_at": pa.array(
            [normalized_at] * len(frame), type=pa.timestamp("ms", tz="UTC")
        ),
    }
    for column in DECIMAL_COLUMNS:
        arrays[column] = pa.array(frame[column].tolist(), type=decimal_type)

    ordered_columns = [
        "schema_version",
        "open_time",
        "close_time_exclusive",
        "source_close_time",
        "open",
        "high",
        "low",
        "close",
        "base_volume",
        "quote_volume",
        "trade_count",
        "taker_buy_base_volume",
        "taker_buy_quote_volume",
        "is_closed",
        "source",
        "source_file_hash",
        "ingested_at",
    ]
    return pa.table({name: arrays[name] for name in ordered_columns})


def normalized_parquet_path(
    normalized_data_dir: Path, spec: ArchiveSpec, source_file_hash: str
) -> Path:
    return (
        normalized_data_dir
        / "klines"
        / "exchange=BINANCE_USDM"
        / f"interval={spec.interval}"
        / f"symbol={spec.symbol}"
        / f"year={spec.year:04d}"
        / f"month={spec.month:02d}"
        / f"part-{source_file_hash[:16]}.parquet"
    )


def write_normalized_parquet(
    frame: pd.DataFrame,
    destination: Path,
    *,
    normalized_at: datetime,
    normalized_content_hash: str,
    source_file_hash: str,
) -> str:
    """Write one content-addressed Parquet file atomically or validate the existing one."""

    if destination.exists():
        metadata = pq.read_metadata(destination)
        schema_metadata = metadata.metadata or {}
        if metadata.num_rows != len(frame):
            raise CandleValidationError("existing Parquet row count does not match input")
        if schema_metadata.get(b"normalized_content_hash", b"").decode() != normalized_content_hash:
            raise CandleValidationError("existing Parquet content hash metadata does not match")
        if schema_metadata.get(b"source_file_hash", b"").decode() != source_file_hash:
            raise CandleValidationError("existing Parquet source hash metadata does not match")
        return sha256_file(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    table = _to_arrow_table(frame, normalized_at)
    table = table.replace_schema_metadata(
        {
            b"candle_schema_version": CANDLE_SCHEMA_VERSION.encode(),
            b"parser_version": PARSER_VERSION.encode(),
            b"normalized_content_hash": normalized_content_hash.encode(),
            b"source_file_hash": source_file_hash.encode(),
            b"exchange": str(frame["exchange"].iloc[0]).encode(),
            b"symbol": str(frame["symbol"].iloc[0]).encode(),
            b"interval": str(frame["interval"].iloc[0]).encode(),
        }
    )
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        pq.write_table(table, temporary, compression="zstd", version="2.6")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return sha256_file(destination)


def normalize_archive_to_parquet(
    archive: Path,
    download_manifest: ArchiveDownloadManifest,
    spec: ArchiveSpec,
    normalized_data_dir: Path,
    *,
    evaluated_at: datetime | None = None,
    require_full_period: bool = False,
) -> tuple[Path, ArchiveNormalizationManifest]:
    """Run the complete local ZIP-to-validated-Parquet normalization step."""

    actual_source_hash = sha256_file(archive)
    if actual_source_hash != download_manifest.actual_sha256:
        raise CandleValidationError("archive hash changed after download verification")

    source_frame = read_archive_csv(archive, spec)
    normalized_frame, duplicate_count, content_hash = normalize_klines(
        source_frame,
        spec,
        actual_source_hash,
        evaluated_at=evaluated_at,
        require_full_period=require_full_period,
    )
    destination = normalized_parquet_path(normalized_data_dir, spec, actual_source_hash)
    if destination.exists():
        stored_times = pq.read_table(destination, columns=["ingested_at"])["ingested_at"]
        unique_times = set(stored_times.to_pylist())
        if len(unique_times) != 1 or None in unique_times:
            raise CandleValidationError("existing Parquet has invalid ingestion timestamps")
        normalized_at = next(iter(unique_times))
    else:
        now = datetime.now(UTC)
        normalized_at = now.replace(microsecond=now.microsecond // 1000 * 1000)
    parquet_hash = write_normalized_parquet(
        normalized_frame,
        destination,
        normalized_at=normalized_at,
        normalized_content_hash=content_hash,
        source_file_hash=actual_source_hash,
    )
    relative_output = destination.relative_to(normalized_data_dir).as_posix()
    manifest = ArchiveNormalizationManifest(
        source_file_hash=actual_source_hash,
        symbol=spec.symbol,
        interval=spec.interval,
        period=spec.period,
        parsed_row_count=len(source_frame),
        normalized_row_count=len(normalized_frame),
        identical_duplicates_removed=duplicate_count,
        first_open_time=normalized_frame["open_time"].iloc[0].to_pydatetime(),
        last_open_time=normalized_frame["open_time"].iloc[-1].to_pydatetime(),
        normalized_content_hash=content_hash,
        parquet_sha256=parquet_hash,
        output_relative_path=relative_output,
        normalized_at=normalized_at,
    )
    return destination, manifest


def write_normalization_manifest(
    manifest: ArchiveNormalizationManifest, manifests_dir: Path
) -> Path:
    """Append one content-addressed normalization manifest."""

    destination = (
        manifests_dir
        / "archive_normalization"
        / manifest.symbol
        / manifest.interval
        / f"{manifest.normalized_content_hash}.json"
    )
    if destination.exists():
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    payload: dict[str, Any] = manifest.model_dump(mode="json")
    try:
        temporary.write_text(
            f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)}\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
