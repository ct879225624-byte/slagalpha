"""Tests for metadata and monthly-object coverage-loss accounting."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from slagalpha.data.coverage_loss import (
    build_coverage_loss_report,
    write_coverage_loss_artifacts,
)
from slagalpha.data.exchange_info import ExchangeContract


def contract(symbol: str, *, eligible: bool = True) -> ExchangeContract:
    return ExchangeContract(
        symbol=symbol,
        pair=symbol,
        contract_type="PERPETUAL" if eligible else "TRADIFI_PERPETUAL",
        status="TRADING",
        onboard_date=None,
        delivery_date=None,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        margin_asset="USDT",
        tick_size=Decimal("0.1"),
        step_size=Decimal("1"),
        min_qty=Decimal("1"),
        max_qty=Decimal("100"),
        min_notional=Decimal("5"),
    )


def rows() -> pd.DataFrame:
    values = []
    for symbol in ("AAAUSDT", "UNKNOWNUSDT", "INDEXUSDT"):
        intervals: tuple[str, ...] = ("15m", "1h", "4h", "1d")
        if symbol == "INDEXUSDT":
            intervals = intervals[:-1]
        for interval in intervals:
            values.append({"symbol": symbol, "period": "2024-02", "interval": interval})
    return pd.DataFrame(values)


def test_coverage_loss_partitions_symbol_months_without_guessing() -> None:
    report, details = build_coverage_loss_report(
        rows(),
        contracts=(contract("AAAUSDT"), contract("INDEXUSDT", eligible=False)),
        capacity_plan_hash="a" * 64,
        exchange_info_hash="b" * 64,
        root_symbol_count=3,
        research_start=date(2024, 2, 1),
        research_end_exclusive=date(2024, 3, 1),
    )

    assert report.observation_symbol_month_count == 3
    assert report.identity_metadata_available_symbol_month_count == 1
    assert report.metadata_missing_symbol_month_count == 1
    assert report.metadata_ineligible_symbol_month_count == 1
    assert report.partial_interval_symbol_month_count == 1
    assert report.locked_research_ready is False
    unknown = details.loc[details["symbol"] == "UNKNOWNUSDT"].iloc[0]
    assert unknown["classification"] == "METADATA_MISSING"


def test_coverage_loss_artifacts_are_content_addressed(tmp_path: Path) -> None:
    report, details = build_coverage_loss_report(
        rows(),
        contracts=(contract("AAAUSDT"), contract("INDEXUSDT", eligible=False)),
        capacity_plan_hash="a" * 64,
        exchange_info_hash="b" * 64,
        root_symbol_count=3,
        research_start=date(2024, 2, 1),
        research_end_exclusive=date(2024, 3, 1),
    )
    artifacts = write_coverage_loss_artifacts(report, details, tmp_path)

    assert write_coverage_loss_artifacts(report, details, tmp_path) == artifacts
    assert report.coverage_hash in artifacts[0].name
    assert report.coverage_hash in artifacts[1].name
