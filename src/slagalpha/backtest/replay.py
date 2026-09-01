"""Event-driven replay primitives for the ARMED signal lifecycle."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self, cast

import pandas as pd
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from slagalpha.domain.symbols import normalize_symbol
from slagalpha.strategy.indicators import IndicatorInputError, validate_indicator_input
from slagalpha.strategy.plans import TRADE_PLAN_VERSION, TakeProfitEvaluation
from slagalpha.strategy.setup import Direction

REPLAY_VERSION = "event-replay/0.1.0"
ALLOWED_MAX_HOLDING_BARS = frozenset({16, 32, 48})


class ReplayInputError(ValueError):
    """Raised when historical replay input cannot be processed safely."""


class ArmedState(StrEnum):
    """Terminal or active state of an ARMED signal replay."""

    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    INVALIDATED = "INVALIDATED"
    MISSED = "MISSED"
    EXPIRED = "EXPIRED"


class ReplayEventType(StrEnum):
    """P7 replay event types implemented through P7.3."""

    ENTRY_FILL = "ENTRY_FILL"
    INVALIDATED = "INVALIDATED"
    MISSED = "MISSED"
    EXPIRED = "EXPIRED"
    TP1_FILL = "TP1_FILL"
    BREAKEVEN_ACTIVATED = "BREAKEVEN_ACTIVATED"
    STOP_FILL = "STOP_FILL"
    TP2_FILL = "TP2_FILL"
    TIME_EXIT = "TIME_EXIT"
    CLOSED = "CLOSED"
    FUNDING = "FUNDING"
    FUNDING_DATA_MISSING = "FUNDING_DATA_MISSING"
    LATE_EVENT_IGNORED = "LATE_EVENT_IGNORED"


class ReplayReason(StrEnum):
    """Stable replay reason codes implemented through P7.3."""

    ENTRY_NORMAL_CROSS = "ENTRY_NORMAL_CROSS"
    ENTRY_GAP_WITHIN_LIMIT = "ENTRY_GAP_WITHIN_LIMIT"
    ENTRY_AND_INVALIDATION_SAME_CANDLE = "ENTRY_AND_INVALIDATION_SAME_CANDLE"
    STRUCTURE_INVALIDATED_BEFORE_ENTRY = "STRUCTURE_INVALIDATED_BEFORE_ENTRY"
    ENTRY_GAP_TOO_LARGE = "ENTRY_GAP_TOO_LARGE"
    ENTRY_WINDOW_EXPIRED = "ENTRY_WINDOW_EXPIRED"
    INITIAL_STOP = "INITIAL_STOP"
    TP1_TARGET = "TP1_TARGET"
    BREAKEVEN_NEXT_MINUTE = "BREAKEVEN_NEXT_MINUTE"
    BREAKEVEN_STOP = "BREAKEVEN_STOP"
    TP2_TARGET = "TP2_TARGET"
    MAX_HOLDING_REACHED = "MAX_HOLDING_REACHED"
    FUNDING_APPLIED = "FUNDING_APPLIED"
    FUNDING_REQUIRED_DATA_MISSING = "FUNDING_REQUIRED_DATA_MISSING"
    LATE_ENTRY_AFTER_TERMINAL = "LATE_ENTRY_AFTER_TERMINAL"


class PositionState(StrEnum):
    """Open-position phase after Entry."""

    FULL = "FULL"
    HALF_AFTER_TP1 = "HALF_AFTER_TP1"
    FLAT = "FLAT"


class TradeExitReason(StrEnum):
    """Terminal reason for a triggered trade."""

    SL = "SL"
    BREAKEVEN_AFTER_TP1 = "BREAKEVEN_AFTER_TP1"
    TP2 = "TP2"
    TIME_EXIT = "TIME_EXIT"


class OrderingSource(StrEnum):
    """Price-path source used by a replay result."""

    AGG_TRADES = "AGG_TRADES"
    OHLC_ADVERSE = "OHLC_ADVERSE"


class AggregateTrade(BaseModel):
    """One public aggregate trade ordered by exchange time and ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    aggregate_trade_id: int
    trade_time: datetime
    price: Decimal
    quantity: Decimal

    @field_validator("trade_time")
    @classmethod
    def validate_trade_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Aggregate Trade time must use UTC")
        return value

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        if self.aggregate_trade_id < 0:
            raise ValueError("aggregate_trade_id must be non-negative")
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("Aggregate Trade price must be finite and positive")
        if not self.quantity.is_finite() or self.quantity < 0:
            raise ValueError("Aggregate Trade quantity must be finite and non-negative")
        return self


