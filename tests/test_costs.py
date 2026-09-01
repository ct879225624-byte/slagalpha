"""Golden tests for P7.4a Fill fees, slippage and PnL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd

from slagalpha.backtest.costs import (
    CostScenario,
    ExecutionSide,
    cost_aware_breakeven,
    replay_with_costs,
)
from slagalpha.backtest.replay import (
    ArmedReplayRequest,
    ReplayEventType,
    TradeExitReason,
    TradeReplayRequest,
)
from slagalpha.strategy.setup import Direction


def trade_request(
    direction: Direction = Direction.LONG,
    *,
    tick_size: Decimal = Decimal("0.1"),
) -> TradeReplayRequest:
    return TradeReplayRequest(
        armed=ArmedReplayRequest(
            logical_signal_id="b" * 64,
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
        tick_size=tick_size,
        max_holding_bars=16,
    )


def completed_candles(direction: Direction = Direction.LONG) -> pd.DataFrame:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    rows = (
        [(start, 99, 101, 98, 100), (start + timedelta(minutes=1), 105, 123, 99, 120)]
        if direction is Direction.LONG
        else [(start, 101, 102, 99, 100), (start + timedelta(minutes=1), 95, 101, 77, 80)]
    )
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


def deadline_candles(
    deadline_ohlc: tuple[int, int, int, int],
) -> pd.DataFrame:
    start = datetime(2024, 1, 1, 10, 1, tzinfo=UTC)
    rows = [
        (start + timedelta(minutes=offset), 101, 105, 95, 102)
        for offset in range(240)
    ]
    rows[0] = (start, 99, 101, 95, 100)
    rows[-1] = (start + timedelta(minutes=239), *deadline_ohlc)
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


def test_baseline_fill_ledger_matches_manual_calculation() -> None:
    result = replay_with_costs(
        trade_request(), completed_candles(), CostScenario.BASELINE
    )
    assert [fill.side for fill in result.fills] == [
        ExecutionSide.BUY,
        ExecutionSide.SELL,
        ExecutionSide.SELL,
    ]
    assert [fill.simulated_price for fill in result.fills] == [
        Decimal("100.0200"),
        Decimal("110.9778"),
        Decimal("121.9756"),
    ]
    assert result.gross_pnl == Decimal("16.5")
    assert result.simulated_execution_pnl == Decimal("16.45670")
    assert result.total_slippage_cost == Decimal("0.04330")
    assert result.total_fee == Decimal("0.129898020")
    assert result.net_pnl_before_funding == Decimal("16.326801980")
    assert result.gross_r == Decimal("1.5")
    assert result.net_r_before_funding is not None
    assert result.net_r_before_funding * Decimal(11) == result.net_pnl_before_funding


def test_zero_and_stress_scenarios_preserve_gross_but_change_net() -> None:
    request = trade_request()
    candles = completed_candles()
    zero = replay_with_costs(request, candles, CostScenario.ZERO)
    stress = replay_with_costs(request, candles, CostScenario.STRESS)
    assert zero.gross_pnl == stress.gross_pnl == Decimal("16.5")
    assert zero.total_fee == zero.total_slippage_cost == Decimal(0)
    assert zero.net_pnl_before_funding == Decimal("16.5")
    assert stress.total_slippage_cost == Decimal("0.108250")
    assert stress.total_fee == Decimal("0.129895050")
    assert stress.net_pnl_before_funding == Decimal("16.261854950")


def test_cost_breakeven_rounding_and_short_fill_sides_are_mirrored() -> None:
    long_request = trade_request()
    short_request = trade_request(Direction.SHORT)
    assert cost_aware_breakeven(
        long_request, Decimal("100"), CostScenario.BASELINE
    ) == Decimal("100.2")
    assert cost_aware_breakeven(
        short_request, Decimal("100"), CostScenario.BASELINE
    ) == Decimal("99.8")
    result = replay_with_costs(
        short_request,
        completed_candles(Direction.SHORT),
        CostScenario.BASELINE,
    )
    assert [fill.side for fill in result.fills] == [
        ExecutionSide.SELL,
        ExecutionSide.BUY,
        ExecutionSide.BUY,
    ]
    assert result.gross_pnl == Decimal("16.5")
    assert result.total_fee == Decimal("0.110098020")
    assert result.net_pnl_before_funding == Decimal("16.353201980")


def test_deadline_collision_selects_worse_net_path() -> None:
    request = trade_request()
    stop_is_worse = replay_with_costs(
        request,
        deadline_candles((100, 105, 88, 90)),
        CostScenario.BASELINE,
    )
    assert stop_is_worse.trade.exit_reason is TradeExitReason.SL
    assert stop_is_worse.fills[-1].event_type is ReplayEventType.STOP_FILL

    time_is_worse = replay_with_costs(
        request,
        deadline_candles((80, 105, 79, 90)),
        CostScenario.BASELINE,
    )
    assert time_is_worse.trade.exit_reason is TradeExitReason.TIME_EXIT
    assert time_is_worse.fills[-1].theoretical_price == Decimal("80")

    favorable_targets_are_not_selected_over_worse_time_exit = replay_with_costs(
        request,
        deadline_candles((100, 123, 95, 120)),
        CostScenario.BASELINE,
    )
    assert (
        favorable_targets_are_not_selected_over_worse_time_exit.trade.exit_reason
        is TradeExitReason.TIME_EXIT
    )
