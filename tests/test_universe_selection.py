"""Golden tests for deterministic daily historical Universe selection."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from slagalpha.data.universe import (
    REQUIRED_INTERVALS,
    UniverseSelectionError,
    build_daily_universe,
    write_universe_snapshot,
)
from slagalpha.domain.universe import (
    ContractIdentityLifecycleEntry,
    ContractIdentityRegistry,
    ContractRegistry,
    ContractRegistryEntry,
    EvidenceConfidence,
    ExclusionCategory,
    ExclusionLedger,
    ExclusionLedgerEntry,
    RegistryVerification,
    UniverseBlockReason,
    UniverseSnapshot,
)

SELECTED_AT = datetime(2024, 1, 2, 0, 5, tzinfo=UTC)
WINDOW_END = datetime(2024, 1, 2, tzinfo=UTC)


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def registry_entry(
    symbol: str,
    *,
    onboard_date: datetime | None = None,
    effective_to: datetime | None = None,
    status: str = "TRADING",
    verification: RegistryVerification = RegistryVerification.VERIFIED,
) -> ContractRegistryEntry:
    onboard = onboard_date or SELECTED_AT - timedelta(days=365)
    return ContractRegistryEntry(
        symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        margin_asset="USDT",
        contract_type="PERPETUAL",
        status=status,
        onboard_date=onboard,
        derived_first_candle_at=onboard,
        inferred_delisted_at=effective_to,
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("1000000"),
        min_notional=Decimal("5"),
        effective_from=onboard,
        effective_to=effective_to,
        source_ref=f"registry:{symbol}",
        source_snapshot_hash=hash_text(symbol),
        derivation_method="OFFICIAL_METADATA",
        reviewed_by="USER",
        verification_status=verification,
        confidence=EvidenceConfidence.HIGH,
    )


def registry(*entries: ContractRegistryEntry) -> ContractRegistry:
    ordered = tuple(sorted(entries, key=lambda entry: (entry.symbol, entry.effective_from)))
    return ContractRegistry(registry_version="registry-v1", entries=ordered)


def identity_registry(symbol: str) -> ContractIdentityRegistry:
    onboard = SELECTED_AT - timedelta(days=365)
    entry = ContractIdentityLifecycleEntry(
        symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        margin_asset="USDT",
        contract_type="PERPETUAL",
        onboard_date=onboard,
        derived_first_candle_at=onboard,
        effective_from=onboard,
        effective_to=None,
        source_ref=f"identity:{symbol}",
        source_snapshot_hash=hash_text(symbol),
        reviewed_by="USER",
        verification_status=RegistryVerification.VERIFIED,
        confidence=EvidenceConfidence.HIGH,
    )
    return ContractIdentityRegistry(
        registry_version="identity-registry-v1",
        entries=(entry,),
    )


def ledger(*entries: ExclusionLedgerEntry) -> ExclusionLedger:
    ordered = tuple(
        sorted(
            entries,
            key=lambda entry: (
                entry.symbol,
                entry.effective_from,
                entry.category.value,
                entry.evidence_ref,
            ),
        )
    )
    return ExclusionLedger(ledger_version="ledger-v1", entries=ordered)


def candles(symbol: str, quote_volume: str) -> pd.DataFrame:
    opens = pd.date_range(
        WINDOW_END - timedelta(days=1),
        periods=96,
        freq="15min",
        tz="UTC",
    )
    return pd.DataFrame(
        {
            "symbol": symbol,
            "interval": "15m",
            "open_time": opens,
            "close_time_exclusive": opens + pd.Timedelta(minutes=15),
            "quote_volume": [Decimal(quote_volume)] * 96,
            "is_closed": True,
            "source_file_hash": hash_text(f"candles:{symbol}"),
        }
    )


def interval_evidence(*symbols: str) -> dict[str, dict[str, str]]:
    return {
        symbol: {
            interval: f"manifest:{symbol}:{interval}" for interval in REQUIRED_INTERVALS
        }
        for symbol in symbols
    }


def select(
    entries: tuple[ContractRegistryEntry, ...],
    frames: dict[str, pd.DataFrame],
    *,
    exclusions: tuple[ExclusionLedgerEntry, ...] = (),
    evidence: dict[str, dict[str, str]] | None = None,
) -> UniverseSnapshot:
    symbols = tuple(entry.symbol for entry in entries)
    return build_daily_universe(
        selected_at=SELECTED_AT,
        registry=registry(*entries),
        exclusion_ledger=ledger(*exclusions),
        fifteen_minute_candles=frames,
        interval_evidence=evidence or interval_evidence(*symbols),
    )


def test_gs047_future_quote_volume_cannot_change_daily_snapshot() -> None:
    entries = (registry_entry("AAAUSDT"), registry_entry("BBBUSDT"))
    base_frames = {
        "AAAUSDT": candles("AAAUSDT", "100"),
        "BBBUSDT": candles("BBBUSDT", "90"),
    }
    future = pd.DataFrame(
        {
            "symbol": ["BBBUSDT"],
            "interval": ["15m"],
            "open_time": [WINDOW_END + timedelta(hours=12)],
            "close_time_exclusive": [WINDOW_END + timedelta(hours=12, minutes=15)],
            "quote_volume": [Decimal("999999999")],
            "is_closed": [True],
            "source_file_hash": [hash_text("future")],
        }
    )
    with_future = {**base_frames, "BBBUSDT": pd.concat([base_frames["BBBUSDT"], future])}

    before = select(entries, base_frames)
    after = select(entries, with_future)

    assert before == after
    assert tuple(member.symbol for member in before.members) == ("AAAUSDT", "BBBUSDT")
    assert before.members[1].rolling_quote_volume_24h == Decimal("8640")


def test_gs048_btc_is_forced_without_growing_past_thirty() -> None:
    symbols = ("BTCUSDT", "ETHUSDT", *(f"X{index:02d}USDT" for index in range(33)))
    entries = tuple(registry_entry(symbol) for symbol in symbols)
    per_candle_volume = {
        "BTCUSDT": "1",
        "ETHUSDT": "999",
        **{f"X{index:02d}USDT": str(1000 - index) for index in range(33)},
    }
    frames = {
        symbol: candles(symbol, per_candle_volume[symbol]) for symbol in symbols
    }

    snapshot = select(entries, frames)
    members = {member.symbol: member for member in snapshot.members}

    assert snapshot.member_count == 30
    assert members["BTCUSDT"].forced is True
    assert members["ETHUSDT"].forced is False
    assert "X28USDT" not in members
    assert tuple(member.rank for member in snapshot.members) == tuple(range(1, 31))


def test_gs049_exact_ninety_day_boundary_passes_and_one_second_short_fails() -> None:
    exact = registry_entry(
        "EXACTUSDT",
        onboard_date=SELECTED_AT - timedelta(days=90),
    )
    young = registry_entry(
        "YOUNGUSDT",
        onboard_date=SELECTED_AT - timedelta(days=90) + timedelta(seconds=1),
    )

    snapshot = select(
        (exact, young),
        {
            "EXACTUSDT": candles("EXACTUSDT", "100"),
            "YOUNGUSDT": candles("YOUNGUSDT", "200"),
        },
    )

    assert tuple(member.symbol for member in snapshot.members) == ("EXACTUSDT",)
    assert snapshot.blocked_candidates[0].symbol == "YOUNGUSDT"
    assert snapshot.blocked_candidates[0].reason_codes == (
        UniverseBlockReason.SYMBOL_INELIGIBLE,
    )


def test_bad_symbol_day_fails_closed_without_stopping_other_symbols() -> None:
    entries = (registry_entry("AAAUSDT"), registry_entry("BBBUSDT"))
    missing = candles("BBBUSDT", "200").drop(index=20).reset_index(drop=True)

    snapshot = select(
        entries,
        {"AAAUSDT": candles("AAAUSDT", "100"), "BBBUSDT": missing},
    )

    assert tuple(member.symbol for member in snapshot.members) == ("AAAUSDT",)
    assert snapshot.blocked_candidates[0].symbol == "BBBUSDT"
    assert snapshot.blocked_candidates[0].reason_codes == (
        UniverseBlockReason.CANDLE_GAP,
    )


def test_active_exclusion_and_missing_interval_evidence_are_auditable() -> None:
    entries = (registry_entry("AAAUSDT"), registry_entry("BBBUSDT"))
    exclusion = ExclusionLedgerEntry(
        symbol="AAAUSDT",
        category=ExclusionCategory.DATA_QUALITY,
        effective_from=SELECTED_AT - timedelta(days=1),
        effective_to=None,
        reason="known anomaly",
        evidence_ref="anomaly:1",
        reviewed_by="USER",
        ledger_version="ledger-v1",
    )
    evidence = interval_evidence("AAAUSDT", "BBBUSDT")
    del evidence["BBBUSDT"]["4h"]

    snapshot = select(
        entries,
        {
            "AAAUSDT": candles("AAAUSDT", "100"),
            "BBBUSDT": candles("BBBUSDT", "200"),
        },
        exclusions=(exclusion,),
        evidence=evidence,
    )

    blocked = {candidate.symbol: candidate for candidate in snapshot.blocked_candidates}
    assert snapshot.members == ()
    assert blocked["AAAUSDT"].reason_codes == (UniverseBlockReason.EXCLUDED,)
    assert blocked["BBBUSDT"].reason_codes == (
        UniverseBlockReason.DATA_INCOMPLETE,
    )


def test_equal_volume_uses_symbol_tie_break_and_hash_is_repeatable() -> None:
    entries = (registry_entry("BBBUSDT"), registry_entry("AAAUSDT"))
    frames = {
        "AAAUSDT": candles("AAAUSDT", "100"),
        "BBBUSDT": candles("BBBUSDT", "100"),
    }

    first = select(entries, frames)
    repeated = select(entries, frames)

    assert tuple(member.symbol for member in first.members) == ("AAAUSDT", "BBBUSDT")
    assert first.universe_version == repeated.universe_version
    assert first.candle_dataset_hash == repeated.candle_dataset_hash


def test_unverified_or_inactive_registry_entry_never_becomes_eligible() -> None:
    unverified = registry_entry(
        "UNKNOWNUSDT",
        verification=RegistryVerification.UNVERIFIED,
    )
    inactive = registry_entry(
        "OLDUSDT",
        effective_to=SELECTED_AT - timedelta(seconds=1),
    )

    snapshot = select(
        (unverified, inactive),
        {
            "UNKNOWNUSDT": candles("UNKNOWNUSDT", "1000"),
            "OLDUSDT": candles("OLDUSDT", "900"),
        },
    )

    blocked = {candidate.symbol: candidate for candidate in snapshot.blocked_candidates}
    assert snapshot.members == ()
    assert blocked["UNKNOWNUSDT"].reason_codes == (
        UniverseBlockReason.CONTRACT_UNVERIFIED,
    )
    assert blocked["OLDUSDT"].reason_codes == (
        UniverseBlockReason.SYMBOL_INELIGIBLE,
    )


def test_verified_identity_registry_drives_universe_without_contract_rules() -> None:
    symbol = "AAAUSDT"

    snapshot = build_daily_universe(
        selected_at=SELECTED_AT,
        registry=identity_registry(symbol),
        exclusion_ledger=ledger(),
        fifteen_minute_candles={symbol: candles(symbol, "100")},
        interval_evidence=interval_evidence(symbol),
    )

    assert tuple(member.symbol for member in snapshot.members) == (symbol,)
    assert snapshot.contract_registry_version == "identity-registry-v1"


def test_universe_snapshot_writer_is_idempotent_and_fail_closed(tmp_path: Path) -> None:
    data_dir = tmp_path
    snapshot = build_daily_universe(
        selected_at=SELECTED_AT,
        registry=identity_registry("AAAUSDT"),
        exclusion_ledger=ledger(),
        fifteen_minute_candles={"AAAUSDT": candles("AAAUSDT", "100")},
        interval_evidence=interval_evidence("AAAUSDT"),
    )

    destination = write_universe_snapshot(snapshot, data_dir)
    assert write_universe_snapshot(snapshot, data_dir) == destination
    assert UniverseSnapshot.model_validate_json(destination.read_text()) == snapshot

    destination.write_text("{}\n", encoding="utf-8")
    try:
        write_universe_snapshot(snapshot, data_dir)
    except UniverseSelectionError as error:
        assert "content-addressed snapshot changed" in str(error)
    else:
        raise AssertionError("mutated snapshot must fail closed")