class AggregateTradeBatch(BaseModel):
    """Completeness assertion and Aggregate Trades for exactly one 1m Candle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candle_open_time: datetime
    candle_close_time_exclusive: datetime
    complete: bool
    trades: tuple[AggregateTrade, ...]

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        if (
            self.candle_open_time.tzinfo is None
            or self.candle_close_time_exclusive.tzinfo is None
            or self.candle_open_time.utcoffset() != timedelta(0)
            or self.candle_close_time_exclusive.utcoffset() != timedelta(0)
        ):
            raise ValueError("Aggregate Trade batch timestamps must use UTC")
        if self.candle_close_time_exclusive != self.candle_open_time + timedelta(minutes=1):
            raise ValueError("Aggregate Trade batch must cover exactly one minute")
        ids = tuple(trade.aggregate_trade_id for trade in self.trades)
        if len(set(ids)) != len(ids):
            raise ValueError("Aggregate Trade IDs must be unique within a batch")
        if any(
            trade.trade_time < self.candle_open_time
            or trade.trade_time >= self.candle_close_time_exclusive
            for trade in self.trades
        ):
            raise ValueError("Aggregate Trade lies outside its Candle interval")
        return self


class ArmedReplayRequest(BaseModel):
    """Frozen plan values needed before a trade exists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    logical_signal_id: str
    direction: Direction
    entry_price: Decimal
    invalidation_price: Decimal
    stop_price: Decimal
    atr_at_confirmation: Decimal
    confirmation_close: datetime
    expires_at: datetime
    symbol: str = "UNKNOWN"

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        return normalized if normalized == "UNKNOWN" else normalize_symbol(value)

    @field_validator("confirmation_close", "expires_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("replay timestamps must be timezone-aware")
        if value.utcoffset() != timedelta(0):
            raise ValueError("replay timestamps must use UTC")
        return value

    @classmethod
    def from_take_profit(cls, plan: TakeProfitEvaluation) -> ArmedReplayRequest:
        if not plan.accepted:
            raise ReplayInputError("ARMED replay requires an accepted TradePlan")
        entry_stop = plan.entry_stop
        if entry_stop.entry_price is None or entry_stop.stop_price is None:
            raise ReplayInputError("accepted TradePlan lacks Entry/Stop")
        request = entry_stop.request
        return cls(
            logical_signal_id=request.logical_signal_id,
            symbol=request.symbol,
            direction=request.direction,
            entry_price=entry_stop.entry_price,
            invalidation_price=request.invalidation_price,
            stop_price=entry_stop.stop_price,
            atr_at_confirmation=request.atr_at_confirmation,
            confirmation_close=request.confirmation_close,
            expires_at=entry_stop.expires_at,
        )


class ReplayEvent(BaseModel):
    """One deterministic state-transition event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    event_type: ReplayEventType
    exchange_time: datetime
    source: str = "KLINE_1M"
    source_ref: str
    reason: ReplayReason
    theoretical_price: Decimal | None
    quantity_fraction: Decimal | None = None


class ArmedReplayResult(BaseModel):
    """Result after consuming all currently supplied 1m candles."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = REPLAY_VERSION
    request: ArmedReplayRequest
    state: ArmedState
    events: tuple[ReplayEvent, ...]
    theoretical_entry_price: Decimal | None
    entry_time: datetime | None
    entry_candle_open_time: datetime | None
    stop_also_touched_on_entry_candle: bool
    ordering_source: OrderingSource = OrderingSource.OHLC_ADVERSE
    ohlc_fallback_count: int = 0


class TradeReplayRequest(BaseModel):
    """Accepted P6 targets and frozen P7.3 holding parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_version: str = TRADE_PLAN_VERSION
    armed: ArmedReplayRequest
    tp1: Decimal
    tp2: Decimal
    tick_size: Decimal
    max_holding_bars: int = 32
    breakeven_price: Decimal | None = None

    @model_validator(mode="after")
    def validate_prices(self) -> Self:
        if self.plan_version != TRADE_PLAN_VERSION:
            raise ValueError("unsupported Trade Plan version for replay")
        values = (self.tp1, self.tp2, self.tick_size)
        if any(not value.is_finite() for value in values) or self.tick_size <= 0:
            raise ValueError("TP prices and tick_size must be finite with positive tick_size")
        if self.breakeven_price is not None and (
            not self.breakeven_price.is_finite() or self.breakeven_price < 0
        ):
            raise ValueError("breakeven_price must be finite and non-negative")
        entry = self.armed.entry_price
        stop = self.armed.stop_price
        ordered = (
            stop < entry < self.tp1 < self.tp2
            if self.armed.direction is Direction.LONG
            else stop > entry > self.tp1 > self.tp2
        )
        if not ordered:
            raise ValueError("Stop, Entry, TP1 and TP2 are not ordered for direction")
        if self.max_holding_bars not in ALLOWED_MAX_HOLDING_BARS:
            raise ValueError("max_holding_bars must be one of 16, 32, or 48")
        return self

    @classmethod
    def from_take_profit(
        cls,
        plan: TakeProfitEvaluation,
        *,
        max_holding_bars: int = 32,
        breakeven_price: Decimal | None = None,
    ) -> TradeReplayRequest:
        if not plan.accepted or plan.tp1 is None or plan.tp2 is None:
            raise ReplayInputError("trade replay requires an accepted TakeProfit plan")
        return cls(
            plan_version=plan.version,
            armed=ArmedReplayRequest.from_take_profit(plan),
            tp1=plan.tp1,
            tp2=plan.tp2,
            tick_size=plan.entry_stop.request.tick_size,
            max_holding_bars=max_holding_bars,
            breakeven_price=breakeven_price,
        )


class TradeReplayResult(BaseModel):
    """Signal and theoretical position lifecycle through P7.3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = REPLAY_VERSION
    request: TradeReplayRequest
    armed_result: ArmedReplayResult
    position_state: PositionState | None
    events: tuple[ReplayEvent, ...]
    open_fraction: Decimal
    breakeven_price: Decimal | None
    breakeven_activation_time: datetime | None
    time_exit_deadline: datetime | None
    exit_reason: TradeExitReason | None
    exit_time: datetime | None
    ordering_source: OrderingSource = OrderingSource.OHLC_ADVERSE
    ohlc_fallback_count: int = 0


def _decimal(value: Any, name: str) -> Decimal:
    converted = Decimal(str(value))
    if not converted.is_finite():
        raise ReplayInputError(f"{name} must be finite")
    return converted


def _event(
    request: ArmedReplayRequest,
    event_type: ReplayEventType,
    exchange_time: datetime,
    source_ref: str,
    reason: ReplayReason,
    price: Decimal | None = None,
    quantity_fraction: Decimal | None = None,
    source: str = "KLINE_1M",
) -> ReplayEvent:
    canonical = "|".join(
        (
            REPLAY_VERSION,
            request.logical_signal_id,
            event_type.value,
            exchange_time.isoformat(timespec="milliseconds"),
            source,
            source_ref,
        )
    )
    return ReplayEvent(
        event_id=hashlib.sha256(canonical.encode()).hexdigest(),
        event_type=event_type,
        exchange_time=exchange_time,
        source=source,
        source_ref=source_ref,
        reason=reason,
        theoretical_price=price,
        quantity_fraction=quantity_fraction,
    )


def validate_replay_candles(candles: pd.DataFrame) -> None:
    """Validate the strict closed UTC 1m replay input contract."""

    try:
        validate_indicator_input(candles)
    except IndicatorInputError as error:
        raise ReplayInputError(str(error)) from error
    required = {"open", "close_time_exclusive"}
    missing = sorted(required.difference(candles.columns))
    if missing:
        raise ReplayInputError(f"missing required 1m columns: {missing}")
    opens = pd.DatetimeIndex(candles["open_time"])
    closes = pd.DatetimeIndex(candles["close_time_exclusive"])
    if opens.tz is None or closes.tz is None:
        raise ReplayInputError("1m timestamps must be timezone-aware")
    if str(opens.tz) != "UTC" or str(closes.tz) != "UTC":
        raise ReplayInputError("1m timestamps must use UTC")
    if any(timestamp.second != 0 or timestamp.microsecond != 0 for timestamp in opens):
        raise ReplayInputError("1m open_time must align to an exact UTC minute")
    if not closes.equals(opens + pd.Timedelta(minutes=1)):
        raise ReplayInputError("1m candles must have exact one-minute intervals")
    if len(opens) > 1 and not bool(
        (opens[1:] - opens[:-1] == pd.Timedelta(minutes=1)).all()
    ):
        raise ReplayInputError("1m candles must use a complete one-minute grid")
    for position in range(len(candles)):
        open_price = _decimal(candles["open"].iloc[position], "open")
        high = _decimal(candles["high"].iloc[position], "high")
        low = _decimal(candles["low"].iloc[position], "low")
        if not low <= open_price <= high:
            raise ReplayInputError("open must be between low and high")


def _aggregate_trade_map(
    candles: pd.DataFrame,
    batches: tuple[AggregateTradeBatch, ...],
) -> dict[datetime, AggregateTradeBatch]:
    by_open: dict[datetime, AggregateTradeBatch] = {}
    candle_positions = {
        cast(datetime, pd.Timestamp(value).to_pydatetime()): position
        for position, value in enumerate(candles["open_time"])
    }
    for batch in batches:
        if batch.candle_open_time in by_open:
            raise ReplayInputError("duplicate Aggregate Trade batch for one Candle")
        position = candle_positions.get(batch.candle_open_time)
        if position is None:
            raise ReplayInputError("Aggregate Trade batch has no matching 1m Candle")
        candle_close = cast(
            datetime,
            pd.Timestamp(candles["close_time_exclusive"].iloc[position]).to_pydatetime(),
        )
        if candle_close != batch.candle_close_time_exclusive:
            raise ReplayInputError("Aggregate Trade batch interval differs from Candle")
        if batch.complete:
            ordered = sorted(
                batch.trades,
                key=lambda trade: (trade.trade_time, trade.aggregate_trade_id),
            )
            open_price = _decimal(candles["open"].iloc[position], "open")
            high = _decimal(candles["high"].iloc[position], "high")
            low = _decimal(candles["low"].iloc[position], "low")
            close = _decimal(candles["close"].iloc[position], "close")
            quote_volume = _decimal(
                candles["quote_volume"].iloc[position], "quote_volume"
            )
            if not ordered:
                if quote_volume != 0 or not open_price == high == low == close:
                    raise ReplayInputError(
                        "complete empty Aggregate Trade batch contradicts Candle"
                    )
            elif (
                ordered[0].price != open_price
                or ordered[-1].price != close
                or max(trade.price for trade in ordered) != high
                or min(trade.price for trade in ordered) != low
            ):
                raise ReplayInputError(
                    "complete Aggregate Trades do not reproduce Candle OHLC"
                )
        by_open[batch.candle_open_time] = batch
    return by_open


def _ordering_source(fallback_count: int, aggregate_count: int) -> OrderingSource:
    return (
        OrderingSource.OHLC_ADVERSE
        if fallback_count > 0 or aggregate_count == 0
        else OrderingSource.AGG_TRADES
    )


def replay_armed(
    request: ArmedReplayRequest,
    candles: pd.DataFrame,
    aggregate_trade_batches: tuple[AggregateTradeBatch, ...] = (),
) -> ArmedReplayResult:
    """Replay Entry/Invalidated/Missed/Expired in exchange-time order."""

    validate_replay_candles(candles)
    aggregate_by_open = _aggregate_trade_map(candles, aggregate_trade_batches)
    gap_limit = Decimal("0.15") * request.atr_at_confirmation
    fallback_count = 0
    aggregate_count = 0
    for position in range(len(candles)):
        open_time = cast(
            datetime, pd.Timestamp(candles["open_time"].iloc[position]).to_pydatetime()
        )
        if open_time < request.confirmation_close:
            continue
        if open_time >= request.expires_at:
            event = _event(
                request,
                ReplayEventType.EXPIRED,
                request.expires_at,
                request.expires_at.isoformat(timespec="milliseconds"),
                ReplayReason.ENTRY_WINDOW_EXPIRED,
            )
            return ArmedReplayResult(
                request=request,
                state=ArmedState.EXPIRED,
                events=(event,),
                theoretical_entry_price=None,
                entry_time=None,
                entry_candle_open_time=None,
                stop_also_touched_on_entry_candle=False,
                ordering_source=_ordering_source(fallback_count, aggregate_count),
                ohlc_fallback_count=fallback_count,
            )
        batch = aggregate_by_open.get(open_time)
        complete_aggregate = batch is not None and batch.complete
        if not complete_aggregate:
            fallback_count += 1
        else:
            aggregate_count += 1
        open_price = _decimal(candles["open"].iloc[position], "open")
        high = _decimal(candles["high"].iloc[position], "high")
        low = _decimal(candles["low"].iloc[position], "low")
        source_ref = open_time.isoformat(timespec="milliseconds")
        gap_entry = (
            open_price >= request.entry_price
            if request.direction is Direction.LONG
            else open_price <= request.entry_price
        )
        invalidation_touched = (
            low <= request.invalidation_price
            if request.direction is Direction.LONG
            else high >= request.invalidation_price
        )
        entry_touched = (
            high >= request.entry_price
            if request.direction is Direction.LONG
            else low <= request.entry_price
        )
        if gap_entry and abs(open_price - request.entry_price) > gap_limit:
            event = _event(
                request,
                ReplayEventType.MISSED,
                open_time,
                source_ref,
                ReplayReason.ENTRY_GAP_TOO_LARGE,
            )
            return ArmedReplayResult(
                request=request,
                state=ArmedState.MISSED,
                events=(event,),
                theoretical_entry_price=None,
                entry_time=None,
                entry_candle_open_time=None,
                stop_also_touched_on_entry_candle=False,
                ordering_source=_ordering_source(fallback_count, aggregate_count),
                ohlc_fallback_count=fallback_count,
            )
        if complete_aggregate and not gap_entry and batch is not None:
            for trade in sorted(
                batch.trades,
                key=lambda item: (item.trade_time, item.aggregate_trade_id),
            ):
                trade_entry = (
                    trade.price >= request.entry_price
                    if request.direction is Direction.LONG
                    else trade.price <= request.entry_price
                )
                trade_invalidation = (
                    trade.price <= request.invalidation_price
                    if request.direction is Direction.LONG
                    else trade.price >= request.invalidation_price
                )
                aggregate_ref = str(trade.aggregate_trade_id)
                if trade_entry:
                    event = _event(
                        request,
                        ReplayEventType.ENTRY_FILL,
                        trade.trade_time,
                        aggregate_ref,
                        ReplayReason.ENTRY_NORMAL_CROSS,
                        request.entry_price,
                        Decimal(1),
                        "AGG_TRADE",
                    )
                    return ArmedReplayResult(
                        request=request,
                        state=ArmedState.TRIGGERED,
                        events=(event,),
                        theoretical_entry_price=request.entry_price,
                        entry_time=trade.trade_time,
                        entry_candle_open_time=open_time,
                        stop_also_touched_on_entry_candle=False,
                        ordering_source=_ordering_source(fallback_count, aggregate_count),
                        ohlc_fallback_count=fallback_count,
                    )
                if trade_invalidation:
                    event = _event(
                        request,
                        ReplayEventType.INVALIDATED,
                        trade.trade_time,
                        aggregate_ref,
                        ReplayReason.STRUCTURE_INVALIDATED_BEFORE_ENTRY,
                        source="AGG_TRADE",
                    )
                    return ArmedReplayResult(
                        request=request,
                        state=ArmedState.INVALIDATED,
                        events=(event,),
                        theoretical_entry_price=None,
                        entry_time=None,
                        entry_candle_open_time=None,
                        stop_also_touched_on_entry_candle=False,
                        ordering_source=_ordering_source(fallback_count, aggregate_count),
                        ohlc_fallback_count=fallback_count,
                    )
            continue
        if gap_entry or entry_touched:
            fill_price = open_price if gap_entry else request.entry_price
            reason = (
                ReplayReason.ENTRY_AND_INVALIDATION_SAME_CANDLE
                if invalidation_touched
                else ReplayReason.ENTRY_GAP_WITHIN_LIMIT
                if gap_entry
                else ReplayReason.ENTRY_NORMAL_CROSS
            )
            event = _event(
                request,
                ReplayEventType.ENTRY_FILL,
                open_time,
                source_ref,
                reason,
                fill_price,
                Decimal(1),
            )
            return ArmedReplayResult(
                request=request,
                state=ArmedState.TRIGGERED,
                events=(event,),
                theoretical_entry_price=fill_price,
                entry_time=open_time,
                entry_candle_open_time=open_time,
                stop_also_touched_on_entry_candle=invalidation_touched,
                ordering_source=_ordering_source(fallback_count, aggregate_count),
                ohlc_fallback_count=fallback_count,
            )
        if invalidation_touched:
            event = _event(
                request,
                ReplayEventType.INVALIDATED,
                open_time,
                source_ref,
                ReplayReason.STRUCTURE_INVALIDATED_BEFORE_ENTRY,
            )
            return ArmedReplayResult(
                request=request,
                state=ArmedState.INVALIDATED,
                events=(event,),
                theoretical_entry_price=None,
                entry_time=None,
                entry_candle_open_time=None,
                stop_also_touched_on_entry_candle=False,
                ordering_source=_ordering_source(fallback_count, aggregate_count),
                ohlc_fallback_count=fallback_count,
            )

    return ArmedReplayResult(
        request=request,
        state=ArmedState.ARMED,
        events=(),
        theoretical_entry_price=None,
        entry_time=None,
        entry_candle_open_time=None,
        stop_also_touched_on_entry_candle=False,
        ordering_source=_ordering_source(fallback_count, aggregate_count),
        ohlc_fallback_count=fallback_count,
    )


def apply_late_entry_event(
    result: ArmedReplayResult,
    *,
    exchange_time: datetime,
    source: str,
    source_ref: str,
    theoretical_price: Decimal,
) -> ArmedReplayResult:
    """Append one ignored late Entry idempotently without changing a terminal state."""

    terminal_states = {
        ArmedState.INVALIDATED,
        ArmedState.MISSED,
        ArmedState.EXPIRED,
    }
    if result.state not in terminal_states or not result.events:
        raise ReplayInputError("late Entry ignore requires a terminal pre-trade result")
    if exchange_time.tzinfo is None or exchange_time.utcoffset() != timedelta(0):
        raise ReplayInputError("late Entry exchange_time must use UTC")
    if exchange_time < result.events[0].exchange_time:
        raise ReplayInputError("late Entry cannot precede the terminal event")
    if source not in {"KLINE_1M", "AGG_TRADE"} or not source_ref:
        raise ReplayInputError("late Entry source or source_ref is invalid")
    if not theoretical_price.is_finite() or theoretical_price < 0:
        raise ReplayInputError("late Entry theoretical_price must be finite and non-negative")
    event = _event(
        result.request,
        ReplayEventType.LATE_EVENT_IGNORED,
        exchange_time,
        source_ref,
        ReplayReason.LATE_ENTRY_AFTER_TERMINAL,
        theoretical_price,
        source=source,
    )
    if any(existing.event_id == event.event_id for existing in result.events):
        return result
    return ArmedReplayResult(
        request=result.request,
        state=result.state,
        events=(*result.events, event),
        theoretical_entry_price=result.theoretical_entry_price,
        entry_time=result.entry_time,
        entry_candle_open_time=result.entry_candle_open_time,
        stop_also_touched_on_entry_candle=result.stop_also_touched_on_entry_candle,
        ordering_source=result.ordering_source,
        ohlc_fallback_count=result.ohlc_fallback_count,
    )


def _deadline(entry_time: datetime, max_holding_bars: int) -> datetime:
    bucket_open = entry_time.replace(
        minute=(entry_time.minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    return bucket_open + timedelta(minutes=15 * max_holding_bars)


def _closed_events(
    request: TradeReplayRequest,
    event_type: ReplayEventType,
    exchange_time: datetime,
    source_ref: str,
    reason: ReplayReason,
    price: Decimal,
    fraction: Decimal,
    source: str = "KLINE_1M",
) -> tuple[ReplayEvent, ReplayEvent]:
    fill = _event(
        request.armed,
        event_type,
        exchange_time,
        source_ref,
        reason,
        price,
        fraction,
        source,
    )
    closed = _event(
        request.armed,
        ReplayEventType.CLOSED,
        exchange_time,
        source_ref,
        reason,
        price,
        source=source,
    )
    return fill, closed


def _trade_result(
    request: TradeReplayRequest,
    armed_result: ArmedReplayResult,
    position_state: PositionState | None,
    events: list[ReplayEvent],
    open_fraction: Decimal,
    breakeven: Decimal | None,
    activation_time: datetime | None,
    time_exit_deadline: datetime | None,
    exit_reason: TradeExitReason | None,
    exit_time: datetime | None,
    fallback_count: int,
    aggregate_used: bool,
) -> TradeReplayResult:
    ordering = (
        OrderingSource.OHLC_ADVERSE
        if fallback_count > 0 or not aggregate_used
        else OrderingSource.AGG_TRADES
    )
    return TradeReplayResult(
        request=request,
        armed_result=armed_result,
        position_state=position_state,
        events=tuple(events),
        open_fraction=open_fraction,
        breakeven_price=breakeven,
        breakeven_activation_time=activation_time,
        time_exit_deadline=time_exit_deadline,
        exit_reason=exit_reason,
        exit_time=exit_time,
        ordering_source=ordering,
        ohlc_fallback_count=fallback_count,
    )


def _price_touches(
    direction: Direction,
    price: Decimal,
    level: Decimal,
    *,
    favorable: bool,
) -> bool:
    if direction is Direction.LONG:
        return price >= level if favorable else price <= level
    return price <= level if favorable else price >= level


@dataclass(frozen=True)
class _DeadlineFill:
    event_type: ReplayEventType
    reason: ReplayReason
    price: Decimal
    fraction: Decimal
    exchange_time: datetime
    source_ref: str
    source: str


def _deadline_risk_path(
    request: TradeReplayRequest,
    position_state: PositionState,
    breakeven: Decimal,
    breakeven_active: bool,
    open_time: datetime,
    high: Decimal,
    low: Decimal,
    batch: AggregateTradeBatch | None,
) -> tuple[tuple[_DeadlineFill, ...], TradeExitReason] | None:
    direction = request.armed.direction
    if batch is not None and batch.complete:
        fills: list[_DeadlineFill] = []
        simulated_state = position_state
        for trade in sorted(
            batch.trades,
            key=lambda item: (item.trade_time, item.aggregate_trade_id),
        ):
            source_ref = str(trade.aggregate_trade_id)
            if simulated_state is PositionState.FULL:
                if _price_touches(
                    direction,
                    trade.price,
                    request.armed.stop_price,
                    favorable=False,
                ):
                    fills.append(
                        _DeadlineFill(
                            ReplayEventType.STOP_FILL,
                            ReplayReason.INITIAL_STOP,
                            request.armed.stop_price,
                            Decimal(1),
                            trade.trade_time,
                            source_ref,
                            "AGG_TRADE",
                        )
                    )
                    return tuple(fills), TradeExitReason.SL
                if _price_touches(
                    direction, trade.price, request.tp1, favorable=True
                ):
                    fills.append(
                        _DeadlineFill(
                            ReplayEventType.TP1_FILL,
                            ReplayReason.TP1_TARGET,
                            request.tp1,
                            Decimal("0.5"),
                            trade.trade_time,
                            source_ref,
                            "AGG_TRADE",
                        )
                    )
                    simulated_state = PositionState.HALF_AFTER_TP1
                    if _price_touches(
                        direction, trade.price, request.tp2, favorable=True
                    ):
                        fills.append(
                            _DeadlineFill(
                                ReplayEventType.TP2_FILL,
                                ReplayReason.TP2_TARGET,
                                request.tp2,
                                Decimal("0.5"),
                                trade.trade_time,
                                source_ref,
                                "AGG_TRADE",
                            )
                        )
                        return tuple(fills), TradeExitReason.TP2
                    continue
            if simulated_state is PositionState.HALF_AFTER_TP1:
                protective_stop = (
                    breakeven if breakeven_active else request.armed.stop_price
                )
                if _price_touches(
                    direction, trade.price, protective_stop, favorable=False
                ):
                    reason = (
                        ReplayReason.BREAKEVEN_STOP
                        if breakeven_active
                        else ReplayReason.INITIAL_STOP
                    )
                    fills.append(
                        _DeadlineFill(
                            ReplayEventType.STOP_FILL,
                            reason,
                            protective_stop,
                            Decimal("0.5"),
                            trade.trade_time,
                            source_ref,
                            "AGG_TRADE",
                        )
                    )
                    exit_reason = (
                        TradeExitReason.BREAKEVEN_AFTER_TP1
                        if breakeven_active
                        else TradeExitReason.SL
                    )
                    return tuple(fills), exit_reason
                if _price_touches(
                    direction, trade.price, request.tp2, favorable=True
                ):
                    fills.append(
                        _DeadlineFill(
                            ReplayEventType.TP2_FILL,
                            ReplayReason.TP2_TARGET,
                            request.tp2,
                            Decimal("0.5"),
                            trade.trade_time,
                            source_ref,
                            "AGG_TRADE",
                        )
                    )
                    return tuple(fills), TradeExitReason.TP2
        return None

    source_ref = open_time.isoformat(timespec="milliseconds")
    stop_touched = _price_touches(
        direction,
        low if direction is Direction.LONG else high,
        request.armed.stop_price,
        favorable=False,
    )
    tp1_touched = _price_touches(
        direction,
        high if direction is Direction.LONG else low,
        request.tp1,
        favorable=True,
    )
    tp2_touched = _price_touches(
        direction,
        high if direction is Direction.LONG else low,
        request.tp2,
        favorable=True,
    )
    if position_state is PositionState.FULL:
        if stop_touched:
            return (
                (
                    _DeadlineFill(
                        ReplayEventType.STOP_FILL,
                        ReplayReason.INITIAL_STOP,
                        request.armed.stop_price,
                        Decimal(1),
                        open_time,
                        source_ref,
                        "KLINE_1M",
                    ),
                ),
                TradeExitReason.SL,
            )
        if tp1_touched and tp2_touched:
            return (
                (
                    _DeadlineFill(
                        ReplayEventType.TP1_FILL,
                        ReplayReason.TP1_TARGET,
                        request.tp1,
                        Decimal("0.5"),
                        open_time,
                        source_ref,
                        "KLINE_1M",
                    ),
                    _DeadlineFill(
                        ReplayEventType.TP2_FILL,
                        ReplayReason.TP2_TARGET,
                        request.tp2,
                        Decimal("0.5"),
                        open_time,
                        source_ref,
                        "KLINE_1M",
                    ),
                ),
                TradeExitReason.TP2,
            )
        return None
    protective_stop = breakeven if breakeven_active else request.armed.stop_price
    protective_touched = _price_touches(
        direction,
        low if direction is Direction.LONG else high,
        protective_stop,
        favorable=False,
    )
    if protective_touched:
        reason = (
            ReplayReason.BREAKEVEN_STOP
            if breakeven_active
            else ReplayReason.INITIAL_STOP
        )
        exit_reason = (
            TradeExitReason.BREAKEVEN_AFTER_TP1
            if breakeven_active
            else TradeExitReason.SL
        )
        return (
            (
                _DeadlineFill(
                    ReplayEventType.STOP_FILL,
                    reason,
                    protective_stop,
                    Decimal("0.5"),
                    open_time,
                    source_ref,
                    "KLINE_1M",
                ),
            ),
            exit_reason,
        )
    if tp2_touched:
        return (
            (
                _DeadlineFill(
                    ReplayEventType.TP2_FILL,
                    ReplayReason.TP2_TARGET,
                    request.tp2,
                    Decimal("0.5"),
                    open_time,
                    source_ref,
                    "KLINE_1M",
                ),
            ),
            TradeExitReason.TP2,
        )
    return None


def _deadline_exit_cashflow(
    direction: Direction,
    fills: tuple[_DeadlineFill, ...],
    fee_rate: Decimal,
    slippage_rate: Decimal,
) -> Decimal:
    total = Decimal(0)
    for fill in fills:
        if direction is Direction.LONG:
            simulated = fill.price * (Decimal(1) - slippage_rate)
            total += simulated * fill.fraction * (Decimal(1) - fee_rate)
        else:
            simulated = fill.price * (Decimal(1) + slippage_rate)
            total -= simulated * fill.fraction * (Decimal(1) + fee_rate)
    return total


def replay_trade(
    request: TradeReplayRequest,
    candles: pd.DataFrame,
    aggregate_trade_batches: tuple[AggregateTradeBatch, ...] = (),
    *,
    deadline_fee_rate: Decimal = Decimal(0),
    deadline_slippage_rate: Decimal = Decimal(0),
) -> TradeReplayResult:
    """Replay the signal and theoretical post-entry lifecycle without P7.4 costs."""

    armed_result = replay_armed(request.armed, candles, aggregate_trade_batches)
    if armed_result.state is not ArmedState.TRIGGERED:
        return _trade_result(
            request,
            armed_result,
            None,
            list(armed_result.events),
            Decimal(0),
            None,
            None,
            None,
            None,
            None,
            armed_result.ohlc_fallback_count,
            armed_result.ordering_source is OrderingSource.AGG_TRADES,
        )

    entry_price = armed_result.theoretical_entry_price
    entry_time = armed_result.entry_time
    entry_candle_open = armed_result.entry_candle_open_time
    if entry_price is None or entry_time is None or entry_candle_open is None:
        raise ReplayInputError("triggered replay lacks Entry price or time")
    breakeven = request.breakeven_price or entry_price
    breakeven_ordered = (
        request.armed.stop_price < breakeven < request.tp2
        if request.armed.direction is Direction.LONG
        else request.armed.stop_price > breakeven > request.tp2
    )
    if not breakeven_ordered:
        raise ReplayInputError("breakeven price is outside Stop and TP2")

    time_exit_deadline = _deadline(entry_time, request.max_holding_bars)
    position_state = PositionState.FULL
    open_fraction = Decimal(1)
    activation_time: datetime | None = None
    breakeven_active = False
    events = list(armed_result.events)
    aggregate_by_open = _aggregate_trade_map(candles, aggregate_trade_batches)
    if (
        not deadline_fee_rate.is_finite()
        or not deadline_slippage_rate.is_finite()
        or deadline_fee_rate < 0
        or deadline_slippage_rate < 0
        or deadline_fee_rate >= 1
        or deadline_slippage_rate >= 1
    ):
        raise ReplayInputError("deadline cost rates must be finite in [0, 1)")
    fallback_count = armed_result.ohlc_fallback_count
    aggregate_used = armed_result.ordering_source is OrderingSource.AGG_TRADES
    entry_index = next(
        (
            position
            for position in range(len(candles))
            if pd.Timestamp(candles["open_time"].iloc[position]).to_pydatetime()
            == entry_candle_open
        ),
        None,
    )
    if entry_index is None:
        raise ReplayInputError("Entry candle is absent from 1m input")

    for position in range(entry_index, len(candles)):
        open_time = cast(
            datetime, pd.Timestamp(candles["open_time"].iloc[position]).to_pydatetime()
        )
        close_time = cast(
            datetime,
            pd.Timestamp(candles["close_time_exclusive"].iloc[position]).to_pydatetime(),
        )
        source_ref = open_time.isoformat(timespec="milliseconds")
        open_price = _decimal(candles["open"].iloc[position], "open")
        high = _decimal(candles["high"].iloc[position], "high")
        low = _decimal(candles["low"].iloc[position], "low")

        if activation_time is not None and not breakeven_active and open_time >= activation_time:
            events.append(
                _event(
                    request.armed,
                    ReplayEventType.BREAKEVEN_ACTIVATED,
                    activation_time,
                    activation_time.isoformat(timespec="milliseconds"),
                    ReplayReason.BREAKEVEN_NEXT_MINUTE,
                    breakeven,
                )
            )
            breakeven_active = True

        if open_time >= time_exit_deadline:
            batch = aggregate_by_open.get(open_time)
            if batch is not None and batch.complete:
                aggregate_used = True
            else:
                fallback_count += 1
            risk_path = _deadline_risk_path(
                request,
                position_state,
                breakeven,
                breakeven_active,
                open_time,
                high,
                low,
                batch,
            )
            time_fill = _DeadlineFill(
                ReplayEventType.TIME_EXIT,
                ReplayReason.MAX_HOLDING_REACHED,
                open_price,
                open_fraction,
                open_time,
                source_ref,
                "KLINE_1M",
            )
            time_cashflow = _deadline_exit_cashflow(
                request.armed.direction,
                (time_fill,),
                deadline_fee_rate,
                deadline_slippage_rate,
            )
            if risk_path is not None:
                risk_fills, risk_exit_reason = risk_path
                risk_cashflow = _deadline_exit_cashflow(
                    request.armed.direction,
                    risk_fills,
                    deadline_fee_rate,
                    deadline_slippage_rate,
                )
                if risk_cashflow <= time_cashflow:
                    for selected in risk_fills[:-1]:
                        events.append(
                            _event(
                                request.armed,
                                selected.event_type,
                                selected.exchange_time,
                                selected.source_ref,
                                selected.reason,
                                selected.price,
                                selected.fraction,
                                selected.source,
                            )
                        )
                    selected = risk_fills[-1]
                    fill, closed = _closed_events(
                        request,
                        selected.event_type,
                        selected.exchange_time,
                        selected.source_ref,
                        selected.reason,
                        selected.price,
                        selected.fraction,
                        selected.source,
                    )
                    events.extend((fill, closed))
                    return _trade_result(
                        request,
                        armed_result,
                        PositionState.FLAT,
                        events,
                        Decimal(0),
                        breakeven,
                        activation_time,
                        time_exit_deadline,
                        risk_exit_reason,
                        selected.exchange_time,
                        fallback_count,
                        aggregate_used,
                    )
            fill, closed = _closed_events(
                request,
                ReplayEventType.TIME_EXIT,
                open_time,
                source_ref,
                ReplayReason.MAX_HOLDING_REACHED,
                open_price,
                open_fraction,
            )
            events.extend((fill, closed))
            return _trade_result(
                request,
                armed_result,
                PositionState.FLAT,
                events,
                Decimal(0),
                breakeven,
                activation_time,
                time_exit_deadline,
                TradeExitReason.TIME_EXIT,
                open_time,
                fallback_count,
                aggregate_used,
            )

        batch = aggregate_by_open.get(open_time)
        complete_aggregate = batch is not None and batch.complete
        if position > entry_index:
            if complete_aggregate:
                aggregate_used = True
            else:
                fallback_count += 1

        if complete_aggregate and batch is not None:
            ordered_trades = sorted(
                batch.trades,
                key=lambda item: (item.trade_time, item.aggregate_trade_id),
            )
            if position == entry_index and events[0].source == "AGG_TRADE":
                entry_key = (entry_time, int(events[0].source_ref))
                ordered_trades = [
                    trade
                    for trade in ordered_trades
                    if (trade.trade_time, trade.aggregate_trade_id) > entry_key
                ]
            elif position == entry_index:
                ordered_trades = [
                    trade for trade in ordered_trades if trade.trade_time >= entry_time
                ]

            for trade in ordered_trades:
                aggregate_ref = str(trade.aggregate_trade_id)
                if position_state is PositionState.FULL:
                    if _price_touches(
                        request.armed.direction,
                        trade.price,
                        request.armed.stop_price,
                        favorable=False,
                    ):
                        fill, closed = _closed_events(
                            request,
                            ReplayEventType.STOP_FILL,
                            trade.trade_time,
                            aggregate_ref,
                            ReplayReason.INITIAL_STOP,
                            request.armed.stop_price,
                            Decimal(1),
                            "AGG_TRADE",
                        )
                        events.extend((fill, closed))
                        return _trade_result(
                            request,
                            armed_result,
                            PositionState.FLAT,
                            events,
                            Decimal(0),
                            breakeven,
                            None,
                            time_exit_deadline,
                            TradeExitReason.SL,
                            trade.trade_time,
                            fallback_count,
                            aggregate_used,
                        )
                    if _price_touches(
                        request.armed.direction,
                        trade.price,
                        request.tp1,
                        favorable=True,
                    ):
                        events.append(
                            _event(
                                request.armed,
                                ReplayEventType.TP1_FILL,
                                trade.trade_time,
                                aggregate_ref,
                                ReplayReason.TP1_TARGET,
                                request.tp1,
                                Decimal("0.5"),
                                "AGG_TRADE",
                            )
                        )
                        position_state = PositionState.HALF_AFTER_TP1
                        open_fraction = Decimal("0.5")
                        activation_time = close_time
                        if _price_touches(
                            request.armed.direction,
                            trade.price,
                            request.tp2,
                            favorable=True,
                        ):
                            fill, closed = _closed_events(
                                request,
                                ReplayEventType.TP2_FILL,
                                trade.trade_time,
                                aggregate_ref,
                                ReplayReason.TP2_TARGET,
                                request.tp2,
                                Decimal("0.5"),
                                "AGG_TRADE",
                            )
                            events.extend((fill, closed))
                            return _trade_result(
                                request,
                                armed_result,
                                PositionState.FLAT,
                                events,
                                Decimal(0),
                                breakeven,
                                None,
                                time_exit_deadline,
                                TradeExitReason.TP2,
                                trade.trade_time,
                                fallback_count,
                                aggregate_used,
                            )
                        continue

                if position_state is PositionState.HALF_AFTER_TP1:
                    protective_stop = (
                        breakeven if breakeven_active else request.armed.stop_price
                    )
                    if _price_touches(
                        request.armed.direction,
                        trade.price,
                        protective_stop,
                        favorable=False,
                    ):
                        reason = (
                            ReplayReason.BREAKEVEN_STOP
                            if breakeven_active
                            else ReplayReason.INITIAL_STOP
                        )
                        exit_reason = (
                            TradeExitReason.BREAKEVEN_AFTER_TP1
                            if breakeven_active
                            else TradeExitReason.SL
                        )
                        fill, closed = _closed_events(
                            request,
                            ReplayEventType.STOP_FILL,
                            trade.trade_time,
                            aggregate_ref,
                            reason,
                            protective_stop,
                            Decimal("0.5"),
                            "AGG_TRADE",
                        )
                        events.extend((fill, closed))
                        return _trade_result(
                            request,
                            armed_result,
                            PositionState.FLAT,
                            events,
                            Decimal(0),
                            breakeven,
                            activation_time,
                            time_exit_deadline,
                            exit_reason,
                            trade.trade_time,
                            fallback_count,
                            aggregate_used,
                        )
                    if _price_touches(
                        request.armed.direction,
                        trade.price,
                        request.tp2,
                        favorable=True,
                    ):
                        fill, closed = _closed_events(
                            request,
                            ReplayEventType.TP2_FILL,
                            trade.trade_time,
                            aggregate_ref,
                            ReplayReason.TP2_TARGET,
                            request.tp2,
                            Decimal("0.5"),
                            "AGG_TRADE",
                        )
                        events.extend((fill, closed))
                        return _trade_result(
                            request,
                            armed_result,
                            PositionState.FLAT,
                            events,
                            Decimal(0),
                            breakeven,
                            activation_time,
                            time_exit_deadline,
                            TradeExitReason.TP2,
                            trade.trade_time,
                            fallback_count,
                            aggregate_used,
                        )
            continue

        stop_touched = (
            low <= request.armed.stop_price
            if request.armed.direction is Direction.LONG
            else high >= request.armed.stop_price
        )
        tp1_touched = (
            high >= request.tp1
            if request.armed.direction is Direction.LONG
            else low <= request.tp1
        )
        tp2_touched = (
            high >= request.tp2
            if request.armed.direction is Direction.LONG
            else low <= request.tp2
        )

        if position_state is PositionState.FULL:
            force_entry_candle_stop = (
                position == entry_index
                and armed_result.stop_also_touched_on_entry_candle
            )
            if stop_touched or force_entry_candle_stop:
                fill, closed = _closed_events(
                    request,
                    ReplayEventType.STOP_FILL,
                    open_time,
                    source_ref,
                    ReplayReason.INITIAL_STOP,
                    request.armed.stop_price,
                    Decimal(1),
                )
                events.extend((fill, closed))
                return _trade_result(
                    request,
                    armed_result,
                    PositionState.FLAT,
                    events,
                    Decimal(0),
                    breakeven,
                    None,
                    time_exit_deadline,
                    TradeExitReason.SL,
                    open_time,
                    fallback_count,
                    aggregate_used,
                )
            if tp1_touched:
                events.append(
                    _event(
                        request.armed,
                        ReplayEventType.TP1_FILL,
                        open_time,
                        source_ref,
                        ReplayReason.TP1_TARGET,
                        request.tp1,
                        Decimal("0.5"),
                    )
                )
                open_fraction = Decimal("0.5")
                if tp2_touched:
                    fill, closed = _closed_events(
                        request,
                        ReplayEventType.TP2_FILL,
                        open_time,
                        source_ref,
                        ReplayReason.TP2_TARGET,
                        request.tp2,
                        Decimal("0.5"),
                    )
                    events.extend((fill, closed))
                    return _trade_result(
                        request,
                        armed_result,
                        PositionState.FLAT,
                        events,
                        Decimal(0),
                        breakeven,
                        None,
                        time_exit_deadline,
                        TradeExitReason.TP2,
                        open_time,
                        fallback_count,
                        aggregate_used,
                    )
                position_state = PositionState.HALF_AFTER_TP1
                activation_time = close_time
                continue

        if position_state is PositionState.HALF_AFTER_TP1:
            breakeven_touched = breakeven_active and (
                low <= breakeven
                if request.armed.direction is Direction.LONG
                else high >= breakeven
            )
            if breakeven_touched:
                fill, closed = _closed_events(
                    request,
                    ReplayEventType.STOP_FILL,
                    open_time,
                    source_ref,
                    ReplayReason.BREAKEVEN_STOP,
                    breakeven,
                    Decimal("0.5"),
                )
                events.extend((fill, closed))
                return _trade_result(
                    request,
                    armed_result,
                    PositionState.FLAT,
                    events,
                    Decimal(0),
                    breakeven,
                    activation_time,
                    time_exit_deadline,
                    TradeExitReason.BREAKEVEN_AFTER_TP1,
                    open_time,
                    fallback_count,
                    aggregate_used,
                )
            if tp2_touched:
                fill, closed = _closed_events(
                    request,
                    ReplayEventType.TP2_FILL,
                    open_time,
                    source_ref,
                    ReplayReason.TP2_TARGET,
                    request.tp2,
                    Decimal("0.5"),
                )
                events.extend((fill, closed))
                return _trade_result(
                    request,
                    armed_result,
                    PositionState.FLAT,
                    events,
                    Decimal(0),
                    breakeven,
                    activation_time,
                    time_exit_deadline,
                    TradeExitReason.TP2,
                    open_time,
                    fallback_count,
                    aggregate_used,
                )

    return _trade_result(
        request,
        armed_result,
        position_state,
        events,
        open_fraction,
        breakeven,
        activation_time,
        time_exit_deadline,
        None,
        None,
        fallback_count,
        aggregate_used,
    )
