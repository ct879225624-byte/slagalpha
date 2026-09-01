"""Integration tests for historical Universe/contract-rule/P7 gates."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd

from slagalpha.backtest.costs import CostScenario
from slagalpha.backtest.historical import (
    HistoricalReplayBlockReason,
    HistoricalReplayStatus,
    evaluate_universe_rule_gate,
    replay_historical_cases,
)
from slagalpha.backtest.replay import ArmedReplayRequest, TradeReplayRequest
from slagalpha.backtest.runner import ReplayCase
from slagalpha.data.universe import build_daily_universe
from slagalpha.domain.universe import (
    ContractRegistry,
    ContractRegistryEntry,
    EvidenceConfidence,
    ExclusionLedger,
    RegistryVerification,
    UniverseSnapshot,
)
from slagalpha.strategy.setup import Direction

SELECTED_AT = datetime(2024, 1, 2, 0, 5, tzinfo=UTC)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _rule(verification: RegistryVerification) -> ContractRegistryEntry:
    return ContractRegistryEntry(
        symbol="AAAUSDT",
        base_asset="AAA",
        quote_asset="USDT",
        margin_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date=datetime(2023, 1, 1, tzinfo=UTC),
        derived_first_candle_at=datetime(2023, 1, 1, tzinfo=UTC),
        inferred_delisted_at=None,
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("10000"),
        min_notional=Decimal("5"),
        effective_from=datetime(2023, 1, 1, tzinfo=UTC),
        effective_to=None,
        source_ref="fixture:rule",
        source_snapshot_hash=_hash("rule"),
        derivation_method="FIXTURE",
        reviewed_by="TEST",
        verification_status=verification,
        confidence=EvidenceConfidence.HIGH,
    )


def _candles_15m() -> pd.DataFrame:
    opens = pd.date_range(
        datetime(2024, 1, 1, tzinfo=UTC),
        periods=96,
        freq="15min",
    )
    return pd.DataFrame(
        {
            "symbol": "AAAUSDT",
            "interval": "15m",
            "open_time": opens,
            "close_time_exclusive": opens + pd.Timedelta(minutes=15),
            "quote_volume": Decimal("100"),
            "is_closed": True,
            "source_file_hash": _hash("candles"),
        }
    )


def _universe(registry: ContractRegistry) -> UniverseSnapshot:
    return build_daily_universe(
        selected_at=SELECTED_AT,
        registry=registry,
        exclusion_ledger=ExclusionLedger(ledger_version="empty", entries=()),
        fifteen_minute_candles={"AAAUSDT": _candles_15m()},
        interval_evidence={
            "AAAUSDT": {
                interval: _hash(interval) for interval in ("15m", "1h", "4h", "1d")
            }
        },
    )


def _case(tick_size: Decimal = Decimal("0.1")) -> ReplayCase:
    confirmation = datetime(2024, 1, 2, 0, 15, tzinfo=UTC)
    request = TradeReplayRequest(
        armed=ArmedReplayRequest(
            logical_signal_id="a" * 64,
            symbol="AAAUSDT",
            direction=Direction.LONG,
            entry_price=Decimal("100"),
            invalidation_price=Decimal("90"),
            stop_price=Decimal("89"),
            atr_at_confirmation=Decimal("10"),
            confirmation_close=confirmation,
            expires_at=confirmation + timedelta(hours=1),
        ),
        tp1=Decimal("111"),
        tp2=Decimal("122"),
        tick_size=tick_size,
        max_holding_bars=16,
    )
    candles = pd.DataFrame(
        {
            "open_time": [confirmation],
            "close_time_exclusive": [confirmation + timedelta(minutes=1)],
            "open": [Decimal("100")],
            "high": [Decimal("123")],
            "low": [Decimal("98")],
            "close": [Decimal("120")],
            "quote_volume": [Decimal("1000")],
            "is_closed": [True],
        }
    )
    return ReplayCase(request=request, candles=candles, scenario=CostScenario.ZERO)


def test_unverified_rule_blocks_before_invalid_replay_data_is_read() -> None:
    rule = _rule(RegistryVerification.UNVERIFIED)
    registry = ContractRegistry(registry_version="draft", entries=(rule,))
    universe = _universe(
        ContractRegistry(
            registry_version="identity-equivalent",
            entries=(_rule(RegistryVerification.VERIFIED),),
        )
    )
    case = _case()
    invalid_candles = case.candles.drop(columns="high")

    result = replay_historical_cases(
        universe=universe,
        registry=registry,
        cases=(ReplayCase(case.request, invalid_candles, case.scenario),),
    )

    assert result.cases[0].status is HistoricalReplayStatus.BLOCKED
    assert result.cases[0].reason_codes == (
        HistoricalReplayBlockReason.CONTRACT_RULE_UNVERIFIED,
    )


def test_verified_matching_rule_executes_p7_and_hash_is_repeatable() -> None:
    rule = _rule(RegistryVerification.VERIFIED)
    registry = ContractRegistry(registry_version="verified", entries=(rule,))
    universe = _universe(registry)
    case = _case()

    first = replay_historical_cases(universe=universe, registry=registry, cases=(case,))
    repeated = replay_historical_cases(universe=universe, registry=registry, cases=(case,))

    assert first == repeated
    assert first.cases[0].status is HistoricalReplayStatus.EXECUTED
    assert first.cases[0].replay is not None


def test_tick_mismatch_and_real_gate_report_fail_closed() -> None:
    verified = _rule(RegistryVerification.VERIFIED)
    registry = ContractRegistry(registry_version="verified", entries=(verified,))
    universe = _universe(registry)

    result = replay_historical_cases(
        universe=universe,
        registry=registry,
        cases=(_case(Decimal("0.01")),),
    )
    report = evaluate_universe_rule_gate(
        universe,
        ContractRegistry(
            registry_version="draft",
            entries=(_rule(RegistryVerification.UNVERIFIED),),
        ),
    )

    assert result.cases[0].reason_codes == (
        HistoricalReplayBlockReason.TICK_SIZE_MISMATCH,
    )
    assert report.member_count == 1
    assert report.eligible_count == 0
    assert report.reason_counts == {"CONTRACT_RULE_UNVERIFIED": 1}
