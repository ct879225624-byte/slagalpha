"""Deterministic 15m trigger evaluation over an accepted Setup context."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self, cast

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from slagalpha.domain.symbols import normalize_symbol
from slagalpha.strategy.indicators import IndicatorInputError, validate_indicator_input
from slagalpha.strategy.pivots import PivotEvent, PivotType, PivotZone
from slagalpha.strategy.setup import Direction, PullbackState

TRIGGER_VERSION = "trigger/0.1.0"
STRATEGY_VERSION = "ma-trend-pullback/0.1.0"
FIFTEEN_MINUTES = timedelta(minutes=15)


class TriggerError(ValueError):
    """Base class for trigger evaluation failures."""


class TriggerInputError(TriggerError):
    """Raised when trigger input is invalid."""


class TriggerNotReadyError(TriggerError):
    """Raised when valid trigger input lacks required history."""


class TriggerType(StrEnum):
    """Frozen V0.1 trigger types."""

    MA_RECLAIM = "MA_RECLAIM"
    SWEEP_RECLAIM = "SWEEP_RECLAIM"


class TriggerReason(StrEnum):
    """Stable P5 trigger reason codes."""

    NO_VALID_EPISODE = "NO_VALID_EPISODE"
    NO_MA_PULLBACK_TOUCH = "NO_MA_PULLBACK_TOUCH"
    MA_RECLAIM_FAILED = "MA_RECLAIM_FAILED"
    PREVIOUS_BAR_BREAK_FAILED = "PREVIOUS_BAR_BREAK_FAILED"
    ZERO_RANGE_TRIGGER = "ZERO_RANGE_TRIGGER"
    TRIGGER_VOLUME_LOW = "TRIGGER_VOLUME_LOW"
    TRIGGER_VOLUME_NOT_EXPANDING = "TRIGGER_VOLUME_NOT_EXPANDING"
    VOLUME_WITH_ADVERSE_CLOSE = "VOLUME_WITH_ADVERSE_CLOSE"
    PULLBACK_VOLUME_NOT_CONTRACTING = "PULLBACK_VOLUME_NOT_CONTRACTING"
    STOP_STRUCTURE_MISSING = "STOP_STRUCTURE_MISSING"
    TRIGGER_A_CONFIRMED = "TRIGGER_A_CONFIRMED"
    SWING_ZONE_MISSING = "SWING_ZONE_MISSING"
    SWEEP_NOT_BEYOND_ZONE = "SWEEP_NOT_BEYOND_ZONE"
    SWEEP_RECLAIM_FAILED = "SWEEP_RECLAIM_FAILED"
    TRIGGER_B_CONFIRMED = "TRIGGER_B_CONFIRMED"


class TriggerSetupContext(BaseModel):
    """Frozen P4 evidence required by a 15m trigger evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    direction: Direction
    pullback_state: PullbackState
    eligible_for_trigger: bool
    episode_id: str
    episode_start: datetime
    region_lower: float
    region_upper: float

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        return normalize_symbol(value)

    @field_validator("episode_id")
    @classmethod
    def validate_episode_id(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("episode_id must be a lowercase SHA-256 value")
        return value

    @field_validator("episode_start")
    @classmethod
    def validate_episode_start(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("episode_start must be timezone-aware")
        return value

    @field_validator("region_lower", "region_upper")
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("pullback region bounds must be finite")
        return value

    @model_validator(mode="after")
    def validate_region(self) -> Self:
        if self.direction is Direction.NEUTRAL:
            raise ValueError("trigger direction must be LONG or SHORT")
        if self.region_lower > self.region_upper:
            raise ValueError("region_lower must not exceed region_upper")
        return self


class VolumeEvidence(BaseModel):
    """Auditable volume and close-location evidence for one candidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pullback_mean: float
    pullback_baseline: float
    trigger_volume: float = Field(ge=0)
    trigger_baseline: float = Field(ge=0)
    previous_volume: float = Field(ge=0)
    close_location: float | None
    trigger_volume_high: bool
    trigger_volume_expanding: bool
    close_location_favorable: bool
    pullback_contracting: bool
    reason_codes: tuple[TriggerReason, ...]

    @field_validator(
        "pullback_mean",
        "pullback_baseline",
        "trigger_volume",
        "trigger_baseline",
        "previous_volume",
        "close_location",
    )
    @classmethod
    def validate_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("volume evidence values must be finite")
        return value


class TriggerAEvidence(BaseModel):
    """Condition evidence specific to MA Reclaim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pullback_touch: bool
    current_sma30: float
    previous_break_level: float
    ma_reclaimed: bool
    previous_bar_broken: bool
    volume: VolumeEvidence
    structure_pivot_id: str | None
    structure_price: float | None

    @field_validator("current_sma30", "previous_break_level", "structure_price")
    @classmethod
    def validate_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("Trigger A evidence values must be finite")
        return value


class TriggerAEvaluation(BaseModel):
    """One deterministic Trigger A evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = TRIGGER_VERSION
    trigger_type: TriggerType
    symbol: str
    direction: Direction
    episode_id: str
    confirmation_open_time: datetime
    confirmation_close_time: datetime
    confirmed: bool
    reason_codes: tuple[TriggerReason, ...]
    invalidation_price: float | None
    evidence: TriggerAEvidence

    @field_validator("confirmation_open_time", "confirmation_close_time")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("confirmation timestamps must be timezone-aware")
        return value


class TriggerBEvidence(BaseModel):
    """Condition evidence specific to Sweep Reclaim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    current_sma30: float
    selected_zone_id: str | None
    zone_lower: float | None
    zone_upper: float | None
    sweep_extreme: float
    swept_beyond_zone: bool | None
    zone_reclaimed: bool | None
    ma_reclaimed: bool
    volume: VolumeEvidence

    @field_validator("current_sma30", "zone_lower", "zone_upper", "sweep_extreme")
    @classmethod
    def validate_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("Trigger B evidence values must be finite")
        return value


class TriggerBEvaluation(BaseModel):
    """One deterministic Trigger B evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = TRIGGER_VERSION
    trigger_type: TriggerType
    symbol: str
    direction: Direction
    episode_id: str
    confirmation_open_time: datetime
    confirmation_close_time: datetime
    confirmed: bool
    reason_codes: tuple[TriggerReason, ...]
    invalidation_price: float | None
    evidence: TriggerBEvidence

    @field_validator("confirmation_open_time", "confirmation_close_time")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("confirmation timestamps must be timezone-aware")
        return value


class TriggerDecision(BaseModel):
    """Merged logical trigger result for one Setup and confirmation candle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = TRIGGER_VERSION
    strategy_version: str = STRATEGY_VERSION
    symbol: str
    direction: Direction
    episode_id: str
    confirmation_open_time: datetime
    confirmation_close_time: datetime
    trigger_a: TriggerAEvaluation
    trigger_b: TriggerBEvaluation | None
    primary_trigger: TriggerType | None
    all_triggers: tuple[TriggerType, ...]
    logical_signal_id: str | None
    eligible_for_plan: bool

    @field_validator("logical_signal_id")
    @classmethod
    def validate_signal_id(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("logical_signal_id must be a lowercase SHA-256 value")
        return value


def _required_values(series: pd.Series, name: str) -> np.ndarray:
    try:
        values = np.asarray(series, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as error:
        raise TriggerInputError(f"{name} contains a non-numeric value") from error
    if bool(np.isnan(values).any()):
        raise TriggerNotReadyError(f"{name} is not warmed up")
    if not bool(np.isfinite(values).all()):
        raise TriggerInputError(f"{name} contains a non-finite value")
    return values


def _timestamp_at(values: pd.Series, position: int, name: str) -> datetime:
    timestamp = pd.Timestamp(values.iloc[position])
    if timestamp.tzinfo is None:
        raise TriggerInputError(f"{name} must be timezone-aware")
    return cast(datetime, timestamp.to_pydatetime())


def _validate_trigger_frames(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    *,
    minimum_rows: int = 24,
) -> None:
    try:
        validate_indicator_input(candles)
    except IndicatorInputError as error:
        raise TriggerInputError(str(error)) from error
    if "close_time_exclusive" not in candles:
        raise TriggerInputError("missing required candle column: close_time_exclusive")
    if len(candles) != len(indicators):
        raise TriggerInputError("candles and indicators lengths must match")
    if not candles.index.equals(indicators.index):
        raise TriggerInputError("candles and indicators indexes must match")
    required_indicators = {
        "sma30",
        "quote_volume_previous",
        "quote_volume_mean3_prev",
        "quote_volume_median20_prev",
        "quote_volume_median20_before_prev3",
    }
    missing = sorted(required_indicators.difference(indicators.columns))
    if missing:
        raise TriggerInputError(f"missing required indicators: {missing}")
    if len(candles) < minimum_rows:
        raise TriggerNotReadyError(
            f"at least {minimum_rows} closed 15m candles are required"
        )

    open_times = pd.DatetimeIndex(candles["open_time"])
    close_times = pd.DatetimeIndex(candles["close_time_exclusive"])
    if close_times.tz is None:
        raise TriggerInputError("close_time_exclusive must be timezone-aware")
    expected_closes = open_times + pd.Timedelta(minutes=15)
    if not close_times.equals(expected_closes):
        raise TriggerInputError("each 15m candle must have an exact 15-minute interval")
    if len(open_times) > 1 and not bool(
        (open_times[1:] - open_times[:-1] == pd.Timedelta(FIFTEEN_MINUTES)).all()
    ):
        raise TriggerInputError("15m candles must use a complete 15-minute grid")


def _volume_evidence(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    direction: Direction,
) -> VolumeEvidence:
    latest = len(candles) - 1
    high, low, close, trigger_volume = (
        float(_required_values(candles[column].iloc[[latest]], column)[0])
        for column in ("high", "low", "close", "quote_volume")
    )
    previous_volume = float(
        _required_values(indicators["quote_volume_previous"].iloc[[latest]], "previous volume")[
            0
        ]
    )
    pullback_mean = float(
        _required_values(
            indicators["quote_volume_mean3_prev"].iloc[[latest]], "pullback mean"
        )[0]
    )
    pullback_baseline = float(
        _required_values(
            indicators["quote_volume_median20_before_prev3"].iloc[[latest]],
            "pullback baseline",
        )[0]
    )
    trigger_baseline = float(
        _required_values(
            indicators["quote_volume_median20_prev"].iloc[[latest]], "trigger baseline"
        )[0]
    )

    reasons: list[TriggerReason] = []
    close_location: float | None
    close_location_favorable = False
    if high == low:
        close_location = None
        reasons.append(TriggerReason.ZERO_RANGE_TRIGGER)
    else:
        close_location = (close - low) / (high - low)
        close_location_favorable = (
            close_location >= 0.65
            if direction is Direction.LONG
            else close_location <= 0.35
        )

    trigger_volume_high = trigger_volume > trigger_baseline
    trigger_volume_expanding = trigger_volume > previous_volume
    pullback_contracting = pullback_mean < pullback_baseline
    if not trigger_volume_high:
        reasons.append(TriggerReason.TRIGGER_VOLUME_LOW)
    if not trigger_volume_expanding:
        reasons.append(TriggerReason.TRIGGER_VOLUME_NOT_EXPANDING)
    if close_location is not None and not close_location_favorable:
        reasons.append(TriggerReason.VOLUME_WITH_ADVERSE_CLOSE)

    return VolumeEvidence(
        pullback_mean=pullback_mean,
        pullback_baseline=pullback_baseline,
        trigger_volume=trigger_volume,
        trigger_baseline=trigger_baseline,
        previous_volume=previous_volume,
        close_location=close_location,
        trigger_volume_high=trigger_volume_high,
        trigger_volume_expanding=trigger_volume_expanding,
        close_location_favorable=close_location_favorable,
        pullback_contracting=pullback_contracting,
        reason_codes=tuple(reasons),
    )


def _select_trigger_a_pivot(
    pivots: tuple[PivotEvent, ...],
    setup: TriggerSetupContext,
    confirmation_close: datetime,
) -> PivotEvent | None:
    required_kind = PivotType.LOW if setup.direction is Direction.LONG else PivotType.HIGH
    available = (
        pivot
        for pivot in pivots
        if pivot.interval.lower() == "15m"
        and pivot.kind is required_kind
        and pivot.pivot_time >= setup.episode_start
        and pivot.confirmed_at <= confirmation_close
    )
    return max(
        available,
        key=lambda pivot: (pivot.confirmed_at, pivot.pivot_time, pivot.pivot_id),
        default=None,
    )


def evaluate_trigger_a(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    *,
    setup: TriggerSetupContext,
    pivots: tuple[PivotEvent, ...],
) -> TriggerAEvaluation:
    """Evaluate V0.1 MA Reclaim against the latest closed 15m candle."""

    _validate_trigger_frames(candles, indicators)
    latest = len(candles) - 1
    confirmation_open = _timestamp_at(candles["open_time"], latest, "open_time")
    confirmation_close = _timestamp_at(
        candles["close_time_exclusive"], latest, "close_time_exclusive"
    )
    sma30 = float(_required_values(indicators["sma30"].iloc[latest - 4 :], "sma30")[-1])
    previous_sma30 = _required_values(indicators["sma30"].iloc[latest - 4 : latest], "sma30")
    previous_close = _required_values(candles["close"].iloc[latest - 4 : latest], "close")
    previous_high = _required_values(candles["high"].iloc[latest - 4 : latest], "high")
    previous_low = _required_values(candles["low"].iloc[latest - 4 : latest], "low")
    candidate_close = float(_required_values(candles["close"].iloc[[latest]], "close")[0])

    setup_valid = (
        setup.eligible_for_trigger
        and setup.pullback_state in (PullbackState.SHALLOW, PullbackState.STANDARD)
    )
    below_or_above_sma = (
        previous_close < previous_sma30
        if setup.direction is Direction.LONG
        else previous_close > previous_sma30
    )
    overlaps_region = (previous_low <= setup.region_upper) & (
        previous_high >= setup.region_lower
    )
    pullback_touch = bool((below_or_above_sma | overlaps_region).any())
    ma_reclaimed = (
        candidate_close > sma30
        if setup.direction is Direction.LONG
        else candidate_close < sma30
    )
    previous_break_level = (
        float(previous_high[-1])
        if setup.direction is Direction.LONG
        else float(previous_low[-1])
    )
    previous_bar_broken = (
        candidate_close > previous_break_level
        if setup.direction is Direction.LONG
        else candidate_close < previous_break_level
    )
    volume = _volume_evidence(candles, indicators, setup.direction)
    pivot = _select_trigger_a_pivot(pivots, setup, confirmation_close)

    reasons: list[TriggerReason] = []
    if not setup_valid:
        reasons.append(TriggerReason.NO_VALID_EPISODE)
    if not pullback_touch:
        reasons.append(TriggerReason.NO_MA_PULLBACK_TOUCH)
    if not ma_reclaimed:
        reasons.append(TriggerReason.MA_RECLAIM_FAILED)
    if not previous_bar_broken:
        reasons.append(TriggerReason.PREVIOUS_BAR_BREAK_FAILED)
    reasons.extend(volume.reason_codes)
    if not volume.pullback_contracting:
        reasons.append(TriggerReason.PULLBACK_VOLUME_NOT_CONTRACTING)
    if pivot is None:
        reasons.append(TriggerReason.STOP_STRUCTURE_MISSING)

    confirmed = not reasons
    if confirmed:
        reasons.append(TriggerReason.TRIGGER_A_CONFIRMED)
    return TriggerAEvaluation(
        trigger_type=TriggerType.MA_RECLAIM,
        symbol=setup.symbol,
        direction=setup.direction,
        episode_id=setup.episode_id,
        confirmation_open_time=confirmation_open,
        confirmation_close_time=confirmation_close,
        confirmed=confirmed,
        reason_codes=tuple(reasons),
        invalidation_price=pivot.price if pivot is not None else None,
        evidence=TriggerAEvidence(
            pullback_touch=pullback_touch,
            current_sma30=sma30,
            previous_break_level=previous_break_level,
            ma_reclaimed=ma_reclaimed,
            previous_bar_broken=previous_bar_broken,
            volume=volume,
            structure_pivot_id=pivot.pivot_id if pivot is not None else None,
            structure_price=pivot.price if pivot is not None else None,
        ),
    )


def _zone_distance(price: float, zone: PivotZone) -> float:
    if zone.lower <= price <= zone.upper:
        return 0.0
    return min(abs(price - zone.lower), abs(price - zone.upper))


def _zone_is_destroyed(
    zone: PivotZone,
    candles: pd.DataFrame,
    direction: Direction,
    candidate_open: datetime,
) -> bool:
    close_times = pd.DatetimeIndex(candles["close_time_exclusive"].iloc[:-1])
    relevant = (close_times > zone.confirmed_at) & (close_times <= candidate_open)
    closes = np.asarray(candles["close"].iloc[:-1], dtype=np.float64)[relevant]
    if direction is Direction.LONG:
        return bool((closes < zone.lower).any())
    return bool((closes > zone.upper).any())


def _select_trigger_b_zone(
    zones: tuple[PivotZone, ...],
    candles: pd.DataFrame,
    setup: TriggerSetupContext,
    candidate_open: datetime,
) -> PivotZone | None:
    required_kind = PivotType.LOW if setup.direction is Direction.LONG else PivotType.HIGH
    latest = len(candles) - 1
    lookback_start = _timestamp_at(candles["open_time"], latest - 96, "open_time")
    previous_close = float(_required_values(candles["close"].iloc[[latest - 1]], "close")[0])
    available = [
        zone
        for zone in zones
        if zone.interval.lower() == "15m"
        and zone.kind is required_kind
        and zone.confirmed_at <= candidate_open
        and lookback_start <= zone.members[-1].pivot_time < candidate_open
        and not _zone_is_destroyed(zone, candles, setup.direction, candidate_open)
    ]
    available.sort(key=lambda zone: zone.zone_id)
    available.sort(key=lambda zone: _zone_distance(previous_close, zone))
    available.sort(key=lambda zone: zone.confirmed_at, reverse=True)
    return available[0] if available else None


def evaluate_trigger_b(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    *,
    setup: TriggerSetupContext,
    zones: tuple[PivotZone, ...],
) -> TriggerBEvaluation:
    """Evaluate V0.1 Liquidity Sweep + MA Reclaim at the latest closed candle."""

    _validate_trigger_frames(candles, indicators, minimum_rows=97)
    latest = len(candles) - 1
    confirmation_open = _timestamp_at(candles["open_time"], latest, "open_time")
    confirmation_close = _timestamp_at(
        candles["close_time_exclusive"], latest, "close_time_exclusive"
    )
    high, low, close = (
        float(_required_values(candles[column].iloc[[latest]], column)[0])
        for column in ("high", "low", "close")
    )
    sma30 = float(_required_values(indicators["sma30"].iloc[[latest]], "sma30")[0])
    setup_valid = (
        setup.eligible_for_trigger
        and setup.pullback_state in (PullbackState.SHALLOW, PullbackState.STANDARD)
    )
    zone = _select_trigger_b_zone(zones, candles, setup, confirmation_open)
    swept_beyond_zone: bool | None = None
    zone_reclaimed: bool | None = None
    if zone is not None:
        if setup.direction is Direction.LONG:
            swept_beyond_zone = low < zone.lower
            zone_reclaimed = close > zone.upper
        else:
            swept_beyond_zone = high > zone.upper
            zone_reclaimed = close < zone.lower
    ma_reclaimed = close > sma30 if setup.direction is Direction.LONG else close < sma30
    volume = _volume_evidence(candles, indicators, setup.direction)

    reasons: list[TriggerReason] = []
    if not setup_valid:
        reasons.append(TriggerReason.NO_VALID_EPISODE)
    if zone is None:
        reasons.append(TriggerReason.SWING_ZONE_MISSING)
    else:
        if not swept_beyond_zone:
            reasons.append(TriggerReason.SWEEP_NOT_BEYOND_ZONE)
        if not zone_reclaimed:
            reasons.append(TriggerReason.SWEEP_RECLAIM_FAILED)
    if not ma_reclaimed:
        reasons.append(TriggerReason.MA_RECLAIM_FAILED)
    reasons.extend(volume.reason_codes)

    confirmed = not reasons
    if confirmed:
        reasons.append(TriggerReason.TRIGGER_B_CONFIRMED)
    sweep_extreme = low if setup.direction is Direction.LONG else high
    return TriggerBEvaluation(
        trigger_type=TriggerType.SWEEP_RECLAIM,
        symbol=setup.symbol,
        direction=setup.direction,
        episode_id=setup.episode_id,
        confirmation_open_time=confirmation_open,
        confirmation_close_time=confirmation_close,
        confirmed=confirmed,
        reason_codes=tuple(reasons),
        invalidation_price=sweep_extreme if confirmed else None,
        evidence=TriggerBEvidence(
            current_sma30=sma30,
            selected_zone_id=zone.zone_id if zone is not None else None,
            zone_lower=zone.lower if zone is not None else None,
            zone_upper=zone.upper if zone is not None else None,
            sweep_extreme=sweep_extreme,
            swept_beyond_zone=swept_beyond_zone,
            zone_reclaimed=zone_reclaimed,
            ma_reclaimed=ma_reclaimed,
            volume=volume,
        ),
    )


def _logical_signal_id(
    symbol: str,
    direction: Direction,
    episode_id: str,
    confirmation_open_time: datetime,
) -> str:
    timestamp = confirmation_open_time.isoformat(timespec="milliseconds")
    canonical = "|".join(
        (
            TRIGGER_VERSION,
            STRATEGY_VERSION,
            symbol,
            direction.value,
            episode_id,
            timestamp,
        )
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def merge_trigger_evaluations(
    trigger_a: TriggerAEvaluation,
    trigger_b: TriggerBEvaluation | None,
) -> TriggerDecision:
    """Merge A/B without creating duplicate logical signals for one candle."""

    if trigger_a.trigger_type is not TriggerType.MA_RECLAIM:
        raise TriggerInputError("trigger_a must be an MA_RECLAIM evaluation")
    if trigger_b is not None:
        if trigger_b.trigger_type is not TriggerType.SWEEP_RECLAIM:
            raise TriggerInputError("trigger_b must be a SWEEP_RECLAIM evaluation")
        comparable_a = (
            trigger_a.symbol,
            trigger_a.direction,
            trigger_a.episode_id,
            trigger_a.confirmation_open_time,
            trigger_a.confirmation_close_time,
        )
        comparable_b = (
            trigger_b.symbol,
            trigger_b.direction,
            trigger_b.episode_id,
            trigger_b.confirmation_open_time,
            trigger_b.confirmation_close_time,
        )
        if comparable_a != comparable_b:
            raise TriggerInputError("A/B evaluations must describe the same Setup and candle")

    confirmed_a = trigger_a.confirmed
    confirmed_b = trigger_b is not None and trigger_b.confirmed
    primary_trigger: TriggerType | None
    all_triggers: tuple[TriggerType, ...]
    if confirmed_b:
        primary_trigger = TriggerType.SWEEP_RECLAIM
        all_triggers = (
            (TriggerType.SWEEP_RECLAIM, TriggerType.MA_RECLAIM)
            if confirmed_a
            else (TriggerType.SWEEP_RECLAIM,)
        )
    elif confirmed_a:
        primary_trigger = TriggerType.MA_RECLAIM
        all_triggers = (TriggerType.MA_RECLAIM,)
    else:
        primary_trigger = None
        all_triggers = ()

    eligible_for_plan = primary_trigger is not None
    signal_id = (
        _logical_signal_id(
            trigger_a.symbol,
            trigger_a.direction,
            trigger_a.episode_id,
            trigger_a.confirmation_open_time,
        )
        if eligible_for_plan
        else None
    )
    return TriggerDecision(
        symbol=trigger_a.symbol,
        direction=trigger_a.direction,
        episode_id=trigger_a.episode_id,
        confirmation_open_time=trigger_a.confirmation_open_time,
        confirmation_close_time=trigger_a.confirmation_close_time,
        trigger_a=trigger_a,
        trigger_b=trigger_b,
        primary_trigger=primary_trigger,
        all_triggers=all_triggers,
        logical_signal_id=signal_id,
        eligible_for_plan=eligible_for_plan,
    )


def evaluate_triggers(
    candles: pd.DataFrame,
    indicators: pd.DataFrame,
    *,
    setup: TriggerSetupContext,
    pivots: tuple[PivotEvent, ...],
    zones: tuple[PivotZone, ...],
) -> TriggerDecision:
    """Evaluate both triggers; unavailable B history does not erase a valid A."""

    trigger_a = evaluate_trigger_a(candles, indicators, setup=setup, pivots=pivots)
    try:
        trigger_b = evaluate_trigger_b(candles, indicators, setup=setup, zones=zones)
    except TriggerNotReadyError:
        trigger_b = None
    return merge_trigger_evaluations(trigger_a, trigger_b)
