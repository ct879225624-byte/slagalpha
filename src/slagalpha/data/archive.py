"""Download and verify Binance USD-M public monthly kline archives."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from slagalpha.domain.symbols import normalize_symbol

BINANCE_ARCHIVE_BASE_URL = "https://data.binance.vision/data/futures/um/monthly/klines"
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class ArchiveError(RuntimeError):
    """Base exception for a failed archive operation."""


class ChecksumFormatError(ArchiveError):
    """Raised when an official checksum response is malformed or mismatched."""


class ChecksumMismatchError(ArchiveError):
    """Raised when downloaded bytes do not match the official checksum."""


class ExistingArchiveMismatchError(ArchiveError):
    """Raised when a local archive differs from the currently published checksum."""


class ArchiveSpec(BaseModel):
    """Validated identity of one Binance monthly USD-M kline archive."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    interval: Literal["1m", "15m", "1h", "4h", "1d"]
    year: int = Field(ge=2019, le=2100)
    month: int = Field(ge=1, le=12)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return normalize_symbol(value)

    @property
    def period(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def filename(self) -> str:
        return f"{self.symbol}-{self.interval}-{self.period}.zip"

    @property
    def url(self) -> str:
        return (
            f"{BINANCE_ARCHIVE_BASE_URL}/{self.symbol}/{self.interval}/{self.filename}"
        )

    @property
    def checksum_url(self) -> str:
        return f"{self.url}.CHECKSUM"

    @classmethod
    def btcusdt_15m_january_2024(cls) -> Self:
        """Return the fixed P2 real-data acceptance sample."""

        return cls(symbol="BTCUSDT", interval="15m", year=2024, month=1)


class ArchiveDownloadManifest(BaseModel):
    """Immutable evidence that archive bytes matched an official checksum."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["archive-download/0.1.0"] = "archive-download/0.1.0"
    source: Literal["BINANCE_PUBLIC_ARCHIVE"] = "BINANCE_PUBLIC_ARCHIVE"
    symbol: str
    interval: str
    period: str
    url: str
    checksum_url: str
    filename: str
    expected_sha256: str
    actual_sha256: str
    checksum_verified: Literal[True] = True
    file_size: int = Field(ge=0)
    verified_at: datetime

    @field_validator("expected_sha256", "actual_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if not _SHA256_PATTERN.fullmatch(normalized):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        return normalized


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading it entirely into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksum(text: str, expected_filename: str) -> str:
    """Parse the official ``<sha256>  <filename>`` checksum format."""

    parts = text.strip().split()
    if len(parts) != 2:
        raise ChecksumFormatError("checksum must contain a hash and one filename")

    checksum, filename = parts
    filename = filename.removeprefix("*")
    if not _SHA256_PATTERN.fullmatch(checksum):
        raise ChecksumFormatError("checksum is not a valid SHA-256 value")
    if filename != expected_filename:
        raise ChecksumFormatError(
            f"checksum filename {filename!r} does not match {expected_filename!r}"
        )
    return checksum.lower()


def archive_path(raw_data_dir: Path, spec: ArchiveSpec) -> Path:
    """Return the stable raw archive location without creating directories."""

    return (
        raw_data_dir
        / "binance"
        / "futures"
        / "um"
        / "monthly"
        / "klines"
        / spec.symbol
        / spec.interval
        / spec.filename
    )


def _request_with_retry(
    client: httpx.Client,
    url: str,
    *,
    attempts: int,
    sleep: Callable[[float], None],
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url)
            response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            last_error = error
            retryable_status = (
                isinstance(error, httpx.TransportError)
                or error.response.status_code >= 500
                or error.response.status_code == 429
            )
            if not retryable_status or attempt == attempts:
                raise ArchiveError(f"failed to download {url}: {error}") from error
            sleep(0.25 * (2 ** (attempt - 1)))

    raise ArchiveError(f"failed to download {url}: {last_error}")


@contextmanager
def _managed_client(client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return

    with httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"User-Agent": "slagalpha/0.1.0"},
    ) as owned_client:
        yield owned_client


def fetch_archive(
    spec: ArchiveSpec,
    raw_data_dir: Path,
    *,
    client: httpx.Client | None = None,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Path, ArchiveDownloadManifest]:
    """Fetch one archive, verify SHA-256, and preserve conflicting local bytes."""

    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    destination = archive_path(raw_data_dir, spec)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with _managed_client(client) as active_client:
        checksum_response = _request_with_retry(
            active_client,
            spec.checksum_url,
            attempts=attempts,
            sleep=sleep,
        )
        expected_sha256 = parse_checksum(checksum_response.text, spec.filename)

        if destination.exists():
            actual_sha256 = sha256_file(destination)
            if actual_sha256 != expected_sha256:
                raise ExistingArchiveMismatchError(
                    f"existing archive {destination} has sha256 {actual_sha256}, "
                    f"official checksum is {expected_sha256}; local file was preserved"
                )
        else:
            archive_response = _request_with_retry(
                active_client,
                spec.url,
                attempts=attempts,
                sleep=sleep,
            )
            temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
            try:
                with temporary.open("wb") as output:
                    output.write(archive_response.content)
                actual_sha256 = sha256_file(temporary)
                if actual_sha256 != expected_sha256:
                    raise ChecksumMismatchError(
                        f"downloaded sha256 {actual_sha256} does not match "
                        f"official checksum {expected_sha256}"
                    )
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)

    manifest = ArchiveDownloadManifest(
        symbol=spec.symbol,
        interval=spec.interval,
        period=spec.period,
        url=spec.url,
        checksum_url=spec.checksum_url,
        filename=spec.filename,
        expected_sha256=expected_sha256,
        actual_sha256=actual_sha256,
        file_size=destination.stat().st_size,
        verified_at=datetime.now(UTC),
    )
    return destination, manifest


def write_download_manifest(
    manifest: ArchiveDownloadManifest, manifests_dir: Path
) -> Path:
    """Append a content-addressed manifest without rewriting an existing receipt."""

    destination = (
        manifests_dir
        / "archive_download"
        / manifest.symbol
        / manifest.interval
        / f"{manifest.actual_sha256}.json"
    )
    if destination.exists():
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    payload = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    try:
        temporary.write_text(f"{payload}\n", encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
