"""Tests for fail-closed registry draft construction."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from slagalpha.data.exchange_info import ExchangeContract
from slagalpha.data.klines import ArchiveNormalizationManifest
from slagalpha.data.registry_draft import build_identity_registry, build_registry_draft
from slagalpha.domain.universe import RegistryVerification


def contract(symbol: str, *, status: str = "TRADING") -> ExchangeContract:
    return ExchangeContract(
        symbol=symbol,
        pair=symbol,
        contract_type="PERPETUAL",
        status=status,
        onboard_date=datetime(2024, 1, 1, 0, 7, tzinfo=UTC),
        delivery_date=(
            datetime(2025, 1, 1, tzinfo=UTC) if status == "SETTLING" else None
        ),
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        margin_asset="USDT",
        tick_size=Decimal("0.1"),
        step_size=Decimal("1"),
        min_qty=Decimal("1"),
        max_qty=Decimal("100"),
        min_notional=Decimal("5"),
    )


def evidence(symbol: str) -> ArchiveNormalizationManifest:
    return ArchiveNormalizationManifest(
        source_file_hash="a" * 64,
        symbol=symbol,
        interval="15m",
        period="2024-01",
        parsed_row_count=100,
        normalized_row_count=100,
        identical_duplicates_removed=0,
        first_open_time=datetime(2024, 1, 1, tzinfo=UTC),
        last_open_time=datetime(2024, 1, 2, tzinfo=UTC),
        normalized_content_hash="b" * 64,
        parquet_sha256="c" * 64,
        output_relative_path="klines/example.parquet",
        normalized_at=datetime(2024, 2, 1, tzinfo=UTC),
    )


def test_registry_draft_never_marks_current_rules_historically_verified() -> None:
    registry, report = build_registry_draft(
        contracts=(contract("AAAUSDT"),),
        fifteen_minute_evidence={"AAAUSDT": (evidence("AAAUSDT"),)},
        exchange_info_hash="d" * 64,
    )

    assert len(registry.entries) == 1
    entry = registry.entries[0]
    assert entry.verification_status is RegistryVerification.UNVERIFIED
    assert entry.eligible_for_locked_research is False
    assert entry.derived_first_candle_at == datetime(2024, 1, 1, 0, 15, tzinfo=UTC)
    assert report.locked_entry_count == 0


def test_settling_contract_uses_delivery_as_draft_effective_end() -> None:
    registry, _ = build_registry_draft(
        contracts=(contract("OLDUSDT", status="SETTLING"),),
        fifteen_minute_evidence={"OLDUSDT": (evidence("OLDUSDT"),)},
        exchange_info_hash="d" * 64,
    )

    assert registry.entries[0].status == "TRADING"
    assert registry.entries[0].effective_to == datetime(2025, 1, 1, tzinfo=UTC)


def test_identity_registry_is_verified_but_contains_no_filter_rules() -> None:
    registry, report = build_identity_registry(
        contracts=(contract("AAAUSDT"),),
        fifteen_minute_evidence={"AAAUSDT": (evidence("AAAUSDT"),)},
        exchange_info_hash="d" * 64,
    )

    entry = registry.entries[0]
    assert entry.eligible_for_locked_research is True
    assert not hasattr(entry, "tick_size")
    assert report.verified_entry_count == 1
