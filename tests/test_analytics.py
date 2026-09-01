"""Golden tests for P7.4b Funding protection and MAE/MFE."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd

from slagalpha.backtest.analytics import (
    FundingDataset,
    FundingObservation,
    apply_funding,
    calculate_excursions,
)
from slagalpha.backtest.costs import CostScenario, replay_with_costs
from slagalpha.backtest.replay import ArmedReplayRequest, ReplayEventType, TradeReplayRequest
from slagalpha.strategy.setup import Direction


def trade_request(direction: Direction = Direction.LONG) -> TradeReplayRequest:
    return TradeReplayRequest(
        armed=ArmedReplayRequest(
            logical_signal_id="c" * 64,
            direction=direction,
            entry_price=Decimal("100"),
            invalidation_price=Decimal("90" if direction is Direction.LONG else "110"),
            stop_price=Decimal("89" if direction is Direction.LONG else "111"),
            atr_at_confirmation=Decimal("10"),
            confirmation_close=datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
            expires_at=datetime(2024, 1, 1, 11, 0, tzinfo=UTC),
        ),
        tp1=Decimal("111" if direction is Direction.LONG else "89"),
        tp2=Decimal("122" if direction is Direction.LONG else "78"),
        tick_size=Decimal("0.1"),
        max_holding_bars=16,
    )


def trade_candles(direction: Direction = Direction.LONG) -> pd.DataFrame:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    rows = [
        (start, 99, 101, 98, 100),
        (start + timedelta(minutes=1), 105, 112, 99, 110),
        (start + timedelta(minutes=2), 108, 110, 105, 109),
        (start + timedelta(minutes=3), 115, 123, 108, 122),
    ]
    if direction is Direction.SHORT:
        rows = [
            (time, 200 - open_price, 200 - low, 200 - high, 200 - close)
            for time, open_price, high, low, close in rows
        ]
    return pd.DataFrame(
        {
            "open_time": pd.to_datetime([row[0] for row in rows], utc=True),
            "close_time_exclusive": pd.to_datetime(
                [row[0] + timedelta(minutes=1) for row in rows], utc=True
            ),
            "open": [row[1] for row in rows],
            "high": [row[2] for row in rows],
            "low": [row[3] for row in rows],
            "close": [row[4] for row in rows],
            "quote_volume": 1_000.0,
            "is_closed": True,
        }
    )


def funding_dataset(*, missing_rate: bool = False) -> FundingDataset:
    settlement = datetime(2024, 1, 1, 10, 1, 30, tzinfo=UTC)
    return FundingDataset(
        coverage_start=datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
        coverage_end=datetime(2024, 1, 1, 10, 5, tzinfo=UTC),
        expected_settlement_times=(settlement,),
        observations=(
            FundingObservation(
                settlement_time=settlement,
                rate=None if missing_rate else Decimal("0.001"),
                mark_price=Decimal("110"),
            ),
        ),
    )


def test_funding_uses_open_fraction_after_tp1() -> None:
    costed = replay_with_costs(
        trade_request(), trade_candles(), CostScenario.BASELINE
    )
    result = apply_funding(costed, funding_dataset())
    assert result.net_statistics_eligible is True
    assert result.funding_cash_flow == Decimal("-0.0550")
    assert result.applications[0].event_type is ReplayEventType.FUNDING
    assert result.applications[0].open_fraction == Decimal("0.5")
    assert costed.net_pnl_before_funding is not None
    assert result.net_pnl == costed.net_pnl_before_funding + Decimal("-0.0550")


def test_gs046_missing_funding_preserves_gross_and_excludes_net() -> None:
    costed = replay_with_costs(
        trade_request(), trade_candles(), CostScenario.BASELINE
    )
    result = apply_funding(costed, funding_dataset(missing_rate=True))
    assert costed.gross_pnl == Decimal("16.5")
    assert result.funding_data_missing is True
    assert result.net_statistics_eligible is False
    assert result.net_pnl is None
    assert result.net_r is None
    assert result.applications[0].event_type is ReplayEventType.FUNDING_DATA_MISSING
    absent_dataset = apply_funding(costed, None)
    assert absent_dataset.funding_data_missing is True
    assert absent_dataset.net_statistics_eligible is False


def test_mae_mfe_use_trade_level_extremes_through_exit_candle() -> None:
    candles = trade_candles()
    costed = replay_with_costs(trade_request(), candles, CostScenario.ZERO)
    excursions = calculate_excursions(costed, candles)
    assert excursions is not None
    assert excursions.observed_candles == 4
    assert excursions.mae_price == Decimal("98")
    assert excursions.mfe_price == Decimal("123")
    assert excursions.mae_r * Decimal(11) == Decimal(2)
    assert excursions.mfe_r * Decimal(11) == Decimal(23)
    assert excursions.ordering_source == "OHLC_ADVERSE"


def test_short_mae_mfe_is_price_axis_mirror() -> None:
    candles = trade_candles(Direction.SHORT)
    costed = replay_with_costs(
        trade_request(Direction.SHORT), candles, CostScenario.ZERO
    )
    excursions = calculate_excursions(costed, candles)
    assert excursions is not None
    assert excursions.mae_price == Decimal("102")
    assert excursions.mfe_price == Decimal("77")
    assert excursions.mae_r * Decimal(11) == Decimal(2)
    assert excursions.mfe_r * Decimal(11) == Decimal(23)
