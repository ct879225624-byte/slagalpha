"""Deterministic multi-timeframe setup context primitives."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from enum import StrEnum
from typing import cast

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from slagalpha.domain.symbols import normalize_symbol
from slagalpha.strategy.indicators import IndicatorInputError, validate_indicator_input

SETUP_CONTEXT_VERSION = "setup-context/0.1.0"
MA_COLUMNS = ("sma30", "sma60", "sma90", "sma180")
ALLOWED_COMPRESSION_THRESHOLDS = frozenset({0.5, 0.75, 1.0})


class SetupError(ValueError):
    """Base class for setup evaluation failures."""


class SetupInputError(SetupError):
    """Raised when setup input is invalid."""


class SetupNotReadyError(SetupError):
    """Raised when valid input has insufficient warmed-up history."""


class Direction(StrEnum):
    """Frozen 4H directional state."""

    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class MaOrder(StrEnum):
    """Strict moving-average ordering for one candle."""

    BULL_ORDER = "BULL_ORDER"
    BEAR_ORDER = "BEAR_ORDER"
    MIXED_ORDER = "MIXED_ORDER"


class BandPosition(StrEnum):
    """Close location relative to the complete moving-average band."""

    ABOVE_BAND = "ABOVE_BAND"
    BELOW_BAND = "BELOW_BAND"
    INSIDE_BAND = "INSIDE_BAND"


class Regime(StrEnum):
    """Frozen 4H trend/compression regime."""

    TRENDING = "TRENDING"
    COMPRESSED = "COMPRESSED"


class DailyContext(StrEnum):
    """1D context relative to the 4H direction."""

    ALIGNED = "ALIGNED"
    MIXED = "MIXED"
    BLOCK_LONG = "BLOCK_LONG"
    BLOCK_SHORT = "BLOCK_SHORT"


class CompressionReason(StrEnum):
    """Stable reason codes for a compressed 4H regime."""

    MA_BAND_NARROW = "MA_BAND_NARROW"
    MA180_FLAT = "MA180_FLAT"
    MA_ORDER_UNSTABLE = "MA_ORDER_UNSTABLE"
    PRICE_BAND_WHIPSAW = "PRICE_BAND_WHIPSAW"


class DailyReason(StrEnum):
    """Stable reason codes for the 1D classification."""

    DAILY_ALIGNED = "DAILY_ALIGNED"
    DAILY_MIXED = "DAILY_MIXED"
    DAILY_OPPOSITION = "DAILY_OPPOSITION"


class PullbackState(StrEnum):
    """Frozen 1H pullback snapshot state."""

    NONE = "NONE"
    WATCHING = "WATCHING"
    SHALLOW = "SHALLOW"
    STANDARD = "STANDARD"
    DAMAGED = "DAMAGED"
    RESET = "RESET"


class PullbackReason(StrEnum):
    """Stable reason codes for 1H pullback evaluation."""

    MA_DIRECTION_INVALID = "MA_DIRECTION_INVALID"
    PULLBACK_NONE = "PULLBACK_NONE"
    PULLBACK_WATCHING = "PULLBACK_WATCHING"
    PULLBACK_SHALLOW = "PULLBACK_SHALLOW"
    PULLBACK_STANDARD = "PULLBACK_STANDARD"
    PULLBACK_DAMAGED = "PULLBACK_DAMAGED"
    PULLBACK_RESET = "PULLBACK_RESET"
    PULLBACK_ACCELERATING = "PULLBACK_ACCELERATING"
    PULLBACK_EPISODE_NEW = "PULLBACK_EPISODE_NEW"
    PULLBACK_EPISODE_NOT_NEW = "PULLBACK_EPISODE_NOT_NEW"
    ELIGIBLE_FOR_TRIGGER = "ELIGIBLE_FOR_TRIGGER"


class SetupDecisionReason(StrEnum):
    """Stable reason for the final setup evaluation path."""

    FOUR_HOUR_NEUTRAL = "FOUR_HOUR_NEUTRAL"
    FOUR_HOUR_COMPRESSED = "FOUR_HOUR_COMPRESSED"
    DAILY_BLOCK_LONG = "DAILY_BLOCK_LONG"
    DAILY_BLOCK_SHORT = "DAILY_BLOCK_SHORT"
    ONE_HOUR_EVALUATED = "ONE_HOUR_EVALUATED"


class FourHourEvaluation(BaseModel):
    """Point-in-time 4H direction and regime evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = SETUP_CONTEXT_VERSION
    direction: Direction
    current_order: MaOrder
    regime: Regime
    band_width: float = Field(ge=0)
    sma180_change_5: float
    order_change_count: int = Field(ge=0)
    band_flip_count: int = Field(ge=0)
    compression_threshold: float
    compression_reasons: tuple[CompressionReason, ...]

    @field_validator("band_width", "sma180_change_5", "compression_threshold")
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("4H numeric values must be finite")
        return value


