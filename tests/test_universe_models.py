"""Contract tests for P8.2 historical registry and Universe models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from slagalpha.domain.universe import (
    ContractRegistry,
    ContractRegistryEntry,
    EvidenceConfidence,
    ExclusionCategory,
    ExclusionLedger,
    ExclusionLedgerEntry,
    RegistryVerification,
    UniverseBlockedCandidate,
    UniverseBlockReason,
    UniverseMember,
    UniverseSnapshot,
)

UTC_MIDNIGHT = datetime(2024, 1, 2, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def contract_entry(**updates: Any) -> ContractRegistryEntry:
    values: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "margin_asset": "USDT",
        "contract_type": "PERPETUAL",
        "status": "TRADING",
        "onboard_date": datetime(2019, 9, 8, tzinfo=UTC),
        "derived_first_candle_at": datetime(2019, 9, 8, tzinfo=UTC),
        "inferred_delisted_at": None,
        "tick_size": "0.10",
        "step_size": "0.001",
        "min_qty": "0.001",
        "max_qty": "1000",
        "min_notional": "5",
        "effective_from": datetime(2023, 1, 1, tzinfo=UTC),
        "effective_to": None,
        "source_ref": "exchange-info:2023-01-01",
        "source_snapshot_hash": HASH_A,
        "derivation_method": "OFFICIAL_EXCHANGE_INFO",
        "reviewed_by": "USER",
        "verification_status": RegistryVerification.VERIFIED,
        "confidence": EvidenceConfidence.HIGH,
    }
    values.update(updates)
    return ContractRegistryEntry(**values)


def universe_member(
    symbol: str = "BTCUSDT",
    rank: int = 1,
    volume: str = "1000000",
    *,
    forced: bool = False,
) -> UniverseMember:
    return UniverseMember(
        symbol=symbol,
        rank=rank,
        rolling_quote_volume_24h=Decimal(volume),
        forced=forced,
        eligibility_refs=("registry:v1", "candles:96"),
    )


def universe_snapshot(**updates: Any) -> UniverseSnapshot:
    values: dict[str, Any] = {
        "universe_version": HASH_A,
        "selection_algorithm_version": "daily-top30/0.1.0",
        "selected_at": UTC_MIDNIGHT + timedelta(minutes=5),
        "effective_from": UTC_MIDNIGHT + timedelta(minutes=15),
        "effective_to": UTC_MIDNIGHT + timedelta(days=1, minutes=15),
        "ranking_window_start": UTC_MIDNIGHT - timedelta(days=1),
        "ranking_window_end": UTC_MIDNIGHT,
        "member_count": 2,
        "members": (
            universe_member(),
            universe_member("ETHUSDT", 2, "900000", forced=True),
        ),
        "blocked_candidates": (),
        "contract_registry_version": "registry-v1",
        "exclusion_ledger_version": "ledger-v1",
        "candle_dataset_hash": HASH_B,
    }
    values.update(updates)
    return UniverseSnapshot(**values)


def test_contract_registry_is_canonical_immutable_and_repeatable() -> None:
    btc = contract_entry()
    eth = contract_entry(
        symbol="ethusdt",
        base_asset="eth",
        source_snapshot_hash=HASH_B,
    )
    registry = ContractRegistry(
        registry_version="registry-v1",
        entries=(btc, eth),
    )

    assert eth.symbol == "ETHUSDT"
    assert eth.base_asset == "ETH"
    assert registry.model_dump_json() == ContractRegistry.model_validate(
        registry.model_dump()
    ).model_dump_json()
    with pytest.raises(ValidationError, match="frozen"):
        registry.registry_version = "changed"

    with pytest.raises(ValidationError, match="canonical order"):
        ContractRegistry(registry_version="registry-v1", entries=(eth, btc))


def test_first_candle_does_not_silently_verify_onboard_date() -> None:
    entry = contract_entry(
        onboard_date=None,
        verification_status=RegistryVerification.UNVERIFIED,
        confidence=EvidenceConfidence.LOW,
        derivation_method="FIRST_VALID_1D_CANDLE_CANDIDATE",
    )

    assert entry.onboard_date is None
    assert entry.derived_first_candle_at is not None
    assert entry.verification_status is RegistryVerification.UNVERIFIED
    assert entry.eligible_for_locked_research is False


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"quote_asset": "USDC"}, "quote_asset"),
        ({"margin_asset": "BTC"}, "margin_asset"),
        ({"contract_type": "CURRENT_QUARTER"}, "contract_type"),
        ({"tick_size": "0"}, "tick_size"),
        ({"step_size": "NaN"}, "step_size"),
        ({"min_qty": "2", "max_qty": "1"}, "min_qty"),
    ],
)
def test_contract_registry_entry_rejects_ineligible_or_invalid_values(
    updates: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        contract_entry(**updates)


def test_contract_registry_rejects_overlapping_symbol_intervals() -> None:
    first = contract_entry(effective_to=datetime(2024, 1, 1, tzinfo=UTC))
    overlap = contract_entry(
        effective_from=datetime(2023, 12, 1, tzinfo=UTC),
        source_snapshot_hash=HASH_B,
    )

    with pytest.raises(ValidationError, match="overlap"):
        ContractRegistry(registry_version="registry-v1", entries=(first, overlap))


def test_exclusion_ledger_requires_exact_versioned_reviewed_entries() -> None:
    entry = ExclusionLedgerEntry(
        symbol="usdcusdt",
        category=ExclusionCategory.STABLE_SWAP,
        effective_from=datetime(2023, 1, 1, tzinfo=UTC),
        effective_to=None,
        reason="stable-to-stable contract",
        evidence_ref="official-notice:123",
        reviewed_by="USER",
        ledger_version="ledger-v1",
    )
    ledger = ExclusionLedger(ledger_version="ledger-v1", entries=(entry,))

    assert entry.symbol == "USDCUSDT"
    assert ledger.entries == (entry,)
    with pytest.raises(ValidationError, match="ledger_version"):
        ExclusionLedger(ledger_version="ledger-v2", entries=(entry,))
    with pytest.raises(ValidationError, match="reviewed_by"):
        ExclusionLedgerEntry(
            **{**entry.model_dump(), "reviewed_by": " "}
        )


def test_universe_snapshot_enforces_frozen_daily_time_boundaries() -> None:
    snapshot = universe_snapshot()

    assert snapshot.ranking_window_end == UTC_MIDNIGHT
    assert snapshot.effective_from == UTC_MIDNIGHT + timedelta(minutes=15)

    with pytest.raises(ValidationError, match="00:05"):
        universe_snapshot(selected_at=UTC_MIDNIGHT + timedelta(minutes=6))
    with pytest.raises(ValidationError, match="24 hours"):
        universe_snapshot(
            ranking_window_start=UTC_MIDNIGHT - timedelta(hours=23, minutes=45)
        )
    with pytest.raises(ValidationError, match="00:15"):
        universe_snapshot(effective_from=UTC_MIDNIGHT + timedelta(minutes=30))


def test_universe_snapshot_requires_ranked_unique_disjoint_members() -> None:
    with pytest.raises(ValidationError, match="member_count"):
        universe_snapshot(member_count=1)
    with pytest.raises(ValidationError, match="contiguous"):
        universe_snapshot(
            members=(universe_member(), universe_member("ETHUSDT", 3)),
        )
    with pytest.raises(ValidationError, match="unique"):
        universe_snapshot(
            members=(universe_member(), universe_member("BTCUSDT", 2)),
        )

    blocked = UniverseBlockedCandidate(
        symbol="BTCUSDT",
        reason_codes=(UniverseBlockReason.CANDLE_GAP,),
        evidence_refs=("anomaly:1",),
    )
    with pytest.raises(ValidationError, match="both selected and blocked"):
        universe_snapshot(blocked_candidates=(blocked,))


def test_universe_models_reject_bad_hashes_naive_times_and_extra_fields() -> None:
    with pytest.raises(ValidationError, match="sha256"):
        universe_snapshot(universe_version="not-a-hash")
    with pytest.raises(ValidationError, match="UTC"):
        contract_entry(effective_from=datetime(2023, 1, 1))
    with pytest.raises(ValidationError, match="extra_forbidden"):
        universe_snapshot(unexpected=True)

    blocked = UniverseBlockedCandidate(
        symbol="DOGEUSDT",
        reason_codes=(UniverseBlockReason.SOURCE_MISMATCH,),
        evidence_refs=("archive:a", "rest:b"),
    )
    first = universe_snapshot(
        members=(universe_member(),),
        member_count=1,
        blocked_candidates=(blocked,),
        universe_version=HASH_C,
    )
    repeated = UniverseSnapshot.model_validate(first.model_dump())
    assert first.model_dump_json() == repeated.model_dump_json()
