"""Aggregate Trade ordering and whole-Candle OHLC fallback tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from slagalpha.backtest.replay import (
    AggregateTrade,
    AggregateTradeBatch,
    ArmedReplayRequest,
    ArmedState,
    OrderingSource,
    ReplayEventType,
    ReplayInputError,
    TradeExitReason,
    TradeReplayRequest,
    replay_armed,
    replay_trade,
)
from slagalpha.strategy.setup import Direction


def armed_request() -> ArmedReplayRequest:
    return ArmedReplayRequest(
        logical_signal_id="d" * 64,
        direction=Direction.LONG,
        entry_price=Decimal("100"),
        invalidation_price=Decimal("90"),
        stop_price=Decimal("89"),
        atr_at_confirmation=Decimal("10"),
        confirmation_close=datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
        expires_at=datetime(2024, 1, 1, 11, 0, tzinfo=UTC),
    )


def trade_request() -> TradeReplayRequest:
    return TradeReplayRequest(
        armed=armed_request(),
        tp1=Decimal("111"),
        tp2=Decimal("122"),
        tick_size=Decimal("1"),
        max_holding_bars=16,
    )


def candles(rows: list[tuple[datetime, int, int, int, int]]) -> pd.DataFrame:
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


def batch(
    open_time: datetime,
    prices: list[int],
    *,
    first_id: int,
    complete: bool = True,
) -> AggregateTradeBatch:
    return AggregateTradeBatch(
        candle_open_time=open_time,
        candle_close_time_exclusive=open_time + timedelta(minutes=1),
        complete=complete,
        trades=tuple(
            AggregateTrade(
                aggregate_trade_id=first_id + index,
                trade_time=open_time + timedelta(seconds=index * 10),
                price=Decimal(price),
                quantity=Decimal(1),
            )
            for index, price in enumerate(prices)
        ),
    )


def test_aggregate_order_resolves_entry_before_or_after_invalidation() -> None:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    frame = candles([(start, 95, 101, 89, 100)])
    entry_first = replay_armed(
        armed_request(), frame, (batch(start, [95, 101, 89, 100], first_id=1),)
    )
    invalidation_first = replay_armed(
        armed_request(), frame, (batch(start, [95, 89, 101, 100], first_id=10),)
    )
    assert entry_first.state is ArmedState.TRIGGERED
    assert entry_first.events[0].source == "AGG_TRADE"
    assert entry_first.ordering_source is OrderingSource.AGG_TRADES
    assert entry_first.ohlc_fallback_count == 0
    assert invalidation_first.state is ArmedState.INVALIDATED


def test_aggregate_tp_path_closes_before_later_stop() -> None:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    frame = candles(
        [(start, 99, 101, 98, 100), (start + timedelta(minutes=1), 100, 123, 88, 100)]
    )
    batches = (
        batch(start, [99, 98, 101, 100], first_id=1),
        batch(start + timedelta(minutes=1), [100, 111, 122, 123, 88, 100], first_id=10),
    )
    result = replay_trade(trade_request(), frame, batches)
    assert result.exit_reason is TradeExitReason.TP2
    assert result.ordering_source is OrderingSource.AGG_TRADES
    assert [event.event_type for event in result.events] == [
        ReplayEventType.ENTRY_FILL,
        ReplayEventType.TP1_FILL,
        ReplayEventType.TP2_FILL,
        ReplayEventType.CLOSED,
    ]
    assert all(event.source == "AGG_TRADE" for event in result.events)
    stop_first_batches = (
        batches[0],
        batch(start + timedelta(minutes=1), [100, 88, 123, 100], first_id=20),
    )
    stop_first = replay_trade(trade_request(), frame, stop_first_batches)
    assert stop_first.exit_reason is TradeExitReason.SL
    assert all(
        event.event_type is not ReplayEventType.TP1_FILL
        for event in stop_first.events
    )


def test_incomplete_batch_falls_back_for_whole_candle() -> None:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    frame = candles(
        [(start, 99, 101, 98, 100), (start + timedelta(minutes=1), 100, 123, 88, 100)]
    )
    batches = (
        batch(start, [99, 98, 101, 100], first_id=1),
        batch(
            start + timedelta(minutes=1),
            [100, 111, 122, 123, 88, 100],
            first_id=10,
            complete=False,
        ),
    )
    result = replay_trade(trade_request(), frame, batches)
    assert result.exit_reason is TradeExitReason.SL
    assert result.ordering_source is OrderingSource.OHLC_ADVERSE
    assert result.ohlc_fallback_count == 1


def test_complete_batch_that_contradicts_ohlc_fails_closed() -> None:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    frame = candles([(start, 99, 101, 98, 100)])
    contradictory = batch(start, [99, 100], first_id=1)
    with pytest.raises(ReplayInputError, match="do not reproduce Candle OHLC"):
        replay_armed(armed_request(), frame, (contradictory,))
