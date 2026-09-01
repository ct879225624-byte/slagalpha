"""Versioned Binance Public Data inventory snapshots and download planning."""

from __future__ import annotations

import hashlib
import json
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Self
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.data.archive import ArchiveSpec

PUBLIC_DATA_S3_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
MONTHLY_KLINE_PREFIX = "data/futures/um/monthly/klines/"


class ArchiveInventoryError(RuntimeError):
    """Raised when official inventory data is malformed or incomplete."""


class ArchiveObject(BaseModel):
    """One immutable S3 object descriptor from the official listing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    last_modified: datetime
    etag: str
    size: int = Field(ge=0)

    @field_validator("key", "etag")
    @classmethod
    def validate_text(cls, value: str, info: object) -> str:
        normalized = value.strip()
        if not normalized:
            field_name = getattr(info, "field_name", "inventory field")
            raise ValueError(f"{field_name} must not be empty")
        return normalized

    @field_validator("last_modified")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_modified must be timezone-aware")
        return value.astimezone(UTC)


class ArchiveInventory(BaseModel):
    """Content-addressed result of one complete official prefix listing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["archive-inventory/0.1.0"] = "archive-inventory/0.1.0"
    source_url: Literal[
        "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
    ] = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
    prefix: str
    fetched_at: datetime
    objects: tuple[ArchiveObject, ...]
    common_prefixes: tuple[str, ...]
    is_truncated: bool = False
    next_marker: str | None = None
    listing_hash: str

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("prefix must be a non-root relative object prefix")
        return normalized

    @field_validator("fetched_at")
    @classmethod
    def validate_fetched_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("listing_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("listing_hash must be a SHA-256")
        return normalized

    @model_validator(mode="after")
    def validate_canonical_content(self) -> Self:
        object_keys = tuple(item.key for item in self.objects)
        if object_keys != tuple(sorted(object_keys)) or len(set(object_keys)) != len(
            object_keys
        ):
            raise ValueError("inventory objects must have unique keys in canonical order")
        if self.common_prefixes != tuple(sorted(set(self.common_prefixes))):
            raise ValueError("common_prefixes must be unique and canonical")
        if any(not value.startswith(self.prefix) for value in self.common_prefixes):
            raise ValueError("common prefix lies outside inventory prefix")
        if any(not item.key.startswith(self.prefix) for item in self.objects):
            raise ValueError("object key lies outside inventory prefix")
        if self.is_truncated and not self.next_marker:
            raise ValueError("truncated inventory page requires next_marker")
        if not self.is_truncated and self.next_marker is not None:
            raise ValueError("complete inventory must not have next_marker")
        expected_hash = _listing_hash(self.prefix, self.objects, self.common_prefixes)
        if self.listing_hash != expected_hash:
            raise ValueError("listing_hash does not match inventory content")
        return self

    @property
    def archived_symbols(self) -> tuple[str, ...]:
        """Return symbol directory names for the monthly-kline root inventory."""

        if self.prefix != MONTHLY_KLINE_PREFIX:
            return ()
        symbols = []
        for value in self.common_prefixes:
            relative = value.removeprefix(self.prefix).strip("/")
            if relative and "/" not in relative:
                symbols.append(relative)
        return tuple(symbols)


class MonthlyArchivePlan(BaseModel):
    """Immutable no-download plan for one symbol, interval and month range."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["monthly-archive-plan/0.1.0"] = (
        "monthly-archive-plan/0.1.0"
    )
    symbol: str
    interval: Literal["1m", "15m", "1h", "4h", "1d"]
    start_period: str
    end_period_exclusive: str
    inventory_hash: str
    available: tuple[ArchiveSpec, ...]
    missing_periods: tuple[str, ...]
    plan_hash: str

    @field_validator("inventory_hash", "plan_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("plan hashes must be SHA-256")
        return normalized

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        periods = tuple(spec.period for spec in self.available)
        if periods != tuple(sorted(set(periods))):
            raise ValueError("available specs must use unique chronological periods")
        if any(
            spec.symbol != self.symbol or spec.interval != self.interval
            for spec in self.available
        ):
            raise ValueError("available spec lies outside plan identity")
        if self.missing_periods != tuple(sorted(set(self.missing_periods))):
            raise ValueError("missing periods must be unique and chronological")
        expected_hash = _plan_hash(
            symbol=self.symbol,
            interval=self.interval,
            start_period=self.start_period,
            end_period_exclusive=self.end_period_exclusive,
            inventory_hash=self.inventory_hash,
            available=self.available,
            missing_periods=self.missing_periods,
        )
        if self.plan_hash != expected_hash:
            raise ValueError("plan_hash does not match plan content")
        return self


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _listing_hash(
    prefix: str,
    objects: tuple[ArchiveObject, ...],
    common_prefixes: tuple[str, ...],
) -> str:
    return _canonical_sha256(
        {
            "prefix": prefix,
            "objects": [item.model_dump(mode="json") for item in objects],
            "common_prefixes": common_prefixes,
        }
    )


def _plan_hash(
    *,
    symbol: str,
    interval: str,
    start_period: str,
    end_period_exclusive: str,
    inventory_hash: str,
    available: tuple[ArchiveSpec, ...],
    missing_periods: tuple[str, ...],
) -> str:
    return _canonical_sha256(
        {
            "symbol": symbol,
            "interval": interval,
            "start_period": start_period,
            "end_period_exclusive": end_period_exclusive,
            "inventory_hash": inventory_hash,
            "available": [spec.model_dump(mode="json") for spec in available],
            "missing_periods": missing_periods,
        }
    )


def _child_text(root: ET.Element, name: str, *, required: bool = True) -> str | None:
    child = root.find(f"{{*}}{name}")
    if child is None or child.text is None:
        if required:
            raise ArchiveInventoryError(f"S3 listing is missing {name}")
        return None
    return child.text


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ArchiveInventoryError("S3 LastModified is not an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ArchiveInventoryError("S3 LastModified must be timezone-aware")
    return parsed.astimezone(UTC)


def _build_inventory(
    *,
    prefix: str,
    fetched_at: datetime,
    objects: tuple[ArchiveObject, ...],
    common_prefixes: tuple[str, ...],
    is_truncated: bool = False,
    next_marker: str | None = None,
) -> ArchiveInventory:
    canonical_objects = tuple(sorted(objects, key=lambda item: item.key))
    canonical_prefixes = tuple(sorted(set(common_prefixes)))
    return ArchiveInventory(
        prefix=prefix,
        fetched_at=fetched_at,
        objects=canonical_objects,
        common_prefixes=canonical_prefixes,
        is_truncated=is_truncated,
        next_marker=next_marker,
        listing_hash=_listing_hash(prefix, canonical_objects, canonical_prefixes),
    )


def parse_s3_inventory_page(
    xml_text: str,
    *,
    expected_prefix: str,
    fetched_at: datetime | None = None,
) -> ArchiveInventory:
    """Parse one S3 ListBucket page without trusting HTML directory rendering."""

    upper = xml_text[:4096].upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ArchiveInventoryError("DOCTYPE and ENTITY are forbidden in inventory XML")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        raise ArchiveInventoryError("official inventory response is not valid XML") from error

    prefix = _child_text(root, "Prefix")
    if prefix != expected_prefix:
        raise ArchiveInventoryError(
            f"S3 listing prefix {prefix!r} does not match expected {expected_prefix!r}"
        )
    truncated_text = _child_text(root, "IsTruncated")
    if truncated_text not in {"true", "false"}:
        raise ArchiveInventoryError("S3 IsTruncated must be true or false")
    is_truncated = truncated_text == "true"

    objects = []
    for element in root.findall("{*}Contents"):
        key = _child_text(element, "Key")
        last_modified = _child_text(element, "LastModified")
        etag = _child_text(element, "ETag")
        size_text = _child_text(element, "Size")
        assert key is not None and last_modified is not None
        assert etag is not None and size_text is not None
        try:
            size = int(size_text)
        except ValueError as error:
            raise ArchiveInventoryError("S3 object Size must be an integer") from error
        objects.append(
            ArchiveObject(
                key=key,
                last_modified=_parse_utc(last_modified),
                etag=etag.strip('"'),
                size=size,
            )
        )

    common_prefixes = tuple(
        value
        for element in root.findall("{*}CommonPrefixes")
        if (value := _child_text(element, "Prefix")) is not None
    )
    next_marker = _child_text(root, "NextMarker", required=False)
    if is_truncated and next_marker is None:
        candidates = [item.key for item in objects] + list(common_prefixes)
        if not candidates:
            raise ArchiveInventoryError("truncated S3 page has no continuation marker")
        next_marker = max(candidates)

    return _build_inventory(
        prefix=expected_prefix,
        fetched_at=fetched_at or datetime.now(UTC),
        objects=tuple(objects),
        common_prefixes=common_prefixes,
        is_truncated=is_truncated,
        next_marker=next_marker,
    )


@contextmanager
def _managed_client(client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    with httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": "slagalpha/0.1.0"},
    ) as owned:
        yield owned


def fetch_archive_inventory(
    prefix: str,
    *,
    delimiter: str | None = None,
    client: httpx.Client | None = None,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    fetched_at: datetime | None = None,
) -> ArchiveInventory:
    """Fetch every page for an official S3 prefix and return one immutable snapshot."""

    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    observed_at = fetched_at or datetime.now(UTC)
    marker: str | None = None
    seen_markers: set[str] = set()
    objects_by_key: dict[str, ArchiveObject] = {}
    common_prefixes: set[str] = set()

    with _managed_client(client) as active_client:
        for _ in range(1000):
            params = {"prefix": prefix, "max-keys": "1000"}
            if delimiter is not None:
                params["delimiter"] = delimiter
            if marker is not None:
                params["marker"] = marker

            last_error: Exception | None = None
            response: httpx.Response | None = None
            for attempt in range(1, attempts + 1):
                try:
                    response = active_client.get(PUBLIC_DATA_S3_URL, params=params)
                    response.raise_for_status()
                    break
                except (httpx.TransportError, httpx.HTTPStatusError) as error:
                    last_error = error
                    retryable = isinstance(error, httpx.TransportError) or (
                        error.response.status_code == 429
                        or error.response.status_code >= 500
                    )
                    if not retryable or attempt == attempts:
                        raise ArchiveInventoryError(
                            f"failed to fetch official inventory prefix {prefix}: {error}"
                        ) from error
                    sleep(0.25 * (2 ** (attempt - 1)))
            if response is None:
                raise ArchiveInventoryError(
                    f"failed to fetch official inventory prefix {prefix}: {last_error}"
                )

            page = parse_s3_inventory_page(
                response.text,
                expected_prefix=prefix,
                fetched_at=observed_at,
            )
            for item in page.objects:
                existing = objects_by_key.get(item.key)
                if existing is not None and existing != item:
                    raise ArchiveInventoryError(
                        f"inventory object changed during pagination: {item.key}"
                    )
                objects_by_key[item.key] = item
            common_prefixes.update(page.common_prefixes)
            if not page.is_truncated:
                return _build_inventory(
                    prefix=prefix,
                    fetched_at=observed_at,
                    objects=tuple(objects_by_key.values()),
                    common_prefixes=tuple(common_prefixes),
                )
            assert page.next_marker is not None
            if page.next_marker in seen_markers:
                raise ArchiveInventoryError("S3 pagination marker repeated")
            seen_markers.add(page.next_marker)
            marker = page.next_marker

    raise ArchiveInventoryError("S3 inventory exceeded 1000 pages")


def _month_sequence(start: date, end_exclusive: date) -> tuple[date, ...]:
    if start.day != 1 or end_exclusive.day != 1:
        raise ArchiveInventoryError("month range boundaries must use the first day")
    if end_exclusive <= start:
        raise ArchiveInventoryError("end_exclusive must be later than start")
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


def plan_monthly_archives(
    inventory: ArchiveInventory,
    *,
    symbol: str,
    interval: Literal["1m", "15m", "1h", "4h", "1d"],
    start: date,
    end_exclusive: date,
) -> MonthlyArchivePlan:
    """Plan available and missing official ZIPs without downloading any object."""

    normalized_symbol = ArchiveSpec(
        symbol=symbol,
        interval=interval,
        year=start.year,
        month=start.month,
    ).symbol
    expected_prefix = f"{MONTHLY_KLINE_PREFIX}{normalized_symbol}/{interval}/"
    if inventory.prefix != expected_prefix:
        raise ArchiveInventoryError(
            f"inventory prefix {inventory.prefix!r} does not match plan {expected_prefix!r}"
        )

    keys = {item.key for item in inventory.objects}
    available = []
    missing = []
    for month in _month_sequence(start, end_exclusive):
        spec = ArchiveSpec(
            symbol=normalized_symbol,
            interval=interval,
            year=month.year,
            month=month.month,
        )
        expected_key = f"{expected_prefix}{spec.filename}"
        expected_checksum = f"{expected_key}.CHECKSUM"
        if expected_key in keys and expected_checksum in keys:
            available.append(spec)
        else:
            missing.append(spec.period)

    available_tuple = tuple(available)
    missing_tuple = tuple(missing)
    start_period = start.strftime("%Y-%m")
    end_period = end_exclusive.strftime("%Y-%m")
    plan_hash = _plan_hash(
        symbol=normalized_symbol,
        interval=interval,
        start_period=start_period,
        end_period_exclusive=end_period,
        inventory_hash=inventory.listing_hash,
        available=available_tuple,
        missing_periods=missing_tuple,
    )
    return MonthlyArchivePlan(
        symbol=normalized_symbol,
        interval=interval,
        start_period=start_period,
        end_period_exclusive=end_period,
        inventory_hash=inventory.listing_hash,
        available=available_tuple,
        missing_periods=missing_tuple,
        plan_hash=plan_hash,
    )


def _write_manifest(payload: BaseModel, destination: Path) -> Path:
    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    text = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    try:
        temporary.write_text(f"{text}\n", encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def write_archive_inventory_manifest(
    inventory: ArchiveInventory,
    manifests_dir: Path,
) -> Path:
    """Write one content-addressed official inventory receipt."""

    destination = (
        manifests_dir / "archive_inventory" / f"{inventory.listing_hash}.json"
    )
    return _write_manifest(inventory, destination)


def write_archive_plan_manifest(plan: MonthlyArchivePlan, manifests_dir: Path) -> Path:
    """Write one content-addressed no-download archive plan."""

    destination = manifests_dir / "archive_plan" / f"{plan.plan_hash}.json"
    return _write_manifest(plan, destination)
