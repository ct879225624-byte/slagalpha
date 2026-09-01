"""Tests for official Public Data inventory snapshots and monthly planning."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from slagalpha.data.inventory import (
    ArchiveInventoryError,
    fetch_archive_inventory,
    parse_s3_inventory_page,
    plan_monthly_archives,
    write_archive_inventory_manifest,
    write_archive_plan_manifest,
)


def page_xml(
    *,
    prefix: str,
    keys: tuple[tuple[str, int], ...] = (),
    prefixes: tuple[str, ...] = (),
    truncated: bool = False,
    next_marker: str | None = None,
) -> str:
    objects = "".join(
        "<Contents>"
        f"<Key>{key}</Key>"
        "<LastModified>2024-04-08T10:00:00.000Z</LastModified>"
        f"<ETag>&quot;etag-{size}&quot;</ETag><Size>{size}</Size>"
        "</Contents>"
        for key, size in keys
    )
    common = "".join(
        f"<CommonPrefixes><Prefix>{value}</Prefix></CommonPrefixes>"
        for value in prefixes
    )
    marker = f"<NextMarker>{next_marker}</NextMarker>" if next_marker else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<Name>data.binance.vision</Name><Prefix>{prefix}</Prefix>"
        f"<IsTruncated>{str(truncated).lower()}</IsTruncated>{marker}{objects}{common}"
        "</ListBucketResult>"
    )


def test_parse_inventory_is_canonical_and_content_addressed() -> None:
    prefix = "data/futures/um/monthly/klines/ANTUSDT/15m/"
    xml = page_xml(
        prefix=prefix,
        keys=(
            (f"{prefix}ANTUSDT-15m-2024-03.zip.CHECKSUM", 100),
            (f"{prefix}ANTUSDT-15m-2024-03.zip", 200),
        ),
    )

    page = parse_s3_inventory_page(xml, expected_prefix=prefix)
    repeated = parse_s3_inventory_page(xml, expected_prefix=prefix)

    assert tuple(item.key for item in page.objects) == (
        f"{prefix}ANTUSDT-15m-2024-03.zip",
        f"{prefix}ANTUSDT-15m-2024-03.zip.CHECKSUM",
    )
    assert page.listing_hash == repeated.listing_hash
    assert page.objects[0].size == 200


def test_parser_rejects_wrong_prefix_and_unsafe_xml() -> None:
    xml = page_xml(prefix="wrong/")
    with pytest.raises(ArchiveInventoryError, match="prefix"):
        parse_s3_inventory_page(xml, expected_prefix="expected/")
    with pytest.raises(ArchiveInventoryError, match="DOCTYPE"):
        parse_s3_inventory_page(
            "<!DOCTYPE foo><ListBucketResult />",
            expected_prefix="expected/",
        )


def test_fetch_inventory_follows_marker_pagination_once() -> None:
    prefix = "data/futures/um/monthly/klines/"
    requests: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        marker = request.url.params.get("marker")
        requests.append(marker)
        if marker is None:
            content = page_xml(
                prefix=prefix,
                prefixes=(f"{prefix}ANTUSDT/",),
                truncated=True,
                next_marker=f"{prefix}ANTUSDT/",
            )
        else:
            content = page_xml(
                prefix=prefix,
                prefixes=(f"{prefix}BTCUSDT/",),
            )
        return httpx.Response(200, text=content)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        inventory = fetch_archive_inventory(
            prefix,
            delimiter="/",
            client=client,
            fetched_at=datetime(2024, 4, 10, tzinfo=UTC),
        )

    assert requests == [None, f"{prefix}ANTUSDT/"]
    assert inventory.common_prefixes == (f"{prefix}ANTUSDT/", f"{prefix}BTCUSDT/")
    assert inventory.archived_symbols == ("ANTUSDT", "BTCUSDT")


def test_monthly_plan_reports_available_and_missing_without_downloading() -> None:
    prefix = "data/futures/um/monthly/klines/ANTUSDT/15m/"
    inventory = parse_s3_inventory_page(
        page_xml(
            prefix=prefix,
            keys=(
                (f"{prefix}ANTUSDT-15m-2024-02.zip", 200),
                (f"{prefix}ANTUSDT-15m-2024-02.zip.CHECKSUM", 100),
                (f"{prefix}ANTUSDT-15m-2024-03.zip", 300),
                (f"{prefix}ANTUSDT-15m-2024-03.zip.CHECKSUM", 100),
            ),
        ),
        expected_prefix=prefix,
    )

    plan = plan_monthly_archives(
        inventory,
        symbol="ANTUSDT",
        interval="15m",
        start=date(2024, 2, 1),
        end_exclusive=date(2024, 5, 1),
    )

    assert tuple(spec.period for spec in plan.available) == ("2024-02", "2024-03")
    assert plan.missing_periods == ("2024-04",)
    assert len(plan.plan_hash) == 64


def test_monthly_plan_rejects_non_month_boundaries() -> None:
    prefix = "data/futures/um/monthly/klines/ANTUSDT/15m/"
    inventory = parse_s3_inventory_page(
        page_xml(prefix=prefix),
        expected_prefix=prefix,
    )
    with pytest.raises(ArchiveInventoryError, match="first day"):
        plan_monthly_archives(
            inventory,
            symbol="ANTUSDT",
            interval="15m",
            start=date(2024, 2, 2),
            end_exclusive=date(2024, 5, 1),
        )


def test_inventory_and_plan_manifests_are_idempotent(tmp_path: Path) -> None:
    prefix = "data/futures/um/monthly/klines/ANTUSDT/15m/"
    inventory = parse_s3_inventory_page(
        page_xml(
            prefix=prefix,
            keys=((f"{prefix}ANTUSDT-15m-2024-03.zip", 300),),
        ),
        expected_prefix=prefix,
        fetched_at=datetime(2024, 4, 10, tzinfo=UTC),
    )
    plan = plan_monthly_archives(
        inventory,
        symbol="ANTUSDT",
        interval="15m",
        start=date(2024, 3, 1),
        end_exclusive=date(2024, 4, 1),
    )

    inventory_path = write_archive_inventory_manifest(inventory, tmp_path)
    plan_path = write_archive_plan_manifest(plan, tmp_path)

    assert write_archive_inventory_manifest(inventory, tmp_path) == inventory_path
    assert write_archive_plan_manifest(plan, tmp_path) == plan_path
    assert inventory.listing_hash in inventory_path.name
    assert plan.plan_hash in plan_path.name
