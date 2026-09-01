"""Tests for current Binance exchangeInfo evidence and coverage auditing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from slagalpha.data.exchange_info import (
    ExchangeInfoRateLimitedError,
    audit_registry_coverage,
    fetch_exchange_info,
    parse_exchange_info,
    write_exchange_info_snapshot,
    write_registry_coverage_report,
)


def exchange_payload() -> bytes:
    return json.dumps(
        {
            "timezone": "UTC",
            "serverTime": 1_706_745_600_000,
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "pair": "BTCUSDT",
                    "contractType": "PERPETUAL",
                    "deliveryDate": 4_133_404_800_000,
                    "onboardDate": 1_568_073_600_000,
                    "status": "TRADING",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "pricePrecision": 1,
                    "quantityPrecision": 0,
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "minPrice": "0.10",
                            "maxPrice": "1000000",
                            "tickSize": "0.10",
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.001",
                            "maxQty": "1000",
                            "stepSize": "0.001",
                        },
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
                {
                    "symbol": "币安人生USDT",
                    "pair": "币安人生USDT",
                    "contractType": "PERPETUAL",
                    "deliveryDate": 4_133_404_800_000,
                    "onboardDate": 1_700_000_000_000,
                    "status": "TRADING",
                    "baseAsset": "币安人生",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "1",
                            "maxQty": "1000000",
                            "stepSize": "1",
                        },
                    ],
                },
                {
                    "symbol": "ETHUSDC",
                    "pair": "ETHUSDC",
                    "contractType": "PERPETUAL",
                    "deliveryDate": 4_133_404_800_000,
                    "onboardDate": 1_700_000_000_000,
                    "status": "TRADING",
                    "baseAsset": "ETH",
                    "quoteAsset": "USDC",
                    "marginAsset": "USDC",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": "0.001",
                            "maxQty": "1000",
                            "stepSize": "0.001",
                        },
                    ],
                },
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def test_exchange_info_uses_filters_not_precision_fields() -> None:
    snapshot = parse_exchange_info(
        exchange_payload(),
        fetched_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    btc = snapshot.contracts[0]

    assert btc.symbol == "BTCUSDT"
    assert btc.tick_size == Decimal("0.10")
    assert btc.step_size == Decimal("0.001")
    assert btc.min_notional == Decimal("5")
    assert snapshot.current_usdt_perpetual_symbols == ("BTCUSDT", "币安人生USDT")
    assert len(snapshot.raw_sha256) == 64


def test_fetch_fails_immediately_on_json_rate_ban_even_with_http_200() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "code": -1003,
                "msg": "Way too many requests; IP banned until 1706749200000.",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExchangeInfoRateLimitedError) as error:
            fetch_exchange_info(client=client, attempts=3)

    assert calls == 1
    assert error.value.banned_until == datetime(2024, 2, 1, 1, tzinfo=UTC)


def test_registry_coverage_separates_current_archive_only_and_metadata_only() -> None:
    snapshot = parse_exchange_info(
        exchange_payload(),
        fetched_at=datetime(2024, 2, 1, tzinfo=UTC),
    )

    report = audit_registry_coverage(
        archive_symbols=("ANTUSDT", "BTCUSDT", "币安人生USDT"),
        exchange_info=snapshot,
    )

    assert report.current_with_archive == ("BTCUSDT", "币安人生USDT")
    assert report.archive_only == ("ANTUSDT",)
    assert report.current_without_archive == ()
    assert report.raw_archive_symbol_count == 3
    assert report.current_usdt_perpetual_count == 2


def test_current_metadata_without_archive_is_reported_not_silently_removed() -> None:
    snapshot = parse_exchange_info(
        exchange_payload(),
        fetched_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    report = audit_registry_coverage(
        archive_symbols=("ANTUSDT",),
        exchange_info=snapshot,
    )

    assert report.current_with_archive == ()
    assert report.current_without_archive == ("BTCUSDT", "币安人生USDT")
    assert report.archive_only == ("ANTUSDT",)


def test_exchange_snapshot_and_coverage_manifests_are_idempotent(tmp_path: Path) -> None:
    snapshot = parse_exchange_info(
        exchange_payload(),
        fetched_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    report = audit_registry_coverage(
        archive_symbols=("ANTUSDT", "BTCUSDT"),
        exchange_info=snapshot,
    )

    raw_path, snapshot_path = write_exchange_info_snapshot(snapshot, tmp_path)
    report_path = write_registry_coverage_report(report, tmp_path)

    assert write_exchange_info_snapshot(snapshot, tmp_path) == (raw_path, snapshot_path)
    assert write_registry_coverage_report(report, tmp_path) == report_path
    assert raw_path.read_bytes() == exchange_payload()
    assert snapshot.raw_sha256 in snapshot_path.name
    assert report.coverage_hash in report_path.name
