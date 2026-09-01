"""Deterministic Decimal trade-plan calculations."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Self, cast

import pandas as pd
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from slagalpha.domain.symbols import normalize_symbol
from slagalpha.strategy.pivots import PivotType, PivotZone
from slagalpha.strategy.setup import DailyContext, Direction, PullbackState
from slagalpha.strategy.triggers import TriggerDecision, TriggerType

TRADE_PLAN_VERSION = "trade-plan/0.1.0"
ALLOWED_STOP_ATR_MULTIPLIERS = frozenset(
    {Decimal("0.10"), Decimal("0.15"), Decimal("0.20")}
)
ALLOWED_ENTRY_TTL_BARS = frozenset({2, 4, 6})


class PlanError(ValueError):
    """Base class for price-plan failures."""


class PlanInputError(PlanError):
    """Raised when a plan request is internally invalid or unsupported."""


class PlanReason(StrEnum):
    """Stable Entry/Stop plan reason codes."""

    TICK_SIZE_INVALID = "TICK_SIZE_INVALID"
    ATR_INVALID = "ATR_INVALID"
    PRICE_RULE_INVALID = "PRICE_RULE_INVALID"
    STOP_SIDE_INVALID = "STOP_SIDE_INVALID"
    STOP_DISTANCE_INVALID = "STOP_DISTANCE_INVALID"
    ENTRY_STOP_ACCEPTED = "ENTRY_STOP_ACCEPTED"
    OBSTACLE_LT_1R = "OBSTACLE_LT_1R"
    TP_PLAN_INVALID = "TP_PLAN_INVALID"
    TAKE_PROFIT_ACCEPTED = "TAKE_PROFIT_ACCEPTED"


class RoundingDirection(StrEnum):
    """Auditable price-grid rounding direction."""

    FLOOR = "FLOOR"
    CEILING = "CEILING"


class TargetSource(StrEnum):
    """Frozen take-profit source labels."""

    STRUCTURE_15M = "STRUCTURE_15M"
    STRUCTURE_1H = "STRUCTURE_1H"
    ATR_EXTENSION = "ATR_EXTENSION"


class OneHourClosePosition(StrEnum):
    """Frozen 1H close-quality buckets for scoring."""

    TREND_SIDE_SMA30 = "TREND_SIDE_SMA30"
    BETWEEN_SMA30_SMA60 = "BETWEEN_SMA30_SMA60"
    DEEP_VALID = "DEEP_VALID"


def _decimal_from_value(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise PlanInputError(f"{name} must be a decimal number")
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise PlanInputError(f"{name} must be a decimal number") from error
    return converted


class EntryStopRequest(BaseModel):
    """Frozen P5 and market-rule evidence required for Entry/Stop."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=True)

    logical_signal_id: str
    symbol: str
    direction: Direction
    primary_trigger: TriggerType
    confirmation_high: Decimal
    confirmation_low: Decimal
    invalidation_price: Decimal
    atr_at_confirmation: Decimal
    tick_size: Decimal
    confirmation_close: datetime

    @field_validator("logical_signal_id")
    @classmethod
    def validate_signal_id(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("logical_signal_id must be a lowercase SHA-256 value")
        return value

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return normalize_symbol(value)

    @field_validator("confirmation_close")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("confirmation_close must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_direction(self) -> Self:
        if self.direction is Direction.NEUTRAL:
            raise ValueError("price-plan direction must be LONG or SHORT")
        return self

    @classmethod
    def create(
        cls,
        *,
        logical_signal_id: str,
        symbol: str,
        direction: Direction,
        primary_trigger: TriggerType,
        confirmation_high: Decimal | float | str,
        confirmation_low: Decimal | float | str,
        invalidation_price: Decimal | float | str,
        atr_at_confirmation: Decimal | float | str,
        tick_size: Decimal | float | str,
        confirmation_close: datetime,
    ) -> Self:
        """Convert external numeric values through their canonical decimal strings."""

        return cls(
            logical_signal_id=logical_signal_id,
            symbol=symbol,
            direction=direction,
            primary_trigger=primary_trigger,
            confirmation_high=_decimal_from_value(confirmation_high, "confirmation_high"),
            confirmation_low=_decimal_from_value(confirmation_low, "confirmation_low"),
            invalidation_price=_decimal_from_value(invalidation_price, "invalidation_price"),
            atr_at_confirmation=_decimal_from_value(
                atr_at_confirmation, "atr_at_confirmation"
            ),
            tick_size=_decimal_from_value(tick_size, "tick_size"),
            confirmation_close=confirmation_close,
        )

    @classmethod
    def from_trigger_decision(
        cls,
        decision: TriggerDecision,
        *,
        confirmation_high: Decimal | float | str,
        confirmation_low: Decimal | float | str,
        atr_at_confirmation: Decimal | float | str,
        tick_size: Decimal | float | str,
    ) -> Self:
        """Create a request while selecting the primary trigger's invalidation point."""

        if (
            not decision.eligible_for_plan
            or decision.logical_signal_id is None
            or decision.primary_trigger is None
        ):
            raise PlanInputError("TriggerDecision is not eligible for a price plan")
        if decision.primary_trigger is TriggerType.MA_RECLAIM:
            invalidation = decision.trigger_a.invalidation_price
        else:
            if decision.trigger_b is None:
                raise PlanInputError("primary Sweep trigger evaluation is missing")
            invalidation = decision.trigger_b.invalidation_price
        if invalidation is None:
            raise PlanInputError("primary trigger invalidation price is missing")
        return cls.create(
            logical_signal_id=decision.logical_signal_id,
            symbol=decision.symbol,
            direction=decision.direction,
            primary_trigger=decision.primary_trigger,
            confirmation_high=confirmation_high,
            confirmation_low=confirmation_low,
            invalidation_price=invalidation,
            atr_at_confirmation=atr_at_confirmation,
            tick_size=tick_size,
            confirmation_close=decision.confirmation_close_time,
        )


class EntryStopEvaluation(BaseModel):
    """Accepted or rejected deterministic Entry/Stop calculation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = TRADE_PLAN_VERSION
    request: EntryStopRequest
    stop_atr_multiplier: Decimal
    entry_ttl_bars: int
    expires_at: datetime
    accepted: bool
    reason_codes: tuple[PlanReason, ...]
    raw_entry: Decimal | None
    entry_price: Decimal | None
    entry_rounding: RoundingDirection | None
    stop_buffer: Decimal | None
    raw_stop: Decimal | None
    stop_price: Decimal | None
    stop_rounding: RoundingDirection | None
    risk_per_unit: Decimal | None
    normalized_risk: Decimal | None


class TargetCandidate(BaseModel):
    """One confirmed, profitable, conservatively rounded structure target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    zone_id: str
    source: TargetSource
    confirmed_at: datetime
    raw_price: Decimal
    target_price: Decimal
    distance: Decimal
    gross_rr: Decimal


class TakeProfitEvaluation(BaseModel):
    """Accepted or rejected TP1/TP2 selection over an accepted Entry/Stop."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = TRADE_PLAN_VERSION
    entry_stop: EntryStopEvaluation
    accepted: bool
    reason_codes: tuple[PlanReason, ...]
    candidates: tuple[TargetCandidate, ...]
    tp1: Decimal | None
    tp1_source: TargetSource | None
    tp1_zone_id: str | None
    gross_rr_tp1: Decimal | None
    tp2: Decimal | None
    tp2_source: TargetSource | None
    tp2_zone_id: str | None
    gross_rr_tp2: Decimal | None


class ScoreInput(BaseModel):
    """Already-gated evidence consumed by the ranking-only Score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    direction: Direction
    four_hour_direction_valid: bool
    band_width: Decimal
    normalized_slope: Decimal
    daily_context: DailyContext
    pullback_state: PullbackState
    one_hour_close_position: OneHourClosePosition
    one_hour_volume_contracting: bool
    primary_trigger: TriggerType
    trigger_pullback_contracting: bool
    sweep_depth_atr: Decimal | None
    sweep_zone_reclaimed: bool
    trigger_volume_ratio: Decimal
    trigger_volume_expanding: bool
    close_location: Decimal
    strict_structure_reclaim: bool
    take_profit: TakeProfitEvaluation


class ScoreBreakdown(BaseModel):
    """Fixed five-component integer ranking score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    four_hour: int
    daily: int
    one_hour: int
    fifteen_minute: int
    risk_reward: int
    score_total: int


def floor_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    """Round a Decimal price down to any positive tick grid."""

    if not price.is_finite() or not tick_size.is_finite() or tick_size <= 0:
        raise PlanInputError("price and tick_size must be finite with tick_size greater than zero")
    units = (price / tick_size).to_integral_value(rounding=ROUND_FLOOR)
    return units * tick_size


def ceil_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    """Round a Decimal price up to any positive tick grid."""

    if not price.is_finite() or not tick_size.is_finite() or tick_size <= 0:
        raise PlanInputError("price and tick_size must be finite with tick_size greater than zero")
    units = (price / tick_size).to_integral_value(rounding=ROUND_CEILING)
    return units * tick_size


def _rejected_entry_stop(
    request: EntryStopRequest,
    multiplier: Decimal,
    ttl_bars: int,
    reasons: tuple[PlanReason, ...],
) -> EntryStopEvaluation:
    return EntryStopEvaluation(
        request=request,
        stop_atr_multiplier=multiplier,
        entry_ttl_bars=ttl_bars,
        expires_at=request.confirmation_close + timedelta(minutes=15 * ttl_bars),
        accepted=False,
        reason_codes=reasons,
        raw_entry=None,
        entry_price=None,
        entry_rounding=None,
        stop_buffer=None,
        raw_stop=None,
        stop_price=None,
        stop_rounding=None,
        risk_per_unit=None,
        normalized_risk=None,
    )


def build_entry_stop(
    request: EntryStopRequest,
    *,
    stop_atr_multiplier: Decimal | float | str = Decimal("0.15"),
    entry_ttl_bars: int = 4,
) -> EntryStopEvaluation:
    """Build Entry/Stop with conservative tick rounding and frozen ATR gates."""

    multiplier = _decimal_from_value(stop_atr_multiplier, "stop_atr_multiplier")
    if multiplier not in ALLOWED_STOP_ATR_MULTIPLIERS:
        raise PlanInputError("stop_atr_multiplier must be one of 0.10, 0.15, or 0.20")
    if entry_ttl_bars not in ALLOWED_ENTRY_TTL_BARS:
        raise PlanInputError("entry_ttl_bars must be one of 2, 4, or 6")

    reasons: list[PlanReason] = []
    tick = request.tick_size
    atr = request.atr_at_confirmation
    prices = (
        request.confirmation_high,
        request.confirmation_low,
        request.invalidation_price,
    )
    if not tick.is_finite() or tick <= 0:
        reasons.append(PlanReason.TICK_SIZE_INVALID)
    if not atr.is_finite() or atr <= 0:
        reasons.append(PlanReason.ATR_INVALID)
    if (
        any(not price.is_finite() or price < 0 for price in prices)
        or request.confirmation_high < request.confirmation_low
    ):
        reasons.append(PlanReason.PRICE_RULE_INVALID)
    if reasons:
        return _rejected_entry_stop(
            request, multiplier, entry_ttl_bars, tuple(reasons)
        )

    if request.direction is Direction.LONG:
        raw_entry = request.confirmation_high + tick
        entry_price = ceil_to_tick(raw_entry, tick)
        entry_rounding = RoundingDirection.CEILING
    else:
        raw_entry = request.confirmation_low - tick
        entry_price = floor_to_tick(raw_entry, tick)
        entry_rounding = RoundingDirection.FLOOR
    buffer = max(multiplier * atr, Decimal(2) * tick)
    if request.direction is Direction.LONG:
        raw_stop = request.invalidation_price - buffer
        stop_price = floor_to_tick(raw_stop, tick)
        stop_rounding = RoundingDirection.FLOOR
    else:
        raw_stop = request.invalidation_price + buffer
        stop_price = ceil_to_tick(raw_stop, tick)
        stop_rounding = RoundingDirection.CEILING

    entry_breaks_confirmation = (
        entry_price > request.confirmation_high
        if request.direction is Direction.LONG
        else entry_price < request.confirmation_low
    )
    if entry_price < 0 or stop_price < 0 or not entry_breaks_confirmation:
        reasons.append(PlanReason.PRICE_RULE_INVALID)
    stop_on_correct_side = (
        stop_price < entry_price
        if request.direction is Direction.LONG
        else stop_price > entry_price
    )
    if not stop_on_correct_side:
        reasons.append(PlanReason.STOP_SIDE_INVALID)
    risk = abs(entry_price - stop_price)
    normalized_risk = risk / atr
    if risk < Decimal("0.5") * atr or risk > Decimal("2.0") * atr:
        reasons.append(PlanReason.STOP_DISTANCE_INVALID)

    accepted = not reasons
    if accepted:
        reasons.append(PlanReason.ENTRY_STOP_ACCEPTED)
    return EntryStopEvaluation(
        request=request,
        stop_atr_multiplier=multiplier,
        entry_ttl_bars=entry_ttl_bars,
        expires_at=request.confirmation_close + timedelta(minutes=15 * entry_ttl_bars),
        accepted=accepted,
        reason_codes=tuple(reasons),
        raw_entry=raw_entry,
        entry_price=entry_price,
        entry_rounding=entry_rounding,
        stop_buffer=buffer,
        raw_stop=raw_stop,
        stop_price=stop_price,
        stop_rounding=stop_rounding,
        risk_per_unit=risk,
        normalized_risk=normalized_risk,
    )


def _lookback_start(
    candles: pd.DataFrame,
    required_rows: int,
    confirmation_close: datetime,
    name: str,
) -> datetime:
    required = {"open_time", "close_time_exclusive", "is_closed"}
    missing = sorted(required.difference(candles.columns))
    if missing:
        raise PlanInputError(f"{name} missing required columns: {missing}")
    if len(candles) < required_rows:
        raise PlanInputError(f"{name} requires at least {required_rows} closed candles")
    open_times = pd.DatetimeIndex(candles["open_time"])
    close_times = pd.DatetimeIndex(candles["close_time_exclusive"])
    if open_times.tz is None or close_times.tz is None:
        raise PlanInputError(f"{name} timestamps must be timezone-aware")
    if not open_times.is_monotonic_increasing or open_times.has_duplicates:
        raise PlanInputError(f"{name} open_time must be unique and strictly increasing")
    if not bool(candles["is_closed"].astype(bool).all()):
        raise PlanInputError(f"{name} candles must be closed")
    visible = close_times <= pd.Timestamp(confirmation_close)
    if int(visible.sum()) < required_rows:
        raise PlanInputError(f"{name} lacks {required_rows} candles visible at confirmation")
    visible_open_times = open_times[visible]
    return cast(datetime, visible_open_times[-required_rows].to_pydatetime())


def _target_candidates(
    entry_stop: EntryStopEvaluation,
    fifteen_minute_candles: pd.DataFrame,
    one_hour_candles: pd.DataFrame,
    fifteen_minute_zones: tuple[PivotZone, ...],
    one_hour_zones: tuple[PivotZone, ...],
) -> tuple[TargetCandidate, ...]:
    request = entry_stop.request
    entry = entry_stop.entry_price
    risk = entry_stop.risk_per_unit
    if entry is None or risk is None:
        raise PlanInputError("accepted Entry/Stop must contain entry and risk")
    cutoff_15m = _lookback_start(
        fifteen_minute_candles, 96, request.confirmation_close, "15m target input"
    )
    cutoff_1h = _lookback_start(
        one_hour_candles, 60, request.confirmation_close, "1H target input"
    )
    required_kind = PivotType.HIGH if request.direction is Direction.LONG else PivotType.LOW
    candidates: list[TargetCandidate] = []
    for source, cutoff, zones in (
        (TargetSource.STRUCTURE_15M, cutoff_15m, fifteen_minute_zones),
        (TargetSource.STRUCTURE_1H, cutoff_1h, one_hour_zones),
    ):
        expected_interval = "15m" if source is TargetSource.STRUCTURE_15M else "1h"
        for zone in zones:
            if (
                zone.interval.lower() != expected_interval
                or zone.kind is not required_kind
                or zone.confirmed_at > request.confirmation_close
                or zone.members[-1].pivot_time < cutoff
            ):
                continue
            if request.direction is Direction.LONG:
                raw_price = _decimal_from_value(zone.lower, "zone lower") - request.tick_size
                target_price = floor_to_tick(raw_price, request.tick_size)
                distance = target_price - entry
            else:
                raw_price = _decimal_from_value(zone.upper, "zone upper") + request.tick_size
                target_price = ceil_to_tick(raw_price, request.tick_size)
                distance = entry - target_price
            if distance <= 0:
                continue
            candidates.append(
                TargetCandidate(
                    zone_id=zone.zone_id,
                    source=source,
                    confirmed_at=zone.confirmed_at,
                    raw_price=raw_price,
                    target_price=target_price,
                    distance=distance,
                    gross_rr=distance / risk,
                )
            )
    candidates.sort(key=lambda candidate: candidate.zone_id)
    candidates.sort(
        key=lambda candidate: 0 if candidate.source is TargetSource.STRUCTURE_15M else 1
    )
    candidates.sort(key=lambda candidate: candidate.confirmed_at, reverse=True)
    candidates.sort(key=lambda candidate: candidate.distance)
    return tuple(candidates)


def _extension_target(
    entry: Decimal,
    distance: Decimal,
    direction: Direction,
    tick: Decimal,
) -> Decimal:
    if direction is Direction.LONG:
        return floor_to_tick(entry + distance, tick)
    return ceil_to_tick(entry - distance, tick)


def build_take_profit(
    entry_stop: EntryStopEvaluation,
    *,
    fifteen_minute_candles: pd.DataFrame,
    one_hour_candles: pd.DataFrame,
    fifteen_minute_zones: tuple[PivotZone, ...] = (),
    one_hour_zones: tuple[PivotZone, ...] = (),
) -> TakeProfitEvaluation:
    """Select structure targets or conservative extensions under frozen R gates."""

    if not entry_stop.accepted:
        raise PlanInputError("Take Profit requires an accepted Entry/Stop")
    entry = entry_stop.entry_price
    risk = entry_stop.risk_per_unit
    if entry is None or risk is None or risk <= 0:
        raise PlanInputError("accepted Entry/Stop must contain positive risk")
    request = entry_stop.request
    candidates = _target_candidates(
        entry_stop,
        fifteen_minute_candles,
        one_hour_candles,
        fifteen_minute_zones,
        one_hour_zones,
    )
    if candidates and candidates[0].distance < risk:
        return TakeProfitEvaluation(
            entry_stop=entry_stop,
            accepted=False,
            reason_codes=(PlanReason.OBSTACLE_LT_1R,),
            candidates=candidates,
            tp1=None,
            tp1_source=None,
            tp1_zone_id=None,
            gross_rr_tp1=None,
            tp2=None,
            tp2_source=None,
            tp2_zone_id=None,
            gross_rr_tp2=None,
        )

    tp1_candidate = next(
        (candidate for candidate in candidates if candidate.distance >= risk), None
    )
    if tp1_candidate is not None:
        tp1 = tp1_candidate.target_price
        tp1_source = tp1_candidate.source
        tp1_zone_id = tp1_candidate.zone_id
        tp1_distance = tp1_candidate.distance
    else:
        tp1_distance_request = max(risk, request.atr_at_confirmation)
        tp1 = _extension_target(entry, tp1_distance_request, request.direction, request.tick_size)
        tp1_source = TargetSource.ATR_EXTENSION
        tp1_zone_id = None
        tp1_distance = abs(tp1 - entry)

    tp2_candidate = next(
        (
            candidate
            for candidate in candidates
            if candidate.zone_id != tp1_zone_id
            and candidate.distance >= Decimal(2) * risk
            and candidate.distance > tp1_distance
        ),
        None,
    )
    if tp2_candidate is not None:
        tp2 = tp2_candidate.target_price
        tp2_source = tp2_candidate.source
        tp2_zone_id = tp2_candidate.zone_id
        tp2_distance = tp2_candidate.distance
    else:
        tp2_distance_request = max(Decimal(2) * risk, Decimal(2) * request.atr_at_confirmation)
        tp2 = _extension_target(entry, tp2_distance_request, request.direction, request.tick_size)
        tp2_source = TargetSource.ATR_EXTENSION
        tp2_zone_id = None
        tp2_distance = abs(tp2 - entry)

    valid = (
        tp1_distance >= risk
        and tp2_distance >= Decimal(2) * risk
        and tp2_distance > tp1_distance
    )
    reason = PlanReason.TAKE_PROFIT_ACCEPTED if valid else PlanReason.TP_PLAN_INVALID
    return TakeProfitEvaluation(
        entry_stop=entry_stop,
        accepted=valid,
        reason_codes=(reason,),
        candidates=candidates,
        tp1=tp1,
        tp1_source=tp1_source,
        tp1_zone_id=tp1_zone_id,
        gross_rr_tp1=tp1_distance / risk,
        tp2=tp2,
        tp2_source=tp2_source,
        tp2_zone_id=tp2_zone_id,
        gross_rr_tp2=tp2_distance / risk,
    )


def score_trade_plan(evidence: ScoreInput) -> ScoreBreakdown:
    """Score an accepted plan for ranking without introducing a rejection threshold."""

    if (
        evidence.direction is Direction.NEUTRAL
        or evidence.direction is not evidence.take_profit.entry_stop.request.direction
        or evidence.primary_trigger is not evidence.take_profit.entry_stop.request.primary_trigger
        or not evidence.four_hour_direction_valid
        or evidence.daily_context in (DailyContext.BLOCK_LONG, DailyContext.BLOCK_SHORT)
        or evidence.pullback_state not in (PullbackState.SHALLOW, PullbackState.STANDARD)
        or not evidence.take_profit.accepted
    ):
        raise PlanInputError("Score requires a plan that passed every mandatory Gate")
    if (
        evidence.band_width < 0
        or evidence.normalized_slope <= Decimal("0.10")
        or evidence.trigger_volume_ratio <= 1
        or not evidence.trigger_volume_expanding
        or not evidence.strict_structure_reclaim
    ):
        raise PlanInputError("Score evidence contradicts mandatory trigger conditions")
    basic_close_passes = (
        evidence.close_location >= Decimal("0.65")
        if evidence.direction is Direction.LONG
        else evidence.close_location <= Decimal("0.35")
    )
    if not basic_close_passes:
        raise PlanInputError("Score close_location did not pass the trigger Gate")
    if evidence.primary_trigger is TriggerType.SWEEP_RECLAIM and (
        evidence.sweep_depth_atr is None or not evidence.sweep_zone_reclaimed
    ):
        raise PlanInputError("Sweep score evidence is incomplete")

    four_hour = 15
    if evidence.band_width >= Decimal("1.50"):
        four_hour += 5
    elif evidence.band_width >= Decimal("1.00"):
        four_hour += 4
    elif evidence.band_width >= Decimal("0.75"):
        four_hour += 3
    if evidence.normalized_slope >= Decimal("0.50"):
        four_hour += 5
    elif evidence.normalized_slope >= Decimal("0.25"):
        four_hour += 4
    elif evidence.normalized_slope > Decimal("0.10"):
        four_hour += 3

    daily = 15 if evidence.daily_context is DailyContext.ALIGNED else 7
    one_hour = 12 if evidence.pullback_state is PullbackState.SHALLOW else 10
    one_hour += {
        OneHourClosePosition.TREND_SIDE_SMA30: 8,
        OneHourClosePosition.BETWEEN_SMA30_SMA60: 6,
        OneHourClosePosition.DEEP_VALID: 4,
    }[evidence.one_hour_close_position]
    if evidence.one_hour_volume_contracting:
        one_hour += 5

    fifteen_minute = 0
    if evidence.primary_trigger is TriggerType.MA_RECLAIM:
        if evidence.trigger_pullback_contracting:
            fifteen_minute += 5
    elif (
        evidence.sweep_depth_atr is not None
        and evidence.sweep_depth_atr <= Decimal("0.50")
        and evidence.sweep_zone_reclaimed
    ):
        fifteen_minute += 5
    if evidence.trigger_volume_ratio >= Decimal("1.50"):
        fifteen_minute += 5
    elif evidence.trigger_volume_ratio > 1:
        fifteen_minute += 3
    fifteen_minute += 2
    strong_close = (
        evidence.close_location >= Decimal("0.80")
        if evidence.direction is Direction.LONG
        else evidence.close_location <= Decimal("0.20")
    )
    fifteen_minute += 4 if strong_close else 2
    fifteen_minute += 4

    normalized_risk = evidence.take_profit.entry_stop.normalized_risk
    if normalized_risk is None:
        raise PlanInputError("accepted plan lacks normalized risk")
    risk_reward = 5 if Decimal("0.75") <= normalized_risk <= Decimal("1.25") else 3
    risk_reward += 4 if evidence.take_profit.tp1_source is not TargetSource.ATR_EXTENSION else 2
    risk_reward += 4 if evidence.take_profit.tp2_source is not TargetSource.ATR_EXTENSION else 2
    gross_rr_tp2 = evidence.take_profit.gross_rr_tp2
    if gross_rr_tp2 is None:
        raise PlanInputError("accepted plan lacks TP2 gross RR")
    if gross_rr_tp2 >= 3:
        risk_reward += 2

    total = four_hour + daily + one_hour + fifteen_minute + risk_reward
    return ScoreBreakdown(
        four_hour=four_hour,
        daily=daily,
        one_hour=one_hour,
        fifteen_minute=fifteen_minute,
        risk_reward=risk_reward,
        score_total=total,
    )