class DailyEvaluation(BaseModel):
    """Point-in-time 1D context evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = SETUP_CONTEXT_VERSION
    direction: Direction
    daily_order: MaOrder
    sma180_change_5: float
    context: DailyContext
    reason_codes: tuple[DailyReason, ...]

    @field_validator("sma180_change_5")
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("1D numeric values must be finite")
        return value


class PullbackEvaluation(BaseModel):
    """Point-in-time 1H pullback, acceleration, and episode evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = SETUP_CONTEXT_VERSION
    symbol: str
    direction: Direction
    state: PullbackState
    ma_direction_valid: bool
    accelerating: bool
    episode_id: str | None
    episode_start: datetime | None
    episode_is_new: bool
    eligible_for_trigger: bool
    reason_codes: tuple[PullbackReason, ...]

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return normalize_symbol(value)

    @field_validator("episode_id")
    @classmethod
    def validate_episode_id(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("episode_id must be a lowercase SHA-256 value")
        return value

    @field_validator("episode_start")
    @classmethod
    def validate_episode_start(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("episode_start must be timezone-aware")
        return value


class SetupContextEvaluation(BaseModel):
    """Combined Setup snapshot with explicit lower-timeframe short circuits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = SETUP_CONTEXT_VERSION
    symbol: str
    four_hour: FourHourEvaluation
    daily: DailyEvaluation | None
    one_hour: PullbackEvaluation | None
    eligible_for_trigger: bool
    decision_reason: SetupDecisionReason

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return normalize_symbol(value)


def classify_ma_order(sma30: float, sma60: float, sma90: float, sma180: float) -> MaOrder:
    """Classify one strict moving-average ordering; equality is mixed."""

    if sma30 > sma60 > sma90 > sma180:
        return MaOrder.BULL_ORDER
    if sma30 < sma60 < sma90 < sma180:
        return MaOrder.BEAR_ORDER
    return MaOrder.MIXED_ORDER


def classify_band_position(
    close: float,
    sma30: float,
    sma60: float,
    sma90: float,
    sma180: float,
) -> BandPosition:
    """Classify close against all moving averages; equality is inside."""

    averages = (sma30, sma60, sma90, sma180)
    if close > max(averages):
        return BandPosition.ABOVE_BAND
    if close < min(averages):
        return BandPosition.BELOW_BAND
    return BandPosition.INSIDE_BAND


def _validate_frame_pair(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    *,
    minimum_rows: int,
    indicator_columns: tuple[str, ...],
    candle_columns: tuple[str, ...] = (),
) -> None:
    try:
        validate_indicator_input(candles)
    except IndicatorInputError as error:
        raise SetupInputError(str(error)) from error
    if len(candles) != len(indicators):
        raise SetupInputError("candles and indicators lengths must match")
    if not candles.index.equals(indicators.index):
        raise SetupInputError("candles and indicators indexes must match")
    missing_candles = sorted(set(candle_columns).difference(candles.columns))
    if missing_candles:
        raise SetupInputError(f"missing required candle columns: {missing_candles}")
    missing = sorted(set(indicator_columns).difference(indicators.columns))
    if missing:
        raise SetupInputError(f"missing required indicators: {missing}")
    if len(candles) < minimum_rows:
        raise SetupNotReadyError(f"at least {minimum_rows} closed candles are required")


def _required_values(series: pd.Series, name: str) -> np.ndarray:
    try:
        values = np.asarray(series, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as error:
        raise SetupInputError(f"{name} contains a non-numeric value") from error
    if bool(np.isnan(values).any()):
        raise SetupNotReadyError(f"{name} is not warmed up")
    if not bool(np.isfinite(values).all()):
        raise SetupInputError(f"{name} contains a non-finite value")
    return values


def _normalize_symbol(symbol: str) -> str:
    try:
        return normalize_symbol(symbol)
    except ValueError as error:
        raise SetupInputError(str(error)) from error


def _pullback_state_at(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    position: int,
    direction: Direction,
) -> tuple[PullbackState, bool]:
    candle_values = {
        column: float(_required_values(candles[column].iloc[[position]], column)[0])
        for column in ("high", "low", "close")
    }
    average_values = {
        column: float(_required_values(indicators[column].iloc[[position]], column)[0])
        for column in MA_COLUMNS
    }
    atr14 = float(_required_values(indicators["atr14"].iloc[[position]], "atr14")[0])
    if atr14 <= 0:
        raise SetupInputError("atr14 must be greater than zero")

    if direction is Direction.LONG:
        ma_direction_valid = average_values["sma30"] > average_values["sma60"]
        if not ma_direction_valid:
            return PullbackState.NONE, False
        if candle_values["low"] <= average_values["sma180"]:
            return PullbackState.RESET, True
        if candle_values["close"] < average_values["sma90"]:
            return PullbackState.DAMAGED, True
        if (
            candle_values["low"] <= average_values["sma60"]
            and candle_values["high"] >= average_values["sma90"]
        ):
            return PullbackState.STANDARD, True
        if (
            candle_values["low"] <= average_values["sma30"]
            and candle_values["high"] >= average_values["sma60"]
        ):
            return PullbackState.SHALLOW, True
        distance = candle_values["close"] - average_values["sma30"]
        if candle_values["close"] > average_values["sma30"] and distance <= 0.50 * atr14:
            return PullbackState.WATCHING, True
        return PullbackState.NONE, True

    if direction is Direction.SHORT:
        ma_direction_valid = average_values["sma30"] < average_values["sma60"]
        if not ma_direction_valid:
            return PullbackState.NONE, False
        if candle_values["high"] >= average_values["sma180"]:
            return PullbackState.RESET, True
        if candle_values["close"] > average_values["sma90"]:
            return PullbackState.DAMAGED, True
        if (
            candle_values["low"] <= average_values["sma90"]
            and candle_values["high"] >= average_values["sma60"]
        ):
            return PullbackState.STANDARD, True
        if (
            candle_values["low"] <= average_values["sma60"]
            and candle_values["high"] >= average_values["sma30"]
        ):
            return PullbackState.SHALLOW, True
        distance = average_values["sma30"] - candle_values["close"]
        if candle_values["close"] < average_values["sma30"] and distance <= 0.50 * atr14:
            return PullbackState.WATCHING, True
        return PullbackState.NONE, True

    raise SetupInputError("neutral direction must not enter pullback evaluation")


def _is_pullback_accelerating(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    direction: Direction,
) -> bool:
    latest = len(candles) - 1
    opens = _required_values(candles["open"].iloc[latest - 1 :], "open")
    closes = _required_values(candles["close"].iloc[latest - 1 :], "close")
    volumes = _required_values(candles["quote_volume"].iloc[latest - 21 :], "quote_volume")
    ranges = _required_values(indicators["true_range"].iloc[latest - 1 :], "true_range")
    if bool((ranges < 0).any()):
        raise SetupInputError("true_range must be non-negative")

    adverse = (
        bool((closes < opens).all())
        if direction is Direction.LONG
        else bool((closes > opens).all())
    )
    baseline = float(np.median(volumes[:20]))
    high_volume = bool((volumes[20:] > baseline).all())
    expanding_range = bool(ranges[1] > ranges[0])
    return adverse and high_volume and expanding_range


def _canonical_timestamp(value: object) -> tuple[datetime, str]:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise SetupInputError("episode start open_time must be timezone-aware")
    python_timestamp = cast(datetime, timestamp.to_pydatetime())
    return python_timestamp, python_timestamp.isoformat(timespec="milliseconds")


def _episode_id(symbol: str, direction: Direction, timestamp: str) -> str:
    canonical = "|".join((SETUP_CONTEXT_VERSION, symbol, direction.value, timestamp))
    return hashlib.sha256(canonical.encode()).hexdigest()


def evaluate_four_hour(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    *,
    compression_threshold: float = 0.75,
) -> FourHourEvaluation:
    """Evaluate frozen 4H direction and compression rules at the latest row."""

    if compression_threshold not in ALLOWED_COMPRESSION_THRESHOLDS:
        raise SetupInputError("compression_threshold must be one of 0.5, 0.75, or 1.0")
    _validate_frame_pair(
        candles,
        indicators,
        minimum_rows=12,
        indicator_columns=(*MA_COLUMNS, "atr14"),
    )

    latest = len(candles) - 1
    start = latest - 11
    moving_averages = {
        column: _required_values(indicators[column].iloc[start:], column)
        for column in MA_COLUMNS
    }
    closes = _required_values(candles["close"].iloc[start:], "close")
    atr14 = _required_values(indicators["atr14"].iloc[[latest]], "atr14")[0]
    if atr14 <= 0:
        raise SetupInputError("atr14 must be greater than zero")

    orders = tuple(
        classify_ma_order(
            moving_averages["sma30"][position],
            moving_averages["sma60"][position],
            moving_averages["sma90"][position],
            moving_averages["sma180"][position],
        )
        for position in range(12)
    )
    band_positions = tuple(
        classify_band_position(
            closes[position],
            moving_averages["sma30"][position],
            moving_averages["sma60"][position],
            moving_averages["sma90"][position],
            moving_averages["sma180"][position],
        )
        for position in range(12)
    )

    current_order = orders[-1]
    sma180_change_5 = float(
        moving_averages["sma180"][-1] - moving_averages["sma180"][-6]
    )
    if current_order is MaOrder.BULL_ORDER and sma180_change_5 > 0:
        direction = Direction.LONG
    elif current_order is MaOrder.BEAR_ORDER and sma180_change_5 < 0:
        direction = Direction.SHORT
    else:
        direction = Direction.NEUTRAL

    band_width = float(
        abs(moving_averages["sma30"][-1] - moving_averages["sma180"][-1]) / atr14
    )
    order_change_count = sum(
        left is not right for left, right in zip(orders, orders[1:], strict=False)
    )
    outside_positions = tuple(
        position for position in band_positions if position is not BandPosition.INSIDE_BAND
    )
    band_flip_count = sum(
        left is not right
        for left, right in zip(outside_positions, outside_positions[1:], strict=False)
    )

    reasons: list[CompressionReason] = []
    if band_width < compression_threshold:
        reasons.append(CompressionReason.MA_BAND_NARROW)
    if abs(sma180_change_5) <= 0.10 * atr14:
        reasons.append(CompressionReason.MA180_FLAT)
    if order_change_count >= 3:
        reasons.append(CompressionReason.MA_ORDER_UNSTABLE)
    if band_flip_count >= 2:
        reasons.append(CompressionReason.PRICE_BAND_WHIPSAW)

    return FourHourEvaluation(
        direction=direction,
        current_order=current_order,
        regime=Regime.COMPRESSED if reasons else Regime.TRENDING,
        band_width=band_width,
        sma180_change_5=sma180_change_5,
        order_change_count=order_change_count,
        band_flip_count=band_flip_count,
        compression_threshold=compression_threshold,
        compression_reasons=tuple(reasons),
    )


def evaluate_daily_context(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    *,
    direction: Direction,
) -> DailyEvaluation:
    """Evaluate the frozen 1D context relative to a non-neutral 4H direction."""

    if direction is Direction.NEUTRAL:
        raise SetupInputError("neutral direction must not enter daily evaluation")
    _validate_frame_pair(
        candles,
        indicators,
        minimum_rows=6,
        indicator_columns=MA_COLUMNS,
    )
    latest = len(candles) - 1
    current_values = tuple(
        float(_required_values(indicators[column].iloc[[latest]], column)[0])
        for column in MA_COLUMNS
    )
    sma180_values = _required_values(indicators["sma180"].iloc[latest - 5 :], "sma180")
    sma180_change_5 = float(sma180_values[-1] - sma180_values[0])
    daily_order = classify_ma_order(*current_values)

    aligned = (
        direction is Direction.LONG
        and daily_order is MaOrder.BULL_ORDER
        and sma180_change_5 > 0
    ) or (
        direction is Direction.SHORT
        and daily_order is MaOrder.BEAR_ORDER
        and sma180_change_5 < 0
    )
    opposed = (
        direction is Direction.LONG
        and daily_order is MaOrder.BEAR_ORDER
        and sma180_change_5 < 0
    ) or (
        direction is Direction.SHORT
        and daily_order is MaOrder.BULL_ORDER
        and sma180_change_5 > 0
    )

    if opposed:
        context = (
            DailyContext.BLOCK_LONG
            if direction is Direction.LONG
            else DailyContext.BLOCK_SHORT
        )
        reasons = (DailyReason.DAILY_OPPOSITION,)
    elif aligned:
        context = DailyContext.ALIGNED
        reasons = (DailyReason.DAILY_ALIGNED,)
    else:
        context = DailyContext.MIXED
        reasons = (DailyReason.DAILY_MIXED,)

    return DailyEvaluation(
        direction=direction,
        daily_order=daily_order,
        sma180_change_5=sma180_change_5,
        context=context,
        reason_codes=reasons,
    )


def evaluate_one_hour_pullback(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    *,
    symbol: str,
    direction: Direction,
) -> PullbackEvaluation:
    """Evaluate frozen 1H pullback, acceleration, and episode rules."""

    if direction is Direction.NEUTRAL:
        raise SetupInputError("neutral direction must not enter pullback evaluation")
    normalized_symbol = _normalize_symbol(symbol)
    _validate_frame_pair(
        candles,
        indicators,
        minimum_rows=22,
        indicator_columns=(*MA_COLUMNS, "atr14", "true_range"),
        candle_columns=("open",),
    )

    latest = len(candles) - 1
    state, ma_direction_valid = _pullback_state_at(candles, indicators, latest, direction)
    accelerating = _is_pullback_accelerating(candles, indicators, direction)
    state_reason = PullbackReason[f"PULLBACK_{state.value}"]
    reasons = [state_reason]
    if not ma_direction_valid:
        reasons.insert(0, PullbackReason.MA_DIRECTION_INVALID)
    if accelerating:
        reasons.append(PullbackReason.PULLBACK_ACCELERATING)

    episode_id: str | None = None
    episode_start: datetime | None = None
    episode_is_new = False
    is_pullback = state in (PullbackState.SHALLOW, PullbackState.STANDARD)
    if is_pullback and ma_direction_valid:
        start = latest
        while start > 0:
            previous_state, previous_ma_valid = _pullback_state_at(
                candles, indicators, start - 1, direction
            )
            if not previous_ma_valid or previous_state not in (
                PullbackState.SHALLOW,
                PullbackState.STANDARD,
            ):
                break
            start -= 1

        episode_start, canonical_timestamp = _canonical_timestamp(
            candles["open_time"].iloc[start]
        )
        episode_id = _episode_id(normalized_symbol, direction, canonical_timestamp)
        if start > 0:
            previous_close = float(
                _required_values(candles["close"].iloc[[start - 1]], "close")[0]
            )
            previous_sma30 = float(
                _required_values(indicators["sma30"].iloc[[start - 1]], "sma30")[0]
            )
            episode_is_new = (
                previous_close > previous_sma30
                if direction is Direction.LONG
                else previous_close < previous_sma30
            )
        reasons.append(
            PullbackReason.PULLBACK_EPISODE_NEW
            if episode_is_new
            else PullbackReason.PULLBACK_EPISODE_NOT_NEW
        )

    eligible_for_trigger = (
        is_pullback and ma_direction_valid and episode_is_new and not accelerating
    )
    if eligible_for_trigger:
        reasons.append(PullbackReason.ELIGIBLE_FOR_TRIGGER)

    return PullbackEvaluation(
        symbol=normalized_symbol,
        direction=direction,
        state=state,
        ma_direction_valid=ma_direction_valid,
        accelerating=accelerating,
        episode_id=episode_id,
        episode_start=episode_start,
        episode_is_new=episode_is_new,
        eligible_for_trigger=eligible_for_trigger,
        reason_codes=tuple(reasons),
    )


def evaluate_setup_context(
    *,
    symbol: str,
    four_hour_candles: pd.DataFrame,
    four_hour_indicators: pd.DataFrame,
    daily_candles: pd.DataFrame,
    daily_indicators: pd.DataFrame,
    one_hour_candles: pd.DataFrame,
    one_hour_indicators: pd.DataFrame,
    compression_threshold: float = 0.75,
) -> SetupContextEvaluation:
    """Evaluate Setup in order and leave skipped lower-timeframe results as ``None``."""

    normalized_symbol = _normalize_symbol(symbol)
    four_hour = evaluate_four_hour(
        four_hour_candles,
        four_hour_indicators,
        compression_threshold=compression_threshold,
    )
    if four_hour.direction is Direction.NEUTRAL:
        return SetupContextEvaluation(
            symbol=normalized_symbol,
            four_hour=four_hour,
            daily=None,
            one_hour=None,
            eligible_for_trigger=False,
            decision_reason=SetupDecisionReason.FOUR_HOUR_NEUTRAL,
        )
    if four_hour.regime is Regime.COMPRESSED:
        return SetupContextEvaluation(
            symbol=normalized_symbol,
            four_hour=four_hour,
            daily=None,
            one_hour=None,
            eligible_for_trigger=False,
            decision_reason=SetupDecisionReason.FOUR_HOUR_COMPRESSED,
        )

    daily = evaluate_daily_context(
        daily_candles,
        daily_indicators,
        direction=four_hour.direction,
    )
    if daily.context in (DailyContext.BLOCK_LONG, DailyContext.BLOCK_SHORT):
        decision_reason = (
            SetupDecisionReason.DAILY_BLOCK_LONG
            if daily.context is DailyContext.BLOCK_LONG
            else SetupDecisionReason.DAILY_BLOCK_SHORT
        )
        return SetupContextEvaluation(
            symbol=normalized_symbol,
            four_hour=four_hour,
            daily=daily,
            one_hour=None,
            eligible_for_trigger=False,
            decision_reason=decision_reason,
        )

    one_hour = evaluate_one_hour_pullback(
        one_hour_candles,
        one_hour_indicators,
        symbol=normalized_symbol,
        direction=four_hour.direction,
    )
    return SetupContextEvaluation(
        symbol=normalized_symbol,
        four_hour=four_hour,
        daily=daily,
        one_hour=one_hour,
        eligible_for_trigger=one_hour.eligible_for_trigger,
        decision_reason=SetupDecisionReason.ONE_HOUR_EVALUATED,
    )
