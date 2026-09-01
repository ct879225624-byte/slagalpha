"""Golden tests for the ARMED portion of event-driven replay."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd

from slagalpha.backtest.event_log import LifecycleState, reduce_event_log
from slagalpha.backtest.replay import (
    ArmedReplayRequest,
    ArmedState,
    PositionState,
    ReplayEventType,
    ReplayReason,
    TradeExitReason,
    TradeReplayRequest,
    apply_late_entry_event,
    replay_armed,
    replay_trade,
)
from slagalpha.strategy.setup import Direction


def armed_request(direction: Direction = Direction.LONG) -> ArmedReplayRequest:
    return ArmedReplayRequest(
        logical_signal_id="a" * 64,
        direction=direction,
        entry_price=Decimal("100"),
        invalidation_price=Decimal("90" if direction is Direction.LONG else "110"),
        stop_price=Decimal("89" if direction is Direction.LONG else "111"),
        atr_at_confirmation=Decimal("10"),
        confirmation_close=datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
        expires_at=datetime(2024, 1, 1, 11, 0, tzinfo=UTC),
    )


def trade_request(direction: Direction = Direction.LONG) -> TradeReplayRequest:
    return TradeReplayRequest(
        armed=armed_request(direction),
        tp1=Decimal("111" if direction is Direction.LONG else "89"),
        tp2=Decimal("122" if direction is Direction.LONG else "78"),
        tick_size=Decimal("1"),
        max_holding_bars=16,
    )


def minute_candles(rows: Sequence[tuple[datetime, float, float, float, float]]) -> pd.DataFrame:
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


def test_gs037_normal_entry_cross() -> None:
    candles = minute_candles(
        [(datetime(2024, 1, 1, 10, 0, tzinfo=UTC), 99, 101, 98.5, 100)]
    )
    result = replay_armed(armed_request(), candles)
    assert result.state is ArmedState.TRIGGERED
    assert result.theoretical_entry_price == Decimal("100")
    assert result.events[0].reason is ReplayReason.ENTRY_NORMAL_CROSS


def test_gs038_gap_boundary_allows_and_above_is_missed() -> None:
    time = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    allowed = replay_armed(armed_request(), minute_candles([(time, 101.5, 102, 101, 101.8)]))
    missed = replay_armed(
        armed_request(), minute_candles([(time, 101.5001, 102, 101, 101.8)])
    )
    assert allowed.state is ArmedState.TRIGGERED
    assert allowed.theoretical_entry_price == Decimal("101.5")
    assert missed.state is ArmedState.MISSED


def test_gs039_entry_window_right_edge_expires_before_price() -> None:
    time = datetime(2024, 1, 1, 11, 0, tzinfo=UTC)
    result = replay_armed(armed_request(), minute_candles([(time, 99, 101, 98, 100)]))
    assert result.state is ArmedState.EXPIRED
    assert result.events[0].exchange_time == armed_request().expires_at


def test_gs040_same_candle_entry_and_invalidation_keeps_trade_path() -> None:
    time = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    result = replay_armed(armed_request(), minute_candles([(time, 99, 101, 89, 95)]))
    assert result.state is ArmedState.TRIGGERED
    assert result.stop_also_touched_on_entry_candle is True
    assert result.events[0].reason is ReplayReason.ENTRY_AND_INVALIDATION_SAME_CANDLE


def test_gs041_invalidation_without_entry() -> None:
    time = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    result = replay_armed(armed_request(), minute_candles([(time, 95, 99, 89, 92)]))
    assert result.state is ArmedState.INVALIDATED


def test_short_is_price_axis_mirror_and_replay_is_repeatable() -> None:
    time = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    candles = minute_candles([(time, 101, 101.5, 99, 100)])
    first = replay_armed(armed_request(Direction.SHORT), candles)
    repeated = replay_armed(armed_request(Direction.SHORT), candles)
    assert first.state is ArmedState.TRIGGERED
    assert first.theoretical_entry_price == Decimal("100")
    assert first == repeated
    assert first.model_dump_json() == repeated.model_dump_json()


def test_gs042_breakeven_activates_on_next_minute_only() -> None:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    candles = minute_candles(
        [
            (start, 99, 101, 98, 100),
            (start + timedelta(minutes=1), 105, 112, 95, 101),
            (start + timedelta(minutes=2), 102, 105, 100, 103),
        ]
    )
    result = replay_trade(trade_request(), candles)
    assert result.position_state is PositionState.FLAT
    assert result.exit_reason is TradeExitReason.BREAKEVEN_AFTER_TP1
    assert result.breakeven_price == Decimal("100")
    assert [event.event_type for event in result.events] == [
        ReplayEventType.ENTRY_FILL,
        ReplayEventType.TP1_FILL,
        ReplayEventType.BREAKEVEN_ACTIVATED,
        ReplayEventType.STOP_FILL,
        ReplayEventType.CLOSED,
    ]
    assert result.events[2].exchange_time == start + timedelta(minutes=2)


def test_gs043_full_position_stop_wins_over_tp1_in_same_minute() -> None:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    candles = minute_candles(
        [
            (start, 99, 101, 98, 100),
            (start + timedelta(minutes=1), 100, 112, 88, 101),
        ]
    )
    result = replay_trade(trade_request(), candles)
    assert result.exit_reason is TradeExitReason.SL
    assert result.events[-2].event_type is ReplayEventType.STOP_FILL
    assert result.events[-2].quantity_fraction == Decimal(1)
    assert all(event.event_type is not ReplayEventType.TP1_FILL for event in result.events)


def test_gs044_tp1_and_tp2_fill_in_order_in_same_minute() -> None:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    candles = minute_candles(
        [
            (start, 99, 101, 98, 100),
            (start + timedelta(minutes=1), 105, 123, 99, 120),
        ]
    )
    result = replay_trade(trade_request(), candles)
    assert result.exit_reason is TradeExitReason.TP2
    assert [event.event_type for event in result.events] == [
        ReplayEventType.ENTRY_FILL,
        ReplayEventType.TP1_FILL,
        ReplayEventType.TP2_FILL,
        ReplayEventType.CLOSED,
    ]
    assert result.events[1].quantity_fraction == Decimal("0.5")
    assert result.events[2].quantity_fraction == Decimal("0.5")


def test_gs045_time_exit_uses_first_open_at_deadline() -> None:
    start = datetime(2024, 1, 1, 10, 1, tzinfo=UTC)
    rows = [
        (start + timedelta(minutes=offset), 101, 105, 95, 102)
        for offset in range(240)
    ]
    rows[0] = (start, 99, 101, 95, 100)
    result = replay_trade(trade_request(), minute_candles(rows))
    deadline = datetime(2024, 1, 1, 14, 0, tzinfo=UTC)
    assert result.exit_reason is TradeExitReason.TIME_EXIT
    assert result.time_exit_deadline == deadline
    assert result.exit_time == deadline
    assert result.events[-2].event_type is ReplayEventType.TIME_EXIT
    assert result.events[-2].theoretical_price == Decimal("101")


def test_short_position_lifecycle_is_mirrored_and_repeatable() -> None:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    candles = minute_candles(
        [
            (start, 101, 102, 99, 100),
            (start + timedelta(minutes=1), 95, 101, 88, 90),
            (start + timedelta(minutes=2), 98, 100, 95, 97),
        ]
    )
    first = replay_trade(trade_request(Direction.SHORT), candles)
    repeated = replay_trade(trade_request(Direction.SHORT), candles)
    assert first.exit_reason is TradeExitReason.BREAKEVEN_AFTER_TP1
    assert first == repeated


def test_late_entry_after_terminal_is_appended_idempotently() -> None:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    terminal = replay_armed(
        armed_request(), minute_candles([(start, 95, 99, 89, 92)])
    )
    first = apply_late_entry_event(
        terminal,
        exchange_time=start + timedelta(minutes=1),
        source="KLINE_1M",
        source_ref=(start + timedelta(minutes=1)).isoformat(timespec="milliseconds"),
        theoretical_price=Decimal("100"),
    )
    repeated = apply_late_entry_event(
        first,
        exchange_time=start + timedelta(minutes=1),
        source="KLINE_1M",
        source_ref=(start + timedelta(minutes=1)).isoformat(timespec="milliseconds"),
        theoretical_price=Decimal("100.0"),
    )
    assert repeated == first
    assert repeated.state is ArmedState.INVALIDATED
    assert len(repeated.events) == 2
    assert repeated.events[-1].event_type is ReplayEventType.LATE_EVENT_IGNORED
    assert repeated.theoretical_entry_price is None


def test_event_log_checkpoint_resume_and_duplicate_application_are_idempotent() -> None:
    start = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    result = replay_trade(
        trade_request(),
        minute_candles(
            [
                (start, 99, 101, 98, 100),
                (start + timedelta(minutes=1), 105, 123, 99, 120),
            ]
        ),
    )
    uninterrupted = reduce_event_log(result.events)
    checkpoint = reduce_event_log(result.events[:2])
    resumed = reduce_event_log(result.events[2:], checkpoint)
    replayed_duplicates = reduce_event_log(result.events, resumed)
    assert checkpoint.position_state is PositionState.HALF_AFTER_TP1
    assert uninterrupted.lifecycle_state is LifecycleState.CLOSED
    assert uninterrupted.closed_event_seen is True
    assert resumed.model_copy(update={"ignored_duplicate_count": 0}) == uninterrupted
    assert replayed_duplicates.lifecycle_state is LifecycleState.CLOSED
    assert replayed_duplicates.open_fraction == Decimal(0)
    assert replayed_duplicates.applied_event_ids == uninterrupted.applied_event_ids
    assert replayed_duplicates.ignored_duplicate_count == len(result.events)
