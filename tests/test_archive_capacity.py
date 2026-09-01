"""Tests for exact multi-symbol archive capacity planning."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from slagalpha.data.archive import ArchiveSpec
from slagalpha.data.capacity import (
    estimate_archive_capacity,
    write_archive_capacity_artifacts,
)
from slagalpha.data.inventory import ArchiveInventory, ArchiveObject


def inventory(prefix: str, objects: tuple[ArchiveObject, ...]) -> ArchiveInventory:
    ordered = tuple(sorted(objects, key=lambda item: item.key))
    from slagalpha.data.inventory import _listing_hash

    return ArchiveInventory(
        prefix=prefix,
        fetched_at=datetime(2024, 5, 1, tzinfo=UTC),
        objects=ordered,
        common_prefixes=(),
        listing_hash=_listing_hash(prefix, ordered, ()),
    )


def archive_objects(
    symbol: str,
    interval: str,
    year: int,
    month: int,
    size: int,
    *,
    checksum: bool = True,
) -> tuple[ArchiveObject, ...]:
    spec = ArchiveSpec(
        symbol=symbol,
        interval=cast(Literal["1m", "15m", "1h", "4h", "1d"], interval),
        year=year,
        month=month,
    )
    prefix = f"data/futures/um/monthly/klines/{symbol}/{interval}/"
    base = ArchiveObject(
        key=f"{prefix}{spec.filename}",
        last_modified=datetime(2024, 5, 1, tzinfo=UTC),
        etag=f"etag-{symbol}-{interval}-{year}-{month}",
        size=size,
    )
    if not checksum:
        return (base,)
    return (
        base,
        ArchiveObject(
            key=f"{prefix}{spec.filename}.CHECKSUM",
            last_modified=datetime(2024, 5, 1, tzinfo=UTC),
            etag=f"checksum-{symbol}-{interval}-{year}-{month}",
            size=100,
        ),
    )


def test_capacity_uses_exact_zip_sizes_and_ignores_outside_months() -> None:
    def fetch(prefix: str) -> ArchiveInventory:
        symbol, interval = prefix.rstrip("/").split("/")[-2:]
        objects = (
            *archive_objects(symbol, interval, 2023, 1, 999),
            *archive_objects(symbol, interval, 2023, 2, 100),
            *archive_objects(symbol, interval, 2023, 3, 200),
        )
        return inventory(prefix, objects)

    estimate, rows = estimate_archive_capacity(
        symbols=("AAAUSDT",),
        intervals=("15m", "1h"),
        start=date(2023, 2, 1),
        end_exclusive=date(2023, 4, 1),
        root_listing_hash="a" * 64,
        exchange_info_hash="b" * 64,
        fetch_inventory=fetch,
        threshold_bytes=10_000,
    )

    assert estimate.complete is True
    assert estimate.zip_object_count == 4
    assert estimate.downloadable_zip_bytes == 600
    assert estimate.projected_total_bytes == 2400
    assert estimate.within_threshold is True
    assert set(rows["period"]) == {"2023-02", "2023-03"}


def test_missing_checksum_is_counted_and_excluded_from_download_bytes() -> None:
    def fetch(prefix: str) -> ArchiveInventory:
        symbol, interval = prefix.rstrip("/").split("/")[-2:]
        return inventory(
            prefix,
            archive_objects(symbol, interval, 2023, 2, 500, checksum=False),
        )

    estimate, rows = estimate_archive_capacity(
        symbols=("AAAUSDT",),
        intervals=("15m",),
        start=date(2023, 2, 1),
        end_exclusive=date(2023, 3, 1),
        root_listing_hash="a" * 64,
        exchange_info_hash="b" * 64,
        fetch_inventory=fetch,
    )

    assert estimate.zip_object_count == 1
    assert estimate.checksum_missing_count == 1
    assert estimate.downloadable_zip_bytes == 0
    assert bool(rows.iloc[0]["checksum_present"]) is False


def test_failed_prefix_makes_estimate_incomplete_and_blocks_threshold() -> None:
    def fetch(prefix: str) -> ArchiveInventory:
        if "/1h/" in prefix:
            raise RuntimeError("network failed")
        return inventory(prefix, ())

    estimate, _ = estimate_archive_capacity(
        symbols=("AAAUSDT",),
        intervals=("15m", "1h"),
        start=date(2023, 2, 1),
        end_exclusive=date(2023, 3, 1),
        root_listing_hash="a" * 64,
        exchange_info_hash="b" * 64,
        fetch_inventory=fetch,
    )

    assert estimate.complete is False
    assert estimate.within_threshold is False
    assert estimate.failed_prefixes == (
        "data/futures/um/monthly/klines/AAAUSDT/1h/",
    )


def test_projection_multiplier_and_artifacts_are_repeatable(tmp_path: Path) -> None:
    def fetch(prefix: str) -> ArchiveInventory:
        symbol, interval = prefix.rstrip("/").split("/")[-2:]
        return inventory(prefix, archive_objects(symbol, interval, 2023, 2, 1000))

    estimate, rows = estimate_archive_capacity(
        symbols=("AAAUSDT",),
        intervals=("15m",),
        start=date(2023, 2, 1),
        end_exclusive=date(2023, 3, 1),
        root_listing_hash="a" * 64,
        exchange_info_hash="b" * 64,
        fetch_inventory=fetch,
        projection_multiplier=Decimal("4"),
        threshold_bytes=3999,
    )
    parquet, manifest = write_archive_capacity_artifacts(estimate, rows, tmp_path)

    assert estimate.projected_total_bytes == 4000
    assert estimate.within_threshold is False
    assert write_archive_capacity_artifacts(estimate, rows, tmp_path) == (
        parquet,
        manifest,
    )
    assert estimate.plan_content_hash in parquet.name
    assert estimate.plan_content_hash in manifest.name
